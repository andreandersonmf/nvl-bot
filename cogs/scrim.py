import discord
from discord import app_commands
from discord.ext import commands

import config

SCRIM_TYPE_CHOICES = [
    app_commands.Choice(name="Now", value="Now"),
    app_commands.Choice(name="Schedule", value="Schedule"),
]


class ScrimView(discord.ui.View):
    def __init__(self, user: discord.abc.User):
        super().__init__(timeout=None)
        self.user = user

    @discord.ui.button(label="Message", style=discord.ButtonStyle.secondary, emoji="✉️")
    async def message_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"Send a DM to {self.user.mention}",
            ephemeral=True
        )


@app_commands.command(name="scrim", description="Create a scrim request")
@app_commands.guilds(discord.Object(id=config.GUILD_ID))
@app_commands.choices(scrim_type=SCRIM_TYPE_CHOICES)
async def scrim_command(
    interaction: discord.Interaction,
    scrim_type: app_commands.Choice[str]
):
    if interaction.channel_id != config.SCRIM_CHANNEL_ID:
        await interaction.response.send_message(
            "You can only use this command in the scrim channel.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="Scrim request",
        color=discord.Color.blurple()
    )
    embed.description = (
        f"from {interaction.user.mention}\n"
        f"Request: **{scrim_type.value}**"
    )
    embed.set_footer(text="NVL Services")

    await interaction.response.defer()
    await interaction.followup.send(
        embed=embed,
        view=ScrimView(interaction.user)
    )


async def setup(bot: commands.Bot):
    bot.tree.add_command(scrim_command)


async def teardown(bot: commands.Bot):
    bot.tree.remove_command("scrim", guild=discord.Object(id=config.GUILD_ID))