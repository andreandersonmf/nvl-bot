from __future__ import annotations

"""
Schedules official matches with a 15-minute Discord DM reminder.

Loaded by default (see EXTENSIONS in bot.py).

Unified with the site: this cog used to keep its own local `schedules`
table, completely separate from the site's `matches` table (so a match
scheduled on Discord never showed up on the site, and vice versa). Now
that the bot shares the same Supabase database as the site, /schedule
match creates a row directly in the same `matches` table the site's
admin panel manages - so it shows up in both places automatically,
with no separate sync step needed.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import database

BRT = ZoneInfo("America/Sao_Paulo")


def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator


def parse_match_datetime(date_brt: str, time_brt: str) -> datetime | None:
    try:
        day, month, year = map(int, date_brt.split("/"))
        hour, minute = map(int, time_brt.split(":"))
        target = datetime(year, month, day, hour, minute, tzinfo=BRT)
    except ValueError:
        return None

    return target


async def get_team_by_role(role_id: int):
    return await database.fetchone(
        "SELECT * FROM teams WHERE discord_role_id = $1", database.did(role_id)
    )


class ScheduleCog(commands.Cog):
    schedule = app_commands.Group(
        name="schedule",
        description="Schedule commands",
        guild_ids=[config.GUILD_ID]
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.schedule_reminder_loop.start()

    def cog_unload(self):
        self.schedule_reminder_loop.cancel()

    @tasks.loop(minutes=1)
    async def schedule_reminder_loop(self):
        if not database.is_ready():
            return

        rows = await database.fetchall(
            """
            SELECT * FROM matches
            WHERE status = 'Scheduled' AND discord_reminder_sent = false
              AND match_date IS NOT NULL AND match_time IS NOT NULL
            """
        )

        for row in rows:
            match_dt = _combine_match_datetime(row["match_date"], row["match_time"])
            if match_dt is None:
                continue

            current_time = datetime.now(BRT)
            reminder_dt = match_dt - timedelta(minutes=15)

            if current_time >= match_dt:
                await database.execute(
                    "UPDATE matches SET discord_reminder_sent = true WHERE id = $1", row["id"]
                )
                continue

            if current_time >= reminder_dt:
                guild = self.bot.get_guild(config.GUILD_ID)
                if guild is None:
                    continue

                home_team = await database.fetchone(
                    "SELECT discord_role_id FROM teams WHERE country = $1", row["home_country"]
                )
                away_team = await database.fetchone(
                    "SELECT discord_role_id FROM teams WHERE country = $1", row["away_country"]
                )

                members = set()
                for team in (home_team, away_team):
                    if team and team["discord_role_id"]:
                        role = guild.get_role(int(team["discord_role_id"]))
                        if role:
                            members.update(m for m in role.members if not m.bot)

                for member in members:
                    try:
                        await member.send(
                            f"Reminder: **{row['home_country']} vs {row['away_country']}** starts in 15 minutes.\n"
                            f"Time: {match_dt.strftime('%d/%m/%Y %H:%M')} BRT\n"
                            f"Match ID: {row['id']}"
                        )
                    except discord.Forbidden:
                        pass

                await database.execute(
                    "UPDATE matches SET discord_reminder_sent = true WHERE id = $1", row["id"]
                )

    @schedule_reminder_loop.before_loop
    async def before_schedule_loop(self):
        await self.bot.wait_until_ready()

    @schedule.command(name="match", description="Schedules a match")
    @app_commands.describe(
        team1="First team",
        team2="Second team",
        date_brt="Match date in DD/MM/YYYY format",
        time_brt="Match time in HH:MM format"
    )
    async def schedule_match(
        self,
        interaction: discord.Interaction,
        team1: discord.Role,
        team2: discord.Role,
        date_brt: str,
        time_brt: str
    ):
        if not isinstance(interaction.user, discord.Member):
            return

        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "Only administration can use this command.",
                ephemeral=True
            )
            return

        if team1.id == team2.id:
            await interaction.response.send_message(
                "The two teams cannot be the same.",
                ephemeral=True
            )
            return

        target_dt = parse_match_datetime(date_brt, time_brt)
        if target_dt is None:
            await interaction.response.send_message(
                "Invalid format.\nUse the date as **DD/MM/YYYY** and the time as **HH:MM**.\nExample: `05/04/2026` and `16:00`",
                ephemeral=True
            )
            return

        current_time = datetime.now(BRT)
        if target_dt <= current_time:
            await interaction.response.send_message(
                "The provided date/time has already passed. Provide a future time.",
                ephemeral=True
            )
            return

        # Three sequential DB round-trips follow - ack immediately so a
        # slow moment never shows "The application did not respond".
        await interaction.response.defer()

        team1_row = await get_team_by_role(team1.id)
        team2_row = await get_team_by_role(team2.id)
        if not team1_row or not team2_row:
            await interaction.followup.send(
                "Both teams must be registered (see /team create or the site admin panel) before scheduling a match.",
                ephemeral=True
            )
            return

        match_row = await database.insert_returning("matches", {
            "season_id": team1_row["season_id"],
            "home_country": team1_row["country"],
            "away_country": team2_row["country"],
            "status": "Scheduled",
            "match_date": target_dt.date(),
            "match_time": target_dt.strftime("%H:%M"),
            "created_by_discord_id": database.did(interaction.user.id),
        })

        embed = discord.Embed(
            title="Match Scheduled",
            color=discord.Color.blurple()
        )
        embed.add_field(name="Match ID", value=str(match_row["id"]), inline=True)
        embed.add_field(name="Teams", value=f"{team1.mention} vs {team2.mention}", inline=False)
        embed.add_field(name="Date & Time (BRT)", value=target_dt.strftime("%d/%m/%Y %H:%M"), inline=False)
        embed.set_footer(text="15 minutes reminder enabled")

        await interaction.followup.send(embed=embed)

    @schedule.command(name="list", description="Lists scheduled matches")
    async def schedule_list(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member):
            return

        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "Only administration can use this command.",
                ephemeral=True
            )
            return

        rows = await database.fetchall(
            """
            SELECT * FROM matches
            WHERE status = 'Scheduled'
            ORDER BY match_date ASC, match_time ASC
            """
        )

        if not rows:
            await interaction.response.send_message("There are no scheduled matches.", ephemeral=True)
            return

        embed = discord.Embed(
            title="Scheduled Matches",
            color=discord.Color.blue()
        )

        lines = []
        for row in rows:
            dt = _combine_match_datetime(row["match_date"], row["match_time"])
            dt_text = dt.strftime("%d/%m/%Y %H:%M") if dt else "TBA"
            lines.append(
                f"**ID {row['id']}** — {row['home_country']} vs {row['away_country']} — {dt_text} BRT"
            )

        embed.description = "\n".join(lines[:20])
        await interaction.response.send_message(embed=embed)

    @schedule.command(name="remove", description="Removes a scheduled match")
    async def schedule_remove(self, interaction: discord.Interaction, match_id: int):
        if not isinstance(interaction.user, discord.Member):
            return

        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "Only administration can use this command.",
                ephemeral=True
            )
            return

        # Two sequential DB round-trips follow - ack immediately.
        await interaction.response.defer()

        row = await database.fetchone("SELECT * FROM matches WHERE id = $1", match_id)
        if not row:
            await interaction.followup.send("This Match ID does not exist.", ephemeral=True)
            return

        if row["status"] != "Scheduled":
            await interaction.followup.send(
                "Only matches still in `Scheduled` status can be removed with this command. "
                "Use the site admin panel to edit a Live/Finished match.",
                ephemeral=True
            )
            return

        await database.execute("DELETE FROM matches WHERE id = $1", match_id)

        await interaction.followup.send(
            f"Match **ID {match_id}** removed successfully.\n"
            f"{row['home_country']} vs {row['away_country']}"
        )


def _combine_match_datetime(match_date, match_time: str | None) -> datetime | None:
    if match_date is None or not match_time:
        return None
    try:
        hour, minute = map(int, match_time.split(":")[:2])
    except ValueError:
        return None
    return datetime(match_date.year, match_date.month, match_date.day, hour, minute, tzinfo=BRT)


async def setup(bot: commands.Bot):
    await bot.add_cog(ScheduleCog(bot))
