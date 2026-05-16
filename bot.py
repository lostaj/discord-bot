import os
import re
import io
import json
import time
import asyncio
import logging
import random
import signal
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands
from groq import AsyncGroq
import aiohttp
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv()

# ─── CONFIG ────────────────────────────────────────────────────────────────────

DISCORD_TOKEN      = os.getenv("DISCORD_TOKEN")
OWNER_ID           = int(os.getenv("OWNER_ID", "0"))
CMD_PREFIX         = os.getenv("CMD_PREFIX", ".")
BOT_LOG_CHANNEL_ID = int(os.getenv("BOT_LOG_CHANNEL_ID", "0"))   # <── set in Railway env

GROQ_KEYS = [k for k in [os.getenv(f"GROQ_KEY_{i}") for i in range(1, 11)] if k]
MONGO_URI = os.getenv("MONGO_URI", "")

# ─── ECONOMY CONFIG ────────────────────────────────────────────────────────────

MSG_RARITY   = 1    # 1 = every qualifying message; >1 = 1-in-N chance
MSG_COOLDOWN = 60   # seconds between coin-earning messages
MAX_HIST     = 14
MAX_PURGE    = 200

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

MOD_ACTIONS = {
    "create_role", "delete_role", "rename_role",
    "create_channel", "delete_channel", "rename_channel", "create_category",
    "give_role", "remove_role",
    "ban", "kick", "mute", "unban",
    "purge", "lock_channel", "unlock_channel", "lockdown", "unlock_all",
    "set_trust",
}

def get_rank(coins: int) -> str:
    rank = RANKS[0][1]
    for threshold, title in RANKS:
        if coins >= threshold:
            rank = title
    return rank

# ─── LOGGING ───────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── SYSTEM PROMPT ─────────────────────────────────────────────────────────────

DEFAULT_PROMPT = """You are AJ's Assistant, a powerful Discord server bot. You manage roles, channels, members, and more through natural language commands.

When the owner or a trusted moderator asks you to do something like:
- "make a role called X" / "create a role named X"
- "make a channel called X" / "create a channel named X"
- "delete the role X" / "remove the channel X"
- "ban @user" / "kick @user" / "mute @user for 10 mins"
- "purge 20 messages" / "clear 50 messages"
- "lock this channel" / "unlock this channel"
- "lock the server down" / "lockdown"
- "rename this channel to X" / "rename role X to Y"
- "give @user the X role" / "remove the X role from @user"
- "make @user admin" / "add mod role to @user"
- "set @user trust to 4"
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
{"action":"set_trust","user_id":"123","level":4}
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

BASE_PROMPT = DEFAULT_PROMPT

# ─── DISCORD SETUP ─────────────────────────────────────────────────────────────

intents = discord.Intents.all()
bot     = commands.Bot(command_prefix=CMD_PREFIX, intents=intents, help_command=None)

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

memory:   dict  = {}
registry: dict  = {}
mod_logs: deque = deque(maxlen=300)
dm_logs:  dict  = {}
economy:  dict  = {}
activity: dict  = {}

custom_prompt: str | None = None
prev_prompt:   str | None = None

histories:   dict  = defaultdict(list)
rate_limits: dict  = defaultdict(float)
error_log:   deque = deque(maxlen=50)

key_index      = 0
start_time     = time.time()
msgs_processed = 0

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
    color: int = 0x5865F2,
    fields: list[tuple[str, str, bool]] | None = None,
    level: str = "info",
):
    """
    Send a log embed to the configured BOT_LOG_CHANNEL_ID.
    Also always logs to the Python logger.

    level: "info" | "warn" | "error" | "mod" | "security"
    """
    color_map = {
        "info":     0x5865F2,   # blurple
        "warn":     0xFFA500,   # orange
        "error":    0xFF3333,   # red
        "mod":      0x00C853,   # green
        "security": 0xFF6B00,   # amber
        "shutdown": 0x99AAB5,   # grey
        "startup":  0x43B581,   # green
    }
    color = color_map.get(level, color)

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
        }
    e = economy[key]
    e.setdefault("last_daily", None)
    e.setdefault("daily_streak", 0)
    return e

def save_econ(uid: int):
    asyncio.ensure_future(db_save_economy(str(uid)))

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

def get_trust(uid: int) -> int:
    if uid == OWNER_ID:
        return 5
    return registry.get(str(uid), {}).get("trust", 2)

def set_trust(uid: int, level: int):
    registry.setdefault(str(uid), {})["trust"] = level
    registry[str(uid)]["uid"] = str(uid)
    asyncio.ensure_future(db_save_user(str(uid)))

def get_mem(uid: int) -> dict:
    return memory.get(str(uid), {})

def update_mem(uid: int, data: dict):
    memory.setdefault(str(uid), {}).update(data)
    asyncio.ensure_future(db_save_mem(str(uid)))

def clear_mem(uid: int):
    memory.pop(str(uid), None)
    asyncio.ensure_future(db_save_mem(str(uid)))

def log_mod(action: str, target, by: int, reason: str = ""):
    mod_logs.append({
        "action": action,
        "target": str(target),
        "by":     str(by),
        "reason": reason,
        "ts":     datetime.now(timezone.utc).isoformat(),
    })
    asyncio.ensure_future(db_save_mod_logs())

def track_activity(uid: int, cid: int):
    key = str(uid)
    activity.setdefault(key, {"count": 0, "last": None, "channels": {}})
    activity[key]["count"] += 1
    activity[key]["last"]   = datetime.now(timezone.utc).isoformat()
    activity[key]["channels"][str(cid)] = activity[key]["channels"].get(str(cid), 0) + 1

def register_user(author: discord.Member | discord.User):
    key    = str(author.id)
    is_new = key not in registry
    registry.setdefault(key, {"trust": 2})
    registry[key]["uid"]          = key
    registry[key]["username"]     = author.name
    registry[key]["display_name"] = author.display_name
    if is_new:
        asyncio.ensure_future(db_save_user(key))

def build_context(msg: discord.Message, trust: int) -> str:
    is_owner = msg.author.id == OWNER_ID
    roles    = [r.name for r in getattr(msg.author, "roles", []) if r.name != "@everyone"]
    mem      = get_mem(msg.author.id)

    parts = [f"[CTX] User={msg.author.name}(ID={msg.author.id}) Trust={trust}"]
    if is_owner:
        parts.append("OWNER=true")
    if roles:
        parts.append(f"Roles={','.join(roles[:5])}")
    if mem:
        parts.append("Mem=" + ",".join(f"{k}={v}" for k, v in list(mem.items())[:4]))
    if msg.mentions:
        parts.append("Mentions=" + ",".join(f"{m.name}:{m.id}" for m in msg.mentions[:3]))
    if msg.reference and hasattr(msg.reference, "resolved") and msg.reference.resolved:
        ref = msg.reference.resolved
        parts.append(f'ReplyTo={ref.author.name}:"{ref.content[:80]}"')
    return " | ".join(parts)

# ─── SUSPICIOUS MESSAGE DETECTION ─────────────────────────────────────────────

INJECTION_PATTERNS = [
    r"ignore (all |previous |your )?instructions",
    r"new system prompt",
    r"you are now",
    r"forget everything",
    r"disregard (all |your )?",
    r"act as (a |an )?(?!aj)",          # "act as a DAN" etc
    r"jailbreak",
    r"pretend (you are|to be)",
    r"override (your )?(instructions|prompt|rules)",
    r"\[system\]",
    r"<\|",                              # token injection attempts
]
_injection_re = re.compile("|".join(INJECTION_PATTERNS), re.IGNORECASE)

def is_suspicious(content: str) -> bool:
    return bool(_injection_re.search(content))

# ─── GROQ ──────────────────────────────────────────────────────────────────────

async def call_ai(history: list, system: str | None = None) -> str:
    global key_index, msgs_processed

    if not GROQ_KEYS:
        return '{"action":"chat","message":"No Groq API keys configured. Add GROQ_KEY_1 to your .env file."}'

    sys_content = system if system else active_prompt()

    for _ in range(len(GROQ_KEYS)):
        key = GROQ_KEYS[key_index % len(GROQ_KEYS)]
        try:
            client = AsyncGroq(api_key=key)
            resp   = await asyncio.wait_for(
                client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "system", "content": sys_content}] + history,
                    max_tokens=400,
                    temperature=0.4,
                ),
                timeout=15.0
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

# ─── ACTION EXECUTOR ───────────────────────────────────────────────────────────

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
            asyncio.ensure_future(bot_log(
                "🛡️ Role Created",
                f"**{name}** created by {author.mention}",
                fields=[("Role", role.mention, True), ("Requested By", author.display_name, True)],
                level="mod"
            ))
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
            asyncio.ensure_future(bot_log(
                "🗑️ Role Deleted",
                f"**{name}** deleted by {author.mention}",
                level="mod"
            ))
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
            asyncio.ensure_future(bot_log(
                "✏️ Role Renamed",
                f"**{old}** → **{new}** by {author.mention}",
                level="mod"
            ))
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
            asyncio.ensure_future(bot_log(
                "📢 Channel Created",
                f"**#{name}** ({ch_type}) by {author.mention}",
                level="mod"
            ))
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
            asyncio.ensure_future(bot_log(
                "🗑️ Channel Deleted",
                f"**#{name}** deleted by {author.mention}",
                level="mod"
            ))
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
            asyncio.ensure_future(bot_log(
                "✏️ Channel Renamed",
                f"**#{old}** → **#{new}** by {author.mention}",
                level="mod"
            ))
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
            asyncio.ensure_future(bot_log(
                "📁 Category Created",
                f"**{name}** by {author.mention}",
                level="mod"
            ))
            return f"✅ Category **{cat.name}** created!"
        except discord.Forbidden:
            return "❌ Missing permissions."
        except Exception as e:
            return f"❌ Error: {e}"

    if action == "give_role":
        if not guild: return "Can't do that in DMs."
        uid       = int(data.get("user_id", 0))
        role_name = data.get("role_name", "")
        reason    = data.get("reason", f"Requested by {author.name}")
        member    = guild.get_member(uid) or (msg.mentions[0] if msg.mentions else None)
        if not member: return "❌ Couldn't find that user."
        role = discord.utils.find(lambda r: r.name.lower() == role_name.lower(), guild.roles)
        if not role: return f"❌ Role **{role_name}** not found."
        try:
            await member.add_roles(role, reason=reason)
            log_mod("give_role", member.id, author.id, role_name)
            asyncio.ensure_future(bot_log(
                "🎖️ Role Given",
                f"**{role_name}** given to {member.mention} by {author.mention}",
                level="mod"
            ))
            return f"✅ Gave **{role_name}** to {member.mention}."
        except discord.Forbidden:
            return "❌ Missing permissions."
        except Exception as e:
            return f"❌ Error: {e}"

    if action == "remove_role":
        if not guild: return "Can't do that in DMs."
        uid       = int(data.get("user_id", 0))
        role_name = data.get("role_name", "")
        reason    = data.get("reason", f"Requested by {author.name}")
        member    = guild.get_member(uid) or (msg.mentions[0] if msg.mentions else None)
        if not member: return "❌ Couldn't find that user."
        role = discord.utils.find(lambda r: r.name.lower() == role_name.lower(), guild.roles)
        if not role: return f"❌ Role **{role_name}** not found."
        try:
            await member.remove_roles(role, reason=reason)
            log_mod("remove_role", member.id, author.id, role_name)
            asyncio.ensure_future(bot_log(
                "🎖️ Role Removed",
                f"**{role_name}** removed from {member.mention} by {author.mention}",
                level="mod"
            ))
            return f"✅ Removed **{role_name}** from {member.mention}."
        except discord.Forbidden:
            return "❌ Missing permissions."
        except Exception as e:
            return f"❌ Error: {e}"

    if action == "ban":
        if not guild: return "Can't do that in DMs."
        uid    = int(data.get("user_id", 0))
        reason = data.get("reason", f"Banned by {author.name}")
        member = guild.get_member(uid) or (msg.mentions[0] if msg.mentions else None)
        if not member: return "❌ Couldn't find that user."
        try:
            await guild.ban(member, reason=reason)
            log_mod("ban", member.id, author.id, reason)
            asyncio.ensure_future(bot_log(
                "🔨 Member Banned",
                f"**{member}** (`{member.id}`) banned by {author.mention}",
                fields=[("Reason", reason, False)],
                level="mod"
            ))
            return f"🔨 **{member.name}** has been banned. Reason: {reason}"
        except discord.Forbidden:
            return "❌ Missing permissions to ban."
        except Exception as e:
            return f"❌ Error: {e}"

    if action == "kick":
        if not guild: return "Can't do that in DMs."
        uid    = int(data.get("user_id", 0))
        reason = data.get("reason", f"Kicked by {author.name}")
        member = guild.get_member(uid) or (msg.mentions[0] if msg.mentions else None)
        if not member: return "❌ Couldn't find that user."
        try:
            await guild.kick(member, reason=reason)
            log_mod("kick", member.id, author.id, reason)
            asyncio.ensure_future(bot_log(
                "👢 Member Kicked",
                f"**{member}** (`{member.id}`) kicked by {author.mention}",
                fields=[("Reason", reason, False)],
                level="mod"
            ))
            return f"👢 **{member.name}** has been kicked. Reason: {reason}"
        except discord.Forbidden:
            return "❌ Missing permissions to kick."
        except Exception as e:
            return f"❌ Error: {e}"

    if action == "mute":
        if not guild: return "Can't do that in DMs."
        uid    = int(data.get("user_id", 0))
        secs   = int(data.get("seconds", 300))
        reason = data.get("reason", f"Muted by {author.name}")
        member = guild.get_member(uid) or (msg.mentions[0] if msg.mentions else None)
        if not member: return "❌ Couldn't find that user."
        until  = discord.utils.utcnow() + timedelta(seconds=secs)
        try:
            await member.timeout(until, reason=reason)
            log_mod("mute", member.id, author.id, f"{secs}s — {reason}")
            asyncio.ensure_future(bot_log(
                "🔇 Member Muted",
                f"**{member}** muted for {secs//60}m by {author.mention}",
                fields=[("Reason", reason, False), ("Duration", f"{secs}s", True)],
                level="mod"
            ))
            return f"🔇 **{member.name}** muted for {secs // 60} min(s). Reason: {reason}"
        except discord.Forbidden:
            return "❌ Missing permissions to mute."
        except Exception as e:
            return f"❌ Error: {e}"

    if action == "unban":
        if not guild: return "Can't do that in DMs."
        uid    = int(data.get("user_id", 0))
        reason = data.get("reason", "Appeal accepted")
        try:
            user = await bot.fetch_user(uid)
            await guild.unban(user, reason=reason)
            log_mod("unban", uid, author.id, reason)
            asyncio.ensure_future(bot_log(
                "✅ Member Unbanned",
                f"**{user}** (`{uid}`) unbanned by {author.mention}",
                level="mod"
            ))
            return f"✅ **{user.name}** unbanned."
        except discord.NotFound:
            return "❌ User not found in ban list."
        except Exception as e:
            return f"❌ Error: {e}"

    if action == "purge":
        if not guild: return "Can't do that in DMs."
        count  = min(int(data.get("count", 10)), MAX_PURGE)
        reason = data.get("reason", f"Purge by {author.name}")
        try:
            deleted = await msg.channel.purge(limit=count + 1)
            log_mod("purge", msg.channel.id, author.id, f"{len(deleted)} msgs")
            asyncio.ensure_future(bot_log(
                "🗑️ Messages Purged",
                f"**{len(deleted)-1}** messages purged in {msg.channel.mention} by {author.mention}",
                level="mod"
            ))
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
            asyncio.ensure_future(bot_log(
                "🔒 Channel Locked",
                f"{ch.mention} locked by {author.mention}",
                level="mod"
            ))
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
            asyncio.ensure_future(bot_log(
                "🔓 Channel Unlocked",
                f"{ch.mention} unlocked by {author.mention}",
                level="mod"
            ))
            return f"🔓 {ch.mention} is now unlocked."
        except discord.Forbidden:
            return "❌ Missing permissions."
        except Exception as e:
            return f"❌ Error: {e}"

    if action == "lockdown":
        if not guild: return "Can't do that in DMs."
        reason = data.get("reason", f"Lockdown by {author.name}")
        locked = 0
        for ch in guild.text_channels:
            try:
                ow = ch.overwrites_for(guild.default_role)
                ow.send_messages = False
                await ch.set_permissions(guild.default_role, overwrite=ow)
                locked += 1
            except Exception:
                pass
        log_mod("lockdown", 0, author.id, reason)
        asyncio.ensure_future(bot_log(
            "🚨 SERVER LOCKDOWN",
            f"**{locked} channels** locked by {author.mention}\nReason: {reason}",
            level="warn"
        ))
        return f"🔒 **LOCKDOWN ACTIVE** — {locked} channels locked. Reason: {reason}"

    if action == "unlock_all":
        if not guild: return "Can't do that in DMs."
        reason   = data.get("reason", f"Unlock all by {author.name}")
        unlocked = 0
        for ch in guild.text_channels:
            try:
                ow = ch.overwrites_for(guild.default_role)
                ow.send_messages = None
                await ch.set_permissions(guild.default_role, overwrite=ow)
                unlocked += 1
            except Exception:
                pass
        log_mod("unlock_all", 0, author.id, reason)
        asyncio.ensure_future(bot_log(
            "🔓 Server Unlocked",
            f"**{unlocked} channels** unlocked by {author.mention}",
            level="mod"
        ))
        return f"🔓 All channels unlocked ({unlocked} total)."

    if action == "set_trust":
        uid   = int(data.get("user_id", 0))
        level = int(data.get("level", 2))
        set_trust(uid, level)
        member = guild.get_member(uid) if guild else None
        name   = member.display_name if member else str(uid)
        asyncio.ensure_future(bot_log(
            "🔑 Trust Level Changed",
            f"**{name}** set to trust **{level}** by {author.mention}",
            level="mod"
        ))
        return f"✅ Set **{name}**'s trust level to **{level}**."

    if action == "whois":
        if not guild: return "Can't do that in DMs."
        uid  = int(data.get("user_id", 0))
        tgt  = guild.get_member(uid) or (msg.mentions[0] if msg.mentions else None)
        if not tgt: return "❌ User not found."
        act   = activity.get(str(tgt.id), {})
        warns = [e for e in mod_logs if e["target"] == str(tgt.id)]
        mem   = get_mem(tgt.id)
        econ  = get_econ(tgt.id)
        join  = getattr(tgt, "joined_at", None)
        roles = [r.name for r in tgt.roles if r.name != "@everyone"]
        lines = [
            f"**👤 {tgt.display_name}** (`{tgt.name}` | `{tgt.id}`)",
            f"Joined: {join.strftime('%Y-%m-%d') if join else 'N/A'} | Created: {tgt.created_at.strftime('%Y-%m-%d')}",
            f"Trust: **{get_trust(tgt.id)}** | Session msgs: {act.get('count', 0)} | Last active: {(act.get('last') or 'never')[:10]}",
            f"Roles: {', '.join(roles[:8]) or 'none'}",
            f"Mod actions: {len(warns)}",
            f"Ajax Coins: **{econ['coins']:,}** ({get_rank(econ['coins'])})",
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
        return (
            f"**📊 Server Report — {guild.name}**\n"
            f"Members: {guild.member_count} | Channels: {len(guild.channels)} | Roles: {len(guild.roles)}\n"
            f"Most active this week: {', '.join(top_names) or 'no data'}\n"
            f"Inactive 30+ days (session): {inactive}\n"
            f"Tracked this session: {len(activity)}\n"
            f"Mod actions this week: {recent_actions}\n"
            f"Ajax Coin richest: {', '.join(rich_names) or 'no data'}"
        )

    return f"❓ Unknown action: {action}"

# ─── PARSE AI RESPONSE ─────────────────────────────────────────────────────────

def parse_ai_json(raw: str) -> dict | None:
    raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass
    return None

# ─── COMMANDS ──────────────────────────────────────────────────────────────────

@bot.command(name="help")
async def cmd_help(ctx):
    p = CMD_PREFIX
    embed = discord.Embed(
        title="🤖 AJ's Assistant",
        description=(
            "**Mention me or reply to me** and speak naturally — I understand English!\n"
            "You can also DM me directly.\n\n"
            "**Natural Language Examples:**\n"
            f"`@bot make a role called aj shabeel`\n"
            f"`@bot create a voice channel called music`\n"
            f"`@bot ban @user spamming`\n"
            f"`@bot mute @user for 10 minutes`\n"
            f"`@bot give @user the VIP role`\n"
            f"`@bot purge 20 messages`\n"
            f"`@bot lock this channel`\n"
            f"`@bot what's the capital of France?`"
        ),
        color=discord.Color.blurple()
    )
    embed.add_field(name="🛠️ AI & Mod Commands", value=(
        f"`{p}ask <question>` — Chat with AJ's Assistant\n"
        f"`{p}whois @user` — User info (trust 4+)\n"
        f"`{p}report` — Server stats (trust 4+)\n"
        f"`{p}purge <n>` — Delete messages (trust 4+)\n"
        f"`{p}search <query>` — Web search (trust 4+)\n"
        f"`{p}lockdown` — Lock all channels (owner)\n"
        f"`{p}unlock` — Unlock all channels (owner)\n"
        f"`{p}setprompt <text>` — Change AI personality (owner)\n"
        f"`{p}revertprompt` — Undo prompt change (owner)\n"
        f"`{p}clearmem @user` — Clear user memory (owner)\n"
        f"`{p}backup` — DM a full data backup as JSON (owner)\n"
        f"`{p}debug` — Bot status (owner)"
    ), inline=False)
    embed.add_field(name="🪙 Ajax Coins Economy", value=(
        f"`{p}daily` — Claim your daily reward (50+ coins, streak bonus)\n"
        f"`{p}balance [@user]` — Check your coin balance\n"
        f"`{p}leaderboard` — Top 10 richest members\n"
        f"`{p}pay @user <amount>` — Send coins to someone\n"
        f"`{p}give @user <amount>` — Give coins (trust 4+)\n"
        f"`{p}take @user <amount>` — Remove coins (trust 4+)\n"
        f"`{p}coinreset @user` — Reset a user's coins (owner)\n\n"
        f"💬 Earn **1 Ajax Coin** per message (1-min cooldown, 5+ chars)\n"
        f"📅 Claim **50–110 coins** daily with streak bonuses (max day 7)"
    ), inline=False)
    embed.add_field(name="🔒 Trust Levels", value=(
        "**0** — Blocked\n**1** — Restricted\n**2** — Regular (default)\n"
        "**3** — Trusted\n**4** — Moderator\n**5** — Owner/Admin"
    ), inline=True)
    embed.add_field(name="🪙 Ajax Coin Ranks", value=(
        "💀 Penniless → 🪨 Gravel Rat (10)\n🥉 Bronze Hoarder (50) → 🥈 Silver Stacker (150)\n"
        "🥇 Gold Grinder (500) → 💎 Diamond Hands (1k)\n👑 Ajax Royalty (5k) → 🌟 Ajax Legend (10k)"
    ), inline=True)
    embed.set_footer(text=f"AJ's Assistant • Powered by Groq LLaMA • Prefix: {p}")
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="debug")
async def cmd_debug(ctx):
    if ctx.author.id != OWNER_ID:
        return
    up   = int(time.time() - start_time)
    h, m, s = up // 3600, (up % 3600) // 60, up % 60
    errs = "\n".join(f"  [{e['ts'][11:19]}] {e['err'][:80]}" for e in list(error_log)[-5:]) or "  None"
    await ctx.reply(
        f"**🛠️ Debug Info — AJ's Assistant**\n"
        f"Uptime: {h}h {m}m {s}s\n"
        f"Messages processed: {msgs_processed}\n"
        f"Active Groq key: #{(key_index % max(len(GROQ_KEYS), 1)) + 1} / {len(GROQ_KEYS)}\n"
        f"Session tracked members: {len(activity)}\n"
        f"Registered users: {len(registry)}\n"
        f"Economy entries: {len(economy)}\n"
        f"Mod log entries: {len(mod_logs)}\n"
        f"Bot log channel: {'✅ set' if BOT_LOG_CHANNEL_ID else '❌ not set'}\n"
        f"Last 5 errors:\n{errs}",
        mention_author=False
    )


@bot.command(name="setprompt")
async def cmd_setprompt(ctx, *, text: str):
    global custom_prompt, prev_prompt
    if ctx.author.id != OWNER_ID:
        return
    prev_prompt   = custom_prompt
    custom_prompt = text
    await db_save_prompt()
    asyncio.ensure_future(bot_log(
        "📝 Prompt Updated",
        f"Custom prompt set by <@{ctx.author.id}>",
        fields=[("Preview", text[:200], False)],
        level="info"
    ))
    await ctx.reply("✅ Prompt updated.", mention_author=False)


@bot.command(name="revertprompt")
async def cmd_revertprompt(ctx):
    global custom_prompt, prev_prompt
    if ctx.author.id != OWNER_ID:
        return
    if prev_prompt is not None:
        custom_prompt = prev_prompt
        await db_save_prompt()
        await ctx.reply("✅ Reverted to previous prompt.", mention_author=False)
    else:
        await ctx.reply("No previous prompt to revert to.", mention_author=False)


@bot.command(name="search")
async def cmd_search(ctx, *, query: str):
    if ctx.author.id != OWNER_ID and get_trust(ctx.author.id) < 4:
        return
    result = await web_search(query)
    await ctx.reply(result, mention_author=False)


@bot.command(name="clearmem")
async def cmd_clearmem(ctx, member: discord.Member = None):
    if ctx.author.id != OWNER_ID:
        return
    if not member:
        await ctx.reply("Mention a user.", mention_author=False)
        return
    clear_mem(member.id)
    await ctx.reply(f"✅ Memory cleared for **{member.display_name}**.", mention_author=False)


@bot.command(name="whois")
async def cmd_whois(ctx, member: discord.Member = None):
    if get_trust(ctx.author.id) < 4:
        return
    if not member:
        if ctx.message.mentions:
            member = ctx.message.mentions[0]
        else:
            await ctx.reply("Mention a user.", mention_author=False)
            return
    result = await execute_action(ctx.message, {"action": "whois", "user_id": str(member.id)})
    await ctx.reply(result, mention_author=False)


@bot.command(name="report")
async def cmd_report(ctx):
    if get_trust(ctx.author.id) < 4:
        return
    result = await execute_action(ctx.message, {"action": "report"})
    await ctx.reply(result, mention_author=False)


@bot.command(name="purge")
async def cmd_purge(ctx, count: int = 10):
    if get_trust(ctx.author.id) < 4:
        return
    await execute_action(ctx.message, {"action": "purge", "count": count})


@bot.command(name="lockdown")
async def cmd_lockdown(ctx):
    if get_trust(ctx.author.id) < 5:
        return
    result = await execute_action(ctx.message, {"action": "lockdown"})
    await ctx.reply(result, mention_author=False)


@bot.command(name="unlock")
async def cmd_unlock(ctx):
    if get_trust(ctx.author.id) < 5:
        return
    result = await execute_action(ctx.message, {"action": "unlock_all"})
    await ctx.reply(result, mention_author=False)


@bot.command(name="ask")
async def cmd_ask(ctx, *, question: str):
    ctx.message.content = question
    await process(ctx.message)


# ─── ECONOMY COMMANDS ──────────────────────────────────────────────────────────

@bot.command(name="balance", aliases=["bal", "coins", "ajax"])
async def cmd_balance(ctx, member: discord.Member = None):
    target = member or ctx.author
    econ   = get_econ(target.id)
    rank   = get_rank(econ["coins"])
    embed  = discord.Embed(title="🪙 Ajax Coins Balance", color=0xF5C400)
    embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
    embed.add_field(name="Current Balance",       value=f"**{econ['coins']:,} Ajax Coins**", inline=False)
    embed.add_field(name="Total Ever Earned",     value=f"{econ['total_earned']:,}",         inline=True)
    embed.add_field(name="Messages That Counted", value=f"{econ['messages_counted']:,}",     inline=True)
    embed.add_field(name="Rank",                  value=rank,                                inline=False)
    if econ["last_message_ts"]:
        last = datetime.fromisoformat(econ["last_message_ts"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        next_ts = last + timedelta(seconds=MSG_COOLDOWN)
        now     = datetime.now(timezone.utc)
        if next_ts > now:
            remaining = int((next_ts - now).total_seconds())
            embed.set_footer(text=f"⏳ Next coin in ~{remaining}s — keep chatting!")
        else:
            embed.set_footer(text="✅ Your next message can earn a coin!")
    else:
        embed.set_footer(text="💬 Send a message to start earning Ajax Coins!")
    await ctx.send(embed=embed)


@bot.command(name="leaderboard", aliases=["lb", "top"])
async def cmd_leaderboard(ctx):
    if not economy:
        await ctx.send("No coins have been earned yet!")
        return
    sorted_users = sorted(economy.items(), key=lambda x: x[1].get("coins", 0), reverse=True)[:10]
    medals       = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
    lines        = []
    for i, (uid, udata) in enumerate(sorted_users):
        member = ctx.guild.get_member(int(uid)) if ctx.guild else None
        name   = member.display_name if member else f"Unknown ({uid})"
        coins  = udata.get("coins", 0)
        lines.append(f"{medals[i]} **{name}** — {coins:,} coins  {get_rank(coins)}")
    embed = discord.Embed(title="🏆 Ajax Coins Leaderboard", color=0xF5C400)
    embed.description = "\n".join(lines) or "No entries yet."
    embed.set_footer(text="Earn 1 coin per minute of active chatting")
    await ctx.send(embed=embed)


@bot.command(name="pay")
async def cmd_pay(ctx, member: discord.Member = None, amount: int = 0):
    if not member:
        await ctx.reply("❌ Mention a user to pay.", mention_author=False); return
    if member.bot:
        await ctx.reply("❌ You can't pay bots.", mention_author=False); return
    if member == ctx.author:
        await ctx.reply("❌ You can't pay yourself.", mention_author=False); return
    if amount <= 0:
        await ctx.reply("❌ Amount must be positive.", mention_author=False); return

    async with _econ_locks[ctx.author.id]:
        sender = get_econ(ctx.author.id)
        if sender["coins"] < amount:
            await ctx.reply(f"❌ You only have **{sender['coins']:,} Ajax Coins**.", mention_author=False)
            return
        async with _econ_locks[member.id]:
            receiver = get_econ(member.id)
            sender["coins"]   -= amount
            receiver["coins"] += amount
            save_econ(ctx.author.id)
            save_econ(member.id)

    await ctx.reply(
        f"💸 {ctx.author.mention} sent **{amount:,} Ajax Coins** to {member.mention}!",
        mention_author=False
    )


@bot.command(name="daily")
async def cmd_daily(ctx):
    BASE_REWARD  = 50
    STREAK_BONUS = 10

    async with _econ_locks[ctx.author.id]:
        econ = get_econ(ctx.author.id)
        now  = datetime.now(timezone.utc)

        last_daily = econ.get("last_daily")
        streak     = econ.get("daily_streak", 0)

        if last_daily:
            last_dt = datetime.fromisoformat(last_daily)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            hours_since = (now - last_dt).total_seconds() / 3600
            if hours_since < 24:
                next_claim = last_dt + timedelta(hours=24)
                remaining  = next_claim - now
                h = int(remaining.total_seconds() // 3600)
                m = int((remaining.total_seconds() % 3600) // 60)
                embed = discord.Embed(
                    title="⏳ Daily Already Claimed",
                    description=f"Come back in **{h}h {m}m** for your next reward!",
                    color=discord.Color.red()
                )
                embed.set_footer(text=f"Current streak: {streak} day(s) 🔥")
                await ctx.reply(embed=embed, mention_author=False)
                return
            elif hours_since > 48:
                streak = 0

        streak              = min(streak + 1, 7)
        reward              = BASE_REWARD + (STREAK_BONUS * (streak - 1))
        econ["coins"]        += reward
        econ["total_earned"] += reward
        econ["last_daily"]    = now.isoformat()
        econ["daily_streak"]  = streak
        save_econ(ctx.author.id)

    streak_bar = "🔥" * streak + "⬜" * (7 - streak)
    embed = discord.Embed(title="🪙 Daily Reward Claimed!", color=0xF5C400)
    embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
    embed.add_field(name="Reward",      value=f"**+{reward:,} Ajax Coins**",      inline=True)
    embed.add_field(name="New Balance", value=f"**{econ['coins']:,} Ajax Coins**", inline=True)
    embed.add_field(
        name="Streak",
        value=f"{streak_bar}\n**Day {streak}** {'🔥 MAX!' if streak == 7 else ''}",
        inline=False
    )
    embed.set_footer(text="Come back tomorrow to keep your streak! Streak resets if you miss a day.")
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="give")
async def cmd_give(ctx, member: discord.Member = None, amount: int = 0):
    if ctx.author.id != OWNER_ID and get_trust(ctx.author.id) < 4:
        await ctx.reply("❌ You need trust level 4+ to give coins.", mention_author=False); return
    if not member:
        await ctx.reply("❌ Mention a user.", mention_author=False); return
    if amount <= 0:
        await ctx.reply("❌ Amount must be positive.", mention_author=False); return
    async with _econ_locks[member.id]:
        econ = get_econ(member.id)
        econ["coins"]        += amount
        econ["total_earned"] += amount
        save_econ(member.id)
    log_mod("give_coins", member.id, ctx.author.id, str(amount))
    await ctx.reply(f"✅ Gave **{amount:,} Ajax Coins** to {member.mention}.", mention_author=False)


@bot.command(name="take")
async def cmd_take(ctx, member: discord.Member = None, amount: int = 0):
    if ctx.author.id != OWNER_ID and get_trust(ctx.author.id) < 4:
        await ctx.reply("❌ You need trust level 4+ to take coins.", mention_author=False); return
    if not member:
        await ctx.reply("❌ Mention a user.", mention_author=False); return
    if amount <= 0:
        await ctx.reply("❌ Amount must be positive.", mention_author=False); return
    async with _econ_locks[member.id]:
        econ = get_econ(member.id)
        econ["coins"] = max(0, econ["coins"] - amount)
        save_econ(member.id)
    log_mod("take_coins", member.id, ctx.author.id, str(amount))
    await ctx.reply(f"✅ Removed **{amount:,} Ajax Coins** from {member.mention}.", mention_author=False)


@bot.command(name="coinreset")
async def cmd_coinreset(ctx, member: discord.Member = None):
    if ctx.author.id != OWNER_ID:
        return
    if not member:
        await ctx.reply("❌ Mention a user.", mention_author=False); return
    async with _econ_locks[member.id]:
        key = str(member.id)
        economy[key] = {
            "coins": 0, "total_earned": 0,
            "last_message_ts": None, "messages_counted": 0,
            "last_daily": None, "daily_streak": 0,
        }
        save_econ(member.id)
    await ctx.reply(f"✅ Reset **{member.display_name}**'s Ajax Coins.", mention_author=False)


@bot.command(name="backup")
async def cmd_backup(ctx):
    if ctx.author.id != OWNER_ID:
        return
    await ctx.reply("📦 Generating backup, check your DMs!", mention_author=False)
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
    embed = discord.Embed(
        title="📦 AJ's Assistant — Full Backup",
        description="Keep this file safe.",
        color=0xF5C400,
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Economy entries",      value=f"{len(economy):,} users", inline=True)
    embed.add_field(name="Coins in circulation", value=f"{total_coins:,} 🪙",      inline=True)
    embed.add_field(name="Registered users",     value=f"{len(registry):,}",       inline=True)
    embed.add_field(name="Mod log entries",      value=f"{len(mod_logs):,}",        inline=True)
    embed.add_field(name="Memory entries",       value=f"{len(memory):,}",          inline=True)
    embed.set_footer(text=f"File: {filename}")
    try:
        dm = await ctx.author.create_dm()
        await dm.send(embed=embed, file=file_obj)
    except discord.Forbidden:
        await ctx.reply("❌ Couldn't DM you — make sure your DMs are open.", mention_author=False)


@bot.command(name="ping")
async def cmd_ping(ctx):
    await ctx.reply(f"🏓 Pong! `{round(bot.latency * 1000)}ms`", mention_author=False)

# ─── CORE PROCESS (AI) ─────────────────────────────────────────────────────────

async def process(msg: discord.Message, is_dm: bool = False):
    author  = msg.author
    uid     = author.id
    trust   = get_trust(uid)
    content = msg.content.strip()

    if trust == 0:
        asyncio.ensure_future(bot_log(
            "🚫 Blocked User Tried to Message",
            f"**{author}** (`{uid}`) — trust 0 — sent: `{content[:200]}`",
            level="security"
        ))
        return

    # Rate limiting
    if uid != OWNER_ID:
        cooldown = 1.5 if trust >= 4 else 4.0
        now      = time.time()
        if now - rate_limits[uid] < cooldown:
            await msg.reply("⏱️ Slow down a bit!", mention_author=False)
            return
        rate_limits[uid] = now

    # Injection detection
    if is_suspicious(content):
        asyncio.ensure_future(bot_log(
            "⚠️ Suspicious / Injection Attempt",
            f"**{author}** (`{uid}`) | Trust: {trust}",
            fields=[
                ("Message", f"```{content[:500]}```", False),
                ("Channel", getattr(msg.channel, "mention", "DM"), True),
            ],
            level="security"
        ))

    if not is_dm:
        track_activity(uid, msg.channel.id)
    register_user(author)

    ctx_line = build_context(msg, trust)
    # Context goes into the system prompt, NOT the user message, so it can't be overridden
    system_with_ctx = f"{active_prompt()}\n\n{ctx_line}"
    user_payload    = content   # clean user message only

    hist_key = f"dm_{uid}" if is_dm else str(msg.channel.id)
    hist     = histories[hist_key]
    hist.append({"role": "user", "content": user_payload})
    if len(hist) > MAX_HIST:
        histories[hist_key] = hist[-MAX_HIST:]
    hist = histories[hist_key]

    async with msg.channel.typing():
        raw = await call_ai(hist, system=system_with_ctx)

    histories[hist_key].append({"role": "assistant", "content": raw})
    if len(histories[hist_key]) > MAX_HIST:
        histories[hist_key] = histories[hist_key][-MAX_HIST:]

    parsed = parse_ai_json(raw)
    if parsed:
        action = parsed.get("action", "chat")
        if action in MOD_ACTIONS and trust < 4:
            await msg.reply("❌ You don't have permission to do that (requires trust level 4+).", mention_author=False)
            asyncio.ensure_future(bot_log(
                "🔒 Unauthorized Mod Attempt",
                f"**{author}** (`{uid}`) tried `{action}` with trust **{trust}**",
                fields=[("Message", content[:300], False)],
                level="security"
            ))
            return
        reply = await execute_action(msg, parsed)
        if reply:
            await msg.reply(reply, mention_author=False)
    else:
        await msg.reply(raw[:1990], mention_author=False)

    if is_dm and uid != OWNER_ID:
        logs = dm_logs.setdefault(str(uid), [])
        logs.append({
            "ts":  datetime.now(timezone.utc).isoformat()[:19],
            "msg": content[:200],
            "rep": raw[:200],
        })
        dm_logs[str(uid)] = logs[-15:]
        await db_save_dm_logs()

# ─── EVENTS ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    await db_init()
    await db_load()
    log.info(f"✅ AJ's Assistant ready as {bot.user} (ID: {bot.user.id})")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="the server 👁️")
    )

    # Log startup — fires on every Railway restart too
    guilds = len(bot.guilds)
    total_members = sum(g.member_count for g in bot.guilds)
    asyncio.ensure_future(bot_log(
        "🟢 Bot Started / Restarted",
        f"**{bot.user}** is online",
        fields=[
            ("Guilds",         str(guilds),         True),
            ("Total Members",  str(total_members),  True),
            ("Economy Users",  str(len(economy)),   True),
            ("Registered",     str(len(registry)),  True),
            ("Mod Logs",       str(len(mod_logs)),  True),
            ("Groq Keys",      str(len(GROQ_KEYS)), True),
            ("DB Connected",   "✅ Yes" if _db else "❌ No", True),
            ("Log Channel",    "✅ Set" if BOT_LOG_CHANNEL_ID else "❌ Not set", True),
        ],
        level="startup"
    ))


@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot:
        return

    await bot.process_commands(msg)

    is_dm   = isinstance(msg.channel, discord.DMChannel)
    content = msg.content.strip()

    # ── Economy coin earning ────────────────────────────────────────────────────
    if not is_dm and not content.startswith(CMD_PREFIX) and len(content) >= 5:
        uid = msg.author.id
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
                if MSG_RARITY <= 1 or random.randint(1, MSG_RARITY) == 1:
                    econ["coins"]            += 1
                    econ["total_earned"]     += 1
                    econ["messages_counted"] += 1
                    econ["last_message_ts"]   = now.isoformat()
                    save_econ(uid)

    # ── Passive tracking ────────────────────────────────────────────────────────
    if not is_dm and bot.user not in (msg.mentions or []):
        track_activity(msg.author.id, msg.channel.id)
        register_user(msg.author)

    # ── AI response logic ───────────────────────────────────────────────────────
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
        asyncio.ensure_future(bot_log(
            "❌ Unhandled Error in on_message",
            f"`{type(e).__name__}: {e}`",
            fields=[
                ("User",    f"{msg.author} (`{msg.author.id}`)", True),
                ("Channel", getattr(msg.channel, "mention", "DM"), True),
                ("Message", content[:300], False),
            ],
            level="error"
        ))
        try:
            await msg.reply(f"❌ Error — {type(e).__name__}: {e}", mention_author=False)
        except Exception:
            pass


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MemberNotFound):
        await ctx.reply("❌ Couldn't find that member.", mention_author=False)
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(f"❌ Missing argument. Try `{CMD_PREFIX}help`.", mention_author=False)
    elif isinstance(error, commands.BadArgument):
        await ctx.reply("❌ Invalid argument (make sure amounts are numbers).", mention_author=False)
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        log.error(f"Command error: {error}")
        asyncio.ensure_future(bot_log(
            "⚠️ Command Error",
            f"`{type(error).__name__}: {error}`",
            fields=[("Command", ctx.message.content[:200], False)],
            level="warn"
        ))


@bot.event
async def on_member_join(member: discord.Member):
    asyncio.ensure_future(bot_log(
        "📥 Member Joined",
        f"**{member}** (`{member.id}`) joined **{member.guild.name}**",
        fields=[
            ("Account Created", member.created_at.strftime("%Y-%m-%d"), True),
            ("Member Count",    str(member.guild.member_count),          True),
        ],
        level="info"
    ))


@bot.event
async def on_member_remove(member: discord.Member):
    asyncio.ensure_future(bot_log(
        "📤 Member Left / Removed",
        f"**{member}** (`{member.id}`) left **{member.guild.name}**",
        fields=[("Member Count", str(member.guild.member_count), True)],
        level="info"
    ))


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    asyncio.ensure_future(bot_log(
        "🔨 Member Banned (Discord Audit)",
        f"**{user}** (`{user.id}`) was banned from **{guild.name}**",
        level="mod"
    ))


@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.User):
    asyncio.ensure_future(bot_log(
        "✅ Member Unbanned (Discord Audit)",
        f"**{user}** (`{user.id}`) was unbanned from **{guild.name}**",
        level="mod"
    ))

# ─── GRACEFUL SHUTDOWN ─────────────────────────────────────────────────────────

async def shutdown(signal_name: str = "SIGTERM"):
    asyncio.ensure_future(bot_log(
        "🔴 Bot Shutting Down",
        f"Received `{signal_name}` — saving data and disconnecting…",
        level="shutdown"
    ))
    # Give the log a moment to send
    await asyncio.sleep(1.5)
    await bot.close()

def _handle_signal(sig):
    name = signal.Signals(sig).name
    log.info(f"Received {name}, shutting down…")
    asyncio.ensure_future(shutdown(name))

# ─── RUN ───────────────────────────────────────────────────────────────────────

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
            loop.add_signal_handler(sig, _handle_signal, sig)
        except NotImplementedError:
            pass  # Windows doesn't support add_signal_handler

    try:
        loop.run_until_complete(bot.start(DISCORD_TOKEN))
    except (KeyboardInterrupt, SystemExit):
        loop.run_until_complete(shutdown("KeyboardInterrupt"))
    finally:
        loop.close()
