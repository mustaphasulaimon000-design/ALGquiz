import asyncio
import html
import json
import os
import random
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.constants import ParseMode
import firebase_admin
from firebase_admin import credentials, firestore
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
BOT_TOKEN =  "8887839950:AAECf8okBKkPOhiigMSGySiVWp08PIsBtzk"  # get this from @BotFather
ANSWER_WINDOW_SECONDS = 10
GAME_LENGTH = 5                      # questions per game
MAX_POINTS_PER_QUESTION = 10         # individual mode: max points for an instant correct answer
# All persistent data now lives in Firestore (same Firebase project your ALG quiz website uses)
# instead of local JSON files — this also means data survives Railway redeploys, which local
# files did not. Set FIREBASE_CREDENTIALS_JSON in Railway's Variables tab to your service
# account key's full JSON content (Firebase console > Project Settings > Service Accounts >
# Generate new private key). Locally, you can set the same env var, or point
# GOOGLE_APPLICATION_CREDENTIALS at the downloaded .json file instead.
FIREBASE_CREDENTIALS_ENV = "FIREBASE_CREDENTIALS_JSON"

COL_STATE = "quiz_state"              # doc per chat: question pool + game progress
COL_PENDING = "quiz_pending"          # one shared doc: questions DMed in, not yet loaded
COL_TEAMS = "quiz_teams"              # doc per chat: teams, members, scores, captains, lineups
COL_LEADERBOARD = "quiz_leaderboard"  # doc per chat: individual scores (no-team mode)
COL_FIXTURES = "quiz_fixtures"        # doc per chat: upcoming match fixtures
COL_PLAYER_LINKS = "quiz_player_links"  # doc per (chat, telegram user): permanent link to a real website squad player
WEBSITE_TEAMS_COL = "teams"    # your ALG quiz website's own collection of teams
WEBSITE_PLAYERS_COL = "players"  # your ALG quiz website's own collection of squad players
WEBSITE_MATCHES_COL = "matches"
WEBSITE_LEAGUES_COL = "leagues"
WEBSITE_TEAM_ENTRIES_COL = "teamEntries"
WEBSITE_PLAYER_ENTRIES_COL = "playerEntries"

# The website league this bot's team matches feed results into.
# Find this in Firestore's leagues collection (the document ID).
LEAGUE_ID = "1ALeNC5sJlxcTplPlZmk"

FIXTURE_SQUAD_LINEUP_MINUTES = 6   # auto-post real-squad lineup buttons this many minutes before kickoff

pending_schedule: dict[int, dict] = {}   # chat_id -> {"match_id", "home_team_name", "away_team_name", "admin_id"} awaiting a kickoff date/time reply

_firestore_client = None


def get_db():
    """Lazily initialize and return the Firestore client, using credentials from the env var,
    or GOOGLE_APPLICATION_CREDENTIALS (a key file path) as a fallback for local testing."""
    global _firestore_client
    if _firestore_client is not None:
        return _firestore_client

    if not firebase_admin._apps:
        cred_json = os.environ.get(FIREBASE_CREDENTIALS_ENV)
        gac_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        print(
            f"[firebase] {FIREBASE_CREDENTIALS_ENV} present: {bool(cred_json)} "
            f"(len {len(cred_json) if cred_json else 0}); GOOGLE_APPLICATION_CREDENTIALS: {gac_path!r}"
        )
        if cred_json:
            try:
                parsed = json.loads(cred_json)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"{FIREBASE_CREDENTIALS_ENV} is set but isn't valid JSON: {e}")
            cred = credentials.Certificate(parsed)
            firebase_admin.initialize_app(cred)
        elif gac_path:
            firebase_admin.initialize_app()  # picks up GOOGLE_APPLICATION_CREDENTIALS itself
        else:
            raise RuntimeError(
                f"Neither {FIREBASE_CREDENTIALS_ENV} nor GOOGLE_APPLICATION_CREDENTIALS is set in this "
                "process. Check it's set on the exact service/terminal actually running this, and that "
                "there's no typo in the variable name."
            )

    _firestore_client = firestore.client()
    return _firestore_client


# Admins type kickoff times in THIS timezone (as a fixed UTC offset — no daylight-saving handling).
# Set this once to match your group's local time, e.g. 1 for WAT (Nigeria), 0 for UTC, -5 for US Eastern.
TIMEZONE_OFFSET_HOURS = 1   # WAT (Nigeria) — no daylight saving to worry about
FIXTURES_REFRESH_SECONDS = 60   # how often the live countdown board updates

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
    match_stats: dict = field(default_factory=dict)   # user_id -> {"name", "goals", "assists"} for this whole game
    last_scorer_id: Optional[int] = None    # who scored this round, so an assist can be linked to it
    last_assister_id: Optional[int] = None
    assist_recorded_this_round: bool = False
    linked_match_id: Optional[str] = None       # a real website match this game's result should write back to
    linked_home_team_name: Optional[str] = None
    linked_away_team_name: Optional[str] = None
    linked_home_team_id: Optional[str] = None
    linked_away_team_id: Optional[str] = None
    linked_league_id: Optional[str] = None
    match_events: list = field(default_factory=list)   # [{"type": "goal"/"assist", "playerId": <website id>}]
    active_teams: Optional[set] = None    # set -> team mode (only these teams play); None -> individual leaderboard mode


quiz_states: dict[int, QuizState] = {}          # chat_id -> QuizState (one per group)
pending_questions: list = []                    # shared pool DMed in by any admin, not yet loaded
teams: dict[int, dict[str, dict]] = {}          # chat_id -> team_name -> {"score", "members": {user_id: name}, "captain": user_id, "lineup": {...}}
leaderboard: dict[int, dict[str, dict]] = {}    # chat_id -> str(user_id) -> {"name": str, "score": int}

LINEUP_STARTERS = 5
LINEUP_SUBS = 3

# In-memory only (not persisted) — a captain's in-progress lineup/substitution selection.
# token -> {"chat_id", "team_name", "captain_id", "stage", "starters"/"subs" or "out_id", ...}
lineup_builders: dict[str, dict] = {}
sub_builders: dict[str, dict] = {}
link_builders: dict[str, dict] = {}   # admin's in-progress Telegram-person <-> squad-player linking session

fixtures: dict[int, list] = {}          # chat_id -> list of {"team_a", "team_b", "kickoff_utc" (iso), "started": bool}
fixture_boards: dict[int, int] = {}     # chat_id -> message_id of the live-updating fixtures board


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


def parse_fixture_datetime(text: str) -> Optional[datetime]:
    """Parse "YYYY-MM-DD HH:MM" as local time (TIMEZONE_OFFSET_HOURS) and return it as a UTC datetime."""
    try:
        naive_local = datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    utc_dt = naive_local - timedelta(hours=TIMEZONE_OFFSET_HOURS)
    return utc_dt.replace(tzinfo=timezone.utc)


def format_countdown(target_utc: datetime, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    seconds = (target_utc - now).total_seconds()
    if seconds <= 0:
        return "🔴 LIVE"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if days or hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def get_fixtures(chat_id: int) -> list:
    return fixtures.setdefault(chat_id, [])


def build_fixtures_text(chat_id: int) -> str:
    chat_fixtures = [f for f in get_fixtures(chat_id) if not f["started"]]
    if not chat_fixtures:
        return "📅 <b>Fixtures</b>\n\nNo upcoming fixtures."
    chat_fixtures.sort(key=lambda f: f["kickoff_utc"])
    now = datetime.now(timezone.utc)
    lines = []
    for f in chat_fixtures:
        kickoff = datetime.fromisoformat(f["kickoff_utc"])
        local_str = (kickoff + timedelta(hours=TIMEZONE_OFFSET_HOURS)).strftime("%a %d %b, %H:%M")
        countdown = format_countdown(kickoff, now)
        lines.append(
            f"⚽ {html.escape(f['team_a'])} vs {html.escape(f['team_b'])}\n"
            f"🕒 {local_str} — ⏳ {countdown}"
        )
    return "📅 <b>Fixtures</b>\n\n" + "\n\n".join(lines)


def compute_points(elapsed: float) -> int:
    """Faster answers score more; at least 1 point for any correct answer."""
    clamped = max(0.0, min(elapsed, ANSWER_WINDOW_SECONDS))
    points = round(MAX_POINTS_PER_QUESTION * (1 - clamped / ANSWER_WINDOW_SECONDS))
    return max(1, points)


def tag_mention(user_id: int, name: str) -> str:
    """A clickable HTML mention that works even for members without a @username set."""
    return f'<a href="tg://user?id={user_id}">{html.escape(name)}</a>'


def record_match_stat(state: "QuizState", user_id: int, name: str, kind: str) -> None:
    entry = state.match_stats.setdefault(user_id, {"name": name, "goals": 0, "assists": 0})
    entry["name"] = name  # keep it current
    entry[kind] += 1


def format_match_stats(state: "QuizState") -> str:
    scorers = [(uid, s) for uid, s in state.match_stats.items() if s["goals"] > 0]
    assisters = [(uid, s) for uid, s in state.match_stats.items() if s["assists"] > 0]
    if not scorers and not assisters:
        return ""

    lines = []
    if scorers:
        scorers.sort(key=lambda item: item[1]["goals"], reverse=True)
        parts = [
            tag_mention(uid, s["name"]) + (f" ({s['goals']})" if s["goals"] > 1 else "") for uid, s in scorers
        ]
        lines.append("⚽ Scorers: " + ", ".join(parts))
    if assisters:
        assisters.sort(key=lambda item: item[1]["assists"], reverse=True)
        parts = [
            tag_mention(uid, s["name"]) + (f" ({s['assists']})" if s["assists"] > 1 else "") for uid, s in assisters
        ]
        lines.append("🅰️ Assists: " + ", ".join(parts))
    return "\n".join(lines)


def find_team_of_user(chat_id: int, user_id: int) -> Optional[str]:
    for name, info in get_teams(chat_id).items():
        if user_id in info["members"]:
            return name
    return None


def find_team_of_captain(chat_id: int, user_id: int) -> Optional[str]:
    for name, info in get_teams(chat_id).items():
        if info.get("captain") == user_id:
            return name
    return None


def find_registered_team(chat_id: int, typed_name: str) -> Optional[str]:
    """Match a fixture's team name (as typed in /addfixture) to a registered team, case-insensitively."""
    key = typed_name.strip().lower()
    for name in get_teams(chat_id):
        if name.strip().lower() == key:
            return name
    return None


def find_website_team_by_name(name: str) -> Optional[dict]:
    """Look up a real team on the ALG quiz website by name (case-insensitive). Returns {id, ...fields} or None."""
    db = get_db()
    key = name.strip().lower()
    for doc in db.collection(WEBSITE_TEAMS_COL).stream():
        data = doc.to_dict() or {}
        if (data.get("name") or "").strip().lower() == key:
            return {"id": doc.id, **data}
    return None


def fetch_website_squad(website_team_id: str) -> list:
    """All real squad players for a website team, as [{id, name, ...}]."""
    db = get_db()
    docs = db.collection(WEBSITE_PLAYERS_COL).where("teamId", "==", website_team_id).stream()
    return [{"id": d.id, **(d.to_dict() or {})} for d in docs]


def get_player_links(chat_id: int) -> list:
    """Every permanent Telegram-person <-> squad-player link saved for this chat."""
    db = get_db()
    docs = db.collection(COL_PLAYER_LINKS).where("chat_id", "==", chat_id).stream()
    return [d.to_dict() or {} for d in docs]


def get_linked_player_for(chat_id: int, telegram_user_id: int) -> Optional[dict]:
    """The squad player a given Telegram person is permanently linked to, if any."""
    db = get_db()
    doc = db.collection(COL_PLAYER_LINKS).document(f"{chat_id}_{telegram_user_id}").get()
    return doc.to_dict() if doc.exists else None


def save_player_link(chat_id: int, telegram_user_id: int, telegram_name: str, team_name: str,
                      website_team_id: str, player_id: str, player_name: str) -> None:
    db = get_db()
    db.collection(COL_PLAYER_LINKS).document(f"{chat_id}_{telegram_user_id}").set({
        "chat_id": chat_id,
        "telegram_user_id": telegram_user_id,
        "telegram_name": telegram_name,
        "team_name": team_name,
        "website_team_id": website_team_id,
        "player_id": player_id,
        "player_name": player_name,
    })


def resolve_scoring_identity(chat_id: int, target_user_id: int) -> "tuple[str, int|str]":
    """Which identity to check against a team's on-field lineup: the real squad player this
    Telegram person is linked to, if any, else their raw Telegram id (unlinked/ad-hoc games)."""
    link = get_linked_player_for(chat_id, target_user_id)
    if link:
        return "player", link["player_id"]
    return "telegram", target_user_id


MATCH_COIN_REWARD = {"W": 15000, "D": 5000, "L": 0}


def calculate_market_value(player: dict) -> int:
    base = player.get("marketValue") or 50000
    goals = player.get("goals") or 0
    assists = player.get("assists") or 0
    apps = player.get("appearances") or 0
    yc = player.get("yellowCards") or 0
    rc = player.get("redCards") or 0
    rating = player.get("rating") or 70
    perf_bonus = (goals * 8000) + (assists * 4000) + (apps * 1000)
    card_penalty = (yc * 1000) + (rc * 5000)
    rating_mult = 0.5 + (rating / 100)
    return max(10000, round((base + perf_bonus - card_penalty) * rating_mult))


def apply_team_stats(team_id: str, league_id: Optional[str], gf: int, ga: int, result: str) -> None:
    """Mirrors league.js's applyTeamStats: updates a per-league teamEntries doc if one exists,
    else the team doc directly, plus the match-result coin reward on the team's budget."""
    db = get_db()
    pts = 3 if result == "W" else 1 if result == "D" else 0
    coin_reward = MATCH_COIN_REWARD.get(result, 0)

    entry_ref = db.collection(WEBSITE_TEAM_ENTRIES_COL).document(f"{team_id}_{league_id}") if league_id else None
    entry_snap = entry_ref.get() if entry_ref else None

    if entry_snap is not None and entry_snap.exists:
        e = entry_snap.to_dict() or {}
        form = (e.get("form") or []) + [result]
        form = form[-5:]
        entry_ref.set({
            "played": (e.get("played") or 0) + 1,
            "won": (e.get("won") or 0) + (1 if result == "W" else 0),
            "drawn": (e.get("drawn") or 0) + (1 if result == "D" else 0),
            "lost": (e.get("lost") or 0) + (1 if result == "L" else 0),
            "gf": (e.get("gf") or 0) + gf,
            "ga": (e.get("ga") or 0) + ga,
            "points": (e.get("points") or 0) + pts,
            "form": form,
        }, merge=True)
    else:
        ref = db.collection(WEBSITE_TEAMS_COL).document(team_id)
        snap = ref.get()
        if snap.exists:
            t = snap.to_dict() or {}
            form = (t.get("form") or []) + [result]
            form = form[-5:]
            ref.update({
                "played": (t.get("played") or 0) + 1,
                "won": (t.get("won") or 0) + (1 if result == "W" else 0),
                "drawn": (t.get("drawn") or 0) + (1 if result == "D" else 0),
                "lost": (t.get("lost") or 0) + (1 if result == "L" else 0),
                "gf": (t.get("gf") or 0) + gf,
                "ga": (t.get("ga") or 0) + ga,
                "points": (t.get("points") or 0) + pts,
                "form": form,
            })

    if coin_reward:
        try:
            ref = db.collection(WEBSITE_TEAMS_COL).document(team_id)
            snap = ref.get()
            if snap.exists:
                ref.update({"budget": (snap.to_dict() or {}).get("budget", 0) + coin_reward})
        except Exception:
            pass


def apply_match_stats_to_teams_and_players(home_team_id: str, away_team_id: str, home_score: int,
                                            away_score: int, events: list, lineup: list,
                                            league_id: Optional[str]) -> None:
    """Mirrors league.js's applyMatchStatsToTeamsAndPlayers exactly."""
    hg, ag = home_score, away_score
    h_res = "W" if hg > ag else "L" if hg < ag else "D"
    a_res = "L" if hg > ag else "W" if hg < ag else "D"

    apply_team_stats(home_team_id, league_id, hg, ag, h_res)
    apply_team_stats(away_team_id, league_id, ag, hg, a_res)

    db = get_db()
    event_player_ids = set()
    player_stats: dict = {}
    for e in events or []:
        pid = e.get("playerId")
        if not pid or e.get("type") == "mvp":
            continue
        event_player_ids.add(pid)
        s = player_stats.setdefault(pid, {"goals": 0, "ownGoals": 0, "assists": 0, "yellowCards": 0, "redCards": 0})
        t = e.get("type")
        if t == "goal":
            s["goals"] += 1
        elif t == "own_goal":
            s["ownGoals"] += 1
        elif t == "assist":
            s["assists"] += 1
        elif t == "yellow_card":
            s["yellowCards"] += 1
        elif t == "red_card":
            s["redCards"] += 1

    for pid in event_player_ids:
        try:
            s = player_stats.get(pid, {})
            ref = db.collection(WEBSITE_PLAYERS_COL).document(pid)
            snap = ref.get()
            if not snap.exists:
                continue
            p = snap.to_dict() or {}
            upd = {"appearances": (p.get("appearances") or 0) + 1}
            if s.get("goals"):
                upd["goals"] = (p.get("goals") or 0) + s["goals"]
            if s.get("ownGoals"):
                upd["ownGoals"] = (p.get("ownGoals") or 0) + s["ownGoals"]
            if s.get("assists"):
                upd["assists"] = (p.get("assists") or 0) + s["assists"]
            if s.get("yellowCards"):
                upd["yellowCards"] = (p.get("yellowCards") or 0) + s["yellowCards"]
            if s.get("redCards"):
                upd["redCards"] = (p.get("redCards") or 0) + s["redCards"]
            ref.update(upd)
            if league_id:
                db.collection(WEBSITE_PLAYER_ENTRIES_COL).document(f"{pid}_{league_id}").set(
                    {"playerId": pid, "leagueId": league_id, **upd}, merge=True
                )
            merged = {**p, **upd}
            ref.update({"marketValue": calculate_market_value(merged)})
        except Exception as e:
            print(f"player update failed for {pid}: {e}")

    for pid in lineup or []:
        if pid in event_player_ids:
            continue
        try:
            ref = db.collection(WEBSITE_PLAYERS_COL).document(pid)
            snap = ref.get()
            if not snap.exists:
                continue
            p = snap.to_dict() or {}
            new_apps = (p.get("appearances") or 0) + 1
            ref.update({"appearances": new_apps})
            if league_id:
                db.collection(WEBSITE_PLAYER_ENTRIES_COL).document(f"{pid}_{league_id}").set(
                    {"playerId": pid, "leagueId": league_id, "appearances": new_apps}, merge=True
                )
            merged = {**p, "appearances": new_apps}
            ref.update({"marketValue": calculate_market_value(merged)})
        except Exception as e:
            print(f"lineup appearance update failed for {pid}: {e}")


def write_result_to_website(match_id: str, home_score: int, away_score: int, events: list,
                             lineup: list, league_id: Optional[str] = LEAGUE_ID) -> None:
    """Mirrors league.js's updateMatchScore: writes the score into the real match doc and
    applies the same team/player stats update your website's own admin panel would."""
    db = get_db()
    match_ref = db.collection(WEBSITE_MATCHES_COL).document(match_id)
    match_snap = match_ref.get()
    if not match_snap.exists:
        raise ValueError(f"Website match {match_id} not found")
    match = match_snap.to_dict() or {}

    match_ref.update({
        "homeScore": home_score,
        "awayScore": away_score,
        "status": "played",
        "events": events,
        "lineup": lineup,
    })

    apply_match_stats_to_teams_and_players(
        match["homeTeamId"], match["awayTeamId"], home_score, away_score,
        events, lineup, match.get("leagueId", league_id),
    )


def fetch_unplayed_league_matches(league_id: str = LEAGUE_ID) -> list:
    db = get_db()
    docs = db.collection(WEBSITE_MATCHES_COL).where("leagueId", "==", league_id).stream()
    matches = [{"id": d.id, **(d.to_dict() or {})} for d in docs]
    return [m for m in matches if m.get("status") != "played"]


def has_valid_lineup(info: dict) -> bool:
    lineup = info.get("lineup")
    return bool(lineup and len(lineup.get("starters", [])) > 0)


def format_lineup(info: dict) -> str:
    lineup = info.get("lineup")
    if not lineup:
        return "(no lineup submitted yet)"
    names = lineup.get("names") or {}
    members = info["members"]
    on_field = set(lineup.get("on_field", []))

    def name_of(pid):
        return names.get(str(pid)) or members.get(pid, "?")

    starter_names = [f"{html.escape(name_of(pid))}{' 🟢' if pid in on_field else ''}" for pid in lineup["starters"]]
    sub_names = [f"{html.escape(name_of(pid))}{' 🟢' if pid in on_field else ''}" for pid in lineup["subs"]]
    return f"Starting 5: {', '.join(starter_names)}\nSubs: {', '.join(sub_names) if sub_names else '(none)'}"


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
    db = get_db()
    for chat_id, state in quiz_states.items():
        db.collection(COL_STATE).document(str(chat_id)).set({
            "questions": [{"q": q, "a": a} for q, a in state.questions],
            "game_active": state.game_active,
            "questions_asked_this_game": state.questions_asked_this_game,
        })


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
    db = get_db()
    for doc in db.collection(COL_STATE).stream():
        state = get_state(int(doc.id))
        saved = doc.to_dict() or {}
        raw_questions = [
            (item["q"], item["a"]) if isinstance(item, dict) else tuple(item)
            for item in saved.get("questions", [])
        ]
        state.questions, _removed = dedupe_pool(raw_questions)  # clean out any duplicates from before this fix
        state.game_active = saved.get("game_active", False)
        state.questions_asked_this_game = saved.get("questions_asked_this_game", 0)


def save_pending():
    db = get_db()
    db.collection(COL_PENDING).document("shared").set(
        {"questions": [{"q": q, "a": a} for q, a in pending_questions]}
    )


def load_pending():
    global pending_questions
    db = get_db()
    doc = db.collection(COL_PENDING).document("shared").get()
    if doc.exists:
        items = (doc.to_dict() or {}).get("questions", [])
        pending_questions = [
            (item["q"], item["a"]) if isinstance(item, dict) else tuple(item)
            for item in items
        ]


def save_teams():
    db = get_db()
    for chat_id, chat_teams in teams.items():
        data = {
            name: {
                "score": info["score"],
                "members": {str(uid): mname for uid, mname in info["members"].items()},
                "captain": info.get("captain"),
                "lineup": info.get("lineup"),
            }
            for name, info in chat_teams.items()
        }
        db.collection(COL_TEAMS).document(str(chat_id)).set(data)


def load_teams():
    db = get_db()
    for doc in db.collection(COL_TEAMS).stream():
        chat_teams = doc.to_dict() or {}
        converted = {}
        for name, info in chat_teams.items():
            raw_members = info.get("members", {})
            if isinstance(raw_members, list):
                # Old format from before named lineups: just a list of user_ids, no names stored.
                # Members should re-run /jointeam once so their real name gets filled in.
                members = {int(uid): f"Member {uid}" for uid in raw_members}
            else:
                members = {int(uid): mname for uid, mname in raw_members.items()}
            converted[name] = {
                "score": info.get("score", 0),
                "members": members,
                "captain": info.get("captain"),
                "lineup": info.get("lineup"),
            }
        teams[int(doc.id)] = converted


def save_leaderboard():
    db = get_db()
    for chat_id, entries in leaderboard.items():
        db.collection(COL_LEADERBOARD).document(str(chat_id)).set(entries)


def load_leaderboard():
    db = get_db()
    for doc in db.collection(COL_LEADERBOARD).stream():
        leaderboard[int(doc.id)] = doc.to_dict() or {}


def save_fixtures():
    db = get_db()
    for chat_id, chat_fixtures in fixtures.items():
        db.collection(COL_FIXTURES).document(str(chat_id)).set({
            "fixtures": chat_fixtures,
            "board_message_id": fixture_boards.get(chat_id),
        })


def load_fixtures():
    db = get_db()
    for doc in db.collection(COL_FIXTURES).stream():
        chat_id = int(doc.id)
        saved = doc.to_dict() or {}
        fixtures[chat_id] = saved.get("fixtures", [])
        if saved.get("board_message_id"):
            fixture_boards[chat_id] = saved["board_message_id"]


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
    stats = format_match_stats(state)
    stats_block = f"\n\n{stats}" if stats else ""

    await context.bot.send_message(
        chat_id,
        f"<b>🏁 Game over! Final {label}:\n{scoreboard}\n\n{winner_line}{stats_block}\n\n"
        "Run /startquiz to start a new game (scores will reset).</b>",
        parse_mode=ParseMode.HTML,
    )

    if is_team_mode(state) and state.linked_match_id:
        await write_linked_match_result(chat_id, context.bot, state, chat_teams)


async def write_linked_match_result(chat_id: int, bot, state: "QuizState", chat_teams: dict) -> None:
    home_name = state.linked_home_team_name
    away_name = state.linked_away_team_name
    home_score = chat_teams.get(home_name, {}).get("score", 0)
    away_score = chat_teams.get(away_name, {}).get("score", 0)

    lineup_player_ids = []
    for name in (home_name, away_name):
        info = get_teams(chat_id).get(name, {})
        lu = info.get("lineup") or {}
        if lu.get("source") == "squad":
            lineup_player_ids.extend(lu.get("starters", []))

    try:
        write_result_to_website(
            state.linked_match_id, home_score, away_score,
            state.match_events, lineup_player_ids, state.linked_league_id,
        )
        await bot.send_message(
            chat_id,
            f"<b>🌐 Result saved to the website: {html.escape(home_name)} {home_score} - "
            f"{away_score} {html.escape(away_name)}</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await bot.send_message(
            chat_id,
            f"<b>⚠️ Couldn't save this result to the website:</b> {html.escape(str(e))}\n"
            "The Telegram-side scores above are still correct — this only affects the website sync.",
            parse_mode=ParseMode.HTML,
        )

    state.linked_match_id = None
    state.linked_home_team_name = None
    state.linked_away_team_name = None
    state.linked_home_team_id = None
    state.linked_away_team_id = None
    state.linked_league_id = None
    state.match_events = []
    save_all_state()

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
        info["members"].pop(user_id, None)
    chat_teams[team_name]["members"][user_id] = query.from_user.first_name
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

    chat_teams[name] = {"score": 0, "members": {}, "captain": None, "lineup": None}
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
        info["members"].pop(user_id, None)
    chat_teams[match]["members"][user_id] = update.effective_user.first_name
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


def build_link_telegram_keyboard(token: str, members: dict) -> InlineKeyboardMarkup:
    rows, row = [], []
    for uid, name in members.items():
        row.append(InlineKeyboardButton(name, callback_data=f"lnktg:{token}:{uid}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✖️ Cancel", callback_data=f"lnkcancel:{token}")])
    return InlineKeyboardMarkup(rows)


def build_link_player_keyboard(token: str, squad: list) -> InlineKeyboardMarkup:
    rows, row = [], []
    for p in squad:
        row.append(InlineKeyboardButton(p.get("name", "?"), callback_data=f"lnkpl:{token}:{p['id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✖️ Cancel", callback_data=f"lnkcancel:{token}")])
    return InlineKeyboardMarkup(rows)


async def linkplayers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only. Usage: /squadpair Team Name — links Telegram members to real website squad players."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("Run this inside the group.")
        return
    if not await is_admin(update, context):
        await update.message.reply_text("Only admins can link players.")
        return

    tg_team_arg = update.message.text.partition(" ")[2].strip()
    if not tg_team_arg:
        await update.message.reply_text("Usage: /squadpair Team Name")
        return

    chat_id = update.effective_chat.id
    tg_team = find_registered_team(chat_id, tg_team_arg)
    if not tg_team:
        await update.message.reply_text("No Telegram team by that name. Check /teams for the exact name.")
        return

    website_team = find_website_team_by_name(tg_team)
    if not website_team:
        await update.message.reply_text(
            f"No website team named '{tg_team}' found in Firestore's teams collection. "
            "The Telegram team name must exactly match the website team's name."
        )
        return

    squad = fetch_website_squad(website_team["id"])
    if not squad:
        await update.message.reply_text(f"No squad players found for '{tg_team}' on the website.")
        return

    telegram_members = get_teams(chat_id)[tg_team]["members"]
    if not telegram_members:
        await update.message.reply_text(
            f"{tg_team} has no Telegram members yet — have them /jointeam {tg_team} first."
        )
        return

    linked_uids = {link["telegram_user_id"] for link in get_player_links(chat_id)}
    unlinked = {uid: name for uid, name in telegram_members.items() if uid not in linked_uids}
    if not unlinked:
        await update.message.reply_text(
            f"Everyone on {tg_team} is already linked. Use /squadpairs {tg_team} to view them."
        )
        return

    token = secrets.token_hex(4)
    link_builders[token] = {
        "chat_id": chat_id,
        "tg_team": tg_team,
        "website_team_id": website_team["id"],
        "admin_id": update.effective_user.id,
    }
    keyboard = build_link_telegram_keyboard(token, unlinked)
    await update.message.reply_text(
        f"<b>🔗 Linking players for {html.escape(tg_team)}</b>\nPick a Telegram member to link:",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def link_pick_telegram_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, token, uid_str = query.data.split(":")
    uid = int(uid_str)
    builder = link_builders.get(token)
    if not builder:
        await query.answer("This session expired — run /squadpair again.", show_alert=True)
        return
    if query.from_user.id != builder["admin_id"]:
        await query.answer("Only the admin who started this can use it.", show_alert=True)
        return

    chat_id = builder["chat_id"]
    telegram_name = get_teams(chat_id)[builder["tg_team"]]["members"].get(uid, "?")
    builder["picked_telegram_uid"] = uid
    builder["picked_telegram_name"] = telegram_name

    squad = fetch_website_squad(builder["website_team_id"])
    linked_player_ids = {link["player_id"] for link in get_player_links(chat_id)}
    available_squad = [p for p in squad if p["id"] not in linked_player_ids]

    await query.answer()
    if not available_squad:
        await query.edit_message_text("No unlinked squad players left for this team.")
        del link_builders[token]
        return

    keyboard = build_link_player_keyboard(token, available_squad)
    await query.edit_message_text(
        f"<b>🔗 Linking {html.escape(telegram_name)}</b>\nPick their squad player:",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def link_pick_player_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, token, player_id = query.data.split(":", 2)
    builder = link_builders.get(token)
    if not builder:
        await query.answer("This session expired — run /squadpair again.", show_alert=True)
        return
    if query.from_user.id != builder["admin_id"]:
        await query.answer("Only the admin who started this can use it.", show_alert=True)
        return

    db = get_db()
    player_doc = db.collection(WEBSITE_PLAYERS_COL).document(player_id).get()
    player_name = (player_doc.to_dict() or {}).get("name", "?") if player_doc.exists else "?"

    chat_id = builder["chat_id"]
    uid = builder["picked_telegram_uid"]
    save_player_link(
        chat_id, uid, builder["picked_telegram_name"], builder["tg_team"],
        builder["website_team_id"], player_id, player_name,
    )
    await query.answer(f"Linked to {player_name}!")

    telegram_members = get_teams(chat_id)[builder["tg_team"]]["members"]
    linked_uids = {link["telegram_user_id"] for link in get_player_links(chat_id)}
    remaining = {u: n for u, n in telegram_members.items() if u not in linked_uids}

    if not remaining:
        await query.edit_message_text(
            f"<b>✅ Everyone on {html.escape(builder['tg_team'])} is now linked!</b>", parse_mode=ParseMode.HTML
        )
        del link_builders[token]
        return

    keyboard = build_link_telegram_keyboard(token, remaining)
    await query.edit_message_text(
        f"<b>🔗 Linked to {html.escape(player_name)}! Pick the next Telegram member:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def link_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, token = query.data.split(":")
    builder = link_builders.get(token)
    if builder and query.from_user.id != builder["admin_id"]:
        await query.answer("Only the admin who started this can cancel it.", show_alert=True)
        return
    link_builders.pop(token, None)
    await query.answer("Cancelled.")
    await query.edit_message_text("Player linking cancelled. Run /squadpair again to retry.")


async def links_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /squadpairs Team Name — shows the permanent Telegram-person <-> squad-player links for a team."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("Run this inside the group.")
        return
    team_arg = update.message.text.partition(" ")[2].strip()
    if not team_arg:
        await update.message.reply_text("Usage: /squadpairs Team Name")
        return

    chat_id = update.effective_chat.id
    matching = [link for link in get_player_links(chat_id) if link.get("team_name", "").lower() == team_arg.lower()]
    if not matching:
        await update.message.reply_text(f"No links yet for '{team_arg}'. Use /squadpair {team_arg} to set them up.")
        return

    lines = [f"{html.escape(l['telegram_name'])} → {html.escape(l['player_name'])}" for l in matching]
    await update.message.reply_text("<b>🔗 Player links:</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML)


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


async def setcaptain_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: reply to a team member's message with /setcaptain to make them that team's captain."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("Run this inside the group.")
        return
    if not await is_admin(update, context):
        await update.message.reply_text("Only admins can do this.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to the member's message with /setcaptain to make them a captain.")
        return

    chat_id = update.effective_chat.id
    target_user = update.message.reply_to_message.from_user
    team_name = find_team_of_user(chat_id, target_user.id)
    if not team_name:
        await update.message.reply_text(f"{target_user.first_name} isn't on a team yet — have them /jointeam first.")
        return

    get_teams(chat_id)[team_name]["captain"] = target_user.id
    save_teams()
    await update.message.reply_text(f"👑 {target_user.first_name} is now captain of {team_name}.")


def build_lineup_keyboard(token: str, members: dict, selected: set, pool: list) -> InlineKeyboardMarkup:
    rows, row = [], []
    for uid in pool:
        label = f"{'✅ ' if uid in selected else ''}{members.get(uid, '?')}"
        row.append(InlineKeyboardButton(label, callback_data=f"lbtoggle:{token}:{uid}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"lbconfirm:{token}"),
            InlineKeyboardButton("✖️ Cancel", callback_data=f"lbcancel:{token}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def get_lineup_pool(builder: dict) -> dict:
    """The {id: name} pool a lineup builder session picks from — real squad players for an
    auto-triggered website-linked match, or Telegram team members for a manual /lineup."""
    if builder.get("source") == "squad":
        squad = fetch_website_squad(builder["website_team_id"])
        return {p["id"]: p.get("name", "?") for p in squad}
    return get_teams(builder["chat_id"])[builder["team_name"]]["members"]


def compute_lineup_requirements(pool_size: int) -> tuple:
    """Scale down starters/subs needed when a squad has fewer than 8 available players."""
    starters = min(LINEUP_STARTERS, pool_size)
    subs = min(LINEUP_SUBS, max(0, pool_size - starters))
    return starters, subs


async def lineup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Captain-only: start a tap-to-pick lineup selection."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("Run this inside the group.")
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    team_name = find_team_of_captain(chat_id, user_id)
    if not team_name:
        await update.message.reply_text("Only a team's captain can submit its lineup. Ask an admin for /setcaptain.")
        return

    info = get_teams(chat_id)[team_name]
    members = info["members"]
    if not members:
        await update.message.reply_text(f"{team_name} has no members yet.")
        return
    starters_needed, subs_needed = compute_lineup_requirements(len(members))

    # Drop any stale in-progress session for this team so only one is active at a time.
    for tok in [t for t, b in lineup_builders.items() if b["chat_id"] == chat_id and b["team_name"] == team_name]:
        del lineup_builders[tok]

    token = secrets.token_hex(4)
    lineup_builders[token] = {
        "chat_id": chat_id,
        "team_name": team_name,
        "captain_id": user_id,
        "stage": "starters",
        "source": "telegram",
        "starters_needed": starters_needed,
        "subs_needed": subs_needed,
        "starters": set(),
        "subs": set(),
    }
    pool = list(members.keys())
    keyboard = build_lineup_keyboard(token, members, set(), pool)
    await update.message.reply_text(
        f"<b>📋 Pick {starters_needed} starters for {html.escape(team_name)} (0/{starters_needed} selected)</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def lineup_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, token, uid_str = query.data.split(":")
    builder = lineup_builders.get(token)
    if not builder:
        await query.answer("This session expired — run /lineup again.", show_alert=True)
        return
    if query.from_user.id != builder["captain_id"]:
        await query.answer("Only the captain who started this can edit it.", show_alert=True)
        return

    uid = uid_str if builder.get("source") == "squad" else int(uid_str)
    stage = builder["stage"]
    current = builder[stage]
    limit = builder["starters_needed"] if stage == "starters" else builder["subs_needed"]

    if uid in current:
        current.discard(uid)
    elif len(current) >= limit:
        await query.answer(f"You can only pick {limit}.", show_alert=True)
        return
    else:
        current.add(uid)

    await query.answer()
    team_name = builder["team_name"]
    members = get_lineup_pool(builder)
    if stage == "starters":
        pool = list(members.keys())
        header = f"📋 Pick {builder['starters_needed']} starters for {html.escape(team_name)} ({len(current)}/{builder['starters_needed']} selected)"
    else:
        pool = [m for m in members if m not in builder["starters"]]
        header = f"📋 Pick {builder['subs_needed']} subs for {html.escape(team_name)} ({len(current)}/{builder['subs_needed']} selected)"
    keyboard = build_lineup_keyboard(token, members, current, pool)
    await query.edit_message_text(f"<b>{header}</b>", parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def lineup_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, token = query.data.split(":")
    builder = lineup_builders.get(token)
    if not builder:
        await query.answer("This session expired — run /lineup again.", show_alert=True)
        return
    if query.from_user.id != builder["captain_id"]:
        await query.answer("Only the captain who started this can confirm it.", show_alert=True)
        return

    chat_id, team_name = builder["chat_id"], builder["team_name"]
    starters_needed, subs_needed = builder["starters_needed"], builder["subs_needed"]

    if builder["stage"] == "starters":
        if len(builder["starters"]) != starters_needed:
            await query.answer(f"Pick exactly {starters_needed} starters first.", show_alert=True)
            return
        if subs_needed == 0:
            builder["stage"] = "subs"  # nothing to pick — fall through to finalize below
        else:
            builder["stage"] = "subs"
            await query.answer()
            members = get_lineup_pool(builder)
            pool = [m for m in members if m not in builder["starters"]]
            keyboard = build_lineup_keyboard(token, members, builder["subs"], pool)
            await query.edit_message_text(
                f"<b>📋 Pick {subs_needed} subs for {html.escape(team_name)} (0/{subs_needed} selected)</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            return

    if len(builder["subs"]) != subs_needed:
        await query.answer(f"Pick exactly {subs_needed} subs first.", show_alert=True)
        return

    info = get_teams(chat_id)[team_name]
    starters, subs = list(builder["starters"]), list(builder["subs"])
    pool = get_lineup_pool(builder)
    info["lineup"] = {
        "starters": starters, "subs": subs, "on_field": list(starters),
        "names": {str(pid): pool.get(pid, "?") for pid in starters + subs},
        "source": builder.get("source", "telegram"),
    }
    save_teams()
    del lineup_builders[token]

    await query.answer("Lineup submitted!")
    await query.edit_message_text(
        f"<b>✅ Lineup submitted for {html.escape(team_name)}!</b>\n\n{format_lineup(info)}",
        parse_mode=ParseMode.HTML,
    )


async def lineup_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, token = query.data.split(":")
    builder = lineup_builders.get(token)
    if builder and query.from_user.id != builder["captain_id"]:
        await query.answer("Only the captain can cancel this.", show_alert=True)
        return
    lineup_builders.pop(token, None)
    await query.answer("Cancelled.")
    await query.edit_message_text("Lineup submission cancelled. Run /lineup to try again.")


def build_sub_pick_keyboard(token: str, members: dict, pool: list, prefix: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for uid in pool:
        row.append(InlineKeyboardButton(members.get(uid, "?"), callback_data=f"{prefix}:{token}:{uid}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✖️ Cancel", callback_data=f"subcancel:{token}")])
    return InlineKeyboardMarkup(rows)


async def sub_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Captain-only: start a tap-to-pick substitution."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("Run this inside the group.")
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    team_name = find_team_of_captain(chat_id, user_id)
    if not team_name:
        await update.message.reply_text("Only a team's captain can make substitutions.")
        return

    info = get_teams(chat_id)[team_name]
    if not has_valid_lineup(info):
        await update.message.reply_text("Submit a lineup first with /lineup.")
        return

    on_field = info["lineup"]["on_field"]
    if not on_field:
        await update.message.reply_text("No one is currently on the field.")
        return

    for tok in [t for t, b in sub_builders.items() if b["chat_id"] == chat_id and b["team_name"] == team_name]:
        del sub_builders[tok]

    token = secrets.token_hex(4)
    is_squad = info["lineup"].get("source") == "squad"
    sub_builders[token] = {"chat_id": chat_id, "team_name": team_name, "captain_id": user_id, "out_id": None, "is_squad": is_squad}
    names = info["lineup"].get("names", {}) if is_squad else info["members"]
    keyboard = build_sub_pick_keyboard(token, names, on_field, "suboff")
    await update.message.reply_text(
        f"<b>🔄 Who's coming off for {html.escape(team_name)}?</b>", parse_mode=ParseMode.HTML, reply_markup=keyboard
    )


async def sub_pick_out_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, token, uid_str = query.data.split(":")
    builder = sub_builders.get(token)
    if not builder:
        await query.answer("This session expired — run /sub again.", show_alert=True)
        return
    if query.from_user.id != builder["captain_id"]:
        await query.answer("Only the captain can do this.", show_alert=True)
        return

    uid = uid_str if builder.get("is_squad") else int(uid_str)
    builder["out_id"] = uid
    chat_id, team_name = builder["chat_id"], builder["team_name"]
    info = get_teams(chat_id)[team_name]
    on_field = set(info["lineup"]["on_field"])
    subs_pool = [s for s in info["lineup"]["subs"] if s not in on_field]
    names = info["lineup"].get("names", {}) if builder.get("is_squad") else info["members"]
    out_name = names.get(uid, "?")

    await query.answer()
    if not subs_pool:
        await query.edit_message_text(f"No available subs to bring on for {html.escape(out_name)}.")
        del sub_builders[token]
        return

    keyboard = build_sub_pick_keyboard(token, names, subs_pool, "subon")
    await query.edit_message_text(
        f"<b>🔄 {html.escape(out_name)} is coming off. Who's coming ON?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def sub_pick_in_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, token, uid_str = query.data.split(":")
    builder = sub_builders.get(token)
    if not builder:
        await query.answer("This session expired — run /sub again.", show_alert=True)
        return
    if query.from_user.id != builder["captain_id"]:
        await query.answer("Only the captain can do this.", show_alert=True)
        return

    uid = uid_str if builder.get("is_squad") else int(uid_str)
    chat_id, team_name = builder["chat_id"], builder["team_name"]
    info = get_teams(chat_id)[team_name]
    on_field = set(info["lineup"]["on_field"])
    out_id = builder["out_id"]
    on_field.discard(out_id)
    on_field.add(uid)
    info["lineup"]["on_field"] = list(on_field)
    save_teams()
    del sub_builders[token]

    names = info["lineup"].get("names", {}) if builder.get("is_squad") else info["members"]
    await query.answer("Substitution made!")
    await query.edit_message_text(
        f"<b>🔄 Substitution for {html.escape(team_name)}: {html.escape(names.get(out_id, '?'))} ➡️ off, "
        f"{html.escape(names.get(uid, '?'))} ➡️ on.</b>",
        parse_mode=ParseMode.HTML,
    )


async def sub_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, token = query.data.split(":")
    builder = sub_builders.get(token)
    if builder and query.from_user.id != builder["captain_id"]:
        await query.answer("Only the captain can cancel this.", show_alert=True)
        return
    sub_builders.pop(token, None)
    await query.answer("Cancelled.")
    await query.edit_message_text("Substitution cancelled. Run /sub to try again.")


async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Group only: show current standings for the open (no-team) leaderboard mode."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("Run this inside the group.")
        return
    await update.message.reply_text("Leaderboard:\n" + format_leaderboard(update.effective_chat.id))


def ensure_fixtures_job(chat_id: int, job_queue) -> None:
    """Start the repeating countdown-refresh job for this chat if one isn't already running."""
    if job_queue.get_jobs_by_name(f"fixtures_{chat_id}"):
        return
    job_queue.run_repeating(
        refresh_fixtures_board,
        interval=FIXTURES_REFRESH_SECONDS,
        first=1,
        data={"chat_id": chat_id},
        name=f"fixtures_{chat_id}",
    )


FIXTURE_LINEUP_REMINDER_MINUTES = 5


async def send_lineup_reminder(chat_id: int, bot, fixture: dict) -> None:
    mentions = []
    for typed_name in (fixture["team_a"], fixture["team_b"]):
        team_name = find_registered_team(chat_id, typed_name)
        if not team_name:
            continue
        for uid, name in get_teams(chat_id)[team_name]["members"].items():
            mentions.append(f'<a href="tg://user?id={uid}">{html.escape(name)}</a>')

    text = (
        f"<b>⏰ {FIXTURE_LINEUP_REMINDER_MINUTES} minutes to kickoff: "
        f"{html.escape(fixture['team_a'])} vs {html.escape(fixture['team_b'])}!</b>\n"
        "Captains, submit your lineup now with /lineup."
    )
    if mentions:
        text += "\n\n" + ", ".join(mentions)

    await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)


async def post_squad_lineup_pickers(chat_id: int, bot, fixture: dict) -> None:
    """6 minutes before a website-linked match: auto-post real-squad tap-to-pick lineup
    buttons for both teams' captains, instead of waiting for a manual /lineup."""
    sides = [
        (fixture["team_a"], fixture.get("home_team_id")),
        (fixture["team_b"], fixture.get("away_team_id")),
    ]
    for tg_team_name, website_team_id in sides:
        team_name = find_registered_team(chat_id, tg_team_name)
        if not team_name or not website_team_id:
            continue
        info = get_teams(chat_id)[team_name]
        captain_id = info.get("captain")
        if not captain_id:
            await bot.send_message(
                chat_id,
                f"<b>⚠️ {html.escape(team_name)} has no captain set — an admin needs to /setcaptain "
                "before a lineup can be picked for this match.</b>",
                parse_mode=ParseMode.HTML,
            )
            continue

        squad = fetch_website_squad(website_team_id)
        if not squad:
            await bot.send_message(
                chat_id,
                f"<b>⚠️ {html.escape(team_name)} has no players registered on the website — "
                "a lineup can't be picked.</b>",
                parse_mode=ParseMode.HTML,
            )
            continue
        starters_needed, subs_needed = compute_lineup_requirements(len(squad))

        for tok in [t for t, b in lineup_builders.items() if b["chat_id"] == chat_id and b["team_name"] == team_name]:
            del lineup_builders[tok]

        token = secrets.token_hex(4)
        lineup_builders[token] = {
            "chat_id": chat_id, "team_name": team_name, "captain_id": captain_id,
            "stage": "starters", "source": "squad", "website_team_id": website_team_id,
            "starters_needed": starters_needed, "subs_needed": subs_needed,
            "starters": set(), "subs": set(),
        }
        pool = {p["id"]: p.get("name", "?") for p in squad}
        keyboard = build_lineup_keyboard(token, pool, set(), list(pool.keys()))
        await bot.send_message(
            chat_id,
            f"<b>📋 {html.escape(team_name)} — {FIXTURE_SQUAD_LINEUP_MINUTES} minutes to kickoff! "
            f"Captain, pick your {starters_needed} starters (0/{starters_needed} selected)</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )


async def refresh_fixtures_board_now(chat_id: int, bot) -> bool:
    """Does the actual post/edit of the fixtures board. Returns True if nothing is left to count down to."""
    chat_fixtures = get_fixtures(chat_id)
    now = datetime.now(timezone.utc)

    changed = False
    for f in chat_fixtures:
        if f["started"]:
            continue
        kickoff = datetime.fromisoformat(f["kickoff_utc"])

        if f.get("match_id") and not f.get("squad_lineup_posted") and kickoff - now <= timedelta(minutes=FIXTURE_SQUAD_LINEUP_MINUTES):
            f["squad_lineup_posted"] = True
            changed = True
            await post_squad_lineup_pickers(chat_id, bot, f)
        elif not f.get("match_id") and not f.get("reminder_sent") and kickoff - now <= timedelta(minutes=FIXTURE_LINEUP_REMINDER_MINUTES):
            f["reminder_sent"] = True
            changed = True
            await send_lineup_reminder(chat_id, bot, f)

        if now >= kickoff:
            f["started"] = True
            changed = True
            await bot.send_message(
                chat_id,
                f"<b>⏰ Kickoff! {html.escape(f['team_a'])} vs {html.escape(f['team_b'])} starts now!</b>",
                parse_mode=ParseMode.HTML,
            )
    if changed:
        save_fixtures()

    text = build_fixtures_text(chat_id)
    message_id = fixture_boards.get(chat_id)

    if message_id is None:
        msg = await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        fixture_boards[chat_id] = msg.message_id
        save_fixtures()
        try:
            await bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
        except BadRequest:
            pass  # bot probably isn't an admin with pin rights — board still works, just not pinned
    else:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode=ParseMode.HTML)
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                pass  # countdown text happened to round to the same minute — fine, nothing to do
            else:
                # original board message is gone (deleted, too old to edit, etc.) — post a fresh one
                msg = await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
                fixture_boards[chat_id] = msg.message_id
                save_fixtures()
                try:
                    await bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
                except BadRequest:
                    pass

    return not any(not f["started"] for f in chat_fixtures)


async def refresh_fixtures_board(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    nothing_left = await refresh_fixtures_board_now(chat_id, context.bot)
    if nothing_left:
        context.job.schedule_removal()


def build_matchday_picker_keyboard(matches: list) -> InlineKeyboardMarkup:
    by_matchday: dict = {}
    for m in matches:
        by_matchday.setdefault(m.get("matchday", "?"), []).append(m)
    rows = []
    for md in sorted(by_matchday, key=lambda x: (isinstance(x, str), x)):
        count = len(by_matchday[md])
        rows.append([InlineKeyboardButton(f"Matchday {md} ({count} fixture{'s' if count != 1 else ''})",
                                           callback_data=f"schedmd:{md}")])
    return InlineKeyboardMarkup(rows)


def build_match_picker_keyboard(matches: list) -> InlineKeyboardMarkup:
    rows = []
    for m in matches:
        label = f"{m.get('homeTeamName', '?')} vs {m.get('awayTeamName', '?')}"
        rows.append([InlineKeyboardButton(label[:64], callback_data=f"schedmatch:{m['id']}")])
    rows.append([InlineKeyboardButton("« Back to matchdays", callback_data="schedmdback")])
    return InlineKeyboardMarkup(rows)


async def schedulematch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: pick a real unplayed league match and give it a kickoff time."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("Run this inside the group.")
        return
    if not await is_admin(update, context):
        await update.message.reply_text("Only admins can schedule matches.")
        return

    matches = fetch_unplayed_league_matches()
    if not matches:
        await update.message.reply_text("No unplayed matches found for this league.")
        return

    await update.message.reply_text(
        f"{len(matches)} unplayed fixture(s). Pick a matchday:",
        reply_markup=build_matchday_picker_keyboard(matches),
    )


async def schedmd_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, matchday_str = query.data.split(":", 1)

    if not await is_admin(update, context):
        await query.answer("Only admins can do this.", show_alert=True)
        return

    matches = fetch_unplayed_league_matches()
    # matchday was stringified in callback_data — match it back against either int or str form
    matchday_matches = [m for m in matches if str(m.get("matchday", "?")) == matchday_str]
    if not matchday_matches:
        await query.answer("No fixtures found for that matchday anymore.", show_alert=True)
        return

    await query.answer()
    await query.edit_message_text(
        f"Matchday {matchday_str} — pick the fixture:",
        reply_markup=build_match_picker_keyboard(matchday_matches),
    )


async def schedmd_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await is_admin(update, context):
        await query.answer("Only admins can do this.", show_alert=True)
        return

    matches = fetch_unplayed_league_matches()
    if not matches:
        await query.answer()
        await query.edit_message_text("No unplayed matches found for this league.")
        return
    await query.answer()
    await query.edit_message_text(
        f"{len(matches)} unplayed fixture(s). Pick a matchday:",
        reply_markup=build_matchday_picker_keyboard(matches),
    )


async def schedmatch_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, match_id = query.data.split(":", 1)

    if not await is_admin(update, context):
        await query.answer("Only admins can do this.", show_alert=True)
        return

    db = get_db()
    snap = db.collection(WEBSITE_MATCHES_COL).document(match_id).get()
    if not snap.exists:
        await query.answer("That match no longer exists.", show_alert=True)
        return
    match = snap.to_dict() or {}

    chat_id = query.message.chat_id
    pending_schedule[chat_id] = {
        "match_id": match_id,
        "home_team_name": match.get("homeTeamName", "?"),
        "away_team_name": match.get("awayTeamName", "?"),
        "home_team_id": match.get("homeTeamId"),
        "away_team_id": match.get("awayTeamId"),
        "admin_id": query.from_user.id,
    }
    await query.answer()
    await query.edit_message_text(
        f"Selected: {match.get('homeTeamName')} vs {match.get('awayTeamName')}.\n\n"
        "Now send the kickoff date & time (Nigerian time), like: 2026-09-20 18:00"
    )


async def addfixture_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only. Usage: /addfixture Team A vs Team B | 2026-09-20 18:00"""
    if update.effective_chat.type == "private":
        await update.message.reply_text("Run this inside the group.")
        return
    if not await is_admin(update, context):
        await update.message.reply_text("Only admins can add fixtures.")
        return

    text = update.message.text.partition(" ")[2]
    if "|" not in text or " vs " not in text.lower():
        await update.message.reply_text("Usage: /addfixture Team A vs Team B | 2026-09-20 18:00")
        return

    teams_part, time_part = text.split("|", 1)
    lower = teams_part.lower()
    split_at = lower.index(" vs ")
    team_a = teams_part[:split_at].strip()
    team_b = teams_part[split_at + 4:].strip()
    if not team_a or not team_b:
        await update.message.reply_text("Couldn't read both team names. Usage: /addfixture Team A vs Team B | 2026-09-20 18:00")
        return

    kickoff_utc = parse_fixture_datetime(time_part)
    if kickoff_utc is None:
        await update.message.reply_text("Couldn't read the date/time. Use this format: 2026-09-20 18:00")
        return
    if kickoff_utc <= datetime.now(timezone.utc):
        await update.message.reply_text("That kickoff time is in the past — check the date/time and try again.")
        return

    chat_id = update.effective_chat.id
    get_fixtures(chat_id).append(
        {"team_a": team_a, "team_b": team_b, "kickoff_utc": kickoff_utc.isoformat(), "started": False, "reminder_sent": False}
    )
    save_fixtures()
    ensure_fixtures_job(chat_id, context.job_queue)
    await refresh_fixtures_board_now(chat_id, context.bot)
    await update.message.reply_text(f"✅ Added: {team_a} vs {team_b}")


async def fixtures_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Anyone: show the current fixtures list on demand."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("Run this inside the group.")
        return
    await update.message.reply_text(build_fixtures_text(update.effective_chat.id), parse_mode=ParseMode.HTML)


async def removefixture_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only. Usage: /removefixture N (N from the /fixtures list, 1-based)."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("Run this inside the group.")
        return
    if not await is_admin(update, context):
        await update.message.reply_text("Only admins can remove fixtures.")
        return

    chat_id = update.effective_chat.id
    chat_fixtures = [f for f in get_fixtures(chat_id) if not f["started"]]
    chat_fixtures.sort(key=lambda f: f["kickoff_utc"])

    arg = update.message.text.partition(" ")[2].strip()
    if not arg.isdigit() or not (1 <= int(arg) <= len(chat_fixtures)):
        await update.message.reply_text(f"Usage: /removefixture N — check /fixtures for the right number (1-{len(chat_fixtures)}).")
        return

    target = chat_fixtures[int(arg) - 1]
    get_fixtures(chat_id).remove(target)
    save_fixtures()
    await update.message.reply_text(f"🗑 Removed: {target['team_a']} vs {target['team_b']}")
    await refresh_fixtures_board_now(chat_id, context.bot)


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
            state.match_stats = {}
            state.match_events = []
            state.game_active = True

            # Auto-link to a real scheduled fixture if these two team names match one.
            state.linked_match_id = None
            state.linked_home_team_name = None
            state.linked_away_team_name = None
            state.linked_home_team_id = None
            state.linked_away_team_id = None
            state.linked_league_id = None
            matched_lower = {m.lower() for m in matched}
            for fx in get_fixtures(chat_id):
                if fx.get("match_id") and not fx["started"] and \
                        {fx["team_a"].lower(), fx["team_b"].lower()} == matched_lower:
                    state.linked_match_id = fx["match_id"]
                    state.linked_home_team_name = fx["team_a"]
                    state.linked_away_team_name = fx["team_b"]
                    state.linked_home_team_id = fx.get("home_team_id")
                    state.linked_away_team_id = fx.get("away_team_id")
                    state.linked_league_id = LEAGUE_ID
                    break
            save_all_state()

            playing_desc = ", ".join(html.escape(n) for n in sorted(matched))
            if state.linked_match_id:
                link_note = " This game is linked to a real website fixture."
                lineup_note = (
                    f"Lineups will be posted automatically {FIXTURE_SQUAD_LINEUP_MINUTES} minutes before kickoff."
                )
            else:
                link_note = ""
                lineup_note = (
                    f"📋 Each captain: submit your lineup with /lineup ({LINEUP_STARTERS} starters, {LINEUP_SUBS} subs) "
                    "before the first question can be posted."
                )
            await update.message.reply_text(
                f"<b>🎮 New team match started! Teams playing: {playing_desc}.{link_note} "
                f"First to be marked correct on each question scores a point. {GAME_LENGTH} questions per game.\n\n"
                f"{lineup_note}</b>",
                parse_mode=ParseMode.HTML,
            )
        else:
            # Individual leaderboard mode — end_game() already wiped the board when the last
            # game finished, so there's nothing to reset here.
            state.active_teams = None
            state.questions_asked_this_game = 0
            state.match_stats = {}
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

    if is_team_mode(state):
        chat_teams = get_teams(chat_id)
        missing = [t for t in state.active_teams if not has_valid_lineup(chat_teams.get(t, {}))]
        if missing:
            await query.answer()
            await query.edit_message_text(
                "<b>⏳ Waiting on lineups from: " + ", ".join(html.escape(m) for m in missing) + ". "
                "Each captain should send /lineup before the match can start.</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("▶️ Post next question", callback_data="post_question")]]
                ),
            )
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
    state.last_scorer_id = None
    state.last_assister_id = None
    state.assist_recorded_this_round = False
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
async def handle_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """If someone edits a message that was logged as an answer, call it out — the original still counts."""
    if update.effective_chat.type == "private":
        return
    edited = update.edited_message
    if edited is None or not edited.text:
        return

    chat_id = update.effective_chat.id
    state = quiz_states.get(chat_id)
    if not state:
        return

    match = next((entry for entry in state.submissions if entry[4] == edited.message_id), None)
    if not match:
        return  # not a message the bot was tracking as an answer

    _, name, original_text, _elapsed, _mid = match
    await context.bot.send_message(
        chat_id,
        f"<b>⚠️ {html.escape(name)} edited their answer after sending it — the original "
        f"(\"{html.escape(original_text)}\") is what counts for scoring.</b>",
        parse_mode=ParseMode.HTML,
    )


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        return
    message = update.message
    if message is None or not message.text:
        return

    chat_id = update.effective_chat.id
    state = quiz_states.get(chat_id)

    # If this admin just picked a match via /schedulematch, their next message is the kickoff time.
    pending = pending_schedule.get(chat_id)
    if pending and update.effective_user.id == pending["admin_id"]:
        kickoff_utc = parse_fixture_datetime(message.text)
        if kickoff_utc is None:
            await message.reply_text("Couldn't read that. Use this format: 2026-09-20 18:00")
            return
        if kickoff_utc <= datetime.now(timezone.utc):
            await message.reply_text("That kickoff time is in the past — try again.")
            return

        get_fixtures(chat_id).append({
            "team_a": pending["home_team_name"], "team_b": pending["away_team_name"],
            "kickoff_utc": kickoff_utc.isoformat(), "started": False, "reminder_sent": False,
            "match_id": pending["match_id"], "home_team_id": pending["home_team_id"],
            "away_team_id": pending["away_team_id"], "squad_lineup_posted": False,
        })
        save_fixtures()
        ensure_fixtures_job(chat_id, context.job_queue)
        await refresh_fixtures_board_now(chat_id, context.bot)
        del pending_schedule[chat_id]
        await message.reply_text(
            f"✅ Scheduled: {pending['home_team_name']} vs {pending['away_team_name']}"
        )
        return

    # Admin marking a correct answer: reply to the member's message with "Correct" (any case).
    if message.reply_to_message and message.text.strip().lower().startswith("correct"):
        if not await is_admin(update, context):
            return  # not an admin — ignore silently, don't spam the chat
        await resolve_correct(update, context, state, chat_id)
        return

    # Admin crediting an assist: reply to the member's message with "Assist" (any case).
    if message.reply_to_message and message.text.strip().lower().startswith("assist"):
        if not await is_admin(update, context):
            return
        await resolve_assist(update, context, state, chat_id)
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
        if state.last_scorer_id is not None:
            await update.message.reply_text(
                "A goal was already given for this question. Reply \"Assist\" to credit an assist, or /next to continue."
            )
            return

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

        team_info = get_teams(chat_id)[team_name]
        lineup_source = (team_info.get("lineup") or {}).get("source", "telegram")
        website_player_id = None

        if lineup_source == "squad":
            link = get_linked_player_for(chat_id, target_user.id)
            if not link:
                await update.message.reply_text(
                    f"{target_user.first_name} isn't linked to a squad player yet — "
                    f"an admin needs to run /squadpair {team_name}."
                )
                return
            website_player_id = link["player_id"]
            check_value = website_player_id
        else:
            check_value = target_user.id

        if has_valid_lineup(team_info) and check_value not in set(team_info["lineup"]["on_field"]):
            await update.message.reply_text(
                f"{target_user.first_name} isn't currently on the field for {team_name} "
                "(not in the lineup, or subbed off). Captain can /sub them on if needed."
            )
            return

        team_info["score"] += 1
        save_teams()
        state.last_scorer_id = target_user.id
        state.last_award = {
            "mode": "team", "team_name": team_name, "points": 1,
            "user_id": target_user.id, "user_name": target_user.first_name,
        }
        record_match_stat(state, target_user.id, target_user.first_name, "goals")
        if website_player_id:
            state.match_events.append({"type": "goal", "playerId": website_player_id})

        await update.message.reply_text(
            f"<b>⚽ GOAL! {tag_mention(target_user.id, target_user.first_name)} earns team "
            f"{html.escape(team_name)} a point!\n\n"
            f"📊 Scores:\n{format_scoreboard(chat_id, state.active_teams)}\n\n"
            "Reply \"Assist\" to credit an assist, or /next to move on. Marked the wrong person? /undo.</b>",
            parse_mode=ParseMode.HTML,
        )
        # Deliberately no auto-advance — admin decides when to move on via /next, per their choice.
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
        state.last_scorer_id = target_user.id
        state.last_award = {
            "mode": "individual",
            "user_id": target_user.id,
            "user_name": target_user.first_name,
            "points": points,
        }
        record_match_stat(state, target_user.id, target_user.first_name, "goals")

        await update.message.reply_text(
            f"<b>⚽ Correct! {tag_mention(target_user.id, target_user.first_name)} answered in {elapsed}s and earns "
            f"{points} point(s)!\n\n"
            f"🏆 Leaderboard:\n{format_leaderboard(chat_id)}\n\n"
            "Reply \"Correct\" to credit someone else, \"Assist\" to credit an assist, /next to move on, "
            "or /undo if that was wrong.</b>",
            parse_mode=ParseMode.HTML,
        )
        # Deliberately no auto-advance here — admin may want to credit more people first.


async def resolve_assist(update: Update, context: ContextTypes.DEFAULT_TYPE, state: Optional[QuizState], chat_id: int):
    if not state or not state.round_open:
        await update.message.reply_text("There's no active question to credit an assist on right now.")
        return
    if state.last_scorer_id is None:
        await update.message.reply_text("No goal's been scored yet this question — mark the correct answer first.")
        return
    if state.assist_recorded_this_round:
        await update.message.reply_text("An assist was already credited for this goal.")
        return

    assister = update.message.reply_to_message.from_user
    if assister.is_bot:
        await update.message.reply_text("Can't credit an assist to a bot.")
        return
    if assister.id == state.last_scorer_id:
        await update.message.reply_text("The scorer can't also be credited with the assist.")
        return

    if is_team_mode(state):
        scorer_team = find_team_of_user(chat_id, state.last_scorer_id)
        assister_team = find_team_of_user(chat_id, assister.id)
        if assister_team != scorer_team:
            await update.message.reply_text(f"{assister.first_name} isn't on the same team as the goal scorer.")
            return

        team_info = get_teams(chat_id)[scorer_team] if scorer_team else {}
        if (team_info.get("lineup") or {}).get("source") == "squad":
            link = get_linked_player_for(chat_id, assister.id)
            if link:
                state.match_events.append({"type": "assist", "playerId": link["player_id"]})

    record_match_stat(state, assister.id, assister.first_name, "assists")
    state.assist_recorded_this_round = True
    state.last_assister_id = assister.id

    await update.message.reply_text(
        f"<b>🅰️ Assist! {tag_mention(assister.id, assister.first_name)} gets the assist.\n\n"
        f"{format_match_stats(state)}</b>",
        parse_mode=ParseMode.HTML,
    )


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
    stats = format_match_stats(state)
    stats_block = f"\n\n{stats}" if stats else ""
    await update.message.reply_text(
        f"<b>⏭ Skipped. Correct answer was: {html.escape(state.last_answer)}\n\n"
        f"📊 {label}:\n{standings}{stats_block}</b>",
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
    stats = format_match_stats(state)
    stats_block = f"\n\n{stats}" if stats else ""
    await update.message.reply_text(
        f"<b>➡️ Moving on.\n\n📊 {label}:\n{standings}{stats_block}</b>", parse_mode=ParseMode.HTML
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

    # Reverse the goal (and any assist tied to it) from the match stats too.
    scorer_id = award.get("user_id")
    if scorer_id is not None and scorer_id in state.match_stats:
        state.match_stats[scorer_id]["goals"] = max(0, state.match_stats[scorer_id]["goals"] - 1)
        if state.match_stats[scorer_id]["goals"] == 0 and state.match_stats[scorer_id]["assists"] == 0:
            del state.match_stats[scorer_id]
    if state.assist_recorded_this_round and state.last_assister_id is not None:
        aid = state.last_assister_id
        if aid in state.match_stats:
            state.match_stats[aid]["assists"] = max(0, state.match_stats[aid]["assists"] - 1)
            if state.match_stats[aid]["goals"] == 0 and state.match_stats[aid]["assists"] == 0:
                del state.match_stats[aid]

    # Let the round be re-marked from scratch (relevant in team mode, where a goal blocks re-marking).
    state.last_scorer_id = None
    state.last_assister_id = None
    state.assist_recorded_this_round = False

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
    load_fixtures()

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
    app.add_handler(CommandHandler("squadpair", linkplayers_cmd))
    app.add_handler(CommandHandler("squadpairs", links_cmd))
    app.add_handler(CommandHandler("schedulematch", schedulematch_cmd))
    app.add_handler(CommandHandler("setcaptain", setcaptain_cmd))
    app.add_handler(CommandHandler("lineup", lineup_cmd))
    app.add_handler(CommandHandler("sub", sub_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(CommandHandler("addfixture", addfixture_cmd))
    app.add_handler(CommandHandler("fixtures", fixtures_cmd))
    app.add_handler(CommandHandler("removefixture", removefixture_cmd))
    app.add_handler(CommandHandler("teamlist", teamlist_cmd))
    app.add_handler(CommandHandler("answers", answers_cmd))
    app.add_handler(CallbackQueryHandler(post_question_callback, pattern="^post_question$"))
    app.add_handler(CallbackQueryHandler(jointeam_button_callback, pattern="^jointeam:"))
    app.add_handler(CallbackQueryHandler(lineup_toggle_callback, pattern="^lbtoggle:"))
    app.add_handler(CallbackQueryHandler(lineup_confirm_callback, pattern="^lbconfirm:"))
    app.add_handler(CallbackQueryHandler(lineup_cancel_callback, pattern="^lbcancel:"))
    app.add_handler(CallbackQueryHandler(sub_pick_out_callback, pattern="^suboff:"))
    app.add_handler(CallbackQueryHandler(sub_pick_in_callback, pattern="^subon:"))
    app.add_handler(CallbackQueryHandler(sub_cancel_callback, pattern="^subcancel:"))
    app.add_handler(CallbackQueryHandler(link_pick_telegram_callback, pattern="^lnktg:"))
    app.add_handler(CallbackQueryHandler(link_pick_player_callback, pattern="^lnkpl:"))
    app.add_handler(CallbackQueryHandler(link_cancel_callback, pattern="^lnkcancel:"))
    app.add_handler(CallbackQueryHandler(schedmatch_pick_callback, pattern="^schedmatch:"))
    app.add_handler(CallbackQueryHandler(schedmd_pick_callback, pattern="^schedmd:"))
    app.add_handler(CallbackQueryHandler(schedmd_back_callback, pattern="^schedmdback$"))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_members))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_message))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.TEXT, handle_edited_message))

    # job_queue jobs don't persist across restarts, so re-arm the countdown refresher for any
    # chat that still has upcoming fixtures loaded from disk.
    for chat_id, chat_fixtures in fixtures.items():
        if any(not f["started"] for f in chat_fixtures):
            ensure_fixtures_job(chat_id, app.job_queue)

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
