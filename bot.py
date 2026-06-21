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
import math
import time
import random
import asyncio
import logging
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord.ext import commands, tasks
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()  # reads a .env file in the working directory into os.environ, if present

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
VOICE_XP_INTERVAL = 60   # seconds between voice XP ticks
VOICE_XP_PER_TICK = 5    # XP awarded per tick to each eligible member
VOICE_XP_REQUIRE_UNMUTED = True   # skip self-muted/deafened members
VOICE_XP_REQUIRE_OTHERS  = True   # skip a member who is alone in their channel (no farming alone)
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
        self.counters       = db["counters"]  # atomic per-guild sequence numbers (case#, report#, ...)

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
            await self.config.create_index("guild_id", unique=True)
            await self.levels.create_index([("user_id", 1), ("guild_id", 1)], unique=True)
            await self.levels.create_index([("guild_id", 1), ("total_xp", -1)])
            await self.invites.create_index([("guild_id", 1), ("invite_code", 1)])
            await self.role_menus.create_index([("guild_id", 1), ("menu_id", 1)])
            await self.tickets.create_index([("guild_id", 1), ("channel_id", 1)])
            await self.boosts.create_index([("guild_id", 1), ("user_id", 1)], unique=True)
            await self.analytics.create_index([("guild_id", 1), ("date", 1)], unique=True)
            await self.reaction_roles.create_index([("guild_id", 1), ("message_id", 1)])
            await self.giveaways.create_index([("guild_id", 1), ("message_id", 1)])
            await self.giveaways.create_index("ends_at")
            await self.msg_tracking.create_index([("guild_id", 1), ("user_id", 1)], unique=True)
            await self.msg_tracking.create_index([("guild_id", 1), ("total_messages", -1)])
            await self.warns.create_index([("guild_id", 1), ("user_id", 1)])
            await self.warns.create_index("created_at")
            await self.cases.create_index([("guild_id", 1), ("case_number", 1)], unique=True)
            await self.counters.create_index([("guild_id", 1), ("name", 1)], unique=True)
            await self.cases.create_index([("guild_id", 1), ("target_id", 1)])
            await self.tempmutes.create_index("unmute_at")
            await self.roblox_history.create_index("_id")
            await self.db["join_log"].create_index([("guild_id", 1), ("joined_at", -1)])
            await self.db["leave_log"].create_index([("guild_id", 1), ("left_at", -1)])
            await self.db["join_log"].create_index("user_id")
            await self.tempbans.create_index("unban_at")
            await self.tempbans.create_index([("guild_id", 1), ("user_id", 1)])
            await self.reports.create_index([("guild_id", 1), ("created_at", -1)])
            await self.ticket_ratings.create_index([("guild_id", 1), ("ticket_id", 1)])
            await self.db["daily_msg_counts"].create_index([("guild_id", 1), ("date", 1)], unique=True)
            await self.db["staff_activity"].create_index([("guild_id", 1), ("user_id", 1)], unique=True)
            logger.info("Indexes ready")
        except Exception as exc:
            logger.error("Index error: %s", exc)

    async def close(self):
        self._client.close()

    # ── Atomic Counters ───────────────────────────────────────────────────────

    async def next_counter(self, gid: int, name: str) -> int:
        """Atomically returns the next sequence number for `name` in this guild
        (e.g. 'case_number', 'report_number'). Uses a single atomic $inc via
        find_one_and_update, so concurrent calls can never hand out the same
        number twice — unlike a read-then-increment pattern, which races under
        concurrent staff actions and can throw a duplicate-key error."""
        doc = await self.counters.find_one_and_update(
            {"guild_id": gid, "name": name},
            {"$inc": {"value": 1}},
            upsert=True,
            return_document=True,
        )
        return doc["value"]

    async def seed_counters_from_existing(self):
        """One-time migration: seeds each guild's atomic counters above any
        existing case/report numbers so new and old data never collide.
        Safe to run on every startup — no-ops once counters are already ahead."""
        for coll, field, name in ((self.cases, "case_number", "case_number"),
                                   (self.reports, "report_number", "report_number")):
            pipeline = [{"$group": {"_id": "$guild_id", "max": {"$max": f"${field}"}}}]
            async for row in coll.aggregate(pipeline):
                gid, max_val = row["_id"], row["max"] or 0
                # Single atomic upsert: insert with max_val if missing, or bump
                # to max_val only if the stored value is behind — no race window.
                await self.counters.update_one(
                    {"guild_id": gid, "name": name},
                    [{"$set": {"guild_id": gid, "name": name,
                               "value": {"$max": [{"$ifNull": ["$value", 0]}, max_val]}}}],
                    upsert=True,
                )

    # ── Config ────────────────────────────────────────────────────────────────

    async def get_config(self, gid: int) -> dict:
        return await self._retry(
            lambda: self.config.find_one({"guild_id": gid})
        ) or {}

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
        return await self._retry(
            lambda: self.levels.find_one({"user_id": uid, "guild_id": gid})
        ) or {}

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
            "badges": doc.get("badges", []) if doc else [],
        }

    async def add_voice_xp(self, uid: int, gid: int, xp: int, minutes: int) -> dict:
        """Awards voice XP — stacks into the SAME total_xp/level as message XP
        (one unified level per member), but tracks voice_minutes separately so
        the leaderboard can show a dedicated voice stat. Does not touch
        `messages` or the message-streak — those are message-XP-only."""
        doc = await self.levels.find_one({"user_id": uid, "guild_id": gid})
        old_level = calculate_level(doc.get("total_xp", 0))[0] if doc else 0
        total_xp = (doc.get("total_xp", 0) if doc else 0) + xp
        voice_minutes = (doc.get("voice_minutes", 0) if doc else 0) + minutes
        new_level, xp_in, xp_need = calculate_level(total_xp)
        await self.levels.update_one(
            {"user_id": uid, "guild_id": gid},
            {"$set": {
                "total_xp": total_xp, "level": new_level, "voice_minutes": voice_minutes,
            },
             "$setOnInsert": {"messages": 0, "streak": 0}},
            upsert=True,
        )
        return {
            "total_xp": total_xp, "level": new_level, "voice_minutes": voice_minutes,
            "xp_in": xp_in, "xp_need": xp_need, "leveled": new_level > old_level,
            "old_level": old_level, "badges": doc.get("badges", []) if doc else [],
        }

    async def reset_xp(self, uid: int, gid: int):
        await self.levels.update_one(
            {"user_id": uid, "guild_id": gid},
            {"$set": {"total_xp": 0, "level": 0, "messages": 0, "streak": 0, "voice_minutes": 0}},
            upsert=True,
        )

    async def get_leaderboard(self, gid: int, limit: int = 10) -> list[dict]:
        return await self.levels.find(
            {"guild_id": gid}, sort=[("total_xp", -1)], limit=limit
        ).to_list(length=limit)

    async def get_voice_leaderboard(self, gid: int, limit: int = 10) -> list[dict]:
        return await self.levels.find(
            {"guild_id": gid, "voice_minutes": {"$gt": 0}}, sort=[("voice_minutes", -1)], limit=limit
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

    async def record_member_inviter(self, gid: int, member_id: int, inviter_id: int, code: str):
        """Remembers who invited a specific member, so the count can be
        decremented accurately if/when they leave. Keyed separately from the
        per-code and per-inviter-total docs above."""
        await self.invites.update_one(
            {"guild_id": gid, "invite_code": f"__member_{member_id}"},
            {"$set": {"inviter_id": inviter_id, "code": code}},
            upsert=True,
        )

    async def get_inviter_of_member(self, gid: int, member_id: int) -> Optional[int]:
        doc = await self.invites.find_one({"guild_id": gid, "invite_code": f"__member_{member_id}"})
        return doc.get("inviter_id") if doc else None

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

    async def save_ticket(self, gid: int, channel_id: int, uid: int, ticket_id: int, priority: str = "medium"):
        await self.tickets.update_one(
            {"guild_id": gid, "channel_id": channel_id},
            {"$set": {
                "guild_id": gid, "user_id": uid, "ticket_id": ticket_id, "priority": priority,
                "claimed_by": None, "opened_at": datetime.now(timezone.utc),
                "last_activity_at": datetime.now(timezone.utc), "closed": False,
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

    async def claim_ticket(self, channel_id: int, staff_id: int) -> bool:
        """Claims a ticket for a staff member. Returns False if already claimed
        by someone else."""
        doc = await self.tickets.find_one({"channel_id": channel_id})
        if doc and doc.get("claimed_by") and doc["claimed_by"] != staff_id:
            return False
        await self.tickets.update_one(
            {"channel_id": channel_id},
            {"$set": {"claimed_by": staff_id, "claimed_at": datetime.now(timezone.utc)}},
        )
        return True

    async def unclaim_ticket(self, channel_id: int):
        await self.tickets.update_one(
            {"channel_id": channel_id},
            {"$set": {"claimed_by": None}, "$unset": {"claimed_at": ""}},
        )

    async def set_ticket_priority(self, channel_id: int, priority: str):
        await self.tickets.update_one(
            {"channel_id": channel_id},
            {"$set": {"priority": priority}},
        )

    async def touch_ticket_activity(self, channel_id: int):
        """Bumps last_activity_at — called on every message inside a ticket
        channel so the auto-close loop knows it's still alive. Also clears
        any prior inactivity warning so it can fire again later if the
        ticket goes quiet a second time."""
        await self.tickets.update_one(
            {"channel_id": channel_id},
            {"$set": {"last_activity_at": datetime.now(timezone.utc)},
             "$unset": {"inactivity_warned_at": ""}},
        )

    async def get_open_tickets(self, gid: Optional[int] = None) -> list[dict]:
        query = {"closed": False}
        if gid is not None:
            query["guild_id"] = gid
        return await self.tickets.find(query).to_list(length=500)

    async def mark_ticket_warned_inactive(self, channel_id: int):
        await self.tickets.update_one(
            {"channel_id": channel_id},
            {"$set": {"inactivity_warned_at": datetime.now(timezone.utc)}},
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

    async def add_warn(self, gid: int, uid: int, mod_id: int, reason: str, source: str = "manual") -> int:
        await self.warns.insert_one({
            "guild_id": gid, "user_id": uid, "mod_id": mod_id,
            "reason": reason, "source": source, "created_at": datetime.now(timezone.utc),
        })
        return await self.warns.count_documents({"guild_id": gid, "user_id": uid})

    async def get_warns(self, gid: int, uid: int) -> list[dict]:
        return await self.warns.find(
            {"guild_id": gid, "user_id": uid},
            sort=[("created_at", -1)],
        ).to_list(length=50)

    async def get_active_automod_warns(self, gid: int, uid: int, hours: int = 24) -> int:
        """Count automod-issued warns in the last `hours` — used to decide mute escalation.
        Manual warns from staff never count toward this, so a human warning never auto-mutes."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        return await self.warns.count_documents({
            "guild_id": gid, "user_id": uid, "source": "automod", "created_at": {"$gte": cutoff},
        })

    async def get_active_strikes(self, gid: int, uid: int, hours: int = 24) -> int:
        """Alias over the same rolling-24h automod-warn count used for mute
        escalation — exposed as 'strikes' to staff via `.strikes`/`.clearstrikes`."""
        return await self.get_active_automod_warns(gid, uid, hours=hours)

    async def clear_strikes(self, gid: int, uid: int) -> int:
        """Clears only automod-sourced warns for this member (their active
        'strikes'), leaving any manual staff warns intact. Use `.clearwarns`
        instead if you want to wipe everything."""
        r = await self.warns.delete_many({"guild_id": gid, "user_id": uid, "source": "automod"})
        return r.deleted_count

    async def clear_warns(self, gid: int, uid: int) -> int:
        r = await self.warns.delete_many({"guild_id": gid, "user_id": uid})
        return r.deleted_count

    async def get_all_warns(self, gid: int) -> list[dict]:
        return await self.warns.find({"guild_id": gid}).to_list(length=None)

    # ── Case System ───────────────────────────────────────────────────────────

    async def add_case(self, gid: int, action: str, mod_id: int, target_id: int,
                       reason: str, extra: dict = None) -> int:
        num  = await self.next_counter(gid, "case_number")
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

    # ── Staff Activity Audit Trail ────────────────────────────────────────────

    async def log_staff_action(self, gid: int, mod_id: int, action: str, target_id: Optional[int] = None):
        """Record a staff action for the audit trail / inactivity tracker.
        Also clears any pending inactivity-alert flag — acting again resets
        the clock so a staffer who goes quiet a second time gets DM'd again."""
        await self._client["lxte_assistant"]["staff_activity"].update_one(
            {"guild_id": gid, "user_id": mod_id},
            {
                "$inc": {f"actions.{action}": 1, "actions.total": 1},
                "$set": {"last_action_at": datetime.now(timezone.utc)},
                "$push": {"recent": {"$each": [{"action": action, "target": target_id,
                          "at": datetime.now(timezone.utc)}], "$slice": -50}},
                "$setOnInsert": {"guild_id": gid, "user_id": mod_id, "first_action_at": datetime.now(timezone.utc)},
                "$unset": {"inactivity_alerted_at": ""},
            },
            upsert=True,
        )

    async def get_staff_activity(self, gid: int, limit: int = 15) -> list[dict]:
        coll = self._client["lxte_assistant"]["staff_activity"]
        return await coll.find({"guild_id": gid}, sort=[("actions.total", -1)]).to_list(length=limit)

    async def get_staff_activity_user(self, gid: int, uid: int) -> dict:
        coll = self._client["lxte_assistant"]["staff_activity"]
        return await coll.find_one({"guild_id": gid, "user_id": uid}) or {}

    async def get_stale_staff_activity(self, gid: int, cutoff: datetime) -> list[dict]:
        """Staff who have acted before but gone quiet past `cutoff`, and
        haven't already been DM'd about it since their last action — used by
        the proactive staff-inactivity DM alert loop."""
        coll = self._client["lxte_assistant"]["staff_activity"]
        return await coll.find({
            "guild_id": gid,
            "last_action_at": {"$lt": cutoff},
            "inactivity_alerted_at": {"$exists": False},
        }).to_list(length=200)

    async def mark_staff_alerted(self, gid: int, uid: int):
        coll = self._client["lxte_assistant"]["staff_activity"]
        await coll.update_one(
            {"guild_id": gid, "user_id": uid},
            {"$set": {"inactivity_alerted_at": datetime.now(timezone.utc)}},
        )

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

    # ── Anti-nuke containment snapshots (survive bot restarts) ──────────────────
    # The in-memory `_contained_members` dict is fast but vanishes on restart,
    # which used to mean `.release` could find nothing to restore if the bot
    # redeployed while someone was contained. These mirror every containment
    # to the DB so it's always recoverable, even after a restart.

    async def save_containment(self, gid: int, uid: int, role_ids: list[int], reason: str):
        await self.db["containments"].update_one(
            {"guild_id": gid, "user_id": uid},
            {"$set": {"guild_id": gid, "user_id": uid, "role_ids": role_ids,
                      "reason": reason, "created_at": datetime.now(timezone.utc)}},
            upsert=True,
        )

    async def get_containment(self, gid: int, uid: int) -> Optional[dict]:
        return await self.db["containments"].find_one({"guild_id": gid, "user_id": uid})

    async def delete_containment(self, gid: int, uid: int):
        await self.db["containments"].delete_one({"guild_id": gid, "user_id": uid})

    async def get_all_containments(self, gid: int) -> list[dict]:
        return await self.db["containments"].find({"guild_id": gid}).to_list(length=200)

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
        now = datetime.now(timezone.utc)
        num = await self.next_counter(gid, "report_number")
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

    is_top = False
    if "top_leaderboard" not in badges:
        top_row = await db.get_leaderboard(member.guild.id, limit=1)
        is_top = bool(top_row) and top_row[0].get("user_id") == member.id

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
        ("top_leaderboard", is_top),
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
# Design goals — "nuclear but warn":
#   - Thresholds are tuned aggressive on purpose (low caps %, few emojis, fast
#     flood detection) — this automod is meant to catch things EARLY, not give
#     people room to test the line.
#   - Despite being aggressive on detection, the ACTION on every single rule is
#     uniformly delete + warn — there is no instant-mute-on-first-hit rule, for
#     ANY violation type (scam links, zalgo, mention spam included). There's
#     only ONE warning system in this bot — automod violations create a real
#     warn (tagged source="automod") instead of a separate hidden "strikes"
#     counter, so `.warnings` always shows the whole picture.
#   - Repeat automod-sourced warns in a 24h window escalate to a timeout, and
#     escalate FASTER now (lower warn counts trigger each mute tier). There are
#     no kicks or bans from automod — ever, by design.
#   - This "warn first, always" rule applies ONLY to per-message automod. It
#     does NOT apply to anti-nuke, anti-raid, or staff-abuse detection below —
#     those remain instant-action (contain/strip/mute) by design, since those
#     exist to stop active server-wide damage in real time, not to police
#     individual messages.

AUTOMOD_DEFAULTS = {
    "antispam":    True,
    "antiinvite":  True,
    "antilink":    True,
    "mentionspam": True,
    "zalgo":       True,
    "capsspam":    True,
    "emojispam":   True,
    "scamlinks":   True,
}

INVITE_RE = re.compile(
    r"(?:discord\.gg|discord(?:app)?\.com/invite)/[a-zA-Z0-9-]+", re.IGNORECASE
)
LINK_RE = re.compile(
    r"(?:https?://|www\.)[^\s]+", re.IGNORECASE
)
ZALGO_RE = re.compile(r"[\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff]")
EMOJI_RE = re.compile(
    r"<a?:\w+:\d+>|[\U0001F300-\U0001FAFF\u2600-\u27BF]"
)

ZALGO_THRESHOLD     = 5     # combining marks in one message before it's "zalgo" (was 8)
MENTION_THRESHOLD   = 3     # unique user + role mentions before it's "mention spam" (was 4)
FLOOD_WINDOW_SEC    = 6.0   # burst window — widened so fast (but legitimate) typers aren't caught
FLOOD_MAX_MESSAGES  = 10    # messages allowed in that window — raw speed alone is a weak spam signal
DUPLICATE_WINDOW_SEC  = 20.0  # window for repeated/near-identical content
DUPLICATE_MAX_COUNT   = 5    # repeats of a normal/longer message allowed before it's flagged as spam
DUPLICATE_SHORT_COUNT = 8    # repeats allowed for short/common messages ("hi", "lol", "ok"...) —
                              # short chat-filler is extremely common between real humans, so it
                              # gets a much higher bar before automod treats it as spam
DUPLICATE_SHORT_MAX_LEN = 6  # a normalized message at/under this length is "short" for the above
COMMON_SHORT_PHRASES = {
    # common short replies/greetings that should basically never count as spam,
    # no matter how many times they're repeated back and forth in a chatty channel
    "hi", "hey", "hello", "yo", "sup", "lol", "lmao", "lmfao", "rofl",
    "ok", "okay", "k", "kk", "yes", "no", "yep", "yup", "nope", "nah",
    "gg", "gz", "gn", "gm", "ty", "thx", "thanks", "np", "bye", "cya",
    "haha", "hahaha", "xd", ":)", ":(", "<3", "this", "same",
    "true", "facts", "real", "fr", "ratio", "based", "💀", "😭", "🔥",
}
CAPS_MIN_LENGTH      = 8    # messages shorter than this are never flagged for caps (was 10)
CAPS_RATIO_THRESHOLD = 0.6  # 60%+ uppercase letters trips it (was 0.7 — stricter)
EMOJI_MAX_COUNT      = 5    # emoji (unicode or custom) allowed in one message (was 8 — fewer emojis)

# Known scam/phishing domains impersonating Discord/Steam/giveaway sites.
# This list is intentionally small and curated rather than exhaustive — it
# catches the recurring nuke/phishing patterns without false-positiving on
# legitimate sites. Pair with antilink for full coverage.
SCAM_DOMAINS = {
    "dlscord.com", "discordapp.gift", "discrod.gg", "steamcommunity.ru",
    "steancommunity.com", "discord-nitro.com", "discord-gift.com",
    "steamcommunityy.com", "dlscordgift.com", "discordgift.site",
    "discord.gift-nitro.com", "discord-free-nitro.com", "discordnitro.gift",
    "discordgifts.org", "nitro-discord.com", "free-discord-nitro.com",
    "steamcommunitty.com", "steampowered.gift", "steamgift.com",
    "csgoempire.gift", "free-robux.gg", "bloxflip.gift",
    "roblox-free.com", "epicgames.gift", "claimyourgift.gg",
    "discord-app.net", "discordapp.net", "discod.gg",
}

_msg_history: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=20))


def _normalize(content: str) -> str:
    return re.sub(r"\s+", " ", content.strip().lower())


def _is_flooding(key: tuple[int, int], content: str) -> bool:
    """Smart spam check: weighs duplicate content much more heavily than raw speed,
    so a fast typer sending varied messages is never flagged. Short, extremely
    common chat replies ("hi", "lol", "ok"...) get a much higher repeat
    allowance — two people saying "Hi" back and forth a few times is normal
    conversation, not spam."""
    now  = time.monotonic()
    norm = _normalize(content)
    hist = _msg_history[key]
    hist.append((now, norm))

    if norm:
        dup_count = sum(1 for t, c in hist if now - t <= DUPLICATE_WINDOW_SEC and c == norm)
        if norm in COMMON_SHORT_PHRASES or len(norm) <= DUPLICATE_SHORT_MAX_LEN:
            limit = DUPLICATE_SHORT_COUNT
        else:
            limit = DUPLICATE_MAX_COUNT
        if dup_count >= limit:
            return True

    flood_count = sum(1 for t, _ in hist if now - t <= FLOOD_WINDOW_SEC)
    return flood_count >= FLOOD_MAX_MESSAGES


def _has_invite(content: str) -> bool:
    return bool(INVITE_RE.search(content))


def _has_link(content: str) -> bool:
    return bool(LINK_RE.search(content))


def _is_mention_spam(message: discord.Message) -> bool:
    total = len(message.raw_mentions) + len(message.raw_role_mentions)
    return total >= MENTION_THRESHOLD


def _is_zalgo(content: str) -> bool:
    return len(ZALGO_RE.findall(content)) >= ZALGO_THRESHOLD


def _is_caps_spam(content: str) -> bool:
    letters = [c for c in content if c.isalpha()]
    if len(letters) < CAPS_MIN_LENGTH:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return (upper / len(letters)) >= CAPS_RATIO_THRESHOLD


def _is_emoji_spam(content: str) -> bool:
    return len(EMOJI_RE.findall(content)) >= EMOJI_MAX_COUNT


def _has_scam_link(content: str) -> bool:
    lowered = content.lower()
    return any(domain in lowered for domain in SCAM_DOMAINS)


async def run_automod(message: discord.Message, config: dict, allow_invites: bool = False) -> Optional[str]:
    """Returns a human-readable violation reason, or None if the message is clean."""
    content = message.content or ""

    if config.get("automod_scamlinks", True) and _has_scam_link(content):
        return "posting a known scam/phishing link"

    if config.get("automod_antiinvite", True) and not allow_invites and _has_invite(content):
        return "posting an unauthorized invite link"

    if config.get("automod_antilink", True) and not allow_invites and _has_link(content):
        return "posting an unauthorized link"

    if config.get("automod_mentionspam", True) and _is_mention_spam(message):
        return "mass-mention spam"

    if config.get("automod_zalgo", True) and _is_zalgo(content):
        return "zalgo/unicode spam"

    if config.get("automod_capsspam", True) and _is_caps_spam(content):
        return "excessive caps"

    if config.get("automod_emojispam", True) and _is_emoji_spam(content):
        return "emoji spam"

    if config.get("automod_antispam", True):
        key = (message.guild.id, message.author.id)
        if _is_flooding(key, content):
            return "message flooding/spam"

    return None


def _escalation_for_warns(warns: int) -> Optional[int]:
    """Returns a timeout duration in seconds for the given active automod-warn
    count, or None if it shouldn't trigger a mute yet. Never bans or kicks —
    automod is mute-only, by design. Every automod rule warns and deletes
    first, every time — this ladder is what eventually escalates repeat
    offenders to a timeout, regardless of which rule(s) they tripped.
    Starts at 5 minutes on the 3rd active warn, then climbs gently for every
    extra warn beyond that (3=5m, 4=10m, 5=15m, 6=20m, ... capped at 1h)."""
    if warns < 3:
        return None
    tier = warns - 3  # 0 at warns==3, 1 at warns==4, etc.
    seconds = 300 + (tier * 300)  # +5m per extra warn past the 3rd
    return min(seconds, 3600)


def _format_duration(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


async def handle_automod_violation(message: discord.Message, config: dict, reason: str):
    """Deletes the offending message, issues a real warn (source=automod), and
    escalates to a mute (never a kick/ban) once active automod-warns in the last
    24h cross a threshold. If the offender holds a staff tier role, the abuse is
    zero-tolerance: strip the staff role immediately and mute, regardless of
    warn count — staff are held to a stricter bar than regular members."""
    try:
        await message.delete()
    except discord.HTTPException:
        pass

    member = message.author
    tier = await get_staff_tier(member, config)
    if tier > TIER_NONE and not member.guild_permissions.administrator:
        stripped = []
        for key in (*STAFF_TIER_KEYS.values(), PARTNERSHIP_KEY):
            role_ids = set(config.get(key, []) or [])
            for role in list(member.roles):
                if role.id in role_ids:
                    try:
                        await member.remove_roles(role, reason=f"Automod abuse by staff: {reason}")
                        stripped.append(role.name)
                    except discord.Forbidden:
                        pass
        try:
            await member.timeout(timedelta(minutes=30), reason=f"Staff automod abuse: {reason}")
        except discord.Forbidden:
            pass
        log_chan_id = config.get("automod_log_channel") or config.get("modlog_channel")
        if log_chan_id:
            chan = message.guild.get_channel(log_chan_id)
            if chan:
                embed = discord.Embed(
                    title="🚨 Staff Abuse Detected",
                    description=f"{member.mention} tripped automod while holding staff role(s).",
                    color=discord.Color.dark_red(),
                )
                embed.add_field(name="Trigger", value=reason, inline=False)
                embed.add_field(name="Role(s) stripped", value=", ".join(stripped) or "unknown", inline=True)
                embed.add_field(name="Action", value="🔇 Muted 30m", inline=True)
                embed.set_footer(text=f"{member} • {member.id}")
                embed.timestamp = datetime.now(timezone.utc)
                try:
                    await chan.send(embed=embed)
                except discord.HTTPException:
                    pass
        try:
            await member.send(
                f"🚨 You tripped automod in **{message.guild.name}** while holding a staff role "
                f"(**{reason}**). Your staff role(s) have been stripped and you've been muted 30m. "
                f"This is logged as staff abuse — reach out to a Senior Moderator/Admin if you believe "
                f"this was a mistake."
            )
        except discord.Forbidden:
            pass
        await bot.db.add_warn(message.guild.id, member.id, bot.user.id, f"STAFF ABUSE — Automod: {reason}", source="automod")
        return

    await bot.db.add_warn(message.guild.id, message.author.id, bot.user.id, f"Automod: {reason}", source="automod")
    active = await bot.db.get_active_automod_warns(message.guild.id, message.author.id)
    mute_seconds = _escalation_for_warns(active)

    action_value = None
    if mute_seconds:
        try:
            await message.author.timeout(
                timedelta(seconds=mute_seconds),
                reason=f"Automod escalation: {reason} ({active} warns in 24h)",
            )
            action_value = f"🔇 Muted for **{_format_duration(mute_seconds)}**"
        except discord.Forbidden:
            action_value = "⚠️ Tried to mute — missing permissions"

    log_chan_id = config.get("automod_log_channel") or config.get("modlog_channel")
    if log_chan_id:
        chan = message.guild.get_channel(log_chan_id)
        if chan:
            embed = discord.Embed(
                title="🛡️ Automod",
                description=f"Deleted a message from {message.author.mention} in {message.channel.mention}",
                color=discord.Color.dark_orange() if mute_seconds else discord.Color.gold(),
            )
            embed.add_field(name="Reason", value=reason, inline=False)
            embed.add_field(name="Warns (24h)", value=str(active), inline=True)
            if action_value:
                embed.add_field(name="Action", value=action_value, inline=True)
            embed.set_footer(text=f"{message.author} • {message.author.id}")
            embed.timestamp = datetime.now(timezone.utc)
            try:
                await chan.send(embed=embed)
            except discord.HTTPException:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
#  STAFF TIERS — "fake perms" that work even with zero real Discord permissions
# ═══════════════════════════════════════════════════════════════════════════════
#
# Staff are mapped to a role via quicksetup ("Staff Roles"), independent of real
# Discord permissions. Real `administrator` always passes every check (so you're
# never locked out before configuring this). Recommended setup: give staff roles
# little/no real Discord permissions and let the bot gate everything — that way
# nobody can act outside the bot (and outside its abuse limits) even if they
# wanted to.

TIER_NONE        = 0
TIER_TRIAL       = 1   # warn, mute, purge, basic stuff only
TIER_MOD         = 2   # + kick, ban, tempban, advanced stuff
TIER_SENIOR      = 3   # + server config / staff management
TIER_NAMES = {TIER_TRIAL: "Trial Moderator", TIER_MOD: "Moderator", TIER_SENIOR: "Senior Moderator"}

STAFF_TIER_KEYS = {
    TIER_TRIAL:  "staffrole_trial",
    TIER_MOD:    "staffrole_mod",
    TIER_SENIOR: "staffrole_senior",
}
PARTNERSHIP_KEY = "staffrole_partnership"

STAFF_INACTIVITY_ALERT_DAYS_DEFAULT = 7  # configurable per-guild via `.staffalert`


async def get_staff_tier(member: discord.Member, config: dict) -> int:
    if member.guild_permissions.administrator:
        return TIER_SENIOR
    role_ids = {r.id for r in member.roles}
    tier = TIER_NONE
    for t, key in STAFF_TIER_KEYS.items():
        configured = set(config.get(key, []) or [])
        if role_ids & configured:
            tier = max(tier, t)
    return tier


async def has_partnership_perm(member: discord.Member, config: dict) -> bool:
    if member.guild_permissions.administrator:
        return True
    role_ids = {r.id for r in member.roles}
    configured = set(config.get(PARTNERSHIP_KEY, []) or [])
    return bool(role_ids & configured)


async def is_automod_exempt(member: discord.Member, config: dict) -> bool:
    """Mod-tier and above are trusted not to get caught by spam/zalgo/mention filters."""
    if member.guild_permissions.administrator or member.guild_permissions.manage_messages:
        return True
    tier = await get_staff_tier(member, config)
    return tier >= TIER_MOD


async def can_post_invites(member: discord.Member, config: dict) -> bool:
    """Mod-tier+ and anyone holding the configured Partnership Manager role(s) may
    post invite links without tripping antiinvite."""
    tier = await get_staff_tier(member, config)
    if tier >= TIER_MOD:
        return True
    return await has_partnership_perm(member, config)


def staff_tier_check(min_tier: int):
    """A commands.check that gates a command behind a fake-perm staff tier,
    independent of real Discord permissions. Real `administrator` always passes."""
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.author.guild_permissions.administrator:
            return True
        config = await get_config(ctx.guild.id, bot.db)
        tier = await get_staff_tier(ctx.author, config)
        if tier >= min_tier:
            return True
        needed = TIER_NAMES.get(min_tier, "Staff")
        await ctx.send(f"nah, you need to be **{needed}** to use that.")
        return False
    return commands.check(predicate)


async def validate_mod_target(ctx: commands.Context, target: discord.Member) -> bool:
    """Shared guard for punitive mod commands (warn/mute/kick/ban/tempban/softban).
    Blocks: targeting yourself, targeting the bot, targeting the server owner,
    and targeting another staff member at an equal or higher tier than you
    (real Discord `administrator` always bypasses this, same as staff_tier_check).
    Sends a clear reason and returns False if the action should be blocked —
    callers should `return` immediately when this returns False. Without this,
    any Trial Mod could `.ban` a Senior Mod or the bot itself with no pushback."""
    if ctx.author.guild_permissions.administrator:
        return True
    if target.id == ctx.author.id:
        await ctx.send("you can't do that to yourself lol")
        return False
    if target.id == bot.user.id:
        await ctx.send("bro leave me out of it 💀")
        return False
    if ctx.guild.owner_id and target.id == ctx.guild.owner_id:
        await ctx.send("can't touch the owner, not doing that")
        return False
    config = await get_config(ctx.guild.id, bot.db)
    actor_tier = await get_staff_tier(ctx.author, config)
    target_tier = await get_staff_tier(target, config)
    if target_tier > TIER_NONE and target_tier >= actor_tier and not target.guild_permissions.administrator:
        await ctx.send("can't take action on someone at your tier or above")
        return False
    if target.guild_permissions.administrator:
        await ctx.send("can't take action on an admin")
        return False
    return True


# ═══════════════════════════════════════════════════════════════════════════════
#  ANTI-NUKE — server-wide raid/nuke protection (audit-log based)
# ═══════════════════════════════════════════════════════════════════════════════
#
# This watches for the classic nuke/raid signatures — mass channel deletion,
# mass role deletion, mass bans/kicks, webhook spam, and anyone being granted
# administrator — and reacts immediately. It works *regardless* of whether the
# culprit is a configured staff member, a compromised admin account, or a
# raider who somehow got a destructive role: only the server owner is exempt.
#
# On trigger: strip any configured staff role(s) the culprit holds, apply a
# short safety timeout so the damage stops immediately, and alert the owner in
# the alerts-log channel. Anti-nuke never bans or kicks automatically — kicks
# and bans are reserved for human Moderators/Senior Moderators, by design.

ANTINUKE_WINDOW    = 45   # seconds — the rolling window actions are counted in
ANTINUKE_THRESHOLDS = {
    "channel":   3,   # channel creates+deletes combined
    "role":      3,   # role creates+deletes combined
    "ban":       2,
    "kick":      2,
    "webhook":   1,   # zero tolerance — any unrecognized webhook creation trips it
    "permgrant": 1,   # zero tolerance — any single admin-perm grant trips it
}
ANTINUKE_SAFETY_MUTE_SECONDS = 3600  # 60 minutes, on top of the role strip

_antinuke_log: dict[tuple[int, int, str], deque] = defaultdict(lambda: deque(maxlen=20))
_antinuke_recent_targets: set[tuple[int, int]] = set()  # (guild_id, audit_entry_id) dedupe


def _antinuke_record(gid: int, uid: int, action: str) -> int:
    now = time.monotonic()
    dq = _antinuke_log[(gid, uid, action)]
    dq.append(now)
    active = [t for t in dq if now - t <= ANTINUKE_WINDOW]
    _antinuke_log[(gid, uid, action)] = deque(active, maxlen=20)
    return len(active)


async def _antinuke_find_executor(guild: discord.Guild, action: discord.AuditLogAction,
                                   target_id: Optional[int] = None, within_seconds: int = 8):
    """Looks up who performed a recent audit-logged action. Returns None if the
    bot can't read the audit log or no matching recent entry is found."""
    try:
        async for entry in guild.audit_logs(limit=8, action=action):
            age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
            if age > within_seconds:
                continue
            if target_id is not None and getattr(entry.target, "id", None) != target_id:
                continue
            return entry
    except (discord.Forbidden, discord.HTTPException):
        return None
    return None


_contained_members: dict[tuple[int, int], dict] = {}  # (guild_id, user_id) -> snapshot for restore


async def antinuke_contain(member: discord.Member, reason: str) -> list[str]:
    """Instantly strips ALL roles from this one member (snapshotting them
    first for exact restore later) and times them out. Nothing server-wide is
    touched — every other member, role, and channel is completely unaffected.
    Safe to call even if they're already contained (no-op). The snapshot is
    written to the database (not just kept in memory), so a bot restart while
    someone is contained can never lose the ability to restore them."""
    key = (member.guild.id, member.id)
    if key in _contained_members:
        return []
    existing = await bot.db.get_containment(member.guild.id, member.id)
    if existing:
        return []  # already contained (e.g. from before a restart) — don't re-snapshot stripped roles
    role_ids = [r.id for r in member.roles if not r.is_default()]
    snapshot = {"role_ids": role_ids, "reason": reason}
    _contained_members[key] = snapshot
    await bot.db.save_containment(member.guild.id, member.id, role_ids, reason)
    removed_names = [r.name for r in member.roles if not r.is_default()]
    try:
        if role_ids:
            await member.remove_roles(*[member.guild.get_role(rid) for rid in role_ids if member.guild.get_role(rid)],
                                       reason=f"Anti-nuke containment: {reason}")
        await member.timeout(timedelta(seconds=ANTINUKE_SAFETY_MUTE_SECONDS), reason=f"Anti-nuke containment: {reason}")
    except discord.Forbidden:
        pass
    return removed_names


async def antinuke_release(guild: discord.Guild, user_id: int) -> int:
    """Restores exactly the roles a contained member had before containment.
    Checks the database first (not just the in-memory cache), so this still
    works correctly even if the bot restarted since the member was contained."""
    key = (guild.id, user_id)
    snapshot = _contained_members.pop(key, None)
    if snapshot is None:
        doc = await bot.db.get_containment(guild.id, user_id)
        if not doc:
            return 0
        snapshot = {"role_ids": doc["role_ids"], "reason": doc.get("reason", "")}
    member = guild.get_member(user_id)
    if not member:
        return 0
    roles = [guild.get_role(rid) for rid in snapshot["role_ids"] if guild.get_role(rid)]
    restored = 0
    try:
        if roles:
            await member.add_roles(*roles, reason="Anti-nuke containment lifted")
            restored = len(roles)
        await member.timeout(None, reason="Anti-nuke containment lifted")
    except discord.Forbidden:
        pass
    await bot.db.delete_containment(guild.id, user_id)
    return restored


async def antinuke_punish(guild: discord.Guild, executor: discord.abc.User, reason: str):
    if executor is None or executor.id == bot.user.id:
        return
    if guild.owner_id and executor.id == guild.owner_id:
        return  # only the owner is exempt — staff and admins are NOT

    config = await get_config(guild.id, bot.db)
    member = guild.get_member(executor.id)

    stripped = []
    if member:
        stripped = await antinuke_contain(member, reason)

    alert_id = config.get("log_alerts_channel") or config.get("modlog_channel")
    chan = guild.get_channel(alert_id) if alert_id else None
    if chan:
        embed = discord.Embed(
            title="🚨 ANTI-NUKE TRIGGERED", color=discord.Color.dark_red(),
            description=(
                f"**{executor}** (`{executor.id}`) tripped anti-nuke: **{reason}**.\n\n"
                f"{'🔒 Roles stripped (snapshotted for restore): ' + ', '.join(stripped) if stripped else '⚠️ No roles to strip, or they already left.'}\n"
                f"{'🔇 Contained — timed out 60m. Only this account is affected, nothing server-wide.' if member else '⚠️ Not currently in the server — could not contain.'}\n\n"
                f"**Review immediately.** If this was a false positive, run `{PREFIX}release <user_id>` to restore them exactly as they were."
            ),
        )
        owner_mention = guild.owner.mention if guild.owner else (f"<@{guild.owner_id}>" if guild.owner_id else "")
        try:
            await chan.send(content=owner_mention or None, embed=embed)
        except discord.HTTPException:
            pass
    if member:
        try:
            await member.send(
                f"🚨 Anti-nuke contained your account in **{guild.name}** — your roles were stripped "
                f"(snapshotted, fully restorable) and you were timed out for safety. Reason: {reason}. "
                f"If this was a mistake, contact a Senior Moderator/Admin to get released."
            )
        except discord.Forbidden:
            pass
    logger.warning("ANTI-NUKE: %s tripped '%s' in guild %s", executor, reason, guild.id)


# ── Manual channel lockdown (snapshot-based, fully reversible) ──────────────

_channel_lock_snapshots: dict[int, dict] = {}  # channel_id -> {role_id_or_default: overwrite}


async def lock_channel(channel: discord.abc.GuildChannel, reason: str) -> bool:
    """Snapshots every existing permission overwrite on this channel exactly as
    it is, then denies send_messages/connect for @everyone. `.unlock` restores
    the precise original overwrites afterward — nothing is guessed or reset
    to defaults."""
    if channel.id in _channel_lock_snapshots:
        return False
    snapshot = {key: overwrite for key, overwrite in channel.overwrites.items()}
    _channel_lock_snapshots[channel.id] = snapshot
    default_role = channel.guild.default_role
    new_overwrite = channel.overwrites_for(default_role)
    if isinstance(channel, discord.VoiceChannel):
        new_overwrite.connect = False
    else:
        new_overwrite.send_messages = False
    try:
        await channel.set_permissions(default_role, overwrite=new_overwrite, reason=f"🔒 Locked: {reason}")
    except discord.Forbidden:
        pass
    return True


async def unlock_channel(channel: discord.abc.GuildChannel) -> bool:
    """Restores the exact overwrites a channel had before `lock_channel`."""
    snapshot = _channel_lock_snapshots.pop(channel.id, None)
    if snapshot is None:
        return False
    try:
        for target, overwrite in snapshot.items():
            await channel.set_permissions(target, overwrite=overwrite, reason="🔓 Lock lifted — restored exactly")
        # Remove any overwrite the lock added that wasn't there before
        current_targets = set(channel.overwrites.keys())
        for target in current_targets - set(snapshot.keys()):
            await channel.set_permissions(target, overwrite=None, reason="🔓 Lock lifted — restored exactly")
    except discord.Forbidden:
        pass
    return True


# ── Server-wide lockdown mode (snapshot-based across every text channel) ────
#
# Distinct from `lock_channel`/`unlock_channel` above (which target ONE
# channel). This locks every text channel the bot can see in one shot — used
# for active raids where locking a single channel isn't enough. Still fully
# reversible: every channel's original overwrites are snapshotted exactly as
# they were, restored on `.serverunlock`.

_guild_lockdown_state: dict[int, set[int]] = {}  # guild_id -> set of channel_ids locked by THIS lockdown


async def serverwide_lockdown(guild: discord.Guild, reason: str) -> int:
    """Locks every text channel not already individually locked. Returns the
    number of channels newly locked. Safe to call repeatedly (won't double
    snapshot, won't touch channels already locked via `.lockdown`)."""
    if guild.id in _guild_lockdown_state:
        return 0  # already in server-wide lockdown
    locked_ids: set[int] = set()
    for channel in guild.text_channels:
        ok = await lock_channel(channel, reason)
        if ok:
            locked_ids.add(channel.id)
    _guild_lockdown_state[guild.id] = locked_ids
    return len(locked_ids)


async def serverwide_unlock(guild: discord.Guild) -> int:
    """Restores every channel that THIS server-wide lockdown locked (and only
    those — a channel separately `.lockdown`-ed by staff stays locked)."""
    locked_ids = _guild_lockdown_state.pop(guild.id, None)
    if not locked_ids:
        return 0
    restored = 0
    for channel_id in locked_ids:
        channel = guild.get_channel(channel_id)
        if channel and await unlock_channel(channel):
            restored += 1
    return restored


def is_in_lockdown(guild_id: int) -> bool:
    return guild_id in _guild_lockdown_state


async def antinuke_check(guild: discord.Guild, config_key: str, action_type: str,

                          audit_action: discord.AuditLogAction, target_id: Optional[int],
                          human_reason: str):
    config = await get_config(guild.id, bot.db)
    if not config.get(config_key, True):
        return

    # Real-time targeted response: find exactly who did this (the only piece
    # Discord requires the audit log for) and act on THEM ONLY — nothing
    # server-wide, no other member or channel is ever touched.
    entry = await _antinuke_find_executor(guild, audit_action, target_id)
    if entry is None:
        return
    dedupe_key = (guild.id, entry.id)
    if dedupe_key in _antinuke_recent_targets:
        return
    _antinuke_recent_targets.add(dedupe_key)
    if len(_antinuke_recent_targets) > 500:
        _antinuke_recent_targets.clear()

    count = _antinuke_record(guild.id, entry.user.id, action_type)
    if count >= ANTINUKE_THRESHOLDS[action_type]:
        await antinuke_punish(guild, entry.user, f"{human_reason} ({count} in {ANTINUKE_WINDOW}s)")


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


def _voice_member_eligible(member: discord.Member, channel: discord.VoiceChannel) -> bool:
    """Decides whether a member currently sitting in a voice channel should be
    earning voice XP. Bots never earn it. AFK channel never earns it. If
    configured, self-muted/deafened members and members alone in their
    channel are excluded too — keeps voice XP from being free-farmed by
    sitting muted and alone, or in the AFK channel."""
    if member.bot:
        return False
    if channel.guild.afk_channel and channel.id == channel.guild.afk_channel.id:
        return False
    if VOICE_XP_REQUIRE_UNMUTED and member.voice and (member.voice.self_mute or member.voice.self_deaf):
        return False
    if VOICE_XP_REQUIRE_OTHERS:
        others = [m for m in channel.members if not m.bot and m.id != member.id]
        if not others:
            return False
    return True


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
        self._voice_sessions: dict[tuple[int, int], float] = {}  # (guild_id, user_id) -> monotonic join time

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
        await self.db.seed_counters_from_existing()
        logger.info("Connected to MongoDB.")

        # persistent ticket button views (survive restarts)
        self.add_view(TicketPanelView())
        self.add_view(TicketCloseView())
        self.add_view(GiveawayEndView())

        # seed voice sessions for anyone already in a voice channel when the
        # bot starts/restarts, so they don't lose credit for an ongoing session
        for guild in self.guilds:
            for channel in guild.voice_channels:
                for member in channel.members:
                    if _voice_member_eligible(member, channel):
                        self._voice_sessions[(guild.id, member.id)] = time.monotonic()

        # background loops
        self.tempmute_loop.start()
        self.tempban_loop.start()
        self.giveaway_loop.start()
        self.stats_loop.start()
        self.analytics_loop.start()
        self.ticket_inactivity_loop.start()
        self.voice_xp_loop.start()
        self.staff_inactivity_loop.start()

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
        """Resolves any giveaway whose timer is up. Winner selection always
        goes through `_resolve_giveaway` (weighted by bonus-role entries,
        blacklist-aware) so naturally-ending giveaways behave identically to
        `.gend`/`.greroll` — same weighting, same ended-embed edit with the
        Reroll button attached."""
        try:
            due = await self.db.get_due_giveaways()
            for doc in due:
                await _resolve_giveaway(doc)
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

    @tasks.loop(hours=1)
    async def analytics_loop(self):
        """Records a member-count snapshot once per day per guild (safe to run
        hourly — record_member_count upserts on date, so re-running the same
        day just overwrites with the latest count, which is what you want)."""
        for guild in self.guilds:
            try:
                await self.db.record_member_count(guild.id, guild.member_count)
            except Exception as e:
                logger.error("analytics_loop error for guild %s: %s", guild.id, e)

    @tasks.loop(minutes=30)
    async def ticket_inactivity_loop(self):
        """Auto-closes tickets that have gone quiet. Warns once at
        TICKET_INACTIVITY_WARN_HOURS, then closes (with transcript) at
        TICKET_INACTIVITY_CLOSE_HOURS if still silent."""
        try:
            open_tickets = await self.db.get_open_tickets()
        except Exception as e:
            logger.error("ticket_inactivity_loop fetch error: %s", e)
            return

        now = datetime.now(timezone.utc)
        for doc in open_tickets:
            channel = self.get_channel(doc.get("channel_id"))
            if not channel:
                continue
            last_activity = doc.get("last_activity_at") or doc.get("opened_at")
            if not last_activity:
                continue
            if last_activity.tzinfo is None:
                last_activity = last_activity.replace(tzinfo=timezone.utc)
            idle_hours = (now - last_activity).total_seconds() / 3600

            if idle_hours >= TICKET_INACTIVITY_CLOSE_HOURS:
                try:
                    await channel.send(
                        f"⏳ auto-closing — no activity for over {TICKET_INACTIVITY_CLOSE_HOURS}h"
                    )
                except discord.HTTPException:
                    pass
                try:
                    await _build_ticket_transcript_and_close(channel, self.user, channel.guild, delay=3)
                except Exception as e:
                    logger.error("Auto-close failed for ticket %s: %s", channel.id, e)
                continue

            if idle_hours >= TICKET_INACTIVITY_WARN_HOURS and not doc.get("inactivity_warned_at"):
                try:
                    await channel.send(
                        f"⏳ this ticket's been quiet for {int(idle_hours)}h — it'll close at {TICKET_INACTIVITY_CLOSE_HOURS}h if nobody replies"
                    )
                    await self.db.mark_ticket_warned_inactive(channel.id)
                except discord.HTTPException:
                    pass

    @tasks.loop(hours=1)
    async def staff_inactivity_loop(self):
        """Proactively DMs staff who've gone quiet past the configured
        threshold (default STAFF_INACTIVITY_ALERT_DAYS_DEFAULT days) — the
        active counterpart to `.staffinactive`, which only reports on demand.
        Each staffer is DM'd once per inactive stretch; acting again
        (log_staff_action) clears the flag so they can be re-alerted if they
        go quiet a second time. Off by default toggle is `staffalert_enabled`,
        threshold via `.staffalert [days]`."""
        for guild in self.guilds:
            try:
                config = await get_config(guild.id, self.db)
                if not config.get("staffalert_enabled", True):
                    continue
                days = config.get("staffalert_days", STAFF_INACTIVITY_ALERT_DAYS_DEFAULT)
                cutoff = datetime.now(timezone.utc) - timedelta(days=days)
                stale = await self.db.get_stale_staff_activity(guild.id, cutoff)
                for doc in stale:
                    member = guild.get_member(doc.get("user_id"))
                    if not member:
                        continue
                    last_at = doc.get("last_action_at")
                    idle_days = (datetime.now(timezone.utc) - last_at).days if last_at else days
                    try:
                        await member.send(
                            f"hey — you haven't logged any staff actions in **{guild.name}** for **{idle_days}d** "
                            f"(threshold: {days}d). just a heads up, not a punishment. any mod action resets this."
                        )
                    except discord.Forbidden:
                        pass
                    await self.db.mark_staff_alerted(guild.id, doc.get("user_id"))
            except Exception as e:
                logger.error("staff_inactivity_loop error for guild %s: %s", guild.id, e)

    @tasks.loop(seconds=VOICE_XP_INTERVAL)
    async def voice_xp_loop(self):
        """Awards voice XP to every member currently sitting in an eligible
        voice session. Tracks elapsed time via `_voice_sessions` (set on
        on_voice_state_update) so XP scales with actual time connected even
        if a tick is occasionally late or the bot briefly hiccups."""
        now = time.monotonic()
        for (gid, uid), joined_at in list(self._voice_sessions.items()):
            guild = self.get_guild(gid)
            if not guild:
                continue
            member = guild.get_member(uid)
            if not member or not member.voice or not member.voice.channel:
                self._voice_sessions.pop((gid, uid), None)
                continue
            if not _voice_member_eligible(member, member.voice.channel):
                continue
            elapsed = now - joined_at
            if elapsed < VOICE_XP_INTERVAL:
                continue
            minutes = int(elapsed // 60)
            if minutes < 1:
                continue
            try:
                result = await self.db.add_voice_xp(uid, gid, VOICE_XP_PER_TICK * minutes, minutes)
            except Exception as e:
                logger.error("voice_xp_loop award error for %s in %s: %s", uid, gid, e)
                continue
            self._voice_sessions[(gid, uid)] = now  # reset the clock for this member

            if result["leveled"]:
                reward = await apply_level_roles(member, self.db, result["level"])
                config = await get_config(gid, self.db)
                levelup_channel_id = config.get("levelup_channel")
                chan = guild.get_channel(levelup_channel_id) if levelup_channel_id else None
                if chan:
                    text = f"🎉 {member.mention} hit **level {result['level']}** from voice!"
                    if reward:
                        text += f" earned **{reward}** 🏅"
                    try:
                        await chan.send(text)
                    except discord.HTTPException:
                        pass

            newly = await check_achievements(member, self.db, result)
            for a in newly:
                config = await get_config(gid, self.db)
                levelup_channel_id = config.get("levelup_channel")
                chan = guild.get_channel(levelup_channel_id) if levelup_channel_id else None
                if chan is None and member.voice and member.voice.channel:
                    chan = member.voice.channel
                if chan is None:
                    continue
                try:
                    await chan.send(
                        f"{a['emoji']} {member.mention} unlocked **{a['name']}** — {a['desc']}"
                    )
                except discord.HTTPException:
                    pass

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
TICKET_CLAIM_CUSTOM_ID = "ajscrib:claim_ticket"

TICKET_PRIORITIES = {
    "low":    {"emoji": "🟢", "label": "Low"},
    "medium": {"emoji": "🟡", "label": "Medium"},
    "high":   {"emoji": "🟠", "label": "High"},
    "urgent": {"emoji": "🔴", "label": "Urgent"},
}
TICKET_INACTIVITY_WARN_HOURS  = 24  # warn the opener after this long with no activity
TICKET_INACTIVITY_CLOSE_HOURS = 48  # auto-close after this long with no activity


def _ticket_priority_label(priority: str) -> str:
    p = TICKET_PRIORITIES.get(priority, TICKET_PRIORITIES["medium"])
    return f"{p['emoji']} {p['label']}"


async def _build_ticket_transcript_and_close(channel: discord.abc.GuildChannel, closer: discord.abc.User,
                                               guild: discord.Guild, *, delay: int = 5):
    """Shared close logic: writes a transcript (if configured), then deletes
    the channel after `delay` seconds. Marks the ticket closed in the DB
    first so the auto-close loop never races it."""
    await bot.db.close_ticket(channel.id)
    config = await get_config(guild.id, bot.db)
    transcript_chan_id = config.get("transcript_channel")
    if transcript_chan_id:
        transcript_chan = guild.get_channel(transcript_chan_id)
        if transcript_chan:
            lines = [f"Transcript — #{channel.name} — closed by {closer} ({closer.id})\n" + "=" * 60]
            async for msg in channel.history(limit=500, oldest_first=True):
                ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                content = msg.content or "[embed/attachment]"
                lines.append(f"[{ts}] {msg.author} ({msg.author.id}): {content}")
            buf = io.BytesIO("\n".join(lines).encode("utf-8"))
            try:
                await transcript_chan.send(
                    f"🧾 Transcript for **#{channel.name}** (closed by {closer.mention if hasattr(closer, 'mention') else closer})",
                    file=discord.File(buf, filename=f"transcript-{channel.name}.txt"),
                )
            except discord.HTTPException:
                pass
    await asyncio.sleep(delay)
    try:
        await channel.delete(reason=f"Ticket closed by {closer}")
    except discord.HTTPException:
        pass


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", emoji="🙋", style=discord.ButtonStyle.blurple, custom_id=TICKET_CLAIM_CUSTOM_ID)
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        config = await get_config(interaction.guild.id, bot.db)
        tier = await get_staff_tier(interaction.user, config)
        if tier == TIER_NONE and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("only staff can claim tickets", ephemeral=True)
            return

        doc = await bot.db.get_ticket(interaction.channel.id)
        if doc.get("claimed_by") == interaction.user.id:
            await bot.db.unclaim_ticket(interaction.channel.id)
            await interaction.response.send_message(f"{interaction.user.mention} unclaimed this ticket")
            return

        ok = await bot.db.claim_ticket(interaction.channel.id, interaction.user.id)
        if not ok:
            claimer_id = doc.get("claimed_by")
            await interaction.response.send_message(
                f"already claimed by <@{claimer_id}>", ephemeral=True
            )
            return
        await interaction.response.send_message(f"🙋 {interaction.user.mention} claimed this")

    @discord.ui.select(
        placeholder="Set priority…",
        custom_id="ajscrib:set_priority",
        options=[
            discord.SelectOption(label="Low", value="low", emoji="🟢"),
            discord.SelectOption(label="Medium", value="medium", emoji="🟡", default=True),
            discord.SelectOption(label="High", value="high", emoji="🟠"),
            discord.SelectOption(label="Urgent", value="urgent", emoji="🔴"),
        ],
    )
    async def set_priority(self, interaction: discord.Interaction, select: discord.ui.Select):
        config = await get_config(interaction.guild.id, bot.db)
        tier = await get_staff_tier(interaction.user, config)
        if tier == TIER_NONE and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("only staff can change priority", ephemeral=True)
            return
        priority = select.values[0]
        await bot.db.set_ticket_priority(interaction.channel.id, priority)
        try:
            await interaction.channel.edit(
                topic=f"Priority: {_ticket_priority_label(priority)}"
            )
        except discord.HTTPException:
            pass
        await interaction.response.send_message(
            f"{_ticket_priority_label(priority)} priority set by {interaction.user.mention}."
        )

    @discord.ui.button(label="Close Ticket", emoji="🔒", style=discord.ButtonStyle.red, custom_id=TICKET_CLOSE_CUSTOM_ID)
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        await interaction.response.send_message("🔒 closing in 5...")
        await _build_ticket_transcript_and_close(channel, interaction.user, interaction.guild)


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Ticket", emoji="🎫", style=discord.ButtonStyle.green, custom_id=TICKET_OPEN_CUSTOM_ID)
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        config = await get_config(guild.id, bot.db)

        existing = await bot.db.count_open_tickets(guild.id, interaction.user.id)
        if existing > 0:
            await interaction.response.send_message("you already have an open ticket", ephemeral=True)
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
            topic=f"Priority: {_ticket_priority_label('medium')}",
            reason=f"Ticket opened by {interaction.user}",
        )
        ticket_id = int(time.time())
        await bot.db.save_ticket(guild.id, channel.id, interaction.user.id, ticket_id, priority="medium")

        embed = discord.Embed(
            title="🎫 Ticket Opened",
            description=f"hey {interaction.user.mention}, someone will be with you shortly!",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Priority", value=_ticket_priority_label("medium"), inline=True)
        embed.set_footer(text="Staff: claim, set priority, or close below")
        await channel.send(
            content=support_role.mention if support_role else None,
            embed=embed,
            view=TicketCloseView(),
        )
        await interaction.response.send_message(f"✅ opened {channel.mention}", ephemeral=True)


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
    ("antilink",    "Anti-Link (all URLs)",     "🚫"),
    ("mentionspam", "Mass Mention Spam",        "📣"),
    ("zalgo",       "Zalgo / Unicode Spam",     "👹"),
    ("capsspam",    "Excessive Caps",           "🔠"),
    ("emojispam",   "Emoji Spam",               "🤡"),
    ("scamlinks",   "Scam/Phishing Domains",    "🎣"),
]


def _automod_status_embed(config: dict) -> discord.Embed:
    lines = []
    for key, label, emoji in AUTOMOD_FEATURES:
        on = config.get(f"automod_{key}", True)
        lines.append(f"{emoji} **{label}** — {'🟢 ON' if on else '🔴 OFF'}")
    log_id = config.get("automod_log_channel")
    lines.append(f"\n📋 Log channel: {f'<#{log_id}>' if log_id else '*not set*'}")
    lines.append(
        "\n**Escalation:** strike 3 → 5m mute, +5m each strike after (max 1h). "
        "Never kicks or bans. Strikes decay after 24h."
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

class StaffRolesSelect(discord.ui.Select):
    """Configure the fake-perm staff tiers. None of this touches real Discord
    permissions — it only tells the bot which roles unlock which commands."""
    def __init__(self):
        options = [
            discord.SelectOption(label="Trial Moderator", value="trial", emoji="🟢",
                                  description="warn, mute, purge — basic stuff only"),
            discord.SelectOption(label="Moderator", value="mod", emoji="🔵",
                                  description="+ kick, ban, tempban — advanced stuff"),
            discord.SelectOption(label="Senior Moderator", value="senior", emoji="🟣",
                                  description="+ server config & staff management"),
            discord.SelectOption(label="Partnership Manager", value="partnership", emoji="🤝",
                                  description="May post invite links (multi-select)"),
        ]
        super().__init__(placeholder="🪪 Choose a staff tier to assign a role to...", options=options)

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        mapping = {
            "trial":       ("staffrole_trial", "Trial Moderator role", 1),
            "mod":         ("staffrole_mod", "Moderator role", 1),
            "senior":      ("staffrole_senior", "Senior Moderator role", 1),
            "partnership": (PARTNERSHIP_KEY, "Partnership Manager role(s)", 5),
        }
        key, label, max_values = mapping[value]
        await interaction.response.send_message(
            f"Select the **{label}**:", view=RolePickView(key, label, max_values), ephemeral=True,
        )


class StaffRolesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(StaffRolesSelect())


class LogsSetupSelect(discord.ui.Select):
    """Wires up every log channel the bot can post to."""
    def __init__(self):
        options = [
            discord.SelectOption(label="Mod Log", value="modlog_channel", emoji="📋",
                                  description="Warns, kicks, bans, mutes, cases"),
            discord.SelectOption(label="Message Logs", value="log_messages_channel", emoji="💬",
                                  description="Edited & deleted messages"),
            discord.SelectOption(label="Automod Logs", value="automod_log_channel", emoji="🛡️",
                                  description="Automod deletions, warns, escalation mutes"),
            discord.SelectOption(label="Server Logs", value="log_server_channel", emoji="🏠",
                                  description="Channel/role create, delete, update"),
            discord.SelectOption(label="Entry/Exit Logs", value="log_entryexit_channel", emoji="🚪",
                                  description="Member joins (raid screening included)"),
            discord.SelectOption(label="Invite Tracker", value="invite_log_channel", emoji="📨",
                                  description="Who invited each new member (set text via .setinvitelog)"),
            discord.SelectOption(label="Bot Logs", value="log_bot_channel", emoji="🤖",
                                  description="Command errors & bot status"),
            discord.SelectOption(label="Ticket Transcripts", value="transcript_channel", emoji="🧾",
                                  description="Closed-ticket .txt transcripts"),
            discord.SelectOption(label="Anti-Raid / Anti-Nuke Alerts", value="log_alerts_channel", emoji="🚨",
                                  description="Owner alerts when anti-nuke trips"),
        ]
        super().__init__(placeholder="📚 Choose a log type to point at a channel...", options=options)

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        label = next(o.label for o in self.options if o.value == key)
        await interaction.response.send_message(
            f"Select the channel for **{label}**:", view=ChannelPickView(key, label), ephemeral=True,
        )


class LogsSetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(LogsSetupSelect())


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
            discord.SelectOption(label="Staff Roles", value="staffroles", emoji="🪪",
                                  description="Trial Mod / Mod / Senior Mod / Partnership Manager"),
            discord.SelectOption(label="Logs", value="logs", emoji="📚",
                                  description="Mod, message, automod, server, entry/exit, bot, transcripts"),
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
        elif value == "staffroles":
            embed = discord.Embed(
                title="🪪 Staff Roles", color=discord.Color.blurple(),
                description=(
                    "Pick a Discord role for each tier — no special Discord permissions needed, "
                    "the bot handles access on its own.\n\n"
                    "🟢 **Trial Mod** — `.warn` `.mute` `.purge` `.strikes` `.case`\n"
                    "🔵 **Moderator** — + `.kick` `.ban` `.tempban`\n"
                    "🟣 **Senior Moderator** — + `.clearwarns` `.config` & staff management\n"
                    "🤝 **Partnership Manager** — can post invite links"
                ),
            )
            await interaction.response.send_message(embed=embed, view=StaffRolesView(), ephemeral=True)
        elif value == "logs":
            await interaction.response.send_message(
                "Pick a log type below, then choose its channel:", view=LogsSetupView(), ephemeral=True,
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

async def _send_log(guild: discord.Guild, config: dict, key: str, **kwargs):
    chan_id = config.get(key)
    if not chan_id:
        return
    chan = guild.get_channel(chan_id)
    if not chan:
        return
    try:
        await chan.send(**kwargs)
    except discord.HTTPException:
        pass


@bot.event
async def on_ready():
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="aj's crib")
    )
    for guild in bot.guilds:
        await _refresh_invite_cache(guild)
        config = await get_config(guild.id, bot.db)
        await _send_log(guild, config, "log_bot_channel",
                         embed=discord.Embed(description="🟢 **aj's crib** is online.", color=discord.Color.green()))
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


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    config = await get_config(message.guild.id, bot.db)

    # ── Automod (mod-tier+ staff are trusted; Partnership Manager bypasses invites only) ──
    if not await is_automod_exempt(message.author, config):
        allow_invites = await can_post_invites(message.author, config)
        reason = await run_automod(message, config, allow_invites=allow_invites)
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
            text = f"🎉 {message.author.mention} just hit **level {result['level']}**!"
            if reward:
                text += f" you got **{reward}** 🏅"
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
                    f"{a['emoji']} {message.author.mention} unlocked **{a['name']}** — {a['desc']}"
                )
            except discord.HTTPException:
                pass

    await bot.process_commands(message)

    # Ticket auto-close tracking — cheap channel-name check first so we don't
    # hit the DB for every single message server-wide, only ones that look
    # like they're inside a ticket channel.
    if isinstance(message.channel, discord.TextChannel) and message.channel.name.startswith("ticket-"):
        try:
            await bot.db.touch_ticket_activity(message.channel.id)
        except Exception:
            pass

    # Daily message activity counter (for .analytics activity)
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        await bot.db._client["lxte_assistant"]["daily_msg_counts"].update_one(
            {"guild_id": message.guild.id, "date": today},
            {"$inc": {"count": 1}},
            upsert=True,
        )
    except Exception:
        pass


@bot.event
async def on_message_delete(message: discord.Message):
    if not message.guild or message.author.bot or not message.content:
        return
    config = await get_config(message.guild.id, bot.db)
    embed = discord.Embed(title="🗑️ Message Deleted", color=discord.Color.dark_grey())
    embed.add_field(name="Author", value=f"{message.author.mention} (`{message.author.id}`)", inline=False)
    embed.add_field(name="Channel", value=message.channel.mention, inline=False)
    embed.add_field(name="Content", value=message.content[:1000] or "*(no text content)*", inline=False)
    embed.timestamp = datetime.now(timezone.utc)
    await _send_log(message.guild, config, "log_messages_channel", embed=embed)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if not before.guild or before.author.bot or before.content == after.content:
        return
    config = await get_config(before.guild.id, bot.db)
    embed = discord.Embed(title="✏️ Message Edited", color=discord.Color.blue())
    embed.add_field(name="Author", value=f"{before.author.mention} (`{before.author.id}`)", inline=False)
    embed.add_field(name="Channel", value=before.channel.mention, inline=False)
    embed.add_field(name="Before", value=(before.content[:500] or "*(empty)*"), inline=False)
    embed.add_field(name="After", value=(after.content[:500] or "*(empty)*"), inline=False)
    embed.timestamp = datetime.now(timezone.utc)
    await _send_log(before.guild, config, "log_messages_channel", embed=embed)


_join_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=50))  # guild_id -> join timestamps

JOINRAID_WINDOW       = 10   # seconds — rolling window joins are counted in
JOINRAID_THRESHOLD    = 6    # joins within the window before it's a raid (per-member action)
JOINRAID_LOCKDOWN_THRESHOLD = 12  # joins within the SAME window before it escalates to a full
                                  # server-wide auto-lockdown, not just per-member action
MIN_ACCOUNT_AGE_HOURS_DEFAULT = 0  # configurable per-guild via raidcheck_minage / `.raidage`


async def _check_join_raid(member: discord.Member, config: dict) -> tuple[Optional[str], bool]:
    """Real-time join-rate + new-account screening. Returns (reason, is_severe).
    reason is None if the join is clean. is_severe is True only once the join
    burst crosses JOINRAID_LOCKDOWN_THRESHOLD, signalling the caller should
    escalate to a full server-wide auto-lockdown instead of just acting on
    this one member."""
    if not config.get("raidcheck_enabled", True):
        return None, False

    min_age_hours = config.get("raidcheck_minage_hours", MIN_ACCOUNT_AGE_HOURS_DEFAULT)
    age_hours = (datetime.now(timezone.utc) - member.created_at).total_seconds() / 3600
    if min_age_hours and age_hours < min_age_hours:
        return f"account younger than the configured minimum ({age_hours:.1f}h old, needs {min_age_hours}h)", False

    now = time.monotonic()
    hist = _join_history[member.guild.id]
    hist.append(now)
    recent = sum(1 for t in hist if now - t <= JOINRAID_WINDOW)
    if recent >= JOINRAID_LOCKDOWN_THRESHOLD:
        return f"join-rate burst — {recent} joins in {JOINRAID_WINDOW}s (raid in progress)", True
    if recent >= JOINRAID_THRESHOLD:
        return f"join-rate burst — {recent} joins in {JOINRAID_WINDOW}s (likely raid)", False
    return None, False


async def _dm_raid_flag(member: discord.Member, action: str, reason: str):
    """Best-effort DM explaining why a join was flagged/actioned. Never raises —
    plenty of users have DMs closed, and that's not itself suspicious."""
    if action == "log_only":
        return
    verb = {"kick": "removed you from", "timeout": "temporarily restricted you in"}.get(action, "flagged you in")
    try:
        await member.send(
            f"👋 Heads up — our anti-raid system {verb} **{member.guild.name}**.\n"
            f"Reason: {reason}\n\n"
            f"If this was a mistake, reach out to the server's staff to get it sorted."
        )
    except discord.Forbidden:
        pass


@bot.event
async def on_member_join(member: discord.Member):
    try:
        await bot.refresh_stats_channels(member.guild)
    except Exception as e:
        logger.error("Stats refresh on join failed: %s", e)
    config = await get_config(member.guild.id, bot.db)
    age_days = (datetime.now(timezone.utc) - member.created_at).days

    raid_reason, is_severe = await _check_join_raid(member, config)
    if raid_reason:
        action = config.get("raidcheck_action", "timeout")  # "timeout", "kick", or "log_only"
        if action == "kick":
            try:
                await member.kick(reason=f"Anti-raid: {raid_reason}")
            except discord.Forbidden:
                pass
        elif action == "timeout":
            try:
                await member.timeout(timedelta(hours=1), reason=f"Anti-raid: {raid_reason}")
            except discord.Forbidden:
                pass
        await _dm_raid_flag(member, action, raid_reason)

        alert_id = config.get("log_alerts_channel") or config.get("modlog_channel")
        chan = member.guild.get_channel(alert_id) if alert_id else None
        if chan:
            raid_embed = discord.Embed(
                title="🚨 Anti-Raid Flagged a Join",
                description=f"{member.mention} (`{member.id}`)",
                color=discord.Color.red(),
            )
            raid_embed.add_field(name="Reason", value=raid_reason, inline=False)
            raid_embed.add_field(name="Action taken", value=f"`{action}`", inline=True)
            raid_embed.set_footer(text="Only this account was affected")
            raid_embed.timestamp = datetime.now(timezone.utc)
            try:
                await chan.send(embed=raid_embed)
            except discord.HTTPException:
                pass

        # Severe join burst: escalate beyond the one member and lock the
        # whole server down automatically, if the guild has opted in.
        if is_severe and config.get("raidcheck_autolockdown", True) and not is_in_lockdown(member.guild.id):
            locked_count = await serverwide_lockdown(member.guild, f"auto-triggered: {raid_reason}")
            if chan and locked_count:
                try:
                    await chan.send(embed=discord.Embed(
                        title="🔒🚨 Auto-Lockdown Triggered",
                        description=(
                            f"Join-rate burst crossed the severe threshold — locked **{locked_count}** "
                            f"text channel(s) server-wide.\nRun `{PREFIX}serverunlock` once the raid is handled."
                        ),
                        color=discord.Color.dark_red(),
                    ))
                except discord.HTTPException:
                    pass

    embed = discord.Embed(title="📥 Member Joined", color=discord.Color.green(),
                           description=f"{member.mention} (`{member.id}`)")
    embed.add_field(name="Account Age", value=f"{age_days} day(s) old")
    if age_days < 7:
        embed.add_field(name="⚠️", value="New account — possible raid risk")
    if raid_reason:
        embed.add_field(name="🚨 Anti-raid", value=raid_reason, inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.timestamp = datetime.now(timezone.utc)
    await _send_log(member.guild, config, "log_entryexit_channel", embed=embed)

    # ── Invite tracking — who invited them, stats update, optional announce ──
    used_invite = await _detect_used_invite(member.guild)
    inviter = used_invite.inviter if used_invite and used_invite.inviter else None
    invite_total = None
    if inviter:
        is_fake = age_days < 7
        await bot.db.save_invite(member.guild.id, used_invite.code, inviter.id, used_invite.uses)
        await bot.db.increment_invite_count(member.guild.id, inviter.id, fake=is_fake)
        await bot.db.record_member_inviter(member.guild.id, member.id, inviter.id, used_invite.code)
        invite_total = await bot.db.get_invite_count(member.guild.id, inviter.id)

    invite_tpl = config.get("invite_template")
    if invite_tpl:
        chan_id = config.get("invite_log_channel")
        chan = member.guild.get_channel(chan_id) if chan_id else None
        if chan:
            try:
                invite_embed = _build_invite_embed(invite_tpl, member, member.guild, inviter, invite_total)
                await chan.send(embed=invite_embed)
            except discord.HTTPException:
                pass

    # Welcome message — now sent as an embed (avatar + account age) instead
    # of plain text, but the template variables still work the same way.
    welcome_tpl = config.get("welcome_template")
    if welcome_tpl:
        chan_id = config.get("welcome_channel") or config.get("log_entryexit_channel")
        chan = member.guild.get_channel(chan_id) if chan_id else None
        if chan:
            try:
                welcome_embed = _build_welcome_leave_embed(welcome_tpl, member, member.guild, kind="welcome")
                await chan.send(embed=welcome_embed)
            except discord.HTTPException:
                pass


@bot.event
async def on_member_remove(member: discord.Member):
    try:
        await bot.refresh_stats_channels(member.guild)
    except Exception as e:
        logger.error("Stats refresh on remove failed: %s", e)

    config = await get_config(member.guild.id, bot.db)

    # Was this a kick? Check the audit log so we don't mislabel voluntary leaves.
    # Kicks are still fully logged via post_modlog below — only the generic
    # "member left" announcement is gone, per the no-leave-tracker request.
    entry = await _antinuke_find_executor(member.guild, discord.AuditLogAction.kick, member.id, within_seconds=6)
    if entry:
        await post_modlog(member.guild, "👋 Kick (external)", entry.user,
                           member, entry.reason or "No reason provided", color=discord.Color.orange())
        await antinuke_check(member.guild, "antinuke_bans", "kick", discord.AuditLogAction.kick,
                              member.id, "mass kicking members")

    # Quietly keep invite stats accurate in the background — no announcement,
    # by design (the invite tracker channel only covers joins).
    inviter_id = await bot.db.get_inviter_of_member(member.guild.id, member.id)
    if inviter_id:
        await bot.db.decrement_invite_count(member.guild.id, inviter_id)


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.abc.User):
    config = await get_config(guild.id, bot.db)
    entry = await _antinuke_find_executor(guild, discord.AuditLogAction.ban, user.id, within_seconds=6)
    mod = entry.user if entry else guild.me
    reason = entry.reason if entry else "No reason provided"
    await post_modlog(guild, "🔨 Ban (external)", mod, user, reason or "No reason provided", color=discord.Color.red())
    await antinuke_check(guild, "antinuke_bans", "ban", discord.AuditLogAction.ban, user.id, "mass banning members")


@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel):
    config = await get_config(channel.guild.id, bot.db)
    await _send_log(channel.guild, config, "log_server_channel",
                     embed=discord.Embed(description=f"➕ Channel created: **#{channel.name}**", color=discord.Color.green()))
    await antinuke_check(channel.guild, "antinuke_channels", "channel", discord.AuditLogAction.channel_create,
                          channel.id, "mass channel creation")


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    config = await get_config(channel.guild.id, bot.db)
    await _send_log(channel.guild, config, "log_server_channel",
                     embed=discord.Embed(description=f"➖ Channel deleted: **#{channel.name}**", color=discord.Color.red()))
    await antinuke_check(channel.guild, "antinuke_channels", "channel", discord.AuditLogAction.channel_delete,
                          channel.id, "mass channel deletion")


@bot.event
async def on_guild_role_create(role: discord.Role):
    config = await get_config(role.guild.id, bot.db)
    await _send_log(role.guild, config, "log_server_channel",
                     embed=discord.Embed(description=f"➕ Role created: **{role.name}**", color=discord.Color.green()))
    await antinuke_check(role.guild, "antinuke_roles", "role", discord.AuditLogAction.role_create,
                          role.id, "mass role creation")


@bot.event
async def on_guild_role_delete(role: discord.Role):
    config = await get_config(role.guild.id, bot.db)
    await _send_log(role.guild, config, "log_server_channel",
                     embed=discord.Embed(description=f"➖ Role deleted: **{role.name}**", color=discord.Color.red()))
    await antinuke_check(role.guild, "antinuke_roles", "role", discord.AuditLogAction.role_delete,
                          role.id, "mass role deletion")


@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role):
    if not before.permissions.administrator and after.permissions.administrator:
        config = await get_config(after.guild.id, bot.db)
        await _send_log(after.guild, config, "log_server_channel",
                         embed=discord.Embed(description=f"🚨 Role **{after.name}** was granted **Administrator**!",
                                              color=discord.Color.dark_red()))
        await antinuke_check(after.guild, "antinuke_permgrant", "permgrant", discord.AuditLogAction.role_update,
                              after.id, f"granting Administrator to role '{after.name}'")


@bot.event
async def on_webhooks_update(channel: discord.abc.GuildChannel):
    await antinuke_check(channel.guild, "antinuke_webhooks", "webhook", discord.AuditLogAction.webhook_create,
                          None, "rapid webhook creation (raid-tool signature)")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Starts/stops a voice-XP session as members join, leave, or move
    between voice channels. The actual XP award happens on a timer in
    `voice_xp_loop` — this just tracks *when* an eligible session started so
    that loop knows how much time to credit."""
    if member.bot:
        return
    key = (member.guild.id, member.id)

    if after.channel is None:
        # Left voice entirely — credit whatever partial time accrued, then
        # drop the session so the loop stops counting it.
        joined_at = bot._voice_sessions.pop(key, None)
        if joined_at is not None:
            elapsed_minutes = int((time.monotonic() - joined_at) // 60)
            if elapsed_minutes >= 1:
                try:
                    await bot.db.add_voice_xp(member.id, member.guild.id,
                                               VOICE_XP_PER_TICK * elapsed_minutes, elapsed_minutes)
                except Exception as e:
                    logger.error("Voice XP credit-on-leave failed for %s: %s", member.id, e)
        return

    # Joined or moved channels — (re)start the session clock if eligible.
    if _voice_member_eligible(member, after.channel):
        bot._voice_sessions.setdefault(key, time.monotonic())
    else:
        bot._voice_sessions.pop(key, None)

    # Also re-check whoever ELSE is in the before/after channels, since
    # "alone in channel" eligibility can flip for the people already there.
    for chan in {before.channel, after.channel}:
        if chan is None:
            continue
        for other in chan.members:
            if other.bot or other.id == member.id:
                continue
            other_key = (chan.guild.id, other.id)
            if _voice_member_eligible(other, chan):
                bot._voice_sessions.setdefault(other_key, time.monotonic())
            else:
                bot._voice_sessions.pop(other_key, None)


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

    newly_admin_roles = [r for r in after.roles if r not in before.roles and r.permissions.administrator]
    if newly_admin_roles:
        await antinuke_check(after.guild, "antinuke_permgrant", "permgrant", discord.AuditLogAction.member_role_update,
                              after.id, f"granting an admin role to {after}")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ Slow down — try that again in `{error.retry_after:.1f}s`.", delete_after=5)
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠️ Missing argument: `{error.param.name}`. Check `{PREFIX}help`.")
        return
    if isinstance(error, (commands.MissingPermissions, commands.CheckFailure)):
        return  # staff_tier_check() and friends already message the user themselves
    if isinstance(error, commands.BotMissingPermissions):
        await ctx.send("⛔ I'm missing the permissions needed to do that.")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send(f"⚠️ Bad argument: {error}")
        return
    logger.exception("Unhandled command error in %s: %s", ctx.command, error)
    await ctx.send("❌ Something went wrong running that command.")
    if ctx.guild:
        config = await get_config(ctx.guild.id, bot.db)
        await _send_log(ctx.guild, config, "log_bot_channel",
                         embed=discord.Embed(title="❌ Command Error", color=discord.Color.red(),
                                              description=f"`{ctx.command}` by {ctx.author.mention}: ```{error}```"))


# ═══════════════════════════════════════════════════════════════════════════════
#  COMMANDS — general / utility
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="ping")
async def ping_cmd(ctx: commands.Context):
    await ctx.send(f"🏓 Pong! `{round(bot.latency * 1000)}ms`")


@bot.command(name="help")
async def help_cmd(ctx: commands.Context, category: str = None):
    categories = {
        "leveling": {
            "emoji": "⭐",
            "title": "Leveling",
            "value": (
                f"`{PREFIX}rank [@user]` — your level + XP\n"
                f"`{PREFIX}leaderboard` — top 10\n"
                f"`{PREFIX}top [xp|voice|messages|invites]` — pick a leaderboard\n"
                f"`{PREFIX}achievements [@user]` — badges\n"
                f"XP from chatting, voice time, and daily streaks"
            ),
        },
        "mod": {
            "emoji": "🔨",
            "title": "Moderation",
            "value": (
                f"`{PREFIX}warn` `{PREFIX}warnings` `{PREFIX}clearwarns` @user\n"
                f"`{PREFIX}kick` `{PREFIX}ban` `{PREFIX}tempban @user 1d`\n"
                f"`{PREFIX}mute @user 10m` `{PREFIX}unmute`\n"
                f"`{PREFIX}case <#>` `{PREFIX}cases @user`\n"
                f"`{PREFIX}purge <1-100>`\n"
                f"`{PREFIX}lockdown [#ch]` `{PREFIX}unlock [#ch]` *(Senior+)*\n"
                f"`{PREFIX}release <user_id>` — undo anti-nuke *(Senior+)*"
            ),
        },
        "automod": {
            "emoji": "🛡️",
            "title": "Automod",
            "value": (
                f"Auto-deletes spam, invites, links, mass mentions, zalgo, caps, scam domains\n"
                f"Always warns first — mutes only after repeat warns (5m → +5m each time, max 1h)\n"
                f"`{PREFIX}strikes` `{PREFIX}clearstrikes` @user\n"
                f"Configure: `{PREFIX}quicksetup`"
            ),
        },
        "raid": {
            "emoji": "🚨",
            "title": "Anti-Raid / Anti-Nuke",
            "value": (
                f"**Joins:** screens account age + join speed, then timeout/kick/log *(configurable)*\n"
                f"`{PREFIX}raidage [hours]` — min account age *(Senior+)*\n"
                f"**Lockdown:** `{PREFIX}serverlockdown` / `{PREFIX}serverunlock` *(Senior+)*\n"
                f"`{PREFIX}lockdown [#ch]` / `{PREFIX}unlock [#ch]` — one channel *(Senior+)*\n"
                f"**Nuke protection:** mass deletes, ban/kick sprees, webhook spam, admin grants → "
                f"instantly strips + mutes the culprit, alerts owner. Nobody is exempt but the owner."
            ),
        },
        "tickets": {
            "emoji": "🎫",
            "title": "Tickets",
            "value": (
                f"Members open one via the panel button\n"
                f"Staff: **Claim** button or `{PREFIX}claim` · priority via dropdown or `{PREFIX}priority low|medium|high|urgent`\n"
                f"`{PREFIX}tickets` — list open\n"
                f"`{PREFIX}ticketstats` *(Mod+)*\n"
                f"Auto-closes after 48h idle (warns at 24h)"
            ),
        },
        "giveaways": {
            "emoji": "🎉",
            "title": "Giveaways",
            "value": (
                f"`{PREFIX}gstart <duration> <winners> <prize>` — react 🎉 to enter\n"
                f"`{PREFIX}gend <message_id>`\n"
                f"`{PREFIX}greroll <message_id> [count]`\n"
                f"`{PREFIX}gbonus @role <amount>` *(Mod+)*\n"
                f"`{PREFIX}gblacklist add|remove @user/@role` *(Mod+)*"
            ),
        },
        "staff": {
            "emoji": "🪪",
            "title": "Staff Tools",
            "value": (
                f"`{PREFIX}staffactivity [@user]` — action counts\n"
                f"`{PREFIX}staffinactive [days]` — who's gone quiet *(Senior+)*\n"
                f"`{PREFIX}staffalert [days|on|off]` — auto-DM inactive staff *(Senior+)*\n"
                f"Tiers: 🟢 Trial · 🔵 Mod · 🟣 Senior — set via `{PREFIX}quicksetup`"
            ),
        },
        "analytics": {
            "emoji": "📊",
            "title": "Analytics",
            "value": (
                f"`{PREFIX}analytics growth [days]`\n"
                f"`{PREFIX}analytics activity [days]`\n"
                f"`{PREFIX}analytics joins [days]`\n"
                f"Up to 90 days of history"
            ),
        },
        "setup": {
            "emoji": "⚙️",
            "title": "Setup & Config",
            "value": (
                f"`{PREFIX}quicksetup` — counter channels + config hub\n"
                f"`{PREFIX}setwelcome` / `{PREFIX}setinvitelog <template>` — `{{user}} {{server}} {{count}}`\n"
                f"`{PREFIX}testwelcome` / `{PREFIX}testinvitelog` — preview\n"
                f"`{PREFIX}config [key] [value]` *(Admin)*\n"
                f"`{PREFIX}ping`"
            ),
        },
    }


    if category and category.lower() in categories:
        cat = categories[category.lower()]
        embed = discord.Embed(
            title=f"{cat['emoji']} {cat['title']}",
            description=cat["value"],
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Prefix: {PREFIX}  •  .help for all categories")
        await ctx.send(embed=embed)
        return

    embed = discord.Embed(
        title="aj's crib",
        color=discord.Color.blurple(),
        description=f"Prefix: `{PREFIX}` — pick a category below or run `.help <category>`",
    )
    for key, cat in categories.items():
        embed.add_field(name=f"{cat['emoji']} {cat['title']}", value=f"`.help {key}`", inline=True)
    embed.set_footer(text="Built for aj's crib • not AI slop")

    class HelpView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
        @discord.ui.button(label="Leveling ⭐", style=discord.ButtonStyle.secondary, row=0)
        async def lvl(self, i, b): await _help_cat(i, "leveling", categories)
        @discord.ui.button(label="Moderation 🔨", style=discord.ButtonStyle.secondary, row=0)
        async def mod(self, i, b): await _help_cat(i, "mod", categories)
        @discord.ui.button(label="Automod 🛡️", style=discord.ButtonStyle.secondary, row=0)
        async def am(self, i, b): await _help_cat(i, "automod", categories)
        @discord.ui.button(label="Raid 🚨", style=discord.ButtonStyle.secondary, row=0)
        async def raid(self, i, b): await _help_cat(i, "raid", categories)
        @discord.ui.button(label="Tickets 🎫", style=discord.ButtonStyle.secondary, row=1)
        async def tix(self, i, b): await _help_cat(i, "tickets", categories)
        @discord.ui.button(label="Giveaways 🎉", style=discord.ButtonStyle.secondary, row=1)
        async def gw(self, i, b): await _help_cat(i, "giveaways", categories)
        @discord.ui.button(label="Staff 🪪", style=discord.ButtonStyle.secondary, row=1)
        async def staff(self, i, b): await _help_cat(i, "staff", categories)
        @discord.ui.button(label="Analytics 📊", style=discord.ButtonStyle.secondary, row=2)
        async def anal(self, i, b): await _help_cat(i, "analytics", categories)
        @discord.ui.button(label="Setup ⚙️", style=discord.ButtonStyle.secondary, row=2)
        async def setup(self, i, b): await _help_cat(i, "setup", categories)

    await ctx.send(embed=embed, view=HelpView())


async def _help_cat(interaction: discord.Interaction, key: str, categories: dict):
    cat = categories[key]
    embed = discord.Embed(title=f"{cat['emoji']} {cat['title']}", description=cat["value"], color=discord.Color.blurple())
    embed.set_footer(text=f"Prefix: {PREFIX}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


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
    voice_minutes = data.get("voice_minutes", 0)
    embed = discord.Embed(title=f"{member.display_name}'s Rank", color=discord.Color.gold())
    embed.add_field(name="Level", value=str(level), inline=True)
    embed.add_field(name="Messages", value=str(data.get("messages", 0)), inline=True)
    embed.add_field(name="Streak", value=f"{data.get('streak', 0)} days", inline=True)
    embed.add_field(name="Voice Time", value=f"{voice_minutes // 60}h {voice_minutes % 60}m", inline=True)
    embed.add_field(name="Progress", value=f"`{bar}` {xp_in}/{xp_need} XP", inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name="achievements", aliases=["badges"])
async def achievements_cmd(ctx: commands.Context, member: discord.Member = None):
    """Show a member's unlocked vs locked achievement badges. This command was
    referenced in .help but never actually wired up — fixed."""
    member = member or ctx.author
    data = await bot.db.get_level_data(member.id, ctx.guild.id)
    unlocked = set((data or {}).get("badges", []))
    lines = []
    for a in ACHIEVEMENTS:
        mark = "✅" if a["id"] in unlocked else "🔒"
        lines.append(f"{mark} {a['emoji']} **{a['name']}** — {a['desc']}")
    embed = discord.Embed(
        title=f"🏅 {member.display_name}'s Achievements",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"{len(unlocked)}/{len(ACHIEVEMENTS)} unlocked")
    embed.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name="leaderboard", aliases=["lb"])
async def leaderboard_cmd(ctx: commands.Context):
    """The classic single-stat XP leaderboard. For voice/messages/invites all
    in one place, use `.top`."""
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
    embed.set_footer(text=f"Use {PREFIX}top for voice/message/invite leaderboards too")
    await ctx.send(embed=embed)


def _format_voice_duration(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m" if hours else f"{mins}m"


class TopLeaderboardView(discord.ui.View):
    """One unified `.top` command with buttons to flip between XP, voice,
    messages, and invites — no need to remember four separate command names."""

    def __init__(self, ctx: commands.Context):
        super().__init__(timeout=60)
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.ctx.author.id

    async def _build_embed(self, category: str) -> discord.Embed:
        guild = self.ctx.guild
        if category == "xp":
            rows = await bot.db.get_leaderboard(guild.id, limit=10)
            lines = []
            for i, row in enumerate(rows, start=1):
                member = guild.get_member(row["user_id"])
                name = member.display_name if member else f"User {row['user_id']}"
                level = calculate_level(row.get("total_xp", 0))[0]
                lines.append(f"**#{i}** {name} — Level {level} ({row.get('total_xp', 0)} XP)")
            title, color = "🏆 Top — XP / Level", discord.Color.gold()
        elif category == "voice":
            rows = await bot.db.get_voice_leaderboard(guild.id, limit=10)
            lines = []
            for i, row in enumerate(rows, start=1):
                member = guild.get_member(row["user_id"])
                name = member.display_name if member else f"User {row['user_id']}"
                lines.append(f"**#{i}** {name} — {_format_voice_duration(row.get('voice_minutes', 0))}")
            title, color = "🔊 Top — Voice Time", discord.Color.blurple()
        elif category == "messages":
            rows = await bot.db.get_msg_leaderboard(guild.id, limit=10)
            lines = []
            for i, row in enumerate(rows, start=1):
                member = guild.get_member(row["user_id"])
                name = member.display_name if member else f"User {row['user_id']}"
                lines.append(f"**#{i}** {name} — {row.get('total_messages', 0)} messages")
            title, color = "💬 Top — Messages", discord.Color.green()
        else:  # invites
            rows = await bot.db.get_invite_leaderboard(guild.id, limit=10)
            lines = []
            for i, row in enumerate(rows, start=1):
                member = guild.get_member(row["inviter_id"])
                name = member.display_name if member else f"User {row['inviter_id']}"
                lines.append(f"**#{i}** {name} — {row.get('total_invites', 0)} invites")
            title, color = "📨 Top — Invites", discord.Color.purple()

        embed = discord.Embed(title=title, description="\n".join(lines) or "No data yet.", color=color)
        embed.set_footer(text=f"Requested by {self.ctx.author}")
        return embed

    async def _switch(self, interaction: discord.Interaction, category: str):
        embed = await self._build_embed(category)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="XP", emoji="🏆", style=discord.ButtonStyle.blurple)
    async def xp_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._switch(interaction, "xp")

    @discord.ui.button(label="Voice", emoji="🔊", style=discord.ButtonStyle.secondary)
    async def voice_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._switch(interaction, "voice")

    @discord.ui.button(label="Messages", emoji="💬", style=discord.ButtonStyle.secondary)
    async def messages_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._switch(interaction, "messages")

    @discord.ui.button(label="Invites", emoji="📨", style=discord.ButtonStyle.secondary)
    async def invites_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._switch(interaction, "invites")


@bot.command(name="top")
async def top_cmd(ctx: commands.Context, category: str = "xp"):
    """.top [xp|voice|messages|invites] — one leaderboard command with
    buttons to flip between XP/level, voice time, messages, and invites."""
    category = category.lower()
    aliases = {"level": "xp", "lvl": "xp", "msg": "messages", "msgs": "messages",
               "invite": "invites", "vc": "voice"}
    category = aliases.get(category, category)
    if category not in ("xp", "voice", "messages", "invites"):
        category = "xp"

    view = TopLeaderboardView(ctx)
    embed = await view._build_embed(category)
    await ctx.send(embed=embed, view=view)


# ═══════════════════════════════════════════════════════════════════════════════
#  COMMANDS — moderation / case system
# ═══════════════════════════════════════════════════════════════════════════════

async def post_modlog(guild: discord.Guild, action: str, mod: discord.Member, target, reason: str,
                       case_num: Optional[int] = None, color: discord.Color = discord.Color.orange()):
    config = await get_config(guild.id, bot.db)
    chan_id = config.get("modlog_channel")
    if not chan_id:
        return
    chan = guild.get_channel(chan_id)
    if not chan:
        return
    embed = discord.Embed(title=f"{action}" + (f" — Case #{case_num}" if case_num else ""), color=color)
    embed.add_field(name="Target", value=f"{target.mention} (`{target.id}`)", inline=False)
    embed.add_field(name="Moderator", value=mod.mention, inline=False)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.timestamp = datetime.now(timezone.utc)
    try:
        await chan.send(embed=embed)
    except discord.HTTPException:
        pass

@bot.command(name="staffactivity", aliases=["stafflog", "modactivity"])
@staff_tier_check(TIER_SENIOR)
async def staffactivity_cmd(ctx: commands.Context, member: discord.Member = None):
    """Show mod action counts across all staff, or a detailed breakdown for one person."""
    if member:
        doc = await bot.db.get_staff_activity_user(ctx.guild.id, member.id)
        if not doc:
            await ctx.send(f"No recorded actions for {member.mention} yet.")
            return
        actions = doc.get("actions", {})
        last_at = doc.get("last_action_at")
        last_str = f"<t:{int(last_at.timestamp())}:R>" if last_at else "never"
        lines = [f"**{k}**: {v}" for k, v in actions.items() if k != "total"]
        embed = discord.Embed(
            title=f"📋 {member.display_name}'s Staff Activity",
            color=discord.Color.blurple(),
            description="\n".join(lines) or "No actions logged.",
        )
        embed.add_field(name="Total", value=str(actions.get("total", 0)), inline=True)
        embed.add_field(name="Last action", value=last_str, inline=True)
        await ctx.send(embed=embed)
        return

    rows = await bot.db.get_staff_activity(ctx.guild.id)
    if not rows:
        await ctx.send("No staff activity recorded yet.")
        return
    lines = []
    for row in rows:
        m = ctx.guild.get_member(row["user_id"])
        name = m.display_name if m else f"<@{row['user_id']}>"
        total = row.get("actions", {}).get("total", 0)
        last_at = row.get("last_action_at")
        last_str = f"<t:{int(last_at.timestamp())}:R>" if last_at else "—"
        lines.append(f"**{name}** — {total} action(s), last {last_str}")
    embed = discord.Embed(
        title="📋 Staff Activity Audit Trail",
        color=discord.Color.blurple(),
        description="\n".join(lines),
    )
    embed.set_footer(text="Run .staffactivity @user for a per-action breakdown")
    await ctx.send(embed=embed)


@bot.command(name="staffinactive", aliases=["inactivestaff"])
@staff_tier_check(TIER_SENIOR)
async def staffinactive_cmd(ctx: commands.Context, days: int = 7):
    """.staffinactive [days] — flags every staff-tier role holder with ZERO
    logged actions in the last `days` (default 7). This includes staff who
    have never logged a single action at all, not just ones who went quiet —
    `get_staff_activity` only has docs for people who've acted at least once,
    so this cross-references against everyone currently holding a staff role."""
    days = max(1, days)
    config = await get_config(ctx.guild.id, bot.db)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    all_staff_role_ids: set[int] = set()
    for key in STAFF_TIER_KEYS.values():
        all_staff_role_ids |= set(config.get(key, []) or [])
    if not all_staff_role_ids:
        await ctx.send("⚠️ No staff roles configured yet. Set them up via the setup wizard first.")
        return

    staff_members = {m for m in ctx.guild.members if not m.bot and {r.id for r in m.roles} & all_staff_role_ids}
    if not staff_members:
        await ctx.send("No staff members currently hold a configured staff role.")
        return

    inactive_lines = []
    for member in sorted(staff_members, key=lambda m: m.display_name.lower()):
        doc = await bot.db.get_staff_activity_user(ctx.guild.id, member.id)
        last_at = doc.get("last_action_at") if doc else None
        if last_at and last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=timezone.utc)

        if not doc or not last_at:
            inactive_lines.append(f"⚠️ {member.mention} — **never logged an action**")
        elif last_at < cutoff:
            idle_days = (datetime.now(timezone.utc) - last_at).days
            inactive_lines.append(f"⚠️ {member.mention} — last action **{idle_days}d ago**")

    if not inactive_lines:
        await ctx.send(f"✅ Every staff member has logged an action within the last **{days}d**.")
        return

    embed = discord.Embed(
        title=f"🕵️ Inactive Staff (0 actions in {days}d)",
        description="\n".join(inactive_lines),
        color=discord.Color.orange(),
    )
    embed.set_footer(text=f"{len(inactive_lines)} of {len(staff_members)} staff flagged")
    await ctx.send(embed=embed)


@bot.command(name="staffalert")
@staff_tier_check(TIER_SENIOR)
async def staffalert_cmd(ctx: commands.Context, setting: str = None):
    """.staffalert [days|on|off] — view or configure the PROACTIVE staff-
    inactivity DM alert (runs hourly in the background, separate from the
    on-demand `.staffinactive` report). `.staffalert 10` sets the threshold
    to 10 days, `.staffalert off` disables it, `.staffalert on` re-enables it
    without changing the threshold."""
    config = await get_config(ctx.guild.id, bot.db)
    if setting is None:
        enabled = config.get("staffalert_enabled", True)
        days = config.get("staffalert_days", STAFF_INACTIVITY_ALERT_DAYS_DEFAULT)
        await ctx.send(
            f"🕵️ Staff inactivity DM alerts: **{'ON' if enabled else 'OFF'}** "
            f"— threshold **{days}d** of zero logged actions."
        )
        return
    setting = setting.lower()
    if setting in ("off", "disable", "disabled"):
        await bot.db.update_config(ctx.guild.id, "staffalert_enabled", False)
        await ctx.send("✅ Staff inactivity DM alerts **disabled**.")
        return
    if setting in ("on", "enable", "enabled"):
        await bot.db.update_config(ctx.guild.id, "staffalert_enabled", True)
        await ctx.send("✅ Staff inactivity DM alerts **enabled**.")
        return
    try:
        days = max(1, int(setting))
    except ValueError:
        await ctx.send("⚠️ Usage: `.staffalert [days]`, `.staffalert on`, or `.staffalert off`.")
        return
    await bot.db.update_config(ctx.guild.id, "staffalert_days", days)
    await bot.db.update_config(ctx.guild.id, "staffalert_enabled", True)
    await ctx.send(f"✅ Staff will now be DM'd automatically after **{days}d** of zero logged actions.")


@bot.command(name="warn")
@staff_tier_check(TIER_TRIAL)
@commands.cooldown(3, 10, commands.BucketType.user)
async def warn_cmd(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    if not await validate_mod_target(ctx, member):
        return
    count = await bot.db.add_warn(ctx.guild.id, member.id, ctx.author.id, reason, source="manual")
    case_num = await bot.db.add_case(ctx.guild.id, "warn", ctx.author.id, member.id, reason)
    await bot.db.log_staff_action(ctx.guild.id, ctx.author.id, "warn", member.id)
    await ctx.send(f"⚠️ warned {member.mention} (warn #{count}, case #{case_num}) — {reason}")
    await post_modlog(ctx.guild, "⚠️ Warn", ctx.author, member, reason, case_num, discord.Color.yellow())
    try:
        await member.send(f"you got warned in **{ctx.guild.name}** — {reason}")
    except discord.Forbidden:
        pass


@bot.command(name="warnings")
@staff_tier_check(TIER_TRIAL)
async def warnings_cmd(ctx: commands.Context, member: discord.Member):
    warns = await bot.db.get_warns(ctx.guild.id, member.id)
    if not warns:
        await ctx.send(f"{member.mention} has no warnings.")
        return
    lines = []
    for w in warns:
        tag = "🤖 automod" if w.get("source") == "automod" else f"by <@{w['mod_id']}>"
        lines.append(f"`{w['created_at']:%Y-%m-%d}` — {w['reason']} ({tag})")
    embed = discord.Embed(title=f"Warnings for {member.display_name}", description="\n".join(lines))
    await ctx.send(embed=embed)


@bot.command(name="clearwarns")
@staff_tier_check(TIER_MOD)
async def clearwarns_cmd(ctx: commands.Context, member: discord.Member):
    n = await bot.db.clear_warns(ctx.guild.id, member.id)
    await ctx.send(f"cleared {n} warning(s) for {member.mention}")


@bot.command(name="case")
@staff_tier_check(TIER_TRIAL)
async def case_cmd(ctx: commands.Context, number: int):
    case = await bot.db.get_case(ctx.guild.id, number)
    if not case:
        await ctx.send(f"No case #{number} found.")
        return
    embed = discord.Embed(title=f"Case #{number}", color=discord.Color.orange())
    embed.add_field(name="Action", value=case["action"])
    embed.add_field(name="Target", value=f"<@{case['target_id']}>")
    embed.add_field(name="Moderator", value=f"<@{case['mod_id']}>")
    embed.add_field(name="Reason", value=case["reason"], inline=False)
    await ctx.send(embed=embed)


@bot.command(name="cases")
@staff_tier_check(TIER_TRIAL)
async def cases_cmd(ctx: commands.Context, member: discord.Member):
    cases = await bot.db.get_user_cases(ctx.guild.id, member.id)
    if not cases:
        await ctx.send(f"{member.mention} has no cases.")
        return
    lines = [f"`#{c['case_number']}` {c['action']} — {c['reason']}" for c in cases]
    embed = discord.Embed(title=f"Cases for {member.display_name}", description="\n".join(lines))
    await ctx.send(embed=embed)


@bot.command(name="kick")
@staff_tier_check(TIER_MOD)
@commands.bot_has_permissions(kick_members=True)
@commands.cooldown(2, 10, commands.BucketType.user)
async def kick_cmd(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    if not await validate_mod_target(ctx, member):
        return
    try:
        await member.kick(reason=reason)
    except discord.Forbidden:
        await ctx.send("can't kick that person — their role might be above mine")
        return
    case_num = await bot.db.add_case(ctx.guild.id, "kick", ctx.author.id, member.id, reason)
    await bot.db.log_staff_action(ctx.guild.id, ctx.author.id, "kick", member.id)
    await ctx.send(f"👋 kicked {member.mention} — {reason}")
    await post_modlog(ctx.guild, "👋 Kick", ctx.author, member, reason, case_num, discord.Color.orange())


@bot.command(name="ban")
@staff_tier_check(TIER_MOD)
@commands.bot_has_permissions(ban_members=True)
@commands.cooldown(2, 10, commands.BucketType.user)
async def ban_cmd(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    if not await validate_mod_target(ctx, member):
        return
    try:
        await member.ban(reason=reason)
    except discord.Forbidden:
        await ctx.send("can't ban that person — their role might be above mine")
        return
    case_num = await bot.db.add_case(ctx.guild.id, "ban", ctx.author.id, member.id, reason)
    await bot.db.log_staff_action(ctx.guild.id, ctx.author.id, "ban", member.id)
    await ctx.send(f"🔨 banned {member.mention} — {reason}")
    await post_modlog(ctx.guild, "🔨 Ban", ctx.author, member, reason, case_num, discord.Color.red())


def _parse_duration(s: str) -> Optional[timedelta]:
    """Parses things like 10m, 1h, 1d, 1w into a timedelta. Returns None for
    anything invalid — including zero, negative, or absurdly large amounts,
    all of which used to slip through: a negative duration (e.g. "-5m") used
    to silently produce a negative timedelta, which for `.tempban` meant
    unban_at landed in the past and the tempban loop undid the ban on its very
    next tick; an extreme amount (e.g. "999999999999d") used to crash with an
    uncaught OverflowError instead of a clean "invalid duration" reply."""
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    try:
        unit = s[-1].lower()
        amount = int(s[:-1])
        if unit not in units or amount <= 0:
            return None
        delta = timedelta(**{units[unit]: amount})
    except (ValueError, IndexError, OverflowError):
        return None
    if delta > timedelta(days=365 * 5):  # 5 years — generous, but bounded
        return None
    return delta


@bot.command(name="purge")
@staff_tier_check(TIER_TRIAL)
@commands.bot_has_permissions(manage_messages=True)
async def purge_cmd(ctx: commands.Context, amount: int):
    """Bulk-delete the last `amount` messages (1-100) in this channel. Basic trial-mod tool."""
    amount = max(1, min(amount, 100))
    deleted = await ctx.channel.purge(limit=amount + 1)  # +1 to include the command message itself
    config = await get_config(ctx.guild.id, bot.db)
    await _send_log(ctx.guild, config, "log_mod_extra_channel",
                     embed=discord.Embed(description=f"🧹 {ctx.author.mention} purged **{len(deleted) - 1}** message(s) in {ctx.channel.mention}",
                                          color=discord.Color.dark_grey()))
    await ctx.send(f"🧹 purged {len(deleted) - 1} message(s)", delete_after=5)


@bot.command(name="mute")
@staff_tier_check(TIER_TRIAL)
@commands.bot_has_permissions(moderate_members=True)
@commands.cooldown(3, 15, commands.BucketType.user)
async def mute_cmd(ctx: commands.Context, member: discord.Member, duration: str = "10m", *, reason: str = "No reason provided"):
    if not await validate_mod_target(ctx, member):
        return
    delta = _parse_duration(duration)
    if not delta:
        await ctx.send("invalid duration, use something like `10m`, `1h`, `1d`")
        return
    if delta > timedelta(days=28):
        await ctx.send("discord's timeout max is 28 days — use `.tempban` for longer")
        return
    unmute_at = datetime.now(timezone.utc) + delta
    try:
        await member.timeout(delta, reason=reason)
    except discord.Forbidden:
        await ctx.send("i don't have permission to timeout that person")
        return
    await bot.db.add_tempmute(ctx.guild.id, member.id, ctx.author.id, reason, unmute_at)
    case_num = await bot.db.add_case(ctx.guild.id, "mute", ctx.author.id, member.id, reason)
    await bot.db.log_staff_action(ctx.guild.id, ctx.author.id, "mute", member.id)
    await ctx.send(f"🔇 muted {member.mention} for `{duration}` — {reason}")
    await post_modlog(ctx.guild, f"🔇 Mute ({duration})", ctx.author, member, reason, case_num, discord.Color.dark_orange())


@bot.command(name="unmute")
@staff_tier_check(TIER_TRIAL)
@commands.bot_has_permissions(moderate_members=True)
async def unmute_cmd(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    try:
        await member.timeout(None, reason=reason)
    except discord.Forbidden:
        await ctx.send("i don't have permission to lift that timeout")
        return
    await bot.db.remove_tempmute(ctx.guild.id, member.id)
    case_num = await bot.db.add_case(ctx.guild.id, "unmute", ctx.author.id, member.id, reason)
    await ctx.send(f"🔊 unmuted {member.mention} — {reason}")
    await post_modlog(ctx.guild, "🔊 Unmute", ctx.author, member, reason, case_num, discord.Color.green())


@bot.command(name="tempban")
@staff_tier_check(TIER_MOD)
@commands.bot_has_permissions(ban_members=True)
@commands.cooldown(2, 15, commands.BucketType.user)
async def tempban_cmd(ctx: commands.Context, member: discord.Member, duration: str, *, reason: str = "No reason provided"):
    if not await validate_mod_target(ctx, member):
        return
    delta = _parse_duration(duration)
    if not delta:
        await ctx.send("invalid duration, use something like `1h`, `1d`, `1w`")
        return
    unban_at = datetime.now(timezone.utc) + delta
    try:
        await member.ban(reason=reason)
    except discord.Forbidden:
        await ctx.send("can't ban that person — their role might be above mine")
        return
    await bot.db.add_tempban(ctx.guild.id, member.id, ctx.author.id, reason, unban_at)
    case_num = await bot.db.add_case(ctx.guild.id, "tempban", ctx.author.id, member.id, reason)
    await bot.db.log_staff_action(ctx.guild.id, ctx.author.id, "tempban", member.id)
    await ctx.send(f"⛔ tempbanned {member.mention} for `{duration}` — {reason}")
    await post_modlog(ctx.guild, f"⛔ Tempban ({duration})", ctx.author, member, reason, case_num, discord.Color.dark_red())


@bot.command(name="strikes")
@staff_tier_check(TIER_TRIAL)
async def strikes_cmd(ctx: commands.Context, member: discord.Member):
    """Show a member's active automod strikes (decay after 24h)."""
    count = await bot.db.get_active_strikes(ctx.guild.id, member.id)
    await ctx.send(f"{member.mention} has **{count}** active automod strike(s) (resets after 24h)")


@bot.command(name="clearstrikes")
@staff_tier_check(TIER_TRIAL)
async def clearstrikes_cmd(ctx: commands.Context, member: discord.Member):
    """Reset a member's automod strikes back to zero (manual warns are untouched)."""
    n = await bot.db.clear_strikes(ctx.guild.id, member.id)
    await ctx.send(f"cleared **{n}** automod strike(s) for {member.mention}")


AUTOMOD_RULES = {
    "scamlinks":   "automod_scamlinks",
    "antiinvite":  "automod_antiinvite",
    "antilink":    "automod_antilink",
    "mentionspam": "automod_mentionspam",
    "zalgo":       "automod_zalgo",
    "capsspam":    "automod_capsspam",
    "emojispam":   "automod_emojispam",
    "antispam":    "automod_antispam",
}


@bot.command(name="automod")
@staff_tier_check(TIER_SENIOR)
async def automod_cmd(ctx: commands.Context, rule: str = None, setting: str = None):
    """.automod — view every rule's on/off state.
    .automod <rule> <on|off> — toggle one rule.
    Rules: scamlinks, antiinvite, antilink, mentionspam, zalgo, capsspam, emojispam, antispam."""
    config = await get_config(ctx.guild.id, bot.db)
    if rule is None:
        lines = [f"{'🟢' if config.get(key, True) else '🔴'} `{name}` — "
                 f"{'ON' if config.get(key, True) else 'OFF'}" for name, key in AUTOMOD_RULES.items()]
        embed = discord.Embed(title="🛡️ Automod Rules", description="\n".join(lines), color=discord.Color.blurple())
        embed.set_footer(text=f"{PREFIX}automod <rule> <on|off> to toggle one")
        await ctx.send(embed=embed)
        return
    rule = rule.lower()
    if rule not in AUTOMOD_RULES:
        await ctx.send(f"unknown rule `{rule}` — options: {', '.join(AUTOMOD_RULES)}")
        return
    if not setting or setting.lower() not in ("on", "off"):
        await ctx.send(f"usage: `{PREFIX}automod {rule} <on|off>`")
        return
    enabled = setting.lower() == "on"
    await bot.db.update_config(ctx.guild.id, AUTOMOD_RULES[rule], enabled)
    await ctx.send(f"✅ `{rule}` is now **{'ON 🟢' if enabled else 'OFF 🔴'}**")


@bot.command(name="raidmode")
@staff_tier_check(TIER_SENIOR)
async def raidmode_cmd(ctx: commands.Context, setting: str = None, action: str = None):
    """.raidmode — view current anti-raid settings.
    .raidmode on|off — enable/disable join-rate raid screening entirely.
    .raidmode action <timeout|kick|log_only> — what happens to a flagged joiner.
    .raidmode lockdown <on|off> — auto-lockdown the whole server on a severe join burst."""
    config = await get_config(ctx.guild.id, bot.db)
    if setting is None:
        enabled = config.get("raidcheck_enabled", True)
        cur_action = config.get("raidcheck_action", "timeout")
        autolock = config.get("raidcheck_autolockdown", True)
        min_age = config.get("raidcheck_minage_hours", MIN_ACCOUNT_AGE_HOURS_DEFAULT)
        await ctx.send(embed=discord.Embed(
            title="🚨 Anti-Raid Settings",
            description=(
                f"Status: {'🟢 ON' if enabled else '🔴 OFF'}\n"
                f"Action on flagged join: `{cur_action}`\n"
                f"Auto-lockdown on severe burst: {'🟢 ON' if autolock else '🔴 OFF'}\n"
                f"Minimum account age: `{min_age}h`"
            ),
            color=discord.Color.blurple(),
        ))
        return
    setting = setting.lower()
    if setting in ("on", "off"):
        await bot.db.update_config(ctx.guild.id, "raidcheck_enabled", setting == "on")
        await ctx.send(f"✅ Anti-raid screening is now **{'ON' if setting == 'on' else 'OFF'}**.")
        return
    if setting == "action":
        if action not in ("timeout", "kick", "log_only"):
            await ctx.send(f"⚠️ Usage: `{PREFIX}raidmode action <timeout|kick|log_only>`")
            return
        await bot.db.update_config(ctx.guild.id, "raidcheck_action", action)
        await ctx.send(f"✅ Flagged joins will now trigger: **{action}**.")
        return
    if setting == "lockdown":
        if not action or action.lower() not in ("on", "off"):
            await ctx.send(f"⚠️ Usage: `{PREFIX}raidmode lockdown <on|off>`")
            return
        await bot.db.update_config(ctx.guild.id, "raidcheck_autolockdown", action.lower() == "on")
        await ctx.send(f"✅ Auto-lockdown on severe raid bursts is now **{action.upper()}**.")
        return
    await ctx.send(f"⚠️ Usage: `{PREFIX}raidmode [on|off]`, `{PREFIX}raidmode action <type>`, "
                    f"or `{PREFIX}raidmode lockdown <on|off>`.")


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
            "Both update on their own as people join, leave, or boost.\n\n"
            "**Want more?** Pick a category below — level roles, automod, tickets, logs, and more."
        ),
    )
    await msg.edit(content=None, embed=embed, view=SetupHubView())



@bot.command(name="lockdown", aliases=["lock"])
@staff_tier_check(TIER_SENIOR)
async def lockdown_cmd(ctx: commands.Context, channel: discord.abc.GuildChannel = None):
    """Lock THIS channel only (or a mentioned one) — snapshots every existing
    permission overwrite first, so `.unlock` restores it exactly. Nothing
    else in the server is touched."""
    channel = channel or ctx.channel
    locked = await lock_channel(channel, f"manually triggered by {ctx.author}")
    if not locked:
        await ctx.send(f"{channel.mention} is already locked — use `{PREFIX}unlock` to lift it")
        return
    await ctx.send(f"🔒 {channel.mention} locked")


@bot.command(name="unlock")
@staff_tier_check(TIER_SENIOR)
async def unlock_cmd(ctx: commands.Context, channel: discord.abc.GuildChannel = None):
    """Lift a lock on THIS channel (or a mentioned one), restoring permissions
    to exactly what they were before `.lockdown`."""
    channel = channel or ctx.channel
    restored = await unlock_channel(channel)
    if not restored:
        await ctx.send(f"{channel.mention} isn't locked")
        return
    await ctx.send(f"🔓 {channel.mention} unlocked")


@bot.command(name="serverlockdown", aliases=["lockdownall", "raidlock"])
@staff_tier_check(TIER_SENIOR)
async def serverlockdown_cmd(ctx: commands.Context):
    """Manually lock EVERY text channel server-wide — the full anti-raid
    lockdown mode. Snapshots every channel's original overwrites first, so
    `.serverunlock` restores everything exactly. This is the same lockdown
    auto-triggered by a severe join-raid burst, just started by a human."""
    if is_in_lockdown(ctx.guild.id):
        await ctx.send(f"server's already locked down — use `{PREFIX}serverunlock` to lift it")
        return
    async with ctx.typing():
        locked_count = await serverwide_lockdown(ctx.guild, f"manually triggered by {ctx.author}")
    await ctx.send(
        f"🔒🚨 server-wide lockdown on — locked **{locked_count}** channel(s). "
        f"run `{PREFIX}serverunlock` when it's safe"
    )


@bot.command(name="serverunlock", aliases=["unlockall"])
@staff_tier_check(TIER_SENIOR)
async def serverunlock_cmd(ctx: commands.Context):
    """Lift a server-wide lockdown, restoring every channel it locked to
    exactly what it was before. Channels individually `.lockdown`-ed by staff
    outside of the server-wide lockdown stay locked — use `.unlock` for those."""
    if not is_in_lockdown(ctx.guild.id):
        await ctx.send("server isn't in lockdown right now")
        return
    async with ctx.typing():
        restored = await serverwide_unlock(ctx.guild)
    await ctx.send(f"🔓 lockdown lifted — restored **{restored}** channel(s)")


@bot.command(name="raidage")
@staff_tier_check(TIER_SENIOR)
async def raidage_cmd(ctx: commands.Context, hours: int = None):
    """View or set the minimum account age (in hours) required to join without
    being flagged by anti-raid. `.raidage 0` disables the age gate entirely."""
    if hours is None:
        config = await get_config(ctx.guild.id, bot.db)
        current = config.get("raidcheck_minage_hours", MIN_ACCOUNT_AGE_HOURS_DEFAULT)
        await ctx.send(f"min account age to join: **{current}h** (0 = off)")
        return
    hours = max(0, hours)
    await bot.db.update_config(ctx.guild.id, "raidcheck_minage_hours", hours)
    if hours == 0:
        await ctx.send("✅ account age gate disabled")
    else:
        await ctx.send(f"✅ accounts younger than **{hours}h** will now get flagged on join")


@bot.command(name="release")
@staff_tier_check(TIER_SENIOR)
async def release_cmd(ctx: commands.Context, user_id: int):
    """Restore a member anti-nuke contained — gives back exactly the roles
    they held before containment and lifts their timeout. Use this if
    anti-nuke caught a false positive."""
    restored = await antinuke_release(ctx.guild, user_id)
    if restored == 0:
        await ctx.send("nothing to restore — either they're not contained or they left")
        return
    await ctx.send(f"🔓 released <@{user_id}> — restored **{restored}** role(s) and lifted timeout")


@bot.command(name="analytics")
@staff_tier_check(TIER_TRIAL)
async def analytics_cmd(ctx: commands.Context, subcommand: str = "growth", days: int = 14):
    """`growth` — member count + daily change. `joins` — join/leave trends. `activity` — messages per day."""
    sub = subcommand.lower()
    days = max(1, min(days, 90))

    if sub == "growth":
        history = await bot.db.get_member_count_history(ctx.guild.id, days=days)
        if not history:
            await ctx.send("📊 No data yet — snapshots are taken hourly.")
            return
        lines = []
        prev = None
        for row in history:
            count = row["member_count"]
            diff_str = "—" if prev is None else (f"+{count - prev}" if count >= prev else str(count - prev))
            lines.append(f"`{row['date']}` → **{count}** ({diff_str})")
            prev = count
        net = history[-1]["member_count"] - history[0]["member_count"]
        embed = discord.Embed(title="📊 Member Growth", color=discord.Color.blurple(), description="\n".join(lines[-25:]))
        embed.set_footer(text=f"Net over {len(history)} day(s): {'+' if net >= 0 else ''}{net}")
        await ctx.send(embed=embed)

    elif sub == "joins":
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        join_col = bot.db._client["lxte_assistant"]["join_log"]
        leave_col = bot.db._client["lxte_assistant"]["leave_log"]
        join_count = await join_col.count_documents({"guild_id": ctx.guild.id, "joined_at": {"$gte": cutoff}})
        leave_count = await leave_col.count_documents({"guild_id": ctx.guild.id, "left_at": {"$gte": cutoff}})
        net = join_count - leave_count
        embed = discord.Embed(title=f"📥 Join/Leave Trends (last {days}d)", color=discord.Color.blurple())
        embed.add_field(name="Joins", value=str(join_count), inline=True)
        embed.add_field(name="Leaves", value=str(leave_count), inline=True)
        embed.add_field(name="Net", value=f"{'+' if net >= 0 else ''}{net}", inline=True)
        await ctx.send(embed=embed)

    elif sub == "activity":
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        msg_col = bot.db._client["lxte_assistant"]["daily_msg_counts"]
        rows = await msg_col.find(
            {"guild_id": ctx.guild.id, "date": {"$gte": cutoff.date().isoformat()}},
            sort=[("date", 1)],
        ).to_list(length=days)
        if not rows:
            await ctx.send("📊 No message activity data yet — this starts tracking from now.")
            return
        lines = [f"`{r['date']}` → **{r.get('count', 0)}** messages" for r in rows[-25:]]
        embed = discord.Embed(title=f"💬 Message Activity (last {days}d)", color=discord.Color.blurple(),
                               description="\n".join(lines))
        await ctx.send(embed=embed)

    else:
        await ctx.send(f"⚠️ Unknown subcommand. Options: `growth`, `joins`, `activity`")


@bot.command(name="setwelcome")
@commands.has_permissions(administrator=True)
async def setwelcome_cmd(ctx: commands.Context, *, template: str):
    """Set the join message. Variables: {user} {user.id} {server} {count} {mention}.
    Now sent as an embed with avatar + account age, not plain text."""
    await bot.db.update_config(ctx.guild.id, "welcome_template", template)
    preview = _build_welcome_leave_embed(template, ctx.author, ctx.guild, kind="welcome")
    await ctx.send(content="✅ Welcome template saved. Preview:", embed=preview)


@bot.command(name="setinvitelog")
@commands.has_permissions(administrator=True)
async def setinvitelog_cmd(ctx: commands.Context, *, template: str):
    """Set the invite-tracker join message, posted whenever someone joins via
    a tracked invite. Variables: {user} {user.id} {server} {count} {mention}
    {inviter} {invites}. Point it at a channel with the setup wizard's
    'Invite Tracker' log, or `.config invite_log_channel <channel_id>`."""
    await bot.db.update_config(ctx.guild.id, "invite_template", template)
    sample_total = await bot.db.get_invite_count(ctx.guild.id, ctx.author.id)
    preview = _build_invite_embed(template, ctx.author, ctx.guild, ctx.author, sample_total)
    await ctx.send(content="✅ Invite tracker template saved. Preview:", embed=preview)


@bot.command(name="testinvitelog")
@commands.has_permissions(administrator=True)
async def testinvitelog_cmd(ctx: commands.Context):
    config = await bot.db.get_config(ctx.guild.id)
    tpl = config.get("invite_template")
    if not tpl:
        await ctx.send(f"No invite tracker template set. Use `{PREFIX}setinvitelog <message>`.")
        return
    chan_id = config.get("invite_log_channel")
    chan = ctx.guild.get_channel(chan_id) if chan_id else ctx.channel
    sample_total = await bot.db.get_invite_count(ctx.guild.id, ctx.author.id)
    embed = _build_invite_embed(tpl, ctx.author, ctx.guild, ctx.author, sample_total)
    await (chan or ctx.channel).send(embed=embed)


@bot.command(name="testwelcome")
@commands.has_permissions(administrator=True)
async def testwelcome_cmd(ctx: commands.Context):
    config = await bot.db.get_config(ctx.guild.id)
    tpl = config.get("welcome_template")
    if not tpl:
        await ctx.send(f"No welcome template set. Use `{PREFIX}setwelcome <message>`.")
        return
    chan_id = config.get("welcome_channel") or config.get("log_entryexit_channel")
    chan = ctx.guild.get_channel(chan_id) if chan_id else ctx.channel
    embed = _build_welcome_leave_embed(tpl, ctx.author, ctx.guild, kind="welcome")
    await (chan or ctx.channel).send(embed=embed)


def _render_template(template: str, member: discord.Member, guild: discord.Guild) -> str:
    return (
        template
        .replace("{user}", str(member))
        .replace("{user.id}", str(member.id))
        .replace("{mention}", member.mention)
        .replace("{server}", guild.name)
        .replace("{count}", str(guild.member_count))
    )


def _build_welcome_leave_embed(template: str, member: discord.Member, guild: discord.Guild,
                                *, kind: str) -> discord.Embed:
    """Builds the embed version of a welcome/leave message — avatar thumbnail,
    account age, and join position alongside the rendered template text.
    kind is "welcome" or "leave" and only changes color/title/footer framing;
    the template variables ({user} {user.id} {server} {count} {mention})
    still work exactly as before."""
    description = _render_template(template, member, guild)
    account_age_days = (datetime.now(timezone.utc) - member.created_at).days

    if kind == "welcome":
        embed = discord.Embed(title=f"👋 Welcome to {guild.name}!", description=description,
                               color=discord.Color.green())
        embed.add_field(name="Member #", value=str(guild.member_count), inline=True)
    else:
        embed = discord.Embed(title=f"👋 Goodbye from {guild.name}", description=description,
                               color=discord.Color.red())

    embed.add_field(name="Account Age", value=f"{account_age_days} day(s) old", inline=True)
    if account_age_days < 7:
        embed.add_field(name="⚠️", value="New account", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"{member} • {member.id}")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def _build_invite_embed(template: str, member: discord.Member, guild: discord.Guild,
                         inviter: Optional[discord.abc.User], invite_total: Optional[int]) -> discord.Embed:
    """Builds the invite-tracker embed shown when a member joins via a tracked
    invite. Supports the same {user} {user.id} {server} {count} {mention}
    variables as welcome/leave, plus {inviter} and {invites} (the inviter's
    new running total)."""
    description = (
        _render_template(template, member, guild)
        .replace("{inviter}", inviter.mention if inviter else "an unknown/vanity invite")
        .replace("{invites}", str(invite_total) if invite_total is not None else "?")
    )
    embed = discord.Embed(title="📨 Invite Tracker", description=description, color=discord.Color.blurple())
    if inviter:
        embed.add_field(name="Invited by", value=f"{inviter.mention} ({invite_total or 0} total)", inline=True)
    else:
        embed.add_field(name="Invited by", value="Unknown / vanity URL", inline=True)
    embed.add_field(name="Member #", value=str(guild.member_count), inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"{member} • {member.id}")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def _coerce_config_value(value: str):
    """Raw `.config` input always arrives as a string, but most config reads
    expect a real bool or int (e.g. `config.get("automod_capsspam", True)`,
    `guild.get_channel(config.get("modlog_channel"))`). Without this, setting
    `.config automod_capsspam false` stored the literal string "false" —
    which Python treats as truthy — so the toggle silently did nothing, and
    `.config modlog_channel 123456` stored a string channel ID that
    `get_channel()` can never match. This makes `.config` behave identically
    to the dropdown UI for the same keys."""
    low = value.strip().lower()
    if low in ("true", "on", "yes", "enable", "enabled"):
        return True
    if low in ("false", "off", "no", "disable", "disabled"):
        return False
    if value.lstrip("-").isdigit():
        return int(value)
    return value


@bot.command(name="config")
@commands.has_permissions(administrator=True)
async def config_cmd(ctx: commands.Context, key: str = None, value: str = None):
    if key is None:
        config = await bot.db.get_config(ctx.guild.id)
        if not config:
            await ctx.send("No config set for this server yet.")
            return
        lines = [f"**{k}**: {v}" for k, v in config.items() if k not in ("_id", "guild_id")]
        await ctx.send(embed=discord.Embed(title="Server Config", description="\n".join(lines) or "Empty"))
        return
    if value is None:
        config = await bot.db.get_config(ctx.guild.id)
        await ctx.send(f"`{key}` = `{config.get(key, 'not set')}`")
        return
    parsed = _coerce_config_value(value)
    await bot.db.update_config(ctx.guild.id, key, parsed)
    await ctx.send(f"✅ Set `{key}` = `{parsed}` ({type(parsed).__name__})")



# ═══════════════════════════════════════════════════════════════════════════════
#  COMMANDS — snipe
# ═══════════════════════════════════════════════════════════════════════════════

_snipe_cache: dict[int, dict] = {}  # channel_id -> {content, author_id, author_name, avatar, deleted_at}


@bot.listen("on_message_delete")
async def _cache_snipe(message: discord.Message):
    if message.author.bot or not message.content:
        return
    _snipe_cache[message.channel.id] = {
        "content":     message.content,
        "author_id":   message.author.id,
        "author_name": str(message.author),
        "avatar":      message.author.display_avatar.url,
        "deleted_at":  datetime.now(timezone.utc),
    }


@bot.command(name="snipe")
@staff_tier_check(TIER_TRIAL)
async def snipe_cmd(ctx: commands.Context):
    """Show the last deleted message in this channel."""
    cached = _snipe_cache.get(ctx.channel.id)
    if not cached:
        await ctx.send("Nothing to snipe in this channel.")
        return
    embed = discord.Embed(
        description=cached["content"][:2000],
        color=discord.Color.dark_grey(),
        timestamp=cached["deleted_at"],
    )
    embed.set_author(name=cached["author_name"], icon_url=cached["avatar"])
    embed.set_footer(text=f"Deleted • sniped by {ctx.author}")
    await ctx.send(embed=embed)


# ═══════════════════════════════════════════════════════════════════════════════
#  COMMANDS — slowmode / softban
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="slowmode", aliases=["sm"])
@staff_tier_check(TIER_TRIAL)
@commands.bot_has_permissions(manage_channels=True)
async def slowmode_cmd(ctx: commands.Context, channel: Optional[discord.TextChannel] = None, duration: str = "0"):
    """Set slowmode on a channel. Duration like 5s / 10m / 1h, or 0 to clear.
    Omit channel to target the current one."""
    channel = channel or ctx.channel
    if duration in ("0", "off", "clear", "none"):
        delay = 0
    else:
        delta = _parse_duration(duration)
        if not delta:
            await ctx.send("⚠️ Bad duration. Examples: `5s`, `30s`, `2m`, `1h`.")
            return
        delay = int(delta.total_seconds())
        if delay > 21600:  # Discord max is 6h
            await ctx.send("⚠️ Discord's slowmode max is 6 hours.")
            return
    await channel.edit(slowmode_delay=delay, reason=f"Slowmode set by {ctx.author}")
    if delay == 0:
        await ctx.send(f"✅ Slowmode cleared in {channel.mention}.")
    else:
        await ctx.send(f"🐢 Slowmode set to `{_format_duration(delay)}` in {channel.mention}.")


@bot.command(name="softban")
@staff_tier_check(TIER_MOD)
@commands.bot_has_permissions(ban_members=True)
@commands.cooldown(2, 15, commands.BucketType.user)
async def softban_cmd(ctx: commands.Context, member: discord.Member, delete_days: int = 1, *, reason: str = "No reason provided"):
    """Ban then immediately unban — wipes their recent messages without a permanent ban.
    delete_days controls how many days of messages to prune (1-7, default 1)."""
    if not await validate_mod_target(ctx, member):
        return
    delete_days = max(1, min(delete_days, 7))
    try:
        # delete_message_seconds is the current discord.py API — delete_message_days
        # is deprecated and not guaranteed to keep working across library versions.
        await member.ban(reason=f"Softban by {ctx.author}: {reason}", delete_message_seconds=delete_days * 86400)
        await ctx.guild.unban(member, reason="Softban — lifting immediately")
    except discord.Forbidden:
        await ctx.send("can't do that — their role might be above mine or i'm missing ban perms")
        return
    except discord.HTTPException as e:
        await ctx.send(f"softban failed: {e}")
        return
    case_num = await bot.db.add_case(ctx.guild.id, "softban", ctx.author.id, member.id, reason)
    await bot.db.log_staff_action(ctx.guild.id, ctx.author.id, "softban", member.id)
    await ctx.send(f"🧹 softbanned {member.mention} — wiped {delete_days}d of messages, not permanently banned. {reason}")
    await post_modlog(ctx.guild, "🧹 Softban", ctx.author, member, reason, case_num, discord.Color.orange())


# ═══════════════════════════════════════════════════════════════════════════════
#  COMMANDS — invites
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="invites")
@staff_tier_check(TIER_TRIAL)
async def invites_cmd(ctx: commands.Context, member: discord.Member = None):
    """Show invite stats for a member (or yourself)."""
    member = member or ctx.author
    doc = await bot.db.invites.find_one(
        {"guild_id": ctx.guild.id, "inviter_id": member.id, "invite_code": "__total__"}
    ) or {}
    total   = doc.get("total_invites", 0)
    regular = doc.get("regular", 0)
    fake    = doc.get("fake", 0)
    left    = doc.get("left", 0)
    bonus   = doc.get("bonus", 0)
    embed = discord.Embed(
        title=f"📨 {member.display_name}'s Invites",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Total",   value=str(total),   inline=True)
    embed.add_field(name="Regular", value=str(regular), inline=True)
    embed.add_field(name="Left",    value=str(left),    inline=True)
    embed.add_field(name="Fake",    value=str(fake),    inline=True)
    embed.add_field(name="Bonus",   value=str(bonus),   inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name="inviteleaderboard", aliases=["invitelb", "toplb"])
@staff_tier_check(TIER_TRIAL)
async def invitelb_cmd(ctx: commands.Context):
    """Top 10 inviters in this server."""
    rows = await bot.db.get_invite_leaderboard(ctx.guild.id, limit=10)
    if not rows:
        await ctx.send("No invite data yet.")
        return
    lines = []
    for i, row in enumerate(rows, 1):
        m = ctx.guild.get_member(row["inviter_id"])
        name = m.display_name if m else f"<@{row['inviter_id']}>"
        total   = row.get("total_invites", 0)
        regular = row.get("regular", 0)
        left    = row.get("left", 0)
        fake    = row.get("fake", 0)
        lines.append(f"**#{i}** {name} — {total} total ({regular} reg · {left} left · {fake} fake)")
    embed = discord.Embed(title="📨 Invite Leaderboard", description="\n".join(lines), color=discord.Color.blurple())
    await ctx.send(embed=embed)


# ═══════════════════════════════════════════════════════════════════════════════
#  COMMANDS — tickets
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="tickets", aliases=["openticketsmod"])
@staff_tier_check(TIER_TRIAL)
async def tickets_cmd(ctx: commands.Context):
    """List every open ticket in this server with priority and claim status."""
    open_tickets = await bot.db.get_open_tickets(ctx.guild.id)
    if not open_tickets:
        await ctx.send("📭 No open tickets right now.")
        return
    priority_order = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
    open_tickets.sort(key=lambda d: priority_order.get(d.get("priority", "medium"), 2))
    lines = []
    for doc in open_tickets:
        chan = ctx.guild.get_channel(doc.get("channel_id"))
        chan_str = chan.mention if chan else f"`{doc.get('channel_id')}` (deleted?)"
        claimer = f"<@{doc['claimed_by']}>" if doc.get("claimed_by") else "unclaimed"
        lines.append(f"{_ticket_priority_label(doc.get('priority', 'medium'))} {chan_str} — opened by <@{doc.get('user_id')}> · {claimer}")
    embed = discord.Embed(title=f"🎫 Open Tickets ({len(open_tickets)})", description="\n".join(lines), color=discord.Color.blurple())
    await ctx.send(embed=embed)


@bot.command(name="priority", aliases=["setpriority"])
@staff_tier_check(TIER_TRIAL)
async def priority_cmd(ctx: commands.Context, level: str):
    """Set this ticket channel's priority: low / medium / high / urgent."""
    level = level.lower()
    if level not in TICKET_PRIORITIES:
        await ctx.send(f"⚠️ Invalid priority. Choose from: {', '.join(TICKET_PRIORITIES)}.")
        return
    doc = await bot.db.get_ticket(ctx.channel.id)
    if not doc:
        await ctx.send("⚠️ This doesn't look like a ticket channel.")
        return
    await bot.db.set_ticket_priority(ctx.channel.id, level)
    try:
        await ctx.channel.edit(topic=f"Priority: {_ticket_priority_label(level)}")
    except discord.HTTPException:
        pass
    await ctx.send(f"{_ticket_priority_label(level)} priority set by {ctx.author.mention}.")


@bot.command(name="claim")
@staff_tier_check(TIER_TRIAL)
async def claim_cmd(ctx: commands.Context):
    """Claim this ticket channel."""
    doc = await bot.db.get_ticket(ctx.channel.id)
    if not doc:
        await ctx.send("⚠️ This doesn't look like a ticket channel.")
        return
    if doc.get("claimed_by") == ctx.author.id:
        await bot.db.unclaim_ticket(ctx.channel.id)
        await ctx.send(f"🙋 {ctx.author.mention} unclaimed this ticket.")
        return
    ok = await bot.db.claim_ticket(ctx.channel.id, ctx.author.id)
    if not ok:
        await ctx.send(f"⚠️ Already claimed by <@{doc.get('claimed_by')}>.")
        return
    await ctx.send(f"🙋 {ctx.author.mention} claimed this ticket.")


@bot.command(name="ticketstats")
@staff_tier_check(TIER_MOD)
async def ticketstats_cmd(ctx: commands.Context):
    """Server-wide ticket stats: totals, ratings, and top closers."""
    stats = await bot.db.get_ticket_stats(ctx.guild.id)
    embed = discord.Embed(title="🎫 Ticket Stats", color=discord.Color.blurple())
    embed.add_field(name="Total", value=str(stats["total_tickets"]), inline=True)
    embed.add_field(name="Open", value=str(stats["open_tickets"]), inline=True)
    embed.add_field(name="Closed", value=str(stats["closed_tickets"]), inline=True)
    embed.add_field(name="Avg Rating", value=f"{stats['avg_rating']}/5 ({stats['total_ratings']} ratings)", inline=False)
    if stats["top_closers"]:
        lines = [f"<@{c['closer_id']}> — {c['count']}" for c in stats["top_closers"]]
        embed.add_field(name="Top Closers", value="\n".join(lines), inline=False)
    await ctx.send(embed=embed)


# ═══════════════════════════════════════════════════════════════════════════════
#  COMMANDS — giveaways
# ═══════════════════════════════════════════════════════════════════════════════

GIVEAWAY_EMOJI = "🎉"
GIVEAWAY_REROLL_CUSTOM_ID = "ajscrib:giveaway_reroll"


def _giveaway_bonus_config(config: dict) -> dict[int, int]:
    """role_id -> extra entries granted by that role. Stored as a dict of
    str(role_id) -> int in config (Mongo keys must be strings), returned here
    with int keys for easy lookup against member.roles."""
    raw = config.get("giveaway_bonus_roles", {}) or {}
    return {int(k): v for k, v in raw.items()}


def _giveaway_blacklist_config(config: dict) -> tuple[set[int], set[int]]:
    users = set(config.get("giveaway_blacklist_users", []) or [])
    roles = set(config.get("giveaway_blacklist_roles", []) or [])
    return users, roles


def _is_giveaway_blacklisted(member: discord.Member, config: dict) -> bool:
    users, roles = _giveaway_blacklist_config(config)
    if member.id in users:
        return True
    member_role_ids = {r.id for r in member.roles}
    return bool(member_role_ids & roles)


def _entries_for_member(member: discord.Member, config: dict) -> int:
    """Base 1 entry + any bonus entries from configured roles, stacking
    across every bonus role the member holds."""
    bonus_map = _giveaway_bonus_config(config)
    if not bonus_map:
        return 1
    member_role_ids = {r.id for r in member.roles}
    bonus = sum(amount for role_id, amount in bonus_map.items() if role_id in member_role_ids)
    return 1 + max(bonus, 0)


async def _weighted_pick(guild: discord.Guild, entrants: list[int], config: dict, k: int) -> list[int]:
    """Builds a weighted pool (each entrant appears `_entries_for_member` times)
    and samples without replacement. Falls back to flat random.sample if a
    member has left the server (their entry just counts as 1, no bonus).
    Re-checks the blacklist here (not just at reaction time) so a user/role
    blacklisted *after* entering — or already blacklisted but still present
    in an older entrants list — can never actually win."""
    pool: list[int] = []
    for uid in entrants:
        member = guild.get_member(uid)
        if member and _is_giveaway_blacklisted(member, config):
            continue
        weight = _entries_for_member(member, config) if member else 1
        pool.extend([uid] * weight)
    if not pool:
        return []
    winners: list[int] = []
    seen: set[int] = set()
    random.shuffle(pool)
    for uid in pool:
        if uid not in seen:
            winners.append(uid)
            seen.add(uid)
        if len(winners) >= k:
            break
    return winners


class GiveawayEndView(discord.ui.View):
    """Persistent view attached to ended giveaway embeds — lets staff reroll
    directly from the message instead of needing the message ID for `.greroll`."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Reroll", emoji="🔁", style=discord.ButtonStyle.blurple, custom_id=GIVEAWAY_REROLL_CUSTOM_ID)
    async def reroll_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        config = await get_config(interaction.guild.id, bot.db)
        tier = await get_staff_tier(interaction.user, config)
        if tier < TIER_TRIAL and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("⛔ Only staff can reroll a giveaway.", ephemeral=True)
            return

        doc = await bot.db.get_giveaway(interaction.message.id)
        if not doc:
            await interaction.response.send_message("⚠️ Couldn't find this giveaway's data anymore.", ephemeral=True)
            return
        entrants = doc.get("entrants", [])
        if not entrants:
            await interaction.response.send_message("⚠️ No entrants to reroll from.", ephemeral=True)
            return

        winners = await _weighted_pick(interaction.guild, entrants, config, doc.get("winners", 1))
        mention_str = ", ".join(f"<@{w}>" for w in winners) if winners else "no valid entrants"
        await interaction.response.send_message(
            f"🔁 **Reroll** for **{doc['prize']}** — new winner(s): {mention_str}! Congrats!"
        )


@bot.command(name="gstart", aliases=["gcreate"])
@staff_tier_check(TIER_TRIAL)
async def gstart_cmd(ctx: commands.Context, duration: str, winners: int, *, prize: str):
    """.gstart <duration> <winners> <prize> — starts a giveaway. Duration: 10m, 2h, 1d etc."""
    delta = _parse_duration(duration)
    if not delta:
        await ctx.send("⚠️ Bad duration. Examples: `10m`, `2h`, `1d`.")
        return
    winners = max(1, min(winners, 20))
    ends_at = datetime.now(timezone.utc) + delta

    config = await get_config(ctx.guild.id, bot.db)
    bonus_map = _giveaway_bonus_config(config)
    bonus_line = ""
    if bonus_map:
        bonus_parts = []
        for role_id, amount in bonus_map.items():
            role = ctx.guild.get_role(role_id)
            if role:
                bonus_parts.append(f"{role.mention} +{amount}")
        if bonus_parts:
            bonus_line = f"**Bonus entries:** {', '.join(bonus_parts)}\n"

    embed = discord.Embed(
        title=f"🎉 GIVEAWAY — {prize}",
        color=discord.Color.gold(),
        description=(
            f"React with {GIVEAWAY_EMOJI} to enter!\n\n"
            f"**Winners:** {winners}\n"
            f"{bonus_line}"
            f"**Ends:** <t:{int(ends_at.timestamp())}:R> (<t:{int(ends_at.timestamp())}:f>)\n"
            f"**Hosted by:** {ctx.author.mention}"
        ),
    )
    embed.set_footer(text=f"Ends at")
    embed.timestamp = ends_at
    msg = await ctx.send(embed=embed)
    await msg.add_reaction(GIVEAWAY_EMOJI)

    await bot.db.create_giveaway(
        ctx.guild.id, ctx.channel.id, msg.id,
        ctx.author.id, prize, winners, ends_at,
    )
    await ctx.message.delete()


@bot.command(name="gend")
@staff_tier_check(TIER_TRIAL)
async def gend_cmd(ctx: commands.Context, message_id: int):
    """.gend <message_id> — end a giveaway early and pick winners now."""
    doc = await bot.db.get_giveaway(message_id)
    if not doc:
        await ctx.send("⚠️ No giveaway found with that message ID.")
        return
    if doc.get("ended"):
        await ctx.send("⚠️ That giveaway already ended.")
        return
    await _resolve_giveaway(doc)
    await ctx.send("✅ Giveaway ended early.")


@bot.command(name="greroll")
@staff_tier_check(TIER_TRIAL)
async def greroll_cmd(ctx: commands.Context, message_id: int, count: int = 1):
    """.greroll <message_id> [count] — reroll winners for an ended giveaway."""
    doc = await bot.db.get_giveaway(message_id)
    if not doc:
        await ctx.send("⚠️ No giveaway found with that message ID.")
        return
    entrants = doc.get("entrants", [])
    if not entrants:
        await ctx.send("⚠️ No entrants to reroll from.")
        return
    config = await get_config(ctx.guild.id, bot.db)
    winners = await _weighted_pick(ctx.guild, entrants, config, count)
    mention_str = ", ".join(f"<@{w}>" for w in winners) if winners else "no valid entrants"
    channel = bot.get_channel(doc["channel_id"])
    if channel:
        try:
            await channel.send(f"🔁 **Reroll** for **{doc['prize']}** — new winner(s): {mention_str}! Congrats!")
        except discord.HTTPException:
            pass
    await ctx.send(f"✅ Rerolled — {mention_str}")


@bot.command(name="gblacklist")
@staff_tier_check(TIER_MOD)
async def gblacklist_cmd(ctx: commands.Context, action: str, target: str):
    """.gblacklist add/remove @user|@role — blocks a user or role from
    entering (and winning) any giveaway in this server. Blacklisted entrants
    are blocked at react-time and silently excluded from winner draws."""
    action = action.lower()
    if action not in ("add", "remove"):
        await ctx.send("⚠️ Usage: `.gblacklist add|remove @user` or `@role`.")
        return

    config = await get_config(ctx.guild.id, bot.db)
    users, roles = _giveaway_blacklist_config(config)

    member_conv = commands.MemberConverter()
    role_conv = commands.RoleConverter()
    member = None
    role = None
    try:
        member = await member_conv.convert(ctx, target)
    except commands.BadArgument:
        try:
            role = await role_conv.convert(ctx, target)
        except commands.BadArgument:
            await ctx.send("⚠️ Couldn't resolve that as a member or role.")
            return

    if member:
        if action == "add":
            users.add(member.id)
        else:
            users.discard(member.id)
        await bot.db.update_config(ctx.guild.id, "giveaway_blacklist_users", list(users))
        await ctx.send(f"✅ {'Blacklisted' if action == 'add' else 'Unblacklisted'} {member.mention} from giveaways.")
    else:
        if action == "add":
            roles.add(role.id)
        else:
            roles.discard(role.id)
        await bot.db.update_config(ctx.guild.id, "giveaway_blacklist_roles", list(roles))
        await ctx.send(f"✅ {'Blacklisted' if action == 'add' else 'Unblacklisted'} {role.mention} from giveaways.")


@bot.command(name="gbonus")
@staff_tier_check(TIER_MOD)
async def gbonus_cmd(ctx: commands.Context, role: discord.Role, amount: int):
    """.gbonus @role <amount> — grants extra giveaway entries to anyone with
    that role (stacks across every bonus role a member holds). Use `0` to
    remove a role's bonus."""
    config = await get_config(ctx.guild.id, bot.db)
    bonus_map = _giveaway_bonus_config(config)
    if amount <= 0:
        bonus_map.pop(role.id, None)
        await bot.db.update_config(ctx.guild.id, "giveaway_bonus_roles", {str(k): v for k, v in bonus_map.items()})
        await ctx.send(f"✅ Removed bonus entries for {role.mention}.")
        return
    bonus_map[role.id] = amount
    await bot.db.update_config(ctx.guild.id, "giveaway_bonus_roles", {str(k): v for k, v in bonus_map.items()})
    await ctx.send(f"✅ {role.mention} now grants **+{amount}** bonus entries per giveaway.")


async def _resolve_giveaway(doc: dict):
    """Pick winners and post the result. Shared by the loop and .gend."""
    await bot.db.end_giveaway(doc["message_id"])
    channel = bot.get_channel(doc["channel_id"])
    if not channel:
        return
    config = await get_config(doc["guild_id"], bot.db) if channel.guild else {}
    entrants = doc.get("entrants", [])
    winners_n = doc.get("winners", 1)
    winners = await _weighted_pick(channel.guild, entrants, config, winners_n) if entrants else []

    try:
        orig = await channel.fetch_message(doc["message_id"])
        result_embed = discord.Embed(
            title=f"🎉 GIVEAWAY ENDED — {doc['prize']}",
            color=discord.Color.dark_gold(),
            description=(
                (", ".join(f"<@{w}>" for w in winners) + f"\nwon **{doc['prize']}**! Congrats!")
                if winners else "No valid entrants — no winner this time."
            ),
        )
        result_embed.set_footer(text="Giveaway ended — staff can use the Reroll button below")
        result_embed.timestamp = datetime.now(timezone.utc)
        await orig.edit(embed=result_embed, view=GiveawayEndView())
    except discord.HTTPException:
        pass

    if winners:
        mention_str = ", ".join(f"<@{w}>" for w in winners)
        await channel.send(f"🎉 Congrats {mention_str}! You won **{doc['prize']}**!")
    else:
        await channel.send(f"No valid entrants for **{doc['prize']}** — giveaway cancelled.")


# ─── wire reaction-based giveaway entry ──────────────────────────────────────

@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User):
    if user.bot:
        return
    if str(reaction.emoji) != GIVEAWAY_EMOJI:
        return
    doc = await bot.db.get_giveaway(reaction.message.id)
    if not doc or doc.get("ended"):
        return

    guild = reaction.message.guild
    member = guild.get_member(user.id) if guild else None
    if member and guild:
        config = await get_config(guild.id, bot.db)
        if _is_giveaway_blacklisted(member, config):
            try:
                await reaction.message.remove_reaction(reaction.emoji, user)
            except discord.HTTPException:
                pass
            try:
                await user.send(f"⛔ You're blacklisted from giveaways in **{guild.name}** and can't enter.")
            except discord.Forbidden:
                pass
            return

    await bot.db.add_entrant(reaction.message.id, user.id)


@bot.event
async def on_reaction_remove(reaction: discord.Reaction, user: discord.User):
    if user.bot:
        return
    if str(reaction.emoji) != GIVEAWAY_EMOJI:
        return
    doc = await bot.db.get_giveaway(reaction.message.id)
    if not doc or doc.get("ended"):
        return
    await bot.db.remove_entrant(reaction.message.id, user.id)


# ═══════════════════════════════════════════════════════════════════════════════
#  COMMANDS — userinfo / serverinfo
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="userinfo", aliases=["ui", "whois"])
@staff_tier_check(TIER_TRIAL)
async def userinfo_cmd(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    data   = await bot.db.get_level_data(member.id, ctx.guild.id)
    warns  = await bot.db.get_warns(ctx.guild.id, member.id)
    invite = await bot.db.invites.find_one(
        {"guild_id": ctx.guild.id, "inviter_id": member.id, "invite_code": "__total__"}
    ) or {}

    level  = calculate_level(data.get("total_xp", 0))[0] if data else 0
    joined_at = member.joined_at
    created_at = member.created_at
    account_age = (datetime.now(timezone.utc) - created_at).days
    join_age    = (datetime.now(timezone.utc) - joined_at).days if joined_at else "?"

    top_roles = [r.mention for r in sorted(member.roles[1:], key=lambda r: r.position, reverse=True)][:8]

    badges = []
    if member.guild_permissions.administrator:
        badges.append("🔑 Admin")
    if member.premium_since:
        badges.append("🚀 Booster")
    if member.bot:
        badges.append("🤖 Bot")

    embed = discord.Embed(color=member.color if member.color.value else discord.Color.blurple())
    embed.set_author(name=str(member), icon_url=member.display_avatar.url)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID",          value=f"`{member.id}`", inline=True)
    embed.add_field(name="Status",      value=str(member.status).title(), inline=True)
    embed.add_field(name="Badges",      value=" ".join(badges) or "—", inline=True)
    embed.add_field(name="Account Age", value=f"{account_age}d old\n<t:{int(created_at.timestamp())}:D>", inline=True)
    embed.add_field(name="Joined Server", value=f"{join_age}d ago\n<t:{int(joined_at.timestamp())}:D>" if joined_at else "?", inline=True)
    embed.add_field(name="Level",       value=str(level), inline=True)
    embed.add_field(name="Warns",       value=str(len(warns)), inline=True)
    embed.add_field(name="Invites",     value=str(invite.get("total_invites", 0)), inline=True)
    embed.add_field(name="Messages",    value=str(data.get("messages", 0) if data else 0), inline=True)
    if top_roles:
        embed.add_field(name=f"Roles ({len(member.roles) - 1})", value=" ".join(top_roles), inline=False)
    embed.set_footer(text=f"Requested by {ctx.author}")
    await ctx.send(embed=embed)


@bot.command(name="serverinfo", aliases=["si", "guildinfo"])
async def serverinfo_cmd(ctx: commands.Context):
    g = ctx.guild
    text   = sum(1 for c in g.channels if isinstance(c, discord.TextChannel))
    voice  = sum(1 for c in g.channels if isinstance(c, discord.VoiceChannel))
    humans = sum(1 for m in g.members if not m.bot)
    bots   = sum(1 for m in g.members if m.bot)

    embed = discord.Embed(
        title=g.name,
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name="Owner",     value=f"<@{g.owner_id}>", inline=True)
    embed.add_field(name="Created",   value=f"<t:{int(g.created_at.timestamp())}:D>", inline=True)
    embed.add_field(name="Region",    value=str(getattr(g, "region", "—")), inline=True)
    embed.add_field(name="Members",   value=f"👥 {humans} humans · 🤖 {bots} bots", inline=False)
    embed.add_field(name="Channels",  value=f"💬 {text} text · 🔊 {voice} voice", inline=True)
    embed.add_field(name="Roles",     value=str(len(g.roles) - 1), inline=True)
    embed.add_field(name="Boosts",    value=f"🚀 {g.premium_subscription_count} (Tier {g.premium_tier})", inline=True)
    embed.add_field(name="Emojis",    value=f"{len(g.emojis)}/{g.emoji_limit}", inline=True)
    embed.add_field(name="Verification", value=str(g.verification_level).title(), inline=True)
    embed.set_footer(text=f"ID: {g.id}")
    await ctx.send(embed=embed)


# ═══════════════════════════════════════════════════════════════════════════════
#  COMMANDS — restart
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="restart", aliases=["reboot"])
@commands.has_permissions(administrator=True)
async def restart_cmd(ctx: commands.Context):
    """Gracefully shut down. Requires a process manager (systemd/pm2/supervisor) to auto-restart."""
    await ctx.send("🔄 Restarting...")
    logger.info("Restart triggered by %s (%s)", ctx.author, ctx.author.id)
    await bot.close()
    import sys
    sys.exit(0)




def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN environment variable is not set.")
    bot.run(token)


if __name__ == "__main__":
    main()
