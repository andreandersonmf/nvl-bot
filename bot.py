import discord
from discord.ext import commands
from discord import Object

import config
import database

EXTENSIONS = [
    "cogs.team",
    "cogs.scrim",
    "cogs.matchmaking",
    "cogs.vip",
    "cogs.schedule",
    "cogs.match",
    "cogs.referee_rating",
]
# cogs.schedule (/schedule match|list|remove + 15-minute DM reminder loop)
# and cogs.match (/match result) write directly to the shared "matches"
# table used by the site's admin panel - enabled for launch.

class CVRSABot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.guilds = True
        intents.message_content = True

        super().__init__(
            command_prefix="!",
            intents=intents
        )

    async def setup_hook(self):
        await database.init_pool()
        print("[OK] Connected to Supabase Postgres")

        for ext in EXTENSIONS:
            try:
                await self.load_extension(ext)
                print(f"[OK] Loaded {ext}")
            except Exception as e:
                print(f"[ERROR] Failed to load {ext}: {e}")
                raise

        guild_obj = Object(id=config.GUILD_ID)
        synced = await self.tree.sync(guild=guild_obj)

        print(f"Synced {len(synced)} command(s) to guild {config.GUILD_ID}")
        for cmd in synced:
            print(f" - /{cmd.name}")

    async def close(self):
        await database.close_pool()
        await super().close()

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")

bot = CVRSABot()
bot.run(config.DISCORD_TOKEN)