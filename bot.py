"""
bot.py — aj's crib
A single-file Discord bot (discord.py, prefix commands only, no slash commands).
Built on top of the existing MongoDB data layer (Database class, leveling math,
achievements, level-role logic) and wired up into an actual running bot with
real commands, background tasks, and an entrypoint.

ENV VARS REQUIRED:
    DISCORD_TOKEN   -> your bot token
    MONGO_URI       -> your MongoDB connection string
    PREFIX          -> optional, defaults to "."

Run with:  python bot.py
"""

import os
import io
import re
import json
import math
import time
import random
import asyncio
import logging
import platform
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord.ext import commands, tasks
from motor.motor_asyncio import AsyncIOMotorClient

# ═══════════════════════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("lxte")


# ═══════════════════════════════════════════════════════════════════════════════
#  CONSTANTS (levelling-related)
# ═══════════════════════════════════════════════════════════════════════════════

XP_COOLDOWN_SEC   = 30
VOICE_XP_INTERVAL = 60
VOICE_XP_PER_TICK = 5
STREAK_BONUS_XP   = 5
BOOST_XP_REWARD   = 200

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


# ═══════════════════════════════════════════════════════════════════════════════
#  LEVEL MATH
# ═══════════════════════════════════════════════════════════════════════════════

def calculate_level(total_xp: int) -> tuple[int, int, int]:
    """Returns (level, xp_into_level, xp_needed_for_next_level)."""
    if total_xp <= 0:
        return 0, 0, 50
    level        = int((-1 + math.sqrt(1 + 4 * total_xp / 25)) / 2)
    current_base = 25 * level * (level + 1)
    next_base    = 25 * (level + 1) * (level + 2)
    return level, total_xp - current_base, next_base - current_base


def xp_from_length(text: str, multiplier: float = 1.0) -> int:
    """Calculate XP to award based on message length."""
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


# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG CACHE  (stale-while-revalidate)
# ═══════════════════════════════════════════════════════════════════════════════

_config_cache: dict[int, tuple[dict, float]] = {}
CONFIG_CACHE_TTL   = 60.0   # fresh window
CONFIG_CACHE_STALE = 120.0  # serve stale while refreshing in background
_config_refresh_tasks: set[int] = set()


async def get_config(guild_id: int, db: "Database") -> dict:
    cached = _config_cache.get(guild_id)
    now    = time.monotonic()
    if cached:
        data, ts_cached = cached
        age = now - ts_cached
        if age < CONFIG_CACHE_TTL:
            return data
        if age < CONFIG_CACHE_STALE:
            if guild_id not in _config_refresh_tasks:
                _config_refresh_tasks.add(guild_id)
                async def _bg_refresh(gid: int):
                    try:
                        fresh = await db.get_config(gid)
                        _config_cache[gid] = (fresh, time.monotonic())
                    except Exception as exc:
                        logger.debug("Config background refresh failed for %d: %s", gid, exc)
                    finally:
                        _config_refresh_tasks.discard(gid)
                asyncio.create_task(_bg_refresh(guild_id))
            return data
    config = await db.get_config(guild_id)
    _config_cache[guild_id] = (config, now)
    return config


def invalidate_config(guild_id: int):
    _config_cache.pop(guild_id, None)


# ═══════════════════════════════════════════════════════════════════════════════
#  LOGGING SYSTEM — dedicated channels per log category
# ═══════════════════════════════════════════════════════════════════════════════
#
# Seven distinct log channels, each independently configurable via
# .quicksetup -> Logging Channels (or .config <key> <channel_id> directly):
#   log_transcripts_channel  — ticket transcripts on close
#   log_msg_channel          — message edits/deletes
#   automod_log_channel      — automod + hate-speech filter hits
#   modlog_channel           — warns/kicks/bans/etc (the case system)
#   log_server_channel       — channel/role create/delete, server changes
#   log_entryexit_channel    — member joins/leaves
#   log_bot_channel          — bot startup/shutdown status

LOG_CHANNEL_KEYS = {
    "transcripts": ("log_transcripts_channel", "Transcripts", "📑"),
    "msg":         ("log_msg_channel",         "Message Logs", "📝"),
    "automod":     ("automod_log_channel",     "Automod Logs", "🛡️"),
    "mod":         ("modlog_channel",          "Mod Logs", "📋"),
    "server":      ("log_server_channel",      "Server Logs", "🗂️"),
    "entryexit":   ("log_entryexit_channel",   "Entry/Exit Logs", "🚪"),
    "bot":         ("log_bot_channel",         "Bot Logs", "🤖"),
}


async def send_log(guild: discord.Guild, key: str, embed: Optional[discord.Embed] = None,
                    content: Optional[str] = None, file: Optional[discord.File] = None):
    """Sends to a configured log channel by short key (see LOG_CHANNEL_KEYS). No-ops
    silently if that log category hasn't been configured for this guild."""
    if not bot.db:
        return
    config_key = LOG_CHANNEL_KEYS[key][0]
    config = await get_config(guild.id, bot.db)
    chan_id = config.get(config_key)
    if not chan_id:
        return
    channel = guild.get_channel(chan_id)
    if not channel:
        return
    try:
        await channel.send(content=content, embed=embed, file=file)
    except discord.HTTPException as e:
        logger.warning("Failed to post to log channel '%s': %s", key, e)


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
            maxPoolSize=20,
            minPoolSize=2,
            retryWrites=True,
            retryReads=True,
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
        self.automod_strikes = db["automod_strikes"]
        self.severe_strikes  = db["severe_strikes"]

    @property
    def db(self):
        """Expose the lxte_assistant database directly."""
        return self._client["lxte_assistant"]

    async def _retry(self, coro_fn, *args, retries: int = 3, **kwargs):
        """Retry wrapper for transient MongoDB errors (AutoReconnect, NetworkTimeout, etc.)."""
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
                raise
        raise last_exc or Exception("DB retry exhausted")

    async def ping(self) -> bool:
        try: await self._client.admin.command("ping"); return True
        except Exception: return False

    async def ensure_indexes(self):
        try:
            await self.config.create_index("guild_id", unique=True, background=True)
            await self.levels.create_index([("user_id", 1), ("guild_id", 1)], unique=True, background=True)
            await self.levels.create_index([("guild_id", 1), ("total_xp", -1)], background=True)
            await self.invites.create_index([("guild_id", 1), ("invite_code", 1)], background=True)
            await self.role_menus.create_index([("guild_id", 1), ("menu_id", 1)], background=True)
            await self.tickets.create_index([("guild_id", 1), ("channel_id", 1)], background=True)
            await self.boosts.create_index([("guild_id", 1), ("user_id", 1)], unique=True, background=True)
            await self.analytics.create_index([("guild_id", 1), ("date", 1)], unique=True, background=True)
            await self.reaction_roles.create_index([("guild_id", 1), ("message_id", 1)], background=True)
            await self.giveaways.create_index([("guild_id", 1), ("message_id", 1)], background=True)
            await self.giveaways.create_index("ends_at", background=True)
            await self.msg_tracking.create_index([("guild_id", 1), ("user_id", 1)], unique=True, background=True)
            await self.msg_tracking.create_index([("guild_id", 1), ("total_messages", -1)], background=True)
            await self.warns.create_index([("guild_id", 1), ("user_id", 1)], background=True)
            await self.warns.create_index("created_at", background=True)
            await self.cases.create_index([("guild_id", 1), ("case_number", 1)], unique=True, background=True)
            await self.cases.create_index([("guild_id", 1), ("target_id", 1)], background=True)
            await self.tempmutes.create_index("unmute_at", background=True)
            await self.roblox_history.create_index("_id", background=True)
            await self.db["join_log"].create_index([("guild_id", 1), ("joined_at", -1)], background=True)
            await self.db["leave_log"].create_index([("guild_id", 1), ("left_at", -1)], background=True)
            await self.db["join_log"].create_index("user_id", background=True)
            await self.tempbans.create_index("unban_at", background=True)
            await self.tempbans.create_index([("guild_id", 1), ("user_id", 1)], background=True)
            await self.reports.create_index([("guild_id", 1), ("created_at", -1)], background=True)
            await self.ticket_ratings.create_index([("guild_id", 1), ("ticket_id", 1)], background=True)
            await self.automod_strikes.create_index([("guild_id", 1), ("user_id", 1)], unique=True, background=True)
            logger.info("Indexes ready")
        except Exception as exc:
            logger.error("Index error: %s", exc)

    async def close(self):
        self._client.close()

    # ── Config ────────────────────────────────────────────────────────────────

    async def get_config(self, gid: int) -> dict:
        return await self.config.find_one({"guild_id": gid}) or {}

    async def update_config(self, gid: int, key: str, value):
        await self.config.update_one(
            {"guild_id": gid},
            {"$set": {key: value, "updated_at": datetime.now(timezone.utc)},
             "$setOnInsert": {"guild_id": gid}},
            upsert=True,
        )
        invalidate_config(gid)

    # ── Levels / XP ───────────────────────────────────────────────────────────

    async def get_level_data(self, uid: int, gid: int) -> dict:
        return await self.levels.find_one({"user_id": uid, "guild_id": gid}) or {}

    async def add_xp(self, uid: int, gid: int, xp: int) -> dict:
        """
        Award XP to a user. Handles streak logic and returns a result dict:
          {total_xp, level, messages, xp_in, xp_need, leveled, old_level, streak, streak_bonus}
        """
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
                if lmd.tzinfo is None:
                    lmd = lmd.replace(tzinfo=timezone.utc)
                diff = (now.date() - lmd.date()).days
                if diff == 1:
                    streak += 1; sb = True
                elif diff > 1:
                    streak = 1
                else:
                    streak = doc.get("streak", 1)
            else:
                streak = 1
        else:
            total_xp = xp; messages = 1; old_level = 0; streak = 1; sb = False

        if sb:
            total_xp += STREAK_BONUS_XP
        new_level, xp_in, xp_need = calculate_level(total_xp)
        await self.levels.update_one(
            {"user_id": uid, "guild_id": gid},
            {"$set": {
                "total_xp": total_xp, "level": new_level, "messages": messages,
                "last_xp_time": now, "last_message_date": now, "streak": streak,
            }},
            upsert=True,
        )
        return {
            "total_xp": total_xp, "level": new_level, "messages": messages,
            "xp_in": xp_in, "xp_need": xp_need, "leveled": new_level > old_level,
            "old_level": old_level, "streak": streak, "streak_bonus": sb,
        }

    async def reset_xp(self, uid: int, gid: int):
        await self.levels.update_one(
            {"user_id": uid, "guild_id": gid},
            {"$set": {"total_xp": 0, "level": 0, "messages": 0, "streak": 0}},
            upsert=True,
        )

    async def get_leaderboard(self, gid: int, limit: int = 10) -> list[dict]:
        return await self.levels.find(
            {"guild_id": gid}, sort=[("total_xp", -1)], limit=limit
        ).to_list(length=limit)

    async def get_all_level_data(self, gid: int) -> list[dict]:
        return await self.levels.find({"guild_id": gid}).to_list(length=None)

    async def award_badge(self, uid: int, gid: int, badge_id: str) -> bool:
        doc    = await self.levels.find_one({"user_id": uid, "guild_id": gid})
        badges = doc.get("badges", []) if doc else []
        if badge_id in badges:
            return False
        badges.append(badge_id)
        await self.levels.update_one(
            {"user_id": uid, "guild_id": gid},
            {"$set": {"badges": badges}},
            upsert=True,
        )
        return True

    # ── Message Tracking ──────────────────────────────────────────────────────

    async def track_message(self, uid: int, gid: int, channel_id: int):
        """Increment total message count and per-channel count for a user."""
        chan_key = f"channels.{channel_id}"
        now = datetime.now(timezone.utc)
        await self.msg_tracking.update_one(
            {"guild_id": gid, "user_id": uid},
            {
                "$inc": {"total_messages": 1, chan_key: 1},
                "$set": {"last_message": now},
                "$min": {"first_message": now},
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

    # ── Invites ───────────────────────────────────────────────────────────────

    async def save_invite(self, gid: int, code: str, inviter_id: int, uses: int):
        await self.invites.update_one(
            {"guild_id": gid, "invite_code": code},
            {"$set": {"inviter_id": inviter_id, "uses": uses}},
            upsert=True,
        )

    async def get_invite(self, gid: int, code: str) -> dict:
        return await self.invites.find_one({"guild_id": gid, "invite_code": code}) or {}

    async def increment_invite_count(self, gid: int, inviter_id: int, fake: bool = False):
        """Increment invite count. fake=True for accounts <7 days old."""
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
        await self.invites.update_one(
            {"guild_id": gid, "inviter_id": inviter_id, "invite_code": "__total__"},
            {"$inc": {"bonus": amount, "total_invites": amount}},
            upsert=True,
        )

    async def reset_invites(self, gid: int, inviter_id: int):
        await self.invites.update_one(
            {"guild_id": gid, "inviter_id": inviter_id, "invite_code": "__total__"},
            {"$set": {"total_invites": 0, "regular": 0, "fake": 0, "left": 0, "bonus": 0}},
            upsert=True,
        )

    async def reset_all_invites(self, gid: int, exclude_user_id: int = None):
        query = {"guild_id": gid, "invite_code": "__total__"}
        if exclude_user_id:
            query["inviter_id"] = {"$ne": exclude_user_id}
        result = await self.invites.delete_many(query)
        return result.deleted_count

    async def get_invite_count(self, gid: int, inviter_id: int) -> int:
        doc = await self.invites.find_one(
            {"guild_id": gid, "inviter_id": inviter_id, "invite_code": "__total__"}
        )
        return doc.get("total_invites", 0) if doc else 0

    async def get_invite_leaderboard(self, gid: int, limit: int = 10) -> list[dict]:
        return await self.invites.find(
            {"guild_id": gid, "invite_code": "__total__"},
            sort=[("total_invites", -1)],
            limit=limit,
        ).to_list(length=limit)

    # ── Role Menus ────────────────────────────────────────────────────────────

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
            {"$set": {
                "user_id": uid, "ticket_id": ticket_id,
                "opened_at": datetime.now(timezone.utc), "closed": False,
            }},
            upsert=True,
        )

    async def get_ticket(self, channel_id: int) -> dict:
        return await self.tickets.find_one({"channel_id": channel_id}) or {}

    async def close_ticket(self, channel_id: int):
        await self.tickets.update_one(
            {"channel_id": channel_id},
            {"$set": {"closed": True, "closed_at": datetime.now(timezone.utc)}},
        )

    async def count_open_tickets(self, gid: int, uid: int) -> int:
        return await self.tickets.count_documents({"guild_id": gid, "user_id": uid, "closed": False})

    async def rate_ticket(self, gid: int, ticket_id: int, rater_id: int, closer_id: int, stars: int):
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
            {"$group": {"_id": None, "avg": {"$avg": "$stars"}, "count": {"$sum": 1}}},
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

    # ── Boosts ────────────────────────────────────────────────────────────────

    async def record_boost(self, gid: int, uid: int) -> int:
        r = await self.boosts.find_one_and_update(
            {"guild_id": gid, "user_id": uid},
            {"$inc": {"boost_count": 1}, "$setOnInsert": {"first_boost": datetime.now(timezone.utc)}},
            upsert=True, return_document=True,
        )
        return (r or {}).get("boost_count", 1)

    async def get_boost_leaderboard(self, gid: int, limit: int = 10) -> list[dict]:
        return await self.boosts.find(
            {"guild_id": gid}, sort=[("boost_count", -1)], limit=limit
        ).to_list(length=limit)

    # ── Warn System ───────────────────────────────────────────────────────────

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

    async def get_all_warns(self, gid: int) -> list[dict]:
        return await self.warns.find({"guild_id": gid}).to_list(length=None)

    # ── Case System ───────────────────────────────────────────────────────────

    async def add_case(self, gid: int, action: str, mod_id: int, target_id: int,
                       reason: str, extra: dict = None) -> int:
        last = await self.cases.find_one({"guild_id": gid}, sort=[("case_number", -1)])
        num  = (last.get("case_number", 0) + 1) if last else 1
        doc  = {
            "guild_id": gid, "case_number": num, "action": action,
            "mod_id": mod_id, "target_id": target_id, "reason": reason,
            "created_at": datetime.now(timezone.utc),
        }
        if extra:
            doc.update(extra)
        await self.cases.insert_one(doc)
        return num

    async def get_case(self, gid: int, num: int) -> dict:
        return await self.cases.find_one({"guild_id": gid, "case_number": num}) or {}

    async def get_user_cases(self, gid: int, uid: int, limit: int = 20) -> list[dict]:
        return await self.cases.find(
            {"guild_id": gid, "target_id": uid},
            sort=[("created_at", -1)],
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
        return await self.tempmutes.find(
            {"active": True, "unmute_at": {"$lte": now}}
        ).to_list(length=100)

    # ── Temp Bans ─────────────────────────────────────────────────────────────

    async def add_tempban(self, gid: int, uid: int, mod_id: int, reason: str, unban_at: datetime):
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

    # ── Analytics ─────────────────────────────────────────────────────────────

    async def record_member_count(self, gid: int, count: int):
        today = datetime.now(timezone.utc).date().isoformat()
        await self.analytics.update_one(
            {"guild_id": gid, "date": today},
            {"$set": {"member_count": count, "date": today, "guild_id": gid}},
            upsert=True,
        )

    async def get_member_count_history(self, gid: int, days: int = 30) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        return await self.analytics.find(
            {"guild_id": gid, "date": {"$gte": cutoff}},
            sort=[("date", 1)],
        ).to_list(length=days)

    async def save_snapshot(self, snapshot: dict):
        db = self._client["lxte_assistant"]
        await db["snapshots"].insert_one(snapshot)

    # ── Reaction Roles ────────────────────────────────────────────────────────

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
        if not doc or doc.get("ended"):
            return False
        if user_id in doc.get("entrants", []):
            return False
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

    # ── Reports ───────────────────────────────────────────────────────────────

    async def add_report(self, gid: int, reporter_id: int, target_id: int, reason: str) -> str:
        now  = datetime.now(timezone.utc)
        last = await self.reports.find_one({"guild_id": gid}, sort=[("report_number", -1)])
        num  = (last.get("report_number", 0) if last else 0) + 1
        await self.reports.insert_one({
            "guild_id": gid, "report_number": num,
            "reporter_id": reporter_id, "target_id": target_id,
            "reason": reason, "created_at": now, "actioned": False,
        })
        return str(num)

    # ── Roblox Version History ────────────────────────────────────────────────

    async def get_roblox_history(self) -> list[str]:
        doc = await self.roblox_history.find_one({"_id": "global"})
        return doc.get("hashes", []) if doc else []

    async def push_roblox_hash(self, new_hash: str):
        await self.roblox_history.update_one(
            {"_id": "global"},
            {"$push": {"hashes": {"$each": [new_hash], "$slice": -50}}},
            upsert=True,
        )

    # ── Automod Strikes ───────────────────────────────────────────────────────
    # Strikes decay automatically — only strikes from the last 24h count toward
    # escalation, so a user who behaves doesn't stay punished forever.

    async def add_strike(self, gid: int, uid: int) -> int:
        """Record a new automod violation and return the active (un-decayed) strike count."""
        now    = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        doc    = await self.automod_strikes.find_one({"guild_id": gid, "user_id": uid})
        active = [t for t in (doc.get("strikes", []) if doc else []) if t > cutoff]
        active.append(now)
        await self.automod_strikes.update_one(
            {"guild_id": gid, "user_id": uid},
            {"$set": {"strikes": active}},
            upsert=True,
        )
        return len(active)

    async def get_active_strikes(self, gid: int, uid: int) -> int:
        now    = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        doc    = await self.automod_strikes.find_one({"guild_id": gid, "user_id": uid})
        if not doc:
            return 0
        return len([t for t in doc.get("strikes", []) if t > cutoff])

    async def clear_strikes(self, gid: int, uid: int):
        await self.automod_strikes.delete_one({"guild_id": gid, "user_id": uid})

    # ── Severe (hate-speech) Strikes ──────────────────────────────────────────
    # Tracked separately from regular automod strikes with a much longer decay
    # window (30 days, not 24h) since this category is zero-tolerance.

    async def add_severe_strike(self, gid: int, uid: int) -> int:
        now    = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=30)
        doc    = await self.severe_strikes.find_one({"guild_id": gid, "user_id": uid})
        active = [t for t in (doc.get("strikes", []) if doc else []) if t > cutoff]
        active.append(now)
        await self.severe_strikes.update_one(
            {"guild_id": gid, "user_id": uid},
            {"$set": {"strikes": active}},
            upsert=True,
        )
        return len(active)

    async def get_severe_strikes(self, gid: int, uid: int) -> int:
        now    = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=30)
        doc    = await self.severe_strikes.find_one({"guild_id": gid, "user_id": uid})
        if not doc:
            return 0
        return len([t for t in doc.get("strikes", []) if t > cutoff])



# ═══════════════════════════════════════════════════════════════════════════════
#  ACHIEVEMENT CHECKER
# ═══════════════════════════════════════════════════════════════════════════════

async def check_achievements(member, db: Database, data: dict) -> list[dict]:
    """
    Check all achievement conditions for a member and award any newly unlocked badges.
    Returns a list of newly awarded achievement dicts.
    """
    newly  = []
    level  = data.get("level", 0)
    msgs   = data.get("messages", 0)
    streak = data.get("streak", 0)
    badges = data.get("badges", [])
    checks = [
        ("first_message",   msgs >= 1),
        ("level_5",         level >= 5),
        ("level_10",        level >= 10),
        ("level_25",        level >= 25),
        ("level_50",        level >= 50),
        ("messages_100",    msgs >= 100),
        ("messages_1000",   msgs >= 1000),
        ("streak_7",        streak >= 7),
        ("streak_30",       streak >= 30),
        ("booster",         bool(getattr(member, "premium_since", None))),
    ]
    for bid, cond in checks:
        if cond and bid not in badges:
            if await db.award_badge(member.id, member.guild.id, bid):
                a = next((x for x in ACHIEVEMENTS if x["id"] == bid), None)
                if a:
                    newly.append(a)
    return newly


# ═══════════════════════════════════════════════════════════════════════════════
#  LEVEL ROLE APPLIER
# ═══════════════════════════════════════════════════════════════════════════════

async def apply_level_roles(member, db: Database, new_level: int) -> Optional[str]:
    """
    Give all roles the member has earned up to new_level.
    Returns the name of the role earned at exactly new_level (or the highest held role).
    """
    config      = await get_config(member.guild.id, db)
    config_rows = config.get("level_roles", [])

    if config_rows:
        ladder = [(entry["level"], entry["role_id"], True) for entry in config_rows]
    else:
        ladder = [(req, name, False) for req, name in LEVEL_ROLES]

    def _resolve_role(guild, ref, is_id):
        if is_id:
            return guild.get_role(ref)
        return next((r for r in guild.roles if r.name.lower() == str(ref).strip().lower()), None)

    exact_reward = None
    for req, ref, is_id in sorted(ladder, key=lambda x: x[0]):
        if new_level >= req:
            role = _resolve_role(member.guild, ref, is_id)
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
                role = _resolve_role(member.guild, ref, is_id)
                if role and role in member.roles:
                    exact_reward = role.name
                    break

    return exact_reward


# ═══════════════════════════════════════════════════════════════════════════════
#  AUTOMOD — smart spam / invite / mention / zalgo detection
# ═══════════════════════════════════════════════════════════════════════════════
#
# Design goals:
#   - Never punish fast typers. Plain message-rate limits are the classic way
#     automods nuke legitimate chatty users, so flood detection requires a HIGH
#     burst rate (6+ messages in 4s) and weighs repeated/duplicate content far
#     more heavily than distinct fast messages.
#   - Action is delete-only on the first hit. Repeat offenders accumulate
#     strikes that decay after 24h, and only strikes escalate to a timeout.
#     There are no kicks or bans from automod — ever.

AUTOMOD_DEFAULTS = {
    "antispam":    True,
    "antiinvite":  True,
    "mentionspam": True,
    "zalgo":       True,
}

INVITE_RE = re.compile(
    r"(?:discord\.gg|discord(?:app)?\.com/invite)/[a-zA-Z0-9-]+", re.IGNORECASE
)
ZALGO_RE = re.compile(r"[\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff]")

ZALGO_THRESHOLD     = 8     # combining marks in one message before it's "zalgo"
MENTION_THRESHOLD   = 5     # unique user + role mentions before it's "mention spam"
FLOOD_WINDOW_SEC    = 4.0   # burst window
FLOOD_MAX_MESSAGES  = 6     # messages allowed in that window (generous — real typers won't hit this)
DUPLICATE_WINDOW_SEC = 12.0 # window for repeated/near-identical content
DUPLICATE_MAX_COUNT  = 3    # identical messages allowed in that window

_msg_history: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=8))


def _normalize(content: str) -> str:
    return re.sub(r"\s+", " ", content.strip().lower())


def _is_flooding(key: tuple[int, int], content: str) -> bool:
    """Smart spam check: weighs duplicate content much more heavily than raw speed,
    so a fast typer sending varied messages is never flagged."""
    now  = time.monotonic()
    norm = _normalize(content)
    hist = _msg_history[key]
    hist.append((now, norm))

    if norm:
        dup_count = sum(1 for t, c in hist if now - t <= DUPLICATE_WINDOW_SEC and c == norm)
        if dup_count >= DUPLICATE_MAX_COUNT:
            return True

    flood_count = sum(1 for t, _ in hist if now - t <= FLOOD_WINDOW_SEC)
    return flood_count >= FLOOD_MAX_MESSAGES


def _has_invite(content: str) -> bool:
    return bool(INVITE_RE.search(content))


def _is_mention_spam(message: discord.Message) -> bool:
    total = len(message.raw_mentions) + len(message.raw_role_mentions)
    return total >= MENTION_THRESHOLD


def _is_zalgo(content: str) -> bool:
    return len(ZALGO_RE.findall(content)) >= ZALGO_THRESHOLD


async def run_automod(message: discord.Message, config: dict) -> Optional[str]:
    """Returns a human-readable violation reason, or None if the message is clean."""
    content = message.content or ""

    if config.get("automod_antiinvite", True) and _has_invite(content):
        return "posting an unauthorized invite link"

    if config.get("automod_mentionspam", True) and _is_mention_spam(message):
        return "mass-mention spam"

    if config.get("automod_zalgo", True) and _is_zalgo(content):
        return "zalgo/unicode spam"

    if config.get("automod_antispam", True):
        key = (message.guild.id, message.author.id)
        if _is_flooding(key, content):
            return "message flooding/spam"

    return None


def _escalation_for_strikes(strikes: int) -> Optional[int]:
    """Returns a timeout duration in seconds for the given strike count, capped at
    1 hour, or None if this strike count shouldn't trigger a mute yet. Never bans
    or kicks — automod is mute-only, by design."""
    if strikes >= 8:
        return 3600
    if strikes >= 5:
        return 900
    if strikes >= 3:
        return 300
    return None


def _format_duration(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


async def handle_automod_violation(message: discord.Message, config: dict, reason: str):
    """Deletes the offending message, records a strike, and escalates to a mute
    (never a kick/ban) if the user's active strike count crosses a threshold."""
    try:
        await message.delete()
    except discord.HTTPException:
        pass

    strikes = await bot.db.add_strike(message.guild.id, message.author.id)
    mute_seconds = _escalation_for_strikes(strikes)

    log_line = (
        f"🛡️ Deleted a message from {message.author.mention} in {message.channel.mention} "
        f"— **{reason}**. (active strikes: {strikes})"
    )

    if mute_seconds:
        try:
            await message.author.timeout(
                timedelta(seconds=mute_seconds),
                reason=f"Automod escalation: {reason} (strike {strikes})",
            )
            log_line += f" → 🔇 muted for **{_format_duration(mute_seconds)}**."
        except discord.Forbidden:
            log_line += " (⚠️ couldn't mute — missing permissions)"

    log_chan_id = config.get("automod_log_channel")
    if log_chan_id:
        chan = message.guild.get_channel(log_chan_id)
        if chan:
            try:
                await chan.send(log_line)
            except discord.HTTPException:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
#  AUTOMOD — hate-speech / slur filter (zero tolerance, separate from anti-spam)
# ═══════════════════════════════════════════════════════════════════════════════
#
# This is intentionally separate from the spam/invite/zalgo automod above:
#   - It is NEVER skipped for "first offense" leniency — every hit is deleted
#     and punished immediately.
#   - It targets slurs only (racial/homophobic/ableist slurs), never ordinary
#     profanity — normal swearing is left alone on purpose.
#   - Strikes here decay over 30 days (not 24h) and escalate hard: 1st hit is
#     a long timeout, repeat hits within the window lead straight to a ban.
#   - Evasion via leetspeak, spacing, or repeated letters is normalized away
#     before matching, so "n1gg3r", "n i g g e r", and "niggggger" all match.

HATE_SPEECH_TERMS = [
    "nigger", "nigga",
    "faggot", "fag",
    "retard", "retarded",
    "tranny",
    "chink",
    "spic",
    "kike",
    "wetback",
    "beaner",
]

_LEET_MAP = str.maketrans({
    "4": "a", "@": "a",
    "3": "e",
    "1": "i", "!": "i",
    "0": "o",
    "$": "s", "5": "s",
    "7": "t",
})


def _deleet(text: str) -> str:
    """Normalizes common evasion tricks: leetspeak substitutions, separators
    (spaces/punctuation/underscores between letters), and stretched-out
    repeated letters — so filter evasion doesn't work."""
    text = text.lower().translate(_LEET_MAP)
    text = re.sub(r"[^a-z]+", "", text)          # strip spaces/punctuation entirely
    text = re.sub(r"(.)\1+", r"\1", text)         # collapse repeated letters
    return text


def _contains_hate_speech(content: str) -> Optional[str]:
    """Returns the matched term (for logging) if the message contains a slur, else None."""
    if not content:
        return None
    normalized = _deleet(content)
    for term in HATE_SPEECH_TERMS:
        if _deleet(term) in normalized:
            return term
    return None


def _severe_escalation(strikes: int) -> Optional[str]:
    """Returns 'timeout' or 'ban' depending on severe-strike count. Zero tolerance —
    unlike regular automod, even strike #1 always results in real punishment."""
    if strikes >= 2:
        return "ban"
    return "timeout"


async def handle_hate_speech_violation(message: discord.Message, config: dict, matched_term: str):
    """Deletes the message and immediately escalates — this filter never goes
    easy on a first offense."""
    try:
        await message.delete()
    except discord.HTTPException:
        pass

    strikes = await bot.db.add_severe_strike(message.guild.id, message.author.id)
    action = _severe_escalation(strikes)

    if action == "ban":
        try:
            await message.author.ban(reason=f"Hate-speech filter: repeat offense (strike {strikes})")
            action_text = "🔨 **banned** (repeat hate-speech offense)"
            await bot.db.add_case(message.guild.id, "ban", bot.user.id, message.author.id,
                                   "Automatic ban — repeat hate-speech filter violation")
        except discord.Forbidden:
            action_text = "⚠️ tried to ban but missing permissions"
    else:
        try:
            await message.author.timeout(timedelta(hours=24), reason=f"Hate-speech filter (strike {strikes})")
            action_text = "🔇 timed out for **24h**"
        except discord.Forbidden:
            action_text = "⚠️ tried to timeout but missing permissions"

    embed = discord.Embed(
        title="🚫 Hate Speech Filter Triggered",
        color=discord.Color.dark_red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="User", value=f"{message.author.mention}\n`{message.author.id}`", inline=True)
    embed.add_field(name="Channel", value=message.channel.mention, inline=True)
    embed.add_field(name="Severe Strikes", value=str(strikes), inline=True)
    embed.add_field(name="Action", value=action_text, inline=False)
    embed.add_field(name="Matched Content", value=f"```{message.content[:900]}```", inline=False)
    embed.set_thumbnail(url=message.author.display_avatar.url)

    log_chan_id = config.get("automod_log_channel")
    if log_chan_id:
        chan = message.guild.get_channel(log_chan_id)
        if chan:
            try:
                await chan.send(embed=embed)
            except discord.HTTPException:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
#  BOT SETUP — "aj's crib"
# ═══════════════════════════════════════════════════════════════════════════════

PREFIX = os.getenv("PREFIX", ".")


# ── Stats channel formatting ─────────────────────────────────────────────────
MEMBERS_CHANNEL_FORMAT = "︰🌺・Members: {count}"
BOOSTS_CHANNEL_FORMAT  = "︰🌺・Boosts: {count}"
STATS_UPDATE_COOLDOWN  = 600  # Discord only allows ~2 channel renames per 10 min

intents = discord.Intents.default()
intents.message_content = True
intents.members         = True
intents.guilds          = True
intents.voice_states    = True


class AjsCrib(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned_or(PREFIX),
            intents=intents,
            help_command=None,
            case_insensitive=True,
        )
        self.db: Optional[Database] = None
        self._xp_cooldowns: dict[tuple[int, int], float] = {}
        self._stats_last_update: dict[int, float] = {}  # channel_id -> monotonic time
        self.invite_cache: dict[int, dict[str, int]] = {}  # guild_id -> {code: uses}

    async def setup_hook(self):
        mongo_uri = os.getenv("MONGO_URI")
        if not mongo_uri:
            logger.error("MONGO_URI is not set — the bot cannot start without a database.")
            raise SystemExit(1)
        self.db = Database(mongo_uri)
        ok = await self.db.ping()
        if not ok:
            logger.error("Could not reach MongoDB with the given MONGO_URI.")
            raise SystemExit(1)
        await self.db.ensure_indexes()
        logger.info("Connected to MongoDB.")

        # persistent ticket button views (survive restarts)
        self.add_view(TicketPanelView())
        self.add_view(TicketCloseView())

        # background loops
        self.tempmute_loop.start()
        self.tempban_loop.start()
        self.giveaway_loop.start()
        self.stats_loop.start()

    async def close(self):
        if self.db:
            await self.db.close()
        await super().close()

    # ── background tasks ─────────────────────────────────────────────────────

    @tasks.loop(seconds=30)
    async def tempmute_loop(self):
        try:
            due = await self.db.get_due_tempmutes()
            for doc in due:
                guild = self.get_guild(doc["guild_id"])
                if not guild:
                    continue
                member = guild.get_member(doc["user_id"])
                await self.db.remove_tempmute(doc["guild_id"], doc["user_id"])
                if member:
                    try:
                        await member.timeout(None, reason="Tempmute expired")
                    except Exception as e:
                        logger.warning("Failed to lift tempmute for %s: %s", doc["user_id"], e)
        except Exception as e:
            logger.error("tempmute_loop error: %s", e)

    @tasks.loop(seconds=30)
    async def tempban_loop(self):
        try:
            due = await self.db.get_due_tempbans()
            for doc in due:
                guild = self.get_guild(doc["guild_id"])
                if not guild:
                    continue
                try:
                    await guild.unban(discord.Object(id=doc["user_id"]), reason="Tempban expired")
                except discord.NotFound:
                    pass
                except Exception as e:
                    logger.warning("Failed to lift tempban for %s: %s", doc["user_id"], e)
                await self.db.remove_tempban(doc["guild_id"], doc["user_id"])
        except Exception as e:
            logger.error("tempban_loop error: %s", e)

    @tasks.loop(seconds=20)
    async def giveaway_loop(self):
        try:
            due = await self.db.get_due_giveaways()
            for doc in due:
                await self.db.end_giveaway(doc["message_id"])
                channel = self.get_channel(doc["channel_id"])
                if not channel:
                    continue
                entrants = doc.get("entrants", [])
                winners_n = doc.get("winners", 1)
                winners = random.sample(entrants, k=min(winners_n, len(entrants))) if entrants else []
                if winners:
                    mention_str = ", ".join(f"<@{w}>" for w in winners)
                    await channel.send(f"🎉 Congratulations {mention_str}! You won **{doc['prize']}**!")
                else:
                    await channel.send(f"No valid entrants for **{doc['prize']}** — giveaway cancelled.")
        except Exception as e:
            logger.error("giveaway_loop error: %s", e)

    @tasks.loop(minutes=10)
    async def stats_loop(self):
        """Periodic safety-net refresh of all configured stats channels."""
        for guild in self.guilds:
            try:
                await self.refresh_stats_channels(guild, force=True)
            except Exception as e:
                logger.error("stats_loop error for guild %s: %s", guild.id, e)

    async def refresh_stats_channels(self, guild: discord.Guild, force: bool = False):
        """Update the members/boosts stats channels for a guild, respecting Discord's rename rate limit."""
        if not self.db:
            return
        config = await get_config(guild.id, self.db)
        now = time.monotonic()

        members_chan_id = config.get("members_stats_channel")
        if members_chan_id:
            channel = guild.get_channel(members_chan_id)
            if channel:
                last = self._stats_last_update.get(channel.id, 0)
                if force or now - last >= STATS_UPDATE_COOLDOWN:
                    new_name = MEMBERS_CHANNEL_FORMAT.format(count=guild.member_count)
                    if channel.name != new_name:
                        try:
                            await channel.edit(name=new_name)
                            self._stats_last_update[channel.id] = now
                        except discord.HTTPException as e:
                            logger.warning("Failed to update members stats channel: %s", e)

        boosts_chan_id = config.get("boosts_stats_channel")
        if boosts_chan_id:
            channel = guild.get_channel(boosts_chan_id)
            if channel:
                last = self._stats_last_update.get(channel.id, 0)
                if force or now - last >= STATS_UPDATE_COOLDOWN:
                    new_name = BOOSTS_CHANNEL_FORMAT.format(count=guild.premium_subscription_count)
                    if channel.name != new_name:
                        try:
                            await channel.edit(name=new_name)
                            self._stats_last_update[channel.id] = now
                        except discord.HTTPException as e:
                            logger.warning("Failed to update boosts stats channel: %s", e)


bot = AjsCrib()


# ═══════════════════════════════════════════════════════════════════════════════
#  TICKETS — persistent button views
# ═══════════════════════════════════════════════════════════════════════════════

TICKET_OPEN_CUSTOM_ID  = "ajscrib:open_ticket"
TICKET_CLOSE_CUSTOM_ID = "ajscrib:close_ticket"


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", emoji="🔒", style=discord.ButtonStyle.red, custom_id=TICKET_CLOSE_CUSTOM_ID)
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        await bot.db.close_ticket(channel.id)
        await interaction.response.send_message("🔒 Closing this ticket in 5 seconds...")
        await asyncio.sleep(5)
        try:
            await channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException:
            pass


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Ticket", emoji="🎫", style=discord.ButtonStyle.green, custom_id=TICKET_OPEN_CUSTOM_ID)
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        config = await get_config(guild.id, bot.db)

        existing = await bot.db.count_open_tickets(guild.id, interaction.user.id)
        if existing > 0:
            await interaction.response.send_message("⚠️ You already have an open ticket.", ephemeral=True)
            return

        category_id = config.get("ticket_category_id")
        category = guild.get_channel(category_id) if category_id else None
        support_role_id = config.get("ticket_support_role_id")
        support_role = guild.get_role(support_role_id) if support_role_id else None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        channel_name = f"ticket-{interaction.user.name}".lower()[:90]
        channel = await guild.create_text_channel(
            channel_name,
            category=category,
            overwrites=overwrites,
            reason=f"Ticket opened by {interaction.user}",
        )
        ticket_id = int(time.time())
        await bot.db.save_ticket(guild.id, channel.id, interaction.user.id, ticket_id)

        embed = discord.Embed(
            title="🎫 Support Ticket",
            description=f"{interaction.user.mention} thanks for reaching out — support will be with you shortly.",
            color=discord.Color.blurple(),
        )
        await channel.send(
            content=support_role.mention if support_role else None,
            embed=embed,
            view=TicketCloseView(),
        )
        await interaction.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  INVITE TRACKING HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

async def _refresh_invite_cache(guild: discord.Guild):
    try:
        invites = await guild.invites()
        bot.invite_cache[guild.id] = {inv.code: inv.uses for inv in invites}
    except discord.Forbidden:
        bot.invite_cache[guild.id] = {}


async def _detect_used_invite(guild: discord.Guild) -> Optional[discord.Invite]:
    """Compare current invites against the cache to figure out which invite was just used."""
    try:
        current = await guild.invites()
    except discord.Forbidden:
        return None
    old_map = bot.invite_cache.get(guild.id, {})
    used = None
    for inv in current:
        if inv.uses > old_map.get(inv.code, 0):
            used = inv
            break
    bot.invite_cache[guild.id] = {inv.code: inv.uses for inv in current}
    return used


# ═══════════════════════════════════════════════════════════════════════════════
#  SETUP WIZARD HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
#  INTERACTIVE SETUP — dropdown-driven configuration (no typing required)
# ═══════════════════════════════════════════════════════════════════════════════

def _admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("⛔ Admins only.", ephemeral=True)
            return False
        return True
    return predicate


class CloseButtonView(discord.ui.View):
    """A simple 'done' button to dismiss a setup panel."""
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="Done", emoji="✅", style=discord.ButtonStyle.secondary)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="✅ Saved.", embed=None, view=None)


class ChannelPickSelect(discord.ui.ChannelSelect):
    """Generic single-channel picker that writes straight to guild config."""
    def __init__(self, config_key: str, label: str, channel_types=None):
        super().__init__(
            placeholder=f"Select a channel for {label}...",
            channel_types=channel_types or [discord.ChannelType.text],
            min_values=1, max_values=1,
        )
        self.config_key = config_key
        self.label_text = label

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        await bot.db.update_config(interaction.guild.id, self.config_key, channel.id)
        await interaction.response.edit_message(
            content=f"✅ **{self.label_text}** set to <#{channel.id}>.", view=CloseButtonView(),
        )


class ChannelPickView(discord.ui.View):
    def __init__(self, config_key: str, label: str, channel_types=None):
        super().__init__(timeout=180)
        self.add_item(ChannelPickSelect(config_key, label, channel_types))


class RolePickSelect(discord.ui.RoleSelect):
    """Generic role picker (single or multi) that writes straight to guild config."""
    def __init__(self, config_key: str, label: str, max_values: int = 1):
        super().__init__(
            placeholder=f"Select role(s) for {label}...",
            min_values=1, max_values=max_values,
        )
        self.config_key = config_key
        self.label_text = label

    async def callback(self, interaction: discord.Interaction):
        roles = self.values
        if self.max_values == 1:
            await bot.db.update_config(interaction.guild.id, self.config_key, roles[0].id)
            msg = f"✅ **{self.label_text}** set to {roles[0].mention}."
        else:
            await bot.db.update_config(interaction.guild.id, self.config_key, [r.id for r in roles])
            msg = f"✅ **{self.label_text}** set to: " + ", ".join(r.mention for r in roles)
        await interaction.response.edit_message(content=msg, view=CloseButtonView())


class RolePickView(discord.ui.View):
    def __init__(self, config_key: str, label: str, max_values: int = 1):
        super().__init__(timeout=180)
        self.add_item(RolePickSelect(config_key, label, max_values))


# ── Level Roles configuration ────────────────────────────────────────────────

LEVEL_PRESETS = [1, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 60, 70, 80, 90, 100]


async def _build_ladder_text(guild: discord.Guild) -> str:
    config = await get_config(guild.id, bot.db)
    rows = config.get("level_roles", [])
    if not rows:
        return "*No custom level roles configured yet — the built-in default ladder is being used.*"
    lines = []
    for entry in sorted(rows, key=lambda r: r["level"]):
        role = guild.get_role(entry["role_id"])
        lines.append(f"**Level {entry['level']}** → {role.mention if role else '*(deleted role)*'}")
    return "\n".join(lines)


class CustomLevelModal(discord.ui.Modal, title="Custom Level"):
    level_input = discord.ui.TextInput(label="Level number", placeholder="e.g. 42", max_length=5)

    def __init__(self, parent_view: "LevelRolesView"):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            n = int(self.level_input.value.strip())
            if n <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("⚠️ That's not a valid level number.", ephemeral=True)
            return
        self.parent_view.selected_level = n
        await interaction.response.send_message(
            f"Level set to **{n}**. Now pick the role(s) to award using the dropdown below.",
            ephemeral=True,
        )


class LevelSelect(discord.ui.Select):
    def __init__(self, parent_view: "LevelRolesView"):
        options = [discord.SelectOption(label=f"Level {n}", value=str(n)) for n in LEVEL_PRESETS]
        options.append(discord.SelectOption(label="Custom level…", value="custom", emoji="✏️"))
        super().__init__(placeholder="1️⃣ Pick a level...", options=options)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "custom":
            await interaction.response.send_modal(CustomLevelModal(self.parent_view))
            return
        self.parent_view.selected_level = int(self.values[0])
        await interaction.response.send_message(
            f"Level set to **{self.values[0]}**. Now pick role(s) to award using the second dropdown.",
            ephemeral=True,
        )


class LevelRoleSelect(discord.ui.RoleSelect):
    def __init__(self, parent_view: "LevelRolesView"):
        super().__init__(
            placeholder="2️⃣ Pick role(s) to award (multi-select)...",
            min_values=1, max_values=10,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        if self.parent_view.selected_level is None:
            await interaction.response.send_message("⚠️ Pick a level first (first dropdown).", ephemeral=True)
            return
        level = self.parent_view.selected_level
        config = await get_config(interaction.guild.id, bot.db)
        rows = [r for r in config.get("level_roles", []) if r["level"] != level]
        for role in self.values:
            rows.append({"level": level, "role_id": role.id})
        await bot.db.update_config(interaction.guild.id, "level_roles", rows)
        mentions = ", ".join(r.mention for r in self.values)
        ladder = await _build_ladder_text(interaction.guild)
        embed = discord.Embed(
            title="⭐ Level Roles", color=discord.Color.gold(),
            description=f"✅ Level **{level}** now awards: {mentions}\n\n**Current ladder:**\n{ladder}",
        )
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class RemoveLevelSelect(discord.ui.Select):
    def __init__(self, rows: list):
        options = [
            discord.SelectOption(label=f"Level {r['level']}", value=str(r["level"]))
            for r in sorted(rows, key=lambda r: r["level"])
        ][:25]
        super().__init__(placeholder="🗑️ Remove a configured level...", options=options)

    async def callback(self, interaction: discord.Interaction):
        level = int(self.values[0])
        config = await get_config(interaction.guild.id, bot.db)
        rows = [r for r in config.get("level_roles", []) if r["level"] != level]
        await bot.db.update_config(interaction.guild.id, "level_roles", rows)
        ladder = await _build_ladder_text(interaction.guild)
        embed = discord.Embed(
            title="⭐ Level Roles", color=discord.Color.gold(),
            description=f"🗑️ Removed level **{level}**.\n\n**Current ladder:**\n{ladder}",
        )
        await interaction.response.edit_message(embed=embed, view=LevelRolesView())


class LevelRolesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.selected_level: Optional[int] = None
        self.add_item(LevelSelect(self))
        self.add_item(LevelRoleSelect(self))

    @discord.ui.button(label="Manage / Remove a Level", emoji="🗑️", style=discord.ButtonStyle.secondary, row=2)
    async def manage(self, interaction: discord.Interaction, button: discord.ui.Button):
        config = await get_config(interaction.guild.id, bot.db)
        rows = config.get("level_roles", [])
        if not rows:
            await interaction.response.send_message("No custom level roles configured yet.", ephemeral=True)
            return
        view = discord.ui.View(timeout=180)
        view.add_item(RemoveLevelSelect(rows))
        await interaction.response.send_message("Pick a level to remove:", view=view, ephemeral=True)

    @discord.ui.button(label="Done", emoji="✅", style=discord.ButtonStyle.success, row=2)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(view=None)


# ── Automod configuration ────────────────────────────────────────────────────

AUTOMOD_FEATURES = [
    ("antispam",    "Anti-Spam / Flood",        "🌊"),
    ("antiinvite",  "Anti-Invite Links",        "🔗"),
    ("mentionspam", "Mass Mention Spam",        "📣"),
    ("zalgo",       "Zalgo / Unicode Spam",     "👹"),
]


def _automod_status_embed(config: dict) -> discord.Embed:
    lines = []
    for key, label, emoji in AUTOMOD_FEATURES:
        on = config.get(f"automod_{key}", True)
        lines.append(f"{emoji} **{label}** — {'🟢 ON' if on else '🔴 OFF'}")
    log_id = config.get("automod_log_channel")
    lines.append(f"\n📋 Log channel: {f'<#{log_id}>' if log_id else '*not set*'}")
    lines.append(
        "\n**Escalation (mutes only, never kicks/bans):**\n"
        "Strike 1-2 → delete only · Strike 3-4 → 5m mute · Strike 5-7 → 15m mute · Strike 8+ → 1h mute\n"
        "*Strikes decay automatically after 24h of good behavior.*"
    )
    return discord.Embed(title="🛡️ Automod Settings", color=discord.Color.red(), description="\n".join(lines))


class AutomodToggleSelect(discord.ui.Select):
    def __init__(self, config: dict):
        options = [
            discord.SelectOption(label=label, value=key, emoji=emoji, default=config.get(f"automod_{key}", True))
            for key, label, emoji in AUTOMOD_FEATURES
        ]
        super().__init__(
            placeholder="Toggle filters — selected = ON",
            min_values=0, max_values=len(options), options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        chosen = set(self.values)
        for key, _, _ in AUTOMOD_FEATURES:
            await bot.db.update_config(interaction.guild.id, f"automod_{key}", key in chosen)
        config = await get_config(interaction.guild.id, bot.db)
        await interaction.response.edit_message(embed=_automod_status_embed(config), view=self.view)


class AutomodLogSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="📋 Pick a log channel for automod actions...",
            channel_types=[discord.ChannelType.text], min_values=1, max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        await bot.db.update_config(interaction.guild.id, "automod_log_channel", self.values[0].id)
        config = await get_config(interaction.guild.id, bot.db)
        await interaction.response.edit_message(embed=_automod_status_embed(config), view=self.view)


class AutomodView(discord.ui.View):
    def __init__(self, config: dict):
        super().__init__(timeout=300)
        self.add_item(AutomodToggleSelect(config))
        self.add_item(AutomodLogSelect())

    @discord.ui.button(label="Done", emoji="✅", style=discord.ButtonStyle.success, row=2)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(view=None)


# ── Tickets configuration ────────────────────────────────────────────────────

class TicketsSetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(ChannelPickSelectInline("ticket_category_id", "Ticket Category", [discord.ChannelType.category]))
        self.add_item(RolePickSelectInline("ticket_support_role_id", "Ticket Support Role"))

    @discord.ui.button(label="Done", emoji="✅", style=discord.ButtonStyle.success, row=2)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(view=None)


class ChannelPickSelectInline(discord.ui.ChannelSelect):
    """Like ChannelPickSelect, but stays attached to a shared multi-field view instead of closing it."""
    def __init__(self, config_key: str, label: str, channel_types=None):
        super().__init__(placeholder=f"Select {label}...", channel_types=channel_types, min_values=1, max_values=1)
        self.config_key = config_key
        self.label_text = label

    async def callback(self, interaction: discord.Interaction):
        await bot.db.update_config(interaction.guild.id, self.config_key, self.values[0].id)
        await interaction.response.send_message(f"✅ **{self.label_text}** set to <#{self.values[0].id}>.", ephemeral=True)


class RolePickSelectInline(discord.ui.RoleSelect):
    def __init__(self, config_key: str, label: str):
        super().__init__(placeholder=f"Select {label}...", min_values=1, max_values=1)
        self.config_key = config_key
        self.label_text = label

    async def callback(self, interaction: discord.Interaction):
        await bot.db.update_config(interaction.guild.id, self.config_key, self.values[0].id)
        await interaction.response.send_message(f"✅ **{self.label_text}** set to {self.values[0].mention}.", ephemeral=True)


# ── Main setup hub ────────────────────────────────────────────────────────────

class SetupHubSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Level Roles", value="levelroles", emoji="⭐",
                                  description="Pick roles awarded at each level (multi-select)"),
            discord.SelectOption(label="Level-Up Announcements", value="levelup", emoji="🎉",
                                  description="Channel where level-up messages are posted"),
            discord.SelectOption(label="Automod", value="automod", emoji="🛡️",
                                  description="Toggle spam/invite/mention/zalgo filters"),
            discord.SelectOption(label="Tickets", value="tickets", emoji="🎫",
                                  description="Ticket category & support role"),
            discord.SelectOption(label="Mod Log", value="modlog", emoji="📋",
                                  description="Channel for warns/kicks/bans"),
            discord.SelectOption(label="Re-sync Stats Channels", value="stats", emoji="📊",
                                  description="Refresh the Members/Boosts trackers now"),
        ]
        super().__init__(placeholder="⚙️ Choose something to configure...", options=options)

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        if value == "levelroles":
            ladder = await _build_ladder_text(interaction.guild)
            embed = discord.Embed(title="⭐ Level Roles", color=discord.Color.gold(),
                                   description=f"**Current ladder:**\n{ladder}")
            await interaction.response.send_message(embed=embed, view=LevelRolesView(), ephemeral=True)
        elif value == "levelup":
            await interaction.response.send_message(
                "Pick the channel for level-up announcements:",
                view=ChannelPickView("levelup_channel", "Level-Up Announcements"), ephemeral=True,
            )
        elif value == "automod":
            config = await get_config(interaction.guild.id, bot.db)
            await interaction.response.send_message(embed=_automod_status_embed(config), view=AutomodView(config), ephemeral=True)
        elif value == "tickets":
            await interaction.response.send_message(
                "Set up your ticket category and support role:", view=TicketsSetupView(), ephemeral=True,
            )
        elif value == "modlog":
            await interaction.response.send_message(
                "Pick the mod-log channel (warns/kicks/bans):",
                view=ChannelPickView("modlog_channel", "Mod Log"), ephemeral=True,
            )
        elif value == "stats":
            await interaction.response.defer(ephemeral=True, thinking=True)
            await bot.refresh_stats_channels(interaction.guild, force=True)
            await interaction.followup.send("✅ Stats channels re-synced.", ephemeral=True)


class SetupHubView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(SetupHubSelect())


# ═══════════════════════════════════════════════════════════════════════════════
#  EVENTS
# ═══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="aj's crib")
    )
    for guild in bot.guilds:
        await _refresh_invite_cache(guild)
        embed = discord.Embed(
            title="🟢 Bot Online",
            description=f"**aj's crib** connected as `{bot.user}`.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        await send_log(guild, "bot", embed=embed)
    logger.info("aj's crib is online as %s (id: %s) — prefix '%s'", bot.user, bot.user.id, PREFIX)


@bot.event
async def on_guild_join(guild: discord.Guild):
    await _refresh_invite_cache(guild)


@bot.event
async def on_invite_create(invite: discord.Invite):
    bot.invite_cache.setdefault(invite.guild.id, {})[invite.code] = invite.uses


@bot.event
async def on_invite_delete(invite: discord.Invite):
    bot.invite_cache.get(invite.guild.id, {}).pop(invite.code, None)


# ── Message logs (edits/deletes) ─────────────────────────────────────────────

@bot.event
async def on_message_delete(message: discord.Message):
    if not message.guild or message.author.bot or not (message.content or message.attachments):
        return
    embed = discord.Embed(
        title="🗑️ Message Deleted",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Author", value=f"{message.author.mention}\n`{message.author.id}`", inline=True)
    embed.add_field(name="Channel", value=message.channel.mention, inline=True)
    if message.content:
        embed.add_field(name="Content", value=message.content[:1000], inline=False)
    if message.attachments:
        embed.add_field(name="Attachments", value="\n".join(a.url for a in message.attachments)[:1000], inline=False)
    embed.set_thumbnail(url=message.author.display_avatar.url)
    await send_log(message.guild, "msg", embed=embed)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if not before.guild or before.author.bot or before.content == after.content:
        return
    embed = discord.Embed(
        title="✏️ Message Edited",
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Author", value=f"{before.author.mention}\n`{before.author.id}`", inline=True)
    embed.add_field(name="Channel", value=before.channel.mention, inline=True)
    embed.add_field(name="Before", value=(before.content or "*empty*")[:512], inline=False)
    embed.add_field(name="After", value=(after.content or "*empty*")[:512], inline=False)
    embed.set_thumbnail(url=before.author.display_avatar.url)
    embed.add_field(name="Jump", value=f"[Go to message]({after.jump_url})", inline=False)
    await send_log(before.guild, "msg", embed=embed)


# ── Server logs (channel / role changes) ─────────────────────────────────────

@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel):
    embed = discord.Embed(title="➕ Channel Created", description=f"{channel.mention} (`{channel.name}`)",
                           color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    await send_log(channel.guild, "server", embed=embed)


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    embed = discord.Embed(title="➖ Channel Deleted", description=f"`#{channel.name}`",
                           color=discord.Color.red(), timestamp=datetime.now(timezone.utc))
    await send_log(channel.guild, "server", embed=embed)


@bot.event
async def on_guild_role_create(role: discord.Role):
    embed = discord.Embed(title="➕ Role Created", description=f"{role.mention} (`{role.name}`)",
                           color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    await send_log(role.guild, "server", embed=embed)


@bot.event
async def on_guild_role_delete(role: discord.Role):
    embed = discord.Embed(title="➖ Role Deleted", description=f"`{role.name}`",
                           color=discord.Color.red(), timestamp=datetime.now(timezone.utc))
    await send_log(role.guild, "server", embed=embed)


@bot.event
async def on_guild_update(before: discord.Guild, after: discord.Guild):
    changes = []
    if before.name != after.name:
        changes.append(f"**Name:** {before.name} → {after.name}")
    if before.icon != after.icon:
        changes.append("**Icon** changed")
    if not changes:
        return
    embed = discord.Embed(title="🗂️ Server Updated", description="\n".join(changes),
                           color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
    await send_log(after, "server", embed=embed)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    config = await get_config(message.guild.id, bot.db)

    # ── Hate-speech / slur filter (zero tolerance — applies to EVERYONE, no
    #    staff exemption, unlike the spam/invite/zalgo automod below) ───────────
    matched_term = _contains_hate_speech(message.content or "")
    if matched_term:
        await handle_hate_speech_violation(message, config, matched_term)
        return

    # ── Automod (skip for admins/mods so staff never get caught) ───────────────
    perms = message.author.guild_permissions
    if not (perms.administrator or perms.manage_messages):
        reason = await run_automod(message, config)
        if reason:
            await handle_automod_violation(message, config, reason)
            return  # deleted message — don't award XP or process it as a command

    key = (message.guild.id, message.author.id)
    now = time.monotonic()
    last = bot._xp_cooldowns.get(key, 0)
    if now - last >= XP_COOLDOWN_SEC:
        bot._xp_cooldowns[key] = now
        xp = xp_from_length(message.content)
        result = await bot.db.add_xp(message.author.id, message.guild.id, xp)
        await bot.db.track_message(message.author.id, message.guild.id, message.channel.id)

        if result["leveled"]:
            reward = await apply_level_roles(message.author, bot.db, result["level"])
            text = f"🎉 {message.author.mention} leveled up to **level {result['level']}**!"
            if reward:
                text += f" Earned role **{reward}**."
            target_channel = message.channel
            levelup_channel_id = config.get("levelup_channel")
            if levelup_channel_id:
                chan = message.guild.get_channel(levelup_channel_id)
                if chan:
                    target_channel = chan
            try:
                await target_channel.send(text)
            except discord.HTTPException:
                pass

        newly = await check_achievements(message.author, bot.db, result)
        for a in newly:
            try:
                await message.channel.send(
                    f"{a['emoji']} {message.author.mention} unlocked achievement **{a['name']}** — {a['desc']}"
                )
            except discord.HTTPException:
                pass

    await bot.process_commands(message)


@bot.event
async def on_member_join(member: discord.Member):
    try:
        await bot.refresh_stats_channels(member.guild)
    except Exception as e:
        logger.error("Stats refresh on join failed: %s", e)

    embed = discord.Embed(
        title="📥 Member Joined",
        description=f"{member.mention} (`{member}`)",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Account Created", value=f"<t:{int(member.created_at.timestamp())}:R>", inline=True)
    embed.add_field(name="Member Count", value=str(member.guild.member_count), inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    await send_log(member.guild, "entryexit", embed=embed)


@bot.event
async def on_member_remove(member: discord.Member):
    try:
        await bot.refresh_stats_channels(member.guild)
    except Exception as e:
        logger.error("Stats refresh on remove failed: %s", e)

    roles = [r.mention for r in reversed(member.roles) if r.name != "@everyone"]
    embed = discord.Embed(
        title="📤 Member Left",
        description=f"{member.mention} (`{member}`)",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    if member.joined_at:
        embed.add_field(name="Joined", value=f"<t:{int(member.joined_at.timestamp())}:R>", inline=True)
    embed.add_field(name="Member Count", value=str(member.guild.member_count), inline=True)
    if roles:
        embed.add_field(name="Roles", value=" ".join(roles)[:1000], inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
    await send_log(member.guild, "entryexit", embed=embed)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if before.premium_since is None and after.premium_since is not None:
        count = await bot.db.record_boost(after.guild.id, after.id)
        result = await bot.db.add_xp(after.id, after.guild.id, BOOST_XP_REWARD)
        logger.info("%s boosted %s (boost #%d), awarded %d XP", after, after.guild, count, BOOST_XP_REWARD)
        try:
            await bot.refresh_stats_channels(after.guild)
        except Exception as e:
            logger.error("Stats refresh on boost failed: %s", e)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠️ Missing argument: `{error.param.name}`. Check `{PREFIX}help`.")
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("⛔ You don't have permission to do that.")
        return
    if isinstance(error, commands.BotMissingPermissions):
        await ctx.send("⛔ I'm missing the permissions needed to do that.")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send(f"⚠️ Bad argument: {error}")
        return
    logger.exception("Unhandled command error in %s: %s", ctx.command, error)
    await ctx.send("❌ Something went wrong running that command.")


# ═══════════════════════════════════════════════════════════════════════════════
#  COMMANDS — general / utility
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="ping")
async def ping_cmd(ctx: commands.Context):
    await ctx.send(f"🏓 Pong! `{round(bot.latency * 1000)}ms`")


@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    embed = discord.Embed(
        title="aj's crib — commands",
        color=discord.Color.blurple(),
        description=f"Prefix: `{PREFIX}` • Strict-mode moderation with hierarchy checks, DMs, and case logging.",
    )
    embed.add_field(
        name="Leveling",
        value=f"`{PREFIX}rank [@user]` `{PREFIX}leaderboard`",
        inline=False,
    )
    embed.add_field(
        name="Moderation — Members",
        value=(
            f"`{PREFIX}warn @user reason` `{PREFIX}warnings @user` `{PREFIX}clearwarns @user`\n"
            f"`{PREFIX}kick @user reason` `{PREFIX}ban @user reason` `{PREFIX}unban <id> reason`\n"
            f"`{PREFIX}softban @user reason` `{PREFIX}massban <id> <id>... reason`\n"
            f"`{PREFIX}tempmute @user 10m reason` / `{PREFIX}unmute @user`\n"
            f"`{PREFIX}tempban @user 1d reason`\n"
            f"`{PREFIX}case <number>` `{PREFIX}cases @user`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Moderation — Channels",
        value=(
            f"`{PREFIX}purge <amount> [@user]` `{PREFIX}lockdown [#channel]` `{PREFIX}unlock [#channel]`\n"
            f"`{PREFIX}slowmode <seconds> [#channel]` `{PREFIX}nuke [#channel]`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Setup",
        value=f"`{PREFIX}quicksetup` — one-command setup for live Members/Boosts tracker channels",
        inline=False,
    )
    embed.add_field(
        name="Utility",
        value=f"`{PREFIX}ping` `{PREFIX}config` `{PREFIX}serverinfo` `{PREFIX}userinfo [@user]` `{PREFIX}backup`",
        inline=False,
    )
    await ctx.send(embed=embed)


# ═══════════════════════════════════════════════════════════════════════════════
#  COMMANDS — leveling
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="rank", aliases=["level", "lvl"])
async def rank_cmd(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    data = await bot.db.get_level_data(member.id, ctx.guild.id)
    if not data:
        await ctx.send(f"{member.mention} hasn't earned any XP yet.")
        return
    level, xp_in, xp_need = calculate_level(data.get("total_xp", 0))
    bar = progress_bar(xp_in, xp_need)
    embed = discord.Embed(title=f"{member.display_name}'s Rank", color=discord.Color.gold())
    embed.add_field(name="Level", value=str(level), inline=True)
    embed.add_field(name="Messages", value=str(data.get("messages", 0)), inline=True)
    embed.add_field(name="Streak", value=f"{data.get('streak', 0)} days", inline=True)
    embed.add_field(name="Progress", value=f"`{bar}` {xp_in}/{xp_need} XP", inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name="leaderboard", aliases=["lb", "top"])
async def leaderboard_cmd(ctx: commands.Context):
    rows = await bot.db.get_leaderboard(ctx.guild.id, limit=10)
    if not rows:
        await ctx.send("No leveling data yet for this server.")
        return
    lines = []
    for i, row in enumerate(rows, start=1):
        member = ctx.guild.get_member(row["user_id"])
        name = member.display_name if member else f"User {row['user_id']}"
        level = calculate_level(row.get("total_xp", 0))[0]
        lines.append(f"**#{i}** {name} — Level {level} ({row.get('total_xp', 0)} XP)")
    embed = discord.Embed(title="🏆 Leaderboard", description="\n".join(lines), color=discord.Color.gold())
    await ctx.send(embed=embed)


# ═══════════════════════════════════════════════════════════════════════════════
#  COMMANDS — moderation / case system
# ═══════════════════════════════════════════════════════════════════════════════
#
#  Strict-mode moderation: every action below enforces role hierarchy, blocks
#  self/bot/owner targeting, DMs the target a clean embed, logs a numbered
#  case, and — if a #modlog_channel is configured (via .quicksetup hub) —
#  posts a rich audit embed there. This is the single source of truth for
#  "what happened to who, and why" across the whole server.

MOD_ACTION_STYLE = {
    "warn":     ("⚠️", discord.Color.yellow()),
    "kick":     ("👋", discord.Color.orange()),
    "ban":      ("🔨", discord.Color.red()),
    "unban":    ("🔓", discord.Color.green()),
    "softban":  ("🧹", discord.Color.dark_orange()),
    "massban":  ("🔨", discord.Color.dark_red()),
    "tempmute": ("🔇", discord.Color.orange()),
    "unmute":   ("🔊", discord.Color.green()),
    "tempban":  ("⛔", discord.Color.dark_red()),
    "lockdown": ("🔒", discord.Color.dark_grey()),
    "unlock":   ("🔓", discord.Color.green()),
    "purge":    ("🧽", discord.Color.blurple()),
    "nuke":     ("💣", discord.Color.dark_red()),
    "slowmode": ("🐌", discord.Color.blurple()),
}


def _hierarchy_error(ctx: commands.Context, member: discord.Member) -> Optional[str]:
    """Returns a human-readable error string if `member` is not a valid mod target, else None."""
    if member.id == ctx.author.id:
        return "You can't moderate yourself."
    if member.id == ctx.guild.owner_id:
        return "You can't moderate the server owner."
    if member.id == bot.user.id:
        return "Nice try. I'm not moderating myself."
    if ctx.guild.owner_id == ctx.author.id:
        return None
    if member.top_role >= ctx.author.top_role:
        return f"You can't act on {member.mention} — their highest role is equal to or above yours."
    if member.top_role >= ctx.guild.me.top_role:
        return f"I can't act on {member.mention} — their highest role is equal to or above mine."
    return None


async def post_modlog(guild: discord.Guild, embed: discord.Embed):
    """Posts a case embed to the configured #modlog channel, if one is set."""
    config = await bot.db.get_config(guild.id)
    chan_id = config.get("modlog_channel")
    if not chan_id:
        return
    channel = guild.get_channel(chan_id)
    if channel:
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            logger.warning("Failed to post to modlog: %s", e)


def _case_embed(action: str, case_num: int, target, moderator: discord.abc.User,
                 reason: str, extra: Optional[dict] = None) -> discord.Embed:
    emoji, color = MOD_ACTION_STYLE.get(action, ("📋", discord.Color.greyple()))
    embed = discord.Embed(
        title=f"{emoji} {action.replace('_', ' ').title()} — Case #{case_num}",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Target", value=f"{target.mention}\n`{target.id}`", inline=True)
    embed.add_field(name="Moderator", value=f"{moderator.mention}\n`{moderator.id}`", inline=True)
    if extra:
        for name, value in extra.items():
            embed.add_field(name=name, value=value, inline=True)
    embed.add_field(name="Reason", value=reason or "No reason provided", inline=False)
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.set_footer(text="aj's crib — moderation log")
    return embed


async def _dm_safely(member: discord.abc.User, embed: discord.Embed) -> bool:
    try:
        await member.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


def _action_embed(emoji: str, title: str, description: str, color: discord.Color) -> discord.Embed:
    return discord.Embed(title=f"{emoji} {title}", description=description, color=color)


@bot.command(name="warn")
@commands.has_permissions(moderate_members=True)
async def warn_cmd(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    err = _hierarchy_error(ctx, member)
    if err:
        await ctx.send(f"⛔ {err}")
        return
    count = await bot.db.add_warn(ctx.guild.id, member.id, ctx.author.id, reason)
    case_num = await bot.db.add_case(ctx.guild.id, "warn", ctx.author.id, member.id, reason)

    dm_embed = discord.Embed(
        title=f"⚠️ You were warned in {ctx.guild.name}",
        description=f"**Reason:** {reason}\n**Total warnings:** {count}",
        color=discord.Color.yellow(),
    )
    dmed = await _dm_safely(member, dm_embed)

    embed = _case_embed("warn", case_num, member, ctx.author, reason, extra={"Warning Count": str(count)})
    await ctx.send(embed=embed)
    if not dmed:
        await ctx.send("ℹ️ Couldn't DM the member — they may have DMs disabled.", delete_after=8)
    await post_modlog(ctx.guild, embed)


@bot.command(name="warnings")
@commands.has_permissions(moderate_members=True)
async def warnings_cmd(ctx: commands.Context, member: discord.Member):
    warns = await bot.db.get_warns(ctx.guild.id, member.id)
    if not warns:
        await ctx.send(f"✅ {member.mention} has no warnings.")
        return
    lines = [f"`{w['created_at']:%Y-%m-%d}` — {w['reason']} (by <@{w['mod_id']}>)" for w in warns]
    embed = discord.Embed(
        title=f"⚠️ Warnings for {member.display_name}",
        description="\n".join(lines),
        color=discord.Color.yellow(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"{len(warns)} total warning(s)")
    await ctx.send(embed=embed)


@bot.command(name="clearwarns")
@commands.has_permissions(moderate_members=True)
async def clearwarns_cmd(ctx: commands.Context, member: discord.Member):
    n = await bot.db.clear_warns(ctx.guild.id, member.id)
    embed = _action_embed("🧹", "Warnings Cleared", f"Cleared **{n}** warning(s) for {member.mention}.", discord.Color.green())
    await ctx.send(embed=embed)
    await post_modlog(ctx.guild, embed)


@bot.command(name="case")
@commands.has_permissions(moderate_members=True)
async def case_cmd(ctx: commands.Context, number: int):
    case = await bot.db.get_case(ctx.guild.id, number)
    if not case:
        await ctx.send(f"❌ No case #{number} found.")
        return
    emoji, color = MOD_ACTION_STYLE.get(case["action"], ("📋", discord.Color.greyple()))
    embed = discord.Embed(title=f"{emoji} Case #{number}", color=color, timestamp=case.get("created_at"))
    embed.add_field(name="Action", value=case["action"].title())
    embed.add_field(name="Target", value=f"<@{case['target_id']}>")
    embed.add_field(name="Moderator", value=f"<@{case['mod_id']}>")
    embed.add_field(name="Reason", value=case["reason"], inline=False)
    await ctx.send(embed=embed)


@bot.command(name="cases")
@commands.has_permissions(moderate_members=True)
async def cases_cmd(ctx: commands.Context, member: discord.Member):
    cases = await bot.db.get_user_cases(ctx.guild.id, member.id)
    if not cases:
        await ctx.send(f"✅ {member.mention} has no cases.")
        return
    lines = [f"`#{c['case_number']}` **{c['action'].title()}** — {c['reason']}" for c in cases]
    embed = discord.Embed(
        title=f"📁 Case History — {member.display_name}",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"{len(cases)} case(s) on record")
    await ctx.send(embed=embed)


@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
async def kick_cmd(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    err = _hierarchy_error(ctx, member)
    if err:
        await ctx.send(f"⛔ {err}")
        return
    dm_embed = discord.Embed(title=f"👋 You were kicked from {ctx.guild.name}", description=f"**Reason:** {reason}", color=discord.Color.orange())
    dmed = await _dm_safely(member, dm_embed)
    await member.kick(reason=f"{reason} (by {ctx.author})")
    case_num = await bot.db.add_case(ctx.guild.id, "kick", ctx.author.id, member.id, reason)
    embed = _case_embed("kick", case_num, member, ctx.author, reason)
    await ctx.send(embed=embed)
    if not dmed:
        await ctx.send("ℹ️ Couldn't DM the member before kicking.", delete_after=8)
    await post_modlog(ctx.guild, embed)


@bot.command(name="ban")
@commands.has_permissions(ban_members=True)
async def ban_cmd(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    err = _hierarchy_error(ctx, member)
    if err:
        await ctx.send(f"⛔ {err}")
        return
    dm_embed = discord.Embed(title=f"🔨 You were banned from {ctx.guild.name}", description=f"**Reason:** {reason}", color=discord.Color.red())
    dmed = await _dm_safely(member, dm_embed)
    await member.ban(reason=f"{reason} (by {ctx.author})")
    case_num = await bot.db.add_case(ctx.guild.id, "ban", ctx.author.id, member.id, reason)
    embed = _case_embed("ban", case_num, member, ctx.author, reason)
    await ctx.send(embed=embed)
    if not dmed:
        await ctx.send("ℹ️ Couldn't DM the member before banning.", delete_after=8)
    await post_modlog(ctx.guild, embed)


@bot.command(name="unban")
@commands.has_permissions(ban_members=True)
async def unban_cmd(ctx: commands.Context, user_id: int, *, reason: str = "No reason provided"):
    try:
        user = await bot.fetch_user(user_id)
        await ctx.guild.unban(user, reason=f"{reason} (by {ctx.author})")
    except discord.NotFound:
        await ctx.send("❌ That user isn't banned (or doesn't exist).")
        return
    await bot.db.remove_tempban(ctx.guild.id, user_id)
    case_num = await bot.db.add_case(ctx.guild.id, "unban", ctx.author.id, user_id, reason)
    embed = _case_embed("unban", case_num, user, ctx.author, reason)
    await ctx.send(embed=embed)
    await post_modlog(ctx.guild, embed)


@bot.command(name="softban")
@commands.has_permissions(ban_members=True, manage_messages=True)
async def softban_cmd(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    """Ban + immediately unban, wiping recent messages — a 'kick with cleanup'."""
    err = _hierarchy_error(ctx, member)
    if err:
        await ctx.send(f"⛔ {err}")
        return
    dm_embed = discord.Embed(title=f"🧹 You were softbanned from {ctx.guild.name}", description=f"**Reason:** {reason}", color=discord.Color.dark_orange())
    dmed = await _dm_safely(member, dm_embed)
    await member.ban(reason=f"Softban: {reason} (by {ctx.author})")
    await ctx.guild.unban(member, reason="Softban cleanup")
    case_num = await bot.db.add_case(ctx.guild.id, "softban", ctx.author.id, member.id, reason)
    embed = _case_embed("softban", case_num, member, ctx.author, reason)
    await ctx.send(embed=embed)
    if not dmed:
        await ctx.send("ℹ️ Couldn't DM the member before softbanning.", delete_after=8)
    await post_modlog(ctx.guild, embed)


@bot.command(name="massban")
@commands.has_permissions(ban_members=True, administrator=True)
async def massban_cmd(ctx: commands.Context, user_ids: commands.Greedy[int], *, reason: str = "No reason provided"):
    """Ban a list of raw user IDs in one go. Usage: .massban 123 456 789 spam raid"""
    if not user_ids:
        await ctx.send("⚠️ Provide at least one user ID. Usage: `.massban 123 456 789 reason`")
        return
    banned, failed = [], []
    for uid in user_ids:
        try:
            await ctx.guild.ban(discord.Object(id=uid), reason=f"Massban: {reason} (by {ctx.author})")
            await bot.db.add_case(ctx.guild.id, "massban", ctx.author.id, uid, reason)
            banned.append(uid)
        except discord.HTTPException:
            failed.append(uid)
    embed = discord.Embed(
        title="🔨 Mass Ban Complete",
        color=discord.Color.dark_red(),
        description=f"**Reason:** {reason}",
    )
    embed.add_field(name=f"✅ Banned ({len(banned)})", value=", ".join(f"`{u}`" for u in banned) or "None", inline=False)
    if failed:
        embed.add_field(name=f"❌ Failed ({len(failed)})", value=", ".join(f"`{u}`" for u in failed), inline=False)
    embed.set_footer(text=f"Executed by {ctx.author}")
    await ctx.send(embed=embed)
    await post_modlog(ctx.guild, embed)


def _parse_duration(s: str) -> Optional[timedelta]:
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    try:
        unit = s[-1].lower()
        amount = int(s[:-1])
        if unit not in units:
            return None
        return timedelta(**{units[unit]: amount})
    except (ValueError, IndexError):
        return None


@bot.command(name="tempmute", aliases=["mute"])
@commands.has_permissions(moderate_members=True)
async def tempmute_cmd(ctx: commands.Context, member: discord.Member, duration: str, *, reason: str = "No reason provided"):
    err = _hierarchy_error(ctx, member)
    if err:
        await ctx.send(f"⛔ {err}")
        return
    delta = _parse_duration(duration)
    if not delta:
        await ctx.send("⚠️ Invalid duration. Use formats like `10m`, `1h`, `1d`.")
        return
    if delta > timedelta(days=28):
        await ctx.send("⚠️ Discord timeouts cap out at 28 days.")
        return
    unmute_at = datetime.now(timezone.utc) + delta
    try:
        await member.timeout(delta, reason=f"{reason} (by {ctx.author})")
    except discord.Forbidden:
        await ctx.send("⛔ I don't have permission to timeout that member.")
        return
    await bot.db.add_tempmute(ctx.guild.id, member.id, ctx.author.id, reason, unmute_at)
    case_num = await bot.db.add_case(ctx.guild.id, "tempmute", ctx.author.id, member.id, reason)

    dm_embed = discord.Embed(
        title=f"🔇 You were muted in {ctx.guild.name}",
        description=f"**Duration:** {duration}\n**Reason:** {reason}\n**Expires:** <t:{int(unmute_at.timestamp())}:R>",
        color=discord.Color.orange(),
    )
    dmed = await _dm_safely(member, dm_embed)

    embed = _case_embed("tempmute", case_num, member, ctx.author, reason,
                         extra={"Duration": duration, "Expires": f"<t:{int(unmute_at.timestamp())}:R>"})
    await ctx.send(embed=embed)
    if not dmed:
        await ctx.send("ℹ️ Couldn't DM the member.", delete_after=8)
    await post_modlog(ctx.guild, embed)


@bot.command(name="unmute")
@commands.has_permissions(moderate_members=True)
async def unmute_cmd(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    try:
        await member.timeout(None, reason=f"{reason} (by {ctx.author})")
    except discord.Forbidden:
        await ctx.send("⛔ I don't have permission to do that.")
        return
    await bot.db.remove_tempmute(ctx.guild.id, member.id)
    case_num = await bot.db.add_case(ctx.guild.id, "unmute", ctx.author.id, member.id, reason)
    embed = _case_embed("unmute", case_num, member, ctx.author, reason)
    await ctx.send(embed=embed)
    await post_modlog(ctx.guild, embed)


@bot.command(name="tempban")
@commands.has_permissions(ban_members=True)
async def tempban_cmd(ctx: commands.Context, member: discord.Member, duration: str, *, reason: str = "No reason provided"):
    err = _hierarchy_error(ctx, member)
    if err:
        await ctx.send(f"⛔ {err}")
        return
    delta = _parse_duration(duration)
    if not delta:
        await ctx.send("⚠️ Invalid duration. Use formats like `1h`, `1d`, `1w`.")
        return
    unban_at = datetime.now(timezone.utc) + delta

    dm_embed = discord.Embed(
        title=f"⛔ You were temp-banned from {ctx.guild.name}",
        description=f"**Duration:** {duration}\n**Reason:** {reason}\n**Expires:** <t:{int(unban_at.timestamp())}:R>",
        color=discord.Color.dark_red(),
    )
    dmed = await _dm_safely(member, dm_embed)

    await member.ban(reason=f"{reason} (by {ctx.author})")
    await bot.db.add_tempban(ctx.guild.id, member.id, ctx.author.id, reason, unban_at)
    case_num = await bot.db.add_case(ctx.guild.id, "tempban", ctx.author.id, member.id, reason)

    embed = _case_embed("tempban", case_num, member, ctx.author, reason,
                         extra={"Duration": duration, "Expires": f"<t:{int(unban_at.timestamp())}:R>"})
    await ctx.send(embed=embed)
    if not dmed:
        await ctx.send("ℹ️ Couldn't DM the member before banning.", delete_after=8)
    await post_modlog(ctx.guild, embed)


# ── Channel & message control ────────────────────────────────────────────────

@bot.command(name="purge", aliases=["clear"])
@commands.has_permissions(manage_messages=True)
async def purge_cmd(ctx: commands.Context, amount: int, member: Optional[discord.Member] = None):
    """Bulk-delete messages. .purge 50  or  .purge 50 @user"""
    if amount < 1 or amount > 500:
        await ctx.send("⚠️ Choose an amount between 1 and 500.")
        return
    await ctx.message.delete()

    def check(m: discord.Message) -> bool:
        return member is None or m.author.id == member.id

    deleted = await ctx.channel.purge(limit=amount, check=check)
    embed = _action_embed(
        "🧽", "Messages Purged",
        f"Deleted **{len(deleted)}** message(s) in {ctx.channel.mention}" + (f" from {member.mention}" if member else ""),
        discord.Color.blurple(),
    )
    embed.set_footer(text=f"Requested by {ctx.author}")
    msg = await ctx.send(embed=embed)
    await asyncio.sleep(5)
    try:
        await msg.delete()
    except discord.HTTPException:
        pass
    await post_modlog(ctx.guild, embed)


@bot.command(name="lockdown", aliases=["lock"])
@commands.has_permissions(manage_channels=True)
async def lockdown_cmd(ctx: commands.Context, channel: Optional[discord.TextChannel] = None, *, reason: str = "No reason provided"):
    channel = channel or ctx.channel
    overwrite = channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False
    await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Lockdown: {reason} (by {ctx.author})")
    embed = _action_embed("🔒", "Channel Locked", f"{channel.mention} has been locked.\n**Reason:** {reason}", discord.Color.dark_grey())
    await ctx.send(embed=embed)
    await post_modlog(ctx.guild, embed)


@bot.command(name="unlock")
@commands.has_permissions(manage_channels=True)
async def unlock_cmd(ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
    channel = channel or ctx.channel
    overwrite = channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = None
    await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Unlock (by {ctx.author})")
    embed = _action_embed("🔓", "Channel Unlocked", f"{channel.mention} has been unlocked.", discord.Color.green())
    await ctx.send(embed=embed)
    await post_modlog(ctx.guild, embed)


@bot.command(name="slowmode")
@commands.has_permissions(manage_channels=True)
async def slowmode_cmd(ctx: commands.Context, seconds: int, channel: Optional[discord.TextChannel] = None):
    channel = channel or ctx.channel
    if seconds < 0 or seconds > 21600:
        await ctx.send("⚠️ Slowmode must be between 0 and 21600 seconds (6 hours).")
        return
    await channel.edit(slowmode_delay=seconds)
    desc = f"Slowmode disabled in {channel.mention}." if seconds == 0 else f"Slowmode set to **{seconds}s** in {channel.mention}."
    embed = _action_embed("🐌", "Slowmode Updated", desc, discord.Color.blurple())
    await ctx.send(embed=embed)


@bot.command(name="nuke")
@commands.has_permissions(administrator=True)
async def nuke_cmd(ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
    """Clones and deletes a channel — wipes its entire message history instantly."""
    channel = channel or ctx.channel
    new_channel = await channel.clone(reason=f"Nuke (by {ctx.author})")
    await new_channel.edit(position=channel.position)
    await channel.delete(reason=f"Nuke (by {ctx.author})")
    embed = discord.Embed(
        title="💣 Channel Nuked",
        description=f"{new_channel.mention} has been wiped clean.",
        color=discord.Color.dark_red(),
    )
    embed.set_footer(text=f"Executed by {ctx.author}")
    await new_channel.send(embed=embed)
    await post_modlog(ctx.guild, embed)


# ═══════════════════════════════════════════════════════════════════════════════
#  COMMANDS — config
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="quicksetup", aliases=["setup"])
@commands.has_permissions(administrator=True)
async def quicksetup_cmd(ctx: commands.Context):
    """One-command setup: creates live Members/Boosts counter channels and saves config."""
    guild = ctx.guild
    msg = await ctx.send("✨ Setting up **aj's crib**... this'll take a moment.")

    # Find or create the stats category
    category = discord.utils.find(lambda c: c.name == "📊 Server Stats", guild.categories)
    if category is None:
        category = await guild.create_category("📊 Server Stats", reason="aj's crib quicksetup")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=True),
        guild.me: discord.PermissionOverwrite(connect=True, view_channel=True, manage_channels=True),
    }

    config = await bot.db.get_config(guild.id)
    created = []

    # Members counter channel
    members_chan_id = config.get("members_stats_channel")
    members_chan = guild.get_channel(members_chan_id) if members_chan_id else None
    if members_chan is None:
        members_chan = await guild.create_voice_channel(
            MEMBERS_CHANNEL_FORMAT.format(count=guild.member_count),
            category=category,
            overwrites=overwrites,
            reason="aj's crib quicksetup — members counter",
        )
        await bot.db.update_config(guild.id, "members_stats_channel", members_chan.id)
        created.append("Members counter")
    else:
        await members_chan.edit(name=MEMBERS_CHANNEL_FORMAT.format(count=guild.member_count))

    # Boosts counter channel
    boosts_chan_id = config.get("boosts_stats_channel")
    boosts_chan = guild.get_channel(boosts_chan_id) if boosts_chan_id else None
    if boosts_chan is None:
        boosts_chan = await guild.create_voice_channel(
            BOOSTS_CHANNEL_FORMAT.format(count=guild.premium_subscription_count),
            category=category,
            overwrites=overwrites,
            reason="aj's crib quicksetup — boosts counter",
        )
        await bot.db.update_config(guild.id, "boosts_stats_channel", boosts_chan.id)
        created.append("Boosts counter")
    else:
        await boosts_chan.edit(name=BOOSTS_CHANNEL_FORMAT.format(count=guild.premium_subscription_count))

    bot._stats_last_update[members_chan.id] = time.monotonic()
    bot._stats_last_update[boosts_chan.id] = time.monotonic()

    embed = discord.Embed(
        title="✅ aj's crib is set up!",
        color=discord.Color.green(),
        description=(
            f"📁 Category: **{category.name}**\n"
            f"👥 Members tracker: {members_chan.mention}\n"
            f"🚀 Boosts tracker: {boosts_chan.mention}\n\n"
            "Both update automatically as members join/leave and the server gets boosted "
            "(Discord limits renames, so they refresh instantly when possible and at least every 10 minutes)."
        ),
    )
    await msg.edit(content=None, embed=embed)



@bot.command(name="config")
@commands.has_permissions(administrator=True)
async def config_cmd(ctx: commands.Context, key: str = None, value: str = None):
    if key is None:
        config = await bot.db.get_config(ctx.guild.id)
        if not config:
            await ctx.send("No config set for this server yet.")
            return
        lines = [f"**{k}**: `{v}`" for k, v in config.items() if k not in ("_id", "guild_id")]
        embed = discord.Embed(
            title="⚙️ Server Config",
            description="\n".join(lines) or "Empty",
            color=discord.Color.blurple(),
        )
        await ctx.send(embed=embed)
        return
    if value is None:
        config = await bot.db.get_config(ctx.guild.id)
        await ctx.send(f"`{key}` = `{config.get(key, 'not set')}`")
        return
    await bot.db.update_config(ctx.guild.id, key, value)
    await ctx.send(f"✅ Set `{key}` = `{value}`")


# ═══════════════════════════════════════════════════════════════════════════════
#  COMMANDS — utility / admin tools
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="serverinfo", aliases=["server", "si"])
async def serverinfo_cmd(ctx: commands.Context):
    guild = ctx.guild
    humans = sum(1 for m in guild.members if not m.bot)
    bots = guild.member_count - humans
    text_chans = len(guild.text_channels)
    voice_chans = len(guild.voice_channels)
    embed = discord.Embed(title=f"📊 {guild.name}", color=discord.Color.blurple(), timestamp=guild.created_at)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Owner", value=f"<@{guild.owner_id}>", inline=True)
    embed.add_field(name="Members", value=f"{guild.member_count} ({humans} humans, {bots} bots)", inline=True)
    embed.add_field(name="Boosts", value=f"{guild.premium_subscription_count} (Tier {guild.premium_tier})", inline=True)
    embed.add_field(name="Channels", value=f"{text_chans} text, {voice_chans} voice", inline=True)
    embed.add_field(name="Roles", value=str(len(guild.roles)), inline=True)
    embed.add_field(name="Created", value=f"<t:{int(guild.created_at.timestamp())}:R>", inline=True)
    embed.set_footer(text=f"Server ID: {guild.id}")
    await ctx.send(embed=embed)


@bot.command(name="userinfo", aliases=["whois", "ui"])
async def userinfo_cmd(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    roles = [r.mention for r in reversed(member.roles) if r.name != "@everyone"]
    embed = discord.Embed(title=f"🔎 {member}", color=member.color or discord.Color.blurple(), timestamp=member.joined_at)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID", value=f"`{member.id}`", inline=True)
    embed.add_field(name="Nickname", value=member.nick or "None", inline=True)
    embed.add_field(name="Bot", value="Yes" if member.bot else "No", inline=True)
    embed.add_field(name="Joined Server", value=f"<t:{int(member.joined_at.timestamp())}:R>" if member.joined_at else "Unknown", inline=True)
    embed.add_field(name="Account Created", value=f"<t:{int(member.created_at.timestamp())}:R>", inline=True)
    if member.premium_since:
        embed.add_field(name="Boosting Since", value=f"<t:{int(member.premium_since.timestamp())}:R>", inline=True)
    embed.add_field(name=f"Roles ({len(roles)})", value=" ".join(roles) if roles else "None", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="backup", aliases=["exportconfig"])
@commands.has_permissions(administrator=True)
async def backup_cmd(ctx: commands.Context):
    """Exports this server's full bot config as a JSON file — handy before big changes."""
    config = await bot.db.get_config(ctx.guild.id)
    if not config:
        await ctx.send("⚠️ No config found for this server yet.")
        return
    safe_config = {k: v for k, v in config.items() if k != "_id"}
    buf = io.BytesIO(json.dumps(safe_config, indent=2, default=str).encode("utf-8"))
    file = discord.File(buf, filename=f"{ctx.guild.id}_config_backup.json")
    embed = discord.Embed(
        title="💾 Config Backup Ready",
        description="Your server's bot configuration has been exported. Keep this safe — "
                     "you can hand it to a moderator to restore settings if something goes wrong.",
        color=discord.Color.green(),
    )
    await ctx.send(embed=embed, file=file)


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN environment variable is not set.")
    bot.run(token)


if __name__ == "__main__":
    main()
