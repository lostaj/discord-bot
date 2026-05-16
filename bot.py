import os
import re
import io
import json
import time
import asyncio
import logging
import random
import signal
import psutil
import platform
from aiohttp import web as aiohttp_web
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta, time

import discord
from discord.ext import commands, tasks
from groq import AsyncGroq
import aiohttp
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()

# ─── CONFIG ────────────────────────────────────────────────────────────────────

DISCORD_TOKEN      = os.getenv("DISCORD_TOKEN")
OWNER_ID           = int(os.getenv("OWNER_ID", "0"))
CMD_PREFIX         = os.getenv("CMD_PREFIX", ".")
BOT_LOG_CHANNEL_ID = int(os.getenv("BOT_LOG_CHANNEL_ID", "0"))

GROQ_KEYS = [k for k in [os.getenv(f"GROQ_KEY_{i}") for i in range(1, 11)] if k]
MONGO_URI  = os.getenv("MONGO_URI", "")
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8080"))

VERSION = "2.1.0"

# ─── ECONOMY CONFIG ────────────────────────────────────────────────────────────

MSG_COOLDOWN  = 60
WORK_COOLDOWN = 3600  # 1 hour
MAX_HIST      = 20
MAX_PURGE     = 200
SNIPE_EXPIRY  = 300   # 5 minutes
TEMP_MSG_TTL  = 10    # seconds for auto-delete temp messages

RANKS = [
    (0,     "💀 Penniless"),
    (10,    "🪨 Gravel Rat"),
    (50,    "🥉 Bronze Hoarder"),
    (150,   "🥈 Silver Stacker"),
    (500,   "🥇 Gold Grinder"),
    (1000,  "💎 Diamond Hands"),
    (5000,  "👑 Ajax Royalty"),
    (10000, "🌟 Ajax Legend"),
]

WORK_LINES = [
    "skipped down the hallway in thigh-highs and somehow earned",
    "flipped their hair dramatically and the tips were",
    "served boba with a twirl and collected",
    "did the most adorable little wave and made",
    "wore the cutest fit to work today and got tipped",
    "reorganised the entire stockroom while looking fabulous and earned",
    "greeted every customer with a bow and raked in",
    "perfected their customer service smile and brought home",
    "carried three trays at once with the grace of a deity and earned",
    "showed up and simply ate — the coins just appeared:",
]

def get_rank(coins: int) -> str:
    rank = RANKS[0][1]
    for threshold, title in RANKS:
        if coins >= threshold:
            rank = title
    return rank

def daily_reward(streak: int) -> int:
    if streak >= 30: return 20
    if streak >= 7:  return 15
    if streak >= 3:  return 12
    return 10

# ─── LOGGING ───────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── SYSTEM PROMPT ─────────────────────────────────────────────────────────────

BASE_PROMPT = """You are AJ's Assistant, a powerful Discord server bot. You manage roles, channels, members, and more through natural language commands.

When the owner asks you to do something like:
- "make a role called X" / "create a role named X"
- "make a channel called X" / "create a channel named X"
- "delete the role X" / "remove the channel X"
- "ban @user" / "kick @user" / "mute @user for 10 mins"
- "purge 20 messages" / "clear 50 messages"
- "lock this channel" / "unlock this channel"
- "lock the server down" / "lockdown"
- "rename this channel to X" / "rename role X to Y"
- "give @user the X role" / "remove the X role from @user"
- "whois @user"
- "server report"

You MUST respond ONLY with a valid JSON object. No extra text, no markdown, no explanation.

The JSON must have an "action" field and relevant params. Examples:

{"action":"create_role","name":"aj shabeel","color":"random","mentionable":true,"reason":"Owner requested"}
{"action":"create_channel","name":"general-chat","type":"text","reason":"Owner requested"}
{"action":"create_channel","name":"voice-lobby","type":"voice","reason":"Owner requested"}
{"action":"create_category","name":"Gaming","reason":"Owner requested"}
{"action":"delete_role","name":"old-role","reason":"Owner requested"}
{"action":"delete_channel","name":"spam","reason":"Owner requested"}
{"action":"rename_role","old_name":"mod","new_name":"Moderator","reason":"Owner requested"}
{"action":"rename_channel","old_name":"general","new_name":"main-chat","reason":"Owner requested"}
{"action":"give_role","user_id":"123","role_name":"VIP","reason":"Owner granted"}
{"action":"remove_role","user_id":"123","role_name":"VIP","reason":"Owner removed"}
{"action":"ban","user_id":"123","reason":"Rule violation"}
{"action":"kick","user_id":"123","reason":"Rule violation"}
{"action":"mute","user_id":"123","seconds":300,"reason":"Spamming"}
{"action":"unban","user_id":"123","reason":"Appeal accepted"}
{"action":"purge","count":10,"reason":"Cleanup"}
{"action":"lock_channel","reason":"Temp lock"}
{"action":"unlock_channel","reason":"Reopening"}
{"action":"lockdown","reason":"Emergency"}
{"action":"unlock_all","reason":"Lifting lockdown"}
{"action":"whois","user_id":"123"}
{"action":"report"}
{"action":"chat","message":"Your normal conversational reply here"}

For casual conversation, questions, or anything that isn't a server management action, use {"action":"chat","message":"your reply"}

CRITICAL RULES:
- Output ONLY valid JSON. Nothing else.
- For role/channel names from the user's message, preserve them exactly as requested.
- If the user says "make it red" for a role, use "red" as color.
- Valid colors: red, blue, green, yellow, orange, purple, pink, teal, gold, random, default
- For ambiguous requests, use action "chat" to ask for clarification.
- You are friendly, capable, and get things done fast.
"""

# ─── DISCORD SETUP ─────────────────────────────────────────────────────────────

intents = discord.Intents.all()
bot     = commands.Bot(command_prefix=CMD_PREFIX, intents=intents, help_command=None)

# ─── GROQ CLIENT POOL (built once at startup) ──────────────────────────────────

groq_clients: dict = {}  # key -> AsyncGroq instance, populated in on_ready

# ─── MONGODB ───────────────────────────────────────────────────────────────────

_mongo_client = None
_db           = None

async def db_init():
    global _mongo_client, _db
    if not MONGO_URI:
        log.warning("⚠️  MONGO_URI not set — data won't persist across restarts.")
        return
    try:
        _mongo_client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        _db = _mongo_client["discord_bot"]
        await _db.command("ping")
        log.info("✅ MongoDB connected.")
    except Exception as e:
        log.error(f"❌ MongoDB connection failed: {e}")
        _db = None

def _col(name: str):
    return _db[name] if _db else None

# ─── IN-MEMORY STATE ───────────────────────────────────────────────────────────

memory:      dict  = {}
registry:    dict  = {}
mod_logs:    deque = deque(maxlen=300)
dm_logs:     dict  = {}
economy:     dict  = {}
activity:    dict  = {}
afk_users:   dict  = {}
snipe_cache: dict  = {}  # channel_id -> {content, author, author_avatar, ts}

custom_prompt: str | None = None
prev_prompt:   str | None = None

histories:   dict  = defaultdict(list)
rate_limits: dict  = defaultdict(float)
error_log:   deque = deque(maxlen=50)

key_index      = 0
start_time     = time.time()
msgs_processed = 0
_ready_fired   = False

_econ_locks: dict = defaultdict(asyncio.Lock)

# ─── DB LOAD/SAVE ──────────────────────────────────────────────────────────────

async def db_load():
    global memory, registry, custom_prompt
    if not _db:
        return
    try:
        async for doc in _col("registry").find({}, {"_id": 0}):
            registry[doc["uid"]] = doc
        async for doc in _col("memory").find({}, {"_id": 0}):
            memory[doc["uid"]] = doc.get("data", {})
        async for doc in _col("economy").find({}, {"_id": 0}):
            economy[doc["uid"]] = {
                "coins":            doc.get("coins", 0),
                "total_earned":     doc.get("total_earned", 0),
                "last_message_ts":  doc.get("last_message_ts"),
                "messages_counted": doc.get("messages_counted", 0),
                "last_daily":       doc.get("last_daily"),
                "daily_streak":     doc.get("daily_streak", 0),
                "last_work":        doc.get("last_work"),
            }
        logs_doc = await _col("meta").find_one({"_id": "mod_logs"})
        if logs_doc:
            mod_logs.extend(logs_doc.get("logs", []))
        dms_doc = await _col("meta").find_one({"_id": "dm_logs"})
        if dms_doc:
            dm_logs.update(dms_doc.get("data", {}))
        prompt_doc = await _col("meta").find_one({"_id": "prompt"})
        if prompt_doc:
            custom_prompt = prompt_doc.get("text")
        log.info(f"Loaded {len(registry)} users, {len(mod_logs)} mod logs, {len(economy)} economy entries.")
    except Exception as e:
        log.error(f"db_load error: {e}")

async def db_save_user(uid: str):
    if not _db: return
    try:
        await _col("registry").update_one({"uid": uid}, {"$set": registry[uid]}, upsert=True)
    except Exception as e:
        log.error(f"db_save_user: {e}")

async def db_save_mem(uid: str):
    if not _db: return
    try:
        await _col("memory").update_one(
            {"uid": uid}, {"$set": {"uid": uid, "data": memory.get(uid, {})}}, upsert=True
        )
    except Exception as e:
        log.error(f"db_save_mem: {e}")

async def db_save_economy(uid: str):
    if not _db: return
    try:
        doc = {"uid": uid, **economy.get(uid, {})}
        await _col("economy").update_one({"uid": uid}, {"$set": doc}, upsert=True)
    except Exception as e:
        log.error(f"db_save_economy: {e}")

async def db_save_mod_logs():
    if not _db: return
    try:
        await _col("meta").update_one(
            {"_id": "mod_logs"}, {"$set": {"logs": list(mod_logs)}}, upsert=True
        )
    except Exception as e:
        log.error(f"db_save_mod_logs: {e}")

async def db_save_dm_logs():
    if not _db: return
    try:
        await _col("meta").update_one(
            {"_id": "dm_logs"}, {"$set": {"data": dm_logs}}, upsert=True
        )
    except Exception as e:
        log.error(f"db_save_dm_logs: {e}")

async def db_save_prompt():
    if not _db: return
    try:
        await _col("meta").update_one(
            {"_id": "prompt"}, {"$set": {"text": custom_prompt}}, upsert=True
        )
    except Exception as e:
        log.error(f"db_save_prompt: {e}")

# ─── BOT LOG CHANNEL ───────────────────────────────────────────────────────────

async def bot_log(
    title: str,
    description: str = "",
    fields: list[tuple[str, str, bool]] | None = None,
    level: str = "info",
):
    color_map = {
        "info":     0x5865F2,
        "warn":     0xFFA500,
        "error":    0xFF3333,
        "mod":      0x00C853,
        "security": 0xFF6B00,
        "shutdown": 0x99AAB5,
        "startup":  0x43B581,
    }
    color  = color_map.get(level, 0x5865F2)
    log_fn = log.warning if level in ("warn", "security") else (log.error if level == "error" else log.info)
    log_fn(f"[BOT_LOG:{level.upper()}] {title} — {description[:120]}")

    if not BOT_LOG_CHANNEL_ID:
        return
    channel = bot.get_channel(BOT_LOG_CHANNEL_ID)
    if not channel:
        return

    embed = discord.Embed(
        title=title,
        description=description or discord.utils.MISSING,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=str(value)[:1024], inline=inline)
    embed.set_footer(text=f"AJ's Assistant • {level.upper()}")
    try:
        await channel.send(embed=embed)
    except Exception as e:
        log.error(f"bot_log send failed: {e}")

# ─── PERMISSION DENIED HELPER ──────────────────────────────────────────────────

async def deny(ctx):
    """Reply with a consistent permission-denied embed and log the attempt."""
    embed = discord.Embed(
        description="❌ You don't have permission to use this command.",
        color=0xFF3333,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Command attempted: {ctx.message.content[:80]}")
    await ctx.reply(embed=embed, mention_author=False)
    asyncio.create_task(bot_log(
        "🔒 Unauthorized Command",
        f"**{ctx.author}** (`{ctx.author.id}`) tried `{ctx.message.content[:100]}`",
        level="security",
    ))

# ─── ECONOMY HELPERS ───────────────────────────────────────────────────────────

def get_econ(uid: int) -> dict:
    key = str(uid)
    if key not in economy:
        economy[key] = {
            "coins":            0,
            "total_earned":     0,
            "last_message_ts":  None,
            "messages_counted": 0,
            "last_daily":       None,
            "daily_streak":     0,
            "last_work":        None,
        }
    e = economy[key]
    e.setdefault("last_daily",  None)
    e.setdefault("daily_streak", 0)
    e.setdefault("last_work",   None)
    return e

async def save_econ(uid: int):
    """Await the DB save directly — call inside the lock."""
    await db_save_economy(str(uid))

# ─── COLOR MAP ─────────────────────────────────────────────────────────────────

COLOR_MAP = {
    "red":     discord.Color.red(),
    "blue":    discord.Color.blue(),
    "green":   discord.Color.green(),
    "yellow":  discord.Color.yellow(),
    "orange":  discord.Color.orange(),
    "purple":  discord.Color.purple(),
    "pink":    discord.Color.from_rgb(255, 105, 180),
    "teal":    discord.Color.teal(),
    "gold":    discord.Color.gold(),
    "default": discord.Color.default(),
    "random":  None,
}

def resolve_color(name: str) -> discord.Color:
    name = (name or "random").lower()
    if name == "random":
        return discord.Color(random.randint(0, 0xFFFFFF))
    return COLOR_MAP.get(name, discord.Color.default())

# ─── HELPERS ───────────────────────────────────────────────────────────────────

def active_prompt() -> str:
    return custom_prompt if custom_prompt else BASE_PROMPT

def is_owner(uid: int) -> bool:
    return uid == OWNER_ID

def safe_int(val, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default

def get_mem(uid: int) -> dict:
    return memory.get(str(uid), {})

def update_mem(uid: int, data: dict):
    memory.setdefault(str(uid), {}).update(data)
    asyncio.create_task(db_save_mem(str(uid)))

def clear_mem(uid: int):
    memory.pop(str(uid), None)
    asyncio.create_task(db_save_mem(str(uid)))

def log_mod(action: str, target, by: int, reason: str = ""):
    mod_logs.append({
        "action": action,
        "target": str(target),
        "by":     str(by),
        "reason": reason,
        "ts":     datetime.now(timezone.utc).isoformat(),
    })
    asyncio.create_task(db_save_mod_logs())

def track_activity(uid: int, cid: int):
    key = str(uid)
    activity.setdefault(key, {"count": 0, "last": None, "channels": {}})
    activity[key]["count"] += 1
    activity[key]["last"]   = datetime.now(timezone.utc).isoformat()
    activity[key]["channels"][str(cid)] = activity[key]["channels"].get(str(cid), 0) + 1

def register_user(author: discord.Member | discord.User):
    key      = str(author.id)
    existing = registry.get(key, {})
    registry.setdefault(key, {})
    registry[key]["uid"]          = key
    registry[key]["username"]     = author.name
    registry[key]["display_name"] = author.display_name
    changed = (
        existing.get("username")     != author.name or
        existing.get("display_name") != author.display_name or
        key not in existing
    )
    if changed:
        asyncio.create_task(db_save_user(key))

def ts_unix(dt: datetime) -> int:
    """Return a Unix timestamp integer from a datetime."""
    return int(dt.timestamp())

def discord_ts(dt: datetime, style: str = "f") -> str:
    """Return a Discord timestamp string <t:UNIX:style>."""
    return f"<t:{ts_unix(dt)}:{style}>"

def build_context(msg: discord.Message, guild: discord.Guild | None = None) -> str:
    author = msg.author
    owner  = is_owner(author.id)
    roles  = [r.name for r in getattr(author, "roles", []) if r.name != "@everyone"]
    mem    = get_mem(author.id)

    parts = [f"[CTX] User={author.name}(ID={author.id}) IsOwner={owner}"]
    if roles:
        parts.append(f"Roles={','.join(roles[:5])}")
    if mem:
        parts.append("Mem=" + ",".join(f"{k}={v}" for k, v in list(mem.items())[:4]))
    if msg.mentions:
        parts.append("Mentions=" + ",".join(f"{m.name}:{m.id}" for m in msg.mentions[:3]))
    if msg.reference and hasattr(msg.reference, "resolved") and msg.reference.resolved:
        ref = msg.reference.resolved
        parts.append(f'ReplyTo={ref.author.name}:"{ref.content[:80]}"')

    if guild:
        parts.append(f"Guild={guild.name}(ID={guild.id})")
        parts.append(f"MemberCount={guild.member_count}")
        parts.append(f"Channels={len(guild.channels)}")
        parts.append(f"Roles={len(guild.roles)}")
        online = sum(1 for m in guild.members if m.status != discord.Status.offline)
        parts.append(f"Online={online}")
        boost = guild.premium_subscription_count or 0
        parts.append(f"BoostLevel={guild.premium_tier} Boosts={boost}")
        if guild.owner:
            parts.append(f"GuildOwner={guild.owner.name}:{guild.owner_id}")

    return " | ".join(parts)

async def build_full_server_scan(guild: discord.Guild) -> str:
    """CPU-bound scan — offloaded to a thread to avoid blocking the event loop."""
    def _build():
        lines = []
        lines.append(f"=== SERVER: {guild.name} (ID: {guild.id}) ===")
        lines.append(f"Owner: {guild.owner} ({guild.owner_id})")
        lines.append(f"Created: {guild.created_at.strftime('%Y-%m-%d')}")
        lines.append(f"Members: {guild.member_count} total")
        humans = sum(1 for m in guild.members if not m.bot)
        bots   = sum(1 for m in guild.members if m.bot)
        online = sum(1 for m in guild.members if m.status != discord.Status.offline)
        lines.append(f"Humans: {humans} | Bots: {bots} | Online: {online}")
        lines.append(f"Boost Level: {guild.premium_tier} | Boosts: {guild.premium_subscription_count}")
        lines.append(f"Verification: {guild.verification_level} | MFA: {guild.mfa_level}")
        lines.append(f"Description: {guild.description or 'None'}")
        lines.append(f"Icon: {guild.icon.url if guild.icon else 'None'}")
        lines.append(f"Banner: {guild.banner.url if guild.banner else 'None'}")

        lines.append("\n=== MEMBERS ===")
        for m in guild.members:
            roles    = [r.name for r in m.roles if r.name != "@everyone"]
            joined   = m.joined_at.strftime('%Y-%m-%d') if m.joined_at else "unknown"
            created  = m.created_at.strftime('%Y-%m-%d')
            avatar   = str(m.display_avatar.url)
            status   = str(m.status)
            boosting = bool(m.premium_since)
            act_name = str(m.activity) if m.activity else "None"
            lines.append(
                f"  {m.display_name} ({m.name}, ID:{m.id}) | Bot:{m.bot} | "
                f"Joined:{joined} | Created:{created} | Status:{status} | "
                f"Boosting:{boosting} | Roles:[{', '.join(roles)}] | "
                f"Avatar:{avatar} | Activity:{act_name}"
            )

        lines.append("\n=== ROLES ===")
        for r in sorted(guild.roles, key=lambda x: x.position, reverse=True):
            perms = [p for p, v in r.permissions if v]
            lines.append(
                f"  {r.name} (ID:{r.id}) | Color:{r.color} | Pos:{r.position} | "
                f"Members:{len(r.members)} | Mentionable:{r.mentionable} | "
                f"Hoisted:{r.hoist} | Managed:{r.managed} | Perms:[{', '.join(perms[:8])}]"
            )

        lines.append("\n=== CATEGORIES ===")
        for cat in guild.categories:
            lines.append(f"  {cat.name} (ID:{cat.id}) | Channels:{len(cat.channels)}")

        lines.append("\n=== CHANNELS ===")
        for ch in guild.channels:
            cat = ch.category.name if ch.category else "None"
            if isinstance(ch, discord.TextChannel):
                lines.append(
                    f"  #{ch.name} (ID:{ch.id}) | Type:text | Cat:{cat} | "
                    f"Topic:{(ch.topic or 'None')[:60]} | NSFW:{ch.is_nsfw()} | "
                    f"Slowmode:{ch.slowmode_delay}s | Pos:{ch.position}"
                )
            elif isinstance(ch, discord.VoiceChannel):
                lines.append(
                    f"  🔊{ch.name} (ID:{ch.id}) | Type:voice | Cat:{cat} | "
                    f"Bitrate:{ch.bitrate} | UserLimit:{ch.user_limit} | Members:{len(ch.members)}"
                )
            else:
                lines.append(f"  {ch.name} (ID:{ch.id}) | Type:{type(ch).__name__} | Cat:{cat}")

        lines.append("\n=== EMOJIS ===")
        for e in guild.emojis:
            lines.append(f"  :{e.name}: (ID:{e.id}) | Animated:{e.animated} | URL:{e.url}")

        lines.append("\n=== STICKERS ===")
        for s in guild.stickers:
            lines.append(f"  {s.name} (ID:{s.id}) | Format:{s.format}")

        return "\n".join(lines)

    text = await asyncio.to_thread(_build)

    # Network calls cannot be offloaded to a thread — do them here
    try:
        bans = [entry async for entry in guild.bans(limit=50)]
        ban_lines = [f"\n=== BANS (up to 50) ==="]
        for ban in bans:
            ban_lines.append(f"  {ban.user} (ID:{ban.user.id}) | Reason:{ban.reason}")
        text += "\n".join(ban_lines)
    except Exception:
        pass

    try:
        invites = await guild.invites()
        inv_lines = ["\n=== INVITES ==="]
        for inv in invites:
            inv_lines.append(
                f"  {inv.code} | Creator:{inv.inviter} | Uses:{inv.uses} | "
                f"Max:{inv.max_uses} | Channel:#{inv.channel.name if inv.channel else 'N/A'}"
            )
        text += "\n".join(inv_lines)
    except Exception:
        pass

    try:
        webhooks = await guild.webhooks()
        wh_lines = ["\n=== WEBHOOKS ==="]
        for wh in webhooks:
            wh_lines.append(f"  {wh.name} (ID:{wh.id}) | Channel:{wh.channel}")
        text += "\n".join(wh_lines)
    except Exception:
        pass

    return text

# ─── INJECTION DETECTION ──────────────────────────────────────────────────────

INJECTION_PATTERNS = [
    r"ignore (all |previous |your )?instructions",
    r"new system prompt",
    r"you are now",
    r"forget everything",
    r"disregard (all |your )?",
    r"act as (chatgpt|gpt|an? ai|an? language model)",
    r"jailbreak",
    r"pretend (you are|to be) (an? ai|chatgpt|gpt)",
    r"override (your )?(instructions|prompt|rules)",
    r"\[system\]",
    r"<\|",
]
_injection_re = re.compile("|".join(INJECTION_PATTERNS), re.IGNORECASE)

def is_suspicious(content: str) -> bool:
    return bool(_injection_re.search(content))

# ─── GROQ ──────────────────────────────────────────────────────────────────────

async def call_ai(history: list, system: str | None = None) -> str:
    global key_index, msgs_processed

    if not GROQ_KEYS:
        return '{"action":"chat","message":"No Groq API keys configured."}'

    clean_history = [
        m for m in history
        if isinstance(m, dict)
        and m.get("role") in ("user", "assistant")
        and isinstance(m.get("content"), str)
    ]

    sys_content = system if system else active_prompt()

    for _ in range(len(GROQ_KEYS)):
        key    = GROQ_KEYS[key_index % len(GROQ_KEYS)]
        client = groq_clients.get(key)
        if not client:
            key_index = (key_index + 1) % len(GROQ_KEYS)
            continue
        try:
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "system", "content": sys_content}] + clean_history,
                    max_tokens=400,
                    temperature=0.4,
                ),
                timeout=15.0,
            )
            msgs_processed += 1
            return resp.choices[0].message.content.strip()
        except asyncio.TimeoutError:
            err = f"Key {key_index + 1} timed out"
        except Exception as e:
            err = f"Key {key_index + 1} error: {e}"
        log.error(err)
        error_log.append({"ts": datetime.now(timezone.utc).isoformat(), "err": err})
        key_index = (key_index + 1) % len(GROQ_KEYS)
        await asyncio.sleep(0.3)

    return '{"action":"chat","message":"All API keys are rate limited. Try again in a moment."}'

# ─── WEB SEARCH ────────────────────────────────────────────────────────────────

async def web_search(query: str) -> str:
    try:
        safe_query = re.sub(r"[^\w\s\-]", "", query)[:200]
        async with aiohttp.ClientSession() as s:
            url = f"https://api.duckduckgo.com/?q={safe_query}&format=json&no_html=1&skip_disambig=1"
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=6)) as r:
                data = await r.json(content_type=None)
        abstract = data.get("AbstractText", "")
        source   = data.get("AbstractURL", "")
        if abstract:
            return f"{abstract}\n— {source}"
        for t in data.get("RelatedTopics", []):
            if isinstance(t, dict) and t.get("Text"):
                return t["Text"]
        return "Couldn't find anything solid on that."
    except Exception as e:
        return f"Search error: {e}"

# ─── PARSE AI RESPONSE ────────────────────────────────────────────────────────

def parse_ai_json(raw: str) -> dict | None:
    raw   = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
    match = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass
    return None

# ─── OWNER MOD ACTIONS SET ────────────────────────────────────────────────────

OWNER_ACTIONS = {
    "create_role", "delete_role", "rename_role",
    "create_channel", "delete_channel", "rename_channel", "create_category",
    "give_role", "remove_role",
    "ban", "kick", "mute", "unban",
    "purge", "lock_channel", "unlock_channel", "lockdown", "unlock_all",
}

# ─── ACTION EXECUTOR ──────────────────────────────────────────────────────────

async def execute_action(msg: discord.Message, data: dict) -> str | None:
    guild  = msg.guild
    author = msg.author
    action = data.get("action", "chat")

    if action == "chat":
        return data.get("message", "...")

    if action == "create_role":
        if not guild: return "Can't create roles in DMs."
        name        = data.get("name", "New Role")
        color       = resolve_color(data.get("color", "random"))
        mentionable = data.get("mentionable", False)
        hoisted     = data.get("hoisted", False)
        reason      = data.get("reason", f"Requested by {author.name}")
        try:
            role = await guild.create_role(name=name, color=color, mentionable=mentionable, hoist=hoisted, reason=reason)
            log_mod("create_role", role.id, author.id, name)
            asyncio.create_task(bot_log("🛡️ Role Created", f"**{name}** created by {author.mention}", fields=[("Role", role.mention, True), ("By", author.display_name, True)], level="mod"))
            return f"✅ Role **{role.name}** created! ({role.mention})"
        except discord.Forbidden:
            return "❌ I don't have permission to create roles."
        except Exception as e:
            return f"❌ Error: {e}"

    if action == "delete_role":
        if not guild: return "Can't do that in DMs."
        name   = data.get("name", "")
        reason = data.get("reason", f"Requested by {author.name}")
        role   = discord.utils.find(lambda r: r.name.lower() == name.lower(), guild.roles)
        if not role: return f"❌ Role **{name}** not found."
        try:
            await role.delete(reason=reason)
            log_mod("delete_role", role.id, author.id, name)
            asyncio.create_task(bot_log("🗑️ Role Deleted", f"**{name}** deleted by {author.mention}", level="mod"))
            return f"✅ Role **{name}** deleted."
        except discord.Forbidden:
            return "❌ Missing permissions."
        except Exception as e:
            return f"❌ Error: {e}"

    if action == "rename_role":
        if not guild: return "Can't do that in DMs."
        old    = data.get("old_name", "")
        new    = data.get("new_name", "")
        reason = data.get("reason", f"Requested by {author.name}")
        role   = discord.utils.find(lambda r: r.name.lower() == old.lower(), guild.roles)
        if not role: return f"❌ Role **{old}** not found."
        try:
            await role.edit(name=new, reason=reason)
            log_mod("rename_role", role.id, author.id, f"{old} → {new}")
            asyncio.create_task(bot_log("✏️ Role Renamed", f"**{old}** → **{new}** by {author.mention}", level="mod"))
            return f"✅ Role renamed from **{old}** to **{new}**."
        except discord.Forbidden:
            return "❌ Missing permissions."
        except Exception as e:
            return f"❌ Error: {e}"

    if action == "create_channel":
        if not guild: return "Can't create channels in DMs."
        name     = data.get("name", "new-channel").lower().replace(" ", "-")
        ch_type  = data.get("type", "text").lower()
        topic    = data.get("topic", None)
        reason   = data.get("reason", f"Requested by {author.name}")
        cat_name = data.get("category", None)
        category = None
        if cat_name:
            category = discord.utils.find(lambda c: c.name.lower() == cat_name.lower(), guild.categories)
        try:
            if ch_type == "voice":
                ch = await guild.create_voice_channel(name=name, category=category, reason=reason)
            elif ch_type == "stage":
                ch = await guild.create_stage_channel(name=name, category=category, reason=reason)
            else:
                ch = await guild.create_text_channel(name=name, topic=topic, category=category, reason=reason)
            log_mod("create_channel", ch.id, author.id, name)
            asyncio.create_task(bot_log("📢 Channel Created", f"**#{name}** ({ch_type}) by {author.mention}", level="mod"))
            return f"✅ Channel **#{ch.name}** created! ({ch.mention})"
        except discord.Forbidden:
            return "❌ I don't have permission to create channels."
        except Exception as e:
            return f"❌ Error: {e}"

    if action == "delete_channel":
        if not guild: return "Can't do that in DMs."
        name   = data.get("name", "")
        reason = data.get("reason", f"Requested by {author.name}")
        ch     = discord.utils.find(lambda c: c.name.lower() == name.lower().replace(" ", "-"), guild.channels)
        if not ch: return f"❌ Channel **{name}** not found."
        try:
            await ch.delete(reason=reason)
            log_mod("delete_channel", ch.id, author.id, name)
            asyncio.create_task(bot_log("🗑️ Channel Deleted", f"**#{name}** deleted by {author.mention}", level="mod"))
            return f"✅ Channel **{name}** deleted."
        except discord.Forbidden:
            return "❌ Missing permissions."
        except Exception as e:
            return f"❌ Error: {e}"

    if action == "rename_channel":
        if not guild: return "Can't do that in DMs."
        old    = data.get("old_name", "").lower().replace(" ", "-")
        new    = data.get("new_name", "").lower().replace(" ", "-")
        reason = data.get("reason", f"Requested by {author.name}")
        ch     = discord.utils.find(lambda c: c.name.lower() == old, guild.channels)
        if not ch: return f"❌ Channel **{old}** not found."
        try:
            await ch.edit(name=new, reason=reason)
            log_mod("rename_channel", ch.id, author.id, f"{old} → {new}")
            asyncio.create_task(bot_log("✏️ Channel Renamed", f"**#{old}** → **#{new}** by {author.mention}", level="mod"))
            return f"✅ Channel renamed to **#{new}**."
        except discord.Forbidden:
            return "❌ Missing permissions."
        except Exception as e:
            return f"❌ Error: {e}"

    if action == "create_category":
        if not guild: return "Can't do that in DMs."
        name   = data.get("name", "New Category")
        reason = data.get("reason", f"Requested by {author.name}")
        try:
            cat = await guild.create_category(name=name, reason=reason)
            log_mod("create_category", cat.id, author.id, name)
            asyncio.create_task(bot_log("📁 Category Created", f"**{name}** by {author.mention}", level="mod"))
            return f"✅ Category **{cat.name}** created!"
        except discord.Forbidden:
            return "❌ Missing permissions."
        except Exception as e:
            return f"❌ Error: {e}"

    if action == "give_role":
        if not guild: return "Can't do that in DMs."
        uid_val   = safe_int(data.get("user_id", 0))
        role_name = data.get("role_name", "")
        reason    = data.get("reason", f"Requested by {author.name}")
        member    = guild.get_member(uid_val) or (msg.mentions[0] if msg.mentions else None)
        if not member: return "❌ Couldn't find that user."
        role = discord.utils.find(lambda r: r.name.lower() == role_name.lower(), guild.roles)
        if not role: return f"❌ Role **{role_name}** not found."
        try:
            await member.add_roles(role, reason=reason)
            log_mod("give_role", member.id, author.id, role_name)
            asyncio.create_task(bot_log("🎖️ Role Given", f"**{role_name}** → {member.mention} by {author.mention}", level="mod"))
            return f"✅ Gave **{role_name}** to {member.mention}."
        except discord.Forbidden:
            return "❌ Missing permissions."
        except Exception as e:
            return f"❌ Error: {e}"

    if action == "remove_role":
        if not guild: return "Can't do that in DMs."
        uid_val   = safe_int(data.get("user_id", 0))
        role_name = data.get("role_name", "")
        reason    = data.get("reason", f"Requested by {author.name}")
        member    = guild.get_member(uid_val) or (msg.mentions[0] if msg.mentions else None)
        if not member: return "❌ Couldn't find that user."
        role = discord.utils.find(lambda r: r.name.lower() == role_name.lower(), guild.roles)
        if not role: return f"❌ Role **{role_name}** not found."
        try:
            await member.remove_roles(role, reason=reason)
            log_mod("remove_role", member.id, author.id, role_name)
            asyncio.create_task(bot_log("🎖️ Role Removed", f"**{role_name}** from {member.mention} by {author.mention}", level="mod"))
            return f"✅ Removed **{role_name}** from {member.mention}."
        except discord.Forbidden:
            return "❌ Missing permissions."
        except Exception as e:
            return f"❌ Error: {e}"

    if action == "ban":
        if not guild: return "Can't do that in DMs."
        uid_val = safe_int(data.get("user_id", 0))
        reason  = data.get("reason", f"Banned by {author.name}")
        member  = guild.get_member(uid_val) or (msg.mentions[0] if msg.mentions else None)
        if not member: return "❌ Couldn't find that user."
        try:
            await guild.ban(member, reason=reason)
            log_mod("ban", member.id, author.id, reason)
            asyncio.create_task(bot_log("🔨 Member Banned", f"**{member}** banned by {author.mention}", fields=[("Reason", reason, False)], level="mod"))
            return f"🔨 **{member.name}** has been banned. Reason: {reason}"
        except discord.Forbidden:
            return "❌ Missing permissions to ban."
        except Exception as e:
            return f"❌ Error: {e}"

    if action == "kick":
        if not guild: return "Can't do that in DMs."
        uid_val = safe_int(data.get("user_id", 0))
        reason  = data.get("reason", f"Kicked by {author.name}")
        member  = guild.get_member(uid_val) or (msg.mentions[0] if msg.mentions else None)
        if not member: return "❌ Couldn't find that user."
        try:
            await guild.kick(member, reason=reason)
            log_mod("kick", member.id, author.id, reason)
            asyncio.create_task(bot_log("👢 Member Kicked", f"**{member}** kicked by {author.mention}", fields=[("Reason", reason, False)], level="mod"))
            return f"👢 **{member.name}** has been kicked. Reason: {reason}"
        except discord.Forbidden:
            return "❌ Missing permissions to kick."
        except Exception as e:
            return f"❌ Error: {e}"

    if action == "mute":
        if not guild: return "Can't do that in DMs."
        uid_val = safe_int(data.get("user_id", 0))
        secs    = safe_int(data.get("seconds", 300))
        reason  = data.get("reason", f"Muted by {author.name}")
        member  = guild.get_member(uid_val) or (msg.mentions[0] if msg.mentions else None)
        if not member: return "❌ Couldn't find that user."
        until   = discord.utils.utcnow() + timedelta(seconds=secs)
        try:
            await member.timeout(until, reason=reason)
            log_mod("mute", member.id, author.id, f"{secs}s — {reason}")
            asyncio.create_task(bot_log("🔇 Member Muted", f"**{member}** muted {secs//60}m by {author.mention}", fields=[("Reason", reason, False), ("Duration", f"{secs}s", True)], level="mod"))
            return f"🔇 **{member.name}** muted for {secs // 60} min(s). Reason: {reason}"
        except discord.Forbidden:
            return "❌ Missing permissions to mute."
        except discord.HTTPException as e:
            return f"❌ Could not mute (they may be higher in hierarchy): {e}"

    if action == "unban":
        if not guild: return "Can't do that in DMs."
        uid_val = safe_int(data.get("user_id", 0))
        reason  = data.get("reason", "Appeal accepted")
        try:
            user = await bot.fetch_user(uid_val)
            await guild.unban(user, reason=reason)
            log_mod("unban", uid_val, author.id, reason)
            asyncio.create_task(bot_log("✅ Member Unbanned", f"**{user}** unbanned by {author.mention}", level="mod"))
            return f"✅ **{user.name}** unbanned."
        except discord.NotFound:
            return "❌ User not found in ban list."
        except Exception as e:
            return f"❌ Error: {e}"

    if action == "purge":
        if not guild: return "Can't do that in DMs."
        count  = min(safe_int(data.get("count", 10)), MAX_PURGE)
        reason = data.get("reason", f"Purge by {author.name}")
        try:
            deleted = await msg.channel.purge(limit=count + 1)
            log_mod("purge", msg.channel.id, author.id, f"{len(deleted)} msgs")
            asyncio.create_task(bot_log("🗑️ Messages Purged", f"**{len(deleted)-1}** msgs purged in {msg.channel.mention} by {author.mention}", level="mod"))
            await msg.channel.send(f"🗑️ Purged **{len(deleted) - 1}** messages.", delete_after=5)
            return None
        except discord.Forbidden:
            return "❌ Missing permissions to purge."
        except Exception as e:
            return f"❌ Error: {e}"

    if action == "lock_channel":
        if not guild: return "Can't do that in DMs."
        reason = data.get("reason", f"Locked by {author.name}")
        ch     = msg.channel
        ow     = ch.overwrites_for(guild.default_role)
        ow.send_messages = False
        try:
            await ch.set_permissions(guild.default_role, overwrite=ow, reason=reason)
            log_mod("lock", ch.id, author.id)
            asyncio.create_task(bot_log("🔒 Channel Locked", f"{ch.mention} locked by {author.mention}", level="mod"))
            return f"🔒 {ch.mention} is now locked."
        except discord.Forbidden:
            return "❌ Missing permissions."
        except Exception as e:
            return f"❌ Error: {e}"

    if action == "unlock_channel":
        if not guild: return "Can't do that in DMs."
        reason = data.get("reason", f"Unlocked by {author.name}")
        ch     = msg.channel
        ow     = ch.overwrites_for(guild.default_role)
        ow.send_messages = None
        try:
            await ch.set_permissions(guild.default_role, overwrite=ow, reason=reason)
            log_mod("unlock", ch.id, author.id)
            asyncio.create_task(bot_log("🔓 Channel Unlocked", f"{ch.mention} unlocked by {author.mention}", level="mod"))
            return f"🔓 {ch.mention} is now unlocked."
        except discord.Forbidden:
            return "❌ Missing permissions."
        except Exception as e:
            return f"❌ Error: {e}"

    if action == "lockdown":
        if not guild: return "Can't do that in DMs."
        reason = data.get("reason", f"Lockdown by {author.name}")
        async def lock_ch(ch):
            try:
                ow = ch.overwrites_for(guild.default_role)
                ow.send_messages = False
                await ch.set_permissions(guild.default_role, overwrite=ow)
                return True
            except Exception:
                return False
        results = await asyncio.gather(*[lock_ch(ch) for ch in guild.text_channels])
        locked  = sum(results)
        log_mod("lockdown", 0, author.id, reason)
        asyncio.create_task(bot_log("🚨 SERVER LOCKDOWN", f"**{locked} channels** locked by {author.mention}\nReason: {reason}", level="warn"))
        return f"🔒 **LOCKDOWN ACTIVE** — {locked} channels locked. Reason: {reason}"

    if action == "unlock_all":
        if not guild: return "Can't do that in DMs."
        reason = data.get("reason", f"Unlock all by {author.name}")
        async def unlock_ch(ch):
            try:
                ow = ch.overwrites_for(guild.default_role)
                ow.send_messages = None
                await ch.set_permissions(guild.default_role, overwrite=ow)
                return True
            except Exception:
                return False
        results  = await asyncio.gather(*[unlock_ch(ch) for ch in guild.text_channels])
        unlocked = sum(results)
        log_mod("unlock_all", 0, author.id, reason)
        asyncio.create_task(bot_log("🔓 Server Unlocked", f"**{unlocked} channels** unlocked by {author.mention}", level="mod"))
        return f"🔓 All channels unlocked ({unlocked} total)."

    if action == "whois":
        if not guild: return "Can't do that in DMs."
        uid_val = safe_int(data.get("user_id", 0))
        tgt     = guild.get_member(uid_val) or (msg.mentions[0] if msg.mentions else None)
        if not tgt: return "❌ User not found."
        act    = activity.get(str(tgt.id), {})
        warns  = [e for e in mod_logs if e["target"] == str(tgt.id)]
        mem    = get_mem(tgt.id)
        econ   = get_econ(tgt.id)
        join   = getattr(tgt, "joined_at", None)
        roles  = [r.name for r in tgt.roles if r.name != "@everyone"]
        lines  = [
            f"**👤 {tgt.display_name}** (`{tgt.name}` | `{tgt.id}`)",
            f"Joined: {discord_ts(join, 'D') if join else 'N/A'} | Created: {discord_ts(tgt.created_at, 'D')}",
            f"Session msgs: {act.get('count', 0)} | Last active: {discord_ts(datetime.fromisoformat(act['last']), 'R') if act.get('last') else 'never'}",
            f"Roles: {', '.join(roles[:8]) or 'none'}",
            f"Mod actions on record: {len(warns)}",
            f"Ajax Coins: **{econ['coins']:,}** ({get_rank(econ['coins'])})",
            f"Avatar: {tgt.display_avatar.url}",
        ]
        if mem:
            lines.append("Notes: " + ", ".join(f"{k}={v}" for k, v in list(mem.items())[:4]))
        return "\n".join(lines)

    if action == "report":
        if not guild: return "No guild context."
        week_ago  = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        month_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        top = sorted(
            [(k, v) for k, v in activity.items() if (v.get("last") or "") >= week_ago],
            key=lambda x: x[1]["count"], reverse=True
        )[:5]
        top_names = []
        for k, v in top:
            m = guild.get_member(int(k))
            top_names.append(f"{m.display_name if m else k} ({v['count']})")
        inactive       = sum(1 for v in activity.values() if (v.get("last") or "") < month_ago)
        recent_actions = len([e for e in mod_logs if e["ts"] >= week_ago])
        richest        = sorted(economy.items(), key=lambda x: x[1].get("coins", 0), reverse=True)[:3]
        rich_names     = []
        for k, v in richest:
            m = guild.get_member(int(k))
            rich_names.append(f"{m.display_name if m else k} ({v.get('coins', 0):,}🪙)")
        humans = sum(1 for m in guild.members if not m.bot)
        bots   = sum(1 for m in guild.members if m.bot)
        online = sum(1 for m in guild.members if m.status != discord.Status.offline)
        now    = datetime.now(timezone.utc)
        return (
            f"**📊 Server Report — {guild.name}**\n"
            f"Generated: {discord_ts(now, 'F')}\n"
            f"Members: {guild.member_count} ({humans} humans, {bots} bots) | Online: {online}\n"
            f"Channels: {len(guild.channels)} | Roles: {len(guild.roles)}\n"
            f"Boost Level: {guild.premium_tier} | Boosts: {guild.premium_subscription_count}\n"
            f"Most active this week: {', '.join(top_names) or 'no data'}\n"
            f"Inactive 30+ days (session): {inactive}\n"
            f"Tracked this session: {len(activity)}\n"
            f"Mod actions this week: {recent_actions}\n"
            f"Ajax Coin richest: {', '.join(rich_names) or 'no data'}"
        )

    return f"❓ Unknown action: {action}"

# ─── CORE PROCESS (AI) ────────────────────────────────────────────────────────

async def process(msg: discord.Message, content_override: str | None = None, is_dm: bool = False):
    author  = msg.author
    uid     = author.id
    content = (content_override or msg.content).strip()
    owner   = is_owner(uid)

    now_ts = time.time()
    if not owner:
        cooldown  = 4.0
        last      = rate_limits[uid]
        if now_ts - last < cooldown:
            remaining = int(cooldown - (now_ts - last)) + 1
            embed = discord.Embed(
                description=f"⏱️ Slow down! Please wait **{remaining}s** before messaging again.",
                color=0xFFA500,
                timestamp=datetime.now(timezone.utc),
            )
            await msg.reply(embed=embed, mention_author=False)
            return
        rate_limits[uid] = now_ts

    if is_suspicious(content):
        asyncio.create_task(bot_log(
            "⚠️ Suspicious / Injection Attempt",
            f"**{author}** (`{uid}`)",
            fields=[
                ("Message", f"```{content[:500]}```", False),
                ("Channel", getattr(msg.channel, "mention", "DM"), True),
            ],
            level="security",
        ))

    if not is_dm:
        track_activity(uid, msg.channel.id)
    register_user(author)

    ctx_line        = build_context(msg, msg.guild)
    system_with_ctx = f"{active_prompt()}\n\n{ctx_line}"

    hist_key = f"dm_{uid}" if is_dm else f"ch_{msg.channel.id}_u_{uid}"
    hist     = histories[hist_key]

    hist.append({"role": "user", "content": content})
    if len(hist) > MAX_HIST:
        histories[hist_key] = hist[-MAX_HIST:]
    hist = histories[hist_key]

    # LRU-style eviction: drop oldest 100 keys when we hit 500
    if len(histories) > 500:
        oldest_keys = list(histories.keys())[:100]
        for k in oldest_keys:
            del histories[k]

    async with msg.channel.typing():
        raw = await call_ai(hist, system=system_with_ctx)

    histories[hist_key].append({"role": "assistant", "content": raw})
    if len(histories[hist_key]) > MAX_HIST:
        histories[hist_key] = histories[hist_key][-MAX_HIST:]

    parsed = parse_ai_json(raw)
    if parsed:
        action = parsed.get("action", "chat")
        if action in OWNER_ACTIONS and not owner:
            embed = discord.Embed(
                description="❌ Only the server owner can use moderation commands.",
                color=0xFF3333,
                timestamp=datetime.now(timezone.utc),
            )
            await msg.reply(embed=embed, mention_author=False)
            asyncio.create_task(bot_log(
                "🔒 Unauthorized Mod Attempt",
                f"**{author}** (`{uid}`) tried `{action}`",
                fields=[("Message", content[:300], False)],
                level="security",
            ))
            return
        reply = await execute_action(msg, parsed)
        if reply:
            embed = discord.Embed(
                description=reply,
                color=0x5865F2,
                timestamp=datetime.now(timezone.utc),
            )
            await msg.reply(embed=embed, mention_author=False)
    else:
        safe_raw = discord.utils.escape_mentions(raw[:1990])
        embed = discord.Embed(
            description=safe_raw,
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
        )
        await msg.reply(embed=embed, mention_author=False)

    if is_dm and uid != OWNER_ID:
        logs = dm_logs.setdefault(str(uid), [])
        logs.append({
            "ts":  datetime.now(timezone.utc).isoformat()[:19],
            "msg": content[:200],
            "rep": raw[:200],
        })
        dm_logs[str(uid)] = logs[-15:]
        await db_save_dm_logs()

# ─── COMMANDS ─────────────────────────────────────────────────────────────────

@bot.command(name="help")
async def cmd_help(ctx):
    p = CMD_PREFIX
    embed = discord.Embed(
        title="🤖 AJ's Assistant",
        description="Mention me or reply to me and speak naturally — I understand English!\nYou can also DM me directly.",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="💬 AI Commands", value=(
        f"`{p}ask <question>` — Chat with the bot\n"
        f"`{p}search <query>` — Web search (owner)\n"
        f"`{p}scan` — Full server scan (owner)\n"
        f"`{p}setprompt <text>` — Change AI personality (owner)\n"
        f"`{p}revertprompt` — Undo prompt change (owner)\n"
        f"`{p}clearmem @user` — Clear user memory (owner)"
    ), inline=False)
    embed.add_field(name="🛠️ Moderation (owner only)", value=(
        f"`{p}purge <n>` — Delete messages\n"
        f"`{p}lockdown` — Lock all channels\n"
        f"`{p}unlock` — Unlock all channels\n"
        f"`{p}shutdown` — Shut down the bot\n"
        f"`{p}whois @user` — User info\n"
        f"`{p}report` — Server stats\n"
        f"`{p}backup` — DM a full data backup"
    ), inline=False)
    embed.add_field(name="🪙 Economy", value=(
        f"`{p}daily` — Claim daily coins\n"
        f"`{p}work` — Work a shift (1h cooldown, +10 coins)\n"
        f"`{p}balance [@user]` — Check balance\n"
        f"`{p}leaderboard` — Top 10 richest\n"
        f"`{p}pay @user <amount>` — Send coins\n"
        f"`{p}give @user <amount>` — Give coins (owner)\n"
        f"`{p}take @user <amount>` — Remove coins (owner)\n"
        f"`{p}coinreset @user` — Reset coins (owner)"
    ), inline=False)
    embed.add_field(name="🔧 Utility", value=(
        f"`{p}afk [reason]` — Set AFK status\n"
        f"`{p}snipe` — Show last deleted message\n"
        f"`{p}membercount` — Server member stats\n"
        f"`{p}uptime` — Bot uptime\n"
        f"`{p}botinfo` — Bot stats\n"
        f"`{p}ping` — Latency check\n"
        f"`{p}debug` — Debug info (owner)"
    ), inline=False)
    embed.add_field(name="🪙 Daily Streak Rewards", value=(
        "Day 1–2: **10 coins**\n"
        "Day 3–6: **12 coins**\n"
        "Day 7–29: **15 coins**\n"
        "Day 30+: **20 coins**"
    ), inline=True)
    embed.add_field(name="🏅 Coin Ranks", value=(
        "💀 Penniless → 🪨 Gravel Rat (10)\n"
        "🥉 Bronze (50) → 🥈 Silver (150)\n"
        "🥇 Gold (500) → 💎 Diamond (1k)\n"
        "👑 Royalty (5k) → 🌟 Legend (10k)"
    ), inline=True)
    embed.set_footer(text=f"AJ's Assistant v{VERSION} • Prefix: {p}")
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="ping")
async def cmd_ping(ctx):
    embed = discord.Embed(
        title="🏓 Pong!",
        description=f"Latency: **{round(bot.latency * 1000)}ms**",
        color=0x43B581,
        timestamp=datetime.now(timezone.utc),
    )
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="uptime")
async def cmd_uptime(ctx):
    up = int(time.time() - start_time)
    d  = up // 86400
    h  = (up % 86400) // 3600
    m  = (up % 3600) // 60
    s  = up % 60
    started_at = datetime.fromtimestamp(start_time, tz=timezone.utc)
    embed = discord.Embed(
        title="⏱️ Bot Uptime",
        description=f"**{d}d {h}h {m}m {s}s**\nOnline since: {discord_ts(started_at, 'F')} ({discord_ts(started_at, 'R')})",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="botinfo")
async def cmd_botinfo(ctx):
    up     = int(time.time() - start_time)
    d, rem = divmod(up, 86400)
    h, rem = divmod(rem, 3600)
    m, s   = divmod(rem, 60)
    mem_mb = psutil.Process().memory_info().rss / 1024 / 1024
    cpu_p  = psutil.cpu_percent(interval=0.1)
    started_at = datetime.fromtimestamp(start_time, tz=timezone.utc)
    embed  = discord.Embed(
        title="🤖 AJ's Assistant — Bot Info",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.add_field(name="Version",        value=f"`{VERSION}`",                               inline=True)
    embed.add_field(name="Library",        value=f"`discord.py {discord.__version__}`",        inline=True)
    embed.add_field(name="Python",         value=f"`{platform.python_version()}`",             inline=True)
    embed.add_field(name="Ping",           value=f"`{round(bot.latency * 1000)}ms`",           inline=True)
    embed.add_field(name="Memory",         value=f"`{mem_mb:.1f} MB`",                         inline=True)
    embed.add_field(name="CPU",            value=f"`{cpu_p:.1f}%`",                            inline=True)
    embed.add_field(name="Uptime",         value=f"`{d}d {h}h {m}m {s}s`",                   inline=True)
    embed.add_field(name="Online Since",   value=discord_ts(started_at, "R"),                  inline=True)
    embed.add_field(name="Servers",        value=f"`{len(bot.guilds)}`",                       inline=True)
    embed.add_field(name="AI Model",       value="`LLaMA 3.3 70B`",                            inline=True)
    embed.add_field(name="Msgs Processed", value=f"`{msgs_processed:,}`",                      inline=True)
    embed.add_field(name="DB",             value="`✅ Connected`" if _db else "`❌ Offline`",    inline=True)
    embed.add_field(name="Groq Keys",      value=f"`{len(GROQ_KEYS)}`",                        inline=True)
    embed.set_footer(text="AJ's Assistant")
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="membercount")
async def cmd_membercount(ctx):
    guild = ctx.guild
    if not guild:
        await ctx.reply("This command can only be used in a server.", mention_author=False)
        return
    total   = guild.member_count
    humans  = sum(1 for m in guild.members if not m.bot)
    bots    = sum(1 for m in guild.members if m.bot)
    online  = sum(1 for m in guild.members if m.status == discord.Status.online)
    idle    = sum(1 for m in guild.members if m.status == discord.Status.idle)
    dnd     = sum(1 for m in guild.members if m.status == discord.Status.dnd)
    offline = sum(1 for m in guild.members if m.status == discord.Status.offline)
    embed   = discord.Embed(
        title=f"👥 {guild.name} — Member Count",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Total",      value=f"**{total:,}**",   inline=True)
    embed.add_field(name="Humans",     value=f"**{humans:,}**",  inline=True)
    embed.add_field(name="Bots",       value=f"**{bots:,}**",    inline=True)
    embed.add_field(name="🟢 Online",  value=f"**{online:,}**",  inline=True)
    embed.add_field(name="🌙 Idle",    value=f"**{idle:,}**",    inline=True)
    embed.add_field(name="🔴 DND",     value=f"**{dnd:,}**",     inline=True)
    embed.add_field(name="⚫ Offline", value=f"**{offline:,}**", inline=True)
    embed.add_field(name="Boost Level", value=f"Level {guild.premium_tier} ({guild.premium_subscription_count} boosts)", inline=True)
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="afk")
async def cmd_afk(ctx, *, reason: str = "AFK"):
    uid = ctx.author.id
    afk_users[uid] = {
        "reason": reason,
        "ts":     datetime.now(timezone.utc),
    }
    embed = discord.Embed(
        description=f"💤 **{ctx.author.display_name}** is now AFK: *{reason}*",
        color=0x99AAB5,
        timestamp=datetime.now(timezone.utc),
    )
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="snipe")
async def cmd_snipe(ctx):
    cid   = ctx.channel.id
    entry = snipe_cache.get(cid)
    if not entry:
        embed = discord.Embed(
            description="🔍 Nothing to snipe — no recently deleted messages found.",
            color=0x99AAB5,
            timestamp=datetime.now(timezone.utc),
        )
        await ctx.reply(embed=embed, mention_author=False)
        return
    age = (datetime.now(timezone.utc) - entry["ts"]).total_seconds()
    if age > SNIPE_EXPIRY:
        del snipe_cache[cid]
        embed = discord.Embed(
            description="⏰ That message expired (older than 5 minutes).",
            color=0x99AAB5,
            timestamp=datetime.now(timezone.utc),
        )
        await ctx.reply(embed=embed, mention_author=False)
        return
    embed = discord.Embed(
        description=entry["content"] or "*[no text content]*",
        color=0x5865F2,
        timestamp=entry["ts"],
    )
    embed.set_author(name=entry["author"], icon_url=entry["author_avatar"])
    embed.set_footer(text=f"Deleted {discord_ts(entry['ts'], 'R')} • sniped by {ctx.author.display_name}")
    await ctx.reply(embed=embed, mention_author=False)


# ─── OWNER-ONLY COMMANDS (all give proper feedback on denied) ─────────────────

@bot.command(name="shutdown")
async def cmd_shutdown(ctx):
    if not is_owner(ctx.author.id):
        await deny(ctx); return
    embed = discord.Embed(
        description="🔴 **Shutting down...** Goodbye!",
        color=0xFF3333,
        timestamp=datetime.now(timezone.utc),
    )
    await ctx.reply(embed=embed, mention_author=False)
    await shutdown("channel command")


@bot.command(name="scan")
async def cmd_scan(ctx):
    if not is_owner(ctx.author.id):
        await deny(ctx); return
    if not ctx.guild:
        await ctx.reply("Can only scan a server.", mention_author=False)
        return
    msg = await ctx.reply("🔍 Scanning server...", mention_author=False)
    scan  = await build_full_server_scan(ctx.guild)
    raw   = json.dumps({"scan": scan, "ts": datetime.now(timezone.utc).isoformat()}, indent=2)
    fn    = f"scan_{ctx.guild.id}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    f_obj = discord.File(fp=io.BytesIO(raw.encode()), filename=fn)
    embed = discord.Embed(
        title=f"🔍 Server Scan — {ctx.guild.name}",
        description=(
            f"Full scan complete.\n"
            f"**{len(ctx.guild.members)}** members • **{len(ctx.guild.channels)}** channels • **{len(ctx.guild.roles)}** roles\n"
            f"Generated: {discord_ts(datetime.now(timezone.utc), 'F')}"
        ),
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    if ctx.guild.icon:
        embed.set_thumbnail(url=ctx.guild.icon.url)
    await msg.delete()
    await ctx.reply(embed=embed, file=f_obj, mention_author=False)


@bot.command(name="debug")
async def cmd_debug(ctx):
    if not is_owner(ctx.author.id):
        await deny(ctx); return
    up   = int(time.time() - start_time)
    h, m, s = up // 3600, (up % 3600) // 60, up % 60
    errs = "\n".join(f"  [{e['ts'][11:19]}] {e['err'][:80]}" for e in list(error_log)[-5:]) or "  None"
    started_at = datetime.fromtimestamp(start_time, tz=timezone.utc)
    embed = discord.Embed(
        title="🛠️ Debug Info",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Uptime",          value=f"{h}h {m}m {s}s",                                    inline=True)
    embed.add_field(name="Online Since",    value=discord_ts(started_at, "R"),                           inline=True)
    embed.add_field(name="Msgs Processed",  value=str(msgs_processed),                                   inline=True)
    embed.add_field(name="Active Groq Key", value=f"#{(key_index % max(len(GROQ_KEYS),1))+1}/{len(GROQ_KEYS)}", inline=True)
    embed.add_field(name="Tracked Members", value=str(len(activity)),                                    inline=True)
    embed.add_field(name="Registered",      value=str(len(registry)),                                    inline=True)
    embed.add_field(name="Economy Entries", value=str(len(economy)),                                     inline=True)
    embed.add_field(name="Mod Log Entries", value=str(len(mod_logs)),                                    inline=True)
    embed.add_field(name="Log Channel",     value="✅ Set" if BOT_LOG_CHANNEL_ID else "❌ Not set",       inline=True)
    embed.add_field(name="DB",              value="✅ Connected" if _db else "❌ Offline",                 inline=True)
    embed.add_field(name="Last 5 Errors",   value=f"```{errs}```",                                       inline=False)
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="setprompt")
async def cmd_setprompt(ctx, *, text: str):
    global custom_prompt, prev_prompt
    if not is_owner(ctx.author.id):
        await deny(ctx); return
    prev_prompt   = custom_prompt
    custom_prompt = text
    await db_save_prompt()
    asyncio.create_task(bot_log("📝 Prompt Updated", f"Set by {ctx.author.mention}", fields=[("Preview", text[:200], False)], level="info"))
    embed = discord.Embed(description="✅ Prompt updated.", color=0x43B581, timestamp=datetime.now(timezone.utc))
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="revertprompt")
async def cmd_revertprompt(ctx):
    global custom_prompt, prev_prompt
    if not is_owner(ctx.author.id):
        await deny(ctx); return
    if prev_prompt is not None:
        custom_prompt = prev_prompt
        await db_save_prompt()
        embed = discord.Embed(description="✅ Reverted to previous prompt.", color=0x43B581, timestamp=datetime.now(timezone.utc))
    else:
        embed = discord.Embed(description="No previous prompt to revert to.", color=0xFFA500, timestamp=datetime.now(timezone.utc))
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="search")
async def cmd_search(ctx, *, query: str):
    if not is_owner(ctx.author.id):
        await deny(ctx); return
    result = await web_search(query)
    embed  = discord.Embed(
        title=f"🔎 Search: {query[:100]}",
        description=discord.utils.escape_mentions(result[:2000]),
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="clearmem")
async def cmd_clearmem(ctx, member: discord.Member = None):
    if not is_owner(ctx.author.id):
        await deny(ctx); return
    if not member:
        embed = discord.Embed(description="❌ Mention a user.", color=0xFF3333, timestamp=datetime.now(timezone.utc))
        await ctx.reply(embed=embed, mention_author=False)
        return
    clear_mem(member.id)
    embed = discord.Embed(description=f"✅ Memory cleared for **{member.display_name}**.", color=0x43B581, timestamp=datetime.now(timezone.utc))
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="whois")
async def cmd_whois(ctx, member: discord.Member = None):
    if not is_owner(ctx.author.id):
        await deny(ctx); return
    if not member:
        if ctx.message.mentions:
            member = ctx.message.mentions[0]
        else:
            embed = discord.Embed(description="❌ Mention a user.", color=0xFF3333, timestamp=datetime.now(timezone.utc))
            await ctx.reply(embed=embed, mention_author=False)
            return
    result = await execute_action(ctx.message, {"action": "whois", "user_id": str(member.id)})
    embed  = discord.Embed(
        title="👤 User Info",
        description=result,
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="report")
async def cmd_report(ctx):
    if not is_owner(ctx.author.id):
        await deny(ctx); return
    result = await execute_action(ctx.message, {"action": "report"})
    embed  = discord.Embed(
        description=result,
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    if ctx.guild and ctx.guild.icon:
        embed.set_thumbnail(url=ctx.guild.icon.url)
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="purge")
async def cmd_purge(ctx, count: int = 10):
    if not is_owner(ctx.author.id):
        await deny(ctx); return
    await execute_action(ctx.message, {"action": "purge", "count": count})


@bot.command(name="lockdown")
async def cmd_lockdown(ctx):
    if not is_owner(ctx.author.id):
        await deny(ctx); return
    result = await execute_action(ctx.message, {"action": "lockdown"})
    embed  = discord.Embed(description=result, color=0xFF3333, timestamp=datetime.now(timezone.utc))
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="unlock")
async def cmd_unlock(ctx):
    if not is_owner(ctx.author.id):
        await deny(ctx); return
    result = await execute_action(ctx.message, {"action": "unlock_all"})
    embed  = discord.Embed(description=result, color=0x43B581, timestamp=datetime.now(timezone.utc))
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="ask")
async def cmd_ask(ctx, *, question: str):
    # Pass question as content_override — never mutate ctx.message.content
    await process(ctx.message, content_override=question)


# ─── ECONOMY COMMANDS ─────────────────────────────────────────────────────────

@bot.command(name="balance", aliases=["bal", "coins", "ajax"])
async def cmd_balance(ctx, member: discord.Member = None):
    target = member or ctx.author
    econ   = get_econ(target.id)
    rank   = get_rank(econ["coins"])
    embed  = discord.Embed(title="🪙 Ajax Coins Balance", color=0xF5C400, timestamp=datetime.now(timezone.utc))
    embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
    embed.add_field(name="Balance",           value=f"**{econ['coins']:,} Ajax Coins**", inline=False)
    embed.add_field(name="Total Ever Earned", value=f"{econ['total_earned']:,}",         inline=True)
    embed.add_field(name="Messages Counted",  value=f"{econ['messages_counted']:,}",     inline=True)
    embed.add_field(name="Rank",              value=rank,                                inline=True)
    streak = econ.get("daily_streak", 0)
    if streak > 0:
        embed.add_field(name="Daily Streak", value=f"🔥 {streak} day{'s' if streak != 1 else ''}", inline=True)
    if econ["last_message_ts"]:
        last      = datetime.fromisoformat(econ["last_message_ts"])
        if last.tzinfo is None:
            last  = last.replace(tzinfo=timezone.utc)
        next_coin = last + timedelta(seconds=MSG_COOLDOWN)
        now       = datetime.now(timezone.utc)
        if next_coin > now:
            embed.set_footer(text=f"⏳ Next coin available")
            embed.add_field(name="Next Coin", value=discord_ts(next_coin, "R"), inline=True)
        else:
            embed.set_footer(text="✅ Your next message earns a coin!")
    else:
        embed.set_footer(text="💬 Send a message to start earning!")
    # Work cooldown info
    if econ.get("last_work"):
        last_work = datetime.fromisoformat(econ["last_work"])
        if last_work.tzinfo is None:
            last_work = last_work.replace(tzinfo=timezone.utc)
        next_work = last_work + timedelta(seconds=WORK_COOLDOWN)
        if next_work > datetime.now(timezone.utc):
            embed.add_field(name="Next Work", value=discord_ts(next_work, "R"), inline=True)
    await ctx.send(embed=embed)


@bot.command(name="leaderboard", aliases=["lb", "top"])
async def cmd_leaderboard(ctx):
    if not economy:
        embed = discord.Embed(description="No coins have been earned yet!", color=0xF5C400, timestamp=datetime.now(timezone.utc))
        await ctx.send(embed=embed)
        return
    sorted_users = sorted(economy.items(), key=lambda x: x[1].get("coins", 0), reverse=True)[:10]
    medals       = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
    lines        = []
    for i, (uid, udata) in enumerate(sorted_users):
        member = ctx.guild.get_member(int(uid)) if ctx.guild else None
        name   = member.display_name if member else f"Unknown ({uid})"
        coins  = udata.get("coins", 0)
        lines.append(f"{medals[i]} **{name}** — {coins:,} coins  {get_rank(coins)}")
    embed = discord.Embed(
        title="🏆 Ajax Coins Leaderboard",
        description="\n".join(lines) or "No entries yet.",
        color=0xF5C400,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Earn 1 coin per minute of chatting • +10 coins from .work every hour")
    await ctx.send(embed=embed)


@bot.command(name="work")
async def cmd_work(ctx):
    uid = ctx.author.id
    async with _econ_locks[uid]:
        econ     = get_econ(uid)
        now      = datetime.now(timezone.utc)
        last_work = econ.get("last_work")

        if last_work:
            lw_dt = datetime.fromisoformat(last_work)
            if lw_dt.tzinfo is None:
                lw_dt = lw_dt.replace(tzinfo=timezone.utc)
            elapsed = (now - lw_dt).total_seconds()
            if elapsed < WORK_COOLDOWN:
                next_work = lw_dt + timedelta(seconds=WORK_COOLDOWN)
                embed = discord.Embed(
                    title="😴 Still tired from the last shift...",
                    description=f"You can work again {discord_ts(next_work, 'R')} ({discord_ts(next_work, 'T')})",
                    color=0xFF3333,
                    timestamp=now,
                )
                embed.set_footer(text="Work cooldown: 1 hour")
                await ctx.reply(embed=embed, mention_author=False)
                return

        reward = 10
        econ["coins"]        += reward
        econ["total_earned"] += reward
        econ["last_work"]     = now.isoformat()
        await save_econ(uid)  # awaited inside lock — no race condition

    line = random.choice(WORK_LINES)
    next_work_dt = datetime.fromisoformat(econ["last_work"])
    if next_work_dt.tzinfo is None:
        next_work_dt = next_work_dt.replace(tzinfo=timezone.utc)
    next_work_ts = next_work_dt + timedelta(seconds=WORK_COOLDOWN)

    embed = discord.Embed(
        title="💼 Shift Complete!",
        description=f"**{ctx.author.display_name}** {line} **{reward} Ajax Coins**! 🪙",
        color=0xF5C400,
        timestamp=now,
    )
    embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
    embed.add_field(name="Earned",      value=f"+{reward} coins",              inline=True)
    embed.add_field(name="New Balance", value=f"{econ['coins']:,} coins",      inline=True)
    embed.add_field(name="Rank",        value=get_rank(econ["coins"]),         inline=True)
    embed.add_field(name="Next Shift",  value=discord_ts(next_work_ts, "R"),   inline=False)
    embed.set_footer(text="Work every hour to stack coins!")
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="pay")
async def cmd_pay(ctx, member: discord.Member = None, amount: int = 0):
    if not member:
        embed = discord.Embed(description="❌ Mention a user to pay.", color=0xFF3333, timestamp=datetime.now(timezone.utc))
        await ctx.reply(embed=embed, mention_author=False); return
    if member.bot:
        embed = discord.Embed(description="❌ You can't pay bots.", color=0xFF3333, timestamp=datetime.now(timezone.utc))
        await ctx.reply(embed=embed, mention_author=False); return
    if member == ctx.author:
        embed = discord.Embed(description="❌ You can't pay yourself.", color=0xFF3333, timestamp=datetime.now(timezone.utc))
        await ctx.reply(embed=embed, mention_author=False); return
    if amount <= 0:
        embed = discord.Embed(description="❌ Amount must be positive.", color=0xFF3333, timestamp=datetime.now(timezone.utc))
        await ctx.reply(embed=embed, mention_author=False); return
    if amount > 10_000:
        embed = discord.Embed(description="❌ Max transfer is **10,000 coins** at a time.", color=0xFF3333, timestamp=datetime.now(timezone.utc))
        await ctx.reply(embed=embed, mention_author=False); return

    # Deadlock-safe: always lock lower ID first
    first_id, second_id = sorted([ctx.author.id, member.id])
    async with _econ_locks[first_id]:
        async with _econ_locks[second_id]:
            sender   = get_econ(ctx.author.id)
            receiver = get_econ(member.id)
            if sender["coins"] < amount:
                embed = discord.Embed(description=f"❌ You only have **{sender['coins']:,} Ajax Coins**.", color=0xFF3333, timestamp=datetime.now(timezone.utc))
                await ctx.reply(embed=embed, mention_author=False)
                return
            sender["coins"]   -= amount
            receiver["coins"] += amount
            await save_econ(ctx.author.id)
            await save_econ(member.id)

    embed = discord.Embed(
        title="💸 Coins Sent!",
        description=f"{ctx.author.mention} sent **{amount:,} Ajax Coins** to {member.mention}!",
        color=0xF5C400,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Sent at", value=discord_ts(datetime.now(timezone.utc), "F"), inline=False)
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="daily")
async def cmd_daily(ctx):
    async with _econ_locks[ctx.author.id]:
        econ   = get_econ(ctx.author.id)
        now    = datetime.now(timezone.utc)
        streak = econ.get("daily_streak", 0)

        last_daily = econ.get("last_daily")
        if last_daily:
            last_dt = datetime.fromisoformat(last_daily)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            hours_since = (now - last_dt).total_seconds() / 3600
            if hours_since < 24:
                next_claim = last_dt + timedelta(hours=24)
                embed = discord.Embed(
                    title="⏳ Daily Already Claimed",
                    description=f"Come back {discord_ts(next_claim, 'R')} for your next reward!",
                    color=0xFF3333,
                    timestamp=now,
                )
                embed.set_footer(text=f"Current streak: {streak} day(s) 🔥")
                await ctx.reply(embed=embed, mention_author=False)
                return
            elif hours_since > 48:
                streak = 0

        streak = min(streak + 1, 999)
        reward = daily_reward(streak)

        econ["coins"]        += reward
        econ["total_earned"] += reward
        econ["last_daily"]    = now.isoformat()
        econ["daily_streak"]  = streak
        await save_econ(ctx.author.id)

    if streak >= 30:
        tier_label = "🌟 Month+ Streak!"
    elif streak >= 7:
        tier_label = "🔥 Week Streak!"
    elif streak >= 3:
        tier_label = "⚡ 3-Day Streak!"
    else:
        tier_label = "✨ Day " + str(streak)

    bar_filled = min(streak, 7)
    streak_bar = "🟨" * bar_filled + "⬜" * (7 - bar_filled)

    next_daily = now + timedelta(hours=24)

    embed = discord.Embed(
        title="🪙 Daily Reward Claimed!",
        color=0xF5C400,
        timestamp=now,
    )
    embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
    embed.add_field(name="Reward",      value=f"**+{reward:,} Ajax Coins**",      inline=True)
    embed.add_field(name="New Balance", value=f"**{econ['coins']:,} Ajax Coins**", inline=True)
    embed.add_field(name="Streak",      value=f"{streak_bar}\n**{tier_label}** (Day {streak})", inline=False)
    embed.add_field(name="Next Daily",  value=discord_ts(next_daily, "R"),         inline=False)
    embed.set_footer(text="Miss a day and your streak resets!")
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="give")
async def cmd_give(ctx, member: discord.Member = None, amount: int = 0):
    if not is_owner(ctx.author.id):
        await deny(ctx); return
    if not member:
        embed = discord.Embed(description="❌ Mention a user.", color=0xFF3333, timestamp=datetime.now(timezone.utc))
        await ctx.reply(embed=embed, mention_author=False); return
    if amount <= 0:
        embed = discord.Embed(description="❌ Amount must be positive.", color=0xFF3333, timestamp=datetime.now(timezone.utc))
        await ctx.reply(embed=embed, mention_author=False); return
    async with _econ_locks[member.id]:
        econ = get_econ(member.id)
        econ["coins"]        += amount
        econ["total_earned"] += amount
        await save_econ(member.id)
    log_mod("give_coins", member.id, ctx.author.id, str(amount))
    embed = discord.Embed(description=f"✅ Gave **{amount:,} Ajax Coins** to {member.mention}.", color=0x43B581, timestamp=datetime.now(timezone.utc))
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="take")
async def cmd_take(ctx, member: discord.Member = None, amount: int = 0):
    if not is_owner(ctx.author.id):
        await deny(ctx); return
    if not member:
        embed = discord.Embed(description="❌ Mention a user.", color=0xFF3333, timestamp=datetime.now(timezone.utc))
        await ctx.reply(embed=embed, mention_author=False); return
    if amount <= 0:
        embed = discord.Embed(description="❌ Amount must be positive.", color=0xFF3333, timestamp=datetime.now(timezone.utc))
        await ctx.reply(embed=embed, mention_author=False); return
    async with _econ_locks[member.id]:
        econ = get_econ(member.id)
        econ["coins"] = max(0, econ["coins"] - amount)
        await save_econ(member.id)
    log_mod("take_coins", member.id, ctx.author.id, str(amount))
    embed = discord.Embed(description=f"✅ Removed **{amount:,} Ajax Coins** from {member.mention}.", color=0x43B581, timestamp=datetime.now(timezone.utc))
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="coinreset")
async def cmd_coinreset(ctx, member: discord.Member = None):
    if not is_owner(ctx.author.id):
        await deny(ctx); return
    if not member:
        embed = discord.Embed(description="❌ Mention a user.", color=0xFF3333, timestamp=datetime.now(timezone.utc))
        await ctx.reply(embed=embed, mention_author=False); return
    async with _econ_locks[member.id]:
        key = str(member.id)
        economy[key] = {
            "coins": 0, "total_earned": 0,
            "last_message_ts": None, "messages_counted": 0,
            "last_daily": None, "daily_streak": 0, "last_work": None,
        }
        await save_econ(member.id)
    embed = discord.Embed(description=f"✅ Reset **{member.display_name}**'s Ajax Coins.", color=0x43B581, timestamp=datetime.now(timezone.utc))
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="backup")
async def cmd_backup(ctx):
    if not is_owner(ctx.author.id):
        await deny(ctx); return
    await _do_backup(ctx.author)
    embed = discord.Embed(description="📦 Backup sent to your DMs!", color=0x43B581, timestamp=datetime.now(timezone.utc))
    await ctx.reply(embed=embed, mention_author=False)


async def _do_backup(owner_user):
    backup_data = {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "economy":       economy,
        "registry":      registry,
        "mod_logs":      list(mod_logs),
        "memory":        memory,
        "custom_prompt": custom_prompt,
    }
    raw      = json.dumps(backup_data, indent=2, default=str)
    filename = f"backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    file_obj = discord.File(fp=io.BytesIO(raw.encode()), filename=filename)
    total_coins = sum(v.get("coins", 0) for v in economy.values())
    now = datetime.now(timezone.utc)
    embed = discord.Embed(
        title="📦 AJ's Assistant — Full Backup",
        description=f"Generated: {discord_ts(now, 'F')}",
        color=0xF5C400,
        timestamp=now,
    )
    embed.add_field(name="Economy Users",        value=f"{len(economy):,}",   inline=True)
    embed.add_field(name="Coins in Circulation", value=f"{total_coins:,} 🪙", inline=True)
    embed.add_field(name="Registered Users",     value=f"{len(registry):,}",  inline=True)
    embed.add_field(name="Mod Log Entries",      value=f"{len(mod_logs):,}",  inline=True)
    embed.add_field(name="Memory Entries",       value=f"{len(memory):,}",    inline=True)
    embed.set_footer(text=f"File: {filename}")
    try:
        dm = await owner_user.create_dm()
        await dm.send(embed=embed, file=file_obj)
    except discord.Forbidden:
        log.error("Could not DM backup to owner — DMs may be closed.")

# ─── BACKGROUND TASKS ─────────────────────────────────────────────────────────

# Fixed: use a proper time object, not a datetime evaluated at startup
_MIDNIGHT = time(0, 0, 0, tzinfo=timezone.utc)

@tasks.loop(time=_MIDNIGHT)
async def midnight_backup():
    if not OWNER_ID: return
    try:
        owner = await bot.fetch_user(OWNER_ID)
        await _do_backup(owner)
        log.info("Midnight backup sent.")
    except Exception as e:
        log.error(f"Midnight backup failed: {e}")


@tasks.loop(minutes=5)
async def snipe_cleanup():
    """Proactively evict expired snipe entries every 5 minutes."""
    now     = datetime.now(timezone.utc)
    expired = [cid for cid, entry in snipe_cache.items()
               if (now - entry["ts"]).total_seconds() > SNIPE_EXPIRY]
    for cid in expired:
        snipe_cache.pop(cid, None)
    if expired:
        log.info(f"Snipe cleanup: removed {len(expired)} expired entries.")

# ─── EVENTS ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    global _ready_fired, groq_clients
    if _ready_fired:
        return
    _ready_fired = True

    # Build Groq client pool once at startup
    groq_clients = {key: AsyncGroq(api_key=key) for key in GROQ_KEYS}
    log.info(f"Groq client pool built: {len(groq_clients)} client(s).")

    await db_init()
    await db_load()
    midnight_backup.start()
    snipe_cleanup.start()
    log.info(f"✅ AJ's Assistant ready as {bot.user} (ID: {bot.user.id})")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="the server 👁️")
    )
    guilds        = len(bot.guilds)
    total_members = sum(g.member_count for g in bot.guilds)
    started_at    = datetime.fromtimestamp(start_time, tz=timezone.utc)
    asyncio.create_task(bot_log(
        "🟢 Bot Started",
        f"**{bot.user}** is online • {discord_ts(started_at, 'F')}",
        fields=[
            ("Guilds",        str(guilds),         True),
            ("Total Members", str(total_members),  True),
            ("Economy Users", str(len(economy)),   True),
            ("Registered",    str(len(registry)),  True),
            ("Mod Logs",      str(len(mod_logs)),  True),
            ("Groq Keys",     str(len(GROQ_KEYS)), True),
            ("DB",            "✅ Yes" if _db else "❌ No", True),
            ("Log Channel",   "✅ Set" if BOT_LOG_CHANNEL_ID else "❌ Not set", True),
        ],
        level="startup",
    ))


@bot.event
async def on_message_delete(msg: discord.Message):
    if msg.author.bot: return
    if not isinstance(msg.channel, discord.TextChannel): return
    snipe_cache[msg.channel.id] = {
        "content":       msg.content,
        "author":        str(msg.author),
        "author_avatar": str(msg.author.display_avatar.url),
        "ts":            datetime.now(timezone.utc),
    }


@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot:
        return

    await bot.process_commands(msg)

    is_dm   = isinstance(msg.channel, discord.DMChannel)
    content = msg.content.strip()

    # ── AFK: remove sender's AFK if they send a non-command ──────────────────
    uid = msg.author.id
    if uid in afk_users and not content.startswith(CMD_PREFIX):
        data = afk_users.pop(uid)
        ago  = datetime.now(timezone.utc) - data["ts"]
        mins = int(ago.total_seconds() // 60)
        embed = discord.Embed(
            description=f"👋 Welcome back, {msg.author.mention}! Removed your AFK. *(was away {mins}m)*",
            color=0x43B581,
            timestamp=datetime.now(timezone.utc),
        )
        await msg.channel.send(embed=embed, delete_after=TEMP_MSG_TTL)

    # ── AFK: notify if someone pings an AFK user ──────────────────────────────
    for mentioned in msg.mentions:
        if mentioned.id in afk_users and mentioned.id != uid:
            data  = afk_users[mentioned.id]
            since = datetime.now(timezone.utc) - data["ts"]
            mins  = int(since.total_seconds() // 60)
            embed = discord.Embed(
                description=f"💤 **{mentioned.display_name}** is AFK: *{data['reason']}* *(for {mins}m)*",
                color=0x99AAB5,
                timestamp=datetime.now(timezone.utc),
            )
            await msg.channel.send(embed=embed, delete_after=TEMP_MSG_TTL)

    # ── Economy: earn 1 coin per MSG_COOLDOWN seconds of active chat ──────────
    if not is_dm and not content.startswith(CMD_PREFIX) and len(content) >= 5:
        async with _econ_locks[uid]:
            econ = get_econ(uid)
            now  = datetime.now(timezone.utc)
            can_earn = True
            if econ["last_message_ts"]:
                last = datetime.fromisoformat(econ["last_message_ts"])
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if (now - last).total_seconds() < MSG_COOLDOWN:
                    can_earn = False
            if can_earn:
                econ["coins"]            += 1
                econ["total_earned"]     += 1
                econ["messages_counted"] += 1
                econ["last_message_ts"]   = now.isoformat()
                await save_econ(uid)

    # ── Passive tracking ──────────────────────────────────────────────────────
    if not is_dm and bot.user not in (msg.mentions or []):
        track_activity(msg.author.id, msg.channel.id)
        register_user(msg.author)

    # ── AI: only respond when mentioned, replied to, or in DMs ──────────────
    mentioned    = bot.user in (msg.mentions or [])
    reply_to_bot = (
        msg.reference and
        hasattr(msg.reference, "resolved") and
        msg.reference.resolved and
        getattr(msg.reference.resolved, "author", None) == bot.user
    )
    is_prefix_cmd = content.startswith(CMD_PREFIX) and len(content) > 1

    if not (is_dm or mentioned or reply_to_bot) or is_prefix_cmd:
        return

    try:
        await process(msg, is_dm=is_dm)
    except Exception as e:
        err = f"on_message error: {e}"
        log.error(err)
        error_log.append({"ts": datetime.now(timezone.utc).isoformat(), "err": err})
        asyncio.create_task(bot_log(
            "❌ Unhandled Error in on_message",
            f"`{type(e).__name__}: {e}`",
            fields=[
                ("User",    f"{msg.author} (`{msg.author.id}`)", True),
                ("Channel", getattr(msg.channel, "mention", "DM"), True),
                ("Message", content[:300], False),
            ],
            level="error",
        ))
        try:
            embed = discord.Embed(
                description=f"❌ Error — `{type(e).__name__}: {e}`",
                color=0xFF3333,
                timestamp=datetime.now(timezone.utc),
            )
            await msg.reply(embed=embed, mention_author=False)
        except Exception:
            pass


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MemberNotFound):
        embed = discord.Embed(description="❌ Couldn't find that member.", color=0xFF3333, timestamp=datetime.now(timezone.utc))
        await ctx.reply(embed=embed, mention_author=False)
    elif isinstance(error, commands.MissingRequiredArgument):
        embed = discord.Embed(description=f"❌ Missing argument. Try `{CMD_PREFIX}help`.", color=0xFF3333, timestamp=datetime.now(timezone.utc))
        await ctx.reply(embed=embed, mention_author=False)
    elif isinstance(error, commands.BadArgument):
        embed = discord.Embed(description="❌ Invalid argument (make sure amounts are numbers).", color=0xFF3333, timestamp=datetime.now(timezone.utc))
        await ctx.reply(embed=embed, mention_author=False)
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        log.error(f"Command error: {error}")
        asyncio.create_task(bot_log(
            "⚠️ Command Error",
            f"`{type(error).__name__}: {error}`",
            fields=[("Command", ctx.message.content[:200], False)],
            level="warn",
        ))


@bot.event
async def on_member_join(member: discord.Member):
    asyncio.create_task(bot_log(
        "📥 Member Joined",
        f"**{member}** (`{member.id}`) joined **{member.guild.name}**",
        fields=[
            ("Account Created", discord_ts(member.created_at, "D"), True),
            ("Joined At",       discord_ts(datetime.now(timezone.utc), "F"), True),
            ("Member Count",    str(member.guild.member_count), True),
            ("Avatar",          str(member.display_avatar.url), False),
        ],
        level="info",
    ))


@bot.event
async def on_member_remove(member: discord.Member):
    asyncio.create_task(bot_log(
        "📤 Member Left / Removed",
        f"**{member}** (`{member.id}`) left **{member.guild.name}**",
        fields=[
            ("Left At",      discord_ts(datetime.now(timezone.utc), "F"), True),
            ("Member Count", str(member.guild.member_count), True),
        ],
        level="info",
    ))


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    asyncio.create_task(bot_log(
        "🔨 Member Banned",
        f"**{user}** (`{user.id}`) was banned from **{guild.name}** {discord_ts(datetime.now(timezone.utc), 'R')}",
        level="mod",
    ))


@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.User):
    asyncio.create_task(bot_log(
        "✅ Member Unbanned",
        f"**{user}** (`{user.id}`) was unbanned from **{guild.name}** {discord_ts(datetime.now(timezone.utc), 'R')}",
        level="mod",
    ))

# ─── HEALTH CHECK HTTP SERVER ─────────────────────────────────────────────────

async def health_handler(request):
    now = datetime.now(timezone.utc)
    return aiohttp_web.Response(
        text=json.dumps({
            "status":     "ok",
            "bot":        str(bot.user) if bot.user else "not ready",
            "uptime_s":   int(time.time() - start_time),
            "guilds":     len(bot.guilds),
            "latency_ms": round(bot.latency * 1000),
            "timestamp":  now.isoformat(),
        }),
        content_type="application/json",
    )

async def start_health_server():
    app    = aiohttp_web.Application()
    app.router.add_get("/",       health_handler)
    app.router.add_get("/health", health_handler)
    runner = aiohttp_web.AppRunner(app)
    await runner.setup()
    site   = aiohttp_web.TCPSite(runner, "0.0.0.0", HEALTH_PORT)
    await site.start()
    log.info(f"✅ Health check server running on port {HEALTH_PORT}")

# ─── GRACEFUL SHUTDOWN ────────────────────────────────────────────────────────

async def shutdown(signal_name: str = "SIGTERM"):
    asyncio.create_task(bot_log(
        "🔴 Bot Shutting Down",
        f"Received `{signal_name}` — saving data and disconnecting… {discord_ts(datetime.now(timezone.utc), 'T')}",
        level="shutdown",
    ))
    await asyncio.sleep(1.5)
    await bot.close()

def _handle_signal(sig, loop):
    name = signal.Signals(sig).name
    log.info(f"Received {name}, shutting down…")
    loop.create_task(shutdown(name))

# ─── RUN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN not set in .env")
    if not GROQ_KEYS:
        log.warning("⚠️  No GROQ keys found! Add GROQ_KEY_1 through GROQ_KEY_10 in .env")
    if not BOT_LOG_CHANNEL_ID:
        log.warning("⚠️  BOT_LOG_CHANNEL_ID not set — bot logs won't post to Discord.")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig, loop)
        except NotImplementedError:
            pass

    loop.run_until_complete(start_health_server())

    try:
        loop.run_until_complete(bot.start(DISCORD_TOKEN))
    except (KeyboardInterrupt, SystemExit):
        loop.run_until_complete(shutdown("KeyboardInterrupt"))
    finally:
        loop.close()
