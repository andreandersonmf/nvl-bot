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


# ============================================================
# CONFIG
# ============================================================

MATCH_ORGANIZER_ROLE_ID  = config.MATCH_ORGANIZER_ROLE_ID
MATCHMAKING_CATEGORY_ID  = config.MATCHMAKING_CATEGORY_ID
MM_RESULTS_CHANNEL_ID    = config.MM_RESULTS_CHANNEL_ID
ELO_UPDATE_CHANNEL_ID    = config.ELO_UPDATE_CHANNEL_ID

ROLE_SETTER          = "setter"
ROLE_OUTSIDE_HITTER  = "outside_hitter"
ROLE_MIDDLE_BLOCKER  = "middle_blocker"
ROLE_OPPOSITE_HITTER = "opposite_hitter"

ROLE_ORDER = [ROLE_SETTER, ROLE_OUTSIDE_HITTER, ROLE_MIDDLE_BLOCKER, ROLE_OPPOSITE_HITTER]

ROLE_LABELS = {
    ROLE_SETTER:          "Setter",
    ROLE_OUTSIDE_HITTER:  "Outside Hitter",
    ROLE_MIDDLE_BLOCKER:  "Middle Blocker",
    ROLE_OPPOSITE_HITTER: "Opposite Hitter",
}

ROLE_SHORT = {
    ROLE_SETTER:          "S",
    ROLE_OUTSIDE_HITTER:  "OH",
    ROLE_MIDDLE_BLOCKER:  "MB",
    ROLE_OPPOSITE_HITTER: "OP",
}

ROLE_MAX_TOTAL = {
    ROLE_SETTER:          2,
    ROLE_OUTSIDE_HITTER:  4,
    ROLE_MIDDLE_BLOCKER:  4,
    ROLE_OPPOSITE_HITTER: 2,
}

QUEUE_SIZE = sum(ROLE_MAX_TOTAL.values())  # 12

VIP_QUEUE_CHANNEL_ID     = 1547095549264666654
VIP_QUEUE_ELO_MULTIPLIER = 2

BASE_WIN_ELO          = 22
BASE_LOSS_ELO         = -14
WMVP_BONUS            = 6
LMVP_REDUCTION        = 6
REPLACE_LEAVE_PENALTY = -30

SPECIAL_MATCH_CHANCE     = 0.20
SPECIAL_MATCH_MULTIPLIER = 3
SPECIAL_MATCH_NAME       = "🏆 GOLDEN MATCH"

# Win streak bonus: after N consecutive wins, apply extra ELO on each win
STREAK_THRESHOLD = 3      # streak must reach this to activate bonus
STREAK_BONUS_ELO = 5     # extra ELO per win while on a streak

# Debounce window for queue embed updates.
# Multiple clicks within this window collapse into one Discord API call.
DEBOUNCE_SECONDS = 0.4


# ============================================================
# BASIC HELPERS
# ============================================================

def now() -> datetime:
    return datetime.now(timezone.utc)

def is_admin(m: discord.Member) -> bool:
    return m.guild_permissions.administrator

def has_role(m: discord.Member, role_id: int) -> bool:
    return any(r.id == role_id for r in m.roles)

def can_manage_season(m: discord.Member) -> bool:
    if is_admin(m):
        return True
    ids = {r.id for r in m.roles}
    return any(rid in ids for rid in config.STAFF_APPROVER_ROLE_IDS)

def can_manage_mm(m: discord.Member) -> bool:
    return is_admin(m) or has_role(m, MATCH_ORGANIZER_ROLE_ID)

def side_label(side: str) -> str:
    return "Team A" if side == "A" else "Team B"

def r_short(rp: str) -> str:
    return ROLE_SHORT.get(rp, (rp or "?")[:2].upper())

def r_label(rp: str) -> str:
    return ROLE_LABELS.get(rp, rp)

def is_vip_queue(match_row) -> bool:
    cid = match_row["queue_channel_id"] if match_row else None
    return bool(cid) and str(cid) == str(VIP_QUEUE_CHANNEL_ID)

def is_special(match_row) -> bool:
    return bool(match_row["is_special"]) if match_row else False

def mention(guild: discord.Guild | None, did_val) -> str:
    uid = int(did_val)
    if guild:
        m = guild.get_member(uid)
        if m:
            return m.mention
    return f"<@{uid}>"

def display_name(guild: discord.Guild | None, did_val, fallback: str | None = None) -> str:
    uid = int(did_val)
    if guild:
        m = guild.get_member(uid)
        if m:
            return m.display_name[:80]
    return (fallback or str(uid))[:80]

def format_delta(d: int) -> str:
    return f"+{d}" if d > 0 else str(d)


# ============================================================
# DB HELPERS — all take an explicit asyncpg.Connection
# ============================================================

async def _q(c, query, *p):
    return await c.fetchrow(query, *p)

async def _qa(c, query, *p):
    return await c.fetch(query, *p)

async def _x(c, query, *p):
    return await c.execute(query, *p)


async def db_get_season(c):
    return await _q(c,
        "SELECT * FROM mm_seasons WHERE is_active = true ORDER BY number DESC LIMIT 1")

async def db_get_match(c, number: int):
    return await _q(c, "SELECT * FROM mm_matches WHERE match_number = $1", number)

async def db_get_queue_rows(c, number: int):
    return await _qa(c, """
        SELECT discord_id, role_pref FROM mm_match_players
        WHERE match_number = $1
        ORDER BY
            CASE role_pref
                WHEN 'setter'          THEN 0
                WHEN 'outside_hitter'  THEN 1
                WHEN 'middle_blocker'  THEN 2
                WHEN 'opposite_hitter' THEN 3
                ELSE 4 END,
            id ASC
        """, number)

async def db_get_team(c, number: int, side: str):
    return await _qa(c, """
        SELECT * FROM mm_match_players
        WHERE match_number = $1 AND team_side = $2
        ORDER BY captain DESC, pick_order ASC, id ASC
        """, number, side)

async def db_get_available(c, number: int):
    return await _qa(c, """
        SELECT * FROM mm_match_players
        WHERE match_number = $1 AND team_side IS NULL
        ORDER BY
            CASE role_pref
                WHEN 'setter'          THEN 0
                WHEN 'outside_hitter'  THEN 1
                WHEN 'middle_blocker'  THEN 2
                WHEN 'opposite_hitter' THEN 3
                ELSE 4 END,
            id ASC
        """, number)

async def db_get_all_players(c, number: int):
    return await _qa(c, """
        SELECT * FROM mm_match_players
        WHERE match_number = $1
        ORDER BY captain DESC, pick_order ASC, id ASC
        """, number)

async def db_is_busy(c, discord_id: str) -> bool:
    row = await _q(c, """
        SELECT 1 FROM mm_match_players mp
        JOIN mm_matches m ON m.match_number = mp.match_number
        WHERE mp.discord_id = $1
          AND m.status IN ('queue_open','team_format_vote','captains_pending',
                           'draft','ready_to_start','in_progress')
        LIMIT 1
        """, discord_id)
    return row is not None

async def db_pick_count(c, number: int) -> int:
    row = await _q(c,
        "SELECT COUNT(*) AS n FROM mm_match_players WHERE match_number = $1 AND team_side IS NOT NULL AND captain = false",
        number)
    return row["n"] if row else 0

async def db_turn_side(c, match_row) -> str | None:
    avail = await db_get_available(c, match_row["match_number"])
    if not avail:
        return None
    picks  = await db_pick_count(c, match_row["match_number"])
    first  = "A" if match_row["first_picker_discord_id"] == match_row["captain1_discord_id"] else "B"
    second = "B" if first == "A" else "A"
    return first if picks % 2 == 0 else second

async def db_captain_side(c, number: int, discord_id: str) -> str | None:
    row = await _q(c,
        "SELECT team_side FROM mm_match_players WHERE match_number = $1 AND discord_id = $2 AND captain = true",
        number, discord_id)
    return row["team_side"] if row else None

async def db_role_count_team(c, number: int, side: str, role: str) -> int:
    row = await _q(c,
        "SELECT COUNT(*) AS n FROM mm_match_players WHERE match_number = $1 AND team_side = $2 AND role_pref = $3",
        number, side, role)
    return row["n"] if row else 0

async def db_ensure_mm_player(c, discord_id: str):
    await _x(c,
        "INSERT INTO mm_players (discord_id) VALUES ($1) ON CONFLICT (discord_id) DO NOTHING",
        discord_id)

async def db_ensure_season_player(c, season: int, discord_id: str):
    await _x(c,
        "INSERT INTO mm_season_players (season_number, discord_id) VALUES ($1, $2) ON CONFLICT (season_number, discord_id) DO NOTHING",
        season, discord_id)

async def db_vip_badges(c, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = await _qa(c,
        "SELECT discord_id, tier FROM vip_subscriptions WHERE status = 'active' AND expires_at > now() AND discord_id = ANY($1::text[])",
        ids)
    return {r["discord_id"]: (" 👑 VIP+" if r["tier"] == "vip_plus" else " ⭐ VIP") for r in rows}


# ============================================================
# SCORE / ELO LOGIC
# ============================================================

def parse_score(text: str):
    raw = text.strip()
    if not raw:
        return None, "Final Score cannot be empty."
    parts = [p.strip() for p in re.split(r"[,|\n;]+", raw) if p.strip()]
    sets  = []
    for part in parts:
        norm = re.sub(r"\s*[xX:]\s*", "-", part)
        m    = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})", norm)
        if not m:
            return None, "Invalid score format. Example: `25-20, 22-25, 15-11`"
        a, b = int(m.group(1)), int(m.group(2))
        if a == b:
            return None, "A set cannot end in a tie."
        sets.append((a, b))
    return sets, None

def margin_bonus(avg: float) -> int:
    if avg >= 15: return 8
    if avg >= 11: return 6
    if avg >= 7:  return 4
    if avg >= 4:  return 2
    return 0

def calc_elo_deltas(sets, winner_side: str):
    aw = sum(1 for a, b in sets if a > b)
    bw = len(sets) - aw
    if aw == bw:
        return None, "Score is tied in sets."
    actual = "A" if aw > bw else "B"
    if actual != winner_side:
        return None, f"Score shows Team {actual} as winner, not Team {winner_side}."
    avg   = sum(abs(a - b) for a, b in sets) / len(sets)
    bonus = margin_bonus(avg)
    return {
        "winner_delta": BASE_WIN_ELO + bonus,
        "loser_delta":  BASE_LOSS_ELO - round(bonus * 0.75),
        "display":      " | ".join(f"{a}-{b}" for a, b in sets),
    }, None


# ============================================================
# EMBED BUILDERS — pure, no DB calls
# ============================================================

def _queue_sections(guild, rows) -> str:
    grouped: dict[str, list[str]] = {r: [] for r in ROLE_ORDER}
    for row in rows:
        rp = row["role_pref"]
        grouped.setdefault(rp, []).append(
            f"{mention(guild, row['discord_id'])} `[{r_short(rp)}]`"
        )
    parts = []
    for role in ROLE_ORDER:
        lines = grouped[role]
        parts.append(
            f"**{ROLE_LABELS[role]} ({len(lines)}/{ROLE_MAX_TOTAL[role]})**\n"
            + (chr(10).join(lines) if lines else "—")
        )
    return "\n\n".join(parts)

def embed_queue(guild, match_row, rows) -> discord.Embed:
    vq = is_vip_queue(match_row)
    e  = discord.Embed(
        title=(
            f"NVL Matchmaking Queue #{match_row['match_number']}"
            + (" • VIP Queue (2x ELO on wins)" if vq else "")
        ),
        description=_queue_sections(guild, rows),
        color=discord.Color.gold() if vq else discord.Color.blurple(),
    )
    sn = match_row["season_number"]
    e.set_footer(text=f"NVL Matchmaking • Season {sn}" if sn else "NVL Matchmaking")
    return e

def embed_vote(match_row, rv: int, cv: int, voted: int, total: int, seconds_left: int = 30) -> discord.Embed:
    e = discord.Embed(
        title=f"Queue #{match_row['match_number']} • Team Format Vote",
        description=(
            "The queue is full! Vote how teams should be formed.\n"
            f"Ends in **{seconds_left}s** or when everyone votes. Tie → **Captain Picks**."
        ),
        color=discord.Color.gold(),
    )
    e.add_field(name="🎲 Random Teams",  value=str(rv), inline=True)
    e.add_field(name="👑 Captain Picks", value=str(cv), inline=True)
    e.set_footer(text=f"{voted}/{total} voted")
    return e

def embed_captains(guild, match_row, all_rows) -> discord.Embed:
    lines = []
    for row in all_rows:
        suf = f" [{r_short(row['role_pref'])}]"
        if match_row["captain1_discord_id"] == row["discord_id"]:
            suf += " • CAPTAIN 1"
        elif match_row["captain2_discord_id"] == row["discord_id"]:
            suf += " • CAPTAIN 2"
        lines.append(f"{mention(guild, row['discord_id'])}`{suf}`")
    c1 = mention(guild, match_row["captain1_discord_id"]) if match_row["captain1_discord_id"] else "Not selected"
    c2 = mention(guild, match_row["captain2_discord_id"]) if match_row["captain2_discord_id"] else "Not selected"
    e  = discord.Embed(
        title=f"Queue #{match_row['match_number']} • Set Captains",
        description=(
            "The queue is now full.\n\n**Queued Players**\n"
            + (chr(10).join(lines) if lines else "—")
            + f"\n\n**Captain 1:** {c1}\n**Captain 2:** {c2}"
        ),
        color=discord.Color.gold(),
    )
    e.set_footer(text="Only Match Organizer can choose captains")
    return e

def _team_lines(guild, players, wmvp=None, lmvp=None) -> str:
    lines = []
    for row in players:
        tags = [r_short(row["role_pref"])]
        if row["captain"]:                             tags.append("CAP")
        if wmvp and row["discord_id"] == wmvp:        tags.append("WMVP")
        if lmvp and row["discord_id"] == lmvp:        tags.append("LMVP")
        lines.append(f"{mention(guild, row['discord_id'])} `[{', '.join(tags)}]`")
    return chr(10).join(lines) if lines else "—"

def embed_draft(guild, match_row, team_a, team_b, available, turn_side) -> discord.Embed:
    avail_lines = [
        f"{mention(guild, r['discord_id'])} `[{r_short(r['role_pref'])}]`"
        for r in available
    ]
    turn_text = f"{side_label(turn_side)} Captain" if turn_side else "Draft complete"
    e = discord.Embed(title=f"Queue #{match_row['match_number']} • Draft Phase", color=discord.Color.green())
    e.add_field(name="Team A", value=_team_lines(guild, team_a), inline=False)
    e.add_field(name="Team B", value=_team_lines(guild, team_b), inline=False)
    e.add_field(name=f"Available ({len(available)})", value=chr(10).join(avail_lines) if avail_lines else "—", inline=False)
    e.add_field(name="Current Turn", value=turn_text, inline=False)
    fp = mention(guild, match_row["first_picker_discord_id"]) if match_row["first_picker_discord_id"] else "—"
    e.set_footer(text=f"First pick: {fp}")
    return e

def embed_ready(guild, match_row, team_a, team_b) -> discord.Embed:
    e = discord.Embed(
        title=f"Queue #{match_row['match_number']} • Teams Ready",
        description="All picks complete. Match Organizer can now start the match.",
        color=discord.Color.blue(),
    )
    e.add_field(name="Team A", value=_team_lines(guild, team_a), inline=False)
    e.add_field(name="Team B", value=_team_lines(guild, team_b), inline=False)
    return e

def embed_started(guild, match_row, team_a, team_b) -> discord.Embed:
    sp   = is_special(match_row)
    mult = match_row["special_multiplier"] or 1
    desc = f"**Private Server Link**\n{match_row['private_server_link']}"
    if sp:
        desc = (f"## {SPECIAL_MATCH_NAME}\n⚡ **This is a Special Match!**\n"
                f"Winning team earns **{mult}x Elo**.\n\n**Private Server Link**\n{match_row['private_server_link']}")
    e = discord.Embed(
        title=f"Match In Progress • #{match_row['match_number']}",
        description=desc,
        color=discord.Color.gold() if sp else discord.Color.dark_green(),
    )
    e.add_field(name="Team A", value=_team_lines(guild, team_a), inline=False)
    e.add_field(name="Team B", value=_team_lines(guild, team_b), inline=False)
    if sp:
        e.add_field(name="Bonus Rule", value=f"Winner receives **{mult}x Elo**.", inline=False)
    e.set_footer(text="NVL Matchmaking • VIP Queue (2x ELO)" if is_vip_queue(match_row) else "NVL Matchmaking")
    return e

def embed_result(guild, match_row, winners, losers) -> discord.Embed:
    sp   = is_special(match_row)
    mult = match_row["special_multiplier"] or 1
    e    = discord.Embed(title=f"Match Result • #{match_row['match_number']}", color=discord.Color.purple())
    if sp:
        e.color       = discord.Color.gold()
        e.description = f"{SPECIAL_MATCH_NAME}\nWinner earned **{mult}x Elo**."
    if match_row["final_score_text"]:
        e.add_field(name="Final Score", value=match_row["final_score_text"], inline=False)
    e.add_field(name=f"{side_label(match_row['winner_side'])} • Winner",
                value=_team_lines(guild, winners, wmvp=match_row["wmvp_discord_id"]), inline=False)
    e.add_field(name=f"{side_label(match_row['loser_side'])} • Loser",
                value=_team_lines(guild, losers, lmvp=match_row["lmvp_discord_id"]), inline=False)
    e.set_footer(text="NVL MM Results • VIP Queue" if is_vip_queue(match_row) else "NVL Matchmaking Results")
    return e

def embed_elo_update(guild, match_row, changes: list[dict]) -> discord.Embed:
    sp   = is_special(match_row)
    mult = match_row["special_multiplier"] or 1
    e    = discord.Embed(title=f"ELO Update • Match #{match_row['match_number']}", color=discord.Color.orange())
    if sp:
        e.color       = discord.Color.gold()
        e.description = f"{SPECIAL_MATCH_NAME}\nWinning team received **{mult}x Elo**."
    if match_row["final_score_text"]:
        e.add_field(name="Final Score", value=match_row["final_score_text"], inline=False)
    if sp:
        e.add_field(name="Special Bonus", value=f"Winner base Elo ×**{mult}**.", inline=False)
    winners, losers = [], []
    for ch in changes:
        tags = []
        if ch["is_win_mvp"]:   tags.append("WMVP")
        if ch["is_loss_mvp"]:  tags.append("LMVP")
        if ch.get("vip_tier"):    tags.append(vip_data.vip_tier_label(ch["vip_tier"]))
        streak = ch.get("win_streak", 0) or 0
        if ch.get("streak_bonus") and ch["streak_bonus"] > 0:
            tags.append(f"🔥 {streak}W Streak")
        suf  = f" ({', '.join(tags)})" if tags else ""
        line = f"{mention(guild, ch['discord_id'])}{suf} • `{format_delta(ch['delta'])}` → `{ch['new_elo']}`"
        (winners if ch["is_win"] else losers).append(line)
    e.add_field(name=f"{side_label(match_row['winner_side'])} • Gained", value="\n".join(winners) or "—", inline=False)
    e.add_field(name=f"{side_label(match_row['loser_side'])} • Lost",    value="\n".join(losers)  or "—", inline=False)
    e.set_footer(text="ELO after match finish")
    return e

def embed_cancelled(guild, match_row, rows, by_id=None) -> discord.Embed:
    desc = _queue_sections(guild, rows) + "\n\n**Status:** Cancelled"
    if by_id:
        desc += f"\n**Cancelled by:** {mention(guild, by_id)}"
    e = discord.Embed(
        title=f"NVL Matchmaking Queue #{match_row['match_number']} • Cancelled",
        description=desc,
        color=discord.Color.red(),
    )
    e.set_footer(text="NVL Matchmaking")
    return e

def embed_cancelled_ip(guild, match_row, team_a, team_b, by_id=None) -> discord.Embed:
    e = discord.Embed(
        title=f"Match Cancelled • #{match_row['match_number']}",
        description="This match was cancelled after being started.",
        color=discord.Color.red(),
    )
    e.add_field(name="Team A", value=_team_lines(guild, team_a), inline=False)
    e.add_field(name="Team B", value=_team_lines(guild, team_b), inline=False)
    if by_id:
        e.add_field(name="Cancelled by", value=mention(guild, by_id), inline=False)
    return e


# ============================================================
# ELO
# ============================================================

async def apply_elo(c, discord_id: str, season: int | None, delta: int,
                    is_win: bool, is_wmvp: bool, is_lmvp: bool) -> dict | None:
    await db_ensure_mm_player(c, discord_id)
    player = await _q(c, "SELECT elo, win_streak FROM mm_players WHERE discord_id = $1", discord_id)
    if not player:
        return None

    # Win streak bonus
    current_streak = player["win_streak"] if player["win_streak"] is not None else 0
    streak_bonus = 0
    if is_win:
        new_streak = current_streak + 1
        if new_streak >= STREAK_THRESHOLD:
            streak_bonus = STREAK_BONUS_ELO
            delta += streak_bonus
    else:
        new_streak = 0

    old    = player["elo"]
    new    = max(0, old + delta)
    gained = max(0, delta)
    lost   = max(0, -delta)
    await _x(c, """
        UPDATE mm_players
        SET elo=$1, matches=matches+1, wins=wins+$2, losses=losses+$3,
            win_mvp=win_mvp+$4, loss_mvp=loss_mvp+$5,
            elo_gained_total=elo_gained_total+$6, elo_lost_total=elo_lost_total+$7,
            win_streak=$8
        WHERE discord_id=$9
        """,
        new, int(is_win), int(not is_win),
        int(is_wmvp), int(is_lmvp),
        gained, lost, new_streak, discord_id)
    if season is not None:
        await db_ensure_season_player(c, season, discord_id)
        await _x(c, """
            UPDATE mm_season_players
            SET matches=matches+1, wins=wins+$1, losses=losses+$2,
                win_mvp=win_mvp+$3, loss_mvp=loss_mvp+$4,
                elo_gained=elo_gained+$5, elo_lost=elo_lost+$6
            WHERE season_number=$7 AND discord_id=$8
            """,
            int(is_win), int(not is_win), int(is_wmvp), int(is_lmvp),
            gained, lost, season, discord_id)
    return {"discord_id": discord_id, "old_elo": old, "new_elo": new, "delta": delta,
            "streak_bonus": streak_bonus, "win_streak": new_streak,
            "is_win": is_win, "is_win_mvp": is_wmvp, "is_loss_mvp": is_lmvp}


async def adjust_elo_only(c, discord_id: str, season: int | None, delta: int):
    await db_ensure_mm_player(c, discord_id)
    player = await _q(c, "SELECT elo FROM mm_players WHERE discord_id = $1", discord_id)
    if not player:
        return
    new    = max(0, player["elo"] + delta)
    gained = max(0, delta)
    lost   = max(0, -delta)
    await _x(c,
        "UPDATE mm_players SET elo=$1, elo_gained_total=elo_gained_total+$2, elo_lost_total=elo_lost_total+$3 WHERE discord_id=$4",
        new, gained, lost, discord_id)
    if season is not None:
        await db_ensure_season_player(c, season, discord_id)
        await _x(c,
            "UPDATE mm_season_players SET elo_gained=elo_gained+$1, elo_lost=elo_lost+$2 WHERE season_number=$3 AND discord_id=$4",
            gained, lost, season, discord_id)


# ============================================================
# QUEUE MESSAGE UPDATER
#
# Solves the "N clicks → N Discord edits" problem.
#
# Architecture:
#   - One QueueUpdater per active match, owned by MMCog.
#   - State machine: IDLE → DIRTY (scheduled) → IDLE (after push).
#   - Uses an asyncio.Event + persistent loop task instead of
#     cancel/recreate, which is more robust and avoids task leaks.
#   - The push always reads fresh state from DB, so it never sends
#     a stale embed even if several changes happened during the window.
#   - Always sends embed + the correct view together, fixing the
#     "buttons disappear" bug from the previous version.
# ============================================================

class QueueUpdater:
    """
    Debounced queue embed pusher.

    Call .mark_dirty(guild, view) after every state change.
    The updater waits DEBOUNCE_SECONDS after the *last* call,
    then reads fresh state from DB and pushes one edit.
    """

    def __init__(self, bot: commands.Bot, mn: int,
                 channel_id: int, message_id: int):
        self.bot        = bot
        self.mn         = mn
        self.channel_id = channel_id
        self.message_id = message_id

        self._dirty   = asyncio.Event()
        self._guild:  discord.Guild | None = None
        self._view:   discord.ui.View | None = None
        self._stopped = False
        self._task    = asyncio.create_task(self._loop())

    def mark_dirty(self, guild: discord.Guild | None,
                   view: discord.ui.View | None) -> None:
        """
        Signal that the queue state changed and a push is needed.
        Calling this multiple times within DEBOUNCE_SECONDS results
        in exactly one push with the most recent guild/view.
        """
        if self._stopped:
            return
        self._guild = guild
        self._view  = view
        self._dirty.set()

    def stop(self) -> None:
        """Cancel the background loop (call when match ends/cancels)."""
        self._stopped = True
        self._dirty.set()   # wake loop so it can exit cleanly
        if not self._task.done():
            self._task.cancel()

    async def _loop(self) -> None:
        while not self._stopped:
            # Wait until someone marks us dirty.
            await self._dirty.wait()
            if self._stopped:
                break

            # Debounce: reset the event and wait. If more changes arrive
            # during the sleep they set the event again; we loop back and
            # wait the full window from the new signal.
            self._dirty.clear()
            await asyncio.sleep(DEBOUNCE_SECONDS)

            # If marked dirty again during sleep, go back and wait more.
            if self._dirty.is_set():
                continue

            # Push the update with the latest guild/view snapshot.
            await self._push(self._guild, self._view)

    async def _push(self, guild: discord.Guild | None,
                    view: discord.ui.View | None) -> None:
        """
        Read fresh state from DB and edit the queue message.
        Always sends embed + view together so buttons never disappear.
        """
        try:
            async with database.conn() as c:
                match_row = await db_get_match(c, self.mn)
                if not match_row:
                    return

                status = match_row["status"]

                if status == "queue_open":
                    rows = await db_get_queue_rows(c, self.mn)
                    emb  = embed_queue(guild, match_row, rows)
                    # view is the JoinQueueView passed by the caller
                elif status == "captains_pending":
                    all_rows = await db_get_queue_rows(c, self.mn)
                    emb      = embed_captains(guild, match_row, all_rows)
                    # view is the CaptainSetupView passed by the caller
                elif status == "draft":
                    team_a = await db_get_team(c, self.mn, "A")
                    team_b = await db_get_team(c, self.mn, "B")
                    avail  = await db_get_available(c, self.mn)
                    turn   = await db_turn_side(c, match_row)
                    emb    = embed_draft(guild, match_row, team_a, team_b, avail, turn)
                    # view is the DraftView passed by the caller
                else:
                    # team_format_vote or anything else:
                    # the vote view manages its own edits; skip.
                    return

            channel = self.bot.get_channel(self.channel_id)
            if not isinstance(channel, discord.TextChannel):
                return
            msg = channel.get_partial_message(self.message_id)

            # Always include the view so buttons are never lost.
            edit_kwargs: dict = {"embed": emb}
            if view is not None:
                edit_kwargs["view"] = view
            await msg.edit(**edit_kwargs)

        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # best-effort; never crash the bot


# ============================================================
# VIEWS
# ============================================================

class JoinQueueView(discord.ui.View):
    """
    Queue join/leave buttons.

    Concurrency model:
    - custom_ids set in __init__ (not callbacks) for correct routing.
    - Lock protects ONLY the DB write (INSERT + status check).
      VIP is fetched BEFORE acquiring the lock (separate pool slot).
      Discord followup is sent AFTER releasing the lock.
    - Embed update is debounced via QueueUpdater: many clicks → 1 edit.
    - Transition to team_format_vote is also handled inside the lock
      to ensure exactly one interaction triggers the status change.
    """

    def __init__(self, cog: "MMCog", match_number: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.mn  = match_number

        _map = {
            "q_s":  f"mm_s_{match_number}",
            "q_oh": f"mm_oh_{match_number}",
            "q_mb": f"mm_mb_{match_number}",
            "q_op": f"mm_op_{match_number}",
            "q_lv": f"mm_lv_{match_number}",
        }
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.custom_id in _map:
                item.custom_id = _map[item.custom_id]

    def _update_labels(self, counts: dict[str, int]) -> None:
        """Update button labels in-memory (the updater will push the embed)."""
        for item in self.children:
            if not isinstance(item, discord.ui.Button):
                continue
            for role in ROLE_ORDER:
                if item.custom_id == f"mm_{r_short(role).lower()}_{self.mn}":
                    item.label = f"Join {ROLE_LABELS[role]} ({counts.get(role, 0)}/{ROLE_MAX_TOTAL[role]})"

    async def _join(self, interaction: discord.Interaction, role: str) -> None:
        if not isinstance(interaction.user, discord.Member):
            return

        # ① ACK immediately — before ANY other work.
        await interaction.response.defer(ephemeral=True)

        uid = database.did(interaction.user.id)

        # ② Fetch VIP status BEFORE the lock.
        # This uses its own pool connection and must not be inside the lock,
        # which would block other players while this round-trip completes.
        vip = await vip_data.get_active_vip(interaction.user.id)

        # ③ Lock: only the minimum critical section.
        # Covers: state validation + INSERT + queue-full check.
        # Does NOT cover: VIP fetch (above), followup send (below),
        # or embed update (via updater, completely outside).
        lock = self.cog.get_lock(self.mn)

        error_msg: str | None = None
        joined      = False
        went_full   = False
        counts:  dict[str, int] = {}
        total_after = 0

        async with lock:
            async with database.conn() as c:
                match_row = await db_get_match(c, self.mn)

                if not match_row or match_row["status"] != "queue_open":
                    error_msg = "This queue is no longer open."
                elif await _q(c, "SELECT 1 FROM mm_match_players WHERE match_number=$1 AND discord_id=$2", self.mn, uid):
                    error_msg = "You are already in this queue."
                elif await db_is_busy(c, uid):
                    error_msg = "You are already in another active queue/match."
                else:
                    rc_row      = await _q(c, "SELECT COUNT(*) AS n FROM mm_match_players WHERE match_number=$1 AND role_pref=$2", self.mn, role)
                    role_count  = rc_row["n"] if rc_row else 0

                    if role_count >= ROLE_MAX_TOTAL[role]:
                        error_msg = f"The {ROLE_LABELS[role]} slot is full."

                    if error_msg is None:
                        weight = vip_data.VIP_CAPTAIN_PRIORITY_WEIGHT.get(vip["tier"], 0) if vip else 0
                        try:
                            await _x(c, """
                                INSERT INTO mm_match_players
                                    (match_number, discord_id, role_pref, team_side, captain, pick_order, priority_weight)
                                VALUES ($1, $2, $3, NULL, false, NULL, $4)
                                """, self.mn, uid, role, weight)
                            joined = True
                        except asyncpg.UniqueViolationError:
                            error_msg = "You have already joined this queue."

                if joined:
                    # Check queue-full transition atomically (still inside lock).
                    tot_row     = await _q(c, "SELECT COUNT(*) AS n FROM mm_match_players WHERE match_number=$1", self.mn)
                    total_after = tot_row["n"] if tot_row else 0

                    if total_after >= QUEUE_SIZE and match_row["status"] == "queue_open":
                        await _x(c,
                            "UPDATE mm_matches SET status='captains_pending' WHERE match_number=$1 AND status='queue_open'",
                            self.mn)
                        went_full = True
                    else:
                        # Collect counts for label update (only for normal joins).
                        rows = await db_get_queue_rows(c, self.mn)
                        for r in rows:
                            if r["role_pref"] in counts:
                                counts[r["role_pref"]] = counts.get(r["role_pref"], 0) + 1
                            else:
                                counts[r["role_pref"]] = 1

        # ④ All Discord I/O happens OUTSIDE the lock.

        if error_msg:
            await interaction.followup.send(error_msg, ephemeral=True)
            return

        if went_full:
            # Queue hit 12 — start Captain Picks immediately.
            updater = self.cog.get_updater(self.mn)

            if updater:
                updater.stop()
                self.cog.clear_updater(self.mn)

                try:
                    channel = self.bot.get_channel(updater.channel_id)
                    if isinstance(channel, discord.TextChannel):
                        async with database.conn() as c:
                            mr = await db_get_match(c, self.mn)
                            rows = await db_get_queue_rows(c, self.mn)
                        emb = embed_captains(None, mr, rows)
                        msg = await channel.fetch_message(updater.message_id)
                        await msg.edit(embed=emb, view=CaptainSetupView(self.cog, self.mn))
                except discord.HTTPException:
                    pass

            asyncio.create_task(upsert_profile_from_member(interaction.user))
            await interaction.followup.send(
                "✅ You joined! Queue is full — Captain Picks is ready.", ephemeral=True)
            return

        # Normal join: update button labels and schedule debounced push.
        self._update_labels(counts)
        updater = self.cog.get_updater(self.mn)
        if updater:
            updater.mark_dirty(interaction.guild, self)

        asyncio.create_task(upsert_profile_from_member(interaction.user))
        await interaction.followup.send("✅ You joined the queue!", ephemeral=True)

    @discord.ui.button(label="Join Setter (0/2)",          style=discord.ButtonStyle.primary, custom_id="q_s",  row=0)
    async def btn_setter(self, i: discord.Interaction, b: discord.ui.Button):
        await self._join(i, ROLE_SETTER)

    @discord.ui.button(label="Join Outside Hitter (0/4)",  style=discord.ButtonStyle.success, custom_id="q_oh", row=0)
    async def btn_oh(self, i: discord.Interaction, b: discord.ui.Button):
        await self._join(i, ROLE_OUTSIDE_HITTER)

    @discord.ui.button(label="Join Middle Blocker (0/4)",  style=discord.ButtonStyle.success, custom_id="q_mb", row=1)
    async def btn_mb(self, i: discord.Interaction, b: discord.ui.Button):
        await self._join(i, ROLE_MIDDLE_BLOCKER)

    @discord.ui.button(label="Join Opposite Hitter (0/2)", style=discord.ButtonStyle.primary, custom_id="q_op", row=1)
    async def btn_op(self, i: discord.Interaction, b: discord.ui.Button):
        await self._join(i, ROLE_OPPOSITE_HITTER)

    @discord.ui.button(label="Leave Queue", style=discord.ButtonStyle.danger, custom_id="q_lv", row=2)
    async def btn_leave(self, i: discord.Interaction, b: discord.ui.Button):
        if not isinstance(i.user, discord.Member):
            return
        await i.response.defer(ephemeral=True)

        uid   = database.did(i.user.id)
        lock  = self.cog.get_lock(self.mn)
        error_msg: str | None = None
        counts: dict[str, int] = {}

        async with lock:
            async with database.conn() as c:
                match_row = await db_get_match(c, self.mn)
                if not match_row or match_row["status"] != "queue_open":
                    error_msg = "This queue is no longer open."
                elif not await _q(c, "SELECT 1 FROM mm_match_players WHERE match_number=$1 AND discord_id=$2", self.mn, uid):
                    error_msg = "You are not in this queue."
                else:
                    await _x(c, "DELETE FROM mm_match_players WHERE match_number=$1 AND discord_id=$2", self.mn, uid)
                    rows = await db_get_queue_rows(c, self.mn)
                    for r in rows:
                        counts[r["role_pref"]] = counts.get(r["role_pref"], 0) + 1

        if error_msg:
            await i.followup.send(error_msg, ephemeral=True)
            return

        self._update_labels(counts)
        updater = self.cog.get_updater(self.mn)
        if updater:
            updater.mark_dirty(i.guild, self)
        await i.followup.send("You left the queue.", ephemeral=True)

    @property
    def bot(self) -> commands.Bot:
        return self.cog.bot


# ---------------------------------------------------------------

class VoteView(discord.ui.View):
    """
    Random Teams vs Captain Picks vote.

    Concurrency:
    - self.votes is a dict keyed by discord_id (string).
      It is only written inside the match lock, so no race condition.
    - self.resolved is set inside the lock before any async work begins,
      making double-resolution impossible.
    - The vote count stored in self.total comes from the DB count at
      queue-full time and is never mutated, so reading it outside the
      lock is safe.
    """

    VOTE_SECONDS = 30

    def __init__(self, cog: "MMCog", match_number: int, total: int):
        super().__init__(timeout=self.VOTE_SECONDS)
        self.cog      = cog
        self.mn       = match_number
        self.total    = total
        self.votes:   dict[str, str] = {}
        self.message: discord.Message | None = None
        self.resolved = False
        self._seconds_left = self.VOTE_SECONDS
        self._countdown_task: asyncio.Task | None = None

        for item in self.children:
            if isinstance(item, discord.ui.Button):
                if item.custom_id == "vr":
                    item.custom_id = f"vr_{match_number}"
                elif item.custom_id == "vc":
                    item.custom_id = f"vc_{match_number}"

    def start_countdown(self) -> None:
        """Start the 1-second countdown task that updates the embed timer."""
        if self._countdown_task is None or self._countdown_task.done():
            self._countdown_task = asyncio.create_task(self._run_countdown())

    async def _run_countdown(self) -> None:
        """Edit the vote embed every second to show the remaining time."""
        import time as _time
        start = _time.monotonic()
        while not self.resolved:
            await asyncio.sleep(1)
            if self.resolved:
                break
            elapsed = int(_time.monotonic() - start)
            self._seconds_left = max(0, self.VOTE_SECONDS - elapsed)
            if self.message is None:
                continue
            async with database.conn() as c:
                match_row = await db_get_match(c, self.mn)
                if not match_row:
                    break
                rv, cv = self._tally()
                emb = embed_vote(match_row, rv, cv, len(self.votes), self.total, self._seconds_left)
            try:
                await self.message.edit(embed=emb, view=self)
            except discord.HTTPException:
                pass

    def _tally(self) -> tuple[int, int]:
        rv = sum(1 for v in self.votes.values() if v == "r")
        cv = sum(1 for v in self.votes.values() if v == "c")
        return rv, cv

    async def _vote(self, interaction: discord.Interaction, choice: str):
        if not isinstance(interaction.user, discord.Member):
            return
        await interaction.response.defer(ephemeral=True)

        lock = self.cog.get_lock(self.mn)
        should_resolve = False
        emb = None

        async with lock:
            if self.resolved:
                await interaction.followup.send("Voting has already ended.", ephemeral=True)
                return

            async with database.conn() as c:
                match_row = await db_get_match(c, self.mn)
                if not match_row or match_row["status"] != "team_format_vote":
                    await interaction.followup.send("Voting is no longer active.", ephemeral=True)
                    return

                uid     = database.did(interaction.user.id)
                present = await _q(c,
                    "SELECT 1 FROM mm_match_players WHERE match_number=$1 AND discord_id=$2",
                    self.mn, uid)
                if not present:
                    await interaction.followup.send("Only players in this queue can vote.", ephemeral=True)
                    return

                self.votes[uid] = choice

                if len(self.votes) >= self.total:
                    # Mark resolved inside the lock before releasing it,
                    # so no other coroutine can race to resolve too.
                    self.resolved = True
                    should_resolve = True
                else:
                    rv, cv = self._tally()
                    emb = embed_vote(match_row, rv, cv, len(self.votes), self.total)

        # Outside the lock.
        if should_resolve:
            await self._do_resolve(interaction.guild)
            await interaction.followup.send("Vote registered — resolving now.", ephemeral=True)
        else:
            if emb and self.message:
                try:
                    await self.message.edit(embed=emb, view=self)
                except discord.HTTPException:
                    pass
            await interaction.followup.send("Vote registered.", ephemeral=True)

    @discord.ui.button(label="Random Teams",  style=discord.ButtonStyle.success, custom_id="vr")
    async def vote_random(self, i: discord.Interaction, b: discord.ui.Button):
        await self._vote(i, "r")

    @discord.ui.button(label="Captain Picks", style=discord.ButtonStyle.primary, custom_id="vc")
    async def vote_captains(self, i: discord.Interaction, b: discord.ui.Button):
        await self._vote(i, "c")

    async def on_timeout(self):
        # discord.py timeout is the final authority if not all players voted.
        # Close interaction buttons immediately and resolve the state once.
        lock = self.cog.get_lock(self.mn)
        async with lock:
            if self.resolved:
                return
            self.resolved = True
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        guild = self.cog.bot.get_guild(config.GUILD_ID)
        await self._do_resolve(guild)

    async def _do_resolve(self, guild: discord.Guild | None) -> None:
        """
        Execute the vote result. Must be called with self.resolved = True
        already set (inside the lock), but runs its DB + Discord work here,
        outside the lock, to avoid holding the lock during network I/O.
        """
        self.stop()
        self.cog.clear_vote_view(self.mn)
        if self._countdown_task and not self._countdown_task.done():
            self._countdown_task.cancel()

        rv, cv   = self._tally()
        use_rand = rv > cv

        async with database.conn() as c:
            match_row = await db_get_match(c, self.mn)
            if not match_row or match_row["status"] != "team_format_vote":
                return

            if use_rand:
                for role in ROLE_ORDER:
                    players = await _qa(c,
                        "SELECT discord_id FROM mm_match_players WHERE match_number=$1 AND role_pref=$2",
                        self.mn, role)
                    ids = [r["discord_id"] for r in players]
                    random.shuffle(ids)
                    half    = len(ids) // 2
                    extra   = bool(len(ids) - 2 * half) and random.random() < 0.5
                    a_count = half + (1 if extra else 0)
                    for d in ids[:a_count]:
                        await _x(c, "UPDATE mm_match_players SET team_side='A' WHERE match_number=$1 AND discord_id=$2", self.mn, d)
                    for d in ids[a_count:]:
                        await _x(c, "UPDATE mm_match_players SET team_side='B' WHERE match_number=$1 AND discord_id=$2", self.mn, d)
                await _x(c, "UPDATE mm_matches SET status='ready_to_start' WHERE match_number=$1", self.mn)
                match_row = await db_get_match(c, self.mn)
                team_a    = await db_get_team(c, self.mn, "A")
                team_b    = await db_get_team(c, self.mn, "B")
                emb  = embed_ready(guild, match_row, team_a, team_b)
                view = StartMatchView(self.cog, self.mn)
            else:
                await _x(c, "UPDATE mm_matches SET status='captains_pending' WHERE match_number=$1", self.mn)
                match_row = await db_get_match(c, self.mn)
                all_rows  = await db_get_queue_rows(c, self.mn)
                emb  = embed_captains(guild, match_row, all_rows)
                view = CaptainSetupView(self.cog, self.mn)

        if self.message:
            try:
                await self.message.edit(embed=emb, view=view)
            except discord.HTTPException as exc:
                print(f"[MATCHMAKING] Failed to update vote message for {self.mn}: {exc}")


# ---------------------------------------------------------------

class CaptainSelect(discord.ui.Select):
    def __init__(self, cog: "MMCog", mn: int, slot: int, options):
        self.cog  = cog
        self.mn   = mn
        self.slot = slot
        super().__init__(placeholder=f"Select Captain {slot}", min_values=1, max_values=1, options=options[:25])

    @staticmethod
    async def build_options(c, match_row, guild) -> list[discord.SelectOption]:
        rows  = await _qa(c,
            "SELECT * FROM mm_match_players WHERE match_number=$1 ORDER BY priority_weight DESC, id ASC",
            match_row["match_number"])
        taken = {match_row["captain1_discord_id"], match_row["captain2_discord_id"]}
        taken.discard(None)
        opts  = []
        for row in rows:
            if row["discord_id"] in taken:
                continue
            name = display_name(guild, row["discord_id"])
            rl   = r_label(row["role_pref"])
            w    = row["priority_weight"] or 0
            opts.append(discord.SelectOption(
                label=(f"⭐ {name}" if w else name)[:100],
                value=row["discord_id"],
                description=(f"{rl} • VIP priority" if w else rl)[:100],
            ))
        return opts

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not can_manage_mm(interaction.user):
            await interaction.response.send_message("Only Match Organizer can set captains.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        lock = self.cog.get_lock(self.mn)
        both_set  = False
        match_row = None
        available = team_a = team_b = turn = None

        async with lock:
            async with database.conn() as c:
                match_row = await db_get_match(c, self.mn)
                if not match_row or match_row["status"] != "captains_pending":
                    await interaction.followup.send("Captain setup is no longer active.", ephemeral=True)
                    return

                col = "captain1_discord_id" if self.slot == 1 else "captain2_discord_id"
                await _x(c, f"UPDATE mm_matches SET {col}=$1 WHERE match_number=$2", self.values[0], self.mn)
                match_row = await db_get_match(c, self.mn)

                if match_row["captain1_discord_id"] and match_row["captain2_discord_id"]:
                    both_set = True
                    fp = random.choice([match_row["captain1_discord_id"], match_row["captain2_discord_id"]])
                    await _x(c, "UPDATE mm_matches SET first_picker_discord_id=$1, status='draft' WHERE match_number=$2", fp, self.mn)
                    await _x(c, "UPDATE mm_match_players SET team_side='A', captain=true, pick_order=0 WHERE match_number=$1 AND discord_id=$2", self.mn, match_row["captain1_discord_id"])
                    await _x(c, "UPDATE mm_match_players SET team_side='B', captain=true, pick_order=0 WHERE match_number=$1 AND discord_id=$2", self.mn, match_row["captain2_discord_id"])
                    match_row = await db_get_match(c, self.mn)
                    available = await db_get_available(c, self.mn)
                    team_a    = await db_get_team(c, self.mn, "A")
                    team_b    = await db_get_team(c, self.mn, "B")
                    turn      = await db_turn_side(c, match_row)

        # Discord I/O outside the lock.
        if both_set and interaction.guild and match_row["queue_channel_id"] and match_row["queue_message_id"]:
            ch = interaction.guild.get_channel(int(match_row["queue_channel_id"]))
            if isinstance(ch, discord.TextChannel):
                try:
                    msg = await ch.fetch_message(int(match_row["queue_message_id"]))
                    await msg.edit(
                        embed=embed_draft(interaction.guild, match_row, team_a, team_b, available, turn),
                        view=DraftView(self.cog, self.mn, available),
                    )
                except discord.HTTPException:
                    pass

        await interaction.followup.send(f"Captain {self.slot} set.", ephemeral=True)


class CaptainSelectView(discord.ui.View):
    def __init__(self, cog, mn, slot, options):
        super().__init__(timeout=120)
        self.add_item(CaptainSelect(cog, mn, slot, options))


class CaptainSetupView(discord.ui.View):
    def __init__(self, cog: "MMCog", mn: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.mn  = mn

    @discord.ui.button(label="Set Captain 1", style=discord.ButtonStyle.primary)
    async def cap1(self, i: discord.Interaction, b: discord.ui.Button):
        if not isinstance(i.user, discord.Member) or not can_manage_mm(i.user):
            await i.response.send_message("Only Match Organizer can set captains.", ephemeral=True)
            return
        await i.response.defer(ephemeral=True)
        async with database.conn() as c:
            mr   = await db_get_match(c, self.mn)
            opts = await CaptainSelect.build_options(c, mr, i.guild)
        await i.followup.send("Choose Captain 1:", view=CaptainSelectView(self.cog, self.mn, 1, opts), ephemeral=True)

    @discord.ui.button(label="Set Captain 2", style=discord.ButtonStyle.secondary)
    async def cap2(self, i: discord.Interaction, b: discord.ui.Button):
        if not isinstance(i.user, discord.Member) or not can_manage_mm(i.user):
            await i.response.send_message("Only Match Organizer can set captains.", ephemeral=True)
            return
        await i.response.defer(ephemeral=True)
        async with database.conn() as c:
            mr   = await db_get_match(c, self.mn)
            opts = await CaptainSelect.build_options(c, mr, i.guild)
        await i.followup.send("Choose Captain 2:", view=CaptainSelectView(self.cog, self.mn, 2, opts), ephemeral=True)


# ---------------------------------------------------------------

class PickButton(discord.ui.Button):
    """
    Draft pick button.

    Concurrency fix (identified in review):
    The pick operation now runs inside the match lock AND inside a DB
    transaction. This prevents two simultaneous pick interactions from
    both passing the turn/player checks and both committing — which
    would create duplicate assignments or out-of-order picks.

    The lock serialises the Python-level check-then-act.
    The transaction ensures the DB writes are atomic.
    """

    def __init__(self, cog, mn: int, player_did: str, label: str, row: int):
        super().__init__(label=label[:80], style=discord.ButtonStyle.primary, row=row)
        self.cog        = cog
        self.mn         = mn
        self.player_did = player_did

    async def callback(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member):
            return
        await interaction.response.defer()

        uid  = database.did(interaction.user.id)
        lock = self.cog.get_lock(self.mn)

        error_msg: str | None = None
        draft_done = False
        match_row  = None
        team_a = team_b = remaining = new_turn = None

        async with lock:
            # Use a transaction so the pick is atomic: if anything fails
            # mid-way, the DB state is left unchanged.
            async with database.transaction() as c:
                match_row = await db_get_match(c, self.mn)
                if not match_row or match_row["status"] != "draft":
                    error_msg = "Draft is no longer active."
                else:
                    cap_side = await db_captain_side(c, self.mn, uid)
                    if not cap_side:
                        error_msg = "Only captains can pick players."
                    else:
                        turn = await db_turn_side(c, match_row)
                        if cap_side != turn:
                            error_msg = "It is not your turn."
                        else:
                            player_row = await _q(c,
                                "SELECT * FROM mm_match_players WHERE match_number=$1 AND discord_id=$2 AND team_side IS NULL",
                                self.mn, self.player_did)
                            if not player_row:
                                error_msg = "Player no longer available."
                            else:
                                role     = player_row["role_pref"]
                                tri_row  = await _q(c, "SELECT COUNT(*) AS n FROM mm_match_players WHERE match_number=$1 AND role_pref=$2", self.mn, role)
                                tri      = tri_row["n"] if tri_row else ROLE_MAX_TOTAL.get(role, 0)
                                max_rc   = math.ceil(tri / 2) if tri else 0
                                cur_rc   = await db_role_count_team(c, self.mn, cap_side, role)

                                if cur_rc >= max_rc:
                                    error_msg = f"Your team already has max {r_label(role)}s."
                                else:
                                    pick_n = await db_pick_count(c, self.mn)
                                    await _x(c,
                                        "UPDATE mm_match_players SET team_side=$1, pick_order=$2 WHERE match_number=$3 AND discord_id=$4",
                                        cap_side, pick_n + 1, self.mn, self.player_did)

                                    remaining = await db_get_available(c, self.mn)

                                    if not remaining:
                                        await _x(c, "UPDATE mm_matches SET status='ready_to_start' WHERE match_number=$1", self.mn)
                                        draft_done = True

                                    match_row = await db_get_match(c, self.mn)
                                    team_a    = await db_get_team(c, self.mn, "A")
                                    team_b    = await db_get_team(c, self.mn, "B")
                                    if not draft_done:
                                        new_turn = await db_turn_side(c, match_row)

        # Discord I/O outside the lock.
        if error_msg:
            await interaction.followup.send(error_msg, ephemeral=True)
            return

        if draft_done:
            await interaction.edit_original_response(
                embed=embed_ready(interaction.guild, match_row, team_a, team_b),
                view=StartMatchView(self.cog, self.mn),
            )
        else:
            await interaction.edit_original_response(
                embed=embed_draft(interaction.guild, match_row, team_a, team_b, remaining, new_turn),
                view=DraftView(self.cog, self.mn, remaining),
            )


class DraftView(discord.ui.View):
    def __init__(self, cog, mn: int, available: list):
        super().__init__(timeout=None)
        self.cog = cog
        self.mn  = mn
        guild    = cog.bot.get_guild(config.GUILD_ID)
        for idx, row in enumerate(available[:25]):
            name  = display_name(guild, row["discord_id"])
            label = f"{name} [{r_short(row['role_pref'])}]"
            self.add_item(PickButton(cog, mn, row["discord_id"], label, min(idx // 5, 4)))


# ---------------------------------------------------------------

class StartModal(discord.ui.Modal, title="Start Match"):
    link = discord.ui.TextInput(
        label="Private Server Link", style=discord.TextStyle.paragraph,
        required=True, placeholder="Paste the game's private server link here...")

    def __init__(self, cog, mn: int):
        super().__init__()
        self.cog = cog
        self.mn  = mn

    async def on_submit(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not can_manage_mm(interaction.user):
            await interaction.response.send_message("Only Match Organizer can start the match.", ephemeral=True)
            return
        await interaction.response.defer()

        async with database.conn() as c:
            match_row = await db_get_match(c, self.mn)
            if not match_row or match_row["status"] != "ready_to_start":
                await interaction.followup.send("Match is not ready to start.", ephemeral=True)
                return
            ta = await db_get_team(c, self.mn, "A")
            tb = await db_get_team(c, self.mn, "B")

        guild    = interaction.guild
        category = guild.get_channel(MATCHMAKING_CATEGORY_ID) if guild else None
        if not isinstance(category, discord.CategoryChannel):
            await interaction.followup.send("Matchmaking category not found.", ephemeral=True)
            return

        mor = guild.get_role(MATCH_ORGANIZER_ROLE_ID)

        def base_text_ow():
            ow = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
                interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            }
            if mor:
                ow[mor] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
            return ow

        def base_voice_ow():
            ow = {
                guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True, move_members=True),
                interaction.user: discord.PermissionOverwrite(view_channel=True, connect=True, move_members=True),
            }
            if mor:
                ow[mor] = discord.PermissionOverwrite(view_channel=True, connect=True, move_members=True, speak=True)
            return ow

        text_ow = base_text_ow()
        for row in ta + tb:
            m = guild.get_member(int(row["discord_id"]))
            if m:
                text_ow[m] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        ow_a = base_voice_ow()
        for row in ta:
            m = guild.get_member(int(row["discord_id"]))
            if m: ow_a[m] = discord.PermissionOverwrite(view_channel=True, connect=True)

        ow_b = base_voice_ow()
        for row in tb:
            m = guild.get_member(int(row["discord_id"]))
            if m: ow_b[m] = discord.PermissionOverwrite(view_channel=True, connect=True)

        # Create all three channels in parallel — pure Discord API, no asyncpg.
        tc, va, vb = await asyncio.gather(
            guild.create_text_channel(f"mm-{self.mn}", category=category, overwrites=text_ow),
            guild.create_voice_channel(f"MM #{self.mn} • Team A", category=category, overwrites=ow_a),
            guild.create_voice_channel(f"MM #{self.mn} • Team B", category=category, overwrites=ow_b),
        )

        sp   = random.random() < SPECIAL_MATCH_CHANCE
        mult = SPECIAL_MATCH_MULTIPLIER if sp else 1

        async with database.conn() as c:
            await _x(c, """
                UPDATE mm_matches
                SET status='in_progress', private_server_link=$1,
                    text_channel_id=$2, team_a_voice_id=$3, team_b_voice_id=$4,
                    is_special=$5, special_multiplier=$6, started_at=$7
                WHERE match_number=$8
                """,
                str(self.link), database.did(tc.id), database.did(va.id), database.did(vb.id),
                sp, mult, now(), self.mn)
            match_row = await db_get_match(c, self.mn)

        view = InProgressView(self.cog, self.mn)
        emb  = embed_started(guild, match_row, ta, tb)
        await tc.send(embed=emb, view=view)
        await interaction.edit_original_response(embed=emb, view=view)


class StartMatchView(discord.ui.View):
    def __init__(self, cog, mn: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.mn  = mn

    @discord.ui.button(label="Start Match", style=discord.ButtonStyle.success)
    async def start(self, i: discord.Interaction, b: discord.ui.Button):
        if not isinstance(i.user, discord.Member) or not can_manage_mm(i.user):
            await i.response.send_message("Only Match Organizer can start the match.", ephemeral=True)
            return
        await i.response.send_modal(StartModal(self.cog, self.mn))


class InProgressView(discord.ui.View):
    def __init__(self, cog, mn: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.mn  = mn

    @discord.ui.button(label="Replace Player", style=discord.ButtonStyle.primary)
    async def replace(self, i: discord.Interaction, b: discord.ui.Button):
        if not isinstance(i.user, discord.Member) or not can_manage_mm(i.user):
            await i.response.send_message("Only Match Organizer can replace players.", ephemeral=True)
            return
        async with database.conn() as c:
            players = await db_get_all_players(c, self.mn)
        await i.response.send_message("Choose player to replace:",
            view=ReplacePickView(self.cog, self.mn, players, i.guild), ephemeral=True)

    @discord.ui.button(label="Finish Match", style=discord.ButtonStyle.success)
    async def finish(self, i: discord.Interaction, b: discord.ui.Button):
        if not isinstance(i.user, discord.Member) or not can_manage_mm(i.user):
            await i.response.send_message("Only Match Organizer can finish the match.", ephemeral=True)
            return
        await i.response.send_message("Choose Winner Team:", view=FinishTeamView(self.cog, self.mn), ephemeral=True)


# ---------------------------------------------------------------  replace flow

class ReplaceModal(discord.ui.Modal, title="Replace Player"):
    new_player = discord.ui.TextInput(
        label="New player mention or ID", required=True, placeholder="@user or user id")

    def __init__(self, cog, mn: int, old_did: str):
        super().__init__()
        self.cog     = cog
        self.mn      = mn
        self.old_did = old_did

    async def on_submit(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not can_manage_mm(interaction.user):
            await interaction.response.send_message("Only Match Organizer can replace players.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        raw        = str(self.new_player).strip()
        new_member = None
        if interaction.guild:
            if raw.startswith("<@") and raw.endswith(">"):
                cleaned = raw.replace("<@", "").replace("!", "").replace(">", "")
                if cleaned.isdigit():
                    new_member = interaction.guild.get_member(int(cleaned))
            elif raw.isdigit():
                new_member = interaction.guild.get_member(int(raw))

        if not new_member:
            await interaction.followup.send("Could not find that member.", ephemeral=True)
            return

        new_did = database.did(new_member.id)

        async with database.conn() as c:
            match_row = await db_get_match(c, self.mn)
            if not match_row or match_row["status"] != "in_progress":
                await interaction.followup.send("Match is not in progress.", ephemeral=True)
                return
            if await db_is_busy(c, new_did):
                await interaction.followup.send("That player is already in another queue/match.", ephemeral=True)
                return
            old_row = await _q(c, "SELECT * FROM mm_match_players WHERE match_number=$1 AND discord_id=$2", self.mn, self.old_did)
            if not old_row:
                await interaction.followup.send("Old player not found.", ephemeral=True)
                return
            if await _q(c, "SELECT 1 FROM mm_match_players WHERE match_number=$1 AND discord_id=$2", self.mn, new_did):
                await interaction.followup.send("New player is already in this match.", ephemeral=True)
                return
            await _x(c,
                "UPDATE mm_match_players SET discord_id=$1 WHERE match_number=$2 AND discord_id=$3",
                new_did, self.mn, self.old_did)
            await _x(c, """
                INSERT INTO mm_replacements
                    (match_number, old_discord_id, new_discord_id, replaced_by_discord_id, penalty_applied)
                VALUES ($1, $2, $3, $4, $5)
                """, self.mn, self.old_did, new_did, database.did(interaction.user.id), True)
            await adjust_elo_only(c, self.old_did, match_row["season_number"], REPLACE_LEAVE_PENALTY)
            match_row = await db_get_match(c, self.mn)
            team_a    = await db_get_team(c, self.mn, "A")
            team_b    = await db_get_team(c, self.mn, "B")

        guild = interaction.guild
        if guild:
            old_member = guild.get_member(int(self.old_did))
            side       = old_row["team_side"]
            if match_row["text_channel_id"]:
                tc = guild.get_channel(int(match_row["text_channel_id"]))
                if isinstance(tc, discord.TextChannel):
                    try:
                        await tc.set_permissions(new_member, view_channel=True, send_messages=True)
                        if old_member:
                            await tc.set_permissions(old_member, overwrite=None)
                    except discord.HTTPException:
                        pass
            vc_id = match_row["team_a_voice_id"] if side == "A" else match_row["team_b_voice_id"]
            if vc_id:
                vc = guild.get_channel(int(vc_id))
                if isinstance(vc, discord.VoiceChannel):
                    try:
                        await vc.set_permissions(new_member, view_channel=True, connect=True)
                        if old_member:
                            await vc.set_permissions(old_member, overwrite=None)
                    except discord.HTTPException:
                        pass
            if match_row["queue_channel_id"] and match_row["queue_message_id"]:
                qc = guild.get_channel(int(match_row["queue_channel_id"]))
                if isinstance(qc, discord.TextChannel):
                    try:
                        qm = await qc.fetch_message(int(match_row["queue_message_id"]))
                        await qm.edit(embed=embed_started(guild, match_row, team_a, team_b),
                                      view=InProgressView(self.cog, self.mn))
                    except discord.HTTPException:
                        pass
            if match_row["text_channel_id"]:
                tc = guild.get_channel(int(match_row["text_channel_id"]))
                if isinstance(tc, discord.TextChannel):
                    try:
                        await tc.send(
                            f"{mention(guild, self.old_did)} was replaced by {new_member.mention}. "
                            f"Penalty: `{REPLACE_LEAVE_PENALTY}` ELO.")
                    except discord.HTTPException:
                        pass

        asyncio.create_task(upsert_profile_from_member(new_member))
        await interaction.followup.send(
            f"Replaced. {mention(guild, self.old_did)} received `{REPLACE_LEAVE_PENALTY}` ELO.",
            ephemeral=True)


class ReplaceSelect(discord.ui.Select):
    def __init__(self, cog, mn, players, guild):
        self.cog = cog
        self.mn  = mn
        opts = [discord.SelectOption(
            label=display_name(guild, r["discord_id"]),
            value=r["discord_id"],
            description=f"{side_label(r['team_side']) if r['team_side'] else 'No Team'} • {r_label(r['role_pref'])}"[:100],
        ) for r in players]
        super().__init__(placeholder="Select player to replace", min_values=1, max_values=1, options=opts[:25])

    async def callback(self, i: discord.Interaction):
        if not isinstance(i.user, discord.Member) or not can_manage_mm(i.user):
            await i.response.send_message("Only Match Organizer can replace players.", ephemeral=True)
            return
        await i.response.send_modal(ReplaceModal(self.cog, self.mn, self.values[0]))


class ReplacePickView(discord.ui.View):
    def __init__(self, cog, mn, players, guild):
        super().__init__(timeout=120)
        self.add_item(ReplaceSelect(cog, mn, players, guild))


# ---------------------------------------------------------------  finish flow

class FinishTeamSelect(discord.ui.Select):
    def __init__(self, cog, mn):
        self.cog = cog
        self.mn  = mn
        super().__init__(
            placeholder="Select Winner Team", min_values=1, max_values=1,
            options=[discord.SelectOption(label="Team A", value="A"),
                     discord.SelectOption(label="Team B", value="B")])

    async def callback(self, i: discord.Interaction):
        if not isinstance(i.user, discord.Member) or not can_manage_mm(i.user):
            await i.response.send_message("Only Match Organizer can finish matches.", ephemeral=True)
            return
        ws = self.values[0]
        ls = "B" if ws == "A" else "A"
        async with database.conn() as c:
            wp = await db_get_team(c, self.mn, ws)
        await i.response.edit_message(
            content=f"Winner: **{side_label(ws)}** — now choose Winner MVP.",
            view=FinishWMVPView(self.cog, self.mn, ws, ls, wp, i.guild))

class FinishTeamView(discord.ui.View):
    def __init__(self, cog, mn):
        super().__init__(timeout=120)
        self.add_item(FinishTeamSelect(cog, mn))

class FinishWMVPSelect(discord.ui.Select):
    def __init__(self, cog, mn, ws, ls, players, guild):
        self.cog = cog; self.mn = mn; self.ws = ws; self.ls = ls
        opts = [discord.SelectOption(
            label=display_name(guild, r["discord_id"]), value=r["discord_id"],
            description=r_label(r["role_pref"])[:100]) for r in players]
        super().__init__(placeholder="Select Winner MVP", min_values=1, max_values=1, options=opts[:25])

    async def callback(self, i: discord.Interaction):
        wmvp = self.values[0]
        async with database.conn() as c:
            lp = await db_get_team(c, self.mn, self.ls)
        await i.response.edit_message(
            content=f"Winner: **{side_label(self.ws)}** — now choose Loser MVP.",
            view=FinishLMVPView(self.cog, self.mn, self.ws, self.ls, wmvp, lp, i.guild))

class FinishWMVPView(discord.ui.View):
    def __init__(self, cog, mn, ws, ls, players, guild):
        super().__init__(timeout=120)
        self.add_item(FinishWMVPSelect(cog, mn, ws, ls, players, guild))

class FinishLMVPSelect(discord.ui.Select):
    def __init__(self, cog, mn, ws, ls, wmvp, players, guild):
        self.cog = cog; self.mn = mn; self.ws = ws; self.ls = ls; self.wmvp = wmvp
        opts = [discord.SelectOption(
            label=display_name(guild, r["discord_id"]), value=r["discord_id"],
            description=r_label(r["role_pref"])[:100]) for r in players]
        super().__init__(placeholder="Select Loser MVP", min_values=1, max_values=1, options=opts[:25])

    async def callback(self, i: discord.Interaction):
        if not isinstance(i.user, discord.Member) or not can_manage_mm(i.user):
            await i.response.send_message("Only Match Organizer can finish matches.", ephemeral=True)
            return
        await i.response.send_modal(
            FinishScoreModal(self.cog, self.mn, self.ws, self.ls, self.wmvp, self.values[0]))

class FinishLMVPView(discord.ui.View):
    def __init__(self, cog, mn, ws, ls, wmvp, players, guild):
        super().__init__(timeout=120)
        self.add_item(FinishLMVPSelect(cog, mn, ws, ls, wmvp, players, guild))

class FinishScoreModal(discord.ui.Modal, title="Finish Match"):
    final_score = discord.ui.TextInput(
        label="Final Score (Team A - Team B)", style=discord.TextStyle.paragraph,
        required=True, placeholder="Example: 25-20, 22-25, 15-11")

    def __init__(self, cog, mn, ws, ls, wmvp, lmvp):
        super().__init__()
        self.cog  = cog;  self.mn   = mn
        self.ws   = ws;   self.ls   = ls
        self.wmvp = wmvp; self.lmvp = lmvp

    async def on_submit(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not can_manage_mm(interaction.user):
            await interaction.response.send_message("Only Match Organizer can finish matches.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ok, err = await self.cog.finalize(
            interaction, self.mn, self.ws, self.ls,
            self.wmvp, self.lmvp, str(self.final_score).strip())
        if not ok:
            await interaction.followup.send(err, ephemeral=True)
        else:
            await interaction.followup.send(f"Match #{self.mn} finished.", ephemeral=True)


# ============================================================
# MAIN COG
# ============================================================

TEAM_CHOICES = [
    app_commands.Choice(name="Team A", value="A"),
    app_commands.Choice(name="Team B", value="B"),
]


class MMCog(commands.Cog):
    mm     = app_commands.Group(name="mm",     description="Matchmaking commands", guild_ids=[config.GUILD_ID])
    season = app_commands.Group(name="season", description="Season commands",      guild_ids=[config.GUILD_ID])

    def __init__(self, bot: commands.Bot):
        self.bot          = bot
        self._locks:      dict[int, asyncio.Lock]  = {}
        self._vote_views: dict[int, VoteView]       = {}
        self._updaters:   dict[int, QueueUpdater]   = {}

    def get_lock(self, mn: int) -> asyncio.Lock:
        if mn not in self._locks:
            self._locks[mn] = asyncio.Lock()
        return self._locks[mn]

    def get_vote_view(self, mn: int) -> VoteView | None:
        return self._vote_views.get(mn)

    def set_vote_view(self, mn: int, v: VoteView) -> None:
        self._vote_views[mn] = v

    def clear_vote_view(self, mn: int) -> None:
        self._vote_views.pop(mn, None)

    def get_updater(self, mn: int) -> QueueUpdater | None:
        return self._updaters.get(mn)

    def set_updater(self, mn: int, u: QueueUpdater) -> None:
        self._updaters[mn] = u

    def clear_updater(self, mn: int) -> None:
        u = self._updaters.pop(mn, None)
        if u:
            u.stop()

    async def _send_queue_notify_dms(
        self,
        guild: discord.Guild | None,
        match_number: int,
        queue_url: str,
    ) -> None:
        """DM every player who opted into /mm notify when a new queue opens."""
        if not guild:
            return
        async with database.conn() as c:
            rows = await _qa(c,
                "SELECT discord_id FROM mm_players WHERE mm_notify = true")
        for row in rows:
            member = guild.get_member(int(row["discord_id"]))
            if member and not member.bot:
                try:
                    await member.send(
                        f"🏐 **New Matchmaking Queue opened! — #{match_number}**\n"
                        f"Jump in: {queue_url}\n\n"
                        f"*(Use `/mm notify off` to stop these DMs)*"
                    )
                except discord.Forbidden:
                    pass

    # ── finalize ────────────────────────────────────────────────────────

    async def finalize(self, interaction: discord.Interaction,
                       mn: int, ws: str, ls: str,
                       wmvp_id: str, lmvp_id: str, score_text: str):
        sets, err = parse_score(score_text)
        if err:
            return False, err
        deltas, err = calc_elo_deltas(sets, ws)
        if err:
            return False, err

        async with database.conn() as c:
            match_row = await db_get_match(c, mn)
            if not match_row:
                return False, "Match not found."
            if match_row["status"] != "in_progress":
                return False, "Match is not in progress."

            if not await _q(c, "SELECT 1 FROM mm_match_players WHERE match_number=$1 AND discord_id=$2 AND team_side=$3", mn, wmvp_id, ws):
                return False, "WMVP must belong to the winner team."
            if not await _q(c, "SELECT 1 FROM mm_match_players WHERE match_number=$1 AND discord_id=$2 AND team_side=$3", mn, lmvp_id, ls):
                return False, "LMVP must belong to the loser team."

            players   = await db_get_all_players(c, mn)
            season    = match_row["season_number"]
            vq        = is_vip_queue(match_row)
            sp        = is_special(match_row)
            mult      = match_row["special_multiplier"] or 1
            base_win  = deltas["winner_delta"] * (VIP_QUEUE_ELO_MULTIPLIER if vq else 1) * (mult if sp else 1)
            base_loss = deltas["loser_delta"]
            elo_chgs  = []

            for row in players:
                is_win = row["team_side"] == ws
                # VIP fetch uses its own connection — must be outside conn().
                vip = await vip_data.get_active_vip(row["discord_id"]) if is_win else None
                if is_win:
                    bd  = base_win + (WMVP_BONUS if row["discord_id"] == wmvp_id else 0)
                    vm  = (1.0 + vip_data.VIP_ELO_WIN_BONUS_PERCENT.get(vip["tier"], 0.0)) if vip else 1.0
                    d   = round(bd * vm)
                    res = await apply_elo(c, row["discord_id"], season, d, True,
                                          row["discord_id"] == wmvp_id, False)
                else:
                    d   = base_loss + (LMVP_REDUCTION if row["discord_id"] == lmvp_id else 0)
                    res = await apply_elo(c, row["discord_id"], season, d, False,
                                          False, row["discord_id"] == lmvp_id)
                if res:
                    res["vip_tier"] = vip["tier"] if vip else None
                    elo_chgs.append(res)

            await _x(c, """
                UPDATE mm_matches
                SET status='finished', winner_side=$1, loser_side=$2,
                    wmvp_discord_id=$3, lmvp_discord_id=$4,
                    final_score_text=$5, finished_at=$6
                WHERE match_number=$7
                """, ws, ls, wmvp_id, lmvp_id, deltas["display"], now(), mn)
            match_row = await db_get_match(c, mn)
            winners   = await db_get_team(c, mn, ws)
            losers    = await db_get_team(c, mn, ls)

        self.clear_updater(mn)

        guild = interaction.guild
        if guild:
            rc = guild.get_channel(MM_RESULTS_CHANNEL_ID)
            if isinstance(rc, discord.TextChannel):
                await rc.send(embed=embed_result(guild, match_row, winners, losers))
            ec = guild.get_channel(ELO_UPDATE_CHANNEL_ID)
            if isinstance(ec, discord.TextChannel):
                await ec.send(embed=embed_elo_update(guild, match_row, elo_chgs))
            if match_row["queue_channel_id"] and match_row["queue_message_id"]:
                qc = guild.get_channel(int(match_row["queue_channel_id"]))
                if isinstance(qc, discord.TextChannel):
                    try:
                        qm = await qc.fetch_message(int(match_row["queue_message_id"]))
                        await qm.edit(embed=embed_result(guild, match_row, winners, losers), view=None)
                    except discord.HTTPException:
                        pass
            for cid in [match_row["text_channel_id"], match_row["team_a_voice_id"], match_row["team_b_voice_id"]]:
                if not cid:
                    continue
                ch = guild.get_channel(int(cid))
                if ch:
                    try:
                        await ch.delete()
                    except discord.HTTPException:
                        pass
        return True, None

    # ── /season ─────────────────────────────────────────────────────────

    @season.command(name="start", description="Starts a new Matchmaking season")
    async def season_start(self, i: discord.Interaction, number: int):
        if not isinstance(i.user, discord.Member) or not can_manage_season(i.user):
            await i.response.send_message("Only Staff/Admin can start seasons.", ephemeral=True)
            return
        await i.response.defer()
        async with database.conn() as c:
            active = await db_get_season(c)
            if active:
                await i.followup.send(f"Season {active['number']} is already active.", ephemeral=True)
                return
            ex = await _q(c, "SELECT 1 FROM mm_seasons WHERE number=$1", number)
            if ex:
                await _x(c, "UPDATE mm_seasons SET is_active=true, started_at=$1, ended_at=NULL WHERE number=$2", now(), number)
            else:
                await _x(c, "INSERT INTO mm_seasons (number, is_active, started_at) VALUES ($1, true, $2)", number, now())
        await i.followup.send(f"Season {number} started.")

    @season.command(name="end", description="Ends the active Matchmaking season")
    async def season_end(self, i: discord.Interaction, number: int):
        if not isinstance(i.user, discord.Member) or not can_manage_season(i.user):
            await i.response.send_message("Only Staff/Admin can end seasons.", ephemeral=True)
            return
        await i.response.defer()
        async with database.conn() as c:
            active = await db_get_season(c)
            if not active or active["number"] != number:
                await i.followup.send("This season is not currently active.", ephemeral=True)
                return
            am = await _q(c,
                "SELECT 1 FROM mm_matches WHERE status IN ('queue_open','team_format_vote','captains_pending','draft','ready_to_start','in_progress') LIMIT 1")
            if am:
                await i.followup.send("Finish or cancel active queues/matches first.", ephemeral=True)
                return
            await _x(c, "UPDATE mm_seasons SET is_active=false, ended_at=$1 WHERE number=$2", now(), number)
        await i.followup.send(f"Season {number} ended.")

    @season.command(name="stats", description="Shows season stats")
    async def season_stats(self, i: discord.Interaction, number: int):
        await i.response.defer()
        async with database.conn() as c:
            sr = await _q(c, "SELECT * FROM mm_seasons WHERE number=$1", number)
            if not sr:
                await i.followup.send("Season not found.", ephemeral=True)
                return
            top = await _qa(c,
                "SELECT * FROM mm_season_players WHERE season_number=$1 ORDER BY (elo_gained-elo_lost) DESC, wins DESC LIMIT 10",
                number)
            tot = await _q(c,
                "SELECT COUNT(*) AS n FROM mm_matches WHERE season_number=$1 AND status='finished'",
                number)
        g     = i.guild
        lines = [f"`#{idx}` {mention(g, r['discord_id'])} • Net `{r['elo_gained']-r['elo_lost']}` • W-L `{r['wins']}-{r['losses']}`"
                 for idx, r in enumerate(top, 1)]
        e = discord.Embed(title=f"Season {number} Stats", color=discord.Color.orange())
        e.add_field(name="Status",           value="Active" if sr["is_active"] else "Closed", inline=True)
        e.add_field(name="Started",          value=str(sr["started_at"] or "—"),              inline=True)
        e.add_field(name="Ended",            value=str(sr["ended_at"]   or "—"),              inline=True)
        e.add_field(name="Finished Matches", value=str(tot["n"] if tot else 0),               inline=False)
        e.add_field(name="Top 10",           value=chr(10).join(lines) or "No data.",         inline=False)
        await i.followup.send(embed=e)

    # ── /mm ─────────────────────────────────────────────────────────────

    @mm.command(name="start", description="Starts a Matchmaking queue")
    async def mm_start(self, i: discord.Interaction, number: int):
        if not isinstance(i.user, discord.Member) or not can_manage_mm(i.user):
            await i.response.send_message("Only Match Organizer can use this command.", ephemeral=True)
            return
        await i.response.defer()
        async with database.conn() as c:
            season = await db_get_season(c)
            if not season:
                await i.followup.send("No active season. Use /season start first.", ephemeral=True)
                return
            ex = await db_get_match(c, number)
            if ex:
                if ex["status"] == "cancelled":
                    await _x(c, "DELETE FROM mm_match_players WHERE match_number=$1", number)
                    await _x(c, "DELETE FROM mm_matches WHERE match_number=$1", number)
                else:
                    await i.followup.send(f"Match #{number} already exists (status: `{ex['status']}`).", ephemeral=True)
                    return
            await _x(c, """
                INSERT INTO mm_matches
                    (match_number, season_number, status, created_by_discord_id, queue_channel_id)
                VALUES ($1, $2, 'queue_open', $3, $4)
                """,
                number, season["number"], database.did(i.user.id), database.did(i.channel_id))
            match_row = await db_get_match(c, number)

        view = JoinQueueView(self, number)
        sent = await i.followup.send(
            embed=embed_queue(i.guild, match_row, []), view=view, wait=True)

        async with database.conn() as c:
            await _x(c, "UPDATE mm_matches SET queue_message_id=$1 WHERE match_number=$2",
                     database.did(sent.id), number)

        # Register the debounced updater for this queue.
        updater = QueueUpdater(self.bot, number, sent.channel.id, sent.id)
        self.set_updater(number, updater)

        # Notify players who opted in to queue DMs.
        asyncio.create_task(self._send_queue_notify_dms(i.guild, number, sent.jump_url))

    @mm.command(name="cancel", description="Cancels a Matchmaking queue or match")
    async def mm_cancel(self, i: discord.Interaction, number: int):
        if not isinstance(i.user, discord.Member) or not can_manage_mm(i.user):
            await i.response.send_message("Only Match Organizer can cancel.", ephemeral=True)
            return
        async with database.conn() as c:
            mr = await db_get_match(c, number)
        if not mr:
            await i.response.send_message("Match not found.", ephemeral=True)
            return
        if mr["status"] not in ("queue_open","team_format_vote","captains_pending","draft","ready_to_start","in_progress"):
            await i.response.send_message("Only active queues/matches can be cancelled.", ephemeral=True)
            return
        await i.response.defer(ephemeral=True)
        prev = mr["status"]
        async with database.conn() as c:
            await _x(c, """
                UPDATE mm_matches
                SET status='cancelled', finished_at=$1,
                    is_special=COALESCE(is_special,false),
                    special_multiplier=COALESCE(special_multiplier,1)
                WHERE match_number=$2
                """, now(), number)
            mr     = await db_get_match(c, number)
            q_rows = await db_get_queue_rows(c, number)
            team_a = await db_get_team(c, number, "A")
            team_b = await db_get_team(c, number, "B")

        self.clear_updater(number)
        self.clear_vote_view(number)

        guild = i.guild
        if guild and mr["queue_channel_id"] and mr["queue_message_id"]:
            qc = guild.get_channel(int(mr["queue_channel_id"]))
            if isinstance(qc, discord.TextChannel):
                try:
                    qm  = await qc.fetch_message(int(mr["queue_message_id"]))
                    emb = (embed_cancelled_ip(guild, mr, team_a, team_b, i.user.id)
                           if prev == "in_progress"
                           else embed_cancelled(guild, mr, q_rows, i.user.id))
                    await qm.edit(embed=emb, view=None)
                except discord.HTTPException:
                    pass
            for cid in [mr["text_channel_id"], mr["team_a_voice_id"], mr["team_b_voice_id"]]:
                if not cid:
                    continue
                ch = guild.get_channel(int(cid))
                if ch:
                    try:
                        await ch.delete()
                    except discord.HTTPException:
                        pass
        await i.followup.send(f"Queue #{number} cancelled.", ephemeral=True)

    @mm.command(name="finish", description="Finishes an in-progress match")
    @app_commands.choices(winner_team=TEAM_CHOICES, loser_team=TEAM_CHOICES)
    async def mm_finish(self, i: discord.Interaction, number: int,
                        winner_team: app_commands.Choice[str], loser_team: app_commands.Choice[str],
                        wmvp: discord.Member, lmvp: discord.Member, final_score: str):
        if not isinstance(i.user, discord.Member) or not can_manage_mm(i.user):
            await i.response.send_message("Only Match Organizer can finish matches.", ephemeral=True)
            return
        if winner_team.value == loser_team.value:
            await i.response.send_message("Winner and loser teams must be different.", ephemeral=True)
            return
        await i.response.defer(ephemeral=True)
        ok, err = await self.finalize(i, number, winner_team.value, loser_team.value,
                                      database.did(wmvp.id), database.did(lmvp.id), final_score)
        if not ok:
            await i.followup.send(err, ephemeral=True)
        else:
            await i.followup.send(f"Match #{number} finished.", ephemeral=True)

    # ------------------------------------------------------------------
    # /mm notify on | off
    # ------------------------------------------------------------------

    @mm.command(name="notify", description="Toggle DM notifications when a new queue opens")
    @app_commands.describe(setting="on to receive DMs, off to stop")
    @app_commands.choices(setting=[
        app_commands.Choice(name="on",  value="on"),
        app_commands.Choice(name="off", value="off"),
    ])
    async def mm_notify(self, i: discord.Interaction, setting: app_commands.Choice[str]):
        if not isinstance(i.user, discord.Member):
            return
        await i.response.defer(ephemeral=True)

        uid    = database.did(i.user.id)
        enable = setting.value == "on"

        async with database.conn() as c:
            await _x(c,
                "INSERT INTO mm_players (discord_id, mm_notify) VALUES ($1, $2) "
                "ON CONFLICT (discord_id) DO UPDATE SET mm_notify = $2",
                uid, enable)

        status = "**enabled** ✅" if enable else "**disabled** ❌"
        await i.followup.send(
            f"Queue DM notifications {status}.\n"
            + ("You'll receive a DM whenever a new matchmaking queue opens." if enable
               else "You won't receive queue DMs anymore."),
            ephemeral=True
        )

    @mm.command(name="elo", description="Shows your Matchmaking ELO")
    async def mm_elo(self, i: discord.Interaction, member: discord.Member | None = None):
        target  = member or i.user
        did_val = database.did(target.id)
        await i.response.defer()
        # VIP uses its own pool slot — must be outside conn() block.
        vip = await vip_data.get_active_vip(target.id)
        async with database.conn() as c:
            await db_ensure_mm_player(c, did_val)
            row    = await _q(c, "SELECT * FROM mm_players WHERE discord_id=$1", did_val)
            season = await db_get_season(c)
            sp     = None
            if season:
                sp = await _q(c,
                    "SELECT * FROM mm_season_players WHERE season_number=$1 AND discord_id=$2",
                    season["number"], did_val)
        suf = f" • {vip_data.vip_tier_label(vip['tier'])}" if vip else ""
        e   = discord.Embed(title=f"{target.display_name} • MM Profile{suf}",
                            color=discord.Color.gold() if vip else discord.Color.blurple())
        e.add_field(name="ELO",      value=str(row["elo"]),    inline=True)
        e.add_field(name="Matches",  value=str(row["matches"]),inline=True)
        e.add_field(name="W-L",      value=f"{row['wins']}-{row['losses']}", inline=True)
        e.add_field(name="Win MVP",  value=str(row["win_mvp"]),  inline=True)
        e.add_field(name="Loss MVP", value=str(row["loss_mvp"]), inline=True)
        e.add_field(name="Total ELO",value=f"+{row['elo_gained_total']} / -{row['elo_lost_total']}", inline=True)
        if season and sp:
            e.add_field(name=f"Season {season['number']}",
                        value=f"Matches: `{sp['matches']}`\nW-L: `{sp['wins']}-{sp['losses']}`\nELO: `+{sp['elo_gained']} / -{sp['elo_lost']}`",
                        inline=False)
        await i.followup.send(embed=e)

    @mm.command(name="leaderboard", description="Shows the Matchmaking leaderboard")
    async def mm_leaderboard(self, i: discord.Interaction, page: int = 1, season_number: int | None = None):
        if page < 1:
            await i.response.send_message("Page must be ≥ 1.", ephemeral=True)
            return
        await i.response.defer()
        per, offset = 10, (page - 1) * 10
        g = i.guild
        async with database.conn() as c:
            if season_number is None:
                rows   = await _qa(c, "SELECT * FROM mm_players ORDER BY elo DESC, wins DESC LIMIT $1 OFFSET $2", per, offset)
                badges = await db_vip_badges(c, [r["discord_id"] for r in rows])
                lines  = [f"`#{offset+1+idx}` {mention(g, r['discord_id'])}{badges.get(r['discord_id'],'')} • ELO `{r['elo']}` • W-L `{r['wins']}-{r['losses']}`"
                          for idx, r in enumerate(rows)]
                title  = "MM Global Leaderboard"
            else:
                rows   = await _qa(c, "SELECT * FROM mm_season_players WHERE season_number=$1 ORDER BY (elo_gained-elo_lost) DESC, wins DESC LIMIT $2 OFFSET $3", season_number, per, offset)
                badges = await db_vip_badges(c, [r["discord_id"] for r in rows])
                lines  = [f"`#{offset+1+idx}` {mention(g, r['discord_id'])}{badges.get(r['discord_id'],'')} • Net `{r['elo_gained']-r['elo_lost']}` • W-L `{r['wins']}-{r['losses']}`"
                          for idx, r in enumerate(rows)]
                title  = f"MM Season {season_number} Leaderboard"
        e = discord.Embed(title=title, description=chr(10).join(lines) or "No data.", color=discord.Color.gold())
        e.set_footer(text=f"Page {page}")
        await i.followup.send(embed=e)

    @mm.command(name="vip", description="Shows Matchmaking VIP/VIP+ pricing and benefits")
    async def mm_vip(self, i: discord.Interaction):
        my_vip = await vip_data.get_active_vip(i.user.id)
        e = discord.Embed(
            title="NVL Matchmaking — VIP & VIP+",
            description=f"Buy on the site: {config.NVL_SITE_URL}/matchmaking#vip\nPix via Stripe. Valid {vip_data.VIP_DURATION_DAYS} days.",
            color=discord.Color.gold())
        e.add_field(name=f"VIP — {vip_data.VIP_PRICING['vip']['label']} / 30d",
                    value=f"• +{round(vip_data.VIP_ELO_WIN_BONUS_PERCENT['vip']*100)}% ELO on wins\n• Exclusive role + badge\n• Access to the Queue voice channel\n• VIP queue (2x ELO)", inline=False)
        e.add_field(name=f"VIP+ — {vip_data.VIP_PRICING['vip_plus']['label']} / 30d",
                    value=f"• +{round(vip_data.VIP_ELO_WIN_BONUS_PERCENT['vip_plus']*100)}% ELO on wins\n• Exclusive role + badge\n• Access to the Queue voice channel\n• VIP queue (2x ELO)", inline=False)
        val = f"**{vip_data.vip_tier_label(my_vip['tier'])}** until `{my_vip['expires_at']}`." if my_vip else "No active subscription."
        e.add_field(name="Your status", value=val, inline=False)
        await i.response.send_message(embed=e, ephemeral=True)

    @mm.command(name="addvip", description="Manually grants VIP or VIP+ to a player")
    @app_commands.describe(player="Player", tier="VIP or VIP+", days="Duration in days")
    @app_commands.choices(tier=[app_commands.Choice(name="VIP", value="vip"),
                                 app_commands.Choice(name="VIP+", value="vip_plus")])
    async def mm_addvip(self, i: discord.Interaction, player: discord.Member,
                        tier: app_commands.Choice[str], days: int = vip_data.VIP_DURATION_DAYS):
        if not isinstance(i.user, discord.Member) or not is_admin(i.user):
            await i.response.send_message("Only Administrator can grant VIP.", ephemeral=True)
            return
        if days <= 0:
            await i.response.send_message("Days must be > 0.", ephemeral=True)
            return
        await i.response.defer()
        await upsert_profile_from_member(player)
        tv  = tier.value
        exp = now() + timedelta(days=days)
        async with database.transaction() as c:
            pr = await c.fetchrow(
                "INSERT INTO vip_payments (discord_id, tier, amount_cents, provider, status, paid_at) VALUES ($1,$2,0,'admin_grant','paid',$3) RETURNING id",
                database.did(player.id), tv, now())
            await c.execute("UPDATE vip_subscriptions SET status='cancelled' WHERE discord_id=$1 AND status='active'", database.did(player.id))
            await c.execute("INSERT INTO vip_subscriptions (discord_id, tier, status, source_payment_id, role_applied, expires_at) VALUES ($1,$2,'active',$3,true,$4)",
                            database.did(player.id), tv, pr["id"], exp)
        if i.guild:
            rid   = config.VIP_PLUS_ROLE_ID if tv == "vip_plus" else config.VIP_ROLE_ID
            orid  = config.VIP_ROLE_ID      if tv == "vip_plus" else config.VIP_PLUS_ROLE_ID
            role  = i.guild.get_role(rid)   if rid  else None
            orole = i.guild.get_role(orid)  if orid else None
            try:
                if role:  await player.add_roles(role,  reason=f"VIP by {i.user}")
                if orole and orole in player.roles: await player.remove_roles(orole)
            except discord.Forbidden:
                pass
        await i.followup.send(f"{player.mention} is now **{vip_data.vip_tier_label(tv)}** until {exp.strftime('%d/%m/%Y %H:%M')} BRT.")

    @mm.command(name="addelo", description="Adds Matchmaking ELO to a player")
    async def mm_addelo(self, i: discord.Interaction, elo: int, user: discord.Member):
        if not isinstance(i.user, discord.Member) or not can_manage_mm(i.user):
            await i.response.send_message("Only Match Organizer can adjust ELO.", ephemeral=True)
            return
        if elo <= 0:
            await i.response.send_message("ELO must be > 0.", ephemeral=True)
            return
        await i.response.defer(ephemeral=True)
        async with database.conn() as c:
            season = await db_get_season(c)
            await adjust_elo_only(c, database.did(user.id), season["number"] if season else None, elo)
            row = await _q(c, "SELECT elo FROM mm_players WHERE discord_id=$1", database.did(user.id))
        asyncio.create_task(upsert_profile_from_member(user))
        await i.followup.send(f"Added `{elo}` ELO to {user.mention}. New ELO: `{row['elo']}`", ephemeral=True)

    @mm.command(name="removeelo", description="Removes Matchmaking ELO from a player")
    async def mm_removeelo(self, i: discord.Interaction, elo: int, user: discord.Member):
        if not isinstance(i.user, discord.Member) or not can_manage_mm(i.user):
            await i.response.send_message("Only Match Organizer can adjust ELO.", ephemeral=True)
            return
        if elo <= 0:
            await i.response.send_message("ELO must be > 0.", ephemeral=True)
            return
        await i.response.defer(ephemeral=True)
        async with database.conn() as c:
            season = await db_get_season(c)
            await adjust_elo_only(c, database.did(user.id), season["number"] if season else None, -elo)
            row = await _q(c, "SELECT elo FROM mm_players WHERE discord_id=$1", database.did(user.id))
        asyncio.create_task(upsert_profile_from_member(user))
        await i.followup.send(f"Removed `{elo}` ELO from {user.mention}. New ELO: `{row['elo']}`", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MMCog(bot))
