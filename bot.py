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
import math
import time
import asyncio
import logging
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
DAILY_XP_AMOUNT   = 50

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
#  BOT SETUP — "aj's crib"
# ═══════════════════════════════════════════════════════════════════════════════

PREFIX = os.getenv("PREFIX", ".")

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

        # background loops
        self.tempmute_loop.start()
        self.tempban_loop.start()
        self.giveaway_loop.start()

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
                import random
                winners = random.sample(entrants, k=min(winners_n, len(entrants))) if entrants else []
                if winners:
                    mention_str = ", ".join(f"<@{w}>" for w in winners)
                    await channel.send(f"🎉 Congratulations {mention_str}! You won **{doc['prize']}**!")
                else:
                    await channel.send(f"No valid entrants for **{doc['prize']}** — giveaway cancelled.")
        except Exception as e:
            logger.error("giveaway_loop error: %s", e)


bot = AjsCrib()


# ═══════════════════════════════════════════════════════════════════════════════
#  EVENTS
# ═══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="aj's crib")
    )
    logger.info("aj's crib is online as %s (id: %s) — prefix '%s'", bot.user, bot.user.id, PREFIX)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

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
            try:
                await message.channel.send(text)
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
async def on_member_update(before: discord.Member, after: discord.Member):
    if before.premium_since is None and after.premium_since is not None:
        count = await bot.db.record_boost(after.guild.id, after.id)
        result = await bot.db.add_xp(after.id, after.guild.id, BOOST_XP_REWARD)
        logger.info("%s boosted %s (boost #%d), awarded %d XP", after, after.guild, count, BOOST_XP_REWARD)


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
        description=f"Prefix: `{PREFIX}`",
    )
    embed.add_field(
        name="Leveling",
        value=f"`{PREFIX}rank [@user]` `{PREFIX}leaderboard` `{PREFIX}daily`",
        inline=False,
    )
    embed.add_field(
        name="Moderation",
        value=(
            f"`{PREFIX}warn @user reason` `{PREFIX}warnings @user` `{PREFIX}clearwarns @user`\n"
            f"`{PREFIX}kick @user reason` `{PREFIX}ban @user reason`\n"
            f"`{PREFIX}tempmute @user 10m reason` `{PREFIX}tempban @user 1d reason`\n"
            f"`{PREFIX}case <number>` `{PREFIX}cases @user`"
        ),
        inline=False,
    )
    embed.add_field(name="Utility", value=f"`{PREFIX}ping` `{PREFIX}config`", inline=False)
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


@bot.command(name="daily")
async def daily_cmd(ctx: commands.Context):
    result = await bot.db.add_xp(ctx.author.id, ctx.guild.id, DAILY_XP_AMOUNT)
    await ctx.send(f"✅ {ctx.author.mention} claimed your daily **{DAILY_XP_AMOUNT} XP**! Now level {result['level']}.")


# ═══════════════════════════════════════════════════════════════════════════════
#  COMMANDS — moderation / case system
# ═══════════════════════════════════════════════════════════════════════════════

@bot.command(name="warn")
@commands.has_permissions(moderate_members=True)
async def warn_cmd(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    count = await bot.db.add_warn(ctx.guild.id, member.id, ctx.author.id, reason)
    case_num = await bot.db.add_case(ctx.guild.id, "warn", ctx.author.id, member.id, reason)
    await ctx.send(f"⚠️ {member.mention} has been warned (warn #{count}, case #{case_num}). Reason: {reason}")
    try:
        await member.send(f"You were warned in **{ctx.guild.name}**: {reason}")
    except discord.Forbidden:
        pass


@bot.command(name="warnings")
@commands.has_permissions(moderate_members=True)
async def warnings_cmd(ctx: commands.Context, member: discord.Member):
    warns = await bot.db.get_warns(ctx.guild.id, member.id)
    if not warns:
        await ctx.send(f"{member.mention} has no warnings.")
        return
    lines = [f"`{w['created_at']:%Y-%m-%d}` — {w['reason']} (by <@{w['mod_id']}>)" for w in warns]
    embed = discord.Embed(title=f"Warnings for {member.display_name}", description="\n".join(lines))
    await ctx.send(embed=embed)


@bot.command(name="clearwarns")
@commands.has_permissions(moderate_members=True)
async def clearwarns_cmd(ctx: commands.Context, member: discord.Member):
    n = await bot.db.clear_warns(ctx.guild.id, member.id)
    await ctx.send(f"🧹 Cleared {n} warning(s) for {member.mention}.")


@bot.command(name="case")
@commands.has_permissions(moderate_members=True)
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
@commands.has_permissions(moderate_members=True)
async def cases_cmd(ctx: commands.Context, member: discord.Member):
    cases = await bot.db.get_user_cases(ctx.guild.id, member.id)
    if not cases:
        await ctx.send(f"{member.mention} has no cases.")
        return
    lines = [f"`#{c['case_number']}` {c['action']} — {c['reason']}" for c in cases]
    embed = discord.Embed(title=f"Cases for {member.display_name}", description="\n".join(lines))
    await ctx.send(embed=embed)


@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
async def kick_cmd(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    await member.kick(reason=reason)
    await bot.db.add_case(ctx.guild.id, "kick", ctx.author.id, member.id, reason)
    await ctx.send(f"👋 Kicked {member.mention}. Reason: {reason}")


@bot.command(name="ban")
@commands.has_permissions(ban_members=True)
async def ban_cmd(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    await member.ban(reason=reason)
    await bot.db.add_case(ctx.guild.id, "ban", ctx.author.id, member.id, reason)
    await ctx.send(f"🔨 Banned {member.mention}. Reason: {reason}")


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


@bot.command(name="tempmute")
@commands.has_permissions(moderate_members=True)
async def tempmute_cmd(ctx: commands.Context, member: discord.Member, duration: str, *, reason: str = "No reason provided"):
    delta = _parse_duration(duration)
    if not delta:
        await ctx.send("⚠️ Invalid duration. Use formats like `10m`, `1h`, `1d`.")
        return
    unmute_at = datetime.now(timezone.utc) + delta
    try:
        await member.timeout(delta, reason=reason)
    except discord.Forbidden:
        await ctx.send("⛔ I don't have permission to timeout that member.")
        return
    await bot.db.add_tempmute(ctx.guild.id, member.id, ctx.author.id, reason, unmute_at)
    await bot.db.add_case(ctx.guild.id, "tempmute", ctx.author.id, member.id, reason)
    await ctx.send(f"🔇 Tempmuted {member.mention} for `{duration}`. Reason: {reason}")


@bot.command(name="tempban")
@commands.has_permissions(ban_members=True)
async def tempban_cmd(ctx: commands.Context, member: discord.Member, duration: str, *, reason: str = "No reason provided"):
    delta = _parse_duration(duration)
    if not delta:
        await ctx.send("⚠️ Invalid duration. Use formats like `1h`, `1d`, `1w`.")
        return
    unban_at = datetime.now(timezone.utc) + delta
    await member.ban(reason=reason)
    await bot.db.add_tempban(ctx.guild.id, member.id, ctx.author.id, reason, unban_at)
    await bot.db.add_case(ctx.guild.id, "tempban", ctx.author.id, member.id, reason)
    await ctx.send(f"⛔ Tempbanned {member.mention} for `{duration}`. Reason: {reason}")


# ═══════════════════════════════════════════════════════════════════════════════
#  COMMANDS — config
# ═══════════════════════════════════════════════════════════════════════════════

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
    await bot.db.update_config(ctx.guild.id, key, value)
    await ctx.send(f"✅ Set `{key}` = `{value}`")


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
