from __future__ import annotations

"""
Posts the official result of a match.

Loaded by default (see EXTENSIONS in bot.py).

Unified with the site: writes directly to the same `matches` table the
site's admin panel manages, instead of a separate local `match_results`
table with no connection to the site (the source project's behavior).
If a `Scheduled` or `Live` match between these two teams already
exists, it is updated to `Finished` with the result; otherwise a new
finished match row is created directly, matching the original
behavior of always working standalone even without a prior
/schedule match.
"""

import discord
from discord import app_commands
from discord.ext import commands

import config
import database


def is_allowed(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True

    role_ids = {role.id for role in member.roles}
    return (
        config.REFEREE_ROLE_ID in role_ids
        or config.STREAMER_ROLE_ID in role_ids
    )


def parse_set_score(set_score: str):
    try:
        left, right = set_score.split("-")
        return int(left.strip()), int(right.strip())
    except Exception:
        return None


def count_series(*sets_: str | None):
    wins_a = 0
    wins_b = 0

    for s in sets_:
        if not s:
            continue
        parsed = parse_set_score(s)
        if not parsed:
            continue
        a, b = parsed
        if a > b:
            wins_a += 1
        elif b > a:
            wins_b += 1

    return wins_a, wins_b


async def get_team_by_role(role_id: int):
    return await database.fetchone(
        "SELECT * FROM teams WHERE discord_role_id = $1", database.did(role_id)
    )


class MatchCog(commands.Cog):
    match = app_commands.Group(
        name="match",
        description="Match commands",
        guild_ids=[config.GUILD_ID]
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @match.command(name="result", description="Posts the result of a match")
    async def match_result(
        self,
        interaction: discord.Interaction,
        stage: str,
        set1: str,
        set2: str,
        winner_team: discord.Role,
        loser_team: discord.Role,
        wmvp: discord.Member,
        lmvp: discord.Member,
        referee: discord.Member,
        media: str,
        set3: str | None = None,
        set4: str | None = None,
        set5: str | None = None,
    ):
        if not isinstance(interaction.user, discord.Member):
            return

        if not is_allowed(interaction.user):
            await interaction.response.send_message(
                "Only referee, streamer or administration can use this command.",
                ephemeral=True
            )
            return

        winner_sets, loser_sets = count_series(set1, set2, set3, set4, set5)

        winner_row = await get_team_by_role(winner_team.id)
        loser_row = await get_team_by_role(loser_team.id)
        if not winner_row or not loser_row:
            await interaction.response.send_message(
                "Both teams must be registered (see /team create or the site admin panel) before posting a result.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        set_scores = [parse_set_score(s) if s else None for s in (set1, set2, set3, set4, set5)]

        # Try to update an existing scheduled/live match between these two
        # teams first (either order), otherwise create a finished one directly.
        existing = await database.fetchone(
            """
            SELECT * FROM matches
            WHERE status IN ('Scheduled', 'Live')
              AND (
                (home_country = $1 AND away_country = $2)
                OR (home_country = $2 AND away_country = $1)
              )
            ORDER BY created_at DESC
            LIMIT 1
            """,
            winner_row["country"], loser_row["country"],
        )

        values = {
            "home_country": winner_row["country"],
            "away_country": loser_row["country"],
            "stage": stage,
            "status": "Finished",
            "home_score": winner_sets,
            "away_score": loser_sets,
            "winner_country": winner_row["country"],
            "referee_discord_id": database.did(referee.id),
            "media_link": media,
            "set1_home": set_scores[0][0] if set_scores[0] else None,
            "set1_away": set_scores[0][1] if set_scores[0] else None,
            "set2_home": set_scores[1][0] if set_scores[1] else None,
            "set2_away": set_scores[1][1] if set_scores[1] else None,
            "set3_home": set_scores[2][0] if set_scores[2] else None,
            "set3_away": set_scores[2][1] if set_scores[2] else None,
            "set4_home": set_scores[3][0] if set_scores[3] else None,
            "set4_away": set_scores[3][1] if set_scores[3] else None,
            "set5_home": set_scores[4][0] if set_scores[4] else None,
            "set5_away": set_scores[4][1] if set_scores[4] else None,
        }

        if existing:
            await database.update_returning("matches", values, {"id": existing["id"]})
        else:
            values["season_id"] = winner_row["season_id"]
            values["created_by_discord_id"] = database.did(interaction.user.id)
            await database.insert_returning("matches", values)

        embed = discord.Embed(
            title="Match Result",
            description=f"**{winner_team.mention}** defeated **{loser_team.mention}**",
            color=discord.Color.gold()
        )

        embed.add_field(name="Stage", value=stage, inline=False)
        embed.add_field(name="Series", value=f"{winner_sets} - {loser_sets}", inline=False)

        sets_lines = [
            f"Set 1: {set1}",
            f"Set 2: {set2}",
        ]

        if set3:
            sets_lines.append(f"Set 3: {set3}")
        if set4:
            sets_lines.append(f"Set 4: {set4}")
        if set5:
            sets_lines.append(f"Set 5: {set5}")

        embed.add_field(name="Set Scores", value="\n".join(sets_lines), inline=False)

        embed.add_field(name="Winner MVP", value=wmvp.mention, inline=True)
        embed.add_field(name="Loser MVP", value=lmvp.mention, inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)

        embed.add_field(
            name="Match Staff",
            value=f"*Referee:* {referee.mention}\n*Media:* {media}",
            inline=False
        )

        embed.set_footer(text="CVR SA Services")

        await interaction.channel.send(embed=embed)
        await interaction.followup.send("Result posted successfully.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MatchCog(bot))
