from __future__ import annotations

from datetime import datetime, timezone, timedelta
import random
import asyncio
import math
import re

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands

import config
import database
from services import vip_data
from services.profiles import upsert_profile_from_member


# =========================
# CONFIG
# =========================

MATCH_ORGANIZER_ROLE_ID = config.MATCH_ORGANIZER_ROLE_ID
MATCHMAKING_CATEGORY_ID = config.MATCHMAKING_CATEGORY_ID
MM_RESULTS_CHANNEL_ID = config.MM_RESULTS_CHANNEL_ID
ELO_UPDATE_CHANNEL_ID = config.ELO_UPDATE_CHANNEL_ID

# Position system: the real CVR game's 4 roles, 6 players per team
# (12 total per match). Replaces the old placeholder Setter(2)/Spiker(4)
# per-team split from the previous bot.
ROLE_SETTER = "setter"
ROLE_OUTSIDE_HITTER = "outside_hitter"
ROLE_MIDDLE_BLOCKER = "middle_blocker"
ROLE_OPPOSITE_HITTER = "opposite_hitter"

# Display order used everywhere a queue/roster is listed.
ROLE_ORDER = [ROLE_SETTER, ROLE_OUTSIDE_HITTER, ROLE_MIDDLE_BLOCKER, ROLE_OPPOSITE_HITTER]

ROLE_LABELS = {
    ROLE_SETTER: "Setter",
    ROLE_OUTSIDE_HITTER: "Outside Hitter",
    ROLE_MIDDLE_BLOCKER: "Middle Blocker",
    ROLE_OPPOSITE_HITTER: "Opposite Hitter",
}

ROLE_SHORT = {
    ROLE_SETTER: "S",
    ROLE_OUTSIDE_HITTER: "OH",
    ROLE_MIDDLE_BLOCKER: "MB",
    ROLE_OPPOSITE_HITTER: "OP",
}

# Max per role across the WHOLE queue (both teams combined) - 1 Setter,
# 2 Outside Hitters, 2 Middle Blockers, 1 Opposite Hitter per team.
ROLE_MAX_TOTAL = {
    ROLE_SETTER: 2,
    ROLE_OUTSIDE_HITTER: 4,
    ROLE_MIDDLE_BLOCKER: 4,
    ROLE_OPPOSITE_HITTER: 2,
}

QUEUE_SIZE = sum(ROLE_MAX_TOTAL.values())  # 12

# SQL snippet to order players by role in a stable, consistent way.
ROLE_ORDER_SQL = (
    "CASE role_pref "
    "WHEN 'setter' THEN 0 "
    "WHEN 'outside_hitter' THEN 1 "
    "WHEN 'middle_blocker' THEN 2 "
    "WHEN 'opposite_hitter' THEN 3 "
    "ELSE 4 END"
)

# VIP-only queue channel (access is restricted via Discord role
# permissions on the channel itself - the bot does not need to check
# membership). Wins in a match opened here are worth double ELO, on
# top of the Golden Match multiplier and each winner's own VIP % bonus.
VIP_QUEUE_CHANNEL_ID = 1547095549264666654
VIP_QUEUE_ELO_MULTIPLIER = 2

BASE_WIN_ELO = 22
BASE_LOSS_ELO = -14
WMVP_BONUS = 6
LMVP_REDUCTION = 6
REPLACE_LEAVE_PENALTY = -10

SPECIAL_MATCH_CHANCE = 0.20
SPECIAL_MATCH_MULTIPLIER = 3
SPECIAL_MATCH_NAME = "🏆 GOLDEN MATCH"

# =========================
# BASIC HELPERS
# =========================

def now() -> datetime:
    return datetime.now(timezone.utc)


def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator


def has_role(member: discord.Member, role_id: int) -> bool:
    return any(role.id == role_id for role in member.roles)


def can_manage_season(member: discord.Member) -> bool:
    if is_admin(member):
        return True

    staff_ids = config.STAFF_APPROVER_ROLE_IDS
    member_role_ids = {role.id for role in member.roles}
    return any(role_id in member_role_ids for role_id in staff_ids)


def can_manage_matchmaking(member: discord.Member) -> bool:
    return is_admin(member) or has_role(member, MATCH_ORGANIZER_ROLE_ID)


def team_side_label(side: str) -> str:
    return "Team A" if side == "A" else "Team B"


def role_short(role_pref: str) -> str:
    return ROLE_SHORT.get(role_pref, (role_pref or "?")[:2].upper())


def role_label(role_pref: str) -> str:
    return ROLE_LABELS.get(role_pref, role_pref)


def is_vip_queue(match_row) -> bool:
    channel_id = match_row["queue_channel_id"] if match_row else None
    return bool(channel_id) and str(channel_id) == str(VIP_QUEUE_CHANNEL_ID)


async def get_vip_badges_for(discord_ids: list[str]) -> dict[str, str]:
    """Batch lookup for the leaderboard: {discord_id: ' 👑 VIP+'/' ⭐ VIP'}."""
    ids = [d for d in discord_ids if d]
    if not ids:
        return {}

    rows = await database.fetchall(
        """
        SELECT discord_id, tier FROM vip_subscriptions
        WHERE status = 'active' AND expires_at > now() AND discord_id = ANY($1::text[])
        """,
        ids,
    )

    badges = {}
    for row in rows:
        badges[row["discord_id"]] = " 👑 VIP+" if row["tier"] == "vip_plus" else " ⭐ VIP"
    return badges


# =========================
# DATA ACCESS (async, shared Postgres schema)
# =========================

async def get_active_season():
    return await database.fetchone(
        """
        SELECT * FROM mm_seasons
        WHERE is_active = true
        ORDER BY number DESC
        LIMIT 1
        """
    )


async def ensure_mm_player(discord_id: int):
    await database.execute(
        """
        INSERT INTO mm_players (discord_id)
        VALUES ($1)
        ON CONFLICT (discord_id) DO NOTHING
        """,
        database.did(discord_id),
    )


async def ensure_mm_season_player(season_number: int, discord_id: int):
    await database.execute(
        """
        INSERT INTO mm_season_players (season_number, discord_id)
        VALUES ($1, $2)
        ON CONFLICT (season_number, discord_id) DO NOTHING
        """,
        season_number, database.did(discord_id),
    )


async def get_match_by_number(match_number: int):
    return await database.fetchone(
        "SELECT * FROM mm_matches WHERE match_number = $1", match_number
    )


async def get_match_players(match_number: int):
    return await database.fetchall(
        """
        SELECT * FROM mm_match_players
        WHERE match_number = $1
        ORDER BY captain DESC, pick_order ASC, id ASC
        """,
        match_number,
    )


async def get_team_players(match_number: int, side: str):
    return await database.fetchall(
        """
        SELECT * FROM mm_match_players
        WHERE match_number = $1 AND team_side = $2
        ORDER BY captain DESC, pick_order ASC, id ASC
        """,
        match_number, side,
    )


async def get_available_players(match_number: int):
    return await database.fetchall(
        """
        SELECT * FROM mm_match_players
        WHERE match_number = $1 AND team_side IS NULL
        ORDER BY
            CASE role_pref WHEN 'setter' THEN 0 WHEN 'outside_hitter' THEN 1 WHEN 'middle_blocker' THEN 2 WHEN 'opposite_hitter' THEN 3 ELSE 4 END,
            id ASC
        """,
        match_number,
    )


async def count_team_role(match_number: int, side: str, role_pref: str) -> int:
    row = await database.fetchone(
        """
        SELECT COUNT(*) AS total
        FROM mm_match_players
        WHERE match_number = $1 AND team_side = $2 AND role_pref = $3
        """,
        match_number, side, role_pref,
    )
    return row["total"] if row else 0


async def assign_random_teams(match_number: int) -> None:
    """
    Splits the full queue into two random teams, one role at a time,
    so each team ends up with the right position mix (1 Setter, 2
    Outside Hitters, 2 Middle Blockers, 1 Opposite Hitter). Used when
    "Random Teams" wins the post-queue format vote, as an alternative
    to the normal captains-and-draft flow.

    Mirrors the same ceil(total/2)-per-team split the draft uses to
    handle VIP+ overflow (e.g. 3 Setters instead of 2): the extra
    player's side is picked at random rather than always favoring the
    same team.
    """
    for role in ROLE_ORDER:
        players = await database.fetchall(
            "SELECT discord_id FROM mm_match_players WHERE match_number = $1 AND role_pref = $2",
            match_number, role,
        )
        ids = [row["discord_id"] for row in players]
        random.shuffle(ids)

        half = len(ids) // 2
        remainder = len(ids) - 2 * half
        extra_to_a = bool(remainder) and random.random() < 0.5
        a_count = half + (1 if extra_to_a else 0)

        for discord_id in ids[:a_count]:
            await database.execute(
                "UPDATE mm_match_players SET team_side = 'A' WHERE match_number = $1 AND discord_id = $2",
                match_number, discord_id,
            )
        for discord_id in ids[a_count:]:
            await database.execute(
                "UPDATE mm_match_players SET team_side = 'B' WHERE match_number = $1 AND discord_id = $2",
                match_number, discord_id,
            )


async def get_captain_side(match_number: int, discord_id: int):
    row = await database.fetchone(
        """
        SELECT team_side FROM mm_match_players
        WHERE match_number = $1 AND discord_id = $2 AND captain = true
        """,
        match_number, database.did(discord_id),
    )
    return row["team_side"] if row else None


async def get_pick_count(match_number: int) -> int:
    row = await database.fetchone(
        """
        SELECT COUNT(*) AS total
        FROM mm_match_players
        WHERE match_number = $1 AND team_side IS NOT NULL AND captain = false
        """,
        match_number,
    )
    return row["total"] if row else 0


async def get_current_turn_side(match_row) -> str | None:
    available = await get_available_players(match_row["match_number"])
    if not available:
        return None

    picks_done = await get_pick_count(match_row["match_number"])
    first_side = "A" if match_row["first_picker_discord_id"] == match_row["captain1_discord_id"] else "B"
    second_side = "B" if first_side == "A" else "A"

    return first_side if picks_done % 2 == 0 else second_side


async def is_user_busy(discord_id: int) -> bool:
    row = await database.fetchone(
        """
        SELECT mp.id
        FROM mm_match_players mp
        JOIN mm_matches m ON m.match_number = mp.match_number
        WHERE mp.discord_id = $1
          AND m.status IN ('queue_open', 'team_format_vote', 'captains_pending', 'draft', 'ready_to_start', 'in_progress')
        LIMIT 1
        """,
        database.did(discord_id),
    )
    return row is not None


def format_elo_delta(delta: int) -> str:
    return f"+{delta}" if delta > 0 else str(delta)


def is_special_match(match_row) -> bool:
    return bool(match_row["is_special"]) if match_row is not None else False


def parse_final_score(final_score_text: str) -> tuple[list[tuple[int, int]] | None, str | None]:
    """
    Expected format:
    Team A - Team B for each set

    Valid examples:
    25-20
    25-20, 22-25, 15-11
    25-21 | 25-18
    25:21, 25:18
    25x21, 25x18
    """
    raw = final_score_text.strip()
    if not raw:
        return None, "Final Score cannot be empty."

    parts = [p.strip() for p in re.split(r"[,|\n;]+", raw) if p.strip()]
    if not parts:
        return None, "Invalid Final Score format."

    set_scores: list[tuple[int, int]] = []

    for part in parts:
        normalized = re.sub(r"\s*[xX:]\s*", "-", part)
        match = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})", normalized)
        if not match:
            return None, (
                "Invalid Final Score format. Use Team A - Team B for each set. "
                "Example: `25-20, 22-25, 15-11`"
            )

        a_score = int(match.group(1))
        b_score = int(match.group(2))

        if a_score == b_score:
            return None, "A set cannot end in a tie."

        if a_score < 0 or b_score < 0:
            return None, "Scores cannot be negative."

        set_scores.append((a_score, b_score))

    return set_scores, None


def count_set_wins(set_scores: list[tuple[int, int]]) -> tuple[int, int]:
    team_a_wins = 0
    team_b_wins = 0

    for a_score, b_score in set_scores:
        if a_score > b_score:
            team_a_wins += 1
        else:
            team_b_wins += 1

    return team_a_wins, team_b_wins


def get_margin_bonus(avg_margin: float) -> int:
    if avg_margin >= 15:
        return 8
    if avg_margin >= 11:
        return 6
    if avg_margin >= 7:
        return 4
    if avg_margin >= 4:
        return 2
    return 0


def calculate_match_team_deltas(
    set_scores: list[tuple[int, int]],
    winner_side: str
) -> tuple[dict | None, str | None]:
    team_a_wins, team_b_wins = count_set_wins(set_scores)

    if team_a_wins == team_b_wins:
        return None, "Final Score is tied in sets. A match must have a winner."

    actual_winner = "A" if team_a_wins > team_b_wins else "B"
    if actual_winner != winner_side:
        return None, (
            f"The selected winner team does not match the Final Score. "
            f"Score indicates Team {actual_winner} as winner."
        )

    total_margin = sum(abs(a - b) for a, b in set_scores)
    avg_margin = total_margin / len(set_scores)
    dominance_bonus = get_margin_bonus(avg_margin)

    winner_delta = BASE_WIN_ELO + dominance_bonus
    loser_delta = BASE_LOSS_ELO - round(dominance_bonus * 0.75)

    final_score_display = " | ".join(f"{a}-{b}" for a, b in set_scores)

    return {
        "team_a_sets": team_a_wins,
        "team_b_sets": team_b_wins,
        "avg_margin": avg_margin,
        "dominance_bonus": dominance_bonus,
        "winner_delta": winner_delta,
        "loser_delta": loser_delta,
        "final_score_display": final_score_display,
    }, None


# =========================
# EMBED HELPERS
# =========================

def mention_or_name(guild: discord.Guild | None, discord_id) -> str:
    discord_id = int(discord_id)
    if guild is None:
        return f"<@{discord_id}>"
    member = guild.get_member(discord_id)
    return member.mention if member else f"<@{discord_id}>"


async def build_queue_lines(guild: discord.Guild | None, match_number: int):
    rows = await database.fetchall(
        """
        SELECT * FROM mm_match_players
        WHERE match_number = $1
        ORDER BY
            CASE role_pref WHEN 'setter' THEN 0 WHEN 'outside_hitter' THEN 1 WHEN 'middle_blocker' THEN 2 WHEN 'opposite_hitter' THEN 3 ELSE 4 END,
            id ASC
        """,
        match_number,
    )

    grouped: dict[str, list[str]] = {role: [] for role in ROLE_ORDER}

    for row in rows:
        line = f"{mention_or_name(guild, row['discord_id'])} `[{role_short(row['role_pref'])}]`"
        grouped.setdefault(row["role_pref"], []).append(line)

    return grouped


def build_queue_sections(grouped: dict[str, list[str]]) -> str:
    sections = []
    for role in ROLE_ORDER:
        lines = grouped.get(role, [])
        sections.append(
            f"**{ROLE_LABELS[role]} ({len(lines)}/{ROLE_MAX_TOTAL[role]})**\n"
            f"{chr(10).join(lines) if lines else '—'}"
        )
    return "\n\n".join(sections)


async def build_queue_embed(guild: discord.Guild | None, match_row):
    grouped = await build_queue_lines(guild, match_row["match_number"])
    vip_queue = is_vip_queue(match_row)

    embed = discord.Embed(
        title=(
            f"CVR SA Matchmaking Queue #{match_row['match_number']}"
            + (" • VIP Queue (2x ELO on wins)" if vip_queue else "")
        ),
        description=build_queue_sections(grouped),
        color=discord.Color.gold() if vip_queue else discord.Color.blurple()
    )

    season = await get_active_season()
    embed.set_footer(text=f"CVR SA Matchmaking • Season {season['number']}" if season else "CVR SA Matchmaking")
    return embed


def build_team_format_vote_embed(match_row, random_votes: int, captains_votes: int, voted_count: int, total_players: int):
    embed = discord.Embed(
        title=f"Queue #{match_row['match_number']} • Team Format Vote",
        description=(
            "The queue is full! Vote how the two teams should be formed.\n"
            "Voting ends in 30 seconds, or as soon as everyone in the queue has voted.\n"
            "A tie defaults to **Captain Picks**."
        ),
        color=discord.Color.gold()
    )
    embed.add_field(name="🎲 Random Teams", value=str(random_votes), inline=True)
    embed.add_field(name="👑 Captain Picks", value=str(captains_votes), inline=True)
    embed.set_footer(text=f"{voted_count}/{total_players} voted")
    return embed


async def build_captains_embed(guild: discord.Guild | None, match_row):
    all_players = await database.fetchall(
        """
        SELECT * FROM mm_match_players
        WHERE match_number = $1
        ORDER BY
            CASE role_pref WHEN 'setter' THEN 0 WHEN 'outside_hitter' THEN 1 WHEN 'middle_blocker' THEN 2 WHEN 'opposite_hitter' THEN 3 ELSE 4 END,
            id ASC
        """,
        match_row["match_number"],
    )

    lines = []
    for row in all_players:
        suffix = f" [{role_short(row['role_pref'])}]"
        if match_row["captain1_discord_id"] == row["discord_id"]:
            suffix += " • CAPTAIN 1"
        elif match_row["captain2_discord_id"] == row["discord_id"]:
            suffix += " • CAPTAIN 2"

        lines.append(f"{mention_or_name(guild, row['discord_id'])}`{suffix}`")

    embed = discord.Embed(
        title=f"Queue #{match_row['match_number']} • Set Captains",
        description=(
            "The queue is now full.\n\n"
            "**Queued Players**\n"
            f"{chr(10).join(lines) if lines else '—'}\n\n"
            f"**Captain 1:** {mention_or_name(guild, match_row['captain1_discord_id']) if match_row['captain1_discord_id'] else 'Not selected'}\n"
            f"**Captain 2:** {mention_or_name(guild, match_row['captain2_discord_id']) if match_row['captain2_discord_id'] else 'Not selected'}"
        ),
        color=discord.Color.gold()
    )
    embed.set_footer(text="Only Match Organizer can choose captains")
    return embed


def build_team_lines(guild: discord.Guild | None, players, wmvp_id: str | None = None, lmvp_id: str | None = None):
    lines = []
    for row in players:
        tags = [role_short(row["role_pref"])]
        if row["captain"]:
            tags.append("CAP")
        if wmvp_id and row["discord_id"] == wmvp_id:
            tags.append("WMVP")
        if lmvp_id and row["discord_id"] == lmvp_id:
            tags.append("LMVP")

        lines.append(f"{mention_or_name(guild, row['discord_id'])} `[{', '.join(tags)}]`")

    return chr(10).join(lines) if lines else "—"


async def build_draft_embed(guild: discord.Guild | None, match_row):
    team_a = await get_team_players(match_row["match_number"], "A")
    team_b = await get_team_players(match_row["match_number"], "B")
    available = await get_available_players(match_row["match_number"])
    current_turn_side = await get_current_turn_side(match_row)

    available_lines = [
        f"{mention_or_name(guild, row['discord_id'])} `[{role_short(row['role_pref'])}]`"
        for row in available
    ]

    if not current_turn_side:
        turn_text = "Draft complete"
    else:
        turn_text = f"{team_side_label(current_turn_side)} Captain"

    embed = discord.Embed(
        title=f"Queue #{match_row['match_number']} • Draft Phase",
        color=discord.Color.green()
    )
    embed.add_field(name="Team A", value=build_team_lines(guild, team_a), inline=False)
    embed.add_field(name="Team B", value=build_team_lines(guild, team_b), inline=False)
    embed.add_field(
        name=f"Available Players ({len(available)})",
        value=chr(10).join(available_lines) if available_lines else "—",
        inline=False
    )
    embed.add_field(name="Current Turn", value=turn_text, inline=False)

    first_picker = mention_or_name(guild, match_row["first_picker_discord_id"]) if match_row["first_picker_discord_id"] else "—"
    embed.set_footer(text=f"First pick: {first_picker}")
    return embed


async def build_ready_embed(guild: discord.Guild | None, match_row):
    team_a = await get_team_players(match_row["match_number"], "A")
    team_b = await get_team_players(match_row["match_number"], "B")

    embed = discord.Embed(
        title=f"Queue #{match_row['match_number']} • Teams Ready",
        description="All picks are complete. Match Organizer can now start the match.",
        color=discord.Color.blue()
    )
    embed.add_field(name="Team A", value=build_team_lines(guild, team_a), inline=False)
    embed.add_field(name="Team B", value=build_team_lines(guild, team_b), inline=False)
    return embed


async def build_match_started_embed(guild: discord.Guild | None, match_row):
    team_a = await get_team_players(match_row["match_number"], "A")
    team_b = await get_team_players(match_row["match_number"], "B")

    special = is_special_match(match_row)
    multiplier = match_row["special_multiplier"] or 1

    description = f"**Private Server Link**\n{match_row['private_server_link']}"

    if special:
        description = (
            f"## {SPECIAL_MATCH_NAME}\n"
            f"⚡ **This is a Special Match!**\n"
            f"The winning team will earn **{multiplier}x Elo**.\n\n"
            f"**Private Server Link**\n{match_row['private_server_link']}"
        )

    embed = discord.Embed(
        title=f"Match In Progress • #{match_row['match_number']}",
        description=description,
        color=discord.Color.gold() if special else discord.Color.dark_green()
    )

    embed.add_field(name="Team A", value=build_team_lines(guild, team_a), inline=False)
    embed.add_field(name="Team B", value=build_team_lines(guild, team_b), inline=False)

    if special:
        embed.add_field(
            name="Bonus Rule",
            value=f"Winner receives **{multiplier}x Elo** for this match.",
            inline=False
        )

    embed.set_footer(
        text="CVR SA Matchmaking • VIP Queue (2x ELO on wins)" if is_vip_queue(match_row) else "CVR SA Matchmaking"
    )
    return embed


async def build_result_embed(guild: discord.Guild | None, match_row):
    winners = await get_team_players(match_row["match_number"], match_row["winner_side"])
    losers = await get_team_players(match_row["match_number"], match_row["loser_side"])

    embed = discord.Embed(
        title=f"Match Result • #{match_row['match_number']}",
        color=discord.Color.purple()
    )

    special = is_special_match(match_row)
    multiplier = match_row["special_multiplier"] or 1

    if special:
        embed.color = discord.Color.gold()
        embed.description = (
            f"{SPECIAL_MATCH_NAME}\n"
            f"Winner team earned **{multiplier}x Elo** in this match."
        )

    final_score_text = match_row["final_score_text"]
    if final_score_text:
        embed.add_field(name="Final Score", value=final_score_text, inline=False)

    embed.add_field(
        name=f"{team_side_label(match_row['winner_side'])} • Winner",
        value=build_team_lines(guild, winners, wmvp_id=match_row["wmvp_discord_id"]),
        inline=False
    )
    embed.add_field(
        name=f"{team_side_label(match_row['loser_side'])} • Loser",
        value=build_team_lines(guild, losers, lmvp_id=match_row["lmvp_discord_id"]),
        inline=False
    )
    embed.set_footer(
        text="CVR SA Matchmaking Results • VIP Queue (2x ELO on wins)" if is_vip_queue(match_row) else "CVR SA Matchmaking Results"
    )
    return embed


async def build_elo_update_embed(guild: discord.Guild | None, match_row, elo_changes: list[dict]):
    winners = []
    losers = []

    for change in elo_changes:
        row = await database.fetchone(
            """
            SELECT team_side FROM mm_match_players
            WHERE match_number = $1 AND discord_id = $2
            """,
            match_row["match_number"], database.did(change["discord_id"]),
        )

        if not row:
            continue

        tags = []
        if change["is_win_mvp"]:
            tags.append("WMVP")
        if change["is_loss_mvp"]:
            tags.append("LMVP")
        if change.get("vip_tier"):
            tags.append(vip_data.vip_tier_label(change["vip_tier"]))

        suffix = f" ({', '.join(tags)})" if tags else ""
        line = (
            f"{mention_or_name(guild, change['discord_id'])}{suffix} • "
            f"`{format_elo_delta(change['delta'])}` → `{change['new_elo']}`"
        )

        if row["team_side"] == match_row["winner_side"]:
            winners.append(line)
        else:
            losers.append(line)

    embed = discord.Embed(
        title=f"ELO Update • Match #{match_row['match_number']}",
        color=discord.Color.orange()
    )

    special = is_special_match(match_row)
    multiplier = match_row["special_multiplier"] or 1

    if special:
        embed.color = discord.Color.gold()
        embed.description = (
            f"{SPECIAL_MATCH_NAME}\n"
            f"Winning team received **{multiplier}x Elo**."
        )

    if match_row["final_score_text"]:
        embed.add_field(name="Final Score", value=match_row["final_score_text"], inline=False)

    if special:
        embed.add_field(
            name="Special Bonus",
            value=f"Winner team had its base Elo multiplied by **x{multiplier}**.",
            inline=False
        )

    embed.add_field(
        name=f"{team_side_label(match_row['winner_side'])} • Gained",
        value="\n".join(winners) if winners else "—",
        inline=False
    )
    embed.add_field(
        name=f"{team_side_label(match_row['loser_side'])} • Lost",
        value="\n".join(losers) if losers else "—",
        inline=False
    )
    embed.set_footer(text="ELO after match finish")
    return embed


async def build_cancelled_embed(guild: discord.Guild | None, match_row, cancelled_by_id: int | None = None):
    grouped = await build_queue_lines(guild, match_row["match_number"])

    description = build_queue_sections(grouped) + "\n\n**Status:** Cancelled"

    if cancelled_by_id:
        description += f"\n**Cancelled by:** {mention_or_name(guild, cancelled_by_id)}"

    embed = discord.Embed(
        title=f"CVR SA Matchmaking Queue #{match_row['match_number']} • Cancelled",
        description=description,
        color=discord.Color.red()
    )
    embed.set_footer(text="CVR SA Matchmaking")
    return embed


async def build_cancelled_in_progress_embed(guild: discord.Guild | None, match_row, cancelled_by_id: int | None = None):
    team_a = await get_team_players(match_row["match_number"], "A")
    team_b = await get_team_players(match_row["match_number"], "B")

    embed = discord.Embed(
        title=f"Match Cancelled • #{match_row['match_number']}",
        description="This match was cancelled after being started.",
        color=discord.Color.red()
    )
    embed.add_field(name="Team A", value=build_team_lines(guild, team_a), inline=False)
    embed.add_field(name="Team B", value=build_team_lines(guild, team_b), inline=False)

    if cancelled_by_id:
        embed.add_field(name="Cancelled by", value=mention_or_name(guild, cancelled_by_id), inline=False)

    return embed


# =========================
# ELO / STATS UPDATE
# =========================

async def apply_match_result_to_player(
    discord_id: int,
    season_number: int | None,
    delta: int,
    is_win: bool,
    is_win_mvp: bool,
    is_loss_mvp: bool
):
    await ensure_mm_player(discord_id)

    player = await database.fetchone("SELECT * FROM mm_players WHERE discord_id = $1", database.did(discord_id))
    if not player:
        return None

    old_elo = player["elo"]
    new_elo = max(0, old_elo + delta)
    elo_gained = delta if delta > 0 else 0
    elo_lost = abs(delta) if delta < 0 else 0

    await database.execute(
        """
        UPDATE mm_players
        SET elo = $1,
            matches = matches + 1,
            wins = wins + $2,
            losses = losses + $3,
            win_mvp = win_mvp + $4,
            loss_mvp = loss_mvp + $5,
            elo_gained_total = elo_gained_total + $6,
            elo_lost_total = elo_lost_total + $7
        WHERE discord_id = $8
        """,
        new_elo,
        1 if is_win else 0,
        0 if is_win else 1,
        1 if is_win_mvp else 0,
        1 if is_loss_mvp else 0,
        elo_gained,
        elo_lost,
        database.did(discord_id),
    )

    if season_number is not None:
        await ensure_mm_season_player(season_number, discord_id)

        await database.execute(
            """
            UPDATE mm_season_players
            SET matches = matches + 1,
                wins = wins + $1,
                losses = losses + $2,
                win_mvp = win_mvp + $3,
                loss_mvp = loss_mvp + $4,
                elo_gained = elo_gained + $5,
                elo_lost = elo_lost + $6
            WHERE season_number = $7 AND discord_id = $8
            """,
            1 if is_win else 0,
            0 if is_win else 1,
            1 if is_win_mvp else 0,
            1 if is_loss_mvp else 0,
            elo_gained,
            elo_lost,
            season_number,
            database.did(discord_id),
        )

    return {
        "discord_id": database.did(discord_id),
        "old_elo": old_elo,
        "new_elo": new_elo,
        "delta": delta,
        "is_win": is_win,
        "is_win_mvp": is_win_mvp,
        "is_loss_mvp": is_loss_mvp,
    }


def get_member_label(guild: discord.Guild | None, discord_id, fallback: str | None = None) -> str:
    discord_id = int(discord_id)
    if guild is not None:
        member = guild.get_member(discord_id)
        if member:
            return member.display_name[:80]

    return (fallback or str(discord_id))[:80]


async def adjust_player_elo_only(discord_id: int, season_number: int | None, delta: int):
    await ensure_mm_player(discord_id)

    player = await database.fetchone("SELECT * FROM mm_players WHERE discord_id = $1", database.did(discord_id))
    if not player:
        return

    new_elo = max(0, player["elo"] + delta)
    elo_gained = delta if delta > 0 else 0
    elo_lost = abs(delta) if delta < 0 else 0

    await database.execute(
        """
        UPDATE mm_players
        SET elo = $1,
            elo_gained_total = elo_gained_total + $2,
            elo_lost_total = elo_lost_total + $3
        WHERE discord_id = $4
        """,
        new_elo, elo_gained, elo_lost, database.did(discord_id),
    )

    if season_number is not None:
        await ensure_mm_season_player(season_number, discord_id)
        await database.execute(
            """
            UPDATE mm_season_players
            SET elo_gained = elo_gained + $1,
                elo_lost = elo_lost + $2
            WHERE season_number = $3 AND discord_id = $4
            """,
            elo_gained, elo_lost, season_number, database.did(discord_id),
        )


async def replace_match_player(match_number: int, old_discord_id: int, new_discord_id: int):
    old_row = await database.fetchone(
        "SELECT * FROM mm_match_players WHERE match_number = $1 AND discord_id = $2",
        match_number, database.did(old_discord_id),
    )

    if not old_row:
        return False, "Old player not found."

    existing_new = await database.fetchone(
        "SELECT * FROM mm_match_players WHERE match_number = $1 AND discord_id = $2",
        match_number, database.did(new_discord_id),
    )

    if existing_new:
        return False, "New player is already in this match."

    await database.execute(
        "UPDATE mm_match_players SET discord_id = $1 WHERE match_number = $2 AND discord_id = $3",
        database.did(new_discord_id), match_number, database.did(old_discord_id),
    )

    return True, None


# =========================
# COMPONENTS
# =========================
#
# discord.ui component __init__ methods cannot be async (Discord.py does
# not support it), so every component that needs DB data fetches it in
# the *caller's* async context first, then passes it in as plain data.

class JoinQueueView(discord.ui.View):
    def __init__(self, cog: "MatchmakingCog", match_number: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.match_number = match_number

    async def refresh_labels(self):
        grouped = await build_queue_lines(None, self.match_number)
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                for role in ROLE_ORDER:
                    if item.custom_id == f"mm_join_{role}_{self.match_number}":
                        count = len(grouped.get(role, []))
                        item.label = f"Join {ROLE_LABELS[role]} ({count}/{ROLE_MAX_TOTAL[role]})"

    async def refresh_message(self, interaction: discord.Interaction):
        await self.refresh_labels()

        match_row = await get_match_by_number(self.match_number)
        if not match_row:
            return

        total_row = await database.fetchone(
            "SELECT COUNT(*) AS total FROM mm_match_players WHERE match_number = $1",
            self.match_number,
        )
        total = total_row["total"] if total_row else 0

        # Once full, flip status to team_format_vote exactly once - this
        # kicks off the Random Teams vs Captain Picks vote before
        # deciding how to reach captains_pending/ready_to_start.
        if total >= QUEUE_SIZE and match_row["status"] == "queue_open":
            await database.execute(
                """
                UPDATE mm_matches
                SET status = 'team_format_vote'
                WHERE match_number = $1 AND status = 'queue_open'
                """,
                self.match_number,
            )
            match_row = await get_match_by_number(self.match_number)

        if not match_row:
            return

        if match_row["status"] == "team_format_vote":
            vote_view = TeamFormatVoteView(self.cog, self.match_number, total)
            embed = build_team_format_vote_embed(match_row, 0, 0, 0, total)
            await interaction.message.edit(embed=embed, view=vote_view)
            vote_view.message = interaction.message
            return

        if match_row["status"] == "captains_pending":
            await interaction.message.edit(
                embed=await build_captains_embed(interaction.guild, match_row),
                view=CaptainSetupView(self.cog, self.match_number)
            )
            return

        if match_row["status"] == "queue_open":
            await interaction.message.edit(
                embed=await build_queue_embed(interaction.guild, match_row),
                view=self
            )

    async def _join_role(self, interaction: discord.Interaction, role_pref: str):
        if not isinstance(interaction.user, discord.Member):
            return

        # This does several sequential database calls (and possibly a
        # VIP lookup) before it can know whether the join even
        # succeeds. Ack immediately so a slow round-trip to Supabase
        # never shows "This interaction failed" on the button click.
        await interaction.response.defer()

        lock = self.cog.get_match_lock(self.match_number)

        async with lock:
            match_row = await get_match_by_number(self.match_number)
            if not match_row or match_row["status"] != "queue_open":
                await interaction.followup.send("This queue is no longer open.", ephemeral=True)
                return

            existing_row = await database.fetchone(
                "SELECT * FROM mm_match_players WHERE match_number = $1 AND discord_id = $2",
                self.match_number, database.did(interaction.user.id),
            )
            if existing_row:
                await interaction.followup.send("You are already in this queue.", ephemeral=True)
                return

            if await is_user_busy(interaction.user.id):
                await interaction.followup.send("You are already in another active queue/match.", ephemeral=True)
                return

            count_row = await database.fetchone(
                "SELECT COUNT(*) AS total FROM mm_match_players WHERE match_number = $1 AND role_pref = $2",
                self.match_number, role_pref,
            )
            role_count = count_row["total"] if count_row else 0

            vip = await vip_data.get_active_vip(interaction.user.id)
            is_vip_plus = bool(vip and vip["tier"] == "vip_plus")

            if role_count >= ROLE_MAX_TOTAL[role_pref]:
                if not is_vip_plus:
                    await interaction.followup.send(
                        f"The {ROLE_LABELS[role_pref]} queue is already full.", ephemeral=True
                    )
                    return

                # VIP+ priority: can take a spot in a position that's
                # already full, but only while the overall queue still
                # has an open slot somewhere (never exceeds QUEUE_SIZE).
                total_row = await database.fetchone(
                    "SELECT COUNT(*) AS total FROM mm_match_players WHERE match_number = $1",
                    self.match_number,
                )
                total = total_row["total"] if total_row else 0
                if total >= QUEUE_SIZE:
                    await interaction.followup.send("This queue is already full.", ephemeral=True)
                    return

            priority_weight = await vip_data.get_captain_priority_weight(interaction.user.id)
            await upsert_profile_from_member(interaction.user)

            try:
                await database.execute(
                    """
                    INSERT INTO mm_match_players (
                        match_number, discord_id, role_pref, team_side, captain, pick_order, priority_weight
                    )
                    VALUES ($1, $2, $3, NULL, false, NULL, $4)
                    """,
                    self.match_number, database.did(interaction.user.id), role_pref, priority_weight,
                )
            except asyncpg.UniqueViolationError:
                await interaction.followup.send("You have already joined this queue.", ephemeral=True)
                return

            await self.refresh_message(interaction)

    @discord.ui.button(label="Join Setter (0/2)", style=discord.ButtonStyle.primary, custom_id="temp_setter", row=0)
    async def join_setter(self, interaction: discord.Interaction, button: discord.ui.Button):
        button.custom_id = f"mm_join_{ROLE_SETTER}_{self.match_number}"
        await self._join_role(interaction, ROLE_SETTER)

    @discord.ui.button(label="Join Outside Hitter (0/4)", style=discord.ButtonStyle.success, custom_id="temp_oh", row=0)
    async def join_outside_hitter(self, interaction: discord.Interaction, button: discord.ui.Button):
        button.custom_id = f"mm_join_{ROLE_OUTSIDE_HITTER}_{self.match_number}"
        await self._join_role(interaction, ROLE_OUTSIDE_HITTER)

    @discord.ui.button(label="Join Middle Blocker (0/4)", style=discord.ButtonStyle.success, custom_id="temp_mb", row=1)
    async def join_middle_blocker(self, interaction: discord.Interaction, button: discord.ui.Button):
        button.custom_id = f"mm_join_{ROLE_MIDDLE_BLOCKER}_{self.match_number}"
        await self._join_role(interaction, ROLE_MIDDLE_BLOCKER)

    @discord.ui.button(label="Join Opposite Hitter (0/2)", style=discord.ButtonStyle.primary, custom_id="temp_op", row=1)
    async def join_opposite_hitter(self, interaction: discord.Interaction, button: discord.ui.Button):
        button.custom_id = f"mm_join_{ROLE_OPPOSITE_HITTER}_{self.match_number}"
        await self._join_role(interaction, ROLE_OPPOSITE_HITTER)

    @discord.ui.button(
        label="Leave Queue",
        style=discord.ButtonStyle.danger,
        custom_id="temp3",
        row=2,
    )
    async def leave_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        button.custom_id = f"mm_leave_queue_{self.match_number}"

        # Ack immediately - two DB round-trips plus a delete precede the
        # message refresh below.
        await interaction.response.defer()

        lock = self.cog.get_match_lock(self.match_number)

        async with lock:
            match_row = await get_match_by_number(self.match_number)
            if not match_row or match_row["status"] != "queue_open":
                await interaction.followup.send("This queue is no longer open.", ephemeral=True)
                return

            row = await database.fetchone(
                "SELECT * FROM mm_match_players WHERE match_number = $1 AND discord_id = $2",
                self.match_number, database.did(interaction.user.id),
            )

            if not row:
                await interaction.followup.send("You are not in this queue.", ephemeral=True)
                return

            await database.execute(
                "DELETE FROM mm_match_players WHERE match_number = $1 AND discord_id = $2",
                self.match_number, database.did(interaction.user.id),
            )

            await self.refresh_message(interaction)


class TeamFormatVoteView(discord.ui.View):
    """
    Shown once the queue fills, before captains/draft. Everyone in the
    queue votes Random Teams vs Captain Picks; resolves as soon as
    every queued player has voted, or after 30 seconds, whichever
    comes first. A tie (including nobody voting at all) defaults to
    Captain Picks, matching the format the queue always used before
    this vote existed.
    """

    def __init__(self, cog: "MatchmakingCog", match_number: int, total_players: int):
        super().__init__(timeout=30)
        self.cog = cog
        self.match_number = match_number
        self.total_players = total_players
        self.votes: dict[str, str] = {}
        self.message: discord.Message | None = None
        self.resolved = False

        # Unique per match_number, same convention as JoinQueueView's
        # join buttons - avoids any custom_id collision between two
        # queues reaching this vote at the same time.
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                if item.custom_id == "mm_vote_random":
                    item.custom_id = f"mm_vote_random_{match_number}"
                elif item.custom_id == "mm_vote_captains":
                    item.custom_id = f"mm_vote_captains_{match_number}"

    def _tally(self) -> tuple[int, int]:
        random_votes = sum(1 for v in self.votes.values() if v == "random")
        captains_votes = sum(1 for v in self.votes.values() if v == "captains")
        return random_votes, captains_votes

    async def _register_vote(self, interaction: discord.Interaction, choice: str):
        if not isinstance(interaction.user, discord.Member):
            return

        # Ack immediately - up to two DB round-trips happen before the
        # vote is even recorded below.
        await interaction.response.defer()

        lock = self.cog.get_match_lock(self.match_number)

        async with lock:
            if self.resolved:
                await interaction.followup.send("Voting has already ended.", ephemeral=True)
                return

            match_row = await get_match_by_number(self.match_number)
            if not match_row or match_row["status"] != "team_format_vote":
                await interaction.followup.send("Voting is no longer active.", ephemeral=True)
                return

            player_row = await database.fetchone(
                "SELECT 1 AS present FROM mm_match_players WHERE match_number = $1 AND discord_id = $2",
                self.match_number, database.did(interaction.user.id),
            )
            if not player_row:
                await interaction.followup.send("Only players in this queue can vote.", ephemeral=True)
                return

            self.votes[database.did(interaction.user.id)] = choice

            if len(self.votes) >= self.total_players:
                await self._resolve(interaction.guild)
                return

            if self.message:
                random_votes, captains_votes = self._tally()
                embed = build_team_format_vote_embed(
                    match_row, random_votes, captains_votes, len(self.votes), self.total_players
                )
                try:
                    await self.message.edit(embed=embed, view=self)
                except discord.HTTPException:
                    pass

    @discord.ui.button(label="Random Teams", style=discord.ButtonStyle.success, custom_id="mm_vote_random")
    async def vote_random(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._register_vote(interaction, "random")

    @discord.ui.button(label="Captain Picks", style=discord.ButtonStyle.primary, custom_id="mm_vote_captains")
    async def vote_captains(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._register_vote(interaction, "captains")

    async def on_timeout(self):
        lock = self.cog.get_match_lock(self.match_number)
        async with lock:
            if self.resolved:
                return
            guild = self.cog.bot.get_guild(config.GUILD_ID)
            await self._resolve(guild)

    async def _resolve(self, guild: discord.Guild | None):
        # Caller must already hold self.cog.get_match_lock(self.match_number).
        if self.resolved:
            return
        self.resolved = True
        self.stop()

        match_row = await get_match_by_number(self.match_number)
        if not match_row or match_row["status"] != "team_format_vote":
            return

        random_votes, captains_votes = self._tally()
        use_random = random_votes > captains_votes

        if use_random:
            await assign_random_teams(self.match_number)
            await database.execute(
                "UPDATE mm_matches SET status = 'ready_to_start' WHERE match_number = $1",
                self.match_number,
            )
            final_match = await get_match_by_number(self.match_number)
            embed = await build_ready_embed(guild, final_match)
            view = StartMatchView(self.cog, self.match_number)
        else:
            await database.execute(
                "UPDATE mm_matches SET status = 'captains_pending' WHERE match_number = $1",
                self.match_number,
            )
            final_match = await get_match_by_number(self.match_number)
            embed = await build_captains_embed(guild, final_match)
            view = CaptainSetupView(self.cog, self.match_number)

        if self.message:
            try:
                await self.message.edit(embed=embed, view=view)
            except discord.HTTPException:
                pass


class CaptainPickSelect(discord.ui.Select):
    def __init__(self, cog: "MatchmakingCog", match_number: int, slot: int, options: list[discord.SelectOption]):
        self.cog = cog
        self.match_number = match_number
        self.slot = slot

        super().__init__(
            placeholder=f"Select Captain {slot}",
            min_values=1,
            max_values=1,
            options=options[:25]
        )

    @staticmethod
    async def build_options(match_row, guild: discord.Guild | None) -> list[discord.SelectOption]:
        all_players = await database.fetchall(
            """
            SELECT * FROM mm_match_players
            WHERE match_number = $1
            ORDER BY
                priority_weight DESC,
                CASE role_pref WHEN 'setter' THEN 0 WHEN 'outside_hitter' THEN 1 WHEN 'middle_blocker' THEN 2 WHEN 'opposite_hitter' THEN 3 ELSE 4 END,
                id ASC
            """,
            match_row["match_number"],
        )

        selected_ids = {match_row["captain1_discord_id"], match_row["captain2_discord_id"]}
        selected_ids.discard(None)

        # VIP/VIP+ players (weight > 0, captured at the moment they
        # joined the queue) show up first and marked with a star - the
        # VIP plan's "priority to be drawn as captain" benefit.
        options = []
        for row in all_players:
            if row["discord_id"] in selected_ids:
                continue

            member_name = get_member_label(guild, row["discord_id"])
            role_name = role_label(row["role_pref"])
            weight = row["priority_weight"] or 0
            label = f"⭐ {member_name}" if weight > 0 else member_name

            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=row["discord_id"],
                    description=(f"{role_name} • VIP priority" if weight > 0 else role_name)[:100],
                )
            )

        return options

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member):
            return

        if not can_manage_matchmaking(interaction.user):
            await interaction.response.send_message("Only Match Organizer can set captains.", ephemeral=True)
            return

        # This can chain up to five sequential DB round-trips (plus a
        # message fetch/edit) once both captains are set - ack right away.
        await interaction.response.defer(ephemeral=True)

        lock = self.cog.get_match_lock(self.match_number)

        async with lock:
            match_row = await get_match_by_number(self.match_number)
            if not match_row or match_row["status"] != "captains_pending":
                await interaction.followup.send("This captain setup is no longer active.", ephemeral=True)
                return

            selected_discord_id = self.values[0]

            column = "captain1_discord_id" if self.slot == 1 else "captain2_discord_id"
            await database.execute(
                f"UPDATE mm_matches SET {column} = $1 WHERE match_number = $2",
                selected_discord_id, self.match_number,
            )

            match_row = await get_match_by_number(self.match_number)

            if match_row and match_row["captain1_discord_id"] and match_row["captain2_discord_id"]:
                first_picker = random.choice([match_row["captain1_discord_id"], match_row["captain2_discord_id"]])

                await database.execute(
                    "UPDATE mm_matches SET first_picker_discord_id = $1, status = 'draft' WHERE match_number = $2",
                    first_picker, self.match_number,
                )

                await database.execute(
                    "UPDATE mm_match_players SET team_side = 'A', captain = true, pick_order = 0 WHERE match_number = $1 AND discord_id = $2",
                    self.match_number, match_row["captain1_discord_id"],
                )

                await database.execute(
                    "UPDATE mm_match_players SET team_side = 'B', captain = true, pick_order = 0 WHERE match_number = $1 AND discord_id = $2",
                    self.match_number, match_row["captain2_discord_id"],
                )

                updated = await get_match_by_number(self.match_number)

                if interaction.guild and updated and updated["queue_channel_id"] and updated["queue_message_id"]:
                    channel = interaction.guild.get_channel(int(updated["queue_channel_id"]))
                    if isinstance(channel, discord.TextChannel):
                        try:
                            queue_message = await channel.fetch_message(int(updated["queue_message_id"]))
                            available_options = await get_available_players(self.match_number)
                            await queue_message.edit(
                                embed=await build_draft_embed(interaction.guild, updated),
                                view=DraftView(self.cog, self.match_number, available_options)
                            )
                        except discord.HTTPException:
                            pass

                await interaction.followup.send(
                    f"Captain {self.slot} set successfully.",
                    ephemeral=True
                )
                return

            await interaction.followup.send(
                f"Captain {self.slot} set successfully.",
                ephemeral=True
            )


class CaptainPickView(discord.ui.View):
    def __init__(self, cog: "MatchmakingCog", match_number: int, slot: int, options: list[discord.SelectOption]):
        super().__init__(timeout=120)
        self.add_item(CaptainPickSelect(cog, match_number, slot, options))


class CaptainSetupView(discord.ui.View):
    def __init__(self, cog: "MatchmakingCog", match_number: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.match_number = match_number

    @discord.ui.button(label="Set Captain 1", style=discord.ButtonStyle.primary)
    async def set_captain_1(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not can_manage_matchmaking(interaction.user):
            await interaction.response.send_message("Only Match Organizer can set captains.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        match_row = await get_match_by_number(self.match_number)
        options = await CaptainPickSelect.build_options(match_row, interaction.guild)

        await interaction.followup.send(
            "Choose Captain 1:",
            view=CaptainPickView(self.cog, self.match_number, 1, options),
            ephemeral=True
        )

    @discord.ui.button(label="Set Captain 2", style=discord.ButtonStyle.secondary)
    async def set_captain_2(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not can_manage_matchmaking(interaction.user):
            await interaction.response.send_message("Only Match Organizer can set captains.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        match_row = await get_match_by_number(self.match_number)
        options = await CaptainPickSelect.build_options(match_row, interaction.guild)

        await interaction.followup.send(
            "Choose Captain 2:",
            view=CaptainPickView(self.cog, self.match_number, 2, options),
            ephemeral=True
        )


class PickPlayerButton(discord.ui.Button):
    def __init__(self, cog: "MatchmakingCog", match_number: int, player_discord_id: str, label_text: str, row_position: int):
        super().__init__(
            label=label_text[:80],
            style=discord.ButtonStyle.primary,
            row=row_position
        )
        self.cog = cog
        self.match_number = match_number
        self.player_discord_id = player_discord_id

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member):
            return

        # A draft pick can chain up to ~8 sequential DB round-trips
        # before the embed/view are ready to show - ack immediately so
        # a slow moment never shows "This interaction failed".
        await interaction.response.defer()

        match_row = await get_match_by_number(self.match_number)
        if not match_row or match_row["status"] != "draft":
            await interaction.followup.send("This draft is no longer active.", ephemeral=True)
            return

        captain_side = await get_captain_side(self.match_number, interaction.user.id)
        if not captain_side:
            await interaction.followup.send("Only the selected captains can pick players.", ephemeral=True)
            return

        current_turn_side = await get_current_turn_side(match_row)
        if captain_side != current_turn_side:
            await interaction.followup.send("It is not your turn to pick.", ephemeral=True)
            return

        player_row = await database.fetchone(
            "SELECT * FROM mm_match_players WHERE match_number = $1 AND discord_id = $2 AND team_side IS NULL",
            self.match_number, self.player_discord_id,
        )
        if not player_row:
            await interaction.followup.send("This player is no longer available.", ephemeral=True)
            return

        role_pref = player_row["role_pref"]

        # Normally this equals ROLE_MAX_TOTAL[role_pref] exactly (the
        # queue caps enforce that on the way in). VIP+ priority joining
        # can let a role go over its normal total though - in that case
        # split it as evenly as possible between the two teams (ceil/2)
        # instead of hard-blocking the extra player from ever being
        # draftable, which would otherwise stall the draft forever.
        total_role_row = await database.fetchone(
            "SELECT COUNT(*) AS total FROM mm_match_players WHERE match_number = $1 AND role_pref = $2",
            self.match_number, role_pref,
        )
        total_role_in_queue = total_role_row["total"] if total_role_row else ROLE_MAX_TOTAL.get(role_pref, 0)
        max_role_count = math.ceil(total_role_in_queue / 2) if total_role_in_queue else 0
        current_role_count = await count_team_role(self.match_number, captain_side, role_pref)

        if current_role_count >= max_role_count:
            await interaction.followup.send(
                f"Your team already has the maximum number of {role_label(role_pref)}s.",
                ephemeral=True
            )
            return

        pick_order = await get_pick_count(self.match_number) + 1

        await database.execute(
            "UPDATE mm_match_players SET team_side = $1, pick_order = $2 WHERE match_number = $3 AND discord_id = $4",
            captain_side, pick_order, self.match_number, self.player_discord_id,
        )

        remaining = await get_available_players(self.match_number)

        if not remaining:
            await database.execute(
                "UPDATE mm_matches SET status = 'ready_to_start' WHERE match_number = $1",
                self.match_number,
            )
            final_match = await get_match_by_number(self.match_number)

            await interaction.edit_original_response(
                embed=await build_ready_embed(interaction.guild, final_match),
                view=StartMatchView(self.cog, self.match_number)
            )
            return

        updated = await get_match_by_number(self.match_number)
        options = await get_available_players(self.match_number)
        await interaction.edit_original_response(
            embed=await build_draft_embed(interaction.guild, updated),
            view=DraftView(self.cog, self.match_number, options)
        )


class DraftView(discord.ui.View):
    def __init__(self, cog: "MatchmakingCog", match_number: int, available_players: list):
        super().__init__(timeout=None)
        self.cog = cog
        self.match_number = match_number

        guild = cog.bot.get_guild(config.GUILD_ID)

        for index, row in enumerate(available_players[:25]):
            member_name = get_member_label(guild, row["discord_id"])
            label = f"{member_name} [{role_short(row['role_pref'])}]"
            self.add_item(
                PickPlayerButton(
                    cog=cog,
                    match_number=match_number,
                    player_discord_id=row["discord_id"],
                    label_text=label,
                    row_position=min(index // 5, 4)
                )
            )


class PrivateServerModal(discord.ui.Modal, title="Start Match"):
    private_server_link = discord.ui.TextInput(
        label="Private Server Link",
        style=discord.TextStyle.paragraph,
        required=True,
        placeholder="Paste the game's private server link here..."
    )

    def __init__(self, cog: "MatchmakingCog", match_number: int):
        super().__init__()
        self.cog = cog
        self.match_number = match_number

    async def on_submit(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member):
            return

        if not can_manage_matchmaking(interaction.user):
            await interaction.response.send_message("Only Match Organizer can start the match.", ephemeral=True)
            return

        # Creating three Discord channels below (one text + two voice)
        # plus several DB round-trips easily takes well over 3 seconds -
        # ack immediately.
        await interaction.response.defer()

        match_row = await get_match_by_number(self.match_number)
        if not match_row or match_row["status"] != "ready_to_start":
            await interaction.followup.send("This match is not ready to be started.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Guild not found.", ephemeral=True)
            return

        category = guild.get_channel(MATCHMAKING_CATEGORY_ID)
        if not isinstance(category, discord.CategoryChannel):
            await interaction.followup.send("Matchmaking category not found.", ephemeral=True)
            return

        match_organizer_role = guild.get_role(MATCH_ORGANIZER_ROLE_ID)

        team_a = await get_team_players(self.match_number, "A")
        team_b = await get_team_players(self.match_number, "B")

        overwrites_text = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }

        if match_organizer_role:
            overwrites_text[match_organizer_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True
            )

        for row in team_a + team_b:
            member = guild.get_member(int(row["discord_id"]))
            if member:
                overwrites_text[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        text_channel = await guild.create_text_channel(
            name=f"mm-{self.match_number}",
            category=category,
            overwrites=overwrites_text,
            reason=f"Matchmaking #{self.match_number} started by {interaction.user}"
        )

        overwrites_team_a = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                manage_channels=True,
                move_members=True
            ),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                move_members=True
            ),
        }

        if match_organizer_role:
            overwrites_team_a[match_organizer_role] = discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                move_members=True,
                speak=True
            )

        overwrites_team_b = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                manage_channels=True,
                move_members=True
            ),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                move_members=True
            ),
        }

        if match_organizer_role:
            overwrites_team_b[match_organizer_role] = discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                move_members=True,
                speak=True
            )

        for row in team_a:
            member = guild.get_member(int(row["discord_id"]))
            if member:
                overwrites_team_a[member] = discord.PermissionOverwrite(view_channel=True, connect=True)

        for row in team_b:
            member = guild.get_member(int(row["discord_id"]))
            if member:
                overwrites_team_b[member] = discord.PermissionOverwrite(view_channel=True, connect=True)

        team_a_voice = await guild.create_voice_channel(
            name=f"MM #{self.match_number} • Team A",
            category=category,
            overwrites=overwrites_team_a,
            reason=f"Matchmaking #{self.match_number} Team A voice"
        )

        team_b_voice = await guild.create_voice_channel(
            name=f"MM #{self.match_number} • Team B",
            category=category,
            overwrites=overwrites_team_b,
            reason=f"Matchmaking #{self.match_number} Team B voice"
        )

        is_special = random.random() < SPECIAL_MATCH_CHANCE
        special_multiplier = SPECIAL_MATCH_MULTIPLIER if is_special else 1

        await database.execute(
            """
            UPDATE mm_matches
            SET status = 'in_progress',
                private_server_link = $1,
                text_channel_id = $2,
                team_a_voice_id = $3,
                team_b_voice_id = $4,
                is_special = $5,
                special_multiplier = $6,
                started_at = $7
            WHERE match_number = $8
            """,
            str(self.private_server_link),
            database.did(text_channel.id),
            database.did(team_a_voice.id),
            database.did(team_b_voice.id),
            is_special,
            special_multiplier,
            now(),
            self.match_number,
        )

        updated = await get_match_by_number(self.match_number)

        view = InProgressMatchView(self.cog, self.match_number)

        await text_channel.send(
            embed=await build_match_started_embed(guild, updated),
            view=view
        )

        await interaction.edit_original_response(
            embed=await build_match_started_embed(guild, updated),
            view=view
        )


class StartMatchView(discord.ui.View):
    def __init__(self, cog: "MatchmakingCog", match_number: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.match_number = match_number

    @discord.ui.button(label="Start Match", style=discord.ButtonStyle.success)
    async def start_match(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not can_manage_matchmaking(interaction.user):
            await interaction.response.send_message("Only Match Organizer can start the match.", ephemeral=True)
            return

        await interaction.response.send_modal(PrivateServerModal(self.cog, self.match_number))


class InProgressMatchView(discord.ui.View):
    def __init__(self, cog: "MatchmakingCog", match_number: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.match_number = match_number

    @discord.ui.button(label="Replace Player", style=discord.ButtonStyle.primary)
    async def replace_player(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not can_manage_matchmaking(interaction.user):
            await interaction.response.send_message("Only Match Organizer can replace players.", ephemeral=True)
            return

        players = await get_match_players(self.match_number)
        await interaction.response.send_message(
            "Choose the player to replace:",
            view=ReplacePlayerPickView(self.cog, self.match_number, players, interaction.guild),
            ephemeral=True
        )

    @discord.ui.button(label="Finish Match", style=discord.ButtonStyle.success)
    async def finish_match(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not can_manage_matchmaking(interaction.user):
            await interaction.response.send_message("Only Match Organizer can finish the match.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Choose Winner Team:",
            view=FinishWinnerTeamView(self.cog, self.match_number),
            ephemeral=True
        )


class ReplacePlayerModal(discord.ui.Modal, title="Replace Player"):
    new_player = discord.ui.TextInput(
        label="New player mention or ID",
        required=True,
        placeholder="@user or user id"
    )

    def __init__(self, cog: "MatchmakingCog", match_number: int, old_discord_id: str):
        super().__init__()
        self.cog = cog
        self.match_number = match_number
        self.old_discord_id = old_discord_id

    async def on_submit(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not can_manage_matchmaking(interaction.user):
            await interaction.response.send_message("Only Match Organizer can replace players.", ephemeral=True)
            return

        # Replacing a player chains several DB round-trips plus multiple
        # Discord channel-permission edits - ack immediately.
        await interaction.response.defer(ephemeral=True)

        match_row = await get_match_by_number(self.match_number)
        if not match_row or match_row["status"] != "in_progress":
            await interaction.followup.send("This match is not in progress.", ephemeral=True)
            return

        raw = str(self.new_player).strip()
        new_member = None

        if interaction.guild:
            if raw.startswith("<@") and raw.endswith(">"):
                cleaned = raw.replace("<@", "").replace("!", "").replace(">", "")
                if cleaned.isdigit():
                    new_member = interaction.guild.get_member(int(cleaned))
            elif raw.isdigit():
                new_member = interaction.guild.get_member(int(raw))

        if not new_member:
            await interaction.followup.send("Could not find that member in this server.", ephemeral=True)
            return

        if await is_user_busy(new_member.id):
            await interaction.followup.send("This player is already in another active queue/match.", ephemeral=True)
            return

        old_row = await database.fetchone(
            "SELECT * FROM mm_match_players WHERE match_number = $1 AND discord_id = $2",
            self.match_number, self.old_discord_id,
        )
        if not old_row:
            await interaction.followup.send("Old player not found in this match.", ephemeral=True)
            return

        season_number = match_row["season_number"]
        old_discord_id_int = int(self.old_discord_id)

        ok, error = await replace_match_player(self.match_number, old_discord_id_int, new_member.id)
        if not ok:
            await interaction.followup.send(error, ephemeral=True)
            return

        await database.execute(
            """
            INSERT INTO mm_replacements (
                match_number, old_discord_id, new_discord_id, replaced_by_discord_id, penalty_applied
            )
            VALUES ($1, $2, $3, $4, $5)
            """,
            self.match_number,
            self.old_discord_id,
            database.did(new_member.id),
            database.did(interaction.user.id),
            True,
        )

        await adjust_player_elo_only(old_discord_id_int, season_number, REPLACE_LEAVE_PENALTY)
        await upsert_profile_from_member(new_member)

        updated = await get_match_by_number(self.match_number)

        guild = interaction.guild
        if guild:
            old_member = guild.get_member(old_discord_id_int)
            team_side = old_row["team_side"]

            if updated["text_channel_id"]:
                text_channel = guild.get_channel(int(updated["text_channel_id"]))
                if isinstance(text_channel, discord.TextChannel):
                    try:
                        await text_channel.set_permissions(
                            new_member,
                            view_channel=True,
                            send_messages=True
                        )
                        if old_member:
                            await text_channel.set_permissions(old_member, overwrite=None)
                    except discord.HTTPException:
                        pass

            voice_channel_id = updated["team_a_voice_id"] if team_side == "A" else updated["team_b_voice_id"]
            voice_channel = guild.get_channel(int(voice_channel_id)) if voice_channel_id else None
            if isinstance(voice_channel, discord.VoiceChannel):
                try:
                    await voice_channel.set_permissions(
                        new_member,
                        view_channel=True,
                        connect=True
                    )
                    if old_member:
                        await voice_channel.set_permissions(old_member, overwrite=None)
                except discord.HTTPException:
                    pass
            if updated["queue_channel_id"] and updated["queue_message_id"]:
                queue_channel = guild.get_channel(int(updated["queue_channel_id"]))
                if isinstance(queue_channel, discord.TextChannel):
                    try:
                        queue_message = await queue_channel.fetch_message(int(updated["queue_message_id"]))
                        await queue_message.edit(
                            embed=await build_match_started_embed(guild, updated),
                            view=InProgressMatchView(self.cog, self.match_number)
                        )
                    except discord.HTTPException:
                        pass

            if updated["text_channel_id"]:
                text_channel = guild.get_channel(int(updated["text_channel_id"]))
                if isinstance(text_channel, discord.TextChannel):
                    try:
                        await text_channel.send(
                            f"{mention_or_name(guild, self.old_discord_id)} was replaced by {new_member.mention}. "
                            f"Penalty applied: `{REPLACE_LEAVE_PENALTY}` ELO."
                        )
                    except discord.HTTPException:
                        pass

        await interaction.followup.send(
            f"Player replaced successfully. {mention_or_name(guild, self.old_discord_id)} received `{REPLACE_LEAVE_PENALTY}` ELO.",
            ephemeral=True
        )


class ReplacePlayerSelect(discord.ui.Select):
    def __init__(self, cog: "MatchmakingCog", match_number: int, players: list, guild: discord.Guild | None):
        self.cog = cog
        self.match_number = match_number

        options = []
        for row in players:
            label = get_member_label(guild, row["discord_id"])
            team_label = team_side_label(row["team_side"]) if row["team_side"] else "No Team"
            options.append(
                discord.SelectOption(
                    label=label,
                    value=row["discord_id"],
                    description=f"{team_label} • {role_label(row['role_pref'])}"[:100]
                )
            )

        super().__init__(
            placeholder="Select the player to replace",
            min_values=1,
            max_values=1,
            options=options[:25]
        )

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not can_manage_matchmaking(interaction.user):
            await interaction.response.send_message("Only Match Organizer can replace players.", ephemeral=True)
            return

        old_discord_id = self.values[0]
        await interaction.response.send_modal(
            ReplacePlayerModal(self.cog, self.match_number, old_discord_id)
        )


class ReplacePlayerPickView(discord.ui.View):
    def __init__(self, cog: "MatchmakingCog", match_number: int, players: list, guild: discord.Guild | None):
        super().__init__(timeout=120)
        self.add_item(ReplacePlayerSelect(cog, match_number, players, guild))


class FinishWinnerTeamSelect(discord.ui.Select):
    def __init__(self, cog: "MatchmakingCog", match_number: int):
        self.cog = cog
        self.match_number = match_number
        super().__init__(
            placeholder="Select Winner Team",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Team A", value="A"),
                discord.SelectOption(label="Team B", value="B"),
            ]
        )

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not can_manage_matchmaking(interaction.user):
            await interaction.response.send_message("Only Match Organizer can finish matches.", ephemeral=True)
            return

        winner_side = self.values[0]
        loser_side = "B" if winner_side == "A" else "A"

        winner_players = await get_team_players(self.match_number, winner_side)

        await interaction.response.edit_message(
            content=f"Winner Team: **{team_side_label(winner_side)}**\nNow choose Winner MVP.",
            view=FinishWmvpView(self.cog, self.match_number, winner_side, loser_side, winner_players, interaction.guild)
        )


class FinishWinnerTeamView(discord.ui.View):
    def __init__(self, cog: "MatchmakingCog", match_number: int):
        super().__init__(timeout=120)
        self.add_item(FinishWinnerTeamSelect(cog, match_number))


class FinishWmvpSelect(discord.ui.Select):
    def __init__(self, cog: "MatchmakingCog", match_number: int, winner_side: str, loser_side: str, players: list, guild: discord.Guild | None):
        self.cog = cog
        self.match_number = match_number
        self.winner_side = winner_side
        self.loser_side = loser_side

        options = [
            discord.SelectOption(
                label=get_member_label(guild, row["discord_id"]),
                value=row["discord_id"],
                description=role_label(row["role_pref"])[:100]
            )
            for row in players
        ]

        super().__init__(
            placeholder="Select Winner MVP",
            min_values=1,
            max_values=1,
            options=options[:25]
        )

    async def callback(self, interaction: discord.Interaction):
        wmvp_id = self.values[0]

        loser_players = await get_team_players(self.match_number, self.loser_side)

        await interaction.response.edit_message(
            content=(
                f"Winner Team: **{team_side_label(self.winner_side)}**\n"
                f"Winner MVP selected.\n"
                f"Now choose Loser MVP."
            ),
            view=FinishLmvpView(self.cog, self.match_number, self.winner_side, self.loser_side, wmvp_id, loser_players, interaction.guild)
        )


class FinishWmvpView(discord.ui.View):
    def __init__(self, cog: "MatchmakingCog", match_number: int, winner_side: str, loser_side: str, players: list, guild: discord.Guild | None):
        super().__init__(timeout=120)
        self.add_item(FinishWmvpSelect(cog, match_number, winner_side, loser_side, players, guild))


class FinishLmvpSelect(discord.ui.Select):
    def __init__(self, cog: "MatchmakingCog", match_number: int, winner_side: str, loser_side: str, wmvp_id: str, players: list, guild: discord.Guild | None):
        self.cog = cog
        self.match_number = match_number
        self.winner_side = winner_side
        self.loser_side = loser_side
        self.wmvp_id = wmvp_id

        options = [
            discord.SelectOption(
                label=get_member_label(guild, row["discord_id"]),
                value=row["discord_id"],
                description=role_label(row["role_pref"])[:100]
            )
            for row in players
        ]

        super().__init__(
            placeholder="Select Loser MVP",
            min_values=1,
            max_values=1,
            options=options[:25]
        )

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not can_manage_matchmaking(interaction.user):
            await interaction.response.send_message("Only Match Organizer can finish matches.", ephemeral=True)
            return

        lmvp_id = self.values[0]

        await interaction.response.send_modal(
            FinishScoreModal(
                cog=self.cog,
                match_number=self.match_number,
                winner_side=self.winner_side,
                loser_side=self.loser_side,
                wmvp_id=self.wmvp_id,
                lmvp_id=lmvp_id
            )
        )


class FinishLmvpView(discord.ui.View):
    def __init__(self, cog: "MatchmakingCog", match_number: int, winner_side: str, loser_side: str, wmvp_id: str, players: list, guild: discord.Guild | None):
        super().__init__(timeout=120)
        self.add_item(FinishLmvpSelect(cog, match_number, winner_side, loser_side, wmvp_id, players, guild))


class FinishScoreModal(discord.ui.Modal, title="Finish Match"):
    final_score = discord.ui.TextInput(
        label="Final Score (Team A - Team B)",
        style=discord.TextStyle.paragraph,
        required=True,
        placeholder="Example: 25-20, 22-25, 15-11"
    )

    def __init__(
        self,
        cog: "MatchmakingCog",
        match_number: int,
        winner_side: str,
        loser_side: str,
        wmvp_id: str,
        lmvp_id: str
    ):
        super().__init__()
        self.cog = cog
        self.match_number = match_number
        self.winner_side = winner_side
        self.loser_side = loser_side
        self.wmvp_id = wmvp_id
        self.lmvp_id = lmvp_id

    async def on_submit(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not can_manage_matchmaking(interaction.user):
            await interaction.response.send_message("Only Match Organizer can finish matches.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        ok, error = await self.cog.finalize_match(
            interaction=interaction,
            match_number=self.match_number,
            winner_side=self.winner_side,
            loser_side=self.loser_side,
            wmvp_id=self.wmvp_id,
            lmvp_id=self.lmvp_id,
            final_score_text=str(self.final_score).strip()
        )

        if not ok:
            await interaction.followup.send(error, ephemeral=True)
            return

        await interaction.followup.send(
            f"Match #{self.match_number} finished successfully.",
            ephemeral=True
        )

# =========================
# MAIN COG
# =========================

TEAM_CHOICES = [
    app_commands.Choice(name="Team A", value="A"),
    app_commands.Choice(name="Team B", value="B"),
]


class MatchmakingCog(commands.Cog):
    mm = app_commands.Group(
        name="mm",
        description="Matchmaking commands",
        guild_ids=[config.GUILD_ID]
    )

    season = app_commands.Group(
        name="season",
        description="Season commands",
        guild_ids=[config.GUILD_ID]
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.match_locks: dict[int, asyncio.Lock] = {}

    def get_match_lock(self, match_number: int) -> asyncio.Lock:
        if match_number not in self.match_locks:
            self.match_locks[match_number] = asyncio.Lock()
        return self.match_locks[match_number]

    @season.command(name="start", description="Starts a new Matchmaking season")
    async def season_start(self, interaction: discord.Interaction, number: int):
        if not isinstance(interaction.user, discord.Member):
            return

        if not can_manage_season(interaction.user):
            await interaction.response.send_message("Only Staff/Admin can start seasons.", ephemeral=True)
            return

        # Up to three sequential DB round-trips follow - ack immediately.
        await interaction.response.defer()

        active = await get_active_season()
        if active:
            await interaction.followup.send(
                f"Season {active['number']} is already active.",
                ephemeral=True
            )
            return

        existing = await database.fetchone("SELECT * FROM mm_seasons WHERE number = $1", number)
        if existing:
            await database.execute(
                "UPDATE mm_seasons SET is_active = true, started_at = $1, ended_at = NULL WHERE number = $2",
                now(), number,
            )
        else:
            await database.execute(
                "INSERT INTO mm_seasons (number, is_active, started_at, ended_at) VALUES ($1, true, $2, NULL)",
                number, now(),
            )

        await interaction.followup.send(f"Season {number} started successfully.")

    @season.command(name="end", description="Ends the active Matchmaking season")
    async def season_end(self, interaction: discord.Interaction, number: int):
        if not isinstance(interaction.user, discord.Member):
            return

        if not can_manage_season(interaction.user):
            await interaction.response.send_message("Only Staff/Admin can end seasons.", ephemeral=True)
            return

        # Up to three sequential DB round-trips follow - ack immediately.
        await interaction.response.defer()

        active = await get_active_season()
        if not active or active["number"] != number:
            await interaction.followup.send("This season is not the currently active season.", ephemeral=True)
            return

        active_match = await database.fetchone(
            """
            SELECT * FROM mm_matches
            WHERE status IN ('queue_open', 'team_format_vote', 'captains_pending', 'draft', 'ready_to_start', 'in_progress')
            LIMIT 1
            """
        )
        if active_match:
            await interaction.followup.send(
                "There is an active Matchmaking queue/match. Finish or cancel it before ending the season.",
                ephemeral=True
            )
            return

        await database.execute(
            "UPDATE mm_seasons SET is_active = false, ended_at = $1 WHERE number = $2",
            now(), number,
        )

        await interaction.followup.send(f"Season {number} ended successfully.")

    @season.command(name="stats", description="Shows season stats")
    async def season_stats(self, interaction: discord.Interaction, number: int):
        # Three sequential DB round-trips follow - ack immediately.
        await interaction.response.defer()

        season_row = await database.fetchone("SELECT * FROM mm_seasons WHERE number = $1", number)
        if not season_row:
            await interaction.followup.send("Season not found.", ephemeral=True)
            return

        top_rows = await database.fetchall(
            """
            SELECT *
            FROM mm_season_players
            WHERE season_number = $1
            ORDER BY (elo_gained - elo_lost) DESC, wins DESC, matches DESC
            LIMIT 10
            """,
            number,
        )

        leaderboard_lines = []
        guild = interaction.guild
        for index, row in enumerate(top_rows, start=1):
            net_elo = row["elo_gained"] - row["elo_lost"]
            leaderboard_lines.append(
                f"`#{index}` {mention_or_name(guild, row['discord_id'])} • Net `{net_elo}` • W-L `{row['wins']}-{row['losses']}` • Matches `{row['matches']}`"
            )

        total_matches_row = await database.fetchone(
            "SELECT COUNT(*) AS total FROM mm_matches WHERE season_number = $1 AND status = 'finished'",
            number,
        )
        total_matches = total_matches_row["total"] if total_matches_row else 0

        embed = discord.Embed(
            title=f"Season {number} Stats",
            color=discord.Color.orange()
        )
        embed.add_field(
            name="Status",
            value="Active" if season_row["is_active"] else "Closed",
            inline=True
        )
        embed.add_field(
            name="Started",
            value=str(season_row["started_at"]) if season_row["started_at"] else "—",
            inline=True
        )
        embed.add_field(
            name="Ended",
            value=str(season_row["ended_at"]) if season_row["ended_at"] else "—",
            inline=True
        )
        embed.add_field(
            name="Finished Matches",
            value=str(total_matches),
            inline=False
        )
        embed.add_field(
            name="Top 10 Leaderboard",
            value=chr(10).join(leaderboard_lines) if leaderboard_lines else "No data yet.",
            inline=False
        )

        await interaction.followup.send(embed=embed)

    @mm.command(name="start", description="Starts a Matchmaking queue")
    async def mm_start(self, interaction: discord.Interaction, number: int):
        if not isinstance(interaction.user, discord.Member):
            return

        if not can_manage_matchmaking(interaction.user):
            await interaction.response.send_message("Only Match Organizer can use this command.", ephemeral=True)
            return

        # Everything below does several sequential database round-trips
        # (and sometimes more, e.g. when clearing a cancelled match), which
        # can add up to more than Discord's 3-second interaction window -
        # especially with the bot on Discloud talking to Supabase over the
        # network. Acknowledge immediately and use followups from here on
        # so Discord never shows "The application did not respond" even
        # though the match was actually created successfully.
        await interaction.response.defer()

        season_row = await get_active_season()
        if not season_row:
            await interaction.followup.send("There is no active season. Use /season start first.", ephemeral=True)
            return

        existing = await get_match_by_number(number)
        if existing:
            if existing["status"] == "cancelled":
                await database.execute("DELETE FROM mm_match_players WHERE match_number = $1", number)
                await database.execute("DELETE FROM mm_matches WHERE match_number = $1", number)
            else:
                await interaction.followup.send(
                    f"Match #{number} already exists with status `{existing['status']}`.",
                    ephemeral=True
                )
                return

        await database.execute(
            """
            INSERT INTO mm_matches (
                match_number, season_number, status, created_by_discord_id,
                queue_channel_id, queue_message_id
            )
            VALUES ($1, $2, 'queue_open', $3, $4, NULL)
            """,
            number,
            season_row["number"],
            database.did(interaction.user.id),
            database.did(interaction.channel_id),
        )

        match_row = await get_match_by_number(number)
        view = JoinQueueView(self, number)
        await view.refresh_labels()

        sent_message = await interaction.followup.send(
            embed=await build_queue_embed(interaction.guild, match_row),
            view=view,
            wait=True,
        )

        await database.execute(
            "UPDATE mm_matches SET queue_message_id = $1 WHERE match_number = $2",
            database.did(sent_message.id), number,
        )

    @mm.command(name="cancel", description="Cancels a Matchmaking queue")
    async def mm_cancel(self, interaction: discord.Interaction, number: int):
        if not isinstance(interaction.user, discord.Member):
            return

        if not can_manage_matchmaking(interaction.user):
            await interaction.response.send_message(
                "Only Match Organizer can cancel the queue.",
                ephemeral=True
            )
            return

        match_row = await get_match_by_number(number)
        if not match_row:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return

        if match_row["status"] not in ("queue_open", "team_format_vote", "captains_pending", "draft", "ready_to_start", "in_progress"):
            await interaction.response.send_message(
                "Only active queues or in-progress matches can be cancelled.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        await database.execute(
            """
            UPDATE mm_matches
            SET status = 'cancelled',
                finished_at = $1,
                is_special = COALESCE(is_special, false),
                special_multiplier = COALESCE(special_multiplier, 1)
            WHERE match_number = $2
            """,
            now(), number,
        )

        previous_status = match_row["status"]

        updated = await get_match_by_number(number)
        guild = interaction.guild

        if guild is not None and updated["queue_channel_id"] and updated["queue_message_id"]:
            queue_channel = guild.get_channel(int(updated["queue_channel_id"]))
            if isinstance(queue_channel, discord.TextChannel):
                try:
                    queue_message = await queue_channel.fetch_message(int(updated["queue_message_id"]))
                    embed = (
                        await build_cancelled_in_progress_embed(guild, updated, interaction.user.id)
                        if previous_status == "in_progress"
                        else await build_cancelled_embed(guild, updated, interaction.user.id)
                    )

                    await queue_message.edit(
                        embed=embed,
                        view=None
                    )
                except discord.HTTPException:
                    pass
            for channel_id in [updated["text_channel_id"], updated["team_a_voice_id"], updated["team_b_voice_id"]]:
                if not channel_id:
                    continue
                channel = guild.get_channel(int(channel_id))
                if channel:
                    try:
                        await channel.delete(reason=f"Matchmaking #{number} cancelled")
                    except discord.HTTPException:
                        pass

        await interaction.followup.send(
            f"Matchmaking queue #{number} cancelled successfully.",
            ephemeral=True
        )

    @mm.command(name="finish", description="Finishes an in-progress Matchmaking match")
    @app_commands.choices(winner_team=TEAM_CHOICES, loser_team=TEAM_CHOICES)
    async def mm_finish(
        self,
        interaction: discord.Interaction,
        number: int,
        winner_team: app_commands.Choice[str],
        loser_team: app_commands.Choice[str],
        wmvp: discord.Member,
        lmvp: discord.Member,
        final_score: str
    ):
        if not isinstance(interaction.user, discord.Member):
            return

        if not can_manage_matchmaking(interaction.user):
            await interaction.response.send_message("Only Match Organizer can finish the match.", ephemeral=True)
            return

        if winner_team.value == loser_team.value:
            await interaction.response.send_message("Winner team and loser team must be different.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        ok, error = await self.finalize_match(
            interaction=interaction,
            match_number=number,
            winner_side=winner_team.value,
            loser_side=loser_team.value,
            wmvp_id=database.did(wmvp.id),
            lmvp_id=database.did(lmvp.id),
            final_score_text=final_score
        )

        if not ok:
            await interaction.followup.send(error, ephemeral=True)
            return

        await interaction.followup.send(f"Match #{number} finished successfully.", ephemeral=True)

    @mm.command(name="elo", description="Shows your Matchmaking ELO")
    async def mm_elo(self, interaction: discord.Interaction, member: discord.Member | None = None):
        target = member or interaction.user

        # Up to five sequential DB round-trips follow - ack immediately.
        await interaction.response.defer()

        await ensure_mm_player(target.id)
        row = await database.fetchone("SELECT * FROM mm_players WHERE discord_id = $1", database.did(target.id))
        season_row = await get_active_season()
        vip = await vip_data.get_active_vip(target.id)
        vip_suffix = f" • {vip_data.vip_tier_label(vip['tier'])}" if vip else ""

        embed = discord.Embed(
            title=f"{target.display_name} • MM Profile{vip_suffix}",
            color=discord.Color.gold() if vip else discord.Color.blurple()
        )
        embed.add_field(name="ELO", value=str(row["elo"]), inline=True)
        embed.add_field(name="Matches", value=str(row["matches"]), inline=True)
        embed.add_field(name="W-L", value=f"{row['wins']}-{row['losses']}", inline=True)
        embed.add_field(name="Win MVP", value=str(row["win_mvp"]), inline=True)
        embed.add_field(name="Loss MVP", value=str(row["loss_mvp"]), inline=True)
        embed.add_field(
            name="Total ELO",
            value=f"+{row['elo_gained_total']} / -{row['elo_lost_total']}",
            inline=True
        )

        if season_row:
            season_player = await database.fetchone(
                "SELECT * FROM mm_season_players WHERE season_number = $1 AND discord_id = $2",
                season_row["number"], database.did(target.id),
            )

            if season_player:
                embed.add_field(
                    name=f"Season {season_row['number']}",
                    value=(
                        f"Matches: `{season_player['matches']}`\n"
                        f"W-L: `{season_player['wins']}-{season_player['losses']}`\n"
                        f"ELO: `+{season_player['elo_gained']} / -{season_player['elo_lost']}`"
                    ),
                    inline=False
                )

        await interaction.followup.send(embed=embed)

    async def finalize_match(
        self,
        interaction: discord.Interaction,
        match_number: int,
        winner_side: str,
        loser_side: str,
        wmvp_id: str,
        lmvp_id: str,
        final_score_text: str
    ):
        match_row = await get_match_by_number(match_number)
        if not match_row:
            return False, "Match not found."

        if match_row["status"] != "in_progress":
            return False, "This match is not currently in progress."

        set_scores, parse_error = parse_final_score(final_score_text)
        if parse_error:
            return False, parse_error

        elo_calc, elo_error = calculate_match_team_deltas(set_scores, winner_side)
        if elo_error:
            return False, elo_error

        wmvp_row = await database.fetchone(
            "SELECT * FROM mm_match_players WHERE match_number = $1 AND discord_id = $2 AND team_side = $3",
            match_number, wmvp_id, winner_side,
        )

        lmvp_row = await database.fetchone(
            "SELECT * FROM mm_match_players WHERE match_number = $1 AND discord_id = $2 AND team_side = $3",
            match_number, lmvp_id, loser_side,
        )

        if not wmvp_row:
            return False, "WMVP must belong to the winner team."

        if not lmvp_row:
            return False, "LMVP must belong to the loser team."

        players = await get_match_players(match_number)
        season_number = match_row["season_number"]
        elo_changes: list[dict] = []

        base_winner_delta = elo_calc["winner_delta"]
        base_loser_delta = elo_calc["loser_delta"]
        normalized_final_score = elo_calc["final_score_display"]

        special_multiplier = match_row["special_multiplier"] or 1
        special = is_special_match(match_row)
        vip_queue = is_vip_queue(match_row)

        # VIP queue channel: wins are worth double ELO. Stacks with the
        # Golden Match multiplier and with each winner's own VIP % bonus
        # (applied per-player further below) - by design, per Meds.
        if vip_queue:
            base_winner_delta *= VIP_QUEUE_ELO_MULTIPLIER

        if special:
            base_winner_delta *= special_multiplier

        for row in players:
            is_winner = row["team_side"] == winner_side
            # VIP/VIP+ ELO bonus only applies to the winner's gain
            # (it never reduces the loser's loss, even if they are VIP).
            vip = await vip_data.get_active_vip(row["discord_id"]) if is_winner else None

            if is_winner:
                base_delta = base_winner_delta + (WMVP_BONUS if row["discord_id"] == wmvp_id else 0)
                multiplier = (1.0 + vip_data.VIP_ELO_WIN_BONUS_PERCENT.get(vip["tier"], 0.0)) if vip else 1.0
                delta = round(base_delta * multiplier)
                result = await apply_match_result_to_player(
                    discord_id=int(row["discord_id"]),
                    season_number=season_number,
                    delta=delta,
                    is_win=True,
                    is_win_mvp=(row["discord_id"] == wmvp_id),
                    is_loss_mvp=False
                )
            else:
                delta = base_loser_delta + (LMVP_REDUCTION if row["discord_id"] == lmvp_id else 0)
                result = await apply_match_result_to_player(
                    discord_id=int(row["discord_id"]),
                    season_number=season_number,
                    delta=delta,
                    is_win=False,
                    is_win_mvp=False,
                    is_loss_mvp=(row["discord_id"] == lmvp_id)
                )

            if result:
                result["vip_tier"] = vip["tier"] if vip else None
                elo_changes.append(result)

        await database.execute(
            """
            UPDATE mm_matches
            SET status = 'finished',
                winner_side = $1,
                loser_side = $2,
                wmvp_discord_id = $3,
                lmvp_discord_id = $4,
                final_score_text = $5,
                finished_at = $6
            WHERE match_number = $7
            """,
            winner_side,
            loser_side,
            wmvp_id,
            lmvp_id,
            normalized_final_score,
            now(),
            match_number,
        )

        updated = await get_match_by_number(match_number)
        guild = interaction.guild

        if guild is not None:
            results_channel = guild.get_channel(MM_RESULTS_CHANNEL_ID)
            if isinstance(results_channel, discord.TextChannel):
                await results_channel.send(embed=await build_result_embed(guild, updated))

            elo_update_channel = guild.get_channel(ELO_UPDATE_CHANNEL_ID)
            if isinstance(elo_update_channel, discord.TextChannel):
                await elo_update_channel.send(
                    embed=await build_elo_update_embed(guild, updated, elo_changes)
                )

            if updated["queue_channel_id"] and updated["queue_message_id"]:
                queue_channel = guild.get_channel(int(updated["queue_channel_id"]))
                if isinstance(queue_channel, discord.TextChannel):
                    try:
                        queue_message = await queue_channel.fetch_message(int(updated["queue_message_id"]))
                        await queue_message.edit(embed=await build_result_embed(guild, updated), view=None)
                    except discord.HTTPException:
                        pass

            for channel_id in [updated["text_channel_id"], updated["team_a_voice_id"], updated["team_b_voice_id"]]:
                if not channel_id:
                    continue
                channel = guild.get_channel(int(channel_id))
                if channel:
                    try:
                        await channel.delete(reason=f"Matchmaking #{match_number} finished")
                    except discord.HTTPException:
                        pass

        return True, None


    @mm.command(name="leaderboard", description="Shows the Matchmaking leaderboard")
    async def mm_leaderboard(self, interaction: discord.Interaction, page: int = 1, season_number: int | None = None):
        if page < 1:
            await interaction.response.send_message("Page must be 1 or greater.", ephemeral=True)
            return

        # Two sequential DB round-trips follow - ack immediately.
        await interaction.response.defer()

        per_page = 10
        offset = (page - 1) * per_page
        guild = interaction.guild

        if season_number is None:
            rows = await database.fetchall(
                """
                SELECT *
                FROM mm_players
                ORDER BY elo DESC, wins DESC, matches DESC
                LIMIT $1 OFFSET $2
                """,
                per_page, offset,
            )

            title = "MM Global Leaderboard"
            lines = []
            start_rank = offset + 1
            vip_by_discord_id = await get_vip_badges_for([row["discord_id"] for row in rows])

            for i, row in enumerate(rows, start=start_rank):
                badge = vip_by_discord_id.get(row["discord_id"], "")
                lines.append(
                    f"`#{i}` {mention_or_name(guild, row['discord_id'])}{badge} • ELO `{row['elo']}` • W-L `{row['wins']}-{row['losses']}` • M `{row['matches']}`"
                )
        else:
            rows = await database.fetchall(
                """
                SELECT *
                FROM mm_season_players
                WHERE season_number = $1
                ORDER BY (elo_gained - elo_lost) DESC, wins DESC, matches DESC
                LIMIT $2 OFFSET $3
                """,
                season_number, per_page, offset,
            )

            title = f"MM Season {season_number} Leaderboard"
            lines = []
            start_rank = offset + 1
            vip_by_discord_id = await get_vip_badges_for([row["discord_id"] for row in rows])

            for i, row in enumerate(rows, start=start_rank):
                net = row["elo_gained"] - row["elo_lost"]
                badge = vip_by_discord_id.get(row["discord_id"], "")
                lines.append(
                    f"`#{i}` {mention_or_name(guild, row['discord_id'])}{badge} • Net `{net}` • W-L `{row['wins']}-{row['losses']}` • M `{row['matches']}`"
                )

        embed = discord.Embed(
            title=title,
            description=chr(10).join(lines) if lines else "No data found for this page.",
            color=discord.Color.gold()
        )
        embed.set_footer(text=f"Page {page}")
        await interaction.followup.send(embed=embed)


    @mm.command(name="vip", description="Shows Matchmaking VIP/VIP+ pricing and benefits")
    async def mm_vip(self, interaction: discord.Interaction):
        my_vip = await vip_data.get_active_vip(interaction.user.id)

        embed = discord.Embed(
            title="CVR SA Matchmaking — VIP & VIP+",
            description=(
                f"Buy it on the site: {config.CVR_SA_SITE_URL}/matchmaking#vip\n"
                "Payment via Pix, processed by Stripe. Valid for "
                f"{vip_data.VIP_DURATION_DAYS} days from confirmation."
            ),
            color=discord.Color.gold()
        )
        embed.add_field(
            name=f"VIP — {vip_data.VIP_PRICING['vip']['label']} / 30 days",
            value=(
                f"• +{round(vip_data.VIP_ELO_WIN_BONUS_PERCENT['vip'] * 100)}% ELO gained on wins\n"
                "• Exclusive Discord role + VIP badge on the leaderboard (site and `/mm leaderboard`)\n"
                "• Access to the VIP queue channel - wins there are worth **2x ELO**"
            ),
            inline=False
        )
        embed.add_field(
            name=f"VIP+ — {vip_data.VIP_PRICING['vip_plus']['label']} / 30 days",
            value=(
                f"• +{round(vip_data.VIP_ELO_WIN_BONUS_PERCENT['vip_plus'] * 100)}% ELO gained on wins\n"
                "• Priority queue join - can take any position, even a full one, "
                "as long as the overall queue isn't full and picks haven't started\n"
                "• Exclusive Discord role + VIP badge on the leaderboard (site and `/mm leaderboard`)\n"
                "• Access to the VIP queue channel - wins there are worth **2x ELO**"
            ),
            inline=False
        )

        if my_vip:
            embed.add_field(
                name="Your status",
                value=f"You are **{vip_data.vip_tier_label(my_vip['tier'])}** until `{my_vip['expires_at']}`.",
                inline=False
            )
        else:
            embed.add_field(name="Your status", value="You do not have an active VIP subscription right now.", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @mm.command(name="addvip", description="Manually grants VIP or VIP+ to a player (no site purchase)")
    @app_commands.describe(
        player="Player to grant VIP to",
        tier="VIP or VIP+",
        days="Duration in days (defaults to the site's standard 30 days)"
    )
    @app_commands.choices(tier=[
        app_commands.Choice(name="VIP", value="vip"),
        app_commands.Choice(name="VIP+", value="vip_plus"),
    ])
    async def mm_addvip(
        self,
        interaction: discord.Interaction,
        player: discord.Member,
        tier: app_commands.Choice[str],
        days: int = vip_data.VIP_DURATION_DAYS,
    ):
        if not isinstance(interaction.user, discord.Member):
            return

        if not is_admin(interaction.user):
            await interaction.response.send_message("Only Administrator can grant VIP manually.", ephemeral=True)
            return

        if days <= 0:
            await interaction.response.send_message("Days must be greater than 0.", ephemeral=True)
            return

        await interaction.response.defer()

        await upsert_profile_from_member(player)

        tier_value = tier.value
        expires_at = now() + timedelta(days=days)

        # Recorded as a $0 "admin_grant" payment (rather than skipping
        # vip_payments entirely) so there's an audit trail of who
        # manually granted VIP and when, same as a real purchase would
        # leave behind.
        async with database.transaction() as conn:
            payment_row = await conn.fetchrow(
                """
                INSERT INTO vip_payments (discord_id, tier, amount_cents, provider, status, paid_at)
                VALUES ($1, $2, 0, 'admin_grant', 'paid', $3)
                RETURNING id
                """,
                database.did(player.id), tier_value, now(),
            )
            await conn.execute(
                "UPDATE vip_subscriptions SET status = 'cancelled' WHERE discord_id = $1 AND status = 'active'",
                database.did(player.id),
            )
            await conn.execute(
                """
                INSERT INTO vip_subscriptions (discord_id, tier, status, source_payment_id, role_applied, expires_at)
                VALUES ($1, $2, 'active', $3, true, $4)
                """,
                database.did(player.id), tier_value, payment_row["id"], expires_at,
            )

        if interaction.guild:
            role_id = config.VIP_PLUS_ROLE_ID if tier_value == "vip_plus" else config.VIP_ROLE_ID
            other_role_id = config.VIP_ROLE_ID if tier_value == "vip_plus" else config.VIP_PLUS_ROLE_ID
            role = interaction.guild.get_role(role_id) if role_id else None
            other_role = interaction.guild.get_role(other_role_id) if other_role_id else None
            try:
                if role:
                    await player.add_roles(role, reason=f"VIP granted manually by {interaction.user}")
                if other_role and other_role in player.roles:
                    await player.remove_roles(other_role, reason="Tier changed via /mm addvip")
            except discord.Forbidden:
                pass

        await interaction.followup.send(
            f"{player.mention} is now **{vip_data.vip_tier_label(tier_value)}** until "
            f"{expires_at.strftime('%d/%m/%Y %H:%M')} BRT (granted manually by {interaction.user.mention})."
        )

    @mm.command(name="addelo", description="Adds Matchmaking ELO to a player")
    async def mm_addelo(self, interaction: discord.Interaction, elo: int, user: discord.Member):
        if not isinstance(interaction.user, discord.Member):
            return

        if not can_manage_matchmaking(interaction.user):
            await interaction.response.send_message("Only Match Organizer can adjust ELO.", ephemeral=True)
            return

        if elo <= 0:
            await interaction.response.send_message("ELO must be greater than 0.", ephemeral=True)
            return

        # adjust_player_elo_only alone can chain up to five DB round-trips -
        # ack immediately.
        await interaction.response.defer(ephemeral=True)

        season_row = await get_active_season()
        season_number = season_row["number"] if season_row else None

        await adjust_player_elo_only(user.id, season_number, elo)
        row = await database.fetchone("SELECT * FROM mm_players WHERE discord_id = $1", database.did(user.id))
        await upsert_profile_from_member(user)

        await interaction.followup.send(
            f"Added `{elo}` ELO to {user.mention}. New ELO: `{row['elo']}`",
            ephemeral=True
        )


    @mm.command(name="removeelo", description="Removes Matchmaking ELO from a player")
    async def mm_removeelo(self, interaction: discord.Interaction, elo: int, user: discord.Member):
        if not isinstance(interaction.user, discord.Member):
            return

        if not can_manage_matchmaking(interaction.user):
            await interaction.response.send_message("Only Match Organizer can adjust ELO.", ephemeral=True)
            return

        if elo <= 0:
            await interaction.response.send_message("ELO must be greater than 0.", ephemeral=True)
            return

        # adjust_player_elo_only alone can chain up to five DB round-trips -
        # ack immediately.
        await interaction.response.defer(ephemeral=True)

        season_row = await get_active_season()
        season_number = season_row["number"] if season_row else None

        await adjust_player_elo_only(user.id, season_number, -elo)
        row = await database.fetchone("SELECT * FROM mm_players WHERE discord_id = $1", database.did(user.id))
        await upsert_profile_from_member(user)

        await interaction.followup.send(
            f"Removed `{elo}` ELO from {user.mention}. New ELO: `{row['elo']}`",
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(MatchmakingCog(bot))
