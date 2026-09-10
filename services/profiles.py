"""
Shared helpers for keeping the `profiles` table (a Discord member's
site identity: username, avatar, linked Roblox account) up to date.
Used by both cogs/team.py and cogs/matchmaking.py whenever the bot
touches a row that references a Discord user.
"""

from __future__ import annotations

import asyncpg
import discord

import database


def discord_username_of(member: discord.Member | discord.User) -> str:
    """Clean Discord username, matching the site's convention (never
    'name#0' - the new username system's default discriminator)."""
    discriminator = str(getattr(member, "discriminator", "0") or "0")
    name = str(getattr(member, "name", "") or "").replace("#0", "").lstrip("@")
    if discriminator and discriminator != "0":
        return f"{name}#{discriminator}"
    return name


async def upsert_profile_from_member(
    member: discord.Member | discord.User,
    *,
    roblox_username: str | None = None,
    roblox_user_id: int | str | None = None,
) -> asyncpg.Record | None:
    """Create or update this member's `profiles` row. Safe to call
    often - it's a cheap upsert keyed on discord_id."""
    if not database.is_ready():
        return None

    avatar_url: str | None
    try:
        avatar_url = str(member.display_avatar.url)
    except Exception:
        avatar_url = None

    global_name = getattr(member, "global_name", None) or getattr(member, "display_name", None)

    return await database.fetchone(
        """
        INSERT INTO profiles (discord_id, discord_username, discord_global_name, avatar_url, roblox_username, roblox_user_id)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (discord_id) DO UPDATE SET
            discord_username = EXCLUDED.discord_username,
            discord_global_name = EXCLUDED.discord_global_name,
            avatar_url = EXCLUDED.avatar_url,
            roblox_username = COALESCE(EXCLUDED.roblox_username, profiles.roblox_username),
            roblox_user_id = COALESCE(EXCLUDED.roblox_user_id, profiles.roblox_user_id)
        RETURNING *
        """,
        database.did(member.id),
        discord_username_of(member),
        global_name,
        avatar_url,
        roblox_username,
        database.did(roblox_user_id) if roblox_user_id else None,
    )


async def get_active_season_id() -> str | None:
    row = await database.fetchone("SELECT id FROM seasons WHERE is_active = true LIMIT 1")
    return str(row["id"]) if row else None
