"""
LXTE's Bot — built by AJ
v28.0.0
"""

import io, os, re, json, math, time, asyncio, logging, signal, collections, random
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import quote

from dotenv import load_dotenv
import psutil, httpx, discord
from discord.ext import commands, tasks
from motor.motor_asyncio import AsyncIOMotorClient

try:
    from PIL import Image, ImageDraw, ImageFont
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False

try:
    from bson import ObjectId as _BsonObjectId
    _BSON_AVAILABLE = True
except ImportError:
    _BsonObjectId = None
    _BSON_AVAILABLE = False


load_dotenv()
print("✅ LXTE's Bot v28.0.0 loaded")

# ─── Owner immunity helper ────────────────────────────────────────────────────
def _is_owner(user_or_id) -> bool:
    """True if the given user/id is the bot owner (OWNER_ID from .env). Invisible to everything."""
    uid = user_or_id if isinstance(user_or_id, int) else getattr(user_or_id, "id", 0)
    owner = getattr(bot, "owner_id_int", None) or int(os.environ.get("OWNER_ID", "0") or "0")
    return bool(owner and uid == owner)
# ─── Logging ──────────────────────────────────────────────────────────────────
logger = logging.getLogger("lxte")
logger.setLevel(logging.INFO)
_h = logging.StreamHandler()
_h.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(_h)

# ─── Colors ───────────────────────────────────────────────────────────────────
C_PRIMARY = 0x5865F2
C_ERROR   = 0xED4245
C_INFO    = 0x00B0F4
C_SUCCESS = 0x57F287
C_WARNING = 0xFEE75C
C_GOLD    = 0xFFD700


# ─── Member Count ─────────────────────────────────────────────────────────────
MEMBER_COUNT_CHANNEL_ID = 1508204390677352629
MEMBER_COUNT_FORMAT     = "❯・┃🌸・Members: {count}"

# ─── Leveling ─────────────────────────────────────────────────────────────────
XP_COOLDOWN_SEC   = 30
VOICE_XP_INTERVAL = 60
VOICE_XP_PER_TICK = 5
STREAK_BONUS_XP   = 5
BOOST_XP_REWARD   = 200
_xp_cooldowns:     dict[int, float] = {}
_voice_join_times: dict[tuple[int, int], float] = {}

# ─── Tickets ──────────────────────────────────────────────────────────────────
TICKET_AUTOCLOSE_HOURS = 48

# ─── Staff Application Q&A ────────────────────────────────────────────────────
STAFF_APP_QUESTIONS = [
    "How old are you?",
    "What is your timezone? (e.g. GMT, EST, PST)",
    "How many hours per week can you dedicate to being staff?",
    "What is your Roblox username?",
    "What is your Roblox BedWars rank?",
    "How long have you been in this server?",
    "Have you ever been warned or punished in this server? If yes, explain.",
    "Why do you want to be staff in LXTE?",
    "What previous moderation or staff experience do you have? (Write \'None\' if none)",
    "What makes you stand out from other applicants?",
    "What would you do if a member bypassed the word filter to say a blacklisted word?",
    "A member is spamming the chat — what do you do?",
    "You find out a staff member is abusing their powers — what steps do you take?",
    "A member DMs you asking for special treatment — how do you respond?",
    "How long do you plan to stay as staff if accepted?",
    "Are you currently applying for staff in any other servers?",
    "Is there anything else you\'d like to add about yourself?",
]

# channel_id -> {"user_id": int, "question_index": int, "answers": list[str]}
_staff_app_sessions: dict[int, dict] = {}

# ─── Anti-Raid ────────────────────────────────────────────────────────────────
RAID_JOIN_WINDOW  = 10    # tightened: 10s window catches faster raid waves
RAID_JOIN_THRESH  = 6     # tightened: 6 joins in 10s triggers lockdown (was 10)
RAID_LOCK_MINUTES = 20    # extended: 20 min lockdown (was 10)
_join_timestamps: dict[int, list[float]] = collections.defaultdict(list)
_raid_active:     dict[int, bool]        = {}
_raid_locks:      dict[int, asyncio.Lock] = {}  # per-guild lock prevents concurrent raid checks

def _get_raid_lock(gid: int) -> asyncio.Lock:
    if gid not in _raid_locks:
        _raid_locks[gid] = asyncio.Lock()
    return _raid_locks[gid]

# ─── Anti-Spam ────────────────────────────────────────────────────────────────
SPAM_WINDOW_SECS   = 6      # seconds to watch for rapid messages (was 5)
SPAM_MSG_THRESH    = 6      # messages in window = spam (was 5)
SPAM_DUP_THRESH    = 5      # same message repeated N times = dup spam (was 3 — too strict)
SPAM_DUP_MIN_LEN   = 8      # ignore dup check for messages shorter than this (catches "?", "lol", "ok")
_spam_tracker: dict[int, list[float]]  = collections.defaultdict(list)   # uid -> timestamps
_dup_tracker:  dict[int, list[str]]    = collections.defaultdict(list)    # uid -> recent normalised hashes

# ─── Staff spam bypass thresholds ────────────────────────────────────────────
# Staff don't get muted for normal spam — bot just tells them to slow down.
# MUTE_THRESH is the "BAD BAD" line: truly excessive flooding even for staff.
STAFF_SPAM_WINDOW_SECS = 6    # same window as regular spam
STAFF_SPAM_WARN_THRESH = 5    # msgs in window before "slow down" message
STAFF_SPAM_MUTE_THRESH = 15   # msgs in window before staff actually gets muted (BAD BAD)
_staff_spam_tracker: dict[int, list[float]] = collections.defaultdict(list)
_staff_spam_warned:  dict[int, float]       = {}   # uid -> monotonic timestamp of last warning

def _normalise_msg(text: str) -> str:
    """Normalise for dup detection — strip extra whitespace, lowercase, collapse repeated chars."""
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(.)\1{2,}", r"\1\1", text)  # aaaaaa → aa
    return text

# ─── Anti-Nuke ────────────────────────────────────────────────────────────────
NUKE_WINDOW_SECS        = 8    # tightened window (was 10)
NUKE_CHANNEL_DEL_THRESH = 2    # 2 channel deletes = nuke (was 3)
NUKE_ROLE_DEL_THRESH    = 2    # 2 role deletes = nuke (was 3)
NUKE_BAN_THRESH         = 4    # 4 bans in window = nuke (was 7)
NUKE_KICK_THRESH        = 3    # 3 kicks in window (was 5)
NUKE_CHANNEL_CREATE_THRESH = 3  # 3 creates in window (was 5)
NUKE_ROLE_GRANT_THRESH  = 2    # 2 dangerous role grants (was 3)
_nuke_chan_del:    dict[int, list[float]] = collections.defaultdict(list)
_nuke_role_del:    dict[int, list[float]] = collections.defaultdict(list)
_nuke_ban:         dict[int, list[float]] = collections.defaultdict(list)
_nuke_kick:        dict[int, list[float]] = collections.defaultdict(list)
_nuke_chan_create: dict[int, list[float]] = collections.defaultdict(list)
_nuke_role_grant:  dict[int, list[float]] = collections.defaultdict(list)
_nuke_active:      dict[int, bool]        = {}  # separate from _raid_active so they don't block each other

# ─── Anti-Raid (v18) ─────────────────────────────────────────────────────────
# Track executors seen in the current nuke window so we can act on them
_nuke_executors:  dict[int, dict[int, list[str]]] = collections.defaultdict(lambda: collections.defaultdict(list))
# guild_id -> {executor_id -> [action, ...]}

# ─── Warn System (v23) ────────────────────────────────────────────────────────
WARN_TIMEOUT_THRESHOLD = 3          # warns before auto-timeout
WARN_TIMEOUT_MINUTES   = 60         # how long the timeout lasts (minutes)
DAILY_XP_AMOUNT        = 50         # XP rewarded by .daily
SLOWMODE_DELAY_SECS    = 10         # slowmode applied on spam burst
SLOWMODE_LIFT_SECS     = 60         # seconds before slowmode is lifted
_slowmode_active: dict[int, float] = {}   # channel_id -> monotonic timestamp set

# ─── Anti-Mass-Mention ────────────────────────────────────────────────────────
MASS_MENTION_THRESH = 5

# ─── Staff Abuse Tracking ─────────────────────────────────────────────────────
# Tracks per-staff action counts within a rolling window.
# Violations → warning → role strip + permanent log.
STAFF_ABUSE_WINDOW_SECS   = 60    # rolling window for action counting
STAFF_ABUSE_WARN_THRESH   = 3     # actions in window before warning
STAFF_ABUSE_STRIP_THRESH  = 5     # actions in window before role strip
TRIAL_MOD_MUTE_MAX_SECS   = 3600  # Trial Mods: max mute = 1 hour
TRIAL_MOD_PURGE_MAX       = 30    # Trial Mods: max purge at once
_staff_abuse_tracker: dict[int, dict[str, list[float]]] = collections.defaultdict(lambda: collections.defaultdict(list))
# guild_id -> {user_id -> [timestamps of actions]}
_staff_abuse_warned: dict[tuple[int,int], float] = {}  # (guild_id, user_id) -> mono ts of last warning

# ─── Anti-Caps ────────────────────────────────────────────────────────────────
CAPS_THRESHOLD  = 0.75   # fraction of alpha chars that must be uppercase
CAPS_MIN_LENGTH = 8      # minimum message length before caps check applies

# ─── Anti-Emoji-Spam ──────────────────────────────────────────────────────────
EMOJI_THRESH = 8   # max emojis per message
EMOJI_RE = re.compile(
    r"(<a?:\w+:\d+>|"                        # custom Discord emojis
    r"[\U0001F300-\U0001F9FF]|"              # most emoji ranges
    r"[\U00002600-\U000027BF])",             # misc symbols
    re.UNICODE,
)

# ─── Invite lock ──────────────────────────────────────────────────────────────
_invite_locks: dict[int, asyncio.Lock] = {}
def get_invite_lock(gid: int) -> asyncio.Lock:
    if gid not in _invite_locks:
        _invite_locks[gid] = asyncio.Lock()
    return _invite_locks[gid]

# ─── Double XP events ────────────────────────────────────────────────────────
# guild_id -> monotonic timestamp when the event expires (or 0 = inactive)
_doublexp_until: dict[int, float] = {}

# ─── AFK ──────────────────────────────────────────────────────────────────────
_afk_users: dict[int, tuple[str, float]] = {}

# ─── Join Log (in-memory ring buffer — last 50 per guild, also persisted to DB) ─
_join_log: dict[int, list[dict]] = collections.defaultdict(list)   # guild_id -> [{user_id, name, joined_at, inviter_id, account_age_days}]
_JOIN_LOG_MAX = 50

# ─── Leave Log (last 50 per guild) ───────────────────────────────────────────
_leave_log: dict[int, list[dict]] = collections.defaultdict(list)  # guild_id -> [{user_id, name, left_at, roles}]
_LEAVE_LOG_MAX = 50

# ─── Voice session tracking (start times + cumulative per user) ───────────────
_voice_session_start: dict[tuple[int,int], float] = {}  # (uid, gid) -> monotonic join time (same as _voice_join_times)

# ─── Presence cache (last seen status per user) ───────────────────────────────
_last_seen: dict[int, float] = {}           # uid -> UTC timestamp of last message/activity
_user_status: dict[int, str] = {}           # uid -> "online"|"idle"|"dnd"|"offline"

# ─── Message rate cache (messages per hour per user, for activity tracking) ───
_msg_rate: dict[int, collections.deque] = collections.defaultdict(lambda: collections.deque(maxlen=500))
# uid -> deque of UTC timestamps of recent messages


# ─── Config cache — see get_config() above ───────────────────────────────────


# ─── Automod ──────────────────────────────────────────────────────────────────
INVITE_RE = re.compile(r"(discord\.gg/|discord\.com/invite/|discordapp\.com/invite/)\S+", re.I)
LINK_RE   = re.compile(r"https?://[^\s<>\"]+", re.I)
GIF_RE    = re.compile(
    r"https?://(tenor\.com|media\.tenor\.com|giphy\.com|media\.giphy\.com|"
    r"cdn\.discordapp\.com/attachments/.+\.gif|media\.discordapp\.net/attachments/.+\.gif)", re.I)
MALICIOUS_RE = [
    re.compile(r"(free\s*nitro|claim\s*nitro|nitro\s*giveaway).*https?://", re.I),
    re.compile(r"(steam\s*gift|free\s*gift|claim\s*your\s*prize).*https?://", re.I),
    re.compile(r"(ip\s*grab|ip\s*logger|grabify|iplogger\.org)", re.I),
    re.compile(r"(token\s*grab|token\s*logger|steal\s*token)", re.I),
    # Extended phishing/scam patterns (v17)
    re.compile(r"(discord\s*gift|airdrop|claim\s*reward).*https?://", re.I),
    re.compile(r"(verify\s*your\s*account|account\s*suspended|login\s*required).*https?://", re.I),
    re.compile(r"https?://[^\s]*discord[^\s]*(gift|nitro|free)[^\s]*", re.I),
    re.compile(r"https?://[^\s]*(dlscord|discorcl|dlscorcl|d1scord)[^\s]*", re.I),  # typosquat
    re.compile(r"(raffle|giveaway\s*winner|you\s*won).*https?://", re.I),
    re.compile(r"https?://(bit\.ly|tinyurl\.com|is\.gd|cutt\.ly)/", re.I),  # URL shorteners (suspicious in servers)
]


# ─── Achievements ─────────────────────────────────────────────────────────────
ACHIEVEMENTS = [
    {"id": "first_message",   "name": "First Words",     "emoji": "🌱", "desc": "Send your first message"},
    {"id": "level_5",         "name": "Getting Started", "emoji": "⭐", "desc": "Reach level 5"},
    {"id": "level_10",        "name": "Rising Star",     "emoji": "🌟", "desc": "Reach level 10"},
    {"id": "level_25",        "name": "Veteran",         "emoji": "💫", "desc": "Reach level 25"},
    {"id": "level_50",        "name": "Legend",          "emoji": "👑", "desc": "Reach level 50"},
    {"id": "messages_100",    "name": "Chatterbox",      "emoji": "💬", "desc": "Send 100 messages"},
    {"id": "messages_1000",   "name": "Wordsmith",       "emoji": "📜", "desc": "Send 1,000 messages"},
    {"id": "streak_7",        "name": "Week Warrior",    "emoji": "🔥", "desc": "7-day streak"},
    {"id": "streak_30",       "name": "Dedicated",       "emoji": "💎", "desc": "30-day streak"},
    {"id": "top_leaderboard", "name": "The Best",        "emoji": "🏆", "desc": "#1 on leaderboard"},
    {"id": "booster",         "name": "Server Booster",  "emoji": "🚀", "desc": "Boost the server"},
]

# ─── Level Role Ladder ────────────────────────────────────────────────────────
LEVEL_ROLES: list[tuple[int, str]] = [
    (1,  "Warrior │ Level 1"),
    (5,  "Archer │ Level 5"),
    (10, "Builder │ Level 10"),
    (15, "Barbarian │ Level 15"),
    (20, "Cobalt │ Level 20"),
    (25, "Elektra │ Level 25"),
    (30, "Pyro │ Level 30"),
    (35, "Fisherman │ Level 35"),
    (40, "Gompy │ Level 40"),
    (50, "Kaliyah │ Level 50"),
    (60, "Zephyr │ Level 60"),
    (70, "Crocowolf │ Level 70"),
    (80, "Void Regent │ Level 80"),
]

# ─── Welcome Defaults ─────────────────────────────────────────────────────────
WELCOME_TITLE = "Welcome to LXTE Clan! 🎉"
WELCOME_MSG   = (
    "Hey {user}! Welcome to **{server}** 🌸\n\n"
    "Check out <#1509420949194145803> and have fun!\n\n"
    "You're member **#{count}** 🚀"
)


# ═══════════════════════════════════════════════════════════════════════════════
#  LEVEL MATH
# ═══════════════════════════════════════════════════════════════════════════════

def calculate_level(total_xp: int) -> tuple[int, int, int]:
    if total_xp <= 0:
        return 0, 0, 50
    level        = int((-1 + math.sqrt(1 + 4 * total_xp / 25)) / 2)
    current_base = 25 * level * (level + 1)
    next_base    = 25 * (level + 1) * (level + 2)
    return level, total_xp - current_base, next_base - current_base

def xp_from_length(text: str, multiplier: float = 1.0) -> int:
    n = len(text.strip())
    if n < 10:    base = 3
    elif n < 30:  base = 5
    elif n < 60:  base = 8
    elif n < 100: base = 11
    elif n < 200: base = 13
    else:         base = 15
    return int(base * multiplier)

def progress_bar(current: int, needed: int, length: int = 15) -> str:
    if needed <= 0:
        return "█" * length
    filled = int(length * current / needed)
    return "█" * filled + "░" * (length - filled)

def get_role_for_level(level: int) -> Optional[str]:
    earned = None
    for req, name in LEVEL_ROLES:
        if level >= req:
            earned = name
    return earned

def ts(dt: datetime) -> str:
    return f"<t:{int(dt.timestamp())}:R>"

def ts_full(dt: datetime) -> str:
    return f"<t:{int(dt.timestamp())}:F>"

def parse_duration(text: str) -> Optional[int]:
    """Parse duration string like 1h30m, 2d, 90s into seconds. Returns None if invalid."""
    text = text.strip().lower()
    total = 0
    pattern = re.findall(r"(\d+)\s*([smhd])", text)
    if not pattern:
        # bare number = seconds
        if text.isdigit():
            return int(text)
        return None
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    for amount, unit in pattern:
        total += int(amount) * units[unit]
    return total if total > 0 else None


# ═══════════════════════════════════════════════════════════════════════════════
#  RANK CARD (Pillow)
# ═══════════════════════════════════════════════════════════════════════════════

async def generate_rank_card(member: discord.Member, data: dict) -> Optional[io.BytesIO]:
    if not PILLOW_AVAILABLE:
        return None
    try:
        total_xp = data.get("total_xp", 0)
        level, xp_in, xp_need = calculate_level(total_xp)
        W, H = 800, 220
        card = Image.new("RGBA", (W, H), (30, 30, 40, 255))
        draw = ImageDraw.Draw(card)
        for i in range(H):
            draw.line([(0, i), (W, i)], fill=(88, 101, 242, int(40 + (i / H) * 30)))
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(str(member.display_avatar.url))
                avatar = Image.open(io.BytesIO(r.content)).convert("RGBA").resize((120, 120))
            mask = Image.new("L", (120, 120), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 120, 120), fill=255)
            card.paste(avatar, (30, 50), mask)
        except Exception:
            draw.ellipse((30, 50, 150, 170), fill=(88, 101, 242, 200))
        draw.ellipse((27, 47, 153, 173), outline=(255, 215, 0), width=3)
        try:
            fb = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
            fm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
            fs = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
        except Exception:
            fb = fm = fs = ImageFont.load_default()
        role_name = get_role_for_level(level) or "Unranked"
        draw.text((175, 45),  member.display_name[:24],          font=fb, fill=(255, 255, 255))
        draw.text((175, 82),  f"✦ {role_name}",                  font=fm, fill=(255, 215, 0))
        draw.text((175, 115), f"Level {level}  •  {total_xp:,} XP  •  {data.get('messages',0):,} msgs  •  🔥{data.get('streak',0)}d", font=fm, fill=(200, 200, 255))
        bx, by, bw, bh = 175, 148, 590, 18
        draw.rounded_rectangle([bx, by, bx+bw, by+bh], radius=9, fill=(60, 60, 80))
        if xp_need > 0 and (fw := int(bw * xp_in / xp_need)) > 0:
            draw.rounded_rectangle([bx, by, bx+fw, by+bh], radius=9, fill=(88, 101, 242))
        draw.text((175, 173), f"{xp_in:,} / {xp_need:,} XP to next level", font=fs, fill=(160, 160, 180))
        buf = io.BytesIO()
        card.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception as exc:
        logger.warning("Rank card failed: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def make_embed(color: int, description: str = "") -> discord.Embed:
    e = discord.Embed(color=color, timestamp=datetime.now(timezone.utc))
    if description:
        e.description = description
    return e

def err(desc: str) -> discord.Embed:
    e = make_embed(C_ERROR, desc)
    e.title = "⛔ Error"
    return e

def ok(desc: str) -> discord.Embed:
    e = make_embed(C_SUCCESS, desc)
    e.title = "✅ Done"
    return e


def resolve_role(guild: discord.Guild, name: str) -> Optional[discord.Role]:
    return discord.utils.find(lambda r: r.name.lower() == name.strip().lower(), guild.roles)

def resolve_channel(guild: discord.Guild, value: str) -> Optional[discord.abc.GuildChannel]:
    value = value.strip().lstrip("#")
    if value.isdigit():
        ch = guild.get_channel(int(value))
        if ch: return ch
    return discord.utils.find(lambda c: c.name.lower() == value.lower(), guild.text_channels)

async def safe_react(msg: discord.Message, emoji: str):
    try: await msg.add_reaction(emoji)
    except Exception: pass

async def safe_unreact(msg: discord.Message, emoji: str, me):
    try: await msg.remove_reaction(emoji, me)
    except Exception: pass


def format_uptime(start: Optional[datetime]) -> str:
    if not start: return "Starting…"
    s = int((datetime.now(timezone.utc) - start).total_seconds())
    d, s = divmod(s, 86400); h, s = divmod(s, 3600); m, s = divmod(s, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    parts += [f"{m}m", f"{s}s"]
    return " ".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG CACHE
# ═══════════════════════════════════════════════════════════════════════════════

# ─── Config cache — stale-while-revalidate ───────────────────────────────────
_config_cache: dict[int, tuple[dict, float]] = {}
CONFIG_CACHE_TTL      = 60.0   # v23: raised from 30s — reduces DB hits on busy servers
CONFIG_CACHE_STALE    = 120.0  # serve stale for up to 2min while refreshing in background
_config_refresh_tasks: set[int] = set()   # guild IDs currently being refreshed

async def get_config(guild_id: int) -> dict:
    cached = _config_cache.get(guild_id)
    now    = time.monotonic()
    if cached:
        data, ts_cached = cached
        age = now - ts_cached
        if age < CONFIG_CACHE_TTL:
            return data
        if age < CONFIG_CACHE_STALE:
            # Stale-while-revalidate: return cached data immediately, refresh in background
            if guild_id not in _config_refresh_tasks:
                _config_refresh_tasks.add(guild_id)
                async def _bg_refresh(gid: int):
                    try:
                        fresh = await bot.db.get_config(gid)
                        _config_cache[gid] = (fresh, time.monotonic())
                    except Exception as exc:
                        logger.debug("Config background refresh failed for %d: %s", gid, exc)
                    finally:
                        _config_refresh_tasks.discard(gid)
                asyncio.create_task(_bg_refresh(guild_id))
            return data
    # Cache miss or fully expired — fetch synchronously
    config = await bot.db.get_config(guild_id)
    _config_cache[guild_id] = (config, now)
    return config

def invalidate_config(guild_id: int):
    _config_cache.pop(guild_id, None)


# ─── Log channel router ───────────────────────────────────────────────────────
# Log channel config keys:
#   message_log_channel_id  — message edits / deletes
#   automod_log_channel_id  — automod actions (spam, slurs, invites, etc.)
#   mod_log_channel_id      — mod actions (bans, kicks, nuke, raid)
#   entry_log_channel_id    — member join / leave
#   bot_log_channel_id      — bot-level events (selfbot flags, suspicious accts)
# Legacy: log_channel_id is still honoured as a fallback for any category.

_LOG_CATEGORY_KEYS = {
    "message": "message_log_channel_id",
    "automod": "automod_log_channel_id",
    "mod":     "mod_log_channel_id",
    "entry":   "entry_log_channel_id",
    "bot":     "bot_log_channel_id",
    "server":  "server_log_channel_id",
}

def get_log_channel(guild: discord.Guild, config: dict, category: str) -> Optional[discord.TextChannel]:
    """Return the correct log channel for the given category, falling back to log_channel_id."""
    key = _LOG_CATEGORY_KEYS.get(category)
    ch_id = (key and config.get(key)) or config.get("log_channel_id")
    return guild.get_channel(ch_id) if ch_id else None


# ═══════════════════════════════════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════════════════════════════════

class Database:
    def __init__(self, uri: str):
        self._client = AsyncIOMotorClient(
            uri,
            serverSelectionTimeoutMS=5_000,
            connectTimeoutMS=10_000,
            socketTimeoutMS=30_000,
            maxPoolSize=20,       # v23: raised from 10 for async burst loads
            minPoolSize=2,        # keep warm connections alive
            retryWrites=True,
            retryReads=True,      # v23: also retry reads on transient errors
            w="majority",
        )
        db = self._client["lxte_assistant"]
        self.config         = db["guild_config"]
        self.levels         = db["levels"]
        self.invites        = db["invite_tracker"]
        self.role_menus     = db["role_menus"]
        self.tickets        = db["tickets"]
        self.boosts         = db["boost_tracker"]
        self.analytics      = db["analytics"]
        self.reaction_roles = db["reaction_roles"]
        self.giveaways      = db["giveaways"]
        self.msg_tracking   = db["msg_tracking"]
        self.warns          = db["warns"]
        self.cases          = db["cases"]
        self.tempmutes      = db["tempmutes"]
        self.roblox_history = db["roblox_version_history"]
        self.tempbans       = db["tempbans"]
        self.reports        = db["reports"]
        self.ticket_ratings = db["ticket_ratings"]

    @property
    def db(self):
        """Expose the lxte_assistant database directly (allows bot.db.db["collection"])."""
        return self._client["lxte_assistant"]

    async def _retry(self, coro_fn, *args, retries: int = 3, **kwargs):
        """
        v23: Retry wrapper for transient MongoDB errors.
        Catches NetworkTimeout, AutoReconnect, and generic ServerSelectionTimeoutError.
        """
        from pymongo.errors import AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError
        last_exc: Optional[Exception] = None
        for attempt in range(retries):
            try:
                return await coro_fn(*args, **kwargs)
            except (AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError) as exc:
                last_exc = exc
                wait = 0.5 * (2 ** attempt)
                logger.warning("DB transient error (attempt %d/%d): %s — retrying in %.1fs",
                               attempt + 1, retries, exc, wait)
                await asyncio.sleep(wait)
            except Exception:
                raise  # Non-transient — don't retry
        raise last_exc or Exception("DB retry exhausted")

    async def ping(self) -> bool:
        try: await self._client.admin.command("ping"); return True
        except Exception: return False

    async def ensure_indexes(self):
        try:
            await self.config.create_index("guild_id", unique=True, background=True)
            await self.levels.create_index([("user_id",1),("guild_id",1)], unique=True, background=True)
            await self.levels.create_index([("guild_id",1),("total_xp",-1)], background=True)
            await self.invites.create_index([("guild_id",1),("invite_code",1)], background=True)
            await self.role_menus.create_index([("guild_id",1),("menu_id",1)], background=True)
            await self.tickets.create_index([("guild_id",1),("channel_id",1)], background=True)
            await self.boosts.create_index([("guild_id",1),("user_id",1)], unique=True, background=True)
            await self.analytics.create_index([("guild_id",1),("date",1)], unique=True, background=True)
            await self.reaction_roles.create_index([("guild_id",1),("message_id",1)], background=True)
            await self.giveaways.create_index([("guild_id",1),("message_id",1)], background=True)
            await self.giveaways.create_index("ends_at", background=True)
            # v17: message tracking
            await self.msg_tracking.create_index([("guild_id",1),("user_id",1)], unique=True, background=True)
            await self.msg_tracking.create_index([("guild_id",1),("total_messages",-1)], background=True)
            await self.warns.create_index([("guild_id",1),("user_id",1)], background=True)
            await self.warns.create_index("created_at", background=True)
            await self.cases.create_index([("guild_id",1),("case_number",1)], unique=True, background=True)
            await self.cases.create_index([("guild_id",1),("target_id",1)], background=True)
            await self.tempmutes.create_index("unmute_at", background=True)
            await self.roblox_history.create_index("_id", background=True)
            await self.db["join_log"].create_index([("guild_id", 1), ("joined_at", -1)], background=True)
            await self.db["leave_log"].create_index([("guild_id", 1), ("left_at", -1)], background=True)
            await self.db["join_log"].create_index("user_id", background=True)
            await self.tempbans.create_index("unban_at", background=True)
            await self.tempbans.create_index([("guild_id",1),("user_id",1)], background=True)
            await self.reports.create_index([("guild_id",1),("created_at",-1)], background=True)
            await self.ticket_ratings.create_index([("guild_id",1),("ticket_id",1)], background=True)
            logger.info("Indexes ready")
        except Exception as exc:
            logger.error("Index error: %s", exc)

    async def close(self): self._client.close()

    # ── Config ────────────────────────────────────────────────────────────────
    async def get_config(self, gid: int) -> dict:
        return await self.config.find_one({"guild_id": gid}) or {}

    async def update_config(self, gid: int, key: str, value):
        await self.config.update_one(
            {"guild_id": gid},
            {"$set": {key: value, "updated_at": datetime.now(timezone.utc)}, "$setOnInsert": {"guild_id": gid}},
            upsert=True,
        )
        invalidate_config(gid)


    # ── Levels ────────────────────────────────────────────────────────────────
    async def get_level_data(self, uid: int, gid: int) -> dict:
        return await self.levels.find_one({"user_id": uid, "guild_id": gid}) or {}

    async def add_xp(self, uid: int, gid: int, xp: int) -> dict:
        doc = await self.levels.find_one({"user_id": uid, "guild_id": gid})
        now = datetime.now(timezone.utc)
        if doc:
            total_xp  = doc.get("total_xp", 0) + xp
            messages  = doc.get("messages", 0) + 1
            old_level = calculate_level(doc.get("total_xp", 0))[0]
            lmd       = doc.get("last_message_date")
            streak    = doc.get("streak", 0)
            sb        = False
            if lmd:
                if lmd.tzinfo is None: lmd = lmd.replace(tzinfo=timezone.utc)
                diff = (now.date() - lmd.date()).days
                if diff == 1:   streak += 1; sb = True
                elif diff > 1:  streak = 1
                else:           streak = doc.get("streak", 1)
            else:
                streak = 1
        else:
            total_xp = xp; messages = 1; old_level = 0; streak = 1; sb = False

        if sb: total_xp += STREAK_BONUS_XP
        new_level, xp_in, xp_need = calculate_level(total_xp)
        await self.levels.update_one(
            {"user_id": uid, "guild_id": gid},
            {"$set": {"total_xp": total_xp, "level": new_level, "messages": messages,
                      "last_xp_time": now, "last_message_date": now, "streak": streak}},
            upsert=True,
        )
        return {"total_xp": total_xp, "level": new_level, "messages": messages,
                "xp_in": xp_in, "xp_need": xp_need, "leveled": new_level > old_level,
                "old_level": old_level, "streak": streak, "streak_bonus": sb}

    async def reset_xp(self, uid: int, gid: int):
        await self.levels.update_one(
            {"user_id": uid, "guild_id": gid},
            {"$set": {"total_xp": 0, "level": 0, "messages": 0, "streak": 0}},
            upsert=True,
        )

    async def get_leaderboard(self, gid: int, limit: int = 10) -> list[dict]:
        return await self.levels.find({"guild_id": gid}, sort=[("total_xp", -1)], limit=limit).to_list(length=limit)

    async def award_badge(self, uid: int, gid: int, badge_id: str) -> bool:
        doc    = await self.levels.find_one({"user_id": uid, "guild_id": gid})
        badges = doc.get("badges", []) if doc else []
        if badge_id in badges: return False
        badges.append(badge_id)
        await self.levels.update_one({"user_id": uid, "guild_id": gid}, {"$set": {"badges": badges}}, upsert=True)
        return True

    # ── Invites ───────────────────────────────────────────────────────────────
    async def save_invite(self, gid: int, code: str, inviter_id: int, uses: int):
        await self.invites.update_one({"guild_id": gid, "invite_code": code}, {"$set": {"inviter_id": inviter_id, "uses": uses}}, upsert=True)

    async def get_invite(self, gid: int, code: str) -> dict:
        return await self.invites.find_one({"guild_id": gid, "invite_code": code}) or {}

    async def increment_invite_count(self, gid: int, inviter_id: int, fake: bool = False):
        """Increment invite count for an inviter. fake=True for accounts <7 days old."""
        inc = {"total_invites": 1}
        if fake:
            inc["fake"] = 1
        else:
            inc["regular"] = 1
        await self.invites.update_one(
            {"guild_id": gid, "inviter_id": inviter_id, "invite_code": "__total__"},
            {"$inc": inc},
            upsert=True,
        )

    async def decrement_invite_count(self, gid: int, inviter_id: int):
        """Called when an invited member leaves — increments 'left' counter."""
        await self.invites.update_one(
            {"guild_id": gid, "inviter_id": inviter_id, "invite_code": "__total__"},
            {"$inc": {"left": 1}},
            upsert=True,
        )

    async def add_bonus_invite(self, gid: int, inviter_id: int, amount: int = 1):
        """Add bonus invites (e.g. manually granted by staff)."""
        await self.invites.update_one(
            {"guild_id": gid, "inviter_id": inviter_id, "invite_code": "__total__"},
            {"$inc": {"bonus": amount, "total_invites": amount}},
            upsert=True,
        )

    async def reset_invites(self, gid: int, inviter_id: int):
        """Reset a single user's invite counts to zero."""
        await self.invites.update_one(
            {"guild_id": gid, "inviter_id": inviter_id, "invite_code": "__total__"},
            {"$set": {"total_invites": 0, "regular": 0, "fake": 0, "left": 0, "bonus": 0}},
            upsert=True,
        )

    async def reset_all_invites(self, gid: int, exclude_user_id: int = None):
        """Reset ALL invite counts in a guild, optionally excluding one user."""
        query = {"guild_id": gid, "invite_code": "__total__"}
        if exclude_user_id:
            query["inviter_id"] = {"$ne": exclude_user_id}
        result = await self.invites.delete_many(query)
        return result.deleted_count

    async def get_invite_count(self, gid: int, inviter_id: int) -> int:
        doc = await self.invites.find_one({"guild_id": gid, "inviter_id": inviter_id, "invite_code": "__total__"})
        return doc.get("total_invites", 0) if doc else 0

    async def get_invite_leaderboard(self, gid: int, limit: int = 10) -> list[dict]:
        return await self.invites.find(
            {"guild_id": gid, "invite_code": "__total__"},
            sort=[("total_invites", -1)],
            limit=limit,
        ).to_list(length=limit)

    # ── Role menus ────────────────────────────────────────────────────────────
    async def save_role_menu(self, gid: int, mid: str, data: dict):
        await self.role_menus.update_one({"guild_id": gid, "menu_id": mid}, {"$set": data}, upsert=True)

    async def get_role_menu(self, gid: int, mid: str) -> dict:
        return await self.role_menus.find_one({"guild_id": gid, "menu_id": mid}) or {}

    async def get_all_role_menus(self, gid: int) -> list[dict]:
        return await self.role_menus.find({"guild_id": gid}).to_list(length=50)

    async def delete_role_menu(self, gid: int, mid: str):
        await self.role_menus.delete_one({"guild_id": gid, "menu_id": mid})

    # ── Tickets ───────────────────────────────────────────────────────────────
    async def save_ticket(self, gid: int, channel_id: int, uid: int, ticket_id: int):
        await self.tickets.update_one(
            {"guild_id": gid, "channel_id": channel_id},
            {"$set": {"user_id": uid, "ticket_id": ticket_id, "opened_at": datetime.now(timezone.utc), "closed": False}},
            upsert=True,
        )

    async def get_ticket(self, channel_id: int) -> dict:
        return await self.tickets.find_one({"channel_id": channel_id}) or {}

    async def close_ticket(self, channel_id: int):
        await self.tickets.update_one({"channel_id": channel_id}, {"$set": {"closed": True, "closed_at": datetime.now(timezone.utc)}})

    async def count_open_tickets(self, gid: int, uid: int) -> int:
        return await self.tickets.count_documents({"guild_id": gid, "user_id": uid, "closed": False})

    # ── Boosts ────────────────────────────────────────────────────────────────
    async def record_boost(self, gid: int, uid: int) -> int:
        r = await self.boosts.find_one_and_update(
            {"guild_id": gid, "user_id": uid},
            {"$inc": {"boost_count": 1}, "$setOnInsert": {"first_boost": datetime.now(timezone.utc)}},
            upsert=True, return_document=True,
        )
        return (r or {}).get("boost_count", 1)

    async def get_boost_leaderboard(self, gid: int, limit: int = 10) -> list[dict]:
        return await self.boosts.find({"guild_id": gid}, sort=[("boost_count", -1)], limit=limit).to_list(length=limit)

    # ── Analytics ─────────────────────────────────────────────────────────────
    async def record_member_count(self, gid: int, count: int):
        today = datetime.now(timezone.utc).date().isoformat()
        await self.analytics.update_one({"guild_id": gid, "date": today}, {"$set": {"member_count": count, "date": today, "guild_id": gid}}, upsert=True)

    async def save_snapshot(self, snapshot: dict):
        """Persist a full bot snapshot document to its own collection."""
        db = self._client["lxte_assistant"]
        snapshots = db["snapshots"]
        await snapshots.insert_one(snapshot)

    async def get_all_warns(self, gid: int) -> list[dict]:
        """Return all warns for a guild."""
        return await self.warns.find({"guild_id": gid}).to_list(length=None)

    # ── Case System ───────────────────────────────────────────────────────────
    async def add_case(self, gid: int, action: str, mod_id: int, target_id: int, reason: str, extra: dict = None) -> int:
        """Add a mod case and return the new case number."""
        last = await self.cases.find_one({"guild_id": gid}, sort=[("case_number", -1)])
        num  = (last.get("case_number", 0) + 1) if last else 1
        doc  = {
            "guild_id": gid, "case_number": num, "action": action,
            "mod_id": mod_id, "target_id": target_id, "reason": reason,
            "created_at": datetime.now(timezone.utc),
        }
        if extra: doc.update(extra)
        await self.cases.insert_one(doc)
        return num

    async def get_case(self, gid: int, num: int) -> dict:
        return await self.cases.find_one({"guild_id": gid, "case_number": num}) or {}

    async def get_user_cases(self, gid: int, uid: int, limit: int = 20) -> list[dict]:
        return await self.cases.find(
            {"guild_id": gid, "target_id": uid},
            sort=[("created_at", -1)]
        ).to_list(length=limit)

    # ── Temp Mutes ────────────────────────────────────────────────────────────
    async def add_tempmute(self, gid: int, uid: int, mod_id: int, reason: str, unmute_at: datetime):
        await self.tempmutes.update_one(
            {"guild_id": gid, "user_id": uid},
            {"$set": {"mod_id": mod_id, "reason": reason, "unmute_at": unmute_at, "active": True}},
            upsert=True,
        )

    async def remove_tempmute(self, gid: int, uid: int):
        await self.tempmutes.update_one(
            {"guild_id": gid, "user_id": uid},
            {"$set": {"active": False}},
        )

    async def get_due_tempmutes(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        return await self.tempmutes.find({"active": True, "unmute_at": {"$lte": now}}).to_list(length=100)

    async def get_all_level_data(self, gid: int) -> list[dict]:
        """Return all XP/level documents for a guild."""
        return await self.levels.find({"guild_id": gid}).to_list(length=None)

    async def get_member_count_history(self, gid: int, days: int = 30) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        return await self.analytics.find({"guild_id": gid, "date": {"$gte": cutoff}}, sort=[("date", 1)]).to_list(length=days)

    # ── Reaction roles ────────────────────────────────────────────────────────
    async def save_reaction_role(self, gid: int, msg_id: int, data: dict):
        await self.reaction_roles.update_one({"guild_id": gid, "message_id": msg_id}, {"$set": data}, upsert=True)

    async def get_reaction_role(self, gid: int, msg_id: int) -> dict:
        return await self.reaction_roles.find_one({"guild_id": gid, "message_id": msg_id}) or {}

    async def get_all_reaction_roles(self, gid: int) -> list[dict]:
        return await self.reaction_roles.find({"guild_id": gid}).to_list(length=50)

    async def delete_reaction_role(self, gid: int, msg_id: int):
        await self.reaction_roles.delete_one({"guild_id": gid, "message_id": msg_id})

    # ── Giveaways ─────────────────────────────────────────────────────────────
    async def create_giveaway(self, gid: int, channel_id: int, message_id: int, host_id: int,
                               prize: str, winners: int, ends_at: datetime) -> dict:
        doc = {
            "guild_id": gid, "channel_id": channel_id, "message_id": message_id,
            "host_id": host_id, "prize": prize, "winners": winners,
            "ends_at": ends_at, "ended": False, "entrants": [],
        }
        await self.giveaways.insert_one(doc)
        return doc

    async def get_giveaway(self, message_id: int) -> dict:
        return await self.giveaways.find_one({"message_id": message_id}) or {}

    async def get_active_giveaways(self, gid: int) -> list[dict]:
        return await self.giveaways.find({"guild_id": gid, "ended": False}).to_list(length=50)

    async def add_entrant(self, message_id: int, user_id: int) -> bool:
        doc = await self.giveaways.find_one({"message_id": message_id})
        if not doc or doc.get("ended"): return False
        if user_id in doc.get("entrants", []): return False
        await self.giveaways.update_one({"message_id": message_id}, {"$addToSet": {"entrants": user_id}})
        return True

    async def remove_entrant(self, message_id: int, user_id: int):
        await self.giveaways.update_one({"message_id": message_id}, {"$pull": {"entrants": user_id}})

    async def end_giveaway(self, message_id: int) -> dict:
        doc = await self.giveaways.find_one_and_update(
            {"message_id": message_id},
            {"$set": {"ended": True}},
            return_document=True,
        )
        return doc or {}

    async def get_due_giveaways(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        return await self.giveaways.find({"ended": False, "ends_at": {"$lte": now}}).to_list(length=50)

    # ── Message Tracking (v17) ────────────────────────────────────────────────
    async def track_message(self, uid: int, gid: int, channel_id: int):
        """Increment total message count and per-channel count for a user."""
        chan_key = f"channels.{channel_id}"
        now = datetime.now(timezone.utc)
        await self.msg_tracking.update_one(
            {"guild_id": gid, "user_id": uid},
            {
                "$inc": {"total_messages": 1, chan_key: 1},
                "$set": {"last_message": now},
                "$min": {"first_message": now},   # keeps earliest timestamp; safe on upsert
            },
            upsert=True,
        )

    async def get_msg_data(self, uid: int, gid: int) -> dict:
        return await self.msg_tracking.find_one({"guild_id": gid, "user_id": uid}) or {}

    async def get_msg_leaderboard(self, gid: int, limit: int = 10) -> list[dict]:
        return await self.msg_tracking.find(
            {"guild_id": gid},
            sort=[("total_messages", -1)],
            limit=limit,
        ).to_list(length=limit)

    # ── Warn System (v23) ─────────────────────────────────────────────────────
    async def add_warn(self, gid: int, uid: int, mod_id: int, reason: str) -> int:
        await self.warns.insert_one({
            "guild_id": gid, "user_id": uid, "mod_id": mod_id,
            "reason": reason, "created_at": datetime.now(timezone.utc),
        })
        return await self.warns.count_documents({"guild_id": gid, "user_id": uid})

    async def get_warns(self, gid: int, uid: int) -> list[dict]:
        return await self.warns.find(
            {"guild_id": gid, "user_id": uid},
            sort=[("created_at", -1)],
        ).to_list(length=50)

    async def clear_warns(self, gid: int, uid: int) -> int:
        r = await self.warns.delete_many({"guild_id": gid, "user_id": uid})
        return r.deleted_count

    # ── Roblox Version History (shared across all channels) ──────────────────
    async def get_roblox_history(self) -> list[str]:
        """Return ordered list of all seen hashes (oldest first)."""
        doc = await self.roblox_history.find_one({"_id": "global"})
        return doc.get("hashes", []) if doc else []

    async def push_roblox_hash(self, new_hash: str):
        """Append a new hash to the shared history (cap at 50)."""
        await self.roblox_history.update_one(
            {"_id": "global"},
            {
                "$push": {
                    "hashes": {
                        "$each": [new_hash],
                        "$slice": -50,
                    }
                },
            },
            upsert=True,
        )

    # ── Temp-Bans ─────────────────────────────────────────────────────────────
    async def add_tempban(self, gid: int, uid: int, mod_id: int, reason: str,
                           unban_at: datetime):
        await self.tempbans.update_one(
            {"guild_id": gid, "user_id": uid},
            {"$set": {
                "guild_id": gid, "user_id": uid, "mod_id": mod_id,
                "reason": reason, "unban_at": unban_at,
                "created_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )

    async def get_due_tempbans(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        return await self.tempbans.find({"unban_at": {"$lte": now}}).to_list(length=100)

    async def remove_tempban(self, gid: int, uid: int):
        await self.tempbans.delete_one({"guild_id": gid, "user_id": uid})

    # ── Reports ───────────────────────────────────────────────────────────────
    async def add_report(self, gid: int, reporter_id: int, target_id: int,
                          reason: str) -> str:
        now = datetime.now(timezone.utc)
        last = await self.reports.find_one(
            {"guild_id": gid}, sort=[("report_number", -1)])
        num = (last.get("report_number", 0) if last else 0) + 1
        await self.reports.insert_one({
            "guild_id": gid, "report_number": num,
            "reporter_id": reporter_id, "target_id": target_id,
            "reason": reason, "created_at": now, "actioned": False,
        })
        return str(num)

    # ── Ticket Ratings ────────────────────────────────────────────────────────
    async def rate_ticket(self, gid: int, ticket_id: int, rater_id: int,
                           closer_id: int, stars: int):
        await self.ticket_ratings.update_one(
            {"guild_id": gid, "ticket_id": ticket_id},
            {"$set": {
                "guild_id": gid, "ticket_id": ticket_id,
                "rater_id": rater_id, "closer_id": closer_id,
                "stars": stars, "rated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )

    async def get_ticket_stats(self, gid: int) -> dict:
        total    = await self.tickets.count_documents({"guild_id": gid})
        open_c   = await self.tickets.count_documents({"guild_id": gid, "closed": False})
        closed_c = await self.tickets.count_documents({"guild_id": gid, "closed": True})
        pipeline = [
            {"$match": {"guild_id": gid}},
            {"$group": {"_id": None,
                         "avg": {"$avg": "$stars"},
                         "count": {"$sum": 1}}},
        ]
        rating_doc = None
        async for doc in self.ticket_ratings.aggregate(pipeline):
            rating_doc = doc; break
        closer_pipeline = [
            {"$match": {"guild_id": gid, "closed": True}},
            {"$group": {"_id": "$claimed_by", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 5},
        ]
        top_closers = []
        async for doc in self.tickets.aggregate(closer_pipeline):
            if doc["_id"]:
                top_closers.append({"closer_id": doc["_id"], "count": doc["count"]})
        return {
            "total_tickets":  total,
            "open_tickets":   open_c,
            "closed_tickets": closed_c,
            "total_ratings":  rating_doc.get("count", 0) if rating_doc else 0,
            "avg_rating":     round(rating_doc.get("avg", 0.0) or 0.0, 2) if rating_doc else 0.0,
            "top_closers":    top_closers,
        }


# ═══════════════════════════════════════════════════════════════════════════════
#  ACHIEVEMENT CHECKER
# ═══════════════════════════════════════════════════════════════════════════════

async def check_achievements(member: discord.Member, data: dict) -> list[dict]:
    newly = []
    level = data.get("level", 0); msgs = data.get("messages", 0); streak = data.get("streak", 0)
    badges = data.get("badges", [])
    checks = [
        ("first_message", msgs >= 1), ("level_5", level >= 5), ("level_10", level >= 10),
        ("level_25", level >= 25), ("level_50", level >= 50), ("messages_100", msgs >= 100),
        ("messages_1000", msgs >= 1000), ("streak_7", streak >= 7), ("streak_30", streak >= 30),
        ("booster", bool(member.premium_since)),
    ]
    for bid, cond in checks:
        if cond and bid not in badges:
            if await bot.db.award_badge(member.id, member.guild.id, bid):
                a = next((x for x in ACHIEVEMENTS if x["id"] == bid), None)
                if a: newly.append(a)
    return newly

async def check_top_leaderboard(guild: discord.Guild):
    rows = await bot.db.get_leaderboard(guild.id, 1)
    if not rows: return
    top_uid = rows[0]["user_id"]
    member  = guild.get_member(top_uid)
    if not member: return
    data = await bot.db.get_level_data(top_uid, guild.id)
    if "top_leaderboard" not in data.get("badges", []):
        awarded = await bot.db.award_badge(top_uid, guild.id, "top_leaderboard")
        if awarded:
            config = await get_config(guild.id)
            lc = get_log_channel(guild, config, "bot")
            if lc:
                try: await lc.send(embed=make_embed(C_GOLD, f"🏆 {member.mention} is **#1 on the leaderboard** and earned **The Best** badge!"))
                except Exception: pass


# ═══════════════════════════════════════════════════════════════════════════════
#  LEVEL ROLES  (FIXED: correctly returns the role earned at exactly new_level)
# ═══════════════════════════════════════════════════════════════════════════════

async def apply_level_roles(member: discord.Member, new_level: int) -> Optional[str]:
    """Give all roles the member has earned. Return the name of the role earned at exactly new_level.
    Uses guild config level_roles if set, otherwise falls back to hardcoded LEVEL_ROLES."""
    config      = await get_config(member.guild.id)
    config_rows = config.get("level_roles", [])

    # Build ladder: list of (req_level, role_id_or_name, is_id)
    if config_rows:
        ladder = [(entry["level"], entry["role_id"], True) for entry in config_rows]
    else:
        ladder = [(req, name, False) for req, name in LEVEL_ROLES]

    exact_reward = None
    for req, ref, is_id in sorted(ladder, key=lambda x: x[0]):
        if new_level >= req:
            role = member.guild.get_role(ref) if is_id else resolve_role(member.guild, ref)
            if role and role not in member.roles:
                try:
                    await member.add_roles(role, reason=f"Level {req} reward")
                    if req == new_level:
                        exact_reward = role.name
                except Exception as e:
                    logger.warning("Failed to add level role %s: %s", ref, e)

    if not exact_reward:
        for req, ref, is_id in sorted(ladder, key=lambda x: x[0], reverse=True):
            if new_level >= req:
                role = member.guild.get_role(ref) if is_id else resolve_role(member.guild, ref)
                if role and role in member.roles:
                    exact_reward = role.name
                    break
    return exact_reward


# ═══════════════════════════════════════════════════════════════════════════════
#  GIVEAWAY SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════

def giveaway_embed(prize: str, host_id: int, ends_at: datetime, winners: int, entrant_count: int, ended: bool = False, winner_mentions: list[str] = None) -> discord.Embed:
    if ended:
        e = make_embed(C_WARNING)
        e.title = "🎉 Giveaway Ended"
        if winner_mentions:
            e.description = f"**Prize:** {prize}\n**Winners:** {', '.join(winner_mentions)}"
        else:
            e.description = f"**Prize:** {prize}\n*No winners — not enough entrants.*"
    else:
        e = make_embed(C_GOLD)
        e.title = f"🎉 GIVEAWAY — {prize}"
        e.description = (
            f"React with 🎉 to enter!\n\n"
            f"**Ends:** <t:{int(ends_at.timestamp())}:R>\n"
            f"**Winners:** {winners}\n"
            f"**Hosted by:** <@{host_id}>\n"
            f"**Entries:** {entrant_count}"
        )
    e.set_footer(text=f"{'Ended' if ended else 'Ends'} • {ends_at.strftime('%Y-%m-%d %H:%M UTC')}")
    return e

async def do_end_giveaway(giveaway: dict, guild: discord.Guild) -> list[int]:
    """Pick winners, update the message, announce in channel. Returns list of winner IDs."""
    entrants = giveaway.get("entrants", [])
    num_winners = min(giveaway.get("winners", 1), len(entrants))
    winners = random.sample(entrants, num_winners) if entrants else []

    channel = guild.get_channel(giveaway.get("channel_id"))
    if not channel: return winners

    winner_mentions = [f"<@{w}>" for w in winners]

    # Update original message
    try:
        msg = await channel.fetch_message(giveaway["message_id"])
        await msg.edit(embed=giveaway_embed(
            giveaway["prize"], giveaway["host_id"],
            giveaway["ends_at"], giveaway["winners"],
            len(entrants), ended=True, winner_mentions=winner_mentions,
        ))
    except Exception: pass

    # Announce
    try:
        if winners:
            await channel.send(
                content=" ".join(winner_mentions),
                embed=make_embed(C_GOLD, f"Congratulations {' '.join(winner_mentions)}! You won **{giveaway['prize']}**! 🎉"),
            )
        else:
            await channel.send(embed=make_embed(C_WARNING, f"The giveaway for **{giveaway['prize']}** ended with no valid entrants."))
    except Exception: pass

    return winners

class GiveawayEnterView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="🎉 Enter", style=discord.ButtonStyle.success, custom_id="giveaway:enter")
    async def btn_enter(self, i: discord.Interaction, b):
        giveaway = await bot.db.get_giveaway(i.message.id)
        if not giveaway or giveaway.get("ended"):
            await i.response.send_message("This giveaway has ended.", ephemeral=True); return
        added = await bot.db.add_entrant(i.message.id, i.user.id)
        if added:
            doc = await bot.db.get_giveaway(i.message.id)
            count = len(doc.get("entrants", []))
            try:
                await i.message.edit(embed=giveaway_embed(
                    giveaway["prize"], giveaway["host_id"], giveaway["ends_at"],
                    giveaway["winners"], count,
                ))
            except Exception: pass
            await i.response.send_message("✅ You've entered the giveaway! Good luck 🎉", ephemeral=True)
        else:
            # Already entered — toggle out
            await bot.db.remove_entrant(i.message.id, i.user.id)
            doc = await bot.db.get_giveaway(i.message.id)
            count = len(doc.get("entrants", []))
            try:
                await i.message.edit(embed=giveaway_embed(
                    giveaway["prize"], giveaway["host_id"], giveaway["ends_at"],
                    giveaway["winners"], count,
                ))
            except Exception: pass
            await i.response.send_message("❌ You've left the giveaway.", ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  SETUP SYSTEM  (IMPROVED: multi-select for roles and channels)
# ═══════════════════════════════════════════════════════════════════════════════

# ── Multi-channel select (stores list) ────────────────────────────────────────
class MultiChannelSelect(discord.ui.ChannelSelect):
    """Selects up to 10 channels and appends them to a config list key."""
    def __init__(self, config_key: str, guild_id: int, parent_view,
                 placeholder: str = "Select channels…", max_values: int = 10):
        super().__init__(
            placeholder=placeholder,
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=max_values,
            custom_id=f"mchsel:{config_key}",
        )
        self.config_key  = config_key
        self.guild_id    = guild_id
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        ids = [ch.id for ch in self.values]
        config = await get_config(self.guild_id)
        existing = config.get(self.config_key, [])
        merged = list(dict.fromkeys(existing + ids))  # dedup, preserve order
        await bot.db.update_config(self.guild_id, self.config_key, merged)
        names = ", ".join(ch.mention for ch in self.values)
        await interaction.response.send_message(embed=ok(f"Added: {names}"), ephemeral=True)
        if hasattr(self.parent_view, "refresh"):
            await self.parent_view.refresh(interaction)

class SingleChannelSelect(discord.ui.ChannelSelect):
    """Selects exactly one channel and stores its ID under a single config key."""
    def __init__(self, config_key: str, guild_id: int, parent_view,
                 placeholder: str = "Select a channel…"):
        super().__init__(
            placeholder=placeholder,
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1,
            custom_id=f"schsel:{config_key}",
        )
        self.config_key  = config_key
        self.guild_id    = guild_id
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        ch = self.values[0]
        await bot.db.update_config(self.guild_id, self.config_key, ch.id)
        await interaction.response.send_message(embed=ok(f"Set to {ch.mention}"), ephemeral=True)
        if hasattr(self.parent_view, "refresh"):
            await self.parent_view.refresh(interaction)

# ── Multi-role select (appends to list) ───────────────────────────────────────
class MultiRoleSelect(discord.ui.RoleSelect):
    """Selects up to 10 roles and appends their IDs to a config list key."""
    def __init__(self, config_key: str, guild_id: int, parent_view,
                 placeholder: str = "Select roles…", max_values: int = 10,
                 store_as_objects: bool = False):
        super().__init__(
            placeholder=placeholder,
            min_values=1, max_values=max_values,
            custom_id=f"mrolesel:{config_key}",
        )
        self.config_key     = config_key
        self.guild_id       = guild_id
        self.parent_view    = parent_view
        self.store_as_objects = store_as_objects  # if True, store {"role_id": id} dicts

    async def callback(self, interaction: discord.Interaction):
        config   = await get_config(self.guild_id)
        existing = config.get(self.config_key, [])

        if self.store_as_objects:
            existing_ids = {e.get("role_id") for e in existing}
            new_entries  = [{"role_id": r.id} for r in self.values if r.id not in existing_ids]
            merged = existing + new_entries
        else:
            existing_ids = set(existing)
            new_ids      = [r.id for r in self.values if r.id not in existing_ids]
            merged       = existing + new_ids

        await bot.db.update_config(self.guild_id, self.config_key, merged)
        names = ", ".join(r.mention for r in self.values)
        await interaction.response.send_message(embed=ok(f"Added: {names}"), ephemeral=True)
        if hasattr(self.parent_view, "refresh"):
            await self.parent_view.refresh(interaction)

class SingleRoleSelect(discord.ui.RoleSelect):
    """Selects one role and stores its ID under a single config key."""
    def __init__(self, config_key: str, guild_id: int, parent_view,
                 placeholder: str = "Select a role…"):
        super().__init__(
            placeholder=placeholder,
            min_values=1, max_values=1,
            custom_id=f"srolesel:{config_key}",
        )
        self.config_key  = config_key
        self.guild_id    = guild_id
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        role = self.values[0]
        await bot.db.update_config(self.guild_id, self.config_key, role.id)
        await interaction.response.send_message(embed=ok(f"Set to {role.mention}"), ephemeral=True)
        if hasattr(self.parent_view, "refresh"):
            await self.parent_view.refresh(interaction)


_STAFF_ROLE_SLOTS = [
    ("staff_owner_role_id",             "👑 Manager",
     "Full access to all bot commands. Cannot be abuse-stripped."),
    ("staff_community_manager_role_id", "👑 Community Manager",
     "Full access — same as Manager. Assign to trusted community leads."),
    ("staff_partnership_manager_role_id","🤝 Partnership Manager",
     "Above Senior Mod. Can post invite links. No moderation commands by default."),
    ("staff_senior_mod_role_id",        "🔵 Senior Moderator",
     "Warn, kick, ban, mute (up to 28d), purge (up to 500), unban, unmute, clear warns."),
    ("staff_mod_role_id",               "🟢 Moderator",
     "Same as Senior Mod: warn, kick, ban, mute, purge, unban, unmute."),
    ("staff_trial_mod_role_id",         "🟡 Trial Moderator",
     "Warn, mute (max 1h), purge (max 30 msgs). Cannot kick or ban."),
    ("staff_all_role_id",               "⚪ All Staff (no mod perms)",
     "Cosmetic staff role. Bypasses spam filter. No moderation commands."),
    ("staff_inv_bypass_role_id",        "🔗 Invite Link Bypass",
     "Allows posting invite links without automod deletion."),
]


def setup_embed(config: dict, guild: discord.Guild) -> discord.Embed:
    e = make_embed(C_PRIMARY)
    e.title = "\u2699\ufe0f LXTE\u2019s AI \u2014 Server Setup"

    def ch(key):
        v = config.get(key)
        return f"<#{v}>" if v else "\u274c not set"

    def ch_list(key):
        ids = config.get(key, [])
        if not ids:
            return "\u274c none"
        return " ".join(f"<#{i}>" for i in ids[:3]) + (f" +{len(ids)-3}" if len(ids) > 3 else "")

    def tick(val):
        return "\u2705" if val else "\u274c"

    ticket_panel = config.get("ticket_panel_channel_id")
    ticket_roles = config.get("ticket_staff_role_ids", [])
    essentials_done = sum([bool(ticket_panel), bool(ticket_roles), log_any, bool(welcome_ch)])
    e.description = (
        f"**{essentials_done}/5 essentials configured** \u2014 click a section below to set it up.\n"
        "New here? Start with **\U0001f3ab Tickets** then **\U0001f4cb Logs** then **\U0001f916 AI**.\n\u200b"
    )

    tickets_label = "\U0001f3ab Tickets" + (" \u2705" if ticket_panel and ticket_roles else " \u26a0\ufe0f")
    e.add_field(
        name=tickets_label,
        value=(
            f"Panel: {ch('ticket_panel_channel_id')}\n"
            f"Staff roles: {len(ticket_roles)} set\n"
            f"Log: {ch('ticket_log_channel_id')}"
        ),
        inline=True,
    )

    logs_label = "\U0001f4cb Log Channels" + (" \u2705" if log_any else " \u274c")
    e.add_field(
        name=logs_label,
        value=(
            f"\U0001f4ac {ch('message_log_channel_id')}\n"
            f"\U0001f6e1\ufe0f {ch('automod_log_channel_id')}\n"
            f"\u2696\ufe0f {ch('mod_log_channel_id')}\n"
            f"\U0001f6aa {ch('entry_log_channel_id')}\n"
            f"\U0001f916 {ch('bot_log_channel_id')}"
        ),
        inline=True,
    )

    e.add_field(
        name=ai_label,
        value=(
        ),
        inline=True,
    )

    welcome_label = "\U0001f44b Welcome" + (" \u2705" if welcome_ch else " \u274c")
    e.add_field(
        name=welcome_label,
        value=(
            f"Channel: {ch('welcome_channel_id')}\n"
            f"DM on join: {tick(config.get('welcome_dm_enabled'))}"
        ),
        inline=True,
    )

    automod_on = config.get("automod_enabled", True)
    e.add_field(
        name="\U0001f6e1\ufe0f Automod" + (" \u2705" if automod_on else " \u274c"),
        value=(
            f"Enabled: {tick(automod_on)}\n"
            f"Anti-spam: {tick(config.get('antispam_enabled', True))}\n"
        ),
        inline=True,
    )

    e.add_field(
        name="\U0001f3ad Roles",
        value=(
            f"Auto-roles: {len(config.get('autoroles', []))}\n"
            f"2XP roles: {len(config.get('double_xp_roles', []))}\n"
            f"Level roles: {len(config.get('level_roles', []))}"
        ),
        inline=True,
    )

    e.add_field(name="\U0001f680 Boosts",    value=f"Channel: {ch('boost_channel_id')}",    inline=True)
    e.add_field(name="\U0001f389 Giveaways", value=f"Channel: {ch('giveaway_channel_id')}", inline=True)
    # Staff roles field
    staff_configured = any(config.get(k) for k, _, _ in _STAFF_ROLE_SLOTS)
    e.add_field(
        name="🛡️ Staff Roles" + (" ✅" if staff_configured else " ❌"),
        value=(
            f"Manager: {'✅' if config.get('staff_owner_role_id') else '❌'}  "
            f"ComMgr: {'✅' if config.get('staff_community_manager_role_id') else '❌'}  "
            f"Partner: {'✅' if config.get('staff_partnership_manager_role_id') else '❌'}\n"
            f"Sr.Mod: {'✅' if config.get('staff_senior_mod_role_id') else '❌'}  "
            f"Mod: {'✅' if config.get('staff_mod_role_id') else '❌'}  "
            f"Trial: {'✅' if config.get('staff_trial_mod_role_id') else '❌'}"
        ),
        inline=True,
    )

    e.add_field(name="\u200b", value="\u200b", inline=True)

    e.set_footer(text="Admins only  \u2022  .help for all commands  \u2022  .quickstart for guided setup")
    return e


class SetupView(discord.ui.View):
    def __init__(self, owner_id: int, guild_id: int, msg=None):
        super().__init__(timeout=600)
        self.owner_id = owner_id
        self.guild_id = guild_id
        self._msg     = msg

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if not i.user.guild_permissions.administrator:
            await i.response.send_message(embed=err("Admins only."), ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self._msg:
            try: await self._msg.edit(view=None)
            except Exception: pass

    async def refresh(self, interaction: discord.Interaction):
        config = await get_config(self.guild_id)
        try: await interaction.message.edit(embed=setup_embed(config, interaction.guild), view=self)
        except Exception: pass

    @discord.ui.button(label="👋 Welcome",    style=discord.ButtonStyle.secondary, row=0)
    async def btn_welcome(self, i, b):
        config = await get_config(self.guild_id)
        def ch(k):
            v = config.get(k)
            return f"<#{v}>" if v else "`not set`"
        desc = (
            "**Welcome channel:** " + ch("welcome_channel_id") + "\n"
            "**Leave channel:** " + ch("leave_channel_id") + "\n"
            "**DM on join:** " + ("✅" if config.get("welcome_dm_enabled") else "❌") + "\n\n"
            "Configure where join/leave messages are sent."
        )
        await i.response.send_message(embed=make_embed(C_INFO, desc), view=WelcomeSetupView(self.owner_id, self.guild_id), ephemeral=True)

    @discord.ui.button(label="🛡️ Automod",   style=discord.ButtonStyle.secondary, row=0)
    async def btn_automod(self, i, b):
        config = await get_config(self.guild_id)
        await i.response.send_message(embed=make_embed(C_INFO, AutomodSetupView(self.owner_id, self.guild_id)._status(config)), view=AutomodSetupView(self.owner_id, self.guild_id), ephemeral=True)

    @discord.ui.button(label="📋 Log Channels", style=discord.ButtonStyle.primary, row=0)
    async def btn_logchannels(self, i, b):
        config = await get_config(self.guild_id)
        def ch(key):
            v = config.get(key); return f"<#{v}>" if v else "`not set`"
        desc = (
            f"💬 **Message logs:** {ch('message_log_channel_id')}\n"
            f"🛡️ **Automod logs:** {ch('automod_log_channel_id')}\n"
            f"⚖️ **Mod logs:** {ch('mod_log_channel_id')}\n"
            f"🚪 **Entry/exit logs:** {ch('entry_log_channel_id')}\n"
            f"🤖 **Bot logs:** {ch('bot_log_channel_id')}\n"
            f"🌐 **Server logs:** {ch('server_log_channel_id')}\n"
            f"🎫 **Ticket logs:** {ch('ticket_log_channel_id')}"
        )
        await i.response.send_message(embed=make_embed(C_INFO, desc), view=LogChannelsSetupView(self.owner_id, self.guild_id), ephemeral=True)

    @discord.ui.button(label="🎫 Tickets",   style=discord.ButtonStyle.secondary, row=0)
    async def btn_tickets(self, i, b):
        config = await get_config(self.guild_id)
        def ch(k):
            v = config.get(k)
            return f"<#{v}>" if v else "`not set`"
        roles = config.get("ticket_staff_role_ids", [])
        roles_str = " ".join(f"<@&{r}>" for r in roles) if roles else "`none set`"
        desc = (
            "**Ticket panel channel:** " + ch("ticket_panel_channel_id") + "\n"
            "**Staff roles:** " + roles_str + "\n"
            "**Log channel:** " + ch("ticket_log_channel_id") + "\n\n"
            "Use the buttons below to update any of these."
        )
        await i.response.send_message(embed=make_embed(C_INFO, desc), view=TicketSetupView(self.owner_id, self.guild_id), ephemeral=True)

    @discord.ui.button(label="🚀 Boosts",    style=discord.ButtonStyle.secondary, row=1)
    async def btn_boosts(self, i, b):
        await i.response.send_message(embed=make_embed(C_INFO, "Configure boost announcements:"), view=BoostSetupView(self.owner_id, self.guild_id), ephemeral=True)

    @discord.ui.button(label="🎭 Roles",     style=discord.ButtonStyle.secondary, row=1)
    async def btn_roles(self, i, b):
        await i.response.send_message(embed=make_embed(C_INFO, "Configure auto-roles and 2XP roles:"), view=RolesSetupView(self.owner_id, self.guild_id), ephemeral=True)

    @discord.ui.button(label="🎉 Giveaways", style=discord.ButtonStyle.primary,   row=2)
    async def btn_giveaways(self, i, b):
        await i.response.send_message(embed=make_embed(C_INFO, "Configure giveaway channel:"), view=GiveawaySetupView(self.owner_id, self.guild_id), ephemeral=True)

    @discord.ui.button(label="📋 Role Menus", style=discord.ButtonStyle.secondary, row=2)
    async def btn_rolemenus(self, i, b):
        await i.response.send_message(embed=make_embed(C_INFO, "Manage role menus:"), view=RoleMenuSetupView(self.owner_id, self.guild_id), ephemeral=True)

    @discord.ui.button(label="🎭 Reactions",  style=discord.ButtonStyle.secondary, row=2)
    async def btn_reactions(self, i, b):
        await i.response.send_message(embed=make_embed(C_INFO, "Manage reaction roles:"), view=ReactionSetupView(self.owner_id, self.guild_id), ephemeral=True)

    @discord.ui.button(label="⬆️ Level Roles", style=discord.ButtonStyle.secondary, row=3)
    async def btn_levelroles(self, i, b):
        await i.response.send_message(embed=make_embed(C_INFO, "Configure level-up roles:"), view=LevelRolesSetupView(self.owner_id, self.guild_id), ephemeral=True)

    @discord.ui.button(label="🛡️ Staff Roles", style=discord.ButtonStyle.primary,   row=3)
    async def btn_staff_roles(self, i, b):
        config = await get_config(self.guild_id)
        await i.response.send_message(
            embed=_staff_roles_embed(config, i.guild),
            view=StaffRolesSetupView(self.owner_id, self.guild_id),
            ephemeral=True,
        )

    @discord.ui.button(label="🔒 Fake Perms", style=discord.ButtonStyle.primary,   row=4)
    async def btn_fake_perms(self, i, b):
        await i.response.send_message(
            embed=_fake_perms_embed(i.guild),
            view=FakePermsSetupView(self.owner_id, self.guild_id),
            ephemeral=True,
        )

    @discord.ui.button(label="✖ Close",      style=discord.ButtonStyle.danger,    row=4)
    async def btn_close(self, i, b):
        await i.message.delete()

# ── Welcome Setup ─────────────────────────────────────────────────────────────
class WelcomeSetupView(discord.ui.View):
    def __init__(self, owner_id: int, guild_id: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.add_item(SingleChannelSelect("welcome_channel_id", guild_id, self, "Pick welcome channel…"))

    async def refresh(self, interaction): pass

    @discord.ui.button(label="Toggle DM Welcome", style=discord.ButtonStyle.secondary, row=1)
    async def btn_dm(self, i: discord.Interaction, b):
        config = await get_config(self.guild_id)
        await bot.db.update_config(self.guild_id, "welcome_dm_enabled", not config.get("welcome_dm_enabled", False))
        config = await get_config(self.guild_id)
        await i.response.edit_message(embed=make_embed(C_SUCCESS, f"DM welcome: {'✅' if config.get('welcome_dm_enabled') else '❌'}"), view=self)

    @discord.ui.button(label="Set Custom Message", style=discord.ButtonStyle.primary, row=1)
    async def btn_msg(self, i: discord.Interaction, b):
        await i.response.send_modal(WelcomeMsgModal(self.guild_id))


class WelcomeMsgModal(discord.ui.Modal, title="Welcome Message"):
    title_i = discord.ui.TextInput(label="Embed title", max_length=100, default=WELCOME_TITLE)
    msg_i   = discord.ui.TextInput(label="Message ({user} {server} {count})", style=discord.TextStyle.paragraph, max_length=800, default=WELCOME_MSG)
    def __init__(self, guild_id): super().__init__(); self.guild_id = guild_id
    async def on_submit(self, i):
        await bot.db.update_config(self.guild_id, "welcome_title",   self.title_i.value.strip())
        await bot.db.update_config(self.guild_id, "welcome_message", self.msg_i.value.strip())
        await i.response.send_message(embed=ok("Welcome message updated."), ephemeral=True)


# ── Automod Setup ─────────────────────────────────────────────────────────────
class AutomodSetupView(discord.ui.View):
    def __init__(self, owner_id: int, guild_id: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id

    async def refresh(self, i): pass

    async def _toggle(self, i, key, default=True):
        config = await get_config(self.guild_id)
        await bot.db.update_config(self.guild_id, key, not config.get(key, default))
        config = await get_config(self.guild_id)
        await i.response.edit_message(embed=make_embed(C_INFO, self._status(config)), view=self)

    def _status(self, config):
        return (
            f"Automod: {'✅' if config.get('automod_enabled', True) else '❌'}\n"
            f"No Invites: {'✅' if config.get('automod_no_invites', True) else '❌'}\n"
            f"No Links: {'✅' if config.get('automod_no_links', True) else '❌'}\n"
            f"Anti-Raid: {'✅' if config.get('antiraid_enabled', True) else '❌'}\n"
            f"Anti-Spam: {'✅' if config.get('antispam_enabled', True) else '❌'}\n"
            f"Anti-Caps: {'✅' if config.get('anti_caps_enabled', False) else '❌'}\n"
            f"Anti-Emoji: {'✅' if config.get('anti_emoji_spam_enabled', False) else '❌'}\n"
            f"Anti-Nuke: {'✅' if config.get('antinuke_enabled', True) else '❌'}\n"
            f"Ghost Ping: {'✅' if config.get('anti_ghost_ping_enabled', True) else '❌'}\n"
            f"Mass Mention: {'✅' if config.get('anti_mass_mention_enabled', True) else '❌'}"
        )

    @discord.ui.button(label="Toggle Automod",      style=discord.ButtonStyle.secondary, row=0)
    async def t1(self, i, b): await self._toggle(i, "automod_enabled")

    @discord.ui.button(label="Toggle No Invites",   style=discord.ButtonStyle.secondary, row=0)
    async def t2(self, i, b): await self._toggle(i, "automod_no_invites")

    @discord.ui.button(label="Toggle No Links",     style=discord.ButtonStyle.secondary, row=0)
    async def t3(self, i, b): await self._toggle(i, "automod_no_links")

    @discord.ui.button(label="Toggle Anti-Raid",    style=discord.ButtonStyle.secondary, row=1)
    async def t4(self, i, b): await self._toggle(i, "antiraid_enabled")


    @discord.ui.button(label="Toggle Anti-Spam",    style=discord.ButtonStyle.secondary, row=1)
    async def t6(self, i, b): await self._toggle(i, "antispam_enabled")

    @discord.ui.button(label="Toggle Anti-Caps",    style=discord.ButtonStyle.secondary, row=2)
    async def t7(self, i, b): await self._toggle(i, "anti_caps_enabled", default=False)

    @discord.ui.button(label="Toggle Anti-Emoji",   style=discord.ButtonStyle.secondary, row=2)
    async def t8(self, i, b): await self._toggle(i, "anti_emoji_spam_enabled", default=False)

    @discord.ui.button(label="Toggle Anti-Nuke",    style=discord.ButtonStyle.secondary, row=2)
    async def t9(self, i, b): await self._toggle(i, "antinuke_enabled")

    @discord.ui.button(label="Toggle Ghost Ping",   style=discord.ButtonStyle.secondary, row=3)
    async def t10(self, i, b): await self._toggle(i, "anti_ghost_ping_enabled")

    @discord.ui.button(label="Toggle Mass Mention", style=discord.ButtonStyle.secondary, row=3)
    async def t11(self, i, b): await self._toggle(i, "anti_mass_mention_enabled")


# ── Log Channels Setup ────────────────────────────────────────────────────────
_LOG_CHANNEL_OPTIONS = [
    ("message_log_channel_id", "💬 Message logs",    "Message edits & deletes"),
    ("automod_log_channel_id", "🛡️ Automod logs",   "Spam, slurs, invites, caps…"),
    ("mod_log_channel_id",     "⚖️ Mod logs",        "Warns, timeouts, purges"),
    ("entry_log_channel_id",   "🚪 Entry/Exit logs", "Member joins & leaves"),
    ("bot_log_channel_id",     "🤖 Bot logs",        "Online, shutdown, restart"),
    ("server_log_channel_id",  "🌐 Server logs",     "Every server change"),
    ("ticket_log_channel_id",  "🎫 Ticket logs",     "Ticket transcripts on close"),
]

class LogChannelsSetupView(discord.ui.View):
    """
    Configure the five log channels one at a time.
    Row 0 — category picker (which log type to set)
    Row 1 — channel select (populated after picking a category)
    Row 2 — Clear All button
    This layout uses only 3 rows and stays within Discord's limits.
    """
    def __init__(self, owner_id: int, guild_id: int):
        super().__init__(timeout=180)
        self.guild_id        = guild_id
        self._selected_key   = None  # config key chosen from the category picker

        # Row 0: category picker
        cat_select = discord.ui.Select(
            placeholder="Pick which log channel to set…",
            custom_id="logch:category",
            row=0,
            options=[
                discord.SelectOption(label=label, value=key, description=desc)
                for key, label, desc in _LOG_CHANNEL_OPTIONS
            ],
        )
        cat_select.callback = self._on_category
        self.add_item(cat_select)

        # Row 1: channel select — placeholder until a category is chosen
        self._ch_select = discord.ui.ChannelSelect(
            placeholder="← Pick a log type first",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1,
            custom_id="logch:channel",
            row=1,
            disabled=True,
        )
        self._ch_select.callback = self._on_channel
        self.add_item(self._ch_select)

    async def _on_category(self, interaction: discord.Interaction):
        self._selected_key = interaction.data["values"][0]
        label = next(lbl for key, lbl, _ in _LOG_CHANNEL_OPTIONS if key == self._selected_key)
        # Enable the channel select and update its placeholder
        self._ch_select.disabled     = False
        self._ch_select.placeholder  = f"Set channel for {label}…"
        await interaction.response.edit_message(view=self)

    async def _on_channel(self, interaction: discord.Interaction):
        if not self._selected_key:
            await interaction.response.send_message(embed=err("Pick a log type first."), ephemeral=True)
            return
        ch = self._ch_select.values[0]
        await bot.db.update_config(self.guild_id, self._selected_key, ch.id)
        label = next(lbl for key, lbl, _ in _LOG_CHANNEL_OPTIONS if key == self._selected_key)
        # Reset for next pick
        self._selected_key          = None
        self._ch_select.disabled    = True
        self._ch_select.placeholder = "← Pick a log type first"
        await interaction.response.send_message(embed=ok(f"{label} set to {ch.mention}."), ephemeral=True)
        await self.refresh(interaction)

    async def refresh(self, interaction: discord.Interaction):
        config = await get_config(self.guild_id)
        def ch(key):
            v = config.get(key)
            return f"<#{v}>" if v else "`not set`"
        desc = (
            f"💬 **Message logs:** {ch('message_log_channel_id')}\n"
            f"🛡️ **Automod logs:** {ch('automod_log_channel_id')}\n"
            f"⚖️ **Mod logs:** {ch('mod_log_channel_id')}\n"
            f"🚪 **Entry/exit logs:** {ch('entry_log_channel_id')}\n"
            f"🤖 **Bot logs:** {ch('bot_log_channel_id')}\n"
            f"🌐 **Server logs:** {ch('server_log_channel_id')}\n"
            f"🎫 **Ticket logs:** {ch('ticket_log_channel_id')}"
        )
        try: await interaction.message.edit(embed=make_embed(C_INFO, desc), view=self)
        except Exception: pass

    @discord.ui.button(label="Clear All Log Channels", style=discord.ButtonStyle.danger, row=2)
    async def clear_all(self, i: discord.Interaction, b):
        for key in ("message_log_channel_id", "automod_log_channel_id", "mod_log_channel_id",
                    "entry_log_channel_id", "bot_log_channel_id", "server_log_channel_id",
                    "ticket_log_channel_id", "log_channel_id"):
            await bot.db.update_config(self.guild_id, key, None)
        await i.response.send_message(embed=ok("All log channels cleared."), ephemeral=True)
        await self.refresh(i)


# ── Ticket Setup ──────────────────────────────────────────────────────────────
class TicketSetupView(discord.ui.View):
    def __init__(self, owner_id: int, guild_id: int):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.add_item(SingleChannelSelect("ticket_panel_channel_id", guild_id, self, "Pick ticket panel channel…"))
        self.add_item(SingleChannelSelect("ticket_log_channel_id",   guild_id, self, "Pick ticket log channel…"))
        # Multi-select for staff roles — up to 5 staff roles
        self.add_item(MultiRoleSelect("ticket_staff_role_ids", guild_id, self, "Pick staff roles (multi)…", max_values=5))

    async def refresh(self, i): pass

    @discord.ui.button(label="Post Ticket Panel", style=discord.ButtonStyle.success, row=3)
    async def btn_post(self, i: discord.Interaction, b):
        config = await get_config(self.guild_id)
        ch_id  = config.get("ticket_panel_channel_id")
        if not ch_id:
            await i.response.send_message(embed=err("Set panel channel first."), ephemeral=True); return
        ch = i.guild.get_channel(ch_id)
        if not ch:
            await i.response.send_message(embed=err("Channel not found."), ephemeral=True); return
        e = make_embed(C_PRIMARY)
        e.title       = "🎫 LXTE Clan — Tickets"
        e.description = "Want to join LXTE Clan or need help? Click below."
        e.set_footer(text="LXTE Clan  •  Ticket System")
        await ch.send(embed=e, view=TicketOpenView())
        await i.response.send_message(embed=ok(f"Panel posted in {ch.mention}."), ephemeral=True)

    @discord.ui.button(label="Clear Staff Roles", style=discord.ButtonStyle.danger, row=3)
    async def btn_clear_staff(self, i: discord.Interaction, b):
        await bot.db.update_config(self.guild_id, "ticket_staff_role_ids", [])
        await i.response.send_message(embed=ok("Staff roles cleared."), ephemeral=True)


# ── Boost Setup ───────────────────────────────────────────────────────────────
class BoostSetupView(discord.ui.View):
    def __init__(self, owner_id: int, guild_id: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.add_item(SingleChannelSelect("boost_channel_id", guild_id, self, "Pick boost announce channel…"))
        self.add_item(SingleRoleSelect("boost_perk_role_id", guild_id, self, "Pick boost perk role…"))

    async def refresh(self, i): pass

    @discord.ui.button(label="Set Thank-You Message", style=discord.ButtonStyle.secondary, row=2)
    async def btn_msg(self, i, b): await i.response.send_modal(BoostMsgModal(self.guild_id))


class BoostMsgModal(discord.ui.Modal, title="Boost Thank-You Message"):
    msg = discord.ui.TextInput(label="Message", style=discord.TextStyle.paragraph, max_length=400)
    def __init__(self, guild_id): super().__init__(); self.guild_id = guild_id
    async def on_submit(self, i):
        await bot.db.update_config(self.guild_id, "boost_thank_you_message", self.msg.value.strip())
        await i.response.send_message(embed=ok("Boost message updated."), ephemeral=True)


# ── Roles Setup ───────────────────────────────────────────────────────────────
class RolesSetupView(discord.ui.View):
    def __init__(self, owner_id: int, guild_id: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        # Row 0: add multiple auto-roles at once
        self.add_item(MultiRoleSelect("autoroles", guild_id, self, "Add auto-roles (multi)…", max_values=10, store_as_objects=True))
        # Row 1: add multiple 2XP roles at once
        self.add_item(MultiRoleSelect("double_xp_roles", guild_id, self, "Add 2XP roles (multi)…", max_values=10))

    async def refresh(self, i): pass

    @discord.ui.button(label="Clear All Auto-Roles",  style=discord.ButtonStyle.danger,    row=2)
    async def ar_clear(self, i, b):
        await bot.db.update_config(self.guild_id, "autoroles", [])
        await i.response.send_message(embed=ok("Auto-roles cleared."), ephemeral=True)

    @discord.ui.button(label="Clear All 2XP Roles",   style=discord.ButtonStyle.danger,    row=2)
    async def dxp_clear(self, i, b):
        await bot.db.update_config(self.guild_id, "double_xp_roles", [])
        await i.response.send_message(embed=ok("2XP roles cleared."), ephemeral=True)

    @discord.ui.button(label="Remove Auto-Role",      style=discord.ButtonStyle.secondary, row=3)
    async def ar_rem(self, i, b): await i.response.send_modal(ARRemoveModal(self.guild_id))

    @discord.ui.button(label="Remove 2XP Role",       style=discord.ButtonStyle.secondary, row=3)
    async def dxp_rem(self, i, b): await i.response.send_modal(DXPRemoveModal(self.guild_id))


class ARRemoveModal(discord.ui.Modal, title="Remove Auto-Role"):
    name = discord.ui.TextInput(label="Role name to remove", max_length=100)
    def __init__(self, gid): super().__init__(); self.gid = gid
    async def on_submit(self, i):
        role = resolve_role(i.guild, self.name.value)
        if not role: await i.response.send_message(embed=err(f"No role `{self.name.value}`."), ephemeral=True); return
        config = await get_config(self.gid)
        roles  = [r for r in config.get("autoroles", []) if r.get("role_id") != role.id]
        await bot.db.update_config(self.gid, "autoroles", roles)
        await i.response.send_message(embed=ok(f"Removed `{role.name}` from auto-roles."), ephemeral=True)

class DXPRemoveModal(discord.ui.Modal, title="Remove 2XP Role"):
    name = discord.ui.TextInput(label="Role name to remove", max_length=100)
    def __init__(self, gid): super().__init__(); self.gid = gid
    async def on_submit(self, i):
        role = resolve_role(i.guild, self.name.value)
        if not role: await i.response.send_message(embed=err(f"No role `{self.name.value}`."), ephemeral=True); return
        config = await get_config(self.gid)
        ids    = [r for r in config.get("double_xp_roles", []) if r != role.id]
        await bot.db.update_config(self.gid, "double_xp_roles", ids)
        await i.response.send_message(embed=ok(f"Removed `{role.name}` from 2XP roles."), ephemeral=True)


# ── AI Setup ──────────────────────────────────────────────────────────────────

# ── Giveaway Setup ────────────────────────────────────────────────────────────
class GiveawaySetupView(discord.ui.View):
    def __init__(self, owner_id: int, guild_id: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.add_item(SingleChannelSelect("giveaway_channel_id", guild_id, self, "Pick giveaway channel…"))

    async def refresh(self, i): pass


# ── Role Menu Setup ───────────────────────────────────────────────────────────
class RoleMenuSetupView(discord.ui.View):
    def __init__(self, owner_id: int, guild_id: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id

    async def refresh(self, i): pass

    @discord.ui.button(label="Create Menu",  style=discord.ButtonStyle.success)
    async def create(self, i, b): await i.response.send_modal(CreateRoleMenuModal(self.guild_id))

    @discord.ui.button(label="Add Role",     style=discord.ButtonStyle.primary)
    async def add(self, i, b): await i.response.send_modal(AddRoleToMenuModal(self.guild_id))

    @discord.ui.button(label="Post Menu",    style=discord.ButtonStyle.primary)
    async def post(self, i, b): await i.response.send_message(embed=make_embed(C_INFO, "Pick channel to post menu in:"), view=_PostRoleMenuView(self.guild_id), ephemeral=True)

    @discord.ui.button(label="Delete Menu",  style=discord.ButtonStyle.danger)
    async def delete(self, i, b): await i.response.send_modal(DeleteRoleMenuModal(self.guild_id))

class _PostRoleMenuView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=60)
        self.guild_id = guild_id

    async def refresh(self, i): pass

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="Pick channel to post menu…", channel_types=[discord.ChannelType.text], row=0)
    async def ch_sel(self, i: discord.Interaction, s: discord.ui.ChannelSelect):
        await i.response.send_modal(PostRoleMenuModal(self.guild_id, s.values[0].id))

class CreateRoleMenuModal(discord.ui.Modal, title="Create Role Menu"):
    mid   = discord.ui.TextInput(label="Menu ID (no spaces)", placeholder="e.g. colors", max_length=32)
    title = discord.ui.TextInput(label="Title", placeholder="e.g. 🎨 Pick Your Color", max_length=80)
    desc  = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, required=False, max_length=300)
    def __init__(self, gid): super().__init__(); self.gid = gid
    async def on_submit(self, i):
        mid = self.mid.value.strip().lower().replace(" ", "_")
        await bot.db.save_role_menu(self.gid, mid, {"guild_id": self.gid, "menu_id": mid, "title": self.title.value.strip(), "description": self.desc.value.strip(), "roles": []})
        await i.response.send_message(embed=ok(f"Menu `{mid}` created. Now add roles then post it."), ephemeral=True)

class AddRoleToMenuModal(discord.ui.Modal, title="Add Role to Menu"):
    mid   = discord.ui.TextInput(label="Menu ID", max_length=32)
    rname = discord.ui.TextInput(label="Role name", max_length=100)
    label = discord.ui.TextInput(label="Button label", max_length=80)
    emoji = discord.ui.TextInput(label="Emoji (optional)", required=False, max_length=10)
    def __init__(self, gid): super().__init__(); self.gid = gid
    async def on_submit(self, i):
        mid  = self.mid.value.strip().lower()
        menu = await bot.db.get_role_menu(self.gid, mid)
        if not menu: await i.response.send_message(embed=err(f"No menu `{mid}`."), ephemeral=True); return
        role = resolve_role(i.guild, self.rname.value)
        if not role: await i.response.send_message(embed=err(f"No role `{self.rname.value}`."), ephemeral=True); return
        roles = menu.get("roles", [])
        if any(r["role_id"] == role.id for r in roles): await i.response.send_message(embed=err("Already in menu."), ephemeral=True); return
        roles.append({"role_id": role.id, "name": role.name, "label": self.label.value.strip(), "emoji": self.emoji.value.strip() or None})
        await bot.db.save_role_menu(self.gid, mid, {"roles": roles})
        await i.response.send_message(embed=ok(f"Added `{role.name}` to `{mid}`."), ephemeral=True)

class PostRoleMenuModal(discord.ui.Modal, title="Post Role Menu"):
    mid = discord.ui.TextInput(label="Menu ID", max_length=32)
    def __init__(self, gid, ch_id): super().__init__(); self.gid = gid; self.ch_id = ch_id
    async def on_submit(self, i):
        mid  = self.mid.value.strip().lower()
        menu = await bot.db.get_role_menu(self.gid, mid)
        if not menu or not menu.get("roles"): await i.response.send_message(embed=err("Menu not found or has no roles."), ephemeral=True); return
        ch = i.guild.get_channel(self.ch_id)
        if not ch: await i.response.send_message(embed=err("Channel not found."), ephemeral=True); return
        e = make_embed(C_PRIMARY)
        e.title = menu.get("title", "Role Menu"); e.description = menu.get("description", "Click to toggle a role.")
        msg = await ch.send(embed=e, view=RoleMenuView(mid, menu["roles"]))
        await bot.db.save_role_menu(self.gid, mid, {"message_id": msg.id, "channel_id": ch.id})
        await i.response.send_message(embed=ok(f"Menu posted in {ch.mention}."), ephemeral=True)

class DeleteRoleMenuModal(discord.ui.Modal, title="Delete Role Menu"):
    mid = discord.ui.TextInput(label="Menu ID", max_length=32)
    def __init__(self, gid): super().__init__(); self.gid = gid
    async def on_submit(self, i):
        await bot.db.delete_role_menu(self.gid, self.mid.value.strip().lower())
        await i.response.send_message(embed=ok(f"Menu `{self.mid.value.strip()}` deleted."), ephemeral=True)


# ── Reaction Setup ────────────────────────────────────────────────────────────
class ReactionSetupView(discord.ui.View):
    def __init__(self, owner_id: int, guild_id: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id

    async def refresh(self, i): pass

    @discord.ui.button(label="➕ Add Mapping", style=discord.ButtonStyle.primary)
    async def add(self, i, b): await i.response.send_modal(AddReactionRoleModal(self.guild_id))

    @discord.ui.button(label="➖ Remove",     style=discord.ButtonStyle.danger)
    async def rem(self, i, b): await i.response.send_modal(RemoveReactionRoleModal(self.guild_id))

    @discord.ui.button(label="📋 List",      style=discord.ButtonStyle.secondary)
    async def lst(self, i, b):
        rows = await bot.db.get_all_reaction_roles(self.guild_id)
        if not rows: await i.response.send_message(embed=make_embed(C_INFO, "No reaction roles set."), ephemeral=True); return
        lines = []
        for row in rows[:20]:
            for emoji, rid in row.get("mappings", {}).items():
                role = i.guild.get_role(rid)
                lines.append(f"Msg `{row.get('message_id','?')}` | {emoji} → {role.name if role else rid}")
        await i.response.send_message(embed=make_embed(C_PRIMARY, "\n".join(lines[:20])), ephemeral=True)

class AddReactionRoleModal(discord.ui.Modal, title="Add Reaction Role"):
    msg_id  = discord.ui.TextInput(label="Message ID", max_length=30, placeholder="Right-click → Copy ID")
    emoji   = discord.ui.TextInput(label="Emoji", max_length=20)
    rname   = discord.ui.TextInput(label="Role name", max_length=100)
    def __init__(self, gid): super().__init__(); self.gid = gid
    async def on_submit(self, i):
        try: mid = int(self.msg_id.value.strip())
        except ValueError: await i.response.send_message(embed=err("Invalid message ID."), ephemeral=True); return
        role = resolve_role(i.guild, self.rname.value)
        if not role: await i.response.send_message(embed=err(f"No role `{self.rname.value}`."), ephemeral=True); return
        emoji    = self.emoji.value.strip()
        rr       = await bot.db.get_reaction_role(self.gid, mid)
        mappings = rr.get("mappings", {})
        mappings[emoji] = role.id
        await bot.db.save_reaction_role(self.gid, mid, {"guild_id": self.gid, "message_id": mid, "mappings": mappings})
        for ch in i.guild.text_channels:
            try: msg = await ch.fetch_message(mid); await msg.add_reaction(emoji); break
            except Exception: continue
        await i.response.send_message(embed=ok(f"{emoji} → **{role.name}** on `{mid}`."), ephemeral=True)

class RemoveReactionRoleModal(discord.ui.Modal, title="Remove Reaction Role"):
    msg_id = discord.ui.TextInput(label="Message ID", max_length=30)
    emoji  = discord.ui.TextInput(label="Emoji", max_length=20)
    def __init__(self, gid): super().__init__(); self.gid = gid
    async def on_submit(self, i):
        try: mid = int(self.msg_id.value.strip())
        except ValueError: await i.response.send_message(embed=err("Invalid ID."), ephemeral=True); return
        rr       = await bot.db.get_reaction_role(self.gid, mid)
        mappings = rr.get("mappings", {})
        mappings.pop(self.emoji.value.strip(), None)
        if mappings: await bot.db.save_reaction_role(self.gid, mid, {"mappings": mappings})
        else:        await bot.db.delete_reaction_role(self.gid, mid)
        await i.response.send_message(embed=ok("Removed."), ephemeral=True)

# ── Level Roles Setup ─────────────────────────────────────────────────────────
class LevelRolesSetupView(discord.ui.View):
    def __init__(self, owner_id: int, guild_id: int):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def refresh(self, i): pass

    @discord.ui.button(label="➕ Add Mapping", style=discord.ButtonStyle.success, row=0)
    async def btn_add(self, i: discord.Interaction, b):
        await i.response.send_modal(AddLevelRoleModal(self.guild_id))

    @discord.ui.button(label="➖ Remove",      style=discord.ButtonStyle.danger,   row=0)
    async def btn_remove(self, i: discord.Interaction, b):
        await i.response.send_modal(RemoveLevelRoleModal(self.guild_id))

    @discord.ui.button(label="📋 List",        style=discord.ButtonStyle.secondary, row=0)
    async def btn_list(self, i: discord.Interaction, b):
        config = await get_config(self.guild_id)
        rows   = sorted(config.get("level_roles", []), key=lambda x: x["level"])
        if not rows:
            await i.response.send_message(
                embed=make_embed(C_INFO, "No custom level roles set.\nUsing hardcoded `LEVEL_ROLES` defaults."),
                ephemeral=True,
            ); return
        lines = []
        for entry in rows:
            role = i.guild.get_role(entry["role_id"])
            role_str = role.mention if role else f"`deleted role {entry['role_id']}`"
            lines.append(f"Level **{entry['level']}** → {role_str}")
        e = make_embed(C_PRIMARY, "\n".join(lines))
        e.title = "⬆️ Level Role Mappings"
        await i.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="🗑️ Clear All",   style=discord.ButtonStyle.danger,   row=1)
    async def btn_clear(self, i: discord.Interaction, b):
        await bot.db.update_config(self.guild_id, "level_roles", [])
        await i.response.send_message(
            embed=ok("All custom level roles cleared. Bot will use hardcoded defaults."),
            ephemeral=True,
        )


class AddLevelRoleModal(discord.ui.Modal, title="Add Level Role"):
    level_input = discord.ui.TextInput(label="Level (number)", placeholder="e.g. 10", max_length=5)
    role_input  = discord.ui.TextInput(label="Role name", placeholder="e.g. Builder │ Level 10", max_length=100)

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, i: discord.Interaction):
        if not self.level_input.value.strip().isdigit():
            await i.response.send_message(embed=err("Level must be a number."), ephemeral=True); return
        level = int(self.level_input.value.strip())
        if level < 1 or level > 9999:
            await i.response.send_message(embed=err("Level must be between 1 and 9999."), ephemeral=True); return
        role = resolve_role(i.guild, self.role_input.value)
        if not role:
            await i.response.send_message(embed=err(f"No role `{self.role_input.value}` found."), ephemeral=True); return
        config  = await get_config(self.guild_id)
        entries = config.get("level_roles", [])
        entries = [e for e in entries if e["level"] != level]
        entries.append({"level": level, "role_id": role.id})
        entries.sort(key=lambda x: x["level"])
        await bot.db.update_config(self.guild_id, "level_roles", entries)
        await i.response.send_message(
            embed=ok(f"Level **{level}** → {role.mention} saved."),
            ephemeral=True,
        )


class RemoveLevelRoleModal(discord.ui.Modal, title="Remove Level Role"):
    level_input = discord.ui.TextInput(label="Level to remove", placeholder="e.g. 10", max_length=5)

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, i: discord.Interaction):
        if not self.level_input.value.strip().isdigit():
            await i.response.send_message(embed=err("Enter a valid level number."), ephemeral=True); return
        level   = int(self.level_input.value.strip())
        config  = await get_config(self.guild_id)
        entries = config.get("level_roles", [])
        new     = [e for e in entries if e["level"] != level]
        if len(new) == len(entries):
            await i.response.send_message(embed=err(f"No mapping found for level {level}."), ephemeral=True); return
        await bot.db.update_config(self.guild_id, "level_roles", new)
        await i.response.send_message(embed=ok(f"Removed level **{level}** mapping."), ephemeral=True)

# ═══════════════════════════════════════════════════════════════════════════════
#  STAFF ROLES SETUP VIEW  (v25)
# ═══════════════════════════════════════════════════════════════════════════════

def _staff_roles_embed(config: dict, guild: discord.Guild) -> discord.Embed:
    e = make_embed(C_PRIMARY)
    e.title = "🛡️ Staff Roles Configuration"
    lines = []
    for key, label, desc in _STAFF_ROLE_SLOTS:
        rid  = config.get(key)
        role = guild.get_role(rid) if rid else None
        val  = role.mention if role else "`not set`"
        lines.append(f"**{label}**\n{val}\n*{desc}*")
    e.description = "\n\n".join(lines)
    e.set_footer(text="Staff with assigned roles can use mod commands. Abuse = auto role strip.")
    return e


# ── Shared slot map used by both Set and Clear flows ─────────────────────────
_SLOT_MAP = {
    "manager":             "staff_owner_role_id",
    "community_manager":   "staff_community_manager_role_id",
    "community":           "staff_community_manager_role_id",
    "partnership_manager": "staff_partnership_manager_role_id",
    "partnership":         "staff_partnership_manager_role_id",
    "partner":             "staff_partnership_manager_role_id",
    "senior_mod":          "staff_senior_mod_role_id",
    "senior":              "staff_senior_mod_role_id",
    "mod":                 "staff_mod_role_id",
    "moderator":           "staff_mod_role_id",
    "trial_mod":           "staff_trial_mod_role_id",
    "trial":               "staff_trial_mod_role_id",
    "all":                 "staff_all_role_id",
    "inv_bypass":          "staff_inv_bypass_role_id",
    "bypass":              "staff_inv_bypass_role_id",
}

# Select options for all 8 role slots
_SLOT_SELECT_OPTIONS = [
    discord.SelectOption(label="👑 Manager",                     value="staff_owner_role_id",
                         description="Full access. Exempt from abuse-strip."),
    discord.SelectOption(label="👑 Community Manager",           value="staff_community_manager_role_id",
                         description="Full access — same as Manager."),
    discord.SelectOption(label="🤝 Partnership Manager",         value="staff_partnership_manager_role_id",
                         description="Above Senior Mod. Invite bypass."),
    discord.SelectOption(label="🔵 Senior Moderator",            value="staff_senior_mod_role_id",
                         description="Warn, kick, ban, mute (28d), purge (500), unban."),
    discord.SelectOption(label="🟢 Moderator",                   value="staff_mod_role_id",
                         description="Same as Senior Mod."),
    discord.SelectOption(label="🟡 Trial Moderator",             value="staff_trial_mod_role_id",
                         description="Warn, mute (max 1h), purge (max 30). No kick/ban."),
    discord.SelectOption(label="⚪ All Staff (no mod perms)",     value="staff_all_role_id",
                         description="Spam filter bypass only. Cosmetic."),
    discord.SelectOption(label="🔗 Invite Link Bypass",          value="staff_inv_bypass_role_id",
                         description="Can post invite links without automod deletion."),
]


class StaffRolesSetupView(discord.ui.View):
    """Main staff-roles panel — shown when admin clicks 🛡️ Staff Roles in setup."""
    def __init__(self, owner_id: int, guild_id: int):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.guild_id = guild_id

        # ── Set Role dropdown ─────────────────────────────────────────────────
        set_select = discord.ui.Select(
            placeholder="Set a role → pick a slot…",
            options=_SLOT_SELECT_OPTIONS,
            row=0,
        )
        set_select.callback = self._on_set_select
        self.add_item(set_select)

        # ── Clear Role dropdown ───────────────────────────────────────────────
        clear_options = [
            discord.SelectOption(
                label=o.label, value=o.value, description="Clear this slot",
            )
            for o in _SLOT_SELECT_OPTIONS
        ]
        clear_select = discord.ui.Select(
            placeholder="Clear a role → pick a slot…",
            options=clear_options,
            row=1,
        )
        clear_select.callback = self._on_clear_select
        self.add_item(clear_select)

    async def _on_set_select(self, i: discord.Interaction):
        """User picked a slot to SET — open a modal asking for the role name."""
        config_key = i.data["values"][0]
        label      = next((o.label for o in _SLOT_SELECT_OPTIONS if o.value == config_key), config_key)
        await i.response.send_modal(SetStaffRoleModal(self.guild_id, config_key, label))

    async def _on_clear_select(self, i: discord.Interaction):
        """User picked a slot to CLEAR — do it immediately and confirm."""
        config_key = i.data["values"][0]
        label      = next((o.label for o in _SLOT_SELECT_OPTIONS if o.value == config_key), config_key)
        await bot.db.update_config(self.guild_id, config_key, None)
        await i.response.send_message(
            embed=ok(f"**{label}** has been cleared."),
            ephemeral=True,
        )

    @discord.ui.button(label="📋 View All", style=discord.ButtonStyle.secondary, row=2)
    async def btn_view(self, i: discord.Interaction, b):
        config = await get_config(self.guild_id)
        await i.response.send_message(embed=_staff_roles_embed(config, i.guild), ephemeral=True)

    @discord.ui.button(label="❓ Permissions Guide", style=discord.ButtonStyle.secondary, row=2)
    async def btn_guide(self, i: discord.Interaction, b):
        guide = (
            "**👑 Manager / Community Manager**\n"
            "• `.warn` `.warns` `.clearwarns`\n"
            "• `.kick` `.ban` `.unban`\n"
            "• `.tempmute` (up to 28 days) `.unmute`\n"
            "• `.purge` (up to 500 messages)\n"
            "• `.slowmode` `.case` `.history`\n\n"
            "**🟢 Moderator**\n"
            "Same as Manager — identical permissions.\n\n"
            "**🟡 Trial Moderator**\n"
            "• `.warn` `.warns`\n"
            "• `.tempmute` (max **1 hour**)\n"
            "• `.purge` (max **30 messages**)\n"
            "• `.slowmode` (set only, cannot remove)\n"
            "❌ Cannot kick, ban, unban, clear warns, or mute > 1h\n\n"
            "**⚪ All Staff (no perms)**\n"
            "• Bypasses spam filter  • No mod commands\n\n"
            "**🔗 Invite Bypass**\n"
            "• May post invite links without automod deletion\n\n"
            "**🚨 Abuse System**\n"
            f"• {STAFF_ABUSE_WARN_THRESH}+ actions/{STAFF_ABUSE_WINDOW_SECS}s → public warning\n"
            f"• {STAFF_ABUSE_STRIP_THRESH}+ actions/{STAFF_ABUSE_WINDOW_SECS}s → all staff roles stripped\n"
            "• Managers & admins are exempt"
        )
        e = make_embed(C_INFO, guide)
        e.title = "🛡️ Staff Permissions Guide"
        await i.response.send_message(embed=e, ephemeral=True)


class SetStaffRoleModal(discord.ui.Modal, title="Set Staff Role"):
    """Modal shown after picking a slot — user types the role name."""
    role_input = discord.ui.TextInput(
        label="Role Name",
        placeholder="Type the exact role name, e.g. Senior Moderator",
        max_length=100,
    )

    def __init__(self, guild_id: int, config_key: str, slot_label: str):
        super().__init__(title=f"Set: {slot_label[:40]}")
        self.guild_id   = guild_id
        self.config_key = config_key
        self.slot_label = slot_label

    async def on_submit(self, i: discord.Interaction):
        role = resolve_role(i.guild, self.role_input.value.strip())
        if not role:
            await i.response.send_message(
                embed=err(f"No role named `{self.role_input.value}` found. Check spelling and try again."),
                ephemeral=True,
            ); return
        await bot.db.update_config(self.guild_id, self.config_key, role.id)
        await i.response.send_message(
            embed=ok(f"**{self.slot_label}** → {role.mention} ✅"),
            ephemeral=True,
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  FAKE PERMISSIONS SYSTEM  (v26)
#  DB-backed per-role bot permissions. Roles hold no real Discord perms.
#  Configure via .setup → 🔒 Fake Perms
# ═══════════════════════════════════════════════════════════════════════════════

FAKE_PERM_LABELS = {
    "administrator":     "All bot commands (full access)",
    "ban_members":       ".ban  .unban  .tempban",
    "kick_members":      ".kick",
    "moderate_members":  ".tempmute (.mute)  .unmute",
    "manage_messages":   ".purge  .warn  .warnings  .clearwarns",
    "manage_nicknames":  ".nick",
    "manage_roles":      ".role add/remove",
    "invite_bypass":     "Post invite links without automod deletion",
}

async def _get_fake_perms(guild_id: int, member: discord.Member) -> set:
    """Return union of all fake perms across this member's roles."""
    role_ids = [r.id for r in member.roles]
    if not role_ids: return set()
    docs = await bot.db.db["fake_perms"].find(
        {"guild_id": guild_id, "role_id": {"$in": role_ids}}
    ).to_list(50)
    perms: set = set()
    for doc in docs:
        perms.update(doc.get("perms", []))
    return perms

async def _has_fake_perm(guild_id: int, member: discord.Member, perm: str) -> bool:
    fp = await _get_fake_perms(guild_id, member)
    return "administrator" in fp or perm in fp

async def _require_mod_or_fake(ctx: commands.Context, config: dict,
                                min_level: str = "mod", fake_perm: str = None) -> bool:
    """Like _require_mod but also checks DB fake perms."""
    if ctx.author.id == bot.owner_id_int or ctx.author.guild_permissions.administrator:
        return True
    if min_level == "trial"  and _is_trial_or_above(ctx.author, config):  return True
    if min_level == "mod"    and _is_mod_or_above(ctx.author, config):    return True
    if min_level == "senior" and _is_senior_or_above(ctx.author, config): return True
    if fake_perm and ctx.guild:
        if await _has_fake_perm(ctx.guild.id, ctx.author, fake_perm):
            return True
    await ctx.send(embed=err(
        f"You need a **{'Trial Mod' if min_level=='trial' else 'Mod' if min_level=='mod' else 'Senior Mod'}** "
        f"role or the corresponding bot permission to use this command."
    ))
    return False


def _fake_perms_embed(guild: discord.Guild) -> discord.Embed:
    e = make_embed(C_PRIMARY)
    e.title = "🔒 Fake Permissions"
    e.description = (
        "Grant bot-level permissions to roles **without** giving them real Discord permissions.\n"
        "Staff can only moderate through the bot — no raw Discord access.\n\n"
        "**Valid permissions:**\n" +
        "\n".join(f"`{k}` — {v}" for k, v in FAKE_PERM_LABELS.items())
    )
    e.set_footer(text="Use the buttons to grant or revoke perms per role.")
    return e

async def _fake_perms_list_embed(guild: discord.Guild) -> discord.Embed:
    docs = await bot.db.db["fake_perms"].find({"guild_id": guild.id}).to_list(50)
    e = make_embed(C_PRIMARY)
    e.title = "🔒 Fake Perms — Current Config"
    if not docs:
        e.description = "No fake perms configured yet."
        return e
    lines = []
    for doc in docs:
        role = guild.get_role(doc["role_id"])
        rname = role.mention if role else f"`{doc['role_id']}`"
        perms_str = ", ".join(f"`{p}`" for p in doc.get("perms", []))
        lines.append(f"{rname} → {perms_str or '*none*'}")
    e.description = "\n".join(lines)
    return e


# ── Step 2: pick permissions (multi-select dropdown) ─────────────────────────

class FakePermsPickPermsSelect(discord.ui.Select):
    """Multi-select dropdown for choosing permissions."""
    def __init__(self, guild_id: int, role: discord.Role, action: str):
        self.guild_id = guild_id
        self.role     = role
        self.action   = action  # "grant" or "revoke"
        options = [
            discord.SelectOption(label=perm.replace("_", " ").title(),
                                 value=perm,
                                 description=desc[:100])
            for perm, desc in FAKE_PERM_LABELS.items()
        ]
        super().__init__(
            placeholder="Select one or more permissions…",
            min_values=1,
            max_values=len(options),
            options=options,
        )

    async def callback(self, i: discord.Interaction):
        chosen = self.values
        if self.action == "grant":
            await bot.db.db["fake_perms"].update_one(
                {"guild_id": self.guild_id, "role_id": self.role.id},
                {"$addToSet": {"perms": {"$each": chosen}}},
                upsert=True,
            )
            label = "Granted"
            color = C_SUCCESS
        else:
            await bot.db.db["fake_perms"].update_one(
                {"guild_id": self.guild_id, "role_id": self.role.id},
                {"$pullAll": {"perms": chosen}},
            )
            label = "Revoked"
            color = C_ERROR
        perm_str = ", ".join(f"`{p}`" for p in chosen)
        await i.response.edit_message(
            embed=make_embed(color, f"✅ **{label}** {perm_str} for {self.role.mention}."),
            view=None,
        )


class FakePermsPickPermsView(discord.ui.View):
    def __init__(self, guild_id: int, role: discord.Role, action: str):
        super().__init__(timeout=120)
        self.add_item(FakePermsPickPermsSelect(guild_id, role, action))


# ── Step 1: pick a role (role dropdown) ──────────────────────────────────────

class FakePermsPickRoleSelect(discord.ui.Select):
    """Role dropdown — shows up to 25 server roles."""
    def __init__(self, guild: discord.Guild, action: str):
        self.action = action
        # Skip @everyone and bot-managed roles; take first 25
        roles = [r for r in reversed(guild.roles) if not r.is_default() and not r.managed][:25]
        options = [
            discord.SelectOption(label=r.name[:100], value=str(r.id))
            for r in roles
        ] or [discord.SelectOption(label="(no roles found)", value="0")]
        super().__init__(placeholder="Select a role…", min_values=1, max_values=1, options=options)
        self.guild = guild

    async def callback(self, i: discord.Interaction):
        role_id = int(self.values[0])
        role    = self.guild.get_role(role_id)
        if not role:
            await i.response.send_message(embed=err("Role not found."), ephemeral=True); return
        action_label = "grant to" if self.action == "grant" else "revoke from"
        e = make_embed(C_PRIMARY,
            f"Now pick which permissions to **{self.action}** for {role.mention}.")
        e.title = f"{'➕ Grant' if self.action == 'grant' else '➖ Revoke'} — Step 2/2"
        await i.response.edit_message(
            embed=e,
            view=FakePermsPickPermsView(i.guild.id, role, self.action),
        )


class FakePermsPickRoleView(discord.ui.View):
    def __init__(self, guild: discord.Guild, action: str):
        super().__init__(timeout=120)
        self.add_item(FakePermsPickRoleSelect(guild, action))


# ── Main control panel ────────────────────────────────────────────────────────

class FakePermsSetupView(discord.ui.View):
    def __init__(self, owner_id: int, guild_id: int):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if not i.user.guild_permissions.administrator:
            await i.response.send_message(embed=err("Admins only."), ephemeral=True)
            return False
        return True

    @discord.ui.button(label="➕ Grant Perms", style=discord.ButtonStyle.success, row=0)
    async def btn_grant(self, i: discord.Interaction, b):
        e = make_embed(C_PRIMARY, "**Step 1 of 2** — Pick the role you want to **grant** permissions to.")
        e.title = "➕ Grant Fake Perms"
        await i.response.send_message(embed=e, view=FakePermsPickRoleView(i.guild, "grant"), ephemeral=True)

    @discord.ui.button(label="➖ Revoke Perms", style=discord.ButtonStyle.danger, row=0)
    async def btn_revoke(self, i: discord.Interaction, b):
        e = make_embed(C_WARNING, "**Step 1 of 2** — Pick the role you want to **revoke** permissions from.")
        e.title = "➖ Revoke Fake Perms"
        await i.response.send_message(embed=e, view=FakePermsPickRoleView(i.guild, "revoke"), ephemeral=True)

    @discord.ui.button(label="📋 View All", style=discord.ButtonStyle.secondary, row=0)
    async def btn_view(self, i: discord.Interaction, b):
        e = await _fake_perms_list_embed(i.guild)
        await i.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="🗑️ Reset All", style=discord.ButtonStyle.danger, row=1)
    async def btn_reset(self, i: discord.Interaction, b):
        await bot.db.db["fake_perms"].delete_many({"guild_id": self.guild_id})
        await i.response.send_message(embed=ok("✅ All fake perms cleared for this server."), ephemeral=True)

# ═══════════════════════════════════════════════════════════════════════════════
#  STAFF ROLE SYSTEM  (v25)
# ═══════════════════════════════════════════════════════════════════════════════

# ── Config key helpers ────────────────────────────────────────────────────────
def _sr(config: dict, key: str) -> Optional[int]:
    """Return a single staff role ID from config, or None."""
    return config.get(key) or None

def _has_staff_role(member: discord.Member, config: dict, *keys: str) -> bool:
    """True if member has any of the given staff role config keys."""
    member_role_ids = {r.id for r in member.roles}
    for k in keys:
        rid = config.get(k)
        if rid and rid in member_role_ids:
            return True
    return False

def _is_senior_or_above(member: discord.Member, config: dict) -> bool:
    return _has_staff_role(member, config,
        "staff_owner_role_id", "staff_community_manager_role_id",
        "staff_partnership_manager_role_id", "staff_senior_mod_role_id")

def _is_mod_or_above(member: discord.Member, config: dict) -> bool:
    return _has_staff_role(member, config,
        "staff_owner_role_id", "staff_community_manager_role_id",
        "staff_partnership_manager_role_id",
        "staff_senior_mod_role_id", "staff_mod_role_id")

def _is_trial_or_above(member: discord.Member, config: dict) -> bool:
    return _has_staff_role(member, config,
        "staff_owner_role_id", "staff_community_manager_role_id",
        "staff_partnership_manager_role_id",
        "staff_senior_mod_role_id", "staff_mod_role_id",
        "staff_trial_mod_role_id")

def _is_any_staff(member: discord.Member, config: dict) -> bool:
    return _has_staff_role(member, config,
        "staff_owner_role_id",
        "staff_senior_mod_role_id", "staff_mod_role_id",
        "staff_trial_mod_role_id", "staff_all_role_id",
        "staff_inv_bypass_role_id")

# ── Abuse tracking helper ─────────────────────────────────────────────────────
async def _check_staff_abuse(
    ctx: commands.Context,
    action: str,
    config: dict,
) -> bool:
    """
    Record this mod action. If the staff member is firing too fast:
      - First breach: warn publicly, return False (let action proceed).
      - Second breach: strip all staff roles, log to mod channel, return True (block action).
    Returns True if the action should be BLOCKED (abuse confirmed).
    Owner is always exempt.
    """
    if ctx.author.id == bot.owner_id_int or ctx.author.guild_permissions.administrator:
        return False
    if not _is_any_staff(ctx.author, config):
        return False

    guild_id = ctx.guild.id
    uid      = ctx.author.id
    now      = time.monotonic()
    window   = STAFF_ABUSE_WINDOW_SECS

    tracker  = _staff_abuse_tracker[guild_id][uid]
    # prune old entries
    tracker[:] = [t for t in tracker if now - t < window]
    tracker.append(now)
    count = len(tracker)

    if count < STAFF_ABUSE_WARN_THRESH:
        return False  # fine

    key = (guild_id, uid)
    last_warn = _staff_abuse_warned.get(key, 0)

    if count >= STAFF_ABUSE_STRIP_THRESH:
        # Strip every configured staff role they hold
        staff_keys = [
            "staff_senior_mod_role_id", "staff_mod_role_id",
            "staff_trial_mod_role_id", "staff_all_role_id",
            "staff_inv_bypass_role_id",
        ]
        stripped = []
        for k in staff_keys:
            rid = config.get(k)
            if rid:
                role = ctx.guild.get_role(rid)
                if role and role in ctx.author.roles:
                    try:
                        await ctx.author.remove_roles(role, reason="Staff abuse auto-strip")
                        stripped.append(role.name)
                    except Exception:
                        pass
        # Clear their tracker so they don't keep triggering
        _staff_abuse_tracker[guild_id].pop(uid, None)
        _staff_abuse_warned.pop(key, None)

        # Public callout
        stripped_str = ', '.join(stripped) or 'none found'
        e = make_embed(C_ERROR,
            f"🚨 **{ctx.author.mention}** has been automatically **stripped of their staff role(s)** "
            f"for abusing mod commands.\n"
            f"**Roles removed:** {stripped_str}\n"
            f"**Action that triggered strip:** `{action}` ({count} times in {window}s)\n\n"
            f"An admin must manually review and re-grant roles if appropriate.")
        e.title = "🚨 Staff Abuse Detected — Roles Stripped"
        try:
            await ctx.send(embed=e)
        except Exception:
            pass

        # Mod log
        stripped_str2 = ', '.join(stripped) or 'none'
        _log_mod_action(ctx.guild, config, "🚨 Staff Abuse — Roles Stripped",
            f"**Staff member:** {ctx.author.mention} (`{ctx.author.id}`)\n"
            f"**Roles stripped:** {stripped_str2}\n"
            f"**Trigger:** `{action}` — {count} actions in {window}s",
            C_ERROR)
        return True  # BLOCK the action

    # Warn threshold — warn once per 30s to avoid spam
    if now - last_warn > 30:
        _staff_abuse_warned[key] = now
        try:
            await ctx.send(embed=make_embed(C_WARNING,
                f"⚠️ {ctx.author.mention} — slow down. You're using `{action}` too fast. "
                f"Continue and your staff role will be automatically removed."),
                delete_after=10)
        except Exception:
            pass

    return False  # warn only, don't block yet


# ── Permission check helpers for staff-role-based access ─────────────────────
async def _require_mod(ctx: commands.Context, config: dict, min_level: str = "mod") -> bool:
    """
    Check if ctx.author has at least `min_level` staff role OR the matching Discord permission.
    min_level: "trial" | "mod" | "senior"
    Returns True if allowed, sends error and returns False otherwise.
    """
    # Owner / administrator always pass
    if ctx.author.id == bot.owner_id_int or ctx.author.guild_permissions.administrator:
        return True
    if min_level == "trial"  and _is_trial_or_above(ctx.author, config): return True
    if min_level == "mod"    and _is_mod_or_above(ctx.author, config):   return True
    if min_level == "senior" and _is_senior_or_above(ctx.author, config): return True
    await ctx.send(embed=err(
        f"You need a **{'Trial Mod' if min_level == 'trial' else 'Mod' if min_level == 'mod' else 'Senior Mod'}** "
        f"role or higher to use this command."
    ))
    return False

# ═══════════════════════════════════════════════════════════════════════════════
#  TICKET SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════

TICKET_CATEGORIES = [
    {"id": "join",  "label": "⚔️ Join LXTE", "desc": "Apply to join the LXTE Clan"},
    {"id": "staff", "label": "🛡️ Staff Application", "desc": "Apply for a staff position in LXTE"},
    {"id": "other", "label": "💬 Other",      "desc": "Anything else"},
]

class TicketCategorySelect(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        s = discord.ui.Select(
            placeholder="Pick a category…",
            custom_id="ticket:category_select",
            options=[discord.SelectOption(label=c["label"], value=c["id"], description=c["desc"]) for c in TICKET_CATEGORIES],
        )
        s.callback = self.on_select
        self.add_item(s)

    async def on_select(self, i: discord.Interaction):
        cid = i.data["values"][0]
        if cid == "join":
            await i.response.send_modal(JoinLXTEModal())
        elif cid == "staff":
            await _create_ticket(i, "staff", {})
        else:
            await i.response.send_modal(OtherTicketModal())

class JoinLXTEModal(discord.ui.Modal, title="Join LXTE Clan"):
    roblox   = discord.ui.TextInput(label="Roblox Username", placeholder="Your exact Roblox username", max_length=50)
    bw_stats = discord.ui.TextInput(label="BedWars Rank & Stats", style=discord.TextStyle.paragraph,
                                    placeholder="e.g. Diamond, 500 wins, 2.5 KD, favourite kit: Barbarian", max_length=400)
    async def on_submit(self, i):
        await _create_ticket(i, "join", {"roblox": self.roblox.value, "bw_stats": self.bw_stats.value})

# StaffTicketModal removed — staff apps now use in-channel Q&A (_staff_app_sessions)

class OtherTicketModal(discord.ui.Modal, title="Open a Ticket"):
    reason = discord.ui.TextInput(label="What do you need help with?", style=discord.TextStyle.paragraph, max_length=500)
    async def on_submit(self, i):
        await _create_ticket(i, "other", {"reason": self.reason.value})

# ── Staff app reviewer check ─────────────────────────────────────────────────
async def _is_app_reviewer(user: discord.Member, guild: discord.Guild) -> bool:
    """True if user is the bot owner (env), hard-coded staff-app owner ID, OR has the configured reviewer role."""
    if user.id == getattr(bot, 'owner_id_int', 0):
        return True
    config = await get_config(guild.id)
    role_id = config.get("staff_app_reviewer_role_id")
    if role_id:
        role = guild.get_role(role_id)
        if role and role in user.roles:
            return True
    return False


class StaffAppDenyModal(discord.ui.Modal, title="Deny Application"):
    reason = discord.ui.TextInput(
        label="Reason for denial",
        style=discord.TextStyle.paragraph,
        max_length=500,
        placeholder="Tell them why they were denied…",
    )

    def __init__(self, applicant_id: int, channel_id: int, review_msg: discord.Message):
        super().__init__()
        self.applicant_id = applicant_id
        self.channel_id   = channel_id
        self.review_msg   = review_msg

    async def on_submit(self, i: discord.Interaction):
        member = i.guild.get_member(self.applicant_id)
        # DM the applicant
        if member:
            try:
                dm_e = make_embed(C_ERROR,
                    "Unfortunately your staff application for "
                    f"**{i.guild.name}** has been **denied**.\n\n"
                    f"**Reason:** {self.reason.value}\n\n"
                    "You're welcome to apply again in the future. Keep improving! 💪"
                )
                dm_e.title = "❌ Staff Application Denied"
                dm_e.set_footer(text="LXTE\'s AI — Staff Applications")
                await member.send(embed=dm_e)
            except discord.Forbidden:
                pass
        # Save to DB
        await bot.db.tickets.update_one(
            {"channel_id": self.channel_id},
            {"$set": {
                "app_status":   "denied",
                "deny_reason":  self.reason.value,
                "reviewed_by":  i.user.id,
                "reviewed_at":  datetime.now(timezone.utc),
            }},
        )
        # Stamp the embed
        old_embeds = self.review_msg.embeds
        if old_embeds:
            stamped = old_embeds[0].copy()
            stamped.colour = discord.Color.red()
            stamped.title  = (stamped.title or "") + "  —  ❌ DENIED"
            stamped.add_field(name="Denial Reason", value=self.reason.value, inline=False)
            stamped.add_field(name="Reviewed by",   value=i.user.mention,   inline=True)
            try: await self.review_msg.edit(embed=stamped, view=None)
            except Exception: pass
        ping = member.mention if member else f"<@{self.applicant_id}>"
        await i.response.send_message(embed=ok(f"Application denied. {ping} has been DM\'d."), ephemeral=True)


class StaffAppReviewView(discord.ui.View):
    def __init__(self, applicant_id: int, ticket_channel_id: int):
        super().__init__(timeout=None)
        self.applicant_id      = applicant_id
        self.ticket_channel_id = ticket_channel_id

    @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.success)
    async def btn_accept(self, i: discord.Interaction, b: discord.ui.Button):
        if not await _is_app_reviewer(i.user, i.guild):
            await i.response.send_message(embed=err("Only the owner or app reviewer can do this."), ephemeral=True)
            return
        member = i.guild.get_member(self.applicant_id)
        # DM the applicant
        if member:
            try:
                dm_e = make_embed(C_SUCCESS,
                    f"🎉 Congratulations! Your staff application for **{i.guild.name}** has been **accepted**!\n"
                    "Welcome to the team — a staff member will reach out to you shortly. 🛡️"
                )
                dm_e.title = "✅ Staff Application Accepted"
                dm_e.set_footer(text="LXTE\'s AI — Staff Applications")
                await member.send(embed=dm_e)
            except discord.Forbidden:
                pass
        # Save to DB
        await bot.db.tickets.update_one(
            {"channel_id": self.ticket_channel_id},
            {"$set": {
                "app_status":  "accepted",
                "reviewed_by": i.user.id,
                "reviewed_at": datetime.now(timezone.utc),
            }},
        )
        # Stamp the embed green
        if i.message.embeds:
            stamped = i.message.embeds[0].copy()
            stamped.colour = discord.Color.green()
            stamped.title  = (stamped.title or "") + "  —  ✅ ACCEPTED"
            stamped.add_field(name="Reviewed by", value=i.user.mention, inline=True)
            try: await i.message.edit(embed=stamped, view=None)
            except Exception: pass
        ping = member.mention if member else f"<@{self.applicant_id}>"
        await i.response.send_message(embed=ok(f"Application accepted. {ping} has been DM\'d — congrats to them!"), ephemeral=True)

    @discord.ui.button(label="❌ Deny", style=discord.ButtonStyle.danger)
    async def btn_deny(self, i: discord.Interaction, b: discord.ui.Button):
        if not await _is_app_reviewer(i.user, i.guild):
            await i.response.send_message(embed=err("Only the owner or app reviewer can do this."), ephemeral=True)
            return
        await i.response.send_modal(
            StaffAppDenyModal(self.applicant_id, self.ticket_channel_id, i.message)
        )


async def _send_staff_app_question(channel: discord.TextChannel, session: dict):
    q_idx    = session["question_index"]
    total    = len(STAFF_APP_QUESTIONS)
    question = STAFF_APP_QUESTIONS[q_idx]
    e = make_embed(C_INFO, f"**{question}**")
    e.title = f"Question {q_idx + 1} of {total}"
    e.set_footer(text="Type your answer in chat  •  LXTE Staff Application")
    await channel.send(embed=e)

async def _finish_staff_app(channel: discord.TextChannel, session: dict, guild: discord.Guild):
    user = guild.get_member(session["user_id"])
    name = user.display_name if user else f"<@{session['user_id']}>"

    # ── Summary embed ──────────────────────────────────────────────────────
    e = make_embed(C_GOLD)
    e.title = f"🛡️ Staff Application — {name}"
    e.description = (
        f"**Applicant:** {user.mention if user else '<@' + str(session['user_id']) + '>'} "
        f"(`{str(user)}` | `{session['user_id']}`)"
    )
    if user and user.display_avatar:
        e.set_thumbnail(url=user.display_avatar.url)
    for idx, (q, a) in enumerate(zip(STAFF_APP_QUESTIONS, session["answers"]), 1):
        e.add_field(name=f"Q{idx}. {q}", value=a or "*(no answer)*", inline=False)
    e.set_footer(text="LXTE's AI — Staff Applications")

    # ── Save answers + status to DB ───────────────────────────────────────
    await bot.db.tickets.update_one(
        {"channel_id": channel.id},
        {"$set": {
            "app_answers": [{"q": q, "a": a} for q, a in zip(STAFF_APP_QUESTIONS, session["answers"])],
            "app_status":  "pending",
            "category":    "staff",
        }},
    )

    # ── Tell applicant they're done ────────────────────────────────────────
    done = make_embed(C_SUCCESS,
        f"✅ {user.mention if user else 'Applicant'}, your application is complete!\n"
        "The owner has been notified and will review your answers. **Good luck!**"
    )
    done.title = "Application Submitted!"
    done.set_footer(text="LXTE's AI — Staff Applications")
    await channel.send(embed=done)

    # ── Ping owner with summary + accept/deny buttons ──────────────────────
    view = StaffAppReviewView(applicant_id=session["user_id"], ticket_channel_id=channel.id)
    await channel.send(content=f"<@{getattr(bot, 'owner_id_int', 0)}>", embed=e, view=view)

async def _create_ticket(i: discord.Interaction, cid: str, answers: dict):
    guild  = i.guild
    user   = i.user
    config = await get_config(guild.id)
    if await bot.db.count_open_tickets(guild.id, user.id) >= 1:
        await i.response.send_message(embed=err("You already have an open ticket. Close it first."), ephemeral=True); return
    ticket_num = config.get("ticket_counter", 0) + 1
    await bot.db.update_config(guild.id, "ticket_counter", ticket_num)
    cat_id   = config.get("ticket_category_id")
    category = guild.get_channel(cat_id) if cat_id else None

    # IMPROVED: support multiple staff roles
    staff_role_ids = config.get("ticket_staff_role_ids", [])
    # backwards compat with old single role
    old_single = config.get("ticket_staff_role_id")
    if old_single and old_single not in staff_role_ids:
        staff_role_ids = [old_single] + staff_role_ids

    staff_roles = [guild.get_role(rid) for rid in staff_role_ids if guild.get_role(rid)]

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user:               discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me:           discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
    }
    for sr in staff_roles:
        overwrites[sr] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    cat_label = next((c["label"] for c in TICKET_CATEGORIES if c["id"] == cid), cid)
    try:
        channel = await guild.create_text_channel(
            name=f"{cid}-{ticket_num:04d}",
            category=category,
            overwrites=overwrites,
            topic=f"Ticket #{ticket_num:04d} | {cat_label} | {user.name} ({user.id})",
            reason=f"Ticket by {user}",
        )
    except discord.Forbidden:
        await i.response.send_message(embed=err("I can't create channels. Check my permissions."), ephemeral=True); return

    await bot.db.save_ticket(guild.id, channel.id, user.id, ticket_num)
    await bot.db.tickets.update_one(
        {"guild_id": guild.id, "channel_id": channel.id},
        {"$set": {"category": cid, "last_activity": datetime.now(timezone.utc)}},
    )
    e = make_embed(C_PRIMARY)
    if cid == "join":
        e.title       = f"⚔️ Join Application #{ticket_num:04d}"
        e.description = f"Hey {user.mention}! Your application is in. We'll review it and get back to you here."
        e.add_field(name="🎮 Roblox Username", value=answers.get("roblox", "?"),   inline=True)
        e.add_field(name="⚔️ BedWars Stats",   value=answers.get("bw_stats", "?"), inline=False)
    elif cid == "staff":
        # ── Register Q&A session and send intro ───────────────────────────────
        _staff_app_sessions[channel.id] = {
            "user_id":        user.id,
            "question_index": 0,
            "answers":        [],
        }
        intro = make_embed(C_PRIMARY)
        intro.title = "🛡️ LXTE Staff Application"
        intro.description = (
            f"Welcome {user.mention}! You've opened a **staff application**.\n\n"
            "**Here's how this works:**\n"
            "• The bot will ask you **17 questions** one by one\n"
            "• Answer **each question in a single message** directly in this channel\n"
            "• Be **honest and detailed** — low effort answers will get you denied\n"
            "• Make sure your answers **make sense and count** — no copy-paste spam\n"
            "• Once all questions are answered, your application is sent straight to the owner\n\n"
            "**Take your time, be yourself, and good luck! First question below ↓**"
        )
        intro.set_footer(text="LXTE's AI — Staff Applications")
        await channel.send(content=user.mention, embed=intro)
        await asyncio.sleep(1)
        await _send_staff_app_question(channel, _staff_app_sessions[channel.id])
        # Send ticket control panel so staff can claim / close / transcript
        ctrl_e = make_embed(C_PRIMARY)
        ctrl_e.title       = "🎫 Ticket Controls"
        ctrl_e.description = "Staff: use the buttons below to manage this ticket."
        ctrl_e.set_footer(text="LXTE's AI — Ticket System")
        await channel.send(embed=ctrl_e, view=TicketControlView())
        await i.response.send_message(embed=ok(f"Application ticket opened: {channel.mention}"), ephemeral=True)
        return
    else:
        e.title       = f"💬 Ticket #{ticket_num:04d}"
        e.description = f"Hey {user.mention}! We'll be with you shortly."
        e.add_field(name="Reason", value=answers.get("reason", "No reason given."), inline=False)
    e.set_footer(text="LXTE's AI — Ticket System")

    staff_pings = " ".join(sr.mention for sr in staff_roles)
    await channel.send(
        content=f"{user.mention}{(' ' + staff_pings) if staff_pings else ''}",
        embed=e,
        view=TicketControlView(),
    )
    await i.response.send_message(embed=ok(f"Ticket opened: {channel.mention}"), ephemeral=True)

class TicketOpenView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="🎫 Open a Ticket", style=discord.ButtonStyle.primary, custom_id="ticket:open")
    async def btn_open(self, i: discord.Interaction, b):
        if await bot.db.count_open_tickets(i.guild.id, i.user.id) >= 1:
            await i.response.send_message(embed=err("You already have an open ticket."), ephemeral=True); return
        await i.response.send_message(embed=make_embed(C_PRIMARY, "Select a category:"), view=TicketCategorySelect(), ephemeral=True)


# ── Shared ticket helpers ─────────────────────────────────────────────────────

async def _build_transcript_html(channel: discord.TextChannel, ticket_data: dict, closer: discord.Member) -> tuple[str, int]:
    """Build an HTML transcript string. Returns (html, message_count)."""
    msgs = [m async for m in channel.history(limit=500, oldest_first=True) if not m.author.bot]
    tid  = ticket_data.get("ticket_id", "?")
    guild = channel.guild
    opener = guild.get_member(ticket_data.get("user_id", 0))
    rows_html = []
    for m in msgs:
        ts_str  = m.created_at.strftime("%Y-%m-%d %H:%M UTC")
        name    = m.author.display_name.replace("<", "&lt;").replace(">", "&gt;")
        content = m.content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
        rows_html.append(
            f'<div class="msg"><span class="ts">[{ts_str}]</span>'
            f'<span class="author">{name}</span>'
            f'<span class="content">{content}</span></div>'
        )
    tid_fmt = f"{tid:04d}" if isinstance(tid, int) else str(tid)
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Ticket #{tid_fmt} Transcript</title>
<style>
body{{background:#1e1f22;color:#dcddde;font-family:monospace;font-size:14px;margin:20px}}
h2{{color:#5865F2;border-bottom:1px solid #3f4147;padding-bottom:8px}}
.meta{{color:#9ba3af;margin-bottom:20px;font-size:13px}}
.msg{{padding:4px 0;border-bottom:1px solid #2b2d3030}}
.ts{{color:#5b6375;margin-right:8px}}
.author{{color:#5865F2;font-weight:bold;margin-right:8px}}
.content{{color:#dcddde}}
</style></head><body>
<h2>🎫 Ticket #{tid_fmt} Transcript</h2>
<div class="meta">
Opened by: <b>{opener.display_name if opener else '?'}</b> &nbsp;|&nbsp;
Closed by: <b>{closer.display_name}</b> &nbsp;|&nbsp;
Messages: <b>{len(msgs)}</b> &nbsp;|&nbsp;
Closed at: <b>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</b>
</div>
{''.join(rows_html)}
</body></html>"""
    return html, len(msgs)


async def _send_transcript_to_log(channel: discord.TextChannel, ticket_data: dict, closer: discord.Member):
    """Generate transcript HTML and post it to the ticket log channel."""
    guild     = channel.guild
    config    = await get_config(guild.id)
    log_ch_id = config.get("ticket_log_channel_id")
    if not log_ch_id: return
    log_ch = guild.get_channel(log_ch_id)
    if not log_ch: return
    try:
        html, msg_count = await _build_transcript_html(channel, ticket_data, closer)
        opener  = guild.get_member(ticket_data.get("user_id", 0))
        tid     = ticket_data.get("ticket_id", "?")
        tid_fmt = f"{tid:04d}" if isinstance(tid, int) else str(tid)
        te = make_embed(C_INFO,
            f"**Opened by:** {opener.mention if opener else '?'}\n"
            f"**Closed by:** {closer.mention}\n"
            f"**Messages:** {msg_count}")
        te.title = f"📋 Ticket #{tid_fmt} Closed"
        claimer_id = ticket_data.get("claimed_by")
        if claimer_id:
            claimer = guild.get_member(claimer_id)
            te.add_field(name="Claimed by", value=claimer.mention if claimer else f"`{claimer_id}`", inline=True)
        await log_ch.send(
            embed=te,
            file=discord.File(fp=io.BytesIO(html.encode()), filename=f"ticket-{tid_fmt}-transcript.html"),
        )
    except Exception as exc:
        logger.warning("transcript log error: %s", exc)


async def _do_close_ticket(i: discord.Interaction):
    """Shared close logic — used by button and .close command."""
    channel     = i.channel
    ticket_data = await bot.db.get_ticket(channel.id)
    if not ticket_data:
        await i.response.send_message(embed=err("This isn't a ticket channel."), ephemeral=True); return
    is_staff = (i.user.guild_permissions.manage_channels
                or i.user.id == ticket_data.get("user_id")
                or i.user.id == getattr(bot, 'owner_id_int', 0))
    if not is_staff:
        await i.response.send_message(embed=err("Only staff or the ticket opener can close this."), ephemeral=True); return
    await i.response.send_message(embed=make_embed(C_WARNING, "🔒 Closing in 5 seconds…"))
    await bot.db.close_ticket(channel.id)
    _staff_app_sessions.pop(channel.id, None)
    await _send_transcript_to_log(channel, ticket_data, i.user)
    await asyncio.sleep(5)
    try: await channel.delete(reason=f"Ticket closed by {i.user}")
    except Exception: pass


class TicketControlView(discord.ui.View):
    """Full ticket control panel — Claim / Close / Transcript."""
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="📌 Claim", style=discord.ButtonStyle.secondary, custom_id="ticket:claim")
    async def btn_claim(self, i: discord.Interaction, b: discord.ui.Button):
        channel     = i.channel
        ticket_data = await bot.db.get_ticket(channel.id)
        if not ticket_data:
            await i.response.send_message(embed=err("This isn't a ticket channel."), ephemeral=True); return
        is_staff = i.user.guild_permissions.manage_channels or i.user.id == getattr(bot, 'owner_id_int', 0)
        if not is_staff:
            await i.response.send_message(embed=err("Only staff can claim tickets."), ephemeral=True); return
        already = ticket_data.get("claimed_by")
        if already and already != i.user.id:
            claimer = i.guild.get_member(already)
            name    = claimer.display_name if claimer else f"<@{already}>"
            await i.response.send_message(embed=err(f"Already claimed by **{name}**."), ephemeral=True); return
        await bot.db.tickets.update_one(
            {"channel_id": channel.id},
            {"$set": {"claimed_by": i.user.id}},
        )
        e = make_embed(C_SUCCESS, f"📌 {i.user.mention} has claimed this ticket.")
        e.title = "Ticket Claimed"
        await i.response.send_message(embed=e)

    @discord.ui.button(label="🔒 Close", style=discord.ButtonStyle.danger, custom_id="ticket:close_v2")
    async def btn_close(self, i: discord.Interaction, b: discord.ui.Button):
        await _do_close_ticket(i)

    @discord.ui.button(label="📄 Transcript", style=discord.ButtonStyle.secondary, custom_id="ticket:transcript")
    async def btn_transcript(self, i: discord.Interaction, b: discord.ui.Button):
        channel     = i.channel
        ticket_data = await bot.db.get_ticket(channel.id)
        if not ticket_data:
            await i.response.send_message(embed=err("This isn't a ticket channel."), ephemeral=True); return
        is_staff = i.user.guild_permissions.manage_channels or i.user.id == getattr(bot, 'owner_id_int', 0)
        if not is_staff:
            await i.response.send_message(embed=err("Only staff can pull transcripts."), ephemeral=True); return
        await i.response.defer(ephemeral=True)
        try:
            html, msg_count = await _build_transcript_html(channel, ticket_data, i.user)
            tid     = ticket_data.get("ticket_id", "?")
            tid_fmt = f"{tid:04d}" if isinstance(tid, int) else str(tid)
            await i.followup.send(
                embed=make_embed(C_INFO, f"📄 Transcript — **{msg_count}** messages"),
                file=discord.File(fp=io.BytesIO(html.encode()), filename=f"ticket-{tid_fmt}-transcript.html"),
                ephemeral=True,
            )
        except Exception as exc:
            await i.followup.send(embed=err(f"Failed to generate transcript: {exc}"), ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  ROLE MENU VIEW
# ═══════════════════════════════════════════════════════════════════════════════

class RoleMenuView(discord.ui.View):
    def __init__(self, menu_id: str, roles: list[dict]):
        super().__init__(timeout=None)
        for entry in roles[:25]:
            btn = discord.ui.Button(
                label=entry.get("label", entry.get("name", "Role"))[:80],
                emoji=entry.get("emoji"),
                style=discord.ButtonStyle.secondary,
                custom_id=f"rolemenu:{menu_id}:{entry['role_id']}",
            )
            btn.callback = self._make_cb(entry["role_id"])
            self.add_item(btn)

    def _make_cb(self, role_id: int):
        async def cb(i: discord.Interaction):
            member = i.guild.get_member(i.user.id)
            role   = i.guild.get_role(role_id)
            if not role: await i.response.send_message("That role no longer exists.", ephemeral=True); return
            if role in member.roles:
                await member.remove_roles(role, reason="Role menu")
                await i.response.send_message(embed=make_embed(C_WARNING, f"Removed **{role.name}**."), ephemeral=True)
            else:
                await member.add_roles(role, reason="Role menu")
                await i.response.send_message(embed=ok(f"Added **{role.name}**."), ephemeral=True)
        return cb


# ═══════════════════════════════════════════════════════════════════════════════
#  SERVER UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

async def update_member_count(guild: discord.Guild):
    ch = guild.get_channel(MEMBER_COUNT_CHANNEL_ID)
    if not ch: return
    name = MEMBER_COUNT_FORMAT.format(count=guild.member_count)
    if ch.name != name:
        try: await ch.edit(name=name, reason="Member count update")
        except Exception as e: logger.warning("Member count: %s", e)

WELCOME_CHANNEL_ID = 1507918341551952026  # #general — always send here regardless of config

async def send_welcome(member: discord.Member, config: dict):
    # ── Hardcoded general channel: ghost ping then short welcome embed ─────────
    general_ch = member.guild.get_channel(WELCOME_CHANNEL_ID)
    if general_ch:
        try:
            # Ghost ping — send then immediately delete so they get the notification
            ping_msg = await general_ch.send(member.mention)
            await ping_msg.delete()
        except Exception: pass
        try:
            count = member.guild.member_count
            e = discord.Embed(
                description=(
                    f"**{member.display_name}** just joined 🎉\n"
                    f"You're member **#{count}** — welcome to LXTE! 🌸\n"
                    f"Check out <#1509420949194145803> and have fun."
                ),
                color=C_PRIMARY,
                timestamp=datetime.now(timezone.utc),
            )
            e.set_thumbnail(url=member.display_avatar.url)
            await general_ch.send(embed=e)
        except Exception as exc:
            logger.warning("Welcome (general): %s", exc)

    # ── Configurable welcome channel (set via .setup) ──────────────────────────
    ch_id = config.get("welcome_channel_id")
    if ch_id and ch_id != WELCOME_CHANNEL_ID:
        ch = member.guild.get_channel(ch_id)
        if ch:
            title = config.get("welcome_title", WELCOME_TITLE)
            msg   = config.get("welcome_message", WELCOME_MSG).format(
                user=member.mention, server=member.guild.name, count=member.guild.member_count
            )
            e = discord.Embed(title=title, description=msg, color=C_PRIMARY, timestamp=datetime.now(timezone.utc))
            if member.guild.icon: e.set_thumbnail(url=member.guild.icon.url)
            e.set_footer(text=f"Member #{member.guild.member_count}  •  LXTE's AI")
            try: await ch.send(content=member.mention, embed=e)
            except Exception as exc: logger.warning("Welcome: %s", exc)

    if config.get("welcome_dm_enabled"):
        try: await member.send(f"Welcome to **{member.guild.name}**! Check out the rules and enjoy your stay.")
        except Exception: pass

# ─── Staff bypass helper ─────────────────────────────────────────────────────

def _is_staff_bypass(member: discord.Member, config: dict) -> bool:
    """True if this member holds any configured staff-bypass role.
    Anti-nuke and anti-raid are NEVER bypassed regardless of this flag.
    Also honours the new staff role system keys."""
    if member is None:
        return False
    member_role_ids = {r.id for r in member.roles}
    # Legacy bypass role list
    bypass_ids = set(config.get("staff_bypass_role_ids", []))
    if bypass_ids & member_role_ids:
        return True
    # New staff role system — any configured staff role = bypass spam filter
    for key in ("staff_owner_role_id", "staff_community_manager_role_id",
                "staff_partnership_manager_role_id", "staff_senior_mod_role_id",
                "staff_mod_role_id", "staff_trial_mod_role_id",
                "staff_all_role_id", "staff_inv_bypass_role_id"):
        rid = config.get(key)
        if rid and rid in member_role_ids:
            return True
    return False


def _has_invite_bypass(member: discord.Member, config: dict) -> bool:
    """True if this member has the invite-link bypass role (legacy or staff_inv_bypass_role_id)."""
    if member is None:
        return False
    member_role_ids = {r.id for r in member.roles}
    # Legacy key
    role_id = config.get("invite_bypass_role_id")
    if role_id and role_id in member_role_ids:
        return True
    # New staff system key
    role_id2 = config.get("staff_inv_bypass_role_id")
    if role_id2 and role_id2 in member_role_ids:
        return True
    return False


async def _automod_phishing(message: discord.Message, config: dict) -> bool:
    """Block malicious links, invites, and bare links. Returns True if message was actioned."""
    content = message.content
    member  = message.guild.get_member(message.author.id) if message.guild else None
    is_staff = _is_staff_bypass(member, config)

    # ── Malicious link check — NO bypass for anyone, including staff ──────────
    for pat in MALICIOUS_RE:
        if pat.search(content):
            try: await message.delete()
            except Exception: pass
            try: await message.channel.send(embed=err(f"{message.author.mention} flagged as potentially malicious."), delete_after=8)
            except Exception: pass
            return True

    # ── Invite link check — bypassed by: staff bypass roles OR invite-bypass role
    can_invite = is_staff or _has_invite_bypass(member, config)
    if config.get("automod_no_invites", True) and INVITE_RE.search(content) and not can_invite:
        try: await message.delete()
        except Exception: pass
        try: await message.channel.send(embed=err(f"{message.author.mention} invite links aren't allowed."), delete_after=6)
        except Exception: pass
        return True

    # ── General link check — staff bypass ────────────────────────────────────
    if config.get("automod_no_links", True) and not is_staff:
        bad = [u for u in LINK_RE.findall(content) if not GIF_RE.match(u)]
        if bad:
            try: await message.delete()
            except Exception: pass
            try: await message.channel.send(embed=err(f"{message.author.mention} links aren't allowed here. GIFs are fine 🙂"), delete_after=6)
            except Exception: pass
            return True
    return False


async def _automod_spam(message: discord.Message, config: dict) -> bool:
    """Rate-spam and duplicate-spam detection. Returns True if message was actioned (deleted/muted)."""
    if not config.get("antispam_enabled", True): return False
    uid     = message.author.id
    now     = time.monotonic()
    content = message.content
    member  = message.guild.get_member(uid) if message.guild else None

    # ── Staff path: soft warn, no auto-mute unless truly excessive ────────────
    if _is_staff_bypass(member, config):
        _staff_spam_tracker[uid] = [t for t in _staff_spam_tracker[uid] if now - t < STAFF_SPAM_WINDOW_SECS]
        _staff_spam_tracker[uid].append(now)
        count = len(_staff_spam_tracker[uid])
        if count >= STAFF_SPAM_MUTE_THRESH:
            # BAD BAD — mute them
            _staff_spam_tracker[uid].clear()
            try: await message.delete()
            except Exception: pass
            if member:
                try: await member.timeout(timedelta(minutes=10), reason="Staff anti-spam: extreme flooding")
                except Exception: pass
            try: await message.channel.send(embed=err(f"{message.author.mention} even staff have limits — muted 10 minutes."), delete_after=10)
            except Exception: pass
            return True
        elif count >= STAFF_SPAM_WARN_THRESH:
            last_warn = _staff_spam_warned.get(uid, 0)
            if now - last_warn > 15:   # only warn once per 15s so it's not annoying
                _staff_spam_warned[uid] = now
                try: await message.channel.send(embed=make_embed(C_WARNING, f"⚠️ {message.author.mention} ease up a bit yeah? Type slower 🙏"), delete_after=8)
                except Exception: pass
        return False   # staff message is never deleted by spam detection

    # ── Regular member spam path ───────────────────────────────────────────────
    _spam_tracker[uid] = [t for t in _spam_tracker[uid] if now - t < SPAM_WINDOW_SECS]
    _spam_tracker[uid].append(now)
    if len(_spam_tracker[uid]) >= SPAM_MSG_THRESH:
        _spam_tracker[uid].clear()
        try: await message.delete()
        except Exception: pass
        if member:
            try: await member.timeout(timedelta(minutes=5), reason="Anti-spam: message flood")
            except Exception: pass
        try: await message.channel.send(embed=err(f"{message.author.mention} slow down! Auto-muted for 5 minutes."), delete_after=8)
        except Exception: pass
        _log_automod(message.guild, config, f"🚫 **Anti-Spam** — {message.author.mention} flooded messages in {SPAM_WINDOW_SECS}s", C_ERROR)
        ch = message.channel
        last_sm = _slowmode_active.get(ch.id, 0)
        if now - last_sm > SLOWMODE_LIFT_SECS:
            _slowmode_active[ch.id] = now
            try:
                await ch.edit(slowmode_delay=SLOWMODE_DELAY_SECS, reason="Anti-spam: auto-slowmode")
                await ch.send(
                    embed=make_embed(C_WARNING, f"⏱️ Slowmode enabled ({SLOWMODE_DELAY_SECS}s) due to spam. Lifting in {SLOWMODE_LIFT_SECS}s."),
                    delete_after=SLOWMODE_LIFT_SECS,
                )
                async def _lift(channel, delay):
                    await asyncio.sleep(delay)
                    try: await channel.edit(slowmode_delay=0, reason="Anti-spam: slowmode lifted")
                    except Exception: pass
                    _slowmode_active.pop(channel.id, None)
                asyncio.create_task(_lift(ch, SLOWMODE_LIFT_SECS))
            except discord.Forbidden:
                pass
        return True
    norm = _normalise_msg(content[:150])
    if len(norm) < SPAM_DUP_MIN_LEN:
        return False
    recent_content = _dup_tracker[uid]
    recent_content.append(norm)
    if len(recent_content) > 10: _dup_tracker[uid] = recent_content[-10:]
    if recent_content.count(norm) >= SPAM_DUP_THRESH:
        _dup_tracker[uid].clear()
        try: await message.delete()
        except Exception: pass
        try: await message.channel.send(embed=err(f"{message.author.mention} stop copy-pasting the same message."), delete_after=6)
        except Exception: pass
        return True
    return False


async def _automod_mentions(message: discord.Message, config: dict) -> bool:
    """Mass-mention detection. Returns True if actioned."""
    if not config.get("anti_mass_mention_enabled", True): return False
    unique_mentions = len({u.id for u in message.mentions if not u.bot})
    if unique_mentions >= MASS_MENTION_THRESH:
        try: await message.delete()
        except Exception: pass
        try: await message.channel.send(embed=err(f"{message.author.mention} don't mass-mention users."), delete_after=6)
        except Exception: pass
        _log_automod(message.guild, config, f"📢 **Mass Mention** — {message.author.mention} pinged {unique_mentions} users", C_WARNING)
        return True
    return False


async def _automod_caps(message: discord.Message, config: dict) -> bool:
    """All-caps detection. Returns True if actioned."""
    if not config.get("anti_caps_enabled", False): return False
    alpha = [c for c in message.content if c.isalpha()]
    if len(alpha) >= CAPS_MIN_LENGTH and sum(1 for c in alpha if c.isupper()) / len(alpha) >= CAPS_THRESHOLD:
        try: await message.delete()
        except Exception: pass
        try: await message.channel.send(embed=err(f"{message.author.mention} please don't shout (too many caps)."), delete_after=6)
        except Exception: pass
        return True
    return False


async def _automod_emoji(message: discord.Message, config: dict) -> bool:
    """Emoji-spam detection. Returns True if actioned."""
    if not config.get("anti_emoji_spam_enabled", False): return False
    if len(EMOJI_RE.findall(message.content)) >= EMOJI_THRESH:
        try: await message.delete()
        except Exception: pass
        try: await message.channel.send(embed=err(f"{message.author.mention} too many emojis in one message."), delete_after=6)
        except Exception: pass
        return True
    return False


async def run_automod(message: discord.Message, config: dict, owner_id: int = 0) -> bool:
    """Run all automod sub-checks. Returns True if message was hard-actioned (deleted/muted)."""
    if not message.guild or not config.get("automod_enabled", True): return False
    if _is_owner(message.author): return False  # owner invisible to ALL automod
    member   = message.guild.get_member(message.author.id)
    is_admin = bool(member and member.guild_permissions.administrator)
    is_staff = _is_staff_bypass(member, config)

    # Admins skip all remaining checks
    if is_admin: return False

    # ── Staff path: only malicious-link check + soft spam warn (no hard actions)
    if is_staff:
        await _automod_spam(message, config)      # warns only, won't return True unless extreme
        await _automod_phishing(message, config)  # malicious links still block staff
        return False

    # ── Regular member: full suite
    return (
        await _automod_phishing(message, config) or
        await _automod_spam(message, config) or
        await _automod_mentions(message, config) or
        await _automod_caps(message, config) or
        await _automod_emoji(message, config)
    )

def _log_automod(guild: discord.Guild, config: dict, description: str, color: int = C_WARNING):
    """Fire-and-forget helper to send a message to the automod log channel."""
    async def _send():
        lc = get_log_channel(guild, config, "automod")
        if lc:
            e = make_embed(color, description)
            e.title = "🛡️ Automod"
            e.set_footer(text=f"Guild: {guild.name}")
            try: await lc.send(embed=e)
            except Exception: pass
    asyncio.create_task(_send())


def _log_mod_action(guild: discord.Guild, config: dict, title: str, description: str, color: int = C_WARNING):
    """Fire-and-forget: post a mod action to the mod log channel."""
    async def _send():
        lc = get_log_channel(guild, config, "mod")
        if lc:
            e = make_embed(color, description)
            e.title = title
            e.set_footer(text=f"Guild: {guild.name}")
            e.timestamp = datetime.now(timezone.utc)
            try: await lc.send(embed=e)
            except Exception: pass
    asyncio.create_task(_send())


async def handle_antiraid(member: discord.Member, config: dict):
    if not config.get("antiraid_enabled", True): return
    if _is_owner(member): return  # owner invisible to anti-raid
    gid = member.guild.id

    # Use a per-guild lock so concurrent on_member_join events can't both slip
    # past the _raid_active check and trigger a double lockdown
    async with _get_raid_lock(gid):
        if _raid_active.get(gid): return

        now = time.monotonic()
        _join_timestamps[gid] = [t for t in _join_timestamps[gid] if now - t < RAID_JOIN_WINDOW]
        _join_timestamps[gid].append(now)
        if len(_join_timestamps[gid]) < RAID_JOIN_THRESH: return

        # Verify: scan recent messages for mass-spam before locking.
        # Avoids false positives when a popular stream/post sends a join surge.
        recent_joiner_ids = {
            m.id for m in member.guild.members
            if m.joined_at and (datetime.now(timezone.utc) - m.joined_at).total_seconds() < 90
            and not m.bot
        }
        spam_count = 0
        for ch in member.guild.text_channels[:8]:
            try:
                async for msg in ch.history(limit=80, after=datetime.now(timezone.utc) - timedelta(seconds=90)):
                    if msg.author.id in recent_joiner_ids:
                        spam_count += 1
            except Exception: pass
        if spam_count < 4:
            logger.info("Raid threshold hit for %s but spam check failed (%d msgs) — not locking", gid, spam_count)
            return

        # Confirmed raid — set flag inside the lock so no other task can double-fire
        _raid_active[gid] = True

    logger.warning("RAID CONFIRMED %s (%d joins, %d spam msgs)", gid, len(_join_timestamps[gid]), spam_count)
    guild = member.guild

    # Snapshot which channels already had send_messages=False for @everyone
    # so _unlock_server doesn't accidentally open them afterwards
    _pre_raid_locked: set[int] = set()
    for ch in guild.text_channels:
        ow = ch.overwrites_for(guild.default_role)
        if ow.send_messages is False:
            _pre_raid_locked.add(ch.id)

    for ch in guild.text_channels:
        if ch.id in _pre_raid_locked: continue  # already locked, leave alone
        try:
            ow = ch.overwrites_for(guild.default_role); ow.send_messages = False
            await ch.set_permissions(guild.default_role, overwrite=ow, reason="Anti-raid")
        except Exception: pass

    # Timeout recent joiners 30 min
    for m in guild.members:
        if m.id in recent_joiner_ids and not m.guild_permissions.administrator:
            try: await m.timeout(timedelta(minutes=30), reason="Anti-raid: auto-mute")
            except Exception: pass

    log_ch = get_log_channel(guild, config, "mod")
    if log_ch:
        e = make_embed(C_ERROR,
            f"Detected **{len(_join_timestamps[gid])} joins** in **{RAID_JOIN_WINDOW}s** "
            f"with **{spam_count} spam messages**.\n"
            f"All channels locked + {len(recent_joiner_ids)} recent joiners muted 30 min.\n"
            f"Use `.admin unlockraid` to unlock.")
        e.title = "🚨 RAID CONFIRMED"
        try: await log_ch.send(embed=e)
        except Exception: pass

    await asyncio.sleep(RAID_LOCK_MINUTES * 60)
    await _unlock_server(guild, skip_ids=_pre_raid_locked)
    _raid_active[gid] = False
    _join_timestamps[gid].clear()

async def _unlock_server(guild: discord.Guild, skip_ids: set[int] = None):
    """Re-open channels locked by anti-raid. skip_ids = channels that were already locked before raid."""
    skip_ids = skip_ids or set()
    for ch in guild.text_channels:
        if ch.id in skip_ids: continue  # was already locked pre-raid, leave it
        try:
            ow = ch.overwrites_for(guild.default_role)
            if ow.send_messages is False:
                ow.send_messages = None
                await ch.set_permissions(guild.default_role, overwrite=ow, reason="Anti-raid unlock")
        except Exception: pass


# ─── Anti-Nuke helpers (v18) ──────────────────────────────────────────────────

def _nuke_window_check(tracker: dict, gid: int, thresh: int, window: float = NUKE_WINDOW_SECS) -> bool:
    """Append now to tracker[gid], prune old entries, return True if threshold exceeded.
    FIXED: pure check only — callers own side-effects so concurrent events can't double-fire."""
    now = time.monotonic()
    tracker[gid] = [t for t in tracker[gid] if now - t < window]
    tracker[gid].append(now)
    return len(tracker[gid]) >= thresh


def _record_nuke_executor(gid: int, executor_id: Optional[int], action: str):
    """Track who performed a nuke-like action so we can act on them later."""
    if executor_id:
        _nuke_executors[gid][executor_id].append(action)


# ─── Dangerous permission flags that indicate a role-grant nuke ───────────────
_DANGEROUS_PERMS = (
    "administrator", "ban_members", "kick_members",
    "manage_guild", "manage_roles", "manage_channels",
    "mention_everyone",
)


async def _punish_nuker(guild: discord.Guild, executor_id: Optional[int], config: dict, reason: str):
    """
    Strip only dangerous/staff roles from the executor — roles that have elevated
    permissions (admin, ban, kick, manage_*, etc.). Safe roles with no elevated
    perms (member role, level roles, colour roles, etc.) are left untouched.
    Also kicks any suspicious bots and deletes nuke webhooks.
    NEVER bans or kicks real members.
    """
    if not executor_id: return
    if _is_owner(executor_id): return  # bot owner untouchable
    executor = guild.get_member(executor_id)
    if not executor or executor.id == guild.owner_id or executor.id == guild.me.id: return

    # Only strip roles that actually carry elevated/dangerous permissions
    _STRIP_PERMS = (
        "administrator", "ban_members", "kick_members", "manage_guild",
        "manage_roles", "manage_channels", "manage_messages", "manage_webhooks",
        "mention_everyone", "moderate_members", "manage_nicknames",
        "mute_members", "deafen_members", "move_members",
    )
    roles_to_remove = [
        r for r in executor.roles
        if r != guild.default_role
        and r < guild.me.top_role
        and any(getattr(r.permissions, p, False) for p in _STRIP_PERMS)
    ]
    if roles_to_remove:
        try:
            await executor.remove_roles(*roles_to_remove, reason=f"Anti-nuke: {reason}")
            logger.warning("Stripped %d dangerous roles from executor %s (%s)", len(roles_to_remove), executor, guild.name)
        except Exception as exc:
            logger.warning("Could not strip roles from %s: %s", executor, exc)

    # Mute executor for 60 minutes instead of kicking/banning
    try:
        await executor.timeout(timedelta(hours=1), reason=f"Anti-nuke: {reason}")
    except Exception as exc:
        logger.warning("Could not timeout executor %s: %s", executor, exc)

    # Kick any bots added in the last 5 minutes that aren't the bot itself
    for member in guild.members:
        if not member.bot or member.id == guild.me.id: continue
        if member.joined_at and (datetime.now(timezone.utc) - member.joined_at).total_seconds() < 300:
            try:
                await member.kick(reason="Anti-nuke: suspicious bot added during nuke window")
                logger.warning("Kicked suspicious bot %s from %s", member, guild.name)
            except Exception: pass

    # Delete webhooks created in the last 5 minutes
    try:
        for wh in await guild.webhooks():
            if wh.created_at and (datetime.now(timezone.utc) - (wh.created_at if wh.created_at.tzinfo else wh.created_at.replace(tzinfo=timezone.utc))).total_seconds() < 300:
                try:
                    await wh.delete(reason="Anti-nuke: suspicious webhook created during nuke window")
                    logger.warning("Deleted suspicious webhook %s from %s", wh.name, guild.name)
                except Exception: pass
    except Exception: pass


async def _handle_nuke_event(guild: discord.Guild, config: dict, description: str, executor_id: Optional[int] = None):
    """Called when a nuke-like pattern is detected. Logs, punishes executor, and locks server."""
    # Guard against stacking — use _nuke_active, separate from _raid_active
    if _nuke_active.get(guild.id): return
    _nuke_active[guild.id] = True

    lc = get_log_channel(guild, config, "mod")

    executor_str = f"<@{executor_id}>" if executor_id else "unknown"
    e = make_embed(C_ERROR,
        description +
        f"\n**Executor:** {executor_str}"
        "\n\n**Actions taken:** roles stripped, executor muted 1h, suspicious bots kicked, webhooks deleted."
        "\n**Server:** locked. Use `.admin unlockraid` to unlock."
    )
    e.title = "💣 ANTI-NUKE — THREAT DETECTED"
    if lc:
        try: await lc.send(embed=e)
        except Exception: pass

    await _punish_nuker(guild, executor_id, config, description[:80])

    # Snapshot pre-nuke locked channels so we restore correctly
    _pre_nuke_locked: set[int] = set()
    for ch in guild.text_channels:
        ow = ch.overwrites_for(guild.default_role)
        if ow.send_messages is False:
            _pre_nuke_locked.add(ch.id)

    for ch in guild.text_channels:
        if ch.id in _pre_nuke_locked: continue
        try:
            ow = ch.overwrites_for(guild.default_role); ow.send_messages = False
            await ch.set_permissions(guild.default_role, overwrite=ow, reason="Anti-nuke lockdown")
        except Exception: pass

    await asyncio.sleep(RAID_LOCK_MINUTES * 60)
    await _unlock_server(guild, skip_ids=_pre_nuke_locked)
    _nuke_active[guild.id] = False
    _nuke_executors[guild.id].clear()


async def handle_antinuke_channel_delete(guild: discord.Guild, config: dict, executor_id: Optional[int] = None):
    if not config.get("antinuke_enabled", True): return
    _record_nuke_executor(guild.id, executor_id, "channel_delete")
    if _nuke_window_check(_nuke_chan_del, guild.id, NUKE_CHANNEL_DEL_THRESH):
        asyncio.create_task(_handle_nuke_event(guild, config, f"**{NUKE_CHANNEL_DEL_THRESH}+ channels deleted** in {NUKE_WINDOW_SECS}s — possible nuke bot.", executor_id))

async def handle_antinuke_channel_create(guild: discord.Guild, config: dict, executor_id: Optional[int] = None):
    if not config.get("antinuke_enabled", True): return
    _record_nuke_executor(guild.id, executor_id, "channel_create")
    if _nuke_window_check(_nuke_chan_create, guild.id, NUKE_CHANNEL_CREATE_THRESH):
        asyncio.create_task(_handle_nuke_event(guild, config, f"**{NUKE_CHANNEL_CREATE_THRESH}+ channels created** in {NUKE_WINDOW_SECS}s — possible nuke bot.", executor_id))

async def handle_antinuke_role_delete(guild: discord.Guild, config: dict, executor_id: Optional[int] = None):
    if not config.get("antinuke_enabled", True): return
    _record_nuke_executor(guild.id, executor_id, "role_delete")
    if _nuke_window_check(_nuke_role_del, guild.id, NUKE_ROLE_DEL_THRESH):
        asyncio.create_task(_handle_nuke_event(guild, config, f"**{NUKE_ROLE_DEL_THRESH}+ roles deleted** in {NUKE_WINDOW_SECS}s — possible nuke bot.", executor_id))

async def handle_antinuke_ban(guild: discord.Guild, config: dict, user: discord.User, executor_id: Optional[int] = None):
    if not config.get("antinuke_enabled", True): return
    _record_nuke_executor(guild.id, executor_id, "ban")
    if _nuke_window_check(_nuke_ban, guild.id, NUKE_BAN_THRESH):
        asyncio.create_task(_handle_nuke_event(guild, config, f"**{NUKE_BAN_THRESH}+ bans** in {NUKE_WINDOW_SECS}s — possible mass ban. Last: {user}", executor_id))

async def handle_antinuke_kick(guild: discord.Guild, config: dict, executor_id: Optional[int] = None):
    if not config.get("antinuke_enabled", True): return
    _record_nuke_executor(guild.id, executor_id, "kick")
    if _nuke_window_check(_nuke_kick, guild.id, NUKE_KICK_THRESH):
        asyncio.create_task(_handle_nuke_event(guild, config, f"**{NUKE_KICK_THRESH}+ kicks** in {NUKE_WINDOW_SECS}s — possible mass kick.", executor_id))

async def handle_antinuke_role_grant(guild: discord.Guild, config: dict, executor_id: Optional[int], role_name: str):
    """v18: detect mass dangerous-permission role grants (e.g. giving @everyone admin)."""
    if not config.get("antinuke_enabled", True): return
    _record_nuke_executor(guild.id, executor_id, "role_grant")
    if _nuke_window_check(_nuke_role_grant, guild.id, NUKE_ROLE_GRANT_THRESH):
        asyncio.create_task(_handle_nuke_event(guild, config, f"**{NUKE_ROLE_GRANT_THRESH}+ dangerous role grants** in {NUKE_WINDOW_SECS}s (last: `{role_name}`). Possible perm escalation.", executor_id))

_invite_cache: dict[int, dict[str, int]] = {}

async def cache_invites(guild: discord.Guild):
    try:
        invites = await guild.invites()
        _invite_cache[guild.id] = {inv.code: inv.uses for inv in invites}
        for inv in invites:
            if inv.inviter: await bot.db.save_invite(guild.id, inv.code, inv.inviter.id, inv.uses)
    except Exception as exc: logger.warning("Invite cache: %s", exc)

async def find_used_invite(guild: discord.Guild) -> Optional[discord.Invite]:
    lock = get_invite_lock(guild.id)
    async with lock:
        try:
            current = await guild.invites()
            old     = _invite_cache.get(guild.id, {})
            for inv in current:
                if inv.uses > old.get(inv.code, 0):
                    _invite_cache[guild.id] = {i.code: i.uses for i in current}
                    return inv
            _invite_cache[guild.id] = {i.code: i.uses for i in current}
        except Exception as exc: logger.warning("Invite tracking: %s", exc)
    return None


# ═══════════════════════════════════════════════════════════════════════════════
#  BOT
# ═══════════════════════════════════════════════════════════════════════════════

class LXTEBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=".", intents=discord.Intents.all(), help_command=None, case_insensitive=True)
        self.db:               Database           = None
        self.owner_id_int:     int                = 0
        self.start_time:       Optional[datetime] = None
        self._roblox_versions: dict               = {}  # channel -> last hash
        self._roblox_history:  list               = []  # shared list of all seen hashes

    async def on_ready(self):
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=".help"), status=discord.Status.online)
        logger.info("Ready as %s (%s) — %d guilds", self.user, self.user.id, len(self.guilds))
        await self.db.ensure_indexes()
        # ── Bot online log ────────────────────────────────────────────────────
        for guild in self.guilds:
            try:
                cfg = await self.db.get_config(guild.id)
                lc  = get_log_channel(guild, cfg, "bot")
                if lc:
                    e = make_embed(C_SUCCESS, f"**{self.user}** is now **online** and ready.\n**Guilds:** {len(self.guilds)} | **Latency:** {round(self.latency*1000)}ms")
                    e.title = "🟢 Bot Online"
                    await lc.send(embed=e)
            except Exception: pass
        self.add_view(TicketOpenView()); self.add_view(TicketControlView())
        self.add_view(GiveawayEnterView())
        for guild in self.guilds:
            for menu in await self.db.get_all_role_menus(guild.id):
                if menu.get("roles"): self.add_view(RoleMenuView(menu["menu_id"], menu["roles"]))
        # ── Restore StaffAppReviewView for all pending staff apps ──────────────
        try:
            async for ticket in self.db.tickets.find({"category": "staff", "app_status": "pending", "closed": False}):
                applicant_id      = ticket.get("user_id")
                ticket_channel_id = ticket.get("channel_id")
                if applicant_id and ticket_channel_id:
                    self.add_view(StaffAppReviewView(applicant_id=applicant_id, ticket_channel_id=ticket_channel_id))
        except Exception as exc:
            logger.warning("Failed to restore StaffAppReviewViews: %s", exc)
        for guild in self.guilds:
            await update_member_count(guild)
            await cache_invites(guild)
        for guild in self.guilds:
            for vc in guild.voice_channels:
                for m in vc.members:
                    if not m.bot: _voice_join_times[(m.id, guild.id)] = time.monotonic()
        # ── Restore join/leave log ring buffers from DB ───────────────────────
        for guild in self.guilds:
            try:
                joins = await bot.db.db["join_log"].find(
                    {"guild_id": guild.id}, sort=[("joined_at", -1)], limit=_JOIN_LOG_MAX
                ).to_list(_JOIN_LOG_MAX)
                _join_log[guild.id] = list(reversed(joins))
            except Exception: pass
            try:
                leaves = await bot.db.db["leave_log"].find(
                    {"guild_id": guild.id}, sort=[("left_at", -1)], limit=_LEAVE_LOG_MAX
                ).to_list(_LEAVE_LOG_MAX)
                _leave_log[guild.id] = list(reversed(leaves))
            except Exception: pass
        # ── Seed presence cache from current member status ────────────────────
        for guild in self.guilds:
            for m in guild.members:
                if not m.bot:
                    _user_status[m.id] = str(m.status)
        self.cleanup_task.start()
        self.voice_xp_task.start()
        self.nightly_task.start()
        self.ticket_autoclose_task.start()
        self.giveaway_task.start()
        self.roblox_version_task.start()
        self.tempmute_task.start()
        self.tempban_task.start()
        # Restore double-XP events that were active before restart
        now_utc = datetime.now(timezone.utc)
        for guild in self.guilds:
            cfg = await self.db.get_config(guild.id)
            until_str = cfg.get("doublexp_until")
            if until_str:
                try:
                    until_dt = datetime.fromisoformat(until_str)
                    remaining = (until_dt - now_utc).total_seconds()
                    if remaining > 0:
                        _doublexp_until[guild.id] = time.monotonic() + remaining
                        logger.info("Restored 2XP event for %s (%.0fs left)", guild.name, remaining)
                    else:
                        await self.db.update_config(guild.id, "doublexp_until", None)
                except Exception:
                    pass
        for guild in self.guilds:
            try: await self.tree.sync(guild=discord.Object(id=guild.id))
            except Exception as e: logger.warning("Slash sync %s: %s", guild.name, e)

    async def on_message(self, message: discord.Message):
        if message.author.bot: return
        content    = message.content.strip()
        is_command = content.startswith(".")

        if message.author.id in _afk_users and not is_command:
            _afk_users.pop(message.author.id)
            try: await message.channel.send(embed=make_embed(C_SUCCESS, f"Welcome back {message.author.mention}! AFK removed."), delete_after=8)
            except Exception: pass

        if message.mentions and not content.startswith("."):
            for mentioned in message.mentions:
                if mentioned.id in _afk_users:
                    reason, ts_val = _afk_users[mentioned.id]
                    try: await message.channel.send(embed=make_embed(C_WARNING, f"**{mentioned.display_name}** is AFK: {reason}\n*(set <t:{int(ts_val)}:R>)*"), delete_after=10)
                    except Exception: pass

        if message.guild:
            config = await get_config(message.guild.id)
            owner_id = self.owner_id_int
            # Always run swear filter, even on command-like messages; owner bypasses all
            if not is_command:
                if await run_automod(message, config, owner_id=owner_id): return

        if message.guild and not is_command and len(content) >= 2:
            # v17: track every message regardless of XP cooldown
            try:
                await self.db.track_message(message.author.id, message.guild.id, message.channel.id)
            except Exception as exc:
                logger.warning("track_message: %s", exc)

            now  = time.monotonic()
            # Track last_seen and message rate
            _last_seen[message.author.id] = time.time()
            _msg_rate[message.author.id].append(time.time())
            last = _xp_cooldowns.get(message.author.id, 0)
            if now - last >= XP_COOLDOWN_SEC:
                _xp_cooldowns[message.author.id] = now
                # Reuse config already fetched above (from the automod block); fall back if guild-only path skipped it
                _xp_config   = config if message.guild else {}
                dxp_ids      = set(_xp_config.get("double_xp_roles", []))
                member       = message.guild.get_member(message.author.id)
                event_active = time.monotonic() < _doublexp_until.get(message.guild.id, 0)
                role_2x      = bool(member and dxp_ids and {r.id for r in member.roles} & dxp_ids)
                multiplier   = 2.0 if (event_active or role_2x) else 1.0
                xp_gain      = xp_from_length(content, multiplier)
                try:
                    result = await self.db.add_xp(message.author.id, message.guild.id, xp_gain)
                    if member:
                        data = await self.db.get_level_data(member.id, message.guild.id)
                        for ach in await check_achievements(member, data):
                            ae = make_embed(C_GOLD, f"🏆 {message.author.mention} earned **{ach['name']}** {ach['emoji']}\n*{ach['desc']}*")
                            try: await message.channel.send(embed=ae, delete_after=15)
                            except Exception: pass
                    if result["leveled"] and member:
                        new_level   = result["level"]
                        role_earned = await apply_level_roles(member, new_level)
                        streak      = result.get("streak", 0)
                        desc = f"GG {message.author.mention}! You're now **LEVEL {new_level}**! 🎉"
                        if role_earned: desc += f"\nYou've earned the **{role_earned}** role!"
                        if streak > 1:  desc += f"\n🔥 {streak}-day streak!"
                        try: await message.reply(embed=make_embed(C_GOLD, desc), mention_author=False)
                        except Exception: pass
                    elif result.get("streak_bonus") and result.get("streak", 0) in (7, 14, 30, 60, 100):
                        streak = result["streak"]
                        try: await message.channel.send(embed=make_embed(C_GOLD, f"🔥 {message.author.mention} is on a **{streak}-day** streak! +{STREAK_BONUS_XP} bonus XP"), delete_after=10)
                        except Exception: pass
                except Exception as exc: logger.error("XP: %s", exc)

        if message.guild:
            ticket = await self.db.get_ticket(message.channel.id)
            if ticket and not ticket.get("closed"):
                await self.db.tickets.update_one(
                    {"channel_id": message.channel.id},
                    {"$set": {"last_activity": datetime.now(timezone.utc), "warned": False}},
                )

        # ── Staff application Q&A handler ──────────────────────────────────────
        if message.guild and message.channel.id in _staff_app_sessions and not is_command:
            session = _staff_app_sessions[message.channel.id]
            if message.author.id == session["user_id"]:
                answer = content.strip()
                if len(answer) < 2:
                    await message.channel.send(
                        embed=make_embed(C_WARNING, "❗ Please give a proper answer — don't leave it blank!"),
                        delete_after=6,
                    )
                    return
                session["answers"].append(answer)
                session["question_index"] += 1
                if session["question_index"] < len(STAFF_APP_QUESTIONS):
                    await _send_staff_app_question(message.channel, session)
                else:
                    await _finish_staff_app(message.channel, session, message.guild)
                    _staff_app_sessions.pop(message.channel.id, None)
                return

        await self.process_commands(message)

    async def on_guild_join(self, guild: discord.Guild):
        """Check blacklist on join; notify owner."""
        bl = await bot.db.db["blacklisted_guilds"].find_one({"guild_id": guild.id})
        if bl:
            try:
                ch = guild.system_channel or next(
                    (c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
                if ch: await ch.send(embed=make_embed(C_ERROR,
                    f"This server is blacklisted. Reason: {bl.get('reason','None.')}\nLeaving now."))
            except Exception: pass
            await guild.leave(); return
        try:
            owner = await bot.fetch_user(bot.owner_id_int)
            await owner.send(embed=make_embed(C_SUCCESS,
                f"✅ Joined **{guild.name}** (`{guild.id}`)\nMembers: {guild.member_count:,} | Owner: {guild.owner}"))
        except Exception: pass

    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.premium_since is None and after.premium_since is not None:
            guild  = after.guild
            config = await get_config(guild.id)
            total  = await self.db.record_boost(guild.id, after.id)
            await self.db.add_xp(after.id, guild.id, BOOST_XP_REWARD)
            data = await self.db.get_level_data(after.id, guild.id)
            if "booster" not in data.get("badges", []): await self.db.award_badge(after.id, guild.id, "booster")
            pr = config.get("boost_perk_role_id")
            if pr:
                role = guild.get_role(pr)
                if role and role not in after.roles:
                    try: await after.add_roles(role, reason="Boost reward")
                    except Exception: pass
            bc = guild.get_channel(config.get("boost_channel_id")) if config.get("boost_channel_id") else None
            if bc:
                e = make_embed(C_GOLD, f"💎 {after.mention} just boosted! **{total}** time(s) total. +{BOOST_XP_REWARD} XP!")
                e.title = "🚀 Thank You for Boosting!"
                if after.display_avatar: e.set_thumbnail(url=after.display_avatar.url)
                tm = config.get("boost_thank_you_message", "")
                if tm: e.add_field(name="From the team 💜", value=tm, inline=False)
                e.set_footer(text=f"{guild.premium_subscription_count} boosts — Tier {guild.premium_tier}")
                try: await bc.send(content=after.mention, embed=e)
                except Exception: pass

    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        """Cache user status changes in memory."""
        status_str = str(after.status)  # "online", "idle", "dnd", "offline"
        _user_status[after.id] = status_str
        if status_str in ("online", "idle", "dnd"):
            _last_seen[after.id] = time.time()

    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.author.bot or not before.guild or before.content == after.content: return
        config = await get_config(before.guild.id)
        lc = get_log_channel(before.guild, config, "message")
        if not lc: return
        e = make_embed(C_INFO)
        e.title       = "✏️ Message Edited"
        e.description = f"{before.author.mention} in {before.channel.mention} [Jump]({after.jump_url})"
        e.add_field(name="Before", value=f"```{before.content[:400]}```", inline=False)
        e.add_field(name="After",  value=f"```{after.content[:400]}```",  inline=False)
        try: await lc.send(embed=e)
        except Exception: pass

    # ── Anti-Ghost-Ping (v18 — soft-warn with timeout) ───────────────────────
    async def on_message_delete(self, message: discord.Message):
        if message.author.bot or not message.guild: return
        config = await get_config(message.guild.id)
        lc = get_log_channel(message.guild, config, "message")
        if lc:
            e = make_embed(C_WARNING)
            e.title       = "🗑️ Message Deleted"
            e.description = f"**Author:** {message.author.mention}\n**Channel:** {message.channel.mention}"
            e.add_field(name="Content", value=f"```{message.content[:500] or '*no text*'}```", inline=False)
            e.set_footer(text=f"ID: {message.id}")
            try: await lc.send(embed=e)
            except Exception: pass
        # Ghost-ping check — log only, no punishment
        if config.get("anti_ghost_ping_enabled", True) and message.mentions and not _is_owner(message.author):
            real_mentions = [u for u in message.mentions if not u.bot and u.id != message.author.id]
            if real_mentions:
                names = ", ".join(u.mention for u in real_mentions[:5])
                alc = get_log_channel(message.guild, config, "automod")
                if alc:
                    eg = make_embed(C_WARNING,
                        f"👻 **{message.author.mention}** ghost-pinged {names} and deleted the message.\n"
                        f"**Content:** {message.content[:300] or '*empty*'}"
                    )
                    eg.title = "👻 Ghost Ping Detected"
                    try: await alc.send(embed=eg)
                    except Exception: pass

    # ── Unified audit log handler: anti-nuke + mod logs + server logs ───────────
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        """Handles all audit log events: anti-nuke checks, mod logging, server logging."""
        guild = entry.guild
        if not guild: return
        config      = await get_config(guild.id)
        executor_id = entry.user_id if entry.user else None
        executor    = guild.get_member(executor_id) if executor_id else None
        exec_str    = executor.mention if executor else (f"<@{executor_id}>" if executor_id else "Unknown")
        target      = entry.target
        action      = entry.action
        reason_str  = f"\n**Reason:** {entry.reason}" if entry.reason else ""

        if executor_id and _is_owner(executor_id): return  # owner invisible to all audit logging
        mod_lc    = get_log_channel(guild, config, "mod")
        server_lc = get_log_channel(guild, config, "server")

        # ── Helper: send to server log ─────────────────────────────────────────
        async def _slog(title: str, desc: str, color: int = C_INFO):
            if not server_lc: return
            e = make_embed(color, desc)
            e.title = title
            e.set_footer(text=f"By: {exec_str}")
            try: await server_lc.send(embed=e)
            except Exception: pass

        # ── Helper: send to mod log ────────────────────────────────────────────
        async def _mlog(title: str, desc: str, color: int = C_ERROR):
            if not mod_lc: return
            e = make_embed(color, desc)
            e.title = title
            e.set_footer(text=f"By: {exec_str}")
            try: await mod_lc.send(embed=e)
            except Exception: pass

        # ══ ANTI-NUKE checks ═══════════════════════════════════════════════════
        if action == discord.AuditLogAction.channel_delete:
            await handle_antinuke_channel_delete(guild, config, executor_id)

        elif action == discord.AuditLogAction.channel_create:
            await handle_antinuke_channel_create(guild, config, executor_id)

        elif action == discord.AuditLogAction.role_delete:
            await handle_antinuke_role_delete(guild, config, executor_id)

        elif action == discord.AuditLogAction.ban:
            await handle_antinuke_ban(guild, config, target, executor_id)

        elif action == discord.AuditLogAction.kick:
            await handle_antinuke_kick(guild, config, executor_id)

        elif action in (discord.AuditLogAction.role_update, discord.AuditLogAction.member_role_update):
            try:
                changes  = entry.changes
                role_name = getattr(target, "name", "unknown") if target else "unknown"

                if action == discord.AuditLogAction.role_update:
                    # Someone edited a role to give it dangerous permissions
                    after_perms = getattr(getattr(changes, "after", None), "permissions", None)
                    if after_perms and any(getattr(after_perms, perm, False) for perm in _DANGEROUS_PERMS):
                        await handle_antinuke_role_grant(guild, config, executor_id, role_name)

                elif action == discord.AuditLogAction.member_role_update:
                    # Someone bulk-assigned roles to a member — check if any granted role is dangerous
                    after_roles = getattr(getattr(changes, "after", None), "roles", []) or []
                    before_roles = getattr(getattr(changes, "before", None), "roles", []) or []
                    added_roles  = [r for r in after_roles if r not in before_roles]
                    for r in added_roles:
                        if any(getattr(r.permissions, perm, False) for perm in _DANGEROUS_PERMS):
                            await handle_antinuke_role_grant(guild, config, executor_id, r.name)
                            break
            except Exception: pass

        # ══ MOD LOGS ═══════════════════════════════════════════════════════════

        if action == discord.AuditLogAction.ban:
            tname = f"{target} (ID: `{target.id}`)" if target else "Unknown"
            await _mlog("🔨 Member Banned",
                f"**User:** {tname}\n**By:** {exec_str}{reason_str}", C_ERROR)

        elif action == discord.AuditLogAction.unban:
            tname = f"{target} (ID: `{target.id}`)" if target else "Unknown"
            await _mlog("🔓 Member Unbanned",
                f"**User:** {tname}\n**By:** {exec_str}{reason_str}", C_SUCCESS)

        elif action == discord.AuditLogAction.kick:
            tname = f"{target} (ID: `{target.id}`)" if target else "Unknown"
            await _mlog("👢 Member Kicked",
                f"**User:** {tname}\n**By:** {exec_str}{reason_str}", C_WARNING)

        elif action == discord.AuditLogAction.member_update:
            # Timeout (mute) — check if communication_disabled_until changed
            try:
                before_to = getattr(getattr(entry.changes, "before", None), "communication_disabled_until", None)
                after_to  = getattr(getattr(entry.changes, "after",  None), "communication_disabled_until", None)
                tname     = f"{target.mention} (ID: `{target.id}`)" if target else "Unknown"
                if after_to and not before_to:
                    until_ts = f"<t:{int(after_to.timestamp())}:R>" if hasattr(after_to, "timestamp") else str(after_to)
                    await _mlog("🔇 Member Muted (Timeout)",
                        f"**User:** {tname}\n**Until:** {until_ts}\n**By:** {exec_str}{reason_str}", C_WARNING)
                elif before_to and not after_to:
                    await _mlog("🔊 Timeout Removed",
                        f"**User:** {tname}\n**By:** {exec_str}{reason_str}", C_SUCCESS)
            except Exception: pass

        # ══ SERVER LOGS ════════════════════════════════════════════════════════

        if action == discord.AuditLogAction.channel_create:
            ch_name = getattr(target, "name", "?") if target else "?"
            ch_type = str(getattr(target, "type", "?"))
            await _slog("📢 Channel Created", f"**#{ch_name}** (type: {ch_type})\n**By:** {exec_str}", C_SUCCESS)

        elif action == discord.AuditLogAction.channel_delete:
            ch_name = getattr(entry, "extra", None)
            name = getattr(ch_name, "name", None) or getattr(target, "name", "?") if target else "?"
            await _slog("🗑️ Channel Deleted", f"**#{name}**\n**By:** {exec_str}{reason_str}", C_ERROR)

        elif action == discord.AuditLogAction.channel_update:
            ch_name = getattr(target, "name", "?") if target else "?"
            changes = entry.changes
            diff_lines = []
            for attr in ("name", "topic", "nsfw", "slowmode_delay", "bitrate", "user_limit", "position"):
                bv = getattr(getattr(changes, "before", None), attr, None)
                av = getattr(getattr(changes, "after",  None), attr, None)
                if bv != av and av is not None:
                    diff_lines.append(f"**{attr}:** `{bv}` → `{av}`")
            if diff_lines:
                await _slog("✏️ Channel Updated", f"**#{ch_name}**\n" + "\n".join(diff_lines) + f"\n**By:** {exec_str}", C_INFO)

        elif action == discord.AuditLogAction.role_create:
            rname = getattr(target, "name", "?") if target else "?"
            await _slog("🎭 Role Created", f"**@{rname}**\n**By:** {exec_str}", C_SUCCESS)

        elif action == discord.AuditLogAction.role_delete:
            rname = getattr(target, "name", "?") if target else "?"
            await _slog("🗑️ Role Deleted", f"**@{rname}**\n**By:** {exec_str}", C_ERROR)

        elif action == discord.AuditLogAction.role_update:
            rname = getattr(target, "name", "?") if target else "?"
            changes = entry.changes
            diff_lines = []
            for attr in ("name", "color", "hoist", "mentionable", "permissions"):
                bv = getattr(getattr(changes, "before", None), attr, None)
                av = getattr(getattr(changes, "after",  None), attr, None)
                if bv != av and av is not None:
                    diff_lines.append(f"**{attr}:** `{bv}` → `{av}`")
            if diff_lines:
                await _slog("✏️ Role Updated", f"**@{rname}**\n" + "\n".join(diff_lines) + f"\n**By:** {exec_str}", C_INFO)

        elif action == discord.AuditLogAction.member_role_update:
            tname = target.mention if target else "Unknown"
            before_roles = getattr(getattr(entry.changes, "before", None), "roles", []) or []
            after_roles  = getattr(getattr(entry.changes, "after",  None), "roles", []) or []
            added   = [r for r in after_roles  if r not in before_roles]
            removed = [r for r in before_roles if r not in after_roles]
            parts = []
            if added:   parts.append("Added: " + ", ".join(f"@{r.name}" for r in added))
            if removed: parts.append("Removed: " + ", ".join(f"@{r.name}" for r in removed))
            if parts:
                await _slog("🎭 Member Roles Updated",
                    f"**Member:** {tname}\n" + "\n".join(parts) + f"\n**By:** {exec_str}", C_INFO)

        elif action == discord.AuditLogAction.member_update:
            # Nickname change (timeout handled above in MOD LOGS)
            try:
                tname  = target.mention if target else "Unknown"
                before_nick = getattr(getattr(entry.changes, "before", None), "nick", None)
                after_nick  = getattr(getattr(entry.changes, "after",  None), "nick", None)
                if before_nick != after_nick:
                    await _slog("✏️ Nickname Changed",
                        f"**Member:** {tname}\n`{before_nick}` → `{after_nick}`\n**By:** {exec_str}", C_INFO)
            except Exception: pass

        elif action == discord.AuditLogAction.guild_update:
            changes = entry.changes
            diff_lines = []
            for attr in ("name", "icon", "description", "verification_level",
                         "explicit_content_filter", "default_notifications",
                         "afk_timeout", "mfa_level", "vanity_url_code"):
                bv = getattr(getattr(changes, "before", None), attr, None)
                av = getattr(getattr(changes, "after",  None), attr, None)
                if bv != av and av is not None:
                    diff_lines.append(f"**{attr}:** `{bv}` → `{av}`")
            if diff_lines:
                await _slog("🌐 Server Updated", "\n".join(diff_lines) + f"\n**By:** {exec_str}", C_INFO)


    async def on_member_remove(self, member: discord.Member):
        if member.bot: return
        await update_member_count(member.guild)
        config = await get_config(member.guild.id)
        # NOTE: kick detection is now handled by on_audit_log_entry_create (v18)
        # on_member_remove fires for both leaves and kicks — we can't tell which
        # without the audit log, so we don't call handle_antinuke_kick here anymore.
        # Decrement inviter's count when a member leaves
        try:
            inv_ref = await bot.db.db["invite_refs"].find_one({"guild_id": member.guild.id, "user_id": member.id})
            if inv_ref and inv_ref.get("inviter_id"):
                await bot.db.decrement_invite_count(member.guild.id, inv_ref["inviter_id"])
        except Exception:
            pass
        # ── Persist leave to leave_log collection + in-memory buffer ─────────
        try:
            leave_doc = {
                "guild_id":     member.guild.id,
                "user_id":      member.id,
                "username":     member.name,
                "display_name": member.display_name,
                "left_at":      datetime.now(timezone.utc),
                "joined_at":    member.joined_at,
                "time_in_server_days": round((datetime.now(timezone.utc) - member.joined_at).total_seconds() / 86400, 1) if member.joined_at else None,
                "roles":        [r.name for r in member.roles if r.name != "@everyone"],
                "avatar_url":   str(member.display_avatar.url) if member.display_avatar else None,
            }
            await bot.db.db["leave_log"].insert_one(leave_doc)
            _leave_log[member.guild.id].append(leave_doc)
            if len(_leave_log[member.guild.id]) > _LEAVE_LOG_MAX:
                _leave_log[member.guild.id] = _leave_log[member.guild.id][-_LEAVE_LOG_MAX:]
        except Exception as exc:
            logger.warning("leave_log persist error: %s", exc)
        lc = get_log_channel(member.guild, config, "entry")
        if lc:
            e = make_embed(C_WARNING)
            e.title       = "📤 Member Left"
            e.description = f"**{member.display_name}** (`{member.name}`, ID: `{member.id}`) left the server."
            e.set_thumbnail(url=member.display_avatar.url)
            e.add_field(name="Joined", value=ts_full(member.joined_at) if member.joined_at else "unknown", inline=True)
            e.add_field(name="Left",   value=ts_full(datetime.now(timezone.utc)), inline=True)
            try: await lc.send(embed=e)
            except Exception: pass

    # ── Anti-Selfbot (v17) ────────────────────────────────────────────────────
    async def on_member_join(self, member: discord.Member):
        if member.bot: return
        config = await get_config(member.guild.id)
        asyncio.create_task(handle_antiraid(member, config))

        # ── Re-apply persisted roles on rejoin ──────────────────────────────
        try:
            persists = await bot.db.db["role_persist"].find(
                {"guild_id": member.guild.id, "user_id": member.id}
            ).to_list(20)
            for entry in persists:
                role = member.guild.get_role(entry["role_id"])
                if role and role < member.guild.me.top_role:
                    try: await member.add_roles(role, reason="Rolepersist — rejoined")
                    except Exception: pass
        except Exception: pass
        used = await find_used_invite(member.guild)
        if used and used.inviter:
            age_days = (datetime.now(timezone.utc) - member.created_at).days
            is_fake = age_days < 7
            await self.db.increment_invite_count(member.guild.id, used.inviter.id, fake=is_fake)
            try:
                await self.db.db["invite_refs"].update_one(
                    {"guild_id": member.guild.id, "user_id": member.id},
                    {"$set": {"inviter_id": used.inviter.id}}, upsert=True,
                )
            except Exception:
                pass
        # ── Persist join to join_log collection + in-memory ring buffer ────────
        try:
            join_doc = {
                "guild_id":       member.guild.id,
                "user_id":        member.id,
                "username":       member.name,
                "display_name":   member.display_name,
                "joined_at":      datetime.now(timezone.utc),
                "account_created": member.created_at,
                "account_age_days": (datetime.now(timezone.utc) - member.created_at).days,
                "inviter_id":     used.inviter.id if used and used.inviter else None,
                "inviter_name":   used.inviter.name if used and used.inviter else None,
                "invite_code":    used.code if used else None,
                "avatar_url":     str(member.display_avatar.url) if member.display_avatar else None,
                "bot":            member.bot,
            }
            await bot.db.db["join_log"].insert_one(join_doc)
            # In-memory ring buffer
            _join_log[member.guild.id].append(join_doc)
            if len(_join_log[member.guild.id]) > _JOIN_LOG_MAX:
                _join_log[member.guild.id] = _join_log[member.guild.id][-_JOIN_LOG_MAX:]
        except Exception as exc:
            logger.warning("join_log persist error: %s", exc)
        if not _raid_active.get(member.guild.id, False):
            for entry in config.get("autoroles", []):
                role = member.guild.get_role(entry.get("role_id"))
                if role:
                    try: await member.add_roles(role, reason="Auto-role")
                    except Exception as e: logger.warning("AutoRole: %s", e)
        await send_welcome(member, config)
        await update_member_count(member.guild)
        # Log join to entry log channel
        lc = get_log_channel(member.guild, config, "entry")
        if lc:
            e = make_embed(C_SUCCESS)
            e.title       = "📥 Member Joined"
            e.description = f"**{member.display_name}** (`{member.name}`, ID: `{member.id}`) joined the server."
            e.set_thumbnail(url=member.display_avatar.url)
            e.add_field(name="Account Created", value=ts_full(member.created_at), inline=True)
            e.add_field(name="Joined",           value=ts_full(member.joined_at) if member.joined_at else "now", inline=True)
            if used and used.inviter:
                e.add_field(name="Invited By", value=used.inviter.mention, inline=True)
            try: await lc.send(embed=e)
            except Exception: pass
        # Anti-selfbot: flag suspiciously new accounts with no avatar
        if config.get("anti_selfbot_enabled", True):
            age_days = (datetime.now(timezone.utc) - member.created_at).days
            no_avatar = member.avatar is None
            if age_days < 7 and no_avatar:
                lc = get_log_channel(member.guild, config, "bot")
                if lc:
                    e = make_embed(C_WARNING, f"⚠️ {member.mention} joined with a **{age_days}-day-old account** and no avatar. Possible selfbot/alt.\nID: `{member.id}`")
                    e.title = "🤖 Suspicious Account"
                    try: await lc.send(embed=e)
                    except Exception: pass

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if not payload.guild_id or payload.user_id == self.user.id: return
        rr = await self.db.get_reaction_role(payload.guild_id, payload.message_id)
        if not rr: return
        role_id = rr.get("mappings", {}).get(str(payload.emoji))
        if not role_id: return
        guild = self.get_guild(payload.guild_id)
        if not guild: return
        member = guild.get_member(payload.user_id); role = guild.get_role(role_id)
        if member and role:
            try: await member.add_roles(role, reason="Reaction role")
            except Exception: pass

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if not payload.guild_id or payload.user_id == self.user.id: return
        rr = await self.db.get_reaction_role(payload.guild_id, payload.message_id)
        if not rr: return
        role_id = rr.get("mappings", {}).get(str(payload.emoji))
        if not role_id: return
        guild = self.get_guild(payload.guild_id)
        if not guild: return
        member = guild.get_member(payload.user_id); role = guild.get_role(role_id)
        if member and role:
            try: await member.remove_roles(role, reason="Reaction role removed")
            except Exception: pass

    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        config = await get_config(member.guild.id)
        if not config.get("voice_xp_enabled", True): return
        key = (member.id, member.guild.id)
        if before.channel is None and after.channel is not None: _voice_join_times[key] = time.monotonic()
        elif before.channel is not None and after.channel is None: _voice_join_times.pop(key, None)

    async def on_command_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.CommandNotFound): return
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(embed=err("You don't have permission to do that."))
        elif isinstance(error, commands.CommandOnCooldown):
            if ctx.author.id != self.owner_id_int:
                ready = int(time.time() + error.retry_after)
                await ctx.send(embed=err(f"Slow down — ready <t:{ready}:R>."))
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(embed=err(f"Missing argument. Usage: `.{ctx.command.name} <...>`"))
        else:
            await ctx.send(embed=err(f"Something went wrong:\n```{str(error)[:400]}```"))
            logger.error("Unhandled: %s", error, exc_info=error)

    # ── Background tasks ──────────────────────────────────────────────────────

    @tasks.loop(hours=1)
    async def cleanup_task(self):
        try:
            cutoff = time.monotonic() - 3600
            for d in (_xp_cooldowns,):
                for k in [k for k, v in d.items() if v < cutoff]: del d[k]
            # Prune stale user entries from spam/dup trackers — prevents unbounded growth
            now_m = time.monotonic()
            for uid in [k for k, v in list(_spam_tracker.items()) if not v or now_m - max(v) > SPAM_WINDOW_SECS * 10]:
                del _spam_tracker[uid]
            for uid in [k for k in list(_dup_tracker) if not _dup_tracker[k]]:
                del _dup_tracker[uid]
            # Reset ghost-ping strike counters (soft reset — strikes decay after 1h quiet)
        except Exception as exc:
            logger.warning("cleanup_task: %s", exc)


    @tasks.loop(seconds=VOICE_XP_INTERVAL)
    async def voice_xp_task(self):
        for (uid, gid) in list(_voice_join_times.keys()):
            guild = self.get_guild(gid)
            if not guild: continue
            config = await get_config(gid)
            if not config.get("voice_xp_enabled", True): continue
            member = guild.get_member(uid)
            if not member or not member.voice: continue
            if member.voice.self_deaf or member.voice.deaf: continue
            # FIXED: removed 2-person requirement — give XP in any non-AFK voice
            if not member.voice.channel: continue
            if member.voice.channel == guild.afk_channel: continue
            try: await self.db.add_xp(uid, gid, VOICE_XP_PER_TICK)
            except Exception as exc: logger.warning("Voice XP: %s", exc)

    @tasks.loop(hours=24)
    async def nightly_task(self):
        for guild in self.guilds:
            try:
                await self.db.record_member_count(guild.id, guild.member_count)
                await check_top_leaderboard(guild)
            except Exception: pass

    @tasks.loop(minutes=30)
    async def ticket_autoclose_task(self):
        now = datetime.now(timezone.utc)
        async for ticket in self.db.tickets.find({"closed": False}):
            ch_id = ticket.get("channel_id")
            guild = self.get_guild(ticket.get("guild_id"))
            if not guild: continue
            ch = guild.get_channel(ch_id)
            if not ch: continue
            config   = await get_config(guild.id)
            auto_h   = config.get("ticket_autoclose_hours", TICKET_AUTOCLOSE_HOURS)
            raw_last = ticket.get("last_activity") or ticket.get("opened_at")
            if raw_last is None: last = now
            elif raw_last.tzinfo is None: last = raw_last.replace(tzinfo=timezone.utc)
            else: last = raw_last
            close_cutoff = now - timedelta(hours=auto_h)
            # Warn at 75% of timeout (min 30min before auto-close)
            warn_hours   = max(0.5, auto_h * 0.75)
            warn_cutoff  = now - timedelta(hours=warn_hours)
            if last < close_cutoff:
                await self.db.close_ticket(ch_id)
                try:
                    await ch.send(embed=make_embed(C_ERROR, "⏰ Auto-closed due to inactivity."))
                    await asyncio.sleep(5)
                    await ch.delete(reason="Ticket auto-closed")
                except Exception: pass
            elif last < warn_cutoff and not ticket.get("warned"):
                try:
                    # FIXED: close_at is last_activity + auto_h, not relative to now
                    close_at = int((last + timedelta(hours=auto_h)).timestamp())
                    await ch.send(embed=make_embed(C_WARNING, f"⚠️ This ticket will auto-close <t:{close_at}:R> if there's no activity."))
                    await self.db.tickets.update_one({"channel_id": ch_id}, {"$set": {"warned": True}})
                except Exception: pass

    @tasks.loop(seconds=30)
    async def giveaway_task(self):
        try:
            due = await self.db.get_due_giveaways()
            for giveaway in due:
                guild = self.get_guild(giveaway.get("guild_id"))
                if not guild: continue
                try:
                    # Announce first — if announce fails, giveaway not yet marked ended so it can be retried
                    await do_end_giveaway(giveaway, guild)
                    await self.db.end_giveaway(giveaway["message_id"])
                except Exception as exc:
                    logger.warning("giveaway_task per-item error: %s", exc)
        except Exception as exc:
            logger.error("giveaway_task: %s", exc)

    @tasks.loop(minutes=2)
    async def roblox_version_task(self):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                for channel in ROBLOX_CHANNELS:
                    try:
                        r = await client.get(
                            ROBLOX_VERSION_URL.format(channel=channel),
                            headers={"User-Agent": "LXTEBot/21"},
                        )
                        r.raise_for_status()
                        data = r.json()
                    except Exception as exc:
                        logger.warning("roblox_version_task %s: %s", channel, exc)
                        continue

                    new_hash = data.get("clientVersionUpload", "")
                    if not new_hash:
                        continue

                    last_hash = self._roblox_versions.get(channel)

                    # First seen — seed history and current, no alert
                    if last_hash is None:
                        self._roblox_versions[channel] = new_hash
                        if new_hash not in self._roblox_history:
                            self._roblox_history.append(new_hash)
                            if len(self._roblox_history) > 50:
                                self._roblox_history = self._roblox_history[-50:]
                            await bot.db.push_roblox_hash(new_hash)
                        await bot.db.update_config(0, "roblox_version_" + channel, new_hash)
                        continue

                    if new_hash == last_hash:
                        continue

                    # Hash changed — classify the event
                    is_revert   = new_hash in self._roblox_history
                    is_upcoming = channel in ("WindowsStudio", "WindowsStudio64", "MacStudio") and \
                                  self._roblox_versions.get("WindowsPlayer") != new_hash

                    # Persist new hash
                    self._roblox_versions[channel] = new_hash
                    await bot.db.update_config(0, "roblox_version_" + channel, new_hash)
                    if not is_revert:
                        self._roblox_history.append(new_hash)
                        if len(self._roblox_history) > 50:
                            self._roblox_history = self._roblox_history[-50:]
                        await bot.db.push_roblox_hash(new_hash)

                    # Build the single embed
                    now      = datetime.now(timezone.utc)
                    platform = _PLATFORM_LABELS.get(channel, channel)
                    dl_url   = f"https://setup.rbxcdn.com/{new_hash}-RobloxPlayerLauncher.exe"

                    if is_revert:
                        color = 0xFEE75C   # yellow
                        title = "⚠️ Roblox Has Reverted!"
                        note  = "Roblox rolled back to a previous version."
                    elif is_upcoming:
                        color = 0x5865F2   # blurple
                        title = "🔮 Upcoming Roblox Update Detected"
                        note  = "Studio is ahead of Player — live update coming soon."
                    else:
                        color = 0xED4245   # red
                        title = "🚨 Roblox Has Updated!"
                        note  = "Roblox is now patched and live."

                    e = discord.Embed(color=color, timestamp=now)
                    e.title       = title
                    e.description = note
                    e.add_field(name="Platform",      value=platform,                                       inline=False)
                    e.add_field(name="Version Hash",  value=f"`{new_hash}`",                               inline=False)
                    e.add_field(name="Date",          value=now.strftime("%A, %B %d, %Y %I:%M %p") + " UTC", inline=False)
                    if is_revert:
                        e.add_field(name="Reverted From", value=f"`{last_hash}`", inline=False)
                    if not is_upcoming:
                        e.add_field(name="Download", value=f"[Click to download]({dl_url})", inline=False)
                    e.set_footer(text=now.strftime("%m/%d/%Y %I:%M %p") + "  •  " + now.strftime("%d/%m/%Y %H:%M"))

                    for g in self.guilds:
                        try:
                            cfg   = await bot.db.get_config(g.id)
                            ch_id = cfg.get("roblox_update_channel_id")
                            if not ch_id:
                                continue
                            ch = g.get_channel(ch_id)
                            if not ch:
                                continue
                            ping_id  = cfg.get("roblox_update_role_id")
                            ping_str = f"<@&{ping_id}>" if ping_id else None
                            await ch.send(content=ping_str, embed=e)
                        except Exception as exc:
                            logger.warning("roblox alert %s: %s", g.name, exc)
        except Exception as exc:
            logger.error("roblox_version_task: %s", exc)

    @tasks.loop(minutes=1)
    async def tempmute_task(self):
        """Check for expired temp-mutes every minute and remove timeouts."""
        try:
            due = await self.db.get_due_tempmutes()
            for doc in due:
                gid = doc.get("guild_id"); uid = doc.get("user_id")
                guild = self.get_guild(gid)
                if not guild: await self.db.remove_tempmute(gid, uid); continue
                member = guild.get_member(uid)
                if member:
                    try:
                        await member.timeout(None, reason="Temp-mute expired")
                        config = await get_config(gid)
                        lc = get_log_channel(guild, config, "mod")
                        if lc:
                            e = make_embed(C_SUCCESS, f"**{member.mention}** temp-mute expired and was lifted automatically.")
                            e.title = "🔊 Temp-Mute Lifted"
                            try: await lc.send(embed=e)
                            except Exception: pass
                    except Exception as exc:
                        logger.warning("tempmute_task unmute %s: %s", uid, exc)
                await self.db.remove_tempmute(gid, uid)
        except Exception as exc:
            logger.warning("tempmute_task: %s", exc)

    @tasks.loop(minutes=2)
    async def tempban_task(self):
        """Check for expired temp-bans every 2 minutes and unban automatically."""
        try:
            due = await self.db.get_due_tempbans()
            for doc in due:
                gid = doc.get("guild_id"); uid = doc.get("user_id")
                guild = self.get_guild(gid)
                if not guild:
                    await self.db.remove_tempban(gid, uid); continue
                try:
                    user = discord.Object(id=uid)
                    await guild.unban(user, reason="Temp-ban expired")
                    config = await get_config(gid)
                    lc = get_log_channel(guild, config, "mod")
                    if lc:
                        e = make_embed(C_SUCCESS,
                            f"Temp-ban for <@{uid}> (`{uid}`) has expired and been lifted.")
                        e.title = "🔓 Temp-Ban Expired"
                        try: await lc.send(embed=e)
                        except Exception: pass
                except discord.NotFound:
                    pass  # Already unbanned manually
                except Exception as exc:
                    logger.warning("tempban_task unban %s: %s", uid, exc)
                await self.db.remove_tempban(gid, uid)
        except Exception as exc:
            logger.warning("tempban_task: %s", exc)

    @cleanup_task.before_loop
    @voice_xp_task.before_loop
    @nightly_task.before_loop
    @ticket_autoclose_task.before_loop
    @giveaway_task.before_loop
    @roblox_version_task.before_loop
    @tempmute_task.before_loop
    @tempban_task.before_loop
    async def before_tasks(self): await self.wait_until_ready()


bot = LXTEBot()

# Per-command cooldowns are set with @commands.cooldown on each command.
# The .ask command gets its own 8s cooldown below.
# No global cooldown — lets users use info commands (.level, .ping, .help) freely.

# ═══════════════════════════════════════════════════════════════════════════════
#  COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
#  HELP SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════

def build_help_embed(category: str, user=None) -> discord.Embed:
    avatar = bot.user.display_avatar.url if bot.user else None
    footer = "Prefix: .  |  Use the menu below to browse categories."

    # ── Home ──────────────────────────────────────────────────────────────────
    if category == "home":
        e = make_embed(C_PRIMARY,
            "**LXTE's Bot** — Feature-rich bot built for the LXTE Clan.\n\n"
            "**Quick Access:**\n"
            "• Type `.help` to view this menu\n"
            "• Current prefix: **`.`**\n\n"
        )
        e.title = "LXTE's AI"
        e.add_field(name="⬆️ Leveling",           value="`.help leveling`",  inline=True)
        e.add_field(name="🔨 Moderation",         value="`.help mod`",       inline=True)
        e.add_field(name="📢 Reports & Cases",    value="`.help cases`",     inline=True)
        e.add_field(name="🎟️ Tickets",            value="`.help tickets`",   inline=True)
        e.add_field(name="🎉 Giveaways",          value="`.help giveaways`", inline=True)
        e.add_field(name="📊 Analytics & Stats",  value="`.help analytics`", inline=True)
        e.add_field(name="💬 Social & Info",      value="`.help social`",    inline=True)
        e.add_field(name="🎭 Roles",              value="`.help roles`",     inline=True)
        e.add_field(name="🛡️ Staff System",       value="`.help staff`",     inline=True)
        e.add_field(name="🔒 Admin",              value="`.help admin`",     inline=True)
        e.add_field(name="​",               value="​",            inline=True)
        e.set_thumbnail(url=avatar)
        e.set_footer(text=footer, icon_url=avatar)
        return e

    # ── Leveling ──────────────────────────────────────────────────────────────
    elif category == "leveling":
        e = make_embed(C_GOLD,
            "**Earning XP**\n"
            f"Messages earn **3–15 XP** (30s cooldown between gains).\n"
            f"+{STREAK_BONUS_XP} XP bonus for daily streak · +{VOICE_XP_PER_TICK} XP/min in voice.\n"
            f"Double XP events multiply all gains by 2×.\n\n"
            "**Commands**\n"
            "`.level [@user]` — view rank card  *(also* `.xp` */* `.card` */* `.profile`*)*\n"
            "`/level [@user]` — slash version of rank card\n"
            "`.lb` — XP leaderboard  *(also* `.leaderboard`*)*\n"
            "`.daily` — claim your daily XP bonus (once per 24h)\n"
            "`.roles` — list all level-up roles and their unlock levels\n\n"
            "**Staff / Admin**\n"
            "`.doublexp <duration>` — start a 2× XP event  *(e.g.* `2h`*,* `1d`*)  (also* `.2xp`*)*\n"
            "`.doublexp off` — end the event early\n"
            "`.syncroles` — force-sync level roles for all members\n"
            "`.admin resetxp <user_id>` — wipe a user's XP *(admin only)*"
        )
        e.title = "⬆️ Leveling & XP"
        e.set_footer(text=footer, icon_url=avatar)
        return e

    # ── Moderation ────────────────────────────────────────────────────────────
    elif category == "mod":
        e = make_embed(C_WARNING,
            "**⚠️ Warn System**\n"
            "`.warn @user <reason>` — issue a warning  *(3 warns = 60 min auto-timeout)*\n"
            "`.warns @user` — view a user's warning history  *(also* `.warnlist`*)*\n"
            "`.clearwarns @user` — clear all warns *(Mod+)*\n\n"
            "**🔨 Bans & Kicks**\n"
            "`.kick @user [reason]` — kick a member *(Mod+)*\n"
            "`.ban @user [reason]` — permanent ban *(Mod+)*\n"
            "`.softban @user [reason]` — ban+unban to wipe recent messages *(Mod+)*\n"
            "`.massban <id1> <id2> …` — ban multiple users at once *(Senior+)*\n"
            "`.tempban @user <duration> [reason]` — timed ban, auto-unbans  *(also* `.tb`*) (Mod+)*\n"
            "`.unban <user_id> [reason]` — unban a user *(Mod+)*\n\n"
            "**🔇 Mutes**\n"
            "`.tempmute @user <duration> [reason]` — timeout with auto-lift  *(also* `.mute` */* `.tm`*)*\n"
            "`.unmute @user` — remove timeout early *(Mod+)*\n\n"
            "**🧹 Messages**\n"
            "`.purge <amount>` — delete last N messages *(Trial: max 30)*\n"
            "`.cleanup @user [amount]` — delete one user's messages, max 500  *(also* `.purgeuser`*)*\n"
            "`.slowmode [seconds]` — set channel slowmode  *(also* `.slow`*)  (0 = off)*\n\n"
            "**🔒 Channel Control**\n"
            "`.lock [reason]` — lock channel to @everyone\n"
            "`.unlock [reason]` — unlock channel\n"
            "`.nuke` — delete and recreate channel (wipes history)\n\n"
            "**✏️ Other**\n"
            "`.nick @user <name>` — change a member's nickname *(Mod+)*\n"
            "`.modstats [@user]` — mod action count breakdown"
        )
        e.title = "🔨 Moderation"
        e.set_footer(text=footer, icon_url=avatar)
        return e

    # ── Cases ─────────────────────────────────────────────────────────────────
    elif category == "cases":
        e = make_embed(C_PRIMARY,
            "Every mod action (warn, kick, ban, mute, tempban) creates a numbered case.\n\n"
            "**Lookup**\n"
            "`.case <number>` — view a specific case by number\n"
            "`.history @user` — all mod actions against a user  *(also* `.modhistory` */* `.mh`*)*\n\n"
            "**Reports**\n"
            "`.report @user <reason>` — anonymously report a member to staff\n"
            "— Deletes your message (identity hidden from embed)\n"
            "— Staff see action buttons: Acknowledge / Mute / Kick / View History\n"
            "— You get a DM confirmation\n"
            "— 60 second cooldown to prevent spam\n\n"
            "**Audit**\n"
            "`.serveraudit` — scan server for security issues  *(also* `.audit`*) (admin only)*\n"
            "Checks: dangerous @everyone perms, new accounts, broken channels, bot permissions\n\n"
            "**Notes**\n"
            "• Cases are stored permanently in the database\n"
            "• Staff can view history on a reported user with one click"
        )
        e.title = "📢 Reports & Cases"
        e.set_footer(text=footer, icon_url=avatar)
        return e

    # ── Tickets ───────────────────────────────────────────────────────────────
    elif category == "tickets":
        e = make_embed(C_INFO,
            "**Opening & Closing**\n"
            "`.ticket` — open a ticket from chat  *(also* `.newticket` */* `.openticket`*)*\n"
            "Click the ticket panel button as normal.\n"
            "`.close` — close this ticket channel *(staff or opener)*\n"
            "— Sends transcript to log · DMs opener a star rating request\n\n"
            "**Staff Controls**\n"
            "`.claim` — claim this ticket (assigns it to you)\n"
            "`.adduser @user` — add a user to this ticket\n"
            "`.removeuser @user` — remove a user from this ticket\n"
            "`.renameticket <name>` — rename the ticket channel  *(also* `.ticketrename`*)*\n"
            "`.priority <low|normal|high|urgent>` — set ticket priority\n"
            "`.transcript` — generate an HTML transcript and DM it to yourself\n\n"
            "**Ratings & Stats**\n"
            "After close, the opener gets a DM with ⭐–⭐⭐⭐⭐⭐ rating buttons.\n"
            "`.ticketstats` — avg rating, total tickets, top staff leaderboard\n"
            "*(also* `.tsstats` */* `.supportstats`*) — requires Manage Server*\n\n"
            "**Staff Apps**\n"
            "`.staffapps [all|pending|accepted|denied]` — list staff applications\n"
            "*(also* `.sapps` */* `.apps`*) — owner/reviewer only*"
        )
        e.title = "🎟️ Tickets"
        e.set_footer(text=footer, icon_url=avatar)
        return e

    # ── Giveaways ─────────────────────────────────────────────────────────────
    elif category == "giveaways":
        e = make_embed(C_GOLD,
            "**Running Giveaways**\n"
            "`.gstart <duration> [winners] <prize>` — start a giveaway\n"
            "Duration formats: `30m`  `1h`  `2d`  `1h30m`\n"
            "Winners defaults to 1. Example: `.gstart 24h 3 Nitro`\n\n"
            "`.gend <message_id>` — end a giveaway early and pick winner(s)\n"
            "`.greroll <message_id>` — reroll and pick a new winner\n"
            "`.glist` — list all active giveaways in this server\n\n"
            "**Entering**\n"
            "Click **🎉 Enter** on the giveaway message to join.\n"
            "Click again to leave.\n\n"
            "**Notes**\n"
            "• Winners are DM'd and pinged in channel\n"
            "• Giveaways persist through bot restarts\n"
            "• Requires Manage Server to run giveaways"
        )
        e.title = "🎉 Giveaways"
        e.set_footer(text=footer, icon_url=avatar)
        return e

    # ── Analytics ─────────────────────────────────────────────────────────────
    elif category == "analytics":
        e = make_embed(C_PRIMARY,
            "**📊 Server Analytics**\n"
            "`.analytics growth` — member count chart over 30 days\n"
            "`.analytics activity` — top 5 most active members this month\n"
            "`.analytics streaks` — daily streak leaderboard\n"
            "*(also* `.serverstats`*)*\n\n"
            "**📈 Leaderboards**\n"
            "`.lb` — XP leaderboard  *(top 10 by XP)*\n"
            "`.boostlb` — top server boosters  *(also* `.boosters`*)*\n"
            "`.invitelb` — top inviters by total invite count\n"
            "`.msglb` — top members by total messages  *(also* `.messagelb`*)*\n\n"
            "**👤 Per-User Info**\n"
            "`.level [@user]` — rank card with XP, level, streak\n"
            "`.msgcheck [@user]` — message count, rank, top channels  *(also* `.msgstats`*)*\n"
            "`.invites [@user]` — invite count and breakdown\n"
            "\n"
            "**🛠️ Admin**\n"
            "`.msgsync [limit]` — backfill message history for tracking  *(also* `.syncmessages`*)*"
        )
        e.title = "📊 Analytics & Stats"
        e.set_footer(text=footer, icon_url=avatar)
        return e

    # ── Social & Info ─────────────────────────────────────────────────────────
    elif category == "social":
        e = make_embed(C_INFO,
            "**💬 Social**\n"
            "`.afk [reason]` — set yourself AFK (auto-cleared when you send a message)\n"
            "`.report @user <reason>` — anonymously report someone to staff\n\n"
            "**ℹ️ Server & User Info**\n"
            "`.serverinfo` — server overview: members, roles, channels, boosts  *(also* `.si`*)*\n"
            "`.userinfo [@user]` — member profile: joined, roles, XP, warns  *(also* `.ui` */* `.whois`*)*\n"
            "`.roleinfo @role` — role details: colour, members, permissions  *(also* `.ri`*)*\n"
            "`.roles` — list all roles in this server with member counts\n\n"
            "**📨 Invites**\n"
            "`.invites [@user]` — see how many people a user has invited\n"
            "`.invitelb` — server-wide invite leaderboard\n"
            "`.resetinvites @user` — reset a user's invite count *(admin)*\n"
            "`.resetallinvites` — reset all invite data *(admin, confirmation required)*\n\n"
            "**🔧 Utility**\n"
            "`.ping` — check bot latency and uptime\n"
            "`.about` — bot info and feature list  *(also* `.info`*)*\n"
            "`.say <message>` — make the bot say something *(mod+)*\n"
            "`.embed <title | body>` — post a custom embed *(mod+)*"
        )
        e.title = "💬 Social & Info"
        e.set_footer(text=footer, icon_url=avatar)
        return e

    # ── Roles ─────────────────────────────────────────────────────────────────
    elif category == "roles":
        e = make_embed(C_PRIMARY,
            "**Manage Roles**\n"
            "`.role @user add <RoleName>` — give a role to a member *(Senior Mod+)*\n"
            "`.role @user remove <RoleName>` — remove a role from a member *(Senior Mod+)*\n\n"
            "**Role Menus**\n"
            "Configured via `.setup` → **🎭 Role Menus**\n"
            "Creates button-based self-assign menus in any channel.\n\n"
            "**Reaction Roles**\n"
            "Configured via `.setup` → **⚡ Reaction Roles**\n"
            "Emoji reactions on a message grant/remove roles.\n\n"
            "**Auto-Roles**\n"
            "Configured via `.setup` → **🤖 Autoroles**\n"
            "Roles assigned automatically on member join."
        )
        e.title = "🎭 Roles"
        e.set_footer(text=footer, icon_url=avatar)
        return e

    # ── Staff System ──────────────────────────────────────────────────────────
    elif category == "staff":
        e = make_embed(C_PRIMARY,
            "Configure staff roles via `.setup` → **🛡️ Staff Roles**\n\n"
            "**👑 Owner / Community Manager**\n"
            "Full access to all bot commands. Exempt from abuse auto-strip.\n\n"
            "**🔵 Senior Moderator**\n"
            "Warn · kick · ban · unban · mute up to 28d · unmute\n"
            "Purge up to 500 · slowmode (set & remove) · clear warns\n"
            "Manage roles via `.role`\n\n"
            "**🟢 Moderator**\n"
            "Identical to Senior Mod.\n\n"
            "**🟡 Trial Moderator**\n"
            "Warn · mute (max 1h) · purge (max 30 msgs) · slowmode (set only)\n"
            "❌ Cannot kick, ban, unban, or remove slowmode.\n\n"
            "**🔒 Fake Permissions**\n"
            "`.setup` → **🔒 Fake Perms** — grant bot-level perms to any role\n"
            "without giving real Discord permissions.\n"
            "Pick role & permissions via dropdown — no typing needed.\n\n"
            f"**🚨 Staff Abuse Protection**\n"
            f"• {STAFF_ABUSE_WARN_THRESH}+ mod actions in {STAFF_ABUSE_WINDOW_SECS}s → public warning posted\n"
            f"• {STAFF_ABUSE_STRIP_THRESH}+ mod actions in {STAFF_ABUSE_WINDOW_SECS}s → all staff roles stripped\n"
            "Managers and server admins are exempt."
        )
        e.title = "🛡️ Staff System"
        e.set_footer(text="Use .setup to configure all staff roles", icon_url=avatar)
        return e

    # ── Admin ─────────────────────────────────────────────────────────────────
    elif category == "admin":
        e = make_embed(C_ERROR,
            "**⚙️ Setup**\n"
            "`.setup` — full interactive server config panel  *(also* `.config`*)*\n"
            "Covers: log channels, welcome, automod, staff roles, tickets, role menus,\n"
            "reaction roles, autoroles, fake perms, giveaway channel.\n\n"
            "**🔧 User Management**\n"
            "`.admin resetxp <user_id>` — reset a user's XP and level to zero\n\n"
            "**📡 System**\n"
            "`.admin status` — RAM, latency, uptime, guild count\n"
            "`.admin health` — ping all external services (DB, web)\n"
            "`.admin synccount` — force member count channel update\n"
            "`.admin unlockraid` — manually lift anti-raid lockdown\n\n"
            "**🌐 Owner-only**\n"
            "`.restart` — restart the bot process\n"
            "`.robloxnotify <#channel>` — set Roblox version update alert channel"
        )
        e.title = "🔒 Admin & Owner"
        e.set_footer(text="Most admin commands require Administrator permission", icon_url=avatar)
        return e

    # ── Home fallback ─────────────────────────────────────────────────────────
    e = make_embed(C_PRIMARY,
        "Use the dropdown below to browse all commands.\n"
        "Prefix: **`.`**   Slash commands also supported where shown.\n\n"
        "🤖 **AI** · ⬆️ **Leveling** · 🔨 **Moderation** · 📢 **Reports & Cases**\n"
        "🎟️ **Tickets** · 🎉 **Giveaways** · 📊 **Analytics** · 💬 **Social & Info**\n"
        "🎭 **Roles** · 🛡️ **Staff System** · 🔒 **Admin**\n"
    )
    e.title = "Help — Command Reference"
    e.set_thumbnail(url=avatar)
    e.set_footer(text=footer, icon_url=avatar)
    return e


class HelpView(discord.ui.View):
    def __init__(self, ctx: commands.Context):
        super().__init__(timeout=120)
        self.ctx      = ctx
        self._message = None

        options = [
            discord.SelectOption(label="Home",             value="home",      emoji="🏠"),
            discord.SelectOption(label="AI",               value="ai",        emoji="🤖"),
            discord.SelectOption(label="Leveling & XP",    value="leveling",  emoji="⬆️"),
            discord.SelectOption(label="Moderation",       value="mod",       emoji="🔨"),
            discord.SelectOption(label="Reports & Cases",  value="cases",     emoji="📢"),
            discord.SelectOption(label="Tickets",          value="tickets",   emoji="🎟️"),
            discord.SelectOption(label="Giveaways",        value="giveaways", emoji="🎉"),
            discord.SelectOption(label="Analytics & Stats",value="analytics", emoji="📊"),
            discord.SelectOption(label="Social & Info",    value="social",    emoji="💬"),
            discord.SelectOption(label="Roles",            value="roles",     emoji="🎭"),
            discord.SelectOption(label="Staff System",     value="staff",     emoji="🛡️"),
        ]
        if ctx.author.guild_permissions.administrator or ctx.author.id == getattr(ctx.bot, "owner_id_int", 0):
            options.append(discord.SelectOption(label="Admin & Owner", value="admin", emoji="🔒"))

        select          = discord.ui.Select(placeholder="Pick a category…", options=options)
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=build_help_embed(interaction.data["values"][0], interaction.client.user),
            view=self,
        )

    async def on_timeout(self):
        if self._message:
            try: await self._message.edit(view=None)
            except Exception: pass


@bot.command(name="help", aliases=["h"])
async def cmd_help(ctx: commands.Context, category: str = "home"):
    valid = {"home", "ai", "leveling", "mod", "cases", "tickets", "giveaways",
             "analytics", "social", "roles", "staff", "admin"}
    cat = category.lower().strip()
    if cat not in valid:
        cat = "home"
    view    = HelpView(ctx)
    message = await ctx.send(embed=build_help_embed(cat, ctx.bot.user), view=view)
    view._message = message


@bot.command(name="staffapps", aliases=["sapps", "apps"])
async def cmd_staff_apps(ctx: commands.Context, status: str = "all"):
    """Owner/reviewer only — list staff applications. Status: all | pending | accepted | denied"""
    if not await _is_app_reviewer(ctx.author, ctx.guild):
        await ctx.send(embed=err("Only the owner or app reviewer role can use this command."))
        return

    query: dict = {"guild_id": ctx.guild.id, "category": "staff"}
    status = status.lower()
    if status in ("pending", "accepted", "denied"):
        query["app_status"] = status

    apps = await bot.db.tickets.find(query).sort("opened_at", -1).to_list(length=20)

    if not apps:
        label = f" with status **{status}**" if status != "all" else ""
        await ctx.send(embed=make_embed(C_INFO, f"No staff applications found{label}."))
        return

    status_icons = {"accepted": "✅", "denied": "❌", "pending": "⏳"}

    e = make_embed(C_GOLD)
    e.title = f"🛡️ Staff Applications — {status.title()}"
    e.description = (
        "Showing up to **20** most recent.\n"
        "Filter: `.staffapps pending` · `.staffapps accepted` · `.staffapps denied`\n\u200b"
    )
    for app in apps:
        uid        = app.get("user_id", 0)
        member     = ctx.guild.get_member(uid)
        name       = member.display_name if member else f"User {uid}"
        app_status = app.get("app_status", "pending")
        icon       = status_icons.get(app_status, "⏳")
        ticket_num = app.get("ticket_id", 0)
        opened     = app.get("opened_at")
        opened_str = f"<t:{int(opened.timestamp())}:R>" if opened else "unknown"
        ch_id      = app.get("channel_id")
        ch_link    = f"<#{ch_id}>" if ch_id else "—"
        e.add_field(
            name=f"{icon} #{ticket_num:04d} — {name}",
            value=f"Status: **{app_status.title()}** | Opened: {opened_str} | Channel: {ch_link}",
            inline=False,
        )
    e.set_footer(text=f"LXTE's AI — Staff Applications • {len(apps)} result(s)")
    await ctx.send(embed=e)


@bot.command(name="robloxnotify", aliases=["rbnoti", "robloxalert"], hidden=True)
@commands.has_permissions(administrator=True)
async def cmd_robloxnotify(ctx: commands.Context, channel: discord.TextChannel = None, role: discord.Role = None):
    args = ctx.message.content.split()[1:]
    if args and args[0].lower() == "off":
        await bot.db.update_config(ctx.guild.id, "roblox_update_channel_id", None)
        await bot.db.update_config(ctx.guild.id, "roblox_update_role_id", None)
        await ctx.send(embed=ok("Roblox update alerts **disabled**.")); return
    if channel is None:
        cfg    = await get_config(ctx.guild.id)
        ch_id  = cfg.get("roblox_update_channel_id")
        rl_id  = cfg.get("roblox_update_role_id")
        ch_obj = ctx.guild.get_channel(ch_id) if ch_id else None
        rl_obj = ctx.guild.get_role(rl_id)    if rl_id else None
        ch_str = ch_obj.mention if ch_obj else "not set"
        rl_str = rl_obj.mention if rl_obj else "none"
        await ctx.send(embed=make_embed(C_INFO, "Channel: " + ch_str + "\nRole ping: " + rl_str)); return
    await bot.db.update_config(ctx.guild.id, "roblox_update_channel_id", channel.id)
    if role:
        await bot.db.update_config(ctx.guild.id, "roblox_update_role_id", role.id)
    else:
        await bot.db.update_config(ctx.guild.id, "roblox_update_role_id", None)
    msg = "Roblox update alerts \u2192 " + channel.mention
    if role:
        msg += " | pinging " + role.mention
    await ctx.send(embed=ok(msg))


@bot.command(name="roles", aliases=["rolelist", "allroles"])
async def cmd_roles(ctx: commands.Context):
    roles = [r.name for r in sorted(ctx.guild.roles, key=lambda r: r.position, reverse=True) if r.name != "@everyone"]
    e = make_embed(C_PRIMARY, "\n".join(roles) or "No roles.")
    e.title = f"Roles [{len(roles)}]"
    await ctx.send(embed=e)

@bot.command(name="syncroles", aliases=["syncr", "rolesync"])
@commands.has_permissions(administrator=True)
async def cmd_syncroles(ctx: commands.Context):
    msg = await ctx.send(embed=make_embed(C_INFO, "⏳ Syncing roles for all members…"))
    config = await get_config(ctx.guild.id)
    autoroles = config.get("autoroles", [])
    fixed = 0
    errors = 0
    for member in ctx.guild.members:
        if member.bot: continue
        try:
            # Auto-roles
            for entry in autoroles:
                role = ctx.guild.get_role(entry.get("role_id"))
                if role and role not in member.roles:
                    await member.add_roles(role, reason="syncroles")
                    fixed += 1
            # Level roles
            data = await bot.db.get_level_data(member.id, ctx.guild.id)
            level = data.get("level", 0)
            if level > 0:
                await apply_level_roles(member, level)
                fixed += 1
        except Exception:
            errors += 1
    e = ok(f"Sync complete.\n**Members checked:** {len(ctx.guild.members)}\n**Role assignments made:** {fixed}\n**Errors:** {errors}")
    await msg.edit(embed=e)
  

@bot.command(name="level", aliases=["xp", "card", "profile"])
async def cmd_level(ctx: commands.Context, target: discord.Member = None):
    if not ctx.guild:
        await ctx.send(embed=err("This command only works in a server.")); return
    target = target or ctx.author
    data   = await bot.db.get_level_data(target.id, ctx.guild.id)
    buf    = await generate_rank_card(target, data)
    if buf:
        await ctx.send(file=discord.File(fp=buf, filename="rank.png"))
    else:
        await ctx.send(embed=err("Rank card unavailable — Pillow not installed on this host."))


@bot.command(name="leaderboard", aliases=["lb"])
async def cmd_lb(ctx: commands.Context):
    if not ctx.guild:
        await ctx.send(embed=err("This command only works in a server.")); return
    rows = await bot.db.get_leaderboard(ctx.guild.id, 10)
    if not rows: await ctx.send(embed=make_embed(C_WARNING, "Nobody has XP yet — start chatting!")); return
    medals = ["🥇","🥈","🥉"]
    lines  = []
    for idx, row in enumerate(rows):
        member = ctx.guild.get_member(row["user_id"])
        name   = member.display_name if member else f"<@{row['user_id']}>"
        level  = row.get("level", calculate_level(row.get("total_xp",0))[0])
        xp     = row.get("total_xp",0)
        streak = row.get("streak",0)
        prefix = medals[idx] if idx < 3 else f"`{idx+1}.`"
        lines.append(f"{prefix} {name} — Lv {level} ({xp:,} XP){f' 🔥{streak}d' if streak >= 3 else ''}")
    e = make_embed(C_GOLD, "\n".join(lines))
    e.title = "⬆️ XP Leaderboard"
    e.set_footer(text="LXTE's AI")
    await ctx.send(embed=e)


@bot.command(name="afk", aliases=["away"])
async def cmd_afk(ctx: commands.Context, *, reason: str = "AFK"):
    _afk_users[ctx.author.id] = (reason[:100], time.time())
    await ctx.send(embed=make_embed(C_WARNING, f"💤 {ctx.author.mention} is now AFK: **{reason[:100]}**"))


@bot.command(name="invites", aliases=["inv", "invite"])
async def cmd_invites(ctx: commands.Context, target: discord.Member = None):
    if not ctx.guild: return
    target = target or ctx.author
    doc     = await bot.db.invites.find_one({"guild_id": ctx.guild.id, "inviter_id": target.id, "invite_code": "__total__"}) or {}
    total   = doc.get("total_invites", 0)
    regular = doc.get("regular", 0)
    left    = doc.get("left", 0)
    fake    = doc.get("fake", 0)
    bonus   = doc.get("bonus", 0)
    e = make_embed(C_SUCCESS,
        f"**{target.mention}** has **{total}** invite{'s' if total != 1 else ''}.\n"
        f"({regular} regular, {left} left, {fake} fake, {bonus} bonus)"
    )
    e.title = f"📨 Invites — {target.display_name}"
    e.set_thumbnail(url=target.display_avatar.url)
    await ctx.send(embed=e)


@bot.command(name="invitelb", aliases=["invlb", "inviteleaderboard"])
async def cmd_invitelb(ctx: commands.Context):
    if not ctx.guild: return
    rows = await bot.db.get_invite_leaderboard(ctx.guild.id, 10)
    if not rows: await ctx.send(embed=make_embed(C_WARNING, "No invite data yet.")); return
    medals = ["🥇","🥈","🥉"]
    lines  = []
    for idx, r in enumerate(rows):
        m       = ctx.guild.get_member(r.get("inviter_id", 0))
        name    = m.mention if m else f"<@{r.get('inviter_id')}>"
        total   = r.get("total_invites", 0)
        regular = r.get("regular", 0)
        left    = r.get("left", 0)
        fake    = r.get("fake", 0)
        bonus   = r.get("bonus", 0)
        prefix  = medals[idx] if idx < 3 else f"`{idx+1}.`"
        lines.append(f"{prefix} {name} • **{total}** invites. ({regular} regular, {left} left, {fake} fake, {bonus} bonus)")
    e = make_embed(C_GOLD, "\n".join(lines))
    e.title = "📨 Invites Leaderboard"
    e.set_footer(text="LXTE's AI")
    await ctx.send(embed=e)


# ─── Message Leaderboard & Check (v17) ────────────────────────────────────────

@bot.command(name="msglb", aliases=["messagelb", "msgleaderboard"])
async def cmd_msglb(ctx: commands.Context):
    """Top 10 users by total messages sent in this server."""
    rows = await bot.db.get_msg_leaderboard(ctx.guild.id, 10)
    if not rows:
        await ctx.send(embed=make_embed(C_WARNING, "No message data yet — start chatting!")); return
    medals = ["🥇","🥈","🥉"]
    lines  = []
    for idx, row in enumerate(rows):
        member = ctx.guild.get_member(row["user_id"])
        name   = member.mention if member else f"<@{row['user_id']}>"
        count  = row.get("total_messages", 0)
        prefix = medals[idx] if idx < 3 else f"`{idx+1}.`"
        lines.append(f"{prefix} {name} • **{count:,}** messages sent.")
    e = make_embed(C_INFO, "\n".join(lines))
    e.title = "💬 Messages Leaderboard"
    e.set_footer(text="LXTE's AI")
    await ctx.send(embed=e)


@bot.command(name="msgcheck", aliases=["msgstats"])
async def cmd_msgcheck(ctx: commands.Context, target: discord.Member = None):
    """Check your own (or another user's) message stats."""
    target = target or ctx.author
    data   = await bot.db.get_msg_data(target.id, ctx.guild.id)
    if not data:
        await ctx.send(embed=make_embed(C_WARNING, f"No message data for **{target.display_name}** yet.")); return

    total    = data.get("total_messages", 0)
    channels = data.get("channels", {})
    first_m  = data.get("first_message")
    last_m   = data.get("last_message")

    # Find top 3 most active channels
    top_chans = sorted(channels.items(), key=lambda x: x[1], reverse=True)[:3]
    chan_lines = []
    for cid, cnt in top_chans:
        ch = ctx.guild.get_channel(int(cid))
        ch_name = ch.mention if ch else f"`#{cid}`"
        chan_lines.append(f"{ch_name} — {cnt:,} msg{'s' if cnt != 1 else ''}")

    # Find rank on leaderboard
    rows = await bot.db.get_msg_leaderboard(ctx.guild.id, 100)
    rank = next((i+1 for i, r in enumerate(rows) if r["user_id"] == target.id), None)

    e = make_embed(C_INFO)
    e.title = f"💬 {target.display_name}'s Message Stats"
    e.set_thumbnail(url=target.display_avatar.url)
    e.add_field(name="Total Messages", value=f"{total:,}", inline=True)
    e.add_field(name="Server Rank",    value=f"#{rank}" if rank else "Unranked", inline=True)
    if first_m: e.add_field(name="First Message", value=ts_full(first_m), inline=True)
    if last_m:  e.add_field(name="Last Message",  value=ts(last_m),       inline=True)
    if chan_lines:
        e.add_field(name="Most Active Channels", value="\n".join(chan_lines), inline=False)
    e.set_footer(text="LXTE's AI • v19")
    await ctx.send(embed=e)


# ─── Message History Sync (v17) ───────────────────────────────────────────────

@bot.command(name="analytics", aliases=["serverstats"])
async def cmd_analytics(ctx: commands.Context, sub: str = "growth"):
    sub = sub.lower()
    if sub == "growth":
        rows = await bot.db.get_member_count_history(ctx.guild.id, 30)
        if not rows: await ctx.send(embed=make_embed(C_WARNING, "No data yet — check back tomorrow.")); return
        lines = [f"`{r.get('date','?')}` — **{r.get('member_count',0):,}** members" for r in rows[-10:]]
        diff  = rows[-1].get("member_count",0) - rows[0].get("member_count",0)
        e = make_embed(C_PRIMARY, "\n".join(lines))
        e.title = "📊 Server Growth (30 days)"
        e.add_field(name="Change",  value=f"{'📈' if diff>=0 else '📉'} {diff:+}", inline=True)
        e.add_field(name="Current", value=f"{ctx.guild.member_count:,}", inline=True)
        await ctx.send(embed=e)
    elif sub == "activity":
        lb = await bot.db.get_leaderboard(ctx.guild.id, 5)
        lines = []
        for r in lb:
            m = ctx.guild.get_member(r["user_id"])
            name = m.display_name if m else str(r["user_id"])
            lines.append(f"• **{name}** — {r.get('messages',0):,} msgs · {r.get('total_xp',0):,} XP")
        e = make_embed(C_INFO)
        e.title = "⚡ Server Activity"
        e.add_field(name="Members",     value=f"{ctx.guild.member_count:,}", inline=True)
        e.add_field(name="Boost Tier",  value=f"Tier {ctx.guild.premium_tier}", inline=True)
        e.add_field(name="Most Active", value="\n".join(lines) or "No data yet", inline=False)
        await ctx.send(embed=e)
    elif sub == "streaks":
        lb      = await bot.db.get_leaderboard(ctx.guild.id, 50)
        sorted_ = sorted(lb, key=lambda r: r.get("streak",0), reverse=True)[:10]
        lines   = []
        for idx, r in enumerate(sorted_):
            if r.get("streak",0) > 0:
                m = ctx.guild.get_member(r["user_id"])
                lines.append(f"`{idx+1}.` **{m.display_name if m else r['user_id']}** — 🔥 {r.get('streak',0)}d")
        e = make_embed(C_GOLD, "\n".join(lines) or "No active streaks!")
        e.title = "🔥 Streak Leaderboard"
        await ctx.send(embed=e)
    else:
        await ctx.send(embed=make_embed(C_INFO, "Options: `.analytics growth` · `.analytics activity` · `.analytics streaks`"))


@bot.command(name="serverinfo", aliases=["si"])
async def cmd_serverinfo(ctx: commands.Context):
    g = ctx.guild
    e = make_embed(C_PRIMARY)
    e.title = g.name
    if g.icon: e.set_thumbnail(url=g.icon.url)
    e.add_field(name="Owner",    value=g.owner.mention if g.owner else "?",                              inline=True)
    e.add_field(name="Created",  value=ts_full(g.created_at),                                            inline=True)
    e.add_field(name="Members",  value=f"{g.member_count:,}",                                            inline=True)
    e.add_field(name="Boost",    value=f"Tier {g.premium_tier} ({g.premium_subscription_count} boosts)", inline=True)
    e.add_field(name="Channels", value=f"💬 {len(g.text_channels)}  🔊 {len(g.voice_channels)}",         inline=True)
    e.add_field(name="Roles",    value=f"{len(g.roles)}",                                                inline=True)
    await ctx.send(embed=e)


@bot.command(name="userinfo", aliases=["ui", "whois"])
async def cmd_userinfo(ctx: commands.Context, target: discord.Member = None):
    target      = target or ctx.author
    data        = await bot.db.get_level_data(target.id, ctx.guild.id)
    total_xp    = data.get("total_xp", 0)
    level, _, _ = calculate_level(total_xp)
    badges      = " ".join(a["emoji"] for a in ACHIEVEMENTS if a["id"] in data.get("badges", [])) or "None"
    e = make_embed(C_PRIMARY)
    e.set_author(name=target.display_name, icon_url=target.display_avatar.url)
    e.set_thumbnail(url=target.display_avatar.url)
    e.add_field(name="ID",       value=f"`{target.id}`",                                                 inline=True)
    e.add_field(name="Created",  value=ts_full(target.created_at),                                       inline=True)
    e.add_field(name="Joined",   value=ts_full(target.joined_at) if target.joined_at else "?",           inline=True)
    e.add_field(name="Boosting", value=ts_full(target.premium_since) if target.premium_since else "No",  inline=True)
    e.add_field(name="Level",    value=f"{level} ({total_xp:,} XP)",                                     inline=True)
    e.add_field(name="Streak",   value=f"🔥 {data.get('streak',0)}d",                                    inline=True)
    e.add_field(name="Badges",   value=badges,                                                            inline=False)
    roles_str = " ".join(r.mention for r in reversed(target.roles) if r.name != "@everyone") or "None"
    e.add_field(name=f"Roles [{len(target.roles)-1}]", value=roles_str[:500], inline=False)
    await ctx.send(embed=e)


@bot.command(name="roleinfo", aliases=["ri"])
async def cmd_roleinfo(ctx: commands.Context, *, role: discord.Role = None):
    if not role: await ctx.send(embed=err("Usage: `.roleinfo @role`")); return
    mc    = sum(1 for m in ctx.guild.members if role in m.roles)
    perms = [p.replace("_"," ").title() for p, v in role.permissions if v]
    e = make_embed(role.color.value or C_PRIMARY)
    e.title = f"@{role.name}"
    e.add_field(name="ID",          value=f"`{role.id}`",   inline=True)
    e.add_field(name="Members",     value=f"{mc}",           inline=True)
    e.add_field(name="Created",     value=ts_full(role.created_at), inline=True)
    e.add_field(name="Hoisted",     value="Yes" if role.hoist else "No",       inline=True)
    e.add_field(name="Mentionable", value="Yes" if role.mentionable else "No", inline=True)
    if perms: e.add_field(name="Key Permissions", value=", ".join(perms[:10]), inline=False)
    await ctx.send(embed=e)


@bot.command(name="purge", aliases=["bulkdelete", "bd"])
@commands.cooldown(rate=1, per=5, type=commands.BucketType.user)
async def cmd_purge(ctx: commands.Context, amount: int = 10):
    if not ctx.guild: return
    config = await get_config(ctx.guild.id)
    if not await _require_mod(ctx, config, "trial"): return
    # Trial mods are capped at TRIAL_MOD_PURGE_MAX messages
    if _has_staff_role(ctx.author, config, "staff_trial_mod_role_id") and        not _is_mod_or_above(ctx.author, config) and        ctx.author.id != bot.owner_id_int:
        if amount > TRIAL_MOD_PURGE_MAX:
            await ctx.send(embed=err(f"Trial Mods can only purge up to **{TRIAL_MOD_PURGE_MAX}** messages at once.")); return
    if amount < 1 or amount > 500:
        await ctx.send(embed=err("Amount must be between 1 and 500."), delete_after=5); return
    if await _check_staff_abuse(ctx, "purge", config): return
    await ctx.message.delete()
    deleted = await ctx.channel.purge(limit=amount)
    await ctx.send(embed=ok(f"Deleted **{len(deleted)}** messages."), delete_after=5)
    # Log to mod channel
    if ctx.guild:
        config = await get_config(ctx.guild.id)
        lc = get_log_channel(ctx.guild, config, "mod")
        if lc:
            e = make_embed(C_WARNING, f"**{ctx.author.mention}** purged **{len(deleted)}** messages in {ctx.channel.mention}.")
            e.title = "🧹 Messages Purged"
            try: await lc.send(embed=e)
            except Exception: pass


# ─── Warn System (v23) ────────────────────────────────────────────────────────

@bot.command(name="warn", aliases=["w"])
async def cmd_warn(ctx: commands.Context, member: discord.Member = None, *, reason: str = "No reason given."):
    """Warn a user. 3 warns = 60min timeout. Usage: .warn @user reason"""
    if not ctx.guild: return
    config = await get_config(ctx.guild.id)
    if not await _require_mod_or_fake(ctx, config, "trial", "manage_messages"): return
    if not member:
        await ctx.send(embed=err("Usage: `.warn @user reason`")); return
    if member.bot:
        await ctx.send(embed=err("Can't warn bots.")); return
    if member.id == ctx.author.id:
        await ctx.send(embed=err("Can't warn yourself.")); return
    if member.top_role >= ctx.guild.me.top_role:
        await ctx.send(embed=err("I can't action that member (their role is too high).")); return
    if await _check_staff_abuse(ctx, "warn", config): return

    warn_count = await bot.db.add_warn(ctx.guild.id, member.id, ctx.author.id, reason)
    case_num   = await bot.db.add_case(ctx.guild.id, "warn", ctx.author.id, member.id, reason)

    e = make_embed(C_WARNING, f"**{member.mention}** has been warned.\nReason: {reason}\nTotal warns: **{warn_count}** | Case: **#{case_num}**")
    e.title = "⚠️ Warning Issued"
    await ctx.send(embed=e)

    try:
        dm = make_embed(C_WARNING, f"You were warned in **{ctx.guild.name}**.\nReason: {reason}\nTotal warns: {warn_count}")
        dm.title = "⚠️ You've Been Warned"
        await member.send(embed=dm)
    except Exception:
        pass

    config = await get_config(ctx.guild.id)
    log_desc = (
        f"**User:** {member.mention} (`{member.id}`)\n"
        f"**Mod:** {ctx.author.mention}\n"
        f"**Reason:** {reason}\n"
        f"**Total Warns:** {warn_count}\n"
        f"**Case:** #{case_num}"
    )
    if warn_count >= WARN_TIMEOUT_THRESHOLD:
        try:
            until_dt = datetime.now(timezone.utc) + timedelta(minutes=WARN_TIMEOUT_MINUTES)
            await member.timeout(timedelta(minutes=WARN_TIMEOUT_MINUTES), reason=f"Auto-timeout: {warn_count} warns")
            await bot.db.add_tempmute(ctx.guild.id, member.id, ctx.bot.user.id, f"Auto-timeout: {warn_count} warns", until_dt)
            await ctx.send(embed=make_embed(C_ERROR,
                f"⏱️ {member.mention} auto-timed out for **{WARN_TIMEOUT_MINUTES} minutes** ({warn_count} warns)."))
            log_desc += f"\n⏱️ *Auto-timed out for {WARN_TIMEOUT_MINUTES} min*"
        except discord.Forbidden:
            await ctx.send(embed=make_embed(C_WARNING, "Couldn't timeout — missing permissions or role too high."))
    _log_mod_action(ctx.guild, config, "⚠️ Warn Issued", log_desc, C_WARNING)


@bot.command(name="warns")
async def cmd_warns(ctx: commands.Context, member: discord.Member = None):
    """View warns for a user. Usage: .warns @user"""
    if not ctx.guild: return
    config = await get_config(ctx.guild.id)
    if not await _require_mod(ctx, config, "trial"): return
    if not member:
        await ctx.send(embed=err("Usage: `.warns @user`")); return
    warns = await bot.db.get_warns(ctx.guild.id, member.id)
    if not warns:
        await ctx.send(embed=make_embed(C_SUCCESS, f"{member.mention} has no warns. Clean record! ✅")); return
    lines = []
    for i, w in enumerate(warns[:10], 1):
        mod = ctx.guild.get_member(w.get("mod_id", 0))
        mod_str = mod.display_name if mod else "Unknown"
        ts_str = w["created_at"].strftime("%Y-%m-%d") if w.get("created_at") else "?"
        lines.append(f"`{i}.` [{ts_str}] by **{mod_str}** — {w.get('reason','?')[:80]}")
    e = make_embed(C_WARNING, "\n".join(lines))
    e.title = f"⚠️ Warns for {member.display_name} ({len(warns)} total)"
    await ctx.send(embed=e)


@bot.command(name="clearwarns", aliases=["cw", "delwarns"])
async def cmd_clearwarns(ctx: commands.Context, member: discord.Member = None):
    """Clear all warns for a user. Usage: .clearwarns @user"""
    if not ctx.guild: return
    config = await get_config(ctx.guild.id)
    if not await _require_mod_or_fake(ctx, config, "mod", "manage_messages"): return
    if not member:
        await ctx.send(embed=err("Usage: `.clearwarns @user`")); return
    count = await bot.db.clear_warns(ctx.guild.id, member.id)
    await ctx.send(embed=ok(f"Cleared **{count}** warn(s) for {member.mention}."))
    config = await get_config(ctx.guild.id)
    _log_mod_action(ctx.guild, config, "🧹 Warns Cleared",
        f"**User:** {member.mention}\n**Cleared by:** {ctx.author.mention}\n**Count:** {count}", C_INFO)


# ─── Daily XP (v23) ───────────────────────────────────────────────────────────

@bot.command(name="setup", aliases=["config"])
@commands.cooldown(rate=1, per=30, type=commands.BucketType.guild)
async def cmd_setup(ctx: commands.Context):
    if not (ctx.author.id == bot.owner_id_int or (ctx.guild and ctx.author.guild_permissions.administrator)):
        await ctx.send(embed=err("Admins only.")); return
    config = await get_config(ctx.guild.id)
    view   = SetupView(bot.owner_id_int, ctx.guild.id)
    msg    = await ctx.send(embed=setup_embed(config, ctx.guild), view=view)
    view._msg = msg


# ─── Ticket text commands ─────────────────────────────────────────────────────

@bot.command(name="ticket", aliases=["newticket", "openticket"])
@commands.cooldown(rate=1, per=10, type=commands.BucketType.user)
async def cmd_ticket(ctx: commands.Context):
    """Open a ticket from a text command (same as clicking the panel button)."""
    if await bot.db.count_open_tickets(ctx.guild.id, ctx.author.id) >= 1:
        await ctx.send(embed=err("You already have an open ticket. Close it first.")); return
    view = TicketCategorySelect()
    await ctx.send(embed=make_embed(C_PRIMARY, "Select a category:"), view=view, delete_after=60)


@bot.command(name="close", aliases=["closeticket", "ct"])
async def cmd_close(ctx: commands.Context):
    """Close the current ticket channel. Usage: .close"""
    ticket_data = await bot.db.get_ticket(ctx.channel.id)
    if not ticket_data:
        await ctx.send(embed=err("This isn't a ticket channel.")); return
    is_staff = (ctx.author.guild_permissions.manage_channels
                or ctx.author.id == ticket_data.get("user_id")
                or ctx.author.id == getattr(bot, 'owner_id_int', 0))
    if not is_staff:
        await ctx.send(embed=err("Only staff or the ticket opener can close this.")); return
    await ctx.send(embed=make_embed(C_WARNING, "🔒 Closing in 5 seconds…"))
    await bot.db.close_ticket(ctx.channel.id)
    _staff_app_sessions.pop(ctx.channel.id, None)
    await _send_transcript_to_log(ctx.channel, ticket_data, ctx.author)
    opener_id = ticket_data.get("user_id")
    ticket_id = ticket_data.get("ticket_id")
    if opener_id and ticket_id and opener_id != ctx.author.id:
        opener = bot.get_user(opener_id) or await bot.fetch_user(opener_id)
        if opener:
            asyncio.create_task(
                send_ticket_rating_dm(opener, ctx.guild.id, ticket_id, ctx.author.id)
            )
    await asyncio.sleep(5)
    try: await ctx.channel.delete(reason=f"Ticket closed by {ctx.author}")
    except Exception: pass


@bot.command(name="transcript", aliases=["trans", "tlog"])
@commands.has_permissions(manage_channels=True)
async def cmd_transcript(ctx: commands.Context):
    """Generate and DM yourself a transcript of the current ticket. Usage: .transcript"""
    ticket_data = await bot.db.get_ticket(ctx.channel.id)
    if not ticket_data:
        await ctx.send(embed=err("This isn't a ticket channel.")); return
    await ctx.send(embed=make_embed(C_INFO, "📄 Generating transcript…"), delete_after=5)
    try:
        html, msg_count = await _build_transcript_html(ctx.channel, ticket_data, ctx.author)
        tid     = ticket_data.get("ticket_id", "?")
        tid_fmt = f"{tid:04d}" if isinstance(tid, int) else str(tid)
        # Post to ticket log channel first
        await _send_transcript_to_log(ctx.channel, ticket_data, ctx.author)
        # Also send directly to requester
        await ctx.send(
            embed=make_embed(C_INFO, f"📄 Transcript — **{msg_count}** messages"),
            file=discord.File(fp=io.BytesIO(html.encode()), filename=f"ticket-{tid_fmt}-transcript.html"),
        )
    except Exception as exc:
        await ctx.send(embed=err(f"Failed to generate transcript: {exc}"))


@bot.command(name="about", aliases=["info"])
async def cmd_about(ctx: commands.Context):
    e = make_embed(C_AI)
    e.title       = "LXTE's AI v24"
    e.description = "Built by AJ. Smart AI with real-time web search, leveling, giveaways, tickets, multi-select setup, reaction roles, automod, anti-raid, boost tracking, invite tracking, analytics."
    e.set_thumbnail(url=bot.user.display_avatar.url if bot.user else None)
    e.add_field(name="Prefix",   value="`.`",                  inline=True)
    e.add_field(name="Memory",   value="Per channel, 14 days", inline=True)
    e.add_field(name="Cooldown", value="5s",                   inline=True)
    e.add_field(name="Real-time", value="🌐 Web search · ☁️ Weather · 💹 Crypto · 🎮 Roblox", inline=False)
    e.set_footer(text=f"{len(bot.guilds)} server(s)  •  Built by AJ  •  v26")
    await ctx.send(embed=e)


# ─── Giveaway commands ────────────────────────────────────────────────────────

@bot.command(name="gstart", aliases=["gs"])
@commands.has_permissions(manage_guild=True)
@commands.cooldown(rate=1, per=10, type=commands.BucketType.guild)
async def cmd_gstart(ctx: commands.Context, duration: str = None, *, prize: str = None):
    if not duration or not prize:
        await ctx.send(embed=err("Usage: `.gstart <time> <prize>`\nExample: `.gstart 1h Robux`")); return

    secs = parse_duration(duration)
    if not secs or secs < 10:
        await ctx.send(embed=err("Invalid duration. Use formats like `30m`, `1h`, `2d`, `1h30m`.")); return
    if secs > 86400 * 30:
        await ctx.send(embed=err("Max duration is 30 days.")); return

    # Optional: check for winners arg at start of prize like "3w Prize name"
    winners = 1
    prize_parts = prize.split(" ", 1)
    if prize_parts[0].endswith("w") and prize_parts[0][:-1].isdigit():
        winners = max(1, min(20, int(prize_parts[0][:-1])))
        prize   = prize_parts[1] if len(prize_parts) > 1 else "Prize"

    config  = await get_config(ctx.guild.id)
    ch_id   = config.get("giveaway_channel_id")
    channel = ctx.guild.get_channel(ch_id) if ch_id else ctx.channel

    ends_at = datetime.now(timezone.utc) + timedelta(seconds=secs)
    e       = giveaway_embed(prize, ctx.author.id, ends_at, winners, 0)
    msg     = await channel.send(embed=e, view=GiveawayEnterView())

    await bot.db.create_giveaway(ctx.guild.id, channel.id, msg.id, ctx.author.id, prize, winners, ends_at)

    if channel != ctx.channel:
        await ctx.send(embed=ok(f"Giveaway started in {channel.mention}! [Jump]({msg.jump_url})"))
    else:
        await ctx.message.delete()


@bot.command(name="gend", aliases=["ge"])
@commands.has_permissions(manage_guild=True)
async def cmd_gend(ctx: commands.Context, message_id: int = None):
    if not message_id:
        await ctx.send(embed=err("Usage: `.gend <message_id>`")); return
    giveaway = await bot.db.get_giveaway(message_id)
    if not giveaway: await ctx.send(embed=err("Giveaway not found.")); return
    if giveaway.get("ended"): await ctx.send(embed=err("That giveaway already ended.")); return
    await bot.db.end_giveaway(message_id)
    await do_end_giveaway(giveaway, ctx.guild)
    if giveaway.get("channel_id") != ctx.channel.id:
        await ctx.send(embed=ok("Giveaway ended."))


@bot.command(name="greroll", aliases=["gr"])
@commands.has_permissions(manage_guild=True)
async def cmd_greroll(ctx: commands.Context, message_id: int = None):
    if not message_id:
        await ctx.send(embed=err("Usage: `.greroll <message_id>`")); return
    giveaway = await bot.db.get_giveaway(message_id)
    if not giveaway: await ctx.send(embed=err("Giveaway not found.")); return
    entrants = giveaway.get("entrants", [])
    if not entrants: await ctx.send(embed=err("No entrants to reroll from.")); return

    num     = min(giveaway.get("winners", 1), len(entrants))
    winners = random.sample(entrants, num)
    mentions = " ".join(f"<@{w}>" for w in winners)
    await ctx.send(embed=make_embed(C_GOLD, f"🎉 New winner(s): {mentions}\nCongratulations on **{giveaway['prize']}**!"))


@bot.command(name="glist", aliases=["gl"])
async def cmd_glist(ctx: commands.Context):
    active = await bot.db.get_active_giveaways(ctx.guild.id)
    if not active: await ctx.send(embed=make_embed(C_INFO, "No active giveaways.")); return
    lines = []
    for g in active:
        ends_ts = int(g["ends_at"].timestamp()) if g.get("ends_at") else 0
        ch      = ctx.guild.get_channel(g.get("channel_id"))
        lines.append(f"• **{g['prize']}** — ends <t:{ends_ts}:R> in {ch.mention if ch else '?'} ({len(g.get('entrants',[]))} entries)")
    e = make_embed(C_GOLD, "\n".join(lines))
    e.title = "🎉 Active Giveaways"
    await ctx.send(embed=e)


# ─── Double XP event command ─────────────────────────────────────────────────

@bot.command(name="doublexp", aliases=["2xp", "xpevent"])
@commands.has_permissions(administrator=True)
async def cmd_doublexp(ctx: commands.Context, duration: str = ""):
    """Start or stop a server-wide double XP event. Usage: !doublexp 2h | !doublexp off"""
    gid = ctx.guild.id

    # !doublexp off — cancel active event
    if duration.lower() in ("off", "stop", "end", "cancel"):
        _doublexp_until[gid] = 0
        await bot.db.update_config(gid, "doublexp_until", None)
        e = make_embed(C_INFO, "Double XP event ended.")
        e.title = "⚡ Double XP Off"
        await ctx.send(embed=e)
        return

    # !doublexp with no argument — show current status
    if not duration:
        remaining = _doublexp_until.get(gid, 0) - time.monotonic()
        if remaining > 0:
            m, s = divmod(int(remaining), 60)
            h, m = divmod(m, 60)
            parts = []
            if h: parts.append(f"{h}h")
            if m: parts.append(f"{m}m")
            parts.append(f"{s}s")
            await ctx.send(embed=make_embed(C_GOLD, f"⚡ Double XP active — **{' '.join(parts)}** remaining."))
        else:
            await ctx.send(embed=make_embed(C_INFO, "No active double XP event.\nUsage: `!doublexp 2h` or `!doublexp 30m`"))
        return

    secs = parse_duration(duration)
    if not secs or secs <= 0:
        await ctx.send(embed=err("Invalid duration. Examples: `!doublexp 1h`, `!doublexp 30m`, `!doublexp 2h30m`"))
        return
    if secs > 86400:
        await ctx.send(embed=err("Max event duration is 24 hours."))
        return

    _doublexp_until[gid] = time.monotonic() + secs
    until_dt = datetime.now(timezone.utc) + timedelta(seconds=secs)
    await bot.db.update_config(gid, "doublexp_until", until_dt.isoformat())
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    parts = []
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s: parts.append(f"{s}s")
    e = make_embed(C_GOLD, f"All members earn **2× XP** for the next **{' '.join(parts)}**! 🚀")
    e.title = "⚡ Double XP Event Started!"
    await ctx.send(embed=e)


# ─── Owner filter toggle ──────────────────────────────────────────────────────


# ─── Admin commands ───────────────────────────────────────────────────────────

@bot.command(name="admin", hidden=True)
async def cmd_admin(ctx: commands.Context, action: str = "status", *args):
    if ctx.author.id != bot.owner_id_int: return
    if action == "status":
        cpu  = psutil.cpu_percent(interval=0.1)
        mem  = psutil.virtual_memory()
        proc = psutil.Process(os.getpid()).memory_info().rss
        desc = (
            f"Guilds   : {len(bot.guilds)}\n"
            f"Members  : {sum(g.member_count for g in bot.guilds):,}\n"
            f"Latency  : {round(bot.latency*1000)}ms\n"
            f"CPU      : {cpu}%\n"
            f"RAM      : {mem.percent}% ({round(mem.used/1048576,1)}/{round(mem.total/1048576,1)} MB)\n"
            f"Bot RAM  : {round(proc/1048576,1)} MB\n"
            f"Uptime   : {format_uptime(bot.start_time)}\n"
            f"Pillow   : {'✅' if PILLOW_AVAILABLE else '❌'}"
        )
        await ctx.send(embed=make_embed(C_INFO, f"```{desc}```"))

    elif action == "resetxp" and args:
        try:
            if not ctx.guild: await ctx.send(embed=err("Server only.")); return
            uid = int(re.sub(r"[<@!>]", "", args[0]))
            await bot.db.reset_xp(uid, ctx.guild.id)
            await ctx.send(embed=ok(f"Reset XP for `{uid}`."))
        except Exception as e: await ctx.send(embed=err(str(e)))

    elif action == "synccount":
        for guild in bot.guilds: await update_member_count(guild)
        await ctx.send(embed=ok("Member counts updated."))

    elif action == "health":
        mongo_ok = await bot.db.ping()
        await ctx.send(embed=make_embed(C_INFO, (
            f"Discord : ✅ {round(bot.latency*1000)}ms\n"
            f"MongoDB : {'✅' if mongo_ok else '❌'}\n"
            f"Pillow  : {'✅' if PILLOW_AVAILABLE else '❌'}"
        )))

    elif action == "unlockraid":
        for guild in bot.guilds:
            await _unlock_server(guild)
            _raid_active[guild.id] = False
            _nuke_active[guild.id] = False
            _join_timestamps[guild.id].clear()
        await ctx.send(embed=ok("All servers unlocked."))

    else:
        await ctx.send(embed=make_embed(C_INFO,
            "`status` `health` `synccount`\n"
            "`resetxp <id>` `unlockraid`"
        ))


# ─── Ping ─────────────────────────────────────────────────────────────────────

@bot.command(name="ping", aliases=["latency", "pong"])
async def cmd_ping(ctx: commands.Context):
    """Check if the bot is alive. Shows latency and prefix."""
    latency = round(bot.latency * 1000)
    filled  = min(int(latency / 20), 10)
    bar     = "▓" * filled + "░" * (10 - filled)
    color   = C_SUCCESS if latency < 100 else (C_WARNING if latency < 250 else C_ERROR)
    e = make_embed(color,
        f"🏓 **Pong!**  `{latency}ms` `[{bar}]`\n\n"
        f"**Prefix:** `.`\n"
        f"**Commands:** `.help` to see everything\n"
        f"**Examples:** `.level` · `.warn @user reason` · `.ticket`"
    )
    e.title = "🤖 LXTE's Bot — Online"
    e.set_footer(text=f"Uptime: {format_uptime(bot.start_time)}")
    await ctx.send(embed=e)


# ─── Slowmode ─────────────────────────────────────────────────────────────────

@bot.command(name="slowmode", aliases=["slow"])
async def cmd_slowmode(ctx: commands.Context, seconds: int = 0):
    """Set channel slowmode. Requires Trial Mod or above. .slowmode 5 = 5s delay. .slowmode 0 = off."""
    if not ctx.guild: return
    config = await get_config(ctx.guild.id)
    if not await _require_mod(ctx, config, "trial"): return
    # Trial mods cannot remove slowmode (0) — only set it
    is_trial_only = (_has_staff_role(ctx.author, config, "staff_trial_mod_role_id")
                     and not _is_mod_or_above(ctx.author, config)
                     and ctx.author.id != bot.owner_id_int
                     and not ctx.author.guild_permissions.administrator)
    if is_trial_only and seconds == 0:
        await ctx.send(embed=err("Trial Mods can set slowmode but cannot remove it. Ask a Mod+.")); return
    if seconds < 0 or seconds > 21600:
        await ctx.send(embed=err("Slowmode must be between 0 and 21600 seconds (6 hours).")); return
    await ctx.channel.edit(slowmode_delay=seconds, reason=f"Slowmode set by {ctx.author}")
    if seconds == 0:
        await ctx.send(embed=ok(f"✅ Slowmode **disabled** in {ctx.channel.mention}."))
    else:
        m, s = divmod(seconds, 60); h, m = divmod(m, 60)
        parts = [p for p in [f"{h}h", f"{m}m", f"{s}s"] if p[0] != "0"]
        await ctx.send(embed=ok(f"⏱️ Slowmode set to **{' '.join(parts) or f'{seconds}s'}** in {ctx.channel.mention}."))


# ─── Kick ──────────────────────────────────────────────────────────────────────

@bot.command(name="kick", aliases=["k"])
async def cmd_kick(ctx: commands.Context, member: discord.Member = None, *, reason: str = "No reason given."):
    """Kick a member. Requires Mod or above. Usage: .kick @user [reason]"""
    if not ctx.guild: return
    config = await get_config(ctx.guild.id)
    if not await _require_mod_or_fake(ctx, config, "mod", "kick_members"): return
    if not member:
        await ctx.send(embed=err("Usage: `.kick @user [reason]`")); return
    if member.bot:
        await ctx.send(embed=err("Can't kick bots with this command.")); return
    if member.id == ctx.author.id:
        await ctx.send(embed=err("You can't kick yourself.")); return
    if member.id == ctx.guild.owner_id:
        await ctx.send(embed=err("The server owner cannot be kicked.")); return
    if member.top_role >= ctx.guild.me.top_role:
        await ctx.send(embed=err("I can't kick that member — their highest role is above mine.")); return
    if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
        await ctx.send(embed=err("You can't kick someone with an equal or higher role than you.")); return
    if await _check_staff_abuse(ctx, "kick", config): return
    try:
        await member.kick(reason=f"By {ctx.author}: {reason}")
    except discord.Forbidden:
        await ctx.send(embed=err("I don't have permission to kick that member.")); return
    # DM after successful kick (best-effort — member is already gone)
    try:
        dm = make_embed(C_ERROR, f"You were **kicked** from **{ctx.guild.name}**.\n**Reason:** {reason}")
        dm.title = "👢 You've Been Kicked"
        await member.send(embed=dm)
    except Exception: pass
    case_num = await bot.db.add_case(ctx.guild.id, "kick", ctx.author.id, member.id, reason)
    e = make_embed(C_WARNING,
        f"**{member.display_name}** (`{member.id}`) has been kicked.\n"
        f"**Reason:** {reason}\n**Case:** #{case_num}")
    e.title = "👢 Member Kicked"
    await ctx.send(embed=e)
    config = await get_config(ctx.guild.id)
    _log_mod_action(ctx.guild, config, "👢 Member Kicked",
        f"**User:** {member} (`{member.id}`)\n**Mod:** {ctx.author.mention}\n"
        f"**Reason:** {reason}\n**Case:** #{case_num}", C_WARNING)


# ─── Ban ───────────────────────────────────────────────────────────────────────

@bot.command(name="ban", aliases=["b"])
async def cmd_ban(ctx: commands.Context, member: discord.Member = None, *, reason: str = "No reason given."):
    """Ban a member. Requires Mod or above. Usage: .ban @user [reason]"""
    if not ctx.guild: return
    config = await get_config(ctx.guild.id)
    if not await _require_mod_or_fake(ctx, config, "mod", "ban_members"): return
    if not member:
        await ctx.send(embed=err("Usage: `.ban @user [reason]`")); return
    if member.bot:
        await ctx.send(embed=err("Can't ban bots with this command.")); return
    if member.id == ctx.author.id:
        await ctx.send(embed=err("You can't ban yourself.")); return
    if member.id == ctx.guild.owner_id:
        await ctx.send(embed=err("The server owner cannot be banned.")); return
    if member.top_role >= ctx.guild.me.top_role:
        await ctx.send(embed=err("I can't ban that member — their highest role is above mine.")); return
    if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
        await ctx.send(embed=err("You can't ban someone with an equal or higher role than you.")); return
    if await _check_staff_abuse(ctx, "ban", config): return
    try:
        await member.ban(reason=f"By {ctx.author}: {reason}", delete_message_days=0)
    except discord.Forbidden:
        await ctx.send(embed=err("I don't have permission to ban that member.")); return
    # DM after successful ban (best-effort — member is banned so DM may fail)
    try:
        dm = make_embed(C_ERROR, f"You were **banned** from **{ctx.guild.name}**.\n**Reason:** {reason}")
        dm.title = "🔨 You've Been Banned"
        await member.send(embed=dm)
    except Exception: pass
    case_num = await bot.db.add_case(ctx.guild.id, "ban", ctx.author.id, member.id, reason)
    e = make_embed(C_ERROR,
        f"**{member.display_name}** (`{member.id}`) has been banned.\n"
        f"**Reason:** {reason}\n**Case:** #{case_num}")
    e.title = "🔨 Member Banned"
    await ctx.send(embed=e)
    config = await get_config(ctx.guild.id)
    _log_mod_action(ctx.guild, config, "🔨 Member Banned",
        f"**User:** {member} (`{member.id}`)\n**Mod:** {ctx.author.mention}\n"
        f"**Reason:** {reason}\n**Case:** #{case_num}", C_ERROR)


# ─── Unban ─────────────────────────────────────────────────────────────────────

@bot.command(name="unban", aliases=["ub"])
async def cmd_unban(ctx: commands.Context, user_id: int = None, *, reason: str = "No reason given."):
    """Unban a user by ID. Requires Mod or above. Usage: .unban <user_id> [reason]"""
    if not ctx.guild: return
    config = await get_config(ctx.guild.id)
    if not await _require_mod(ctx, config, "mod"): return
    if not user_id:
        await ctx.send(embed=err("Usage: `.unban <user_id> [reason]`")); return
    try:
        user = discord.Object(id=user_id)
        await ctx.guild.unban(user, reason=f"By {ctx.author}: {reason}")
        case_num = await bot.db.add_case(ctx.guild.id, "unban", ctx.author.id, user_id, reason)
        e = make_embed(C_SUCCESS,
            f"User `{user_id}` has been unbanned.\n"
            f"**Reason:** {reason}\n**Case:** #{case_num}")
        e.title = "🔓 Member Unbanned"
        await ctx.send(embed=e)
        config = await get_config(ctx.guild.id)
        _log_mod_action(ctx.guild, config, "🔓 Member Unbanned",
            f"**User ID:** `{user_id}`\n**Mod:** {ctx.author.mention}\n**Reason:** {reason}", C_SUCCESS)
    except discord.NotFound:
        await ctx.send(embed=err(f"No ban found for user ID `{user_id}`."))
    except discord.Forbidden:
        await ctx.send(embed=err("I don't have permission to unban members."))


# ─── Temp-Mute ─────────────────────────────────────────────────────────────────

@bot.command(name="tempmute", aliases=["mute", "tm"])
async def cmd_tempmute(ctx: commands.Context, member: discord.Member = None, duration: str = None, *, reason: str = "No reason given."):
    """Temp-mute (timeout) a member with auto-expiry. Requires Trial Mod or above.
    Usage: .tempmute @user 10m [reason]
    Duration: 5m, 1h, 2h30m, 1d (Trial Mods max 1h)"""
    if not ctx.guild: return
    config = await get_config(ctx.guild.id)
    if not await _require_mod_or_fake(ctx, config, "trial", "moderate_members"): return
    if not member or not duration:
        await ctx.send(embed=err(
            "Usage: `.tempmute @user <duration> [reason]`\n"
            "**Examples:**\n"
            "`.tempmute @user 10m Spamming`\n"
            "`.tempmute @user 1h Bad behaviour`\n"
            "`.tempmute @user 1d Repeated violations` *(Mod+ only)*"
        )); return
    if member.bot:
        await ctx.send(embed=err("Can't mute bots.")); return
    if member.id == ctx.author.id:
        await ctx.send(embed=err("You can't mute yourself.")); return
    if member.id == ctx.guild.owner_id:
        await ctx.send(embed=err("The server owner cannot be muted.")); return
    if member.top_role >= ctx.guild.me.top_role:
        await ctx.send(embed=err("I can't mute that member — their role is too high.")); return
    if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
        await ctx.send(embed=err("You can't mute someone with an equal or higher role than you.")); return
    secs = parse_duration(duration)
    if not secs or secs <= 0:
        await ctx.send(embed=err("Invalid duration. Examples: `5m`, `1h`, `2h30m`, `1d`")); return
    # Trial mods: hard cap at 1 hour
    is_trial_only = (_has_staff_role(ctx.author, config, "staff_trial_mod_role_id")
                     and not _is_mod_or_above(ctx.author, config)
                     and ctx.author.id != bot.owner_id_int
                     and not ctx.author.guild_permissions.administrator)
    if is_trial_only and secs > TRIAL_MOD_MUTE_MAX_SECS:
        await ctx.send(embed=err(f"Trial Mods can only mute for up to **1 hour**. Use `.tempmute @user 1h` max.")); return
    if secs > 86400 * 28:
        await ctx.send(embed=err("Max mute duration is 28 days (Discord limit).")); return
    if await _check_staff_abuse(ctx, "tempmute", config): return
    until = datetime.now(timezone.utc) + timedelta(seconds=secs)
    try:
        await member.timeout(timedelta(seconds=secs), reason=f"Tempmute by {ctx.author}: {reason}")
    except discord.Forbidden:
        await ctx.send(embed=err("I don't have permission to timeout that member.")); return
    await bot.db.add_tempmute(ctx.guild.id, member.id, ctx.author.id, reason, until)
    case_num = await bot.db.add_case(ctx.guild.id, "tempmute", ctx.author.id, member.id, reason,
                                      {"duration_secs": secs, "unmute_at": until})
    # Format duration
    m2, s2 = divmod(secs, 60); h2, m2 = divmod(m2, 60); d2, h2 = divmod(h2, 24)
    parts = [p for p in [f"{d2}d", f"{h2}h", f"{m2}m", f"{s2}s"] if not p.startswith("0")]
    dur_str = " ".join(parts) or f"{secs}s"
    e = make_embed(C_WARNING,
        f"**{member.mention}** has been muted for **{dur_str}**.\n"
        f"**Reason:** {reason}\n"
        f"**Auto-unmutes:** <t:{int(until.timestamp())}:R>\n"
        f"**Case:** #{case_num}")
    e.title = "🔇 Member Temp-Muted"
    await ctx.send(embed=e)
    try:
        dm = make_embed(C_WARNING,
            f"You were **temp-muted** in **{ctx.guild.name}** for **{dur_str}**.\n"
            f"**Reason:** {reason}\n**Auto-unmutes:** <t:{int(until.timestamp())}:R>")
        dm.title = "🔇 You've Been Muted"
        await member.send(embed=dm)
    except Exception: pass
    config = await get_config(ctx.guild.id)
    _log_mod_action(ctx.guild, config, "🔇 Temp-Mute Applied",
        f"**User:** {member.mention} (`{member.id}`)\n**Mod:** {ctx.author.mention}\n"
        f"**Duration:** {dur_str}\n**Reason:** {reason}\n"
        f"**Unmutes:** <t:{int(until.timestamp())}:R>\n**Case:** #{case_num}", C_WARNING)


# ─── Unmute ────────────────────────────────────────────────────────────────────

@bot.command(name="unmute", aliases=["um", "untimeout"])
async def cmd_unmute(ctx: commands.Context, member: discord.Member = None, *, reason: str = "Manually unmuted."):
    """Remove a mute/timeout from a member. Requires Mod or above. Usage: .unmute @user"""
    if not ctx.guild: return
    config = await get_config(ctx.guild.id)
    if not await _require_mod(ctx, config, "mod"): return
    if not member:
        await ctx.send(embed=err("Usage: `.unmute @user [reason]`")); return
    try:
        await member.timeout(None, reason=f"Unmuted by {ctx.author}: {reason}")
        await bot.db.remove_tempmute(ctx.guild.id, member.id)
        case_num = await bot.db.add_case(ctx.guild.id, "unmute", ctx.author.id, member.id, reason)
        e = make_embed(C_SUCCESS,
            f"**{member.mention}** has been unmuted.\n**Reason:** {reason}\n**Case:** #{case_num}")
        e.title = "🔊 Member Unmuted"
        await ctx.send(embed=e)
        config = await get_config(ctx.guild.id)
        _log_mod_action(ctx.guild, config, "🔊 Unmuted",
            f"**User:** {member.mention}\n**Mod:** {ctx.author.mention}\n**Reason:** {reason}", C_SUCCESS)
    except discord.Forbidden:
        await ctx.send(embed=err("I don't have permission to remove that member's timeout."))


# ─── Case System ───────────────────────────────────────────────────────────────

_CASE_EMOJIS = {
    "warn": "⚠️", "kick": "👢", "ban": "🔨",
    "unban": "🔓", "tempmute": "🔇", "unmute": "🔊",
}

@bot.command(name="case", aliases=["c", "modcase"])
@commands.has_permissions(manage_messages=True)
async def cmd_case(ctx: commands.Context, num: int = None):
    """Look up a mod case by number. Usage: .case 12"""
    if num is None:
        await ctx.send(embed=err("Usage: `.case <number>`\nExample: `.case 12`")); return
    case = await bot.db.get_case(ctx.guild.id, num)
    if not case:
        await ctx.send(embed=err(f"No case #{num} found in this server.")); return
    action  = case.get("action", "?")
    mod     = ctx.guild.get_member(case.get("mod_id", 0))
    target  = ctx.guild.get_member(case.get("target_id", 0))
    emoji   = _CASE_EMOJIS.get(action, "📋")
    created = case.get("created_at")
    ts_str  = f"<t:{int(created.timestamp())}:F>" if created else "unknown"
    target_str = target.mention if target else f"ID: `{case.get('target_id', '?')}`  *(left server)*"
    mod_str    = mod.mention if mod else f"ID: `{case.get('mod_id', '?')}`  *(left server)*"
    e = make_embed(C_INFO,
        f"**Action:** {emoji} {action.title()}\n"
        f"**Target:** {target_str}\n"
        f"**Moderator:** {mod_str}\n"
        f"**Reason:** {case.get('reason', 'No reason given.')}\n"
        f"**Date:** {ts_str}"
    )
    if action == "tempmute":
        secs = case.get("duration_secs", 0)
        m2, s2 = divmod(secs, 60); h2, m2 = divmod(m2, 60); d2, h2 = divmod(h2, 24)
        dur_parts = [p for p in [f"{d2}d", f"{h2}h", f"{m2}m", f"{s2}s"] if not p.startswith("0")]
        e.add_field(name="Duration", value=" ".join(dur_parts) or "?", inline=True)
        unmute_at = case.get("unmute_at")
        if unmute_at:
            e.add_field(name="Unmuted at", value=f"<t:{int(unmute_at.timestamp())}:R>", inline=True)
    e.title = f"📋 Case #{num}"
    e.set_footer(text="LXTE's AI — Case System")
    await ctx.send(embed=e)


# ─── History ───────────────────────────────────────────────────────────────────

@bot.command(name="history", aliases=["modhistory", "mh"])
@commands.has_permissions(manage_messages=True)
async def cmd_history(ctx: commands.Context, member: discord.Member = None):
    """Show all mod actions against a user. Usage: .history @user"""
    if not member:
        await ctx.send(embed=err("Usage: `.history @user`")); return
    cases = await bot.db.get_user_cases(ctx.guild.id, member.id, 20)
    if not cases:
        await ctx.send(embed=make_embed(C_SUCCESS,
            f"**{member.display_name}** has a clean record — no mod actions found. ✅")); return
    lines = []
    for c in cases:
        action  = c.get("action", "?")
        emoji   = _CASE_EMOJIS.get(action, "📋")
        mod     = ctx.guild.get_member(c.get("mod_id", 0))
        mod_str = mod.display_name if mod else f"`{c.get('mod_id', '?')}`"
        created = c.get("created_at")
        ts_str  = f"<t:{int(created.timestamp())}:d>" if created else "?"
        reason  = (c.get("reason") or "?")[:60]
        lines.append(
            f"{emoji} **#{c['case_number']}** {action.title()} by {mod_str} — {ts_str}\n"
            f"> {reason}"
        )
    e = make_embed(C_WARNING, "\n".join(lines))
    e.title = f"📋 Mod History — {member.display_name}  ({len(cases)} action{'s' if len(cases) != 1 else ''})"
    e.set_thumbnail(url=member.display_avatar.url)
    e.set_footer(text="LXTE's AI — Showing last 20 actions • use .case <num> for details")
    await ctx.send(embed=e)


# ═══════════════════════════════════════════════════════════════════════════════
#  NEW COMMANDS — v26
# ═══════════════════════════════════════════════════════════════════════════════

# ─── Bot Customization (owner only) ──────────────────────────────────────────


# ─── Channel Management ───────────────────────────────────────────────────────

@bot.command(name="lock", aliases=["lc"])
async def cmd_lock(ctx: commands.Context, channel: discord.TextChannel = None, *, reason: str = "Locked by staff."):
    if not ctx.guild: return
    config = await get_config(ctx.guild.id)
    if not await _require_mod_or_fake(ctx, config, "mod", "manage_messages"): return
    ch = channel or ctx.channel
    ow = ch.overwrites_for(ctx.guild.default_role)
    ow.send_messages = False
    try:
        await ch.edit(overwrites={ctx.guild.default_role: ow}, reason=reason)
        await ctx.send(embed=ok(f"🔒 {ch.mention} locked. Reason: {reason}"))
        _log_mod_action(ctx.guild, config, "🔒 Channel Locked",
            f"**Channel:** {ch.mention}\n**By:** {ctx.author.mention}\n**Reason:** {reason}")
    except discord.Forbidden:
        await ctx.send(embed=err("Missing permissions."))

@bot.command(name="unlock", aliases=["ulc"])
async def cmd_unlock(ctx: commands.Context, channel: discord.TextChannel = None, *, reason: str = "Unlocked by staff."):
    if not ctx.guild: return
    config = await get_config(ctx.guild.id)
    if not await _require_mod_or_fake(ctx, config, "mod", "manage_messages"): return
    ch = channel or ctx.channel
    ow = ch.overwrites_for(ctx.guild.default_role)
    ow.send_messages = None
    try:
        await ch.edit(overwrites={ctx.guild.default_role: ow}, reason=reason)
        await ctx.send(embed=ok(f"🔓 {ch.mention} unlocked."))
        _log_mod_action(ctx.guild, config, "🔓 Channel Unlocked",
            f"**Channel:** {ch.mention}\n**By:** {ctx.author.mention}\n**Reason:** {reason}")
    except discord.Forbidden:
        await ctx.send(embed=err("Missing permissions."))

@bot.command(name="nick", aliases=["nickname", "setnickname"])
async def cmd_nick(ctx: commands.Context, member: discord.Member = None, *, nick: str = None):
    if not ctx.guild: return
    config = await get_config(ctx.guild.id)
    if not await _require_mod_or_fake(ctx, config, "mod", "manage_nicknames"): return
    if not member: await ctx.send(embed=err("Usage: `.nick @user [nickname | clear]`")); return
    try:
        new = None if not nick or nick.lower() in ("clear", "reset", "none") else nick[:32]
        await member.edit(nick=new, reason=f"Nick changed by {ctx.author}")
        await ctx.send(embed=ok(f"✅ Nickname {'cleared' if not new else f'set to **{new}**'} for {member.mention}."))
        _log_mod_action(ctx.guild, config, "✏️ Nickname Changed",
            f"**User:** {member.mention}\n**Nick:** `{new or 'cleared'}`\n**By:** {ctx.author.mention}")
    except discord.Forbidden:
        await ctx.send(embed=err("Missing permissions or target is higher in hierarchy."))


# ─── Softban / Massban ────────────────────────────────────────────────────────

@bot.command(name="role", aliases=["giverole"])
async def cmd_role(ctx: commands.Context, action: str = None, member: discord.Member = None, *, role_name: str = None):
    if not ctx.guild: return
    config = await get_config(ctx.guild.id)
    if not await _require_mod_or_fake(ctx, config, "senior", "manage_roles"): return
    if action not in ("add", "remove", "give", "take") or not member or not role_name:
        await ctx.send(embed=err("Usage: `.role add @user RoleName` | `.role remove @user RoleName`")); return
    role = discord.utils.find(lambda r: r.name.lower() == role_name.lower(), ctx.guild.roles)
    if not role: await ctx.send(embed=err(f"Role `{role_name}` not found.")); return
    if role >= ctx.guild.me.top_role: await ctx.send(embed=err("That role is above my highest role.")); return
    try:
        if action in ("add", "give"):
            await member.add_roles(role, reason=f"Role given by {ctx.author}")
            await ctx.send(embed=ok(f"✅ Added **{role.name}** to {member.mention}."))
        else:
            await member.remove_roles(role, reason=f"Role removed by {ctx.author}")
            await ctx.send(embed=ok(f"✅ Removed **{role.name}** from {member.mention}."))
        _log_mod_action(ctx.guild, config, "🎭 Role Updated",
            f"**User:** {member.mention}\n**Role:** {role.mention} ({'added' if action in ('add','give') else 'removed'})\n**By:** {ctx.author.mention}")
    except discord.Forbidden:
        await ctx.send(embed=err("Missing permissions to manage that role."))


# ─── Mod Stats / Warnings alias ──────────────────────────────────────────────

class EmbedBuilderModal(discord.ui.Modal, title="Create Embed"):
    embed_title   = discord.ui.TextInput(label="Title", max_length=100, required=False)
    embed_desc    = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, max_length=2000)
    embed_color   = discord.ui.TextInput(label="Color hex (e.g. 5865F2)", max_length=8, required=False, default="5865F2")
    embed_footer  = discord.ui.TextInput(label="Footer (optional)", max_length=100, required=False)
    embed_channel = discord.ui.TextInput(label="Channel ID (blank = here)", max_length=20, required=False)
    async def on_submit(self, i: discord.Interaction):
        try: color = int(self.embed_color.value.lstrip("#") or "5865F2", 16)
        except ValueError: color = C_PRIMARY
        e = discord.Embed(description=self.embed_desc.value, color=color)
        if self.embed_title.value:  e.title = self.embed_title.value
        if self.embed_footer.value: e.set_footer(text=self.embed_footer.value)
        try:
            ch_id = int(self.embed_channel.value.strip()) if self.embed_channel.value.strip() else 0
            ch = i.guild.get_channel(ch_id) or i.channel
            await ch.send(embed=e)
            await i.response.send_message(embed=ok("✅ Embed sent."), ephemeral=True)
        except Exception as exc:
            await i.response.send_message(embed=err(f"Failed: `{exc}`"), ephemeral=True)

    class EmbedTrigger(discord.ui.View):
        def __init__(self): super().__init__(timeout=60)
        @discord.ui.button(label="📝 Open Builder", style=discord.ButtonStyle.primary)
        async def open_modal(self, i: discord.Interaction, b): await i.response.send_modal(EmbedBuilderModal())
    await ctx.send("Click to open the embed builder:", view=EmbedTrigger(), delete_after=60)

@bot.command(name="resetinvites", aliases=["rinv"])
async def cmd_resetinvites(ctx: commands.Context, member: discord.Member = None):
    """Reset a single member's invite count. Senior Mod+. Usage: .resetinvites @user"""
    if not ctx.guild: return
    config = await get_config(ctx.guild.id)
    if not await _require_mod(ctx, config, "senior"): return
    if not member:
        await ctx.send(embed=err("Usage: `.resetinvites @user`")); return
    await bot.db.reset_invites(ctx.guild.id, member.id)
    e = make_embed(C_SUCCESS, f"Invite count for {member.mention} has been reset to **0**.")
    e.title = "📨 Invites Reset"
    await ctx.send(embed=e)

@bot.command(name="resetallinvites")
@commands.has_permissions(administrator=True)
async def cmd_resetallinvites(ctx: commands.Context):
    """Reset everyone's invite counts (owner excluded). Admin only. Usage: .resetallinvites"""
    if not ctx.guild: return
    if ctx.author.id != bot.owner_id_int and not ctx.author.guild_permissions.administrator:
        await ctx.send(embed=err("Administrator only.")); return

    e = make_embed(C_WARNING,
        "This will reset **everyone's** invite counts to zero — including regular, fake, left, and bonus.\n"
        "The server owner's invites will be **preserved**.\n\n"
        "React ✅ to confirm or ❌ to cancel."
    )
    e.title = "⚠️ Reset All Invites"
    conf = await ctx.send(embed=e)
    await conf.add_reaction("✅")
    await conf.add_reaction("❌")

    def check(r, u):
        return u == ctx.author and str(r.emoji) in ("✅", "❌") and r.message.id == conf.id

    try:
        reaction, _ = await bot.wait_for("reaction_add", timeout=30, check=check)
    except asyncio.TimeoutError:
        await conf.edit(embed=make_embed(C_WARNING, "Timed out — no changes made.")); return

    if str(reaction.emoji) == "❌":
        await conf.edit(embed=make_embed(C_WARNING, "Cancelled — no changes made.")); return

    count = await bot.db.reset_all_invites(ctx.guild.id, exclude_user_id=bot.owner_id_int)
    done = make_embed(C_SUCCESS,
        f"Successfully reset invite counts for **{count}** member(s).\n"
        f"The owner's invite data was preserved."
    )
    done.title = "✅ Invites Reset"
    await conf.edit(embed=done)


# ─── Owner / Multi-guild Tools ────────────────────────────────────────────────

@bot.command(name="restart", aliases=["reboot"], hidden=True)
async def cmd_restart(ctx: commands.Context):
    if ctx.author.id != bot.owner_id_int: await ctx.message.delete(); return
    await ctx.send(embed=make_embed(C_WARNING, "🔄 Restarting…"))
    import sys, os as _os
    await bot.close()
    _os.execv(sys.executable, [sys.executable] + sys.argv)


@bot.command(name="tempban", aliases=["tb"])
async def cmd_tempban(ctx: commands.Context, member: discord.Member = None,
                      duration: str = None, *, reason: str = "No reason given."):
    """
    Temporarily ban a member. Auto-unbans after the duration.
    Usage: .tempban @user 7d reason
    Duration: 10m 1h 2d 1w (max 28d)
    Requires Mod or above.
    """
    if not ctx.guild: return
    config = await get_config(ctx.guild.id)
    if not await _require_mod_or_fake(ctx, config, "mod", "ban_members"): return

    if not member or not duration:
        await ctx.send(embed=err(
            "Usage: `.tempban @user <duration> [reason]`\n"
            "Examples:\n"
            "`.tempban @user 1d Raiding`\n"
            "`.tempban @user 7d Repeated violations`\n"
            "`.tempban @user 1h Trolling` *(short bans)*"
        )); return

    if member.bot:
        await ctx.send(embed=err("Can't temp-ban bots.")); return
    if member.id == ctx.author.id:
        await ctx.send(embed=err("Can't ban yourself.")); return
    if member.id == ctx.guild.owner_id:
        await ctx.send(embed=err("Can't ban the server owner.")); return
    if member.top_role >= ctx.guild.me.top_role:
        await ctx.send(embed=err("Their role is too high for me to ban.")); return
    if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
        await ctx.send(embed=err("You can't ban someone with an equal or higher role.")); return

    secs = parse_duration(duration)
    if not secs or secs <= 0:
        await ctx.send(embed=err("Invalid duration. Examples: `1h`, `7d`, `2d12h`")); return
    if secs > 86400 * 28:
        await ctx.send(embed=err("Max temp-ban is 28 days.")); return

    unban_at = datetime.now(timezone.utc) + timedelta(seconds=secs)

    d2, rem = divmod(secs, 86400); h2, rem = divmod(rem, 3600); m2, s2 = divmod(rem, 60)
    parts = [p for p in [f"{d2}d", f"{h2}h", f"{m2}m", f"{s2}s"] if not p.startswith("0")]
    dur_str = " ".join(parts) or f"{secs}s"

    try:
        dm = make_embed(C_ERROR,
            f"You have been **temp-banned** from **{ctx.guild.name}** for **{dur_str}**.\n"
            f"**Reason:** {reason}\n"
            f"**Unbanned automatically:** <t:{int(unban_at.timestamp())}:R>")
        dm.title = "🔨 Temp-Ban"
        await member.send(embed=dm)
    except Exception: pass

    try:
        await ctx.guild.ban(member, reason=f"Temp-ban by {ctx.author} ({dur_str}): {reason}",
                            delete_message_days=0)
    except discord.Forbidden:
        await ctx.send(embed=err("I don't have permission to ban that member.")); return

    await bot.db.add_tempban(ctx.guild.id, member.id, ctx.author.id, reason, unban_at)
    case_num = await bot.db.add_case(ctx.guild.id, "tempban", ctx.author.id, member.id, reason,
                                     {"duration_secs": secs, "unban_at": unban_at})

    e = make_embed(C_ERROR,
        f"**{member.display_name}** (`{member.id}`) temp-banned for **{dur_str}**.\n"
        f"**Reason:** {reason}\n"
        f"**Auto-unbans:** <t:{int(unban_at.timestamp())}:R>\n"
        f"**Case:** #{case_num}")
    e.title = "🔨 Member Temp-Banned"
    await ctx.send(embed=e)

    config = await get_config(ctx.guild.id)
    _log_mod_action(ctx.guild, config, "🔨 Temp-Ban",
        f"**User:** {member} (`{member.id}`)\n**Mod:** {ctx.author.mention}\n"
        f"**Duration:** {dur_str}\n**Reason:** {reason}\n"
        f"**Unbans:** <t:{int(unban_at.timestamp())}:R>\n**Case:** #{case_num}", C_ERROR)


# ─── Report System ─────────────────────────────────────────────────────────────

class ReportActionView(discord.ui.View):
    def __init__(self, reporter_id: int, target_id: int, guild_id: int):
        super().__init__(timeout=None)
        self.reporter_id = reporter_id
        self.target_id   = target_id
        self.guild_id    = guild_id

    @discord.ui.button(label="✅ Acknowledged", style=discord.ButtonStyle.success,
                        custom_id="report:ack")
    async def btn_ack(self, interaction: discord.Interaction, button: discord.ui.Button):
        button.disabled = True
        button.label    = f"✅ Ack'd by {interaction.user.display_name}"
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            embed=make_embed(C_SUCCESS, f"Report acknowledged by {interaction.user.mention}."),
            ephemeral=True)

    @discord.ui.button(label="🔇 Mute Target", style=discord.ButtonStyle.danger,
                        custom_id="report:mute")
    async def btn_mute(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild  = interaction.guild
        target = guild.get_member(self.target_id)
        if not target:
            await interaction.response.send_message(
                embed=err("Target not found — they may have left."), ephemeral=True); return
        try:
            await target.timeout(timedelta(minutes=60),
                                  reason=f"Muted via report by {interaction.user}")
            for item in self.children: item.disabled = True
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                embed=ok(f"Muted {target.mention} for 1 hour."), ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=err("Missing permissions."), ephemeral=True)

    @discord.ui.button(label="👢 Kick Target", style=discord.ButtonStyle.danger,
                        custom_id="report:kick")
    async def btn_kick(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild  = interaction.guild
        target = guild.get_member(self.target_id)
        if not target:
            await interaction.response.send_message(
                embed=err("Target not found."), ephemeral=True); return
        try:
            await target.kick(reason=f"Kicked via report by {interaction.user}")
            for item in self.children: item.disabled = True
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                embed=ok(f"Kicked {target.display_name}."), ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=err("Missing permissions."), ephemeral=True)

    @discord.ui.button(label="🔍 View History", style=discord.ButtonStyle.secondary,
                        custom_id="report:history")
    async def btn_history(self, interaction: discord.Interaction, button: discord.ui.Button):
        cases = await bot.db.get_user_cases(self.guild_id, self.target_id, 10)
        if not cases:
            await interaction.response.send_message(
                embed=make_embed(C_SUCCESS, "No prior mod actions on this user."),
                ephemeral=True); return
        lines = []
        for c in cases:
            emoji  = _CASE_EMOJIS.get(c.get("action", "?"), "📋")
            ts_str = f"<t:{int(c['created_at'].timestamp())}:d>" if c.get("created_at") else "?"
            lines.append(f"{emoji} **#{c['case_number']}** {c.get('action','?')} — {ts_str} — {(c.get('reason') or '?')[:60]}")
        e = make_embed(C_WARNING, "\n".join(lines))
        e.title = f"📋 History — Last {len(cases)} actions"
        await interaction.response.send_message(embed=e, ephemeral=True)


@bot.command(name="report", aliases=["rep"])
@commands.cooldown(rate=1, per=60, type=commands.BucketType.user)
async def cmd_report(ctx: commands.Context, target: discord.Member = None, *, reason: str = None):
    """
    Anonymously report a member to staff.
    Usage: .report @user <reason>
    """
    if not ctx.guild: return
    if not target:
        await ctx.send(embed=err("Usage: `.report @user <reason>`"), delete_after=8); return
    if not reason:
        await ctx.send(embed=err("Please include a reason: `.report @user <reason>`"),
                       delete_after=8); return
    if target.id == ctx.author.id:
        await ctx.send(embed=err("You can't report yourself."), delete_after=8); return
    if target.bot:
        await ctx.send(embed=err("Can't report bots."), delete_after=8); return

    config    = await get_config(ctx.guild.id)
    log_ch_id = (config.get("report_channel_id")
                 or config.get("mod_log_channel_id")
                 or config.get("automod_log_channel_id")
                 or config.get("log_channel_id"))
    log_ch    = ctx.guild.get_channel(log_ch_id) if log_ch_id else None

    if not log_ch:
        await ctx.send(embed=err(
            "No staff log channel is configured. Ask an admin to set one up via `.setup`."),
            delete_after=10); return

    try: await ctx.message.delete()
    except Exception: pass

    report_id = await bot.db.add_report(ctx.guild.id, ctx.author.id, target.id, reason)

    e = make_embed(C_WARNING,
        f"**Reported User:** {target.mention} (`{target.id}`)\n"
        f"**Reason:** {reason}\n\n"
        f"*Reporter identity is hidden. Use `.case` history to investigate.*\n"
        f"Report ID: `{report_id}`")
    e.title = "🚨 New Member Report"
    e.set_thumbnail(url=target.display_avatar.url)
    e.set_footer(text=f"Guild: {ctx.guild.name}")

    view = ReportActionView(ctx.author.id, target.id, ctx.guild.id)
    try:
        await log_ch.send(embed=e, view=view)
    except Exception as exc:
        logger.warning("report: failed to send to log: %s", exc)

    try:
        conf = make_embed(C_SUCCESS,
            f"Your report against **{target.display_name}** has been sent to staff.\n"
            f"We'll look into it. Thank you for keeping the server safe.")
        conf.title = "✅ Report Submitted"
        await ctx.author.send(embed=conf)
    except Exception: pass


# ─── Cleanup (purge by user) ──────────────────────────────────────────────────

@bot.command(name="cleanup", aliases=["purgeuser", "cleanuser"])
@commands.cooldown(rate=1, per=10, type=commands.BucketType.user)
async def cmd_cleanup(ctx: commands.Context, target: discord.Member = None, amount: int = 50):
    """
    Delete messages from a specific user in the current channel.
    Usage: .cleanup @user [amount]  (max 500, default 50, requires Mod)
    """
    if not ctx.guild: return
    config = await get_config(ctx.guild.id)
    if not await _require_mod_or_fake(ctx, config, "mod", "manage_messages"): return

    if not target:
        await ctx.send(embed=err("Usage: `.cleanup @user [amount]`\nExample: `.cleanup @user 100`")); return

    amount = max(1, min(amount, 500))
    await ctx.message.delete()
    status = await ctx.send(embed=make_embed(C_INFO, f"⏳ Scanning for messages from {target.mention}…"))

    def is_target(msg: discord.Message) -> bool:
        return msg.author.id == target.id

    try:
        deleted_msgs = await ctx.channel.purge(limit=500, check=is_target, bulk=True)
        deleted = len(deleted_msgs)
    except discord.Forbidden:
        await status.edit(embed=err("Missing Manage Messages permission.")); return
    except Exception as exc:
        await status.edit(embed=err(f"Failed: `{exc}`")); return

    await status.edit(embed=ok(f"Deleted **{deleted}** message(s) from {target.mention}."))

    config = await get_config(ctx.guild.id)
    _log_mod_action(ctx.guild, config, "🧹 User Cleanup",
        f"**Target:** {target.mention} (`{target.id}`)\n"
        f"**Deleted:** {deleted} messages\n"
        f"**Channel:** {ctx.channel.mention}\n"
        f"**By:** {ctx.author.mention}", C_WARNING)


# ─── Unban All ────────────────────────────────────────────────────────────────


# ─── Server Audit ─────────────────────────────────────────────────────────────

_AUDIT_DANGEROUS_PERMS = (
    "administrator", "ban_members", "kick_members", "manage_guild",
    "manage_roles", "manage_channels", "manage_webhooks", "mention_everyone",
)

class TicketRatingView(discord.ui.View):
    def __init__(self, guild_id: int, ticket_id: int, closer_id: int):
        super().__init__(timeout=3600)
        self.guild_id  = guild_id
        self.ticket_id = ticket_id
        self.closer_id = closer_id
        self.rated     = False

    async def _submit_rating(self, interaction: discord.Interaction, stars: int):
        if self.rated:
            await interaction.response.send_message("You already rated this ticket.", ephemeral=True)
            return
        self.rated = True
        for item in self.children:
            item.disabled = True
        await bot.db.rate_ticket(self.guild_id, self.ticket_id, interaction.user.id,
                                  self.closer_id, stars)
        star_str = "⭐" * stars + "☆" * (5 - stars)
        await interaction.response.edit_message(
            embed=make_embed(C_SUCCESS,
                f"Thanks for rating! You gave **{star_str}** ({stars}/5)\n"
                f"Your feedback helps us improve."),
            view=self)

    @discord.ui.button(label="⭐", style=discord.ButtonStyle.secondary, custom_id="rate:1")
    async def r1(self, i, b): await self._submit_rating(i, 1)

    @discord.ui.button(label="⭐⭐", style=discord.ButtonStyle.secondary, custom_id="rate:2")
    async def r2(self, i, b): await self._submit_rating(i, 2)

    @discord.ui.button(label="⭐⭐⭐", style=discord.ButtonStyle.secondary, custom_id="rate:3")
    async def r3(self, i, b): await self._submit_rating(i, 3)

    @discord.ui.button(label="⭐⭐⭐⭐", style=discord.ButtonStyle.secondary, custom_id="rate:4")
    async def r4(self, i, b): await self._submit_rating(i, 4)

    @discord.ui.button(label="⭐⭐⭐⭐⭐", style=discord.ButtonStyle.success, custom_id="rate:5")
    async def r5(self, i, b): await self._submit_rating(i, 5)


async def send_ticket_rating_dm(user: discord.User, guild_id: int, ticket_id: int,
                                 closer_id: int):
    try:
        e = make_embed(C_PRIMARY,
            "Your ticket has been closed. How was the support you received?\n\n"
            "Click a star rating below (you have 1 hour).")
        e.title = "🎫 Rate Your Support Experience"
        view = TicketRatingView(guild_id, ticket_id, closer_id)
        await user.send(embed=e, view=view)
    except Exception:
        pass


@bot.command(name="ticketstats", aliases=["tsstats", "supportstats"])
@commands.has_permissions(manage_guild=True)
async def cmd_ticketstats(ctx: commands.Context):
    """Show ticket statistics: average rating, totals, top staff. Usage: .ticketstats"""
    if not ctx.guild: return

    stats = await bot.db.get_ticket_stats(ctx.guild.id)

    total_tickets  = stats.get("total_tickets", 0)
    open_tickets   = stats.get("open_tickets", 0)
    closed_tickets = stats.get("closed_tickets", 0)
    total_ratings  = stats.get("total_ratings", 0)
    avg_rating     = stats.get("avg_rating", 0.0)
    top_closers    = stats.get("top_closers", [])

    if total_ratings > 0:
        full  = int(round(avg_rating))
        stars = "⭐" * full + "☆" * (5 - full)
        rating_str = f"{stars} **{avg_rating:.1f}/5** ({total_ratings} rating{'s' if total_ratings != 1 else ''})"
    else:
        rating_str = "No ratings yet"

    staff_lines = []
    for idx, entry in enumerate(top_closers[:5]):
        m = ctx.guild.get_member(entry["closer_id"])
        name = m.display_name if m else f"`{entry['closer_id']}`"
        medals = ["🥇", "🥈", "🥉"]
        prefix = medals[idx] if idx < 3 else f"`{idx+1}.`"
        staff_lines.append(f"{prefix} {name} — {entry['count']} ticket{'s' if entry['count'] != 1 else ''} closed")

    e = make_embed(C_PRIMARY)
    e.title = "🎫 Ticket Statistics"
    e.add_field(name="Total Tickets",  value=f"{total_tickets:,}", inline=True)
    e.add_field(name="Open",           value=f"{open_tickets:,}",  inline=True)
    e.add_field(name="Closed",         value=f"{closed_tickets:,}", inline=True)
    e.add_field(name="Avg Rating",     value=rating_str, inline=False)
    if staff_lines:
        e.add_field(name="🏆 Most Active Staff",
                    value="\n".join(staff_lines), inline=False)
    else:
        e.add_field(name="🏆 Most Active Staff", value="No data yet", inline=False)
    e.set_footer(text="LXTE's AI — Ticket System")
    await ctx.send(embed=e)


# ═══════════════════════════════════════════════════════════════════════════════
#  STARTUP
# ═══════════════════════════════════════════════════════════════════════════════

async def _startup():
    token     = os.environ.get("DISCORD_TOKEN")
    mongo_uri = os.environ.get("MONGO_URI")
    owner_id  = os.environ.get("OWNER_ID")
    missing   = [n for n, v in [("DISCORD_TOKEN", token), ("MONGO_URI", mongo_uri), ("OWNER_ID", owner_id)] if not v]
    if missing: raise EnvironmentError(f"Missing env vars: {', '.join(missing)}")
    try: int(owner_id)
    except ValueError: raise EnvironmentError("OWNER_ID must be an integer.")
    logger.info("Connecting to MongoDB…")
    db = Database(mongo_uri)
    if not await db.ping(): raise ConnectionError("MongoDB unreachable.")
    logger.info("MongoDB connected.")
    # Restore saved Roblox version hashes so restart doesn't re-alert
    _saved = await db.get_config(0)
    for _c in ROBLOX_CHANNELS:
        _v = _saved.get("roblox_version_" + _c)
        if _v: bot._roblox_versions[_c] = _v
    bot._roblox_history = await db.get_roblox_history()

    bot.db           = db
    bot.owner_id_int = int(owner_id)
    bot.start_time   = datetime.now(timezone.utc)
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try: loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.close()))
        except NotImplementedError: pass
    logger.info("Starting bot…")
    try:
        await bot.start(token)
    except discord.LoginFailure: logger.critical("Invalid Discord token.")
    except Exception as exc:     logger.critical("Fatal: %s", exc, exc_info=exc)
    finally:
        # ── Bot shutdown log (best-effort before DB closes) ───────────────────
        try:
            for guild in bot.guilds:
                cfg = await db.get_config(guild.id)
                lc_id = cfg.get("bot_log_channel_id") or cfg.get("log_channel_id")
                if lc_id:
                    lc = guild.get_channel(lc_id)
                    if lc:
                        e = make_embed(C_ERROR, f"**{bot.user}** is going **offline** / shutting down.")
                        e.title = "🔴 Bot Offline"
                        await lc.send(embed=e)
        except Exception: pass
        await db.close()
        logger.info("DB closed.")

def main():
    try: asyncio.run(_startup())
    except KeyboardInterrupt: logger.info("Shutting down.")
    except Exception as exc:  logger.critical("Startup failed: %s", exc, exc_info=exc); raise

if __name__ == "__main__":
    main()
