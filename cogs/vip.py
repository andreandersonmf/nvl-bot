from __future__ import annotations

from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

import config
import database


class VipCog(commands.Cog):
    """Checks every 5 minutes whether any VIP/VIP+ subscription has
    expired and, if so, marks it as 'expired' in the database and
    removes the matching Discord role. The purchase itself (checkout +
    payment confirmation) happens entirely on the site - this cog only
    handles expiry."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_expired_vips.start()

    def cog_unload(self):
        self.check_expired_vips.cancel()

    @tasks.loop(minutes=5)
    async def check_expired_vips(self):
        if not database.is_ready():
            return

        expired = await database.fetchall(
            """
            UPDATE vip_subscriptions
            SET status = 'expired'
            WHERE status = 'active' AND expires_at < $1
            RETURNING discord_id, tier
            """,
            datetime.now(timezone.utc),
        )

        if not expired:
            return

        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            return

        vip_role_id = config.VIP_ROLE_ID
        vip_plus_role_id = config.VIP_PLUS_ROLE_ID

        for sub in expired:
            role_id = vip_plus_role_id if sub["tier"] == "vip_plus" else vip_role_id
            if not role_id:
                continue
            try:
                member = guild.get_member(int(sub["discord_id"])) or await guild.fetch_member(int(sub["discord_id"]))
            except (discord.NotFound, discord.HTTPException, ValueError):
                continue
            role = guild.get_role(int(role_id))
            if member and role:
                try:
                    await member.remove_roles(role, reason="VIP subscription expired")
                except discord.HTTPException:
                    pass

    @check_expired_vips.before_loop
    async def before_check_expired_vips(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(VipCog(bot))
