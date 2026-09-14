from __future__ import annotations

"""
Automatic reminder loop for official matches.

All match scheduling happens via the site's admin panel — this cog
has no slash commands. It runs a background task every minute that:

1. Sends a 30-minute DM reminder to every player on both rosters
   for any Scheduled match that is 30 minutes away and hasn't had
   a reminder sent yet.

The `discord_reminder_sent` and `discord_schedule_embed_sent` flags
on the `matches` table are used to prevent duplicate sends.

The schedule embed in #match-schedule is posted by the site's
/api/match-notify route the moment a match is created (status=Scheduled).
The bot does NOT post that embed — it only handles the DM reminder.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

import config
import database

BRT = ZoneInfo("America/Sao_Paulo")


def _combine_match_datetime(match_date, match_time: str | None) -> datetime | None:
    if match_date is None or not match_time:
        return None
    try:
        hour, minute = map(int, match_time.split(":")[:2])
    except ValueError:
        return None
    return datetime(
        match_date.year, match_date.month, match_date.day,
        hour, minute, tzinfo=BRT
    )


class ScheduleCog(commands.Cog):
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
            WHERE status = 'Scheduled'
              AND discord_reminder_sent = false
              AND match_date IS NOT NULL
              AND match_time IS NOT NULL
            """
        )

        for row in rows:
            match_dt = _combine_match_datetime(row["match_date"], row["match_time"])
            if match_dt is None:
                continue

            current_time = datetime.now(BRT)
            reminder_dt  = match_dt - timedelta(minutes=30)

            # If the match already started, just mark it so we never retry.
            if current_time >= match_dt:
                await database.execute(
                    "UPDATE matches SET discord_reminder_sent = true WHERE id = $1",
                    row["id"]
                )
                continue

            if current_time < reminder_dt:
                continue

            # Time to send reminders.
            guild = self.bot.get_guild(config.GUILD_ID)
            if guild is None:
                continue

            home_team = await database.fetchone(
                "SELECT discord_role_id FROM teams WHERE country = $1",
                row["home_country"]
            )
            away_team = await database.fetchone(
                "SELECT discord_role_id FROM teams WHERE country = $1",
                row["away_country"]
            )

            members: set[discord.Member] = set()
            for team in (home_team, away_team):
                if team and team["discord_role_id"]:
                    role = guild.get_role(int(team["discord_role_id"]))
                    if role:
                        members.update(m for m in role.members if not m.bot)

            star = " ⭐" if row.get("is_star_match") else ""
            for member in members:
                try:
                    await member.send(
                        f"⏰ **Match Reminder{star}**\n"
                        f"**{row['home_country']} vs {row['away_country']}** "
                        f"starts in **30 minutes**!\n"
                        f"📅 {match_dt.strftime('%d/%m/%Y')} • 🕐 {match_dt.strftime('%H:%M')} BRT"
                    )
                except discord.Forbidden:
                    pass

            await database.execute(
                "UPDATE matches SET discord_reminder_sent = true WHERE id = $1",
                row["id"]
            )

    @schedule_reminder_loop.before_loop
    async def before_schedule_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(ScheduleCog(bot))
