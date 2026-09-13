import asyncio
import html
import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
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
MAX_POINTS_PER_QUESTION = 10         # individual mode: max points for an instant correct answer
STATE_FILE = "quiz_data.json"        # each group's remaining question pool + game progress
PENDING_FILE = "pending_questions.json"  # questions admins have DMed in, not yet loaded into a group
TEAMS_FILE = "teams.json"            # teams, members, scores per group
LEADERBOARD_FILE = "leaderboard.json"    # individual scores per group (used when no teams are named)

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
    round_open: bool = False              # True from question posted until admin resolves it
    accepting_answers: bool = False        # True only during the timed window; stops logging after
    last_answer: Optional[str] = None
    window_start: float = 0.0
    submissions: list = field(default_factory=list)   # (order, user_id, user_name, text, elapsed_seconds, message_id)
    credited_user_ids: set = field(default_factory=set)   # individual mode: who's already scored this round
    last_award: Optional[dict] = None   # most recent correct-mark, so an admin mistake can be undone
    active_teams: Optional[set] = None    # set -> team mode (only these teams play); None -> individual leaderboard mode


quiz_states: dict[int, QuizState] = {}          # chat_id -> QuizState (one per group)
pending_questions: list = []                    # shared pool DMed in by any admin, not yet loaded
teams: dict[int, dict[str, dict]] = {}          # chat_id -> team_name -> {"score": int, "members": set(user_id)}
leaderboard: dict[int, dict[str, dict]] = {}    # chat_id -> str(user_id) -> {"name": str, "score": int}


def get_state(chat_id: int) -> QuizState:
    if chat_id not in quiz_states:
        quiz_states[chat_id] = QuizState()
    return quiz_states[chat_id]


def get_teams(chat_id: int) -> dict:
    return teams.setdefault(chat_id, {})


def get_leaderboard(chat_id: int) -> dict:
    return leaderboard.setdefault(chat_id, {})


def is_team_mode(state: "QuizState") -> bool:
    return state.active_teams is not None


def compute_points(elapsed: float) -> int:
    """Faster answers score more; at least 1 point for any correct answer."""
    clamped = max(0.0, min(elapsed, ANSWER_WINDOW_SECONDS))
    points = round(MAX_POINTS_PER_QUESTION * (1 - clamped / ANSWER_WINDOW_SECONDS))
    return max(1, points)


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
    return "\n".join(f"{html.escape(name)}: {info['score']} pt(s)" for name, info in ordered)


def format_leaderboard(chat_id: int) -> str:
    lb = get_leaderboard(chat_id)
    if not lb:
        return "(no one has scored yet)"
    ordered = sorted(lb.values(), key=lambda e: e["score"], reverse=True)
    return "\n".join(f"{html.escape(entry['name'])}: {entry['score']} pt(s)" for entry in ordered)


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


def dedupe_pool(questions: list) -> tuple:
    """Remove duplicate question text (case/whitespace-insensitive), keeping the first occurrence."""
    seen = set()
    deduped = []
    removed = 0
    for q, a in questions:
        key = q.strip().lower()
        if key in seen:
            removed += 1
            continue
        seen.add(key)
        deduped.append((q, a))
    return deduped, removed


def load_all_state():
    if not os.path.exists(STATE_FILE):
        return
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    for chat_id_str, saved in data.items():
        state = get_state(int(chat_id_str))
        raw_questions = [tuple(q) for q in saved.get("questions", [])]
        state.questions, _removed = dedupe_pool(raw_questions)  # clean out any duplicates from before this fix
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


def save_leaderboard():
    data = {str(chat_id): entries for chat_id, entries in leaderboard.items()}
    with open(LEADERBOARD_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_leaderboard():
    if not os.path.exists(LEADERBOARD_FILE):
        return
    with open(LEADERBOARD_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    for chat_id_str, entries in data.items():
        leaderboard[int(chat_id_str)] = entries


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
            chat_id,
            "<b>🗂 No questions left in the pool. An admin can DM me more with /addq.</b>",
            parse_mode=ParseMode.HTML,
        )
        return
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("▶️ Post next question", callback_data="post_question")]]
    )
    progress = (
        f"{state.questions_asked_this_game}/{GAME_LENGTH} done"
        if is_team_mode(state)
        else f"{state.questions_asked_this_game} asked so far"
    )
    await context.bot.send_message(
        chat_id,
        f"<b>✅ Question {progress}. Ready for the next one?</b>",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


async def advance_or_end_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    state = get_state(chat_id)
    if is_team_mode(state) and state.questions_asked_this_game >= GAME_LENGTH:
        await end_game(chat_id, context)
    else:
        await send_next_button(chat_id, context)


async def end_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    state = get_state(chat_id)
    state.game_active = False
    save_all_state()

    if is_team_mode(state):
        chat_teams = {name: info for name, info in get_teams(chat_id).items() if name in state.active_teams}
        scoreboard = format_scoreboard(chat_id, state.active_teams)
        if not chat_teams:
            await context.bot.send_message(chat_id, "<b>🏁 Game over!</b>", parse_mode=ParseMode.HTML)
            return
        max_score = max(info["score"] for info in chat_teams.values())
        winners = [name for name, info in chat_teams.items() if info["score"] == max_score]
        label = "scores"
    else:
        lb = get_leaderboard(chat_id)
        scoreboard = format_leaderboard(chat_id)
        if not lb:
            await context.bot.send_message(chat_id, "<b>🏁 Game over!</b>", parse_mode=ParseMode.HTML)
            return
        max_score = max(entry["score"] for entry in lb.values())
        winners = [entry["name"] for entry in lb.values() if entry["score"] == max_score]
        label = "leaderboard"

    winners_esc = [html.escape(w) for w in winners]
    winner_line = f"🏆 Winner: {winners_esc[0]}!" if len(winners_esc) == 1 else f"🏆 It's a tie: {', '.join(winners_esc)}!"

    await context.bot.send_message(
        chat_id,
        f"<b>🏁 Game over! Final {label}:\n{scoreboard}\n\n{winner_line}\n\n"
        "Run /startquiz to start a new game (scores will reset).</b>",
        parse_mode=ParseMode.HTML,
    )

    if not is_team_mode(state):
        # Wipe the leaderboard now so it's already clean for whenever the next game starts.
        leaderboard[chat_id] = {}
        save_leaderboard()


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
    lines = [f"{n}. {name}: {text}  ({t}s)" for n, name, text, t, _mid in state.submissions]
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


async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Group only: show current standings for the open (no-team) leaderboard mode."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("Run this inside the group.")
        return
    await update.message.reply_text("Leaderboard:\n" + format_leaderboard(update.effective_chat.id))


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
            progress = (
                f"{state.questions_asked_this_game}/{GAME_LENGTH} done"
                if is_team_mode(state)
                else f"{state.questions_asked_this_game} asked so far (no limit — /endquiz to finish)"
            )
            text += f"\nGame in progress: question {progress}."
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

    # Pull in whatever's been DMed to the bot (shared across all admins), skipping exact duplicates.
    if pending_questions:
        existing_keys = {q.strip().lower() for q, _a in state.questions}
        to_add = []
        seen_keys = set()
        skipped = 0
        for q, a in pending_questions:
            key = q.strip().lower()
            if key in existing_keys or key in seen_keys:
                skipped += 1
                continue
            seen_keys.add(key)
            to_add.append((q, a))

        state.questions.extend(to_add)
        random.shuffle(state.questions)
        pending_questions.clear()
        save_pending()

        note = f"Loaded {len(to_add)} new question(s) into the pool."
        if skipped:
            note += f" Skipped {skipped} duplicate(s) already in the pool."
        await update.message.reply_text(note)

    # Clean out any duplicates already sitting in the pool from before this fix existed.
    state.questions, removed_existing = dedupe_pool(state.questions)
    if removed_existing:
        await update.message.reply_text(f"Removed {removed_existing} duplicate question(s) already in the pool.")
    save_all_state()

    if not state.game_active:
        # /startquiz Team Alpha, Team Beta  -> team match, restricted to just those teams.
        # /startquiz (no names)             -> open leaderboard, everyone in the group can play.
        arg_text = update.message.text.partition(" ")[2].strip()

        if arg_text:
            chat_teams = get_teams(chat_id)
            if not chat_teams:
                await update.message.reply_text(
                    "No teams set up yet. Create some first with /createteam Name, "
                    "then have members join with /jointeam Name."
                )
                return
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

            for name in matched:
                chat_teams[name]["score"] = 0
            save_teams()

            state.active_teams = matched
            state.questions_asked_this_game = 0
            state.game_active = True
            save_all_state()

            playing_desc = ", ".join(html.escape(n) for n in sorted(matched))
            await update.message.reply_text(
                f"<b>🎮 New team match started! Teams playing: {playing_desc}. "
                f"First to be marked correct on each question scores a point. {GAME_LENGTH} questions per game.</b>",
                parse_mode=ParseMode.HTML,
            )
        else:
            # Individual leaderboard mode — end_game() already wiped the board when the last
            # game finished, so there's nothing to reset here.
            state.active_teams = None
            state.questions_asked_this_game = 0
            state.game_active = True
            save_all_state()

            await update.message.reply_text(
                "<b>🎮 New open game started — anyone in the group can answer! An admin picks the correct "
                f"answerer from the list after each question; the faster you're correct, the more points "
                f"(up to {MAX_POINTS_PER_QUESTION}). No question limit — run /endquiz whenever you're ready to finish.</b>",
                parse_mode=ParseMode.HTML,
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
        await query.edit_message_text(
            "<b>No game in progress. Use /startquiz to begin one.</b>", parse_mode=ParseMode.HTML
        )
        return

    if is_team_mode(state) and state.questions_asked_this_game >= GAME_LENGTH:
        await query.answer()
        await end_game(chat_id, context)
        return

    if not state.questions:
        await query.answer()
        await query.edit_message_text("<b>No questions left in the pool.</b>", parse_mode=ParseMode.HTML)
        return

    # Countdown: yellow (get ready) -> pause -> green (go), then the question posts immediately.
    await query.answer()
    await query.edit_message_text("🟡 <b>Get ready...</b>", parse_mode=ParseMode.HTML, reply_markup=None)
    await asyncio.sleep(2)
    await query.edit_message_text("🟢 <b>Go!</b>", parse_mode=ParseMode.HTML)

    # Pick one at random and remove it permanently from the pool.
    idx = random.randrange(len(state.questions))
    question, answer = state.questions.pop(idx)

    state.questions_asked_this_game += 1
    state.round_seq += 1
    state.current_round = state.round_seq
    state.round_open = True
    state.accepting_answers = True
    state.last_answer = answer
    state.submissions = []
    state.credited_user_ids = set()
    state.window_start = time.monotonic()
    save_all_state()

    progress = f"{state.questions_asked_this_game}/{GAME_LENGTH}" if is_team_mode(state) else f"{state.questions_asked_this_game}"
    safe_question = html.escape(question)
    await context.bot.send_message(
        chat_id,
        f"<b>❓ Question {progress}\n{safe_question}\n\n⏱ {ANSWER_WINDOW_SECONDS} seconds!</b>",
        parse_mode=ParseMode.HTML,
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

    # Otherwise, just log it as a potential answer while the timed window is open.
    if state and state.accepting_answers and not update.effective_user.is_bot:
        elapsed = round(time.monotonic() - state.window_start, 2)
        if elapsed > ANSWER_WINDOW_SECONDS:
            return  # arrived after the real window closed (timer job just hasn't fired yet) — don't count it
        state.submissions.append(
            (len(state.submissions) + 1, update.effective_user.first_name, message.text, elapsed, message.message_id)
        )


async def resolve_correct(update: Update, context: ContextTypes.DEFAULT_TYPE, state: Optional[QuizState], chat_id: int):
    if not state or not state.round_open:
        await update.message.reply_text("There's no active question to mark correct right now.")
        return

    target_user = update.message.reply_to_message.from_user
    if target_user.is_bot:
        await update.message.reply_text("Can't award a point to a bot.")
        return

    if is_team_mode(state):
        team_name = find_team_of_user(chat_id, target_user.id)
        if not team_name:
            await update.message.reply_text(
                f"{target_user.first_name} isn't on a team yet — they need to /jointeam first."
            )
            return
        if team_name not in state.active_teams:
            await update.message.reply_text(
                f"{target_user.first_name}'s team ({team_name}) isn't part of this match."
            )
            return

        get_teams(chat_id)[team_name]["score"] += 1
        save_teams()
        state.round_open = False
        state.last_award = {"mode": "team", "team_name": team_name, "points": 1}

        await update.message.reply_text(
            f"<b>✅ Correct! {html.escape(target_user.first_name)} earns team {html.escape(team_name)} a point!\n\n"
            f"📊 Scores:\n{format_scoreboard(chat_id, state.active_teams)}\n\n"
            "Marked the wrong person? Send /undo.</b>",
            parse_mode=ParseMode.HTML,
        )
        await advance_or_end_game(chat_id, context)
    else:
        # Individual leaderboard mode — multiple people can be credited on the same question;
        # points scale with how fast each one's own answer arrived. Round stays open until /next.
        if target_user.id in state.credited_user_ids:
            await update.message.reply_text(f"{target_user.first_name} has already been credited this question.")
            return

        replied_id = update.message.reply_to_message.message_id
        elapsed = next((e for (_, _, _, e, mid) in state.submissions if mid == replied_id), None)
        if elapsed is None:
            elapsed = round(time.monotonic() - state.window_start, 2)  # fallback if not found in the log
        points = compute_points(elapsed)

        entry = get_leaderboard(chat_id).setdefault(str(target_user.id), {"name": target_user.first_name, "score": 0})
        entry["name"] = target_user.first_name  # keep the display name current
        entry["score"] += points
        save_leaderboard()
        state.credited_user_ids.add(target_user.id)
        state.last_award = {
            "mode": "individual",
            "user_id": target_user.id,
            "user_name": target_user.first_name,
            "points": points,
        }

        await update.message.reply_text(
            f"<b>✅ Correct! {html.escape(target_user.first_name)} answered in {elapsed}s and earns "
            f"{points} point(s)!\n\n"
            f"🏆 Leaderboard:\n{format_leaderboard(chat_id)}\n\n"
            "Reply \"Correct\" to credit someone else too, /next to move on, or /undo if that was wrong.</b>",
            parse_mode=ParseMode.HTML,
        )
        # Deliberately no auto-advance here — admin may want to credit more people first.


# ---------------------------------------------------------------------------
# TIMER EXPIRES: stop collecting answers and show them all — round stays open
# ---------------------------------------------------------------------------
async def close_window(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    round_id = context.job.data["round"]
    state = get_state(chat_id)

    # Ignore a stale timer whose round was already resolved or superseded.
    if state.current_round != round_id or not state.round_open:
        return

    state.accepting_answers = False  # stop logging new messages, but round stays open

    if not state.submissions:
        await context.bot.send_message(
            chat_id,
            "<b>🛑 Time's up! No one answered.\n"
            f"✅ Answer: {html.escape(state.last_answer)}\n\n"
            "Use /skip to move on, or reply \"Correct\" to a late answer to still credit it.</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = [f"{n}. {html.escape(name)}: {html.escape(ans)}  ({t}s)" for n, name, ans, t, _mid in state.submissions]
    close_instruction = (
        "Admin: reply \"Correct\" to the right message to award the point, or /skip if no one got it."
        if is_team_mode(state)
        else "Admin: reply \"Correct\" to credit anyone who got it (multiple people welcome), then /next to continue."
    )
    await context.bot.send_message(
        chat_id,
        "<b>🛑 Time's up!\n\n"
        + "\n".join(lines)
        + f"\n\n✅ Answer: {html.escape(state.last_answer)}\n\n"
        + close_instruction
        + "</b>",
        parse_mode=ParseMode.HTML,
    )


async def skip_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: end the current round with no one credited, and move on."""
    if update.effective_chat.type == "private":
        return
    if not await is_admin(update, context):
        await update.message.reply_text("Only admins can do this.")
        return

    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    if not state.round_open:
        await update.message.reply_text("There's no active question to skip.")
        return

    state.round_open = False
    state.accepting_answers = False
    standings = format_scoreboard(chat_id, state.active_teams) if is_team_mode(state) else format_leaderboard(chat_id)
    label = "Scores" if is_team_mode(state) else "Leaderboard"
    await update.message.reply_text(
        f"<b>⏭ Skipped. Correct answer was: {html.escape(state.last_answer)}\n\n"
        f"📊 {label}:\n{standings}</b>",
        parse_mode=ParseMode.HTML,
    )
    await advance_or_end_game(chat_id, context)


async def next_question_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: close the current round (after crediting whoever got it) and move on."""
    if update.effective_chat.type == "private":
        return
    if not await is_admin(update, context):
        await update.message.reply_text("Only admins can do this.")
        return

    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    if not state.round_open:
        await update.message.reply_text("There's no active question to close.")
        return

    state.round_open = False
    state.accepting_answers = False
    standings = format_scoreboard(chat_id, state.active_teams) if is_team_mode(state) else format_leaderboard(chat_id)
    label = "Scores" if is_team_mode(state) else "Leaderboard"
    await update.message.reply_text(
        f"<b>➡️ Moving on.\n\n📊 {label}:\n{standings}</b>", parse_mode=ParseMode.HTML
    )
    await advance_or_end_game(chat_id, context)


async def endquiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: end the current game right now, whatever mode it's in."""
    if update.effective_chat.type == "private":
        return
    if not await is_admin(update, context):
        await update.message.reply_text("Only admins can do this.")
        return

    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    if not state.game_active:
        await update.message.reply_text("There's no game in progress.")
        return

    if state.round_open:
        # Close any open question first, with no one credited, before wrapping up.
        state.round_open = False
        state.accepting_answers = False

    await end_game(chat_id, context)


async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: reverse the most recent correct-mark, in case the wrong person/team was picked."""
    if update.effective_chat.type == "private":
        return
    if not await is_admin(update, context):
        await update.message.reply_text("Only admins can do this.")
        return

    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    award = state.last_award
    if not award:
        await update.message.reply_text("Nothing to undo.")
        return

    if award["mode"] == "team":
        team_name = award["team_name"]
        chat_teams = get_teams(chat_id)
        if team_name in chat_teams:
            chat_teams[team_name]["score"] = max(0, chat_teams[team_name]["score"] - award["points"])
            save_teams()
        standings = format_scoreboard(chat_id, state.active_teams)
        label = "Scores"
        undo_line = f"↩️ Undone: removed {award['points']} point(s) from team {html.escape(team_name)}."
    else:
        entry = get_leaderboard(chat_id).get(str(award["user_id"]))
        if entry:
            entry["score"] = max(0, entry["score"] - award["points"])
            save_leaderboard()
        state.credited_user_ids.discard(award["user_id"])  # let them be credited again if the round's still open
        standings = format_leaderboard(chat_id)
        label = "Leaderboard"
        undo_line = f"↩️ Undone: removed {award['points']} point(s) from {html.escape(award['user_name'])}."

    state.last_award = None
    await update.message.reply_text(
        f"<b>{undo_line}\n\n📊 {label}:\n{standings}</b>", parse_mode=ParseMode.HTML
    )


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------
def main():
    load_all_state()
    load_pending()
    load_teams()
    load_leaderboard()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("addq", addq))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("clearpending", clearpending))
    app.add_handler(CommandHandler("listq", listq))
    app.add_handler(CommandHandler("startquiz", startquiz))
    app.add_handler(CommandHandler("skip", skip_question))
    app.add_handler(CommandHandler("next", next_question_cmd))
    app.add_handler(CommandHandler("endquiz", endquiz_cmd))
    app.add_handler(CommandHandler("undo", undo_cmd))
    app.add_handler(CommandHandler("createteam", createteam))
    app.add_handler(CommandHandler("jointeam", jointeam))
    app.add_handler(CommandHandler("myteam", myteam))
    app.add_handler(CommandHandler("teams", teams_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
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
