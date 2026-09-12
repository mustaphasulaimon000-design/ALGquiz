import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
BOT_TOKEN = "8887839950:AAHszfiEiEvm_V7isrejAi6-kMEQmAqtvf4"   # get this from @BotFather
ANSWER_WINDOW_SECONDS = 10
GAME_LENGTH = 5                      # questions per game
STATE_FILE = "quiz_data.json"        # each group's remaining question pool + game progress
PENDING_FILE = "pending_questions.json"  # questions admins have DMed in, not yet loaded into a group
TEAMS_FILE = "teams.json"            # teams, members, scores per group

# Telegram user IDs allowed to add questions via DM / create teams / start games.
# A DM has no group attached, so there's no "group admin" to check there —
# add your own ID (and any other quizmasters') here. Send /whoami to see your ID.
ADMIN_USER_IDS = {8913859431, 8475867602, 868597186, 6480741267}   # <-- replace with your real Telegram user ID(s)


# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------
@dataclass
class QuizState:
    questions: list = field(default_factory=list)   # remaining pool: list of (question, answer)
    game_active: bool = False
    questions_asked_this_game: int = 0
    round_seq: int = 0                    # increments every question, used to match timers
    current_round: Optional[int] = None   # round_seq of the currently active question
    round_open: bool = False              # True from question posted until resolved
    last_answer: Optional[str] = None
    window_start: float = 0.0
    submissions: list = field(default_factory=list)   # (order, user_name, text, elapsed_seconds)
    active_teams: Optional[set] = None    # team names playing this game; None = all registered teams


quiz_states: dict[int, QuizState] = {}          # chat_id -> QuizState (one per group)
pending_questions: list = []                    # shared pool DMed in by any admin, not yet loaded
teams: dict[int, dict[str, dict]] = {}          # chat_id -> team_name -> {"score": int, "members": set(user_id)}


def get_state(chat_id: int) -> QuizState:
    if chat_id not in quiz_states:
        quiz_states[chat_id] = QuizState()
    return quiz_states[chat_id]


def get_teams(chat_id: int) -> dict:
    return teams.setdefault(chat_id, {})


def find_team_of_user(chat_id: int, user_id: int) -> Optional[str]:
    for name, info in get_teams(chat_id).items():
        if user_id in info["members"]:
            return name
    return None


def format_scoreboard(chat_id: int, only_teams: Optional[set] = None) -> str:
    chat_teams = get_teams(chat_id)
    if only_teams is not None:
        chat_teams = {name: info for name, info in chat_teams.items() if name in only_teams}
    if not chat_teams:
        return "(no teams yet)"
    ordered = sorted(chat_teams.items(), key=lambda kv: kv[1]["score"], reverse=True)
    return "\n".join(f"{name}: {info['score']} pt(s)" for name, info in ordered)


# ---------------------------------------------------------------------------
# PERSISTENCE
# ---------------------------------------------------------------------------
def save_all_state():
    data = {
        str(chat_id): {
            "questions": state.questions,
            "game_active": state.game_active,
            "questions_asked_this_game": state.questions_asked_this_game,
        }
        for chat_id, state in quiz_states.items()
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_all_state():
    if not os.path.exists(STATE_FILE):
        return
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    for chat_id_str, saved in data.items():
        state = get_state(int(chat_id_str))
        state.questions = [tuple(q) for q in saved.get("questions", [])]
        state.game_active = saved.get("game_active", False)
        state.questions_asked_this_game = saved.get("questions_asked_this_game", 0)


def save_pending():
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(pending_questions, f, indent=2, ensure_ascii=False)


def load_pending():
    global pending_questions
    if not os.path.exists(PENDING_FILE):
        return
    with open(PENDING_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    pending_questions = [tuple(q) for q in data]


def save_teams():
    data = {
        str(chat_id): {
            name: {"score": info["score"], "members": list(info["members"])}
            for name, info in chat_teams.items()
        }
        for chat_id, chat_teams in teams.items()
    }
    with open(TEAMS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_teams():
    if not os.path.exists(TEAMS_FILE):
        return
    with open(TEAMS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    for chat_id_str, chat_teams in data.items():
        teams[int(chat_id_str)] = {
            name: {"score": info["score"], "members": set(info["members"])}
            for name, info in chat_teams.items()
        }


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Group-admin check. In DMs (no group to check) this always returns True —
    use is_dm_admin for DM-only actions instead."""
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        return True
    member = await context.bot.get_chat_member(chat.id, user.id)
    return member.status in ("administrator", "creator")


def is_dm_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Your Telegram user ID is: {update.effective_user.id}\n"
        "Ask whoever runs the bot to add this to ADMIN_USER_IDS if you should be able to add questions."
    )


async def send_next_button(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    state = get_state(chat_id)
    if not state.questions:
        await context.bot.send_message(
            chat_id, "No questions left in the pool. An admin can DM me more with /addq."
        )
        return
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("▶️ Post next question", callback_data="post_question")]]
    )
    await context.bot.send_message(
        chat_id,
        f"Question {state.questions_asked_this_game}/{GAME_LENGTH} done. Ready for the next one?",
        reply_markup=keyboard,
    )


async def advance_or_end_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    state = get_state(chat_id)
    if state.questions_asked_this_game >= GAME_LENGTH:
        await end_game(chat_id, context)
    else:
        await send_next_button(chat_id, context)


async def end_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    state = get_state(chat_id)
    state.game_active = False
    save_all_state()

    chat_teams = get_teams(chat_id)
    if state.active_teams is not None:
        chat_teams = {name: info for name, info in chat_teams.items() if name in state.active_teams}
    scoreboard = format_scoreboard(chat_id, state.active_teams)
    if not chat_teams:
        await context.bot.send_message(chat_id, "🏁 Game over!")
        return

    max_score = max(info["score"] for info in chat_teams.values())
    winners = [name for name, info in chat_teams.items() if info["score"] == max_score]
    winner_line = f"🏆 Winner: {winners[0]}!" if len(winners) == 1 else f"🏆 It's a tie: {', '.join(winners)}!"

    await context.bot.send_message(
        chat_id,
        f"🏁 Game over! Final scores:\n{scoreboard}\n\n{winner_line}\n\n"
        "Run /startquiz to start a new game (scores will reset).",
    )


# ---------------------------------------------------------------------------
# TEAM COMMANDS (group only)
# ---------------------------------------------------------------------------
async def answers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Group only: show the bot's own log of messages sent during the current/last question —
    useful if an answer gets edited or deleted, since this log was taken at the moment it arrived."""
    if update.effective_chat.type == "private":
        return
    chat_id = update.effective_chat.id
    state = quiz_states.get(chat_id)
    if not state or not state.submissions:
        await update.message.reply_text("No messages logged for the current question yet.")
        return
    lines = [f"{n}. {name}: {text}  ({t}s)" for n, name, text, t in state.submissions]
    await update.message.reply_text("Messages received so far, in order:\n" + "\n".join(lines))


def build_team_keyboard(chat_teams: dict) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(name, callback_data=f"jointeam:{name}") for name in chat_teams]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]  # 2 buttons per row
    return InlineKeyboardMarkup(rows)


async def teamlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Group only: show every team as a tappable button — no typing required to join."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("Run this inside the group.")
        return
    chat_teams = get_teams(update.effective_chat.id)
    if not chat_teams:
        await update.message.reply_text("No teams yet. An admin can create one with /createteam Name.")
        return
    await update.message.reply_text("Tap a team to join:", reply_markup=build_team_keyboard(chat_teams))


async def jointeam_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    team_name = query.data.split(":", 1)[1]

    chat_teams = get_teams(chat_id)
    if team_name not in chat_teams:
        await query.answer("That team no longer exists.", show_alert=True)
        return

    user_id = query.from_user.id
    for info in chat_teams.values():
        info["members"].discard(user_id)
    chat_teams[team_name]["members"].add(user_id)
    save_teams()

    await query.answer(f"You're now on {team_name}!")


async def welcome_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires when someone joins the group — greets them with tappable team buttons."""
    if not update.message or not update.message.new_chat_members:
        return
    chat_id = update.effective_chat.id
    chat_teams = get_teams(chat_id)
    if not chat_teams:
        return  # no teams set up yet, nothing to offer
    keyboard = build_team_keyboard(chat_teams)
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        await context.bot.send_message(
            chat_id, f"👋 Welcome, {member.first_name}! Tap a team to join:", reply_markup=keyboard
        )


async def createteam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("Run this inside the group.")
        return
    if not await is_admin(update, context):
        await update.message.reply_text("Only admins can create teams.")
        return

    name = update.message.text.partition(" ")[2].strip()
    if not name:
        await update.message.reply_text("Usage: /createteam Team Name")
        return

    chat_id = update.effective_chat.id
    chat_teams = get_teams(chat_id)
    if any(existing.lower() == name.lower() for existing in chat_teams):
        await update.message.reply_text(f"Team '{name}' already exists.")
        return

    chat_teams[name] = {"score": 0, "members": set()}
    save_teams()
    await update.message.reply_text(f"Team '{name}' created. Members can join with /jointeam {name}")


async def jointeam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("Run this inside the group.")
        return

    name = update.message.text.partition(" ")[2].strip()
    if not name:
        await update.message.reply_text("Usage: /jointeam Team Name")
        return

    chat_id = update.effective_chat.id
    chat_teams = get_teams(chat_id)
    match = next((t for t in chat_teams if t.lower() == name.lower()), None)
    if not match:
        await update.message.reply_text("No such team. Use /teams to see the list.")
        return

    user_id = update.effective_user.id
    for info in chat_teams.values():
        info["members"].discard(user_id)
    chat_teams[match]["members"].add(user_id)
    save_teams()
    await update.message.reply_text(f"You're now on team {match}.")


async def myteam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("Run this inside the group.")
        return
    team = find_team_of_user(update.effective_chat.id, update.effective_user.id)
    if team:
        await update.message.reply_text(f"You're on team {team}.")
    else:
        await update.message.reply_text("You're not on a team yet. Use /teams to see the list, then /jointeam Name.")


async def teams_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("Run this inside the group.")
        return
    chat_teams = get_teams(update.effective_chat.id)
    if not chat_teams:
        await update.message.reply_text("No teams yet. An admin can create one with /createteam Name.")
        return
    lines = [f"{name}: {info['score']} pt(s), {len(info['members'])} member(s)" for name, info in chat_teams.items()]
    await update.message.reply_text("Teams:\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# QUESTION-POOL COMMANDS — used in DM (private chat) with the bot
# ---------------------------------------------------------------------------
async def addq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage (DM only, admins only): /addq Question text | Answer text"""
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "Please DM me your questions instead of posting them in the group "
            "(that way the group doesn't see the answer early). Tap my name and send /addq there."
        )
        return

    if not is_dm_admin(update.effective_user.id):
        await update.message.reply_text(
            "Only admins can add questions. Send /whoami to get your user ID if you should be added."
        )
        return

    text = update.message.text.partition(" ")[2]
    if "|" not in text:
        await update.message.reply_text("Usage:\n/addq Question text | Answer text")
        return

    question, answer = (part.strip() for part in text.split("|", 1))
    if not question or not answer:
        await update.message.reply_text("Both a question and an answer are required.")
        return

    pending_questions.append((question, answer))
    save_pending()
    await update.message.reply_text(
        f"Saved. There are now {len(pending_questions)} question(s) waiting to be loaded "
        "(shared across all admins).\n"
        "Go to your group and run /startquiz (or tap the button) to load them in, shuffled."
    )


async def clearpending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """DM only, admin only: wipe the shared not-yet-loaded question pool."""
    if update.effective_chat.type != "private":
        return
    if not is_dm_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can do this.")
        return
    pending_questions.clear()
    save_pending()
    await update.message.reply_text("Cleared the shared pending question pool.")


async def listq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        if not is_dm_admin(update.effective_user.id):
            await update.message.reply_text("Only admins can view the pending question pool.")
            return
        if not pending_questions:
            await update.message.reply_text("No pending questions. Add some with /addq.")
            return
        lines = [f"{i+1}. {q}  (ans: {a})" for i, (q, a) in enumerate(pending_questions)]
        await update.message.reply_text("Pending questions (shared, not yet loaded):\n" + "\n".join(lines))
    else:
        chat_id = update.effective_chat.id
        state = get_state(chat_id)
        text = f"{len(state.questions)} question(s) remaining in the pool."
        if state.game_active:
            text += f"\nGame in progress: question {state.questions_asked_this_game}/{GAME_LENGTH} done."
        else:
            text += "\nNo game in progress."
        if await is_admin(update, context):
            text += f"\n{len(pending_questions)} question(s) waiting in DM to be loaded (shared pool)."
        await update.message.reply_text(text)


# ---------------------------------------------------------------------------
# STARTING A GAME (group only)
# ---------------------------------------------------------------------------
async def startquiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("Run /startquiz inside the group, not here.")
        return
    if not await is_admin(update, context):
        await update.message.reply_text("Only admins can start a game.")
        return

    chat_id = update.effective_chat.id
    state = get_state(chat_id)

    # Pull in whatever's been DMed to the bot (shared across all admins).
    if pending_questions:
        loaded_count = len(pending_questions)
        state.questions.extend(pending_questions)
        random.shuffle(state.questions)
        pending_questions.clear()
        save_pending()
        save_all_state()
        await update.message.reply_text(f"Loaded {loaded_count} new question(s) into the pool.")

    if not state.game_active:
        chat_teams = get_teams(chat_id)
        if not chat_teams:
            await update.message.reply_text(
                "No teams set up yet. Create some first with /createteam Name, "
                "then have members join with /jointeam Name."
            )
            return

        # Optional: /startquiz Team Alpha, Team Beta  -> restrict this game to just those teams.
        arg_text = update.message.text.partition(" ")[2].strip()
        active_teams = None
        if arg_text:
            requested = [n.strip() for n in arg_text.split(",") if n.strip()]
            matched = set()
            unmatched = []
            for req in requested:
                hit = next((t for t in chat_teams if t.lower() == req.lower()), None)
                if hit:
                    matched.add(hit)
                else:
                    unmatched.append(req)
            if unmatched:
                await update.message.reply_text(
                    f"Couldn't find team(s): {', '.join(unmatched)}. Check /teams for exact names."
                )
                return
            if len(matched) < 2:
                await update.message.reply_text("Name at least 2 teams for a match, separated by commas.")
                return
            active_teams = matched

        reset_targets = active_teams if active_teams is not None else set(chat_teams.keys())
        for name in reset_targets:
            chat_teams[name]["score"] = 0
        save_teams()

        state.active_teams = active_teams
        state.questions_asked_this_game = 0
        state.game_active = True
        save_all_state()

        playing_desc = ", ".join(sorted(reset_targets))
        await update.message.reply_text(
            f"🎮 New game started! Teams playing: {playing_desc}. "
            f"First to be marked correct on each question scores a point. {GAME_LENGTH} questions per game."
        )

    if not state.questions:
        await update.message.reply_text(
            "No questions loaded yet. DM me some first with /addq, then run /startquiz again."
        )
        return

    await send_next_button(chat_id, context)


# ---------------------------------------------------------------------------
# BUTTON: POST NEXT QUESTION
# ---------------------------------------------------------------------------
async def post_question_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id

    if not await is_admin(update, context):
        await query.answer("Only admins can do this.", show_alert=True)
        return

    state = get_state(chat_id)

    if not state.game_active:
        await query.answer()
        await query.edit_message_text("No game in progress. Use /startquiz to begin one.")
        return

    if state.questions_asked_this_game >= GAME_LENGTH:
        await query.answer()
        await end_game(chat_id, context)
        return

    if not state.questions:
        await query.answer()
        await query.edit_message_text("No questions left in the pool.")
        return

    # Pick one at random and remove it permanently from the pool.
    idx = random.randrange(len(state.questions))
    question, answer = state.questions.pop(idx)

    state.questions_asked_this_game += 1
    state.round_seq += 1
    state.current_round = state.round_seq
    state.round_open = True
    state.last_answer = answer
    state.submissions = []
    state.window_start = time.monotonic()
    save_all_state()

    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)  # remove the button so it can't be double-clicked
    await context.bot.send_message(
        chat_id,
        f"❓ Question {state.questions_asked_this_game}/{GAME_LENGTH}: {question}\n\n"
        f"⏱ {ANSWER_WINDOW_SECONDS} seconds! First correct answer scores a point for that member's team.",
    )

    context.job_queue.run_once(
        close_window,
        ANSWER_WINDOW_SECONDS,
        data={"chat_id": chat_id, "round": state.current_round},
        name=f"close_{chat_id}_{state.current_round}",
    )


# ---------------------------------------------------------------------------
# GROUP MESSAGES: log potential answers, and let an admin mark the winner by
# replying to a member's message with a word starting "correct"
# ---------------------------------------------------------------------------
async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        return
    message = update.message
    if message is None or not message.text:
        return

    chat_id = update.effective_chat.id
    state = quiz_states.get(chat_id)

    # Admin marking a correct answer: reply to the member's message with "Correct" (any case).
    if message.reply_to_message and message.text.strip().lower().startswith("correct"):
        if not await is_admin(update, context):
            return  # not an admin — ignore silently, don't spam the chat
        await resolve_correct(update, context, state, chat_id)
        return

    # Otherwise, just log it as a potential answer while a question is open.
    if state and state.round_open and not update.effective_user.is_bot:
        elapsed = round(time.monotonic() - state.window_start, 2)
        state.submissions.append(
            (len(state.submissions) + 1, update.effective_user.first_name, message.text, elapsed)
        )


async def resolve_correct(update: Update, context: ContextTypes.DEFAULT_TYPE, state: Optional[QuizState], chat_id: int):
    if not state or not state.round_open:
        await update.message.reply_text("There's no active question to mark correct right now.")
        return

    target_user = update.message.reply_to_message.from_user
    if target_user.is_bot:
        await update.message.reply_text("Can't award a point to a bot.")
        return

    team_name = find_team_of_user(chat_id, target_user.id)
    if not team_name:
        await update.message.reply_text(
            f"{target_user.first_name} isn't on a team yet — they need to /jointeam first."
        )
        return
    if state.active_teams is not None and team_name not in state.active_teams:
        await update.message.reply_text(
            f"{target_user.first_name}'s team ({team_name}) isn't part of this match."
        )
        return

    get_teams(chat_id)[team_name]["score"] += 1
    save_teams()
    state.round_open = False

    await update.message.reply_text(
        f"✅ Correct! {target_user.first_name} earns team {team_name} a point!\n\n"
        f"📊 Scores:\n{format_scoreboard(chat_id, state.active_teams)}"
    )
    await advance_or_end_game(chat_id, context)


# ---------------------------------------------------------------------------
# TIMER EXPIRES WITHOUT ANYONE BEING MARKED CORRECT
# ---------------------------------------------------------------------------
async def close_window(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    round_id = context.job.data["round"]
    state = get_state(chat_id)

    # Ignore a stale timer whose round was already resolved or superseded.
    if state.current_round != round_id or not state.round_open:
        return

    state.round_open = False
    await context.bot.send_message(
        chat_id,
        f"⏰ Time's up! No one was marked correct.\n\n"
        f"✅ Correct answer: {state.last_answer}\n\n"
        f"📊 Scores:\n{format_scoreboard(chat_id, state.active_teams)}",
    )
    await advance_or_end_game(chat_id, context)


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------
def main():
    load_all_state()
    load_pending()
    load_teams()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("addq", addq))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("clearpending", clearpending))
    app.add_handler(CommandHandler("listq", listq))
    app.add_handler(CommandHandler("startquiz", startquiz))
    app.add_handler(CommandHandler("createteam", createteam))
    app.add_handler(CommandHandler("jointeam", jointeam))
    app.add_handler(CommandHandler("myteam", myteam))
    app.add_handler(CommandHandler("teams", teams_cmd))
    app.add_handler(CommandHandler("teamlist", teamlist_cmd))
    app.add_handler(CommandHandler("answers", answers_cmd))
    app.add_handler(CallbackQueryHandler(post_question_callback, pattern="^post_question$"))
    app.add_handler(CallbackQueryHandler(jointeam_button_callback, pattern="^jointeam:"))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_members))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_message))

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
