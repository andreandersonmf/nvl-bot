from __future__ import annotations

"""
Sends an anonymous referee rating request via Discord DM
to both team captains when an official match is finished.

Flow:
  1. The site marks a match as "Finished" (status column).
  2. The schedule loop in cogs/schedule.py (or any future webhook) is
     not involved — instead the bot has its own poll loop here.
  3. When a new Finished match is detected (discord_rating_sent = false),
     the bot DMs both captains a rating embed with buttons 1–5.
  4. The captain clicks a number → a modal appears asking for a written
     opinion (optional).
  5. On submit, the bot POSTs the rating to the site's /api/referee-rating
     endpoint (authenticated via BOT_INTERNAL_SECRET env var).
  6. The match row is marked discord_rating_sent = true.
"""

import asyncio
import os
from datetime import datetime, timezone

import aiohttp
import discord
from discord.ext import commands, tasks
from discord import ui

import config
import database

SITE_URL          = config.NVL_SITE_URL
BOT_SECRET        = os.getenv("BOT_INTERNAL_SECRET", "")
RATING_API        = f"{SITE_URL}/api/referee-rating"


# ── Rating modal ──────────────────────────────────────────────────────────────

class RatingCommentModal(ui.Modal, title="Your opinion (optional)"):
    comment = ui.TextInput(
        label="Comment about the referee",
        style=discord.TextStyle.paragraph,
        placeholder="Write your opinion here (optional)…",
        required=False,
        max_length=500,
    )

    def __init__(
        self,
        rating: int,
        match_id: int,
        referee_discord_id: str,
        captain_discord_id: str,
    ):
        super().__init__()
        self.rating              = rating
        self.match_id            = match_id
        self.referee_discord_id  = referee_discord_id
        self.captain_discord_id  = captain_discord_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await _post_rating(
            match_id            = self.match_id,
            referee_discord_id  = self.referee_discord_id,
            captain_discord_id  = self.captain_discord_id,
            rating              = self.rating,
            comment             = self.comment.value or None,
        )
        await interaction.followup.send(
            f"✅ Thank you! Your rating (**{self.rating}/5**) was recorded.",
            ephemeral=True,
        )


# ── Rating view (buttons 1–5) ─────────────────────────────────────────────────

class RatingView(ui.View):
    def __init__(
        self,
        match_id: int,
        referee_discord_id: str,
        captain_discord_id: str,
    ):
        super().__init__(timeout=86400)  # 24 h
        self.match_id             = match_id
        self.referee_discord_id   = referee_discord_id
        self.captain_discord_id   = captain_discord_id
        self.voted                = False

        for score in range(1, 6):
            btn = ui.Button(
                label    = str(score),
                style    = discord.ButtonStyle.secondary,
                custom_id = f"rate_{match_id}_{captain_discord_id}_{score}",
                emoji    = "⭐" if score == 5 else None,
            )
            btn.callback = self._make_callback(score)
            self.add_item(btn)

    def _make_callback(self, score: int):
        async def callback(interaction: discord.Interaction):
            if self.voted:
                await interaction.response.send_message(
                    "You already rated this match.", ephemeral=True
                )
                return
            self.voted = True
            # Disable all buttons
            for child in self.children:
                child.disabled = True  # type: ignore
            await interaction.response.send_modal(
                RatingCommentModal(
                    rating             = score,
                    match_id           = self.match_id,
                    referee_discord_id = self.referee_discord_id,
                    captain_discord_id = self.captain_discord_id,
                )
            )
            try:
                await interaction.message.edit(view=self)
            except discord.HTTPException:
                pass
        return callback


# ── HTTP helper ────────────────────────────────────────────────────────────────

async def _post_rating(
    *,
    match_id: int,
    referee_discord_id: str,
    captain_discord_id: str,
    rating: int,
    comment: str | None,
) -> None:
    payload = {
        "matchId":            match_id,
        "refereeDiscordId":   referee_discord_id,
        "captainDiscordId":   captain_discord_id,
        "rating":             rating,
        "comment":            comment,
    }
    headers = {}
    if BOT_SECRET:
        headers["Authorization"] = f"Bearer {BOT_SECRET}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(RATING_API, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status not in (200, 201, 409):  # 409 = duplicate, fine
                    text = await resp.text()
                    print(f"[referee_rating] POST failed {resp.status}: {text[:200]}")
    except Exception as e:
        print(f"[referee_rating] POST error: {e}")


# ── Cog ────────────────────────────────────────────────────────────────────────

class RefereeRatingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.rating_loop.start()

    def cog_unload(self):
        self.rating_loop.cancel()

    @tasks.loop(minutes=2)
    async def rating_loop(self):
        """Poll for newly Finished matches that still need rating DMs."""
        if not database.is_ready():
            return

        rows = await database.fetchall(
            """
            SELECT * FROM matches
            WHERE status = 'Finished'
              AND discord_rating_sent = false
              AND referee_discord_id IS NOT NULL
            """
        )

        guild = self.bot.get_guild(config.GUILD_ID)
        if not guild:
            return

        for row in rows:
            await self._send_rating_dms(guild, row)
            await database.execute(
                "UPDATE matches SET discord_rating_sent = true WHERE id = $1",
                row["id"]
            )

    async def _send_rating_dms(self, guild: discord.Guild, row) -> None:
        """Send rating DMs to both team captains."""
        home_team = await database.fetchone(
            "SELECT captain_discord_id FROM teams WHERE country = $1",
            row["home_country"]
        )
        away_team = await database.fetchone(
            "SELECT captain_discord_id FROM teams WHERE country = $1",
            row["away_country"]
        )

        referee_discord_id = str(row["referee_discord_id"])
        match_id           = int(row["id"])
        star               = " ⭐" if row.get("is_star_match") else ""

        captain_ids = set()
        for team in (home_team, away_team):
            if team and team["captain_discord_id"]:
                captain_ids.add(str(team["captain_discord_id"]))

        for captain_did in captain_ids:
            member = guild.get_member(int(captain_did))
            if not member or member.bot:
                continue
            try:
                embed = discord.Embed(
                    title=f"⭐ Rate the Referee{star}",
                    description=(
                        f"How was the referee for **{row['home_country']} vs {row['away_country']}**?\n\n"
                        f"Click a number below (1 = poor · 5 = excellent). "
                        f"A comment box will appear after you choose."
                    ),
                    color=discord.Color.gold(),
                )
                embed.set_footer(text="NVL • Your rating is anonymous")
                view = RatingView(
                    match_id            = match_id,
                    referee_discord_id  = referee_discord_id,
                    captain_discord_id  = captain_did,
                )
                await member.send(embed=embed, view=view)
            except discord.Forbidden:
                pass
            except Exception as e:
                print(f"[referee_rating] DM error for {captain_did}: {e}")

    @rating_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(RefereeRatingCog(bot))
