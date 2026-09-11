from __future__ import annotations

from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config
import database
from services.profiles import discord_username_of, get_active_season_id, upsert_profile_from_member
from utils.roblox import get_profile_data_from_member


ROLE_CHOICES = [
    app_commands.Choice(name="Player", value="Player"),
    app_commands.Choice(name="Vice Captain", value="Vice Captain"),
    app_commands.Choice(name="Court Captain", value="Court Captain"),
]


STAFF_ROLE_CHOICES = [
    app_commands.Choice(name="Player", value="Player"),
    app_commands.Choice(name="Vice Captain", value="Vice Captain"),
    app_commands.Choice(name="Court Captain", value="Court Captain"),
]

# Roster roles that manage a team - Vice Captain and Court Captain have
# identical permissions everywhere in this cog.
MANAGER_ROSTER_ROLES = ("Vice Captain", "Court Captain")


def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator


def has_role(member: discord.Member, role_id: int) -> bool:
    return any(role.id == role_id for role in member.roles)


def can_manage_team(member: discord.Member) -> bool:
    return (
        has_role(member, config.CAPTAIN_ROLE_ID)
        or has_role(member, config.VICE_CAPTAIN_ROLE_ID)
        or has_role(member, config.COURT_CAPTAIN_ROLE_ID)
    )


def can_approve_transfer(member: discord.Member) -> bool:
    if is_admin(member):
        return True

    member_role_ids = {role.id for role in member.roles}
    return any(role_id in member_role_ids for role_id in config.STAFF_APPROVER_ROLE_IDS)


def in_transactions_channel(interaction: discord.Interaction) -> bool:
    return interaction.channel_id == config.TRANSACTIONS_CHANNEL_ID


def in_self_transactions_channel(interaction: discord.Interaction) -> bool:
    return interaction.channel_id == config.SELF_TRANSACTIONS_CHANNEL_ID


def extra_role_id_for(roster_role: str) -> int:
    """Discord role ID for a roster role, in addition to the team role."""
    if roster_role == "Vice Captain":
        return config.VICE_CAPTAIN_ROLE_ID
    if roster_role == "Court Captain":
        return config.COURT_CAPTAIN_ROLE_ID
    return config.PLAYER_ROLE_ID


def team_code_from_name(team_name: str) -> str:
    cleaned = "".join(char for char in str(team_name).upper() if char.isalnum())
    return cleaned[:8] or "TEAM"


# ============================================================
# Data access - single shared schema (same tables the site uses)
# ============================================================

async def get_team_by_id(team_id) -> database.asyncpg.Record | None:  # type: ignore[attr-defined]
    return await database.fetchone("SELECT * FROM teams WHERE id = $1", team_id)


async def get_team_by_role(role_id: int):
    return await database.fetchone(
        "SELECT * FROM teams WHERE discord_role_id = $1", database.did(role_id)
    )


async def get_team_by_name(team_name: str):
    return await database.fetchone("SELECT * FROM teams WHERE country = $1", team_name)


async def get_team_by_captain(discord_id: int):
    return await database.fetchone(
        "SELECT * FROM teams WHERE captain_discord_id = $1", database.did(discord_id)
    )


async def get_management_team(member: discord.Member):
    """The team this member can manage: as Captain, Vice Captain or Court Captain."""
    team = await get_team_by_captain(member.id)
    if team:
        return team

    return await database.fetchone(
        """
        SELECT t.* FROM teams t
        JOIN team_players tp ON tp.team_id = t.id
        WHERE tp.discord_id = $1 AND tp.role = ANY($2::text[])
        LIMIT 1
        """,
        database.did(member.id),
        list(MANAGER_ROSTER_ROLES),
    )


async def get_player_current_team(discord_id: int):
    team = await database.fetchone(
        """
        SELECT t.* FROM teams t
        JOIN team_players tp ON tp.team_id = t.id
        WHERE tp.discord_id = $1
        LIMIT 1
        """,
        database.did(discord_id),
    )
    if team:
        return team
    return await get_team_by_captain(discord_id)


async def get_roster(team_id) -> list:
    return await database.fetchall(
        "SELECT * FROM team_players WHERE team_id = $1 ORDER BY role DESC, discord_id ASC",
        team_id,
    )


async def record_team_transaction(**values) -> database.asyncpg.Record:  # type: ignore[attr-defined]
    values.setdefault("source", "discord")
    return await database.insert_returning("team_transactions", values)


# ============================================================
# Embeds
# ============================================================

def build_captain_changed_embed(
    requester: discord.Member,
    team_name: str,
    old_captain: discord.Member | None,
    new_captain: discord.Member
):
    embed = discord.Embed(
        title="Captain Changed",
        description=(
            f"*manual action by {requester.mention}*\n"
            f"Team **{team_name}** has a new captain.\n\n"
            f"Old Captain: {old_captain.mention if old_captain else 'Not found'}\n"
            f"New Captain: {new_captain.mention}"
        ),
        color=discord.Color.gold()
    )
    embed.set_footer(text="NVL Team System")
    return embed


def build_staff_add_embed(
    requester: discord.Member,
    player: discord.Member,
    team_name: str,
    role_text: str
):
    embed = discord.Embed(
        title="Roster Updated",
        description=(
            f"*manual action by {requester.mention}*\n"
            f"{player.mention} was added to **{team_name}** as **{role_text}**"
        ),
        color=discord.Color.green()
    )
    embed.set_footer(text="NVL Team System")
    return embed


def build_staff_remove_embed(
    requester: discord.Member,
    player: discord.Member,
    team_name: str
):
    embed = discord.Embed(
        title="Roster Updated",
        description=(
            f"*manual action by {requester.mention}*\n"
            f"{player.mention} was removed from **{team_name}**"
        ),
        color=discord.Color.red()
    )
    embed.set_footer(text="NVL Team System")
    return embed


def profile_only_view(profile_url: str):
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Profile", style=discord.ButtonStyle.link, url=profile_url))
    return view


def build_release_embed(requester: discord.Member, player: discord.Member, team_name: str):
    embed = discord.Embed(
        title="Player Released",
        description=(
            f"*submitted by {requester.mention}*\n"
            f"{player.mention} has been released from **{team_name}**"
        ),
        color=discord.Color.dark_gray()
    )
    embed.set_footer(text="NVL Services")
    return embed


def build_pending_transfer_embed(requester: discord.Member, player: discord.Member, team_name: str, requested_role: str, avatar_url: str | None):
    embed = discord.Embed(
        description=(
            f"Submitted by {requester.mention}\n\n"
            f"Transact {player.mention} to **{team_name}** as **{requested_role}**"
        ),
        color=discord.Color.blurple()
    )
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    embed.set_footer(text="NVL Services")
    return embed


def build_success_transfer_embed(requester: discord.Member, player: discord.Member, team_name: str, approver: discord.Member, avatar_url: str | None):
    embed = discord.Embed(
        title="Successful Transfer",
        description=(
            f"*requested by {requester.mention}*\n"
            f"{player.mention} was successfully transferred to **{team_name}**\n\n"
            f"Approved by {approver.mention}"
        ),
        color=discord.Color.green()
    )
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    embed.set_footer(text="NVL Services")
    return embed


def build_denied_transfer_embed(requester: discord.Member, player: discord.Member, team_name: str, approver: discord.Member, reason: str, avatar_url: str | None):
    embed = discord.Embed(
        title="Unsuccessful Transfer",
        description=(
            f"*requested by {requester.mention}*\n"
            f"{player.mention}'s transaction to **{team_name}** was denied by {approver.mention}.\n\n"
            f"**Reason:**\n{reason}"
        ),
        color=discord.Color.red()
    )
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    embed.set_footer(text="NVL Services")
    return embed


def build_cleared_transfer_embed(
    requester: discord.Member | None,
    player: discord.Member,
    team_name: str,
    cleared_by: discord.Member,
    avatar_url: str | None
):
    embed = discord.Embed(
        title="Transfer Cleared",
        description=(
            f"*manual action by {cleared_by.mention}*\n"
            f"The pending transaction for {player.mention} to **{team_name}** was cleared manually."
            + (f"\n\nOriginally requested by {requester.mention}" if requester else "")
        ),
        color=discord.Color.orange()
    )
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    embed.set_footer(text="NVL Services")
    return embed


def build_team_deleted_embed(requester: discord.Member, team_name: str, captain: discord.Member | None):
    embed = discord.Embed(
        title="Team Deleted",
        description=(
            f"*submitted by {requester.mention}*\n"
            f"**{team_name}** has been deleted from the system.\n\n"
            f"Captain removed: {captain.mention if captain else 'Not found'}"
        ),
        color=discord.Color.red()
    )
    embed.set_footer(text="NVL Team System")
    return embed


# ============================================================
# Transfer approval UI
# ============================================================

class DenyReasonModal(discord.ui.Modal, title="Deny Transfer"):
    reason = discord.ui.TextInput(
        label="Reason",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500,
        placeholder="Type the reason for denying this transfer..."
    )

    def __init__(self, bot: commands.Bot, transaction_id: str, original_message: discord.Message):
        super().__init__()
        self.bot = bot
        self.transaction_id = transaction_id
        self.original_message = original_message

    async def on_submit(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member):
            return

        if not can_approve_transfer(interaction.user):
            await interaction.response.send_message("You cannot deny this transfer.", ephemeral=True)
            return

        # Two sequential DB round-trips follow before anything can be
        # confirmed - ack immediately.
        await interaction.response.defer(ephemeral=True)

        transfer = await database.fetchone("SELECT * FROM team_transactions WHERE id = $1", self.transaction_id)
        if not transfer:
            await interaction.followup.send("Transfer not found.", ephemeral=True)
            return

        if transfer["status"] != "pending":
            await interaction.followup.send("This transfer has already been completed.", ephemeral=True)
            return

        team = await get_team_by_id(transfer["team_id"])
        guild = interaction.guild
        if guild is None or team is None:
            await interaction.followup.send("Could not locate the related data.", ephemeral=True)
            return

        requester = guild.get_member(int(transfer["requester_discord_id"])) if transfer["requester_discord_id"] else None
        player = guild.get_member(int(transfer["player_discord_id"])) if transfer["player_discord_id"] else None
        if requester is None or player is None:
            await interaction.followup.send("Could not find the requester/player in the server.", ephemeral=True)
            return

        await database.update_returning(
            "team_transactions",
            {
                "status": "denied",
                "reason": str(self.reason),
                "handled_by_discord_id": database.did(interaction.user.id),
                "handled_by_discord_username": discord_username_of(interaction.user),
                "handled_at": datetime.now(timezone.utc),
            },
            {"id": self.transaction_id},
        )

        profile_data = await get_profile_data_from_member(player)

        embed = build_denied_transfer_embed(
            requester=requester,
            player=player,
            team_name=team["country"],
            approver=interaction.user,
            reason=str(self.reason),
            avatar_url=profile_data["avatar_url"]
        )

        await self.original_message.edit(
            embed=embed,
            view=profile_only_view(profile_data["profile_url"])
        )

        try:
            await requester.send(
                f"Your request to add **{player.display_name}** to team **{team['country']}** was denied.\nReason: {self.reason}"
            )
        except discord.Forbidden:
            pass

        await interaction.followup.send("Transfer denied successfully.", ephemeral=True)


class TransferRequestView(discord.ui.View):
    def __init__(self, bot: commands.Bot, transaction_id: str, profile_url: str):
        super().__init__(timeout=None)
        self.bot = bot
        self.transaction_id = transaction_id

        self.add_item(discord.ui.Button(label="Profile", style=discord.ButtonStyle.link, url=profile_url))

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green)
    async def accept_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member):
            return

        if not can_approve_transfer(interaction.user):
            await interaction.response.send_message("Only Staff/Admin can accept this transaction.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        transfer = await database.fetchone("SELECT * FROM team_transactions WHERE id = $1", self.transaction_id)
        if not transfer:
            await interaction.followup.send("Transfer not found.", ephemeral=True)
            return

        if transfer["status"] != "pending":
            await interaction.followup.send("This transfer has already been completed.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Guild not found.", ephemeral=True)
            return

        team = await get_team_by_id(transfer["team_id"])
        if team is None:
            await interaction.followup.send("Team not found.", ephemeral=True)
            return

        requester = guild.get_member(int(transfer["requester_discord_id"])) if transfer["requester_discord_id"] else None
        player = guild.get_member(int(transfer["player_discord_id"])) if transfer["player_discord_id"] else None

        if requester is None or player is None:
            await interaction.followup.send("Could not find the requester/player in the server.", ephemeral=True)
            return

        existing_team = await get_player_current_team(player.id)
        if existing_team:
            await interaction.followup.send("This player is already registered on a team.", ephemeral=True)
            return

        profile_data = await get_profile_data_from_member(player)
        roster_role = transfer["requested_role"] or "Player"
        player_profile = await upsert_profile_from_member(
            player,
            roblox_username=profile_data.get("username"),
            roblox_user_id=profile_data.get("user_id"),
        )

        await database.insert_returning("team_players", {
            "team_id": team["id"],
            "season_id": team["season_id"],
            "profile_id": player_profile["id"] if player_profile else None,
            "roblox_username": profile_data.get("username") or discord_username_of(player),
            "roblox_user_id": database.did(profile_data.get("user_id") or player.id),
            "discord_username": discord_username_of(player),
            "discord_id": database.did(player.id),
            "role": roster_role,
        })

        await database.update_returning(
            "team_transactions",
            {
                "status": "accepted",
                "handled_by_discord_id": database.did(interaction.user.id),
                "handled_by_discord_username": discord_username_of(interaction.user),
                "handled_at": datetime.now(timezone.utc),
            },
            {"id": self.transaction_id},
        )

        team_role = guild.get_role(int(team["discord_role_id"])) if team["discord_role_id"] else None
        extra_role = guild.get_role(extra_role_id_for(roster_role)) if extra_role_id_for(roster_role) else None

        roles_to_add = [role for role in (team_role, extra_role) if role]
        if roles_to_add:
            await player.add_roles(*roles_to_add, reason=f"Transfer accepted by {interaction.user}")

        embed = build_success_transfer_embed(
            requester=requester,
            player=player,
            team_name=team["country"],
            approver=interaction.user,
            avatar_url=profile_data["avatar_url"]
        )

        await interaction.message.edit(
            embed=embed,
            view=profile_only_view(profile_data["profile_url"])
        )

        await interaction.followup.send("Transfer accepted successfully.", ephemeral=True)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.red)
    async def deny_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member):
            return

        if not can_approve_transfer(interaction.user):
            await interaction.response.send_message("Only Staff/Admin can deny this transaction.", ephemeral=True)
            return

        modal = DenyReasonModal(self.bot, self.transaction_id, interaction.message)
        await interaction.response.send_modal(modal)


class TeamCog(commands.Cog):
    team = app_commands.Group(
        name="team",
        description="Team commands",
        guild_ids=[config.GUILD_ID]
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @team.command(name="create", description="Register a team in the database")
    async def team_create(self, interaction: discord.Interaction, captain: discord.Member, role_team: discord.Role):
        if not isinstance(interaction.user, discord.Member):
            return

        if not is_admin(interaction.user):
            await interaction.response.send_message("Only administration can use this command.", ephemeral=True)
            return

        existing_team = await get_team_by_role(role_team.id)
        if existing_team:
            await interaction.response.send_message("This team role is already registered.", ephemeral=True)
            return

        existing_captain = await get_player_current_team(captain.id)
        if existing_captain:
            await interaction.response.send_message("This captain is already registered on a team.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)

        profile_data = await get_profile_data_from_member(captain)
        captain_profile = await upsert_profile_from_member(
            captain,
            roblox_username=profile_data.get("username"),
            roblox_user_id=profile_data.get("user_id"),
        )
        season_id = await get_active_season_id()
        team_name = role_team.name

        team_row = await database.insert_returning("teams", {
            "season_id": season_id,
            "country": team_name,
            "code": team_code_from_name(team_name),
            "captain_name": profile_data.get("username") or captain.display_name,
            "captain_discord": discord_username_of(captain),
            "captain_discord_id": database.did(captain.id),
            "captain_roblox_id": database.did(profile_data.get("user_id") or captain.id),
            "discord_role_id": database.did(role_team.id),
            "approved": True,
            "approved_at": datetime.now(timezone.utc),
        })

        roles_to_add = [role_team]
        captain_role = interaction.guild.get_role(config.CAPTAIN_ROLE_ID)
        if captain_role:
            roles_to_add.append(captain_role)

        await captain.add_roles(*roles_to_add, reason="Registered as team captain")

        await interaction.followup.send(
            f"Team **{role_team.name}** created successfully.\nCaptain: {captain.mention}\nTeam role: {role_team.mention}",
        )

    @team.command(name="delete", description="Remove a team and its captain from the database")
    async def team_delete(self, interaction: discord.Interaction, team: discord.Role):
        if not isinstance(interaction.user, discord.Member):
            return

        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "Only administration can use this command.",
                ephemeral=True
            )
            return

        team_row = await get_team_by_role(team.id)
        if not team_row:
            await interaction.response.send_message(
                "This team is not registered in the database.",
                ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Guild not found.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)

        captain = guild.get_member(int(team_row["captain_discord_id"])) if team_row["captain_discord_id"] else None
        roster_rows = await get_roster(team_row["id"])

        team_role = guild.get_role(int(team_row["discord_role_id"])) if team_row["discord_role_id"] else None
        captain_role = guild.get_role(config.CAPTAIN_ROLE_ID)

        for row in roster_rows:
            member = guild.get_member(int(row["discord_id"])) if row["discord_id"] else None
            if member is None:
                continue

            extra_role = guild.get_role(extra_role_id_for(row["role"]))
            roles_to_remove = [role for role in (team_role, extra_role) if role]

            if roles_to_remove:
                try:
                    await member.remove_roles(
                        *roles_to_remove,
                        reason=f"Team {team_row['country']} deleted by {interaction.user}"
                    )
                except discord.Forbidden:
                    pass

        if captain is not None:
            captain_roles_to_remove = [role for role in (team_role, captain_role) if role]
            if captain_roles_to_remove:
                try:
                    await captain.remove_roles(
                        *captain_roles_to_remove,
                        reason=f"Team {team_row['country']} deleted by {interaction.user}"
                    )
                except discord.Forbidden:
                    pass

        await database.execute("DELETE FROM team_players WHERE team_id = $1", team_row["id"])
        await database.execute("DELETE FROM team_transactions WHERE team_id = $1", team_row["id"])
        await database.execute("DELETE FROM teams WHERE id = $1", team_row["id"])

        embed = build_team_deleted_embed(
            requester=interaction.user,
            team_name=team_row["country"],
            captain=captain
        )
        await interaction.followup.send(embed=embed)

    @team.command(name="info", description="Show the full information for a team")
    async def team_info(self, interaction: discord.Interaction, team: discord.Role):
        # Two sequential DB round-trips follow - ack immediately so a
        # slow moment never shows "The application did not respond".
        await interaction.response.defer()

        team_row = await get_team_by_role(team.id)
        if not team_row:
            await interaction.followup.send("This team is not registered in the database.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Guild not found.", ephemeral=True)
            return

        captain = guild.get_member(int(team_row["captain_discord_id"])) if team_row["captain_discord_id"] else None
        roster = await get_roster(team_row["id"])

        vice_list = []
        court_captain_list = []
        player_list = []

        for row in roster:
            member = guild.get_member(int(row["discord_id"])) if row["discord_id"] else None
            if member is None:
                continue

            if row["role"] == "Vice Captain":
                vice_list.append(member.mention)
            elif row["role"] == "Court Captain":
                court_captain_list.append(member.mention)
            else:
                player_list.append(member.mention)

        embed = discord.Embed(
            title=f"{team_row['country']} - Team Info",
            color=discord.Color.blue()
        )
        embed.add_field(
            name="Captain",
            value=captain.mention if captain else "Not found",
            inline=False
        )
        embed.add_field(
            name="Vice Captains",
            value="\n".join(vice_list) if vice_list else "None",
            inline=False
        )
        embed.add_field(
            name="Court Captains",
            value="\n".join(court_captain_list) if court_captain_list else "None",
            inline=False
        )
        embed.add_field(
            name="Roster",
            value="\n".join(player_list) if player_list else "None",
            inline=False
        )
        embed.set_footer(text="NVL Team System")

        await interaction.followup.send(embed=embed)

    @team.command(name="add", description="Request that a player be added to the team")
    @app_commands.choices(role=ROLE_CHOICES)
    async def team_add(self, interaction: discord.Interaction, player: discord.Member, role: app_commands.Choice[str]):
        if not isinstance(interaction.user, discord.Member):
            return

        if not in_transactions_channel(interaction):
            await interaction.response.send_message("This command can only be used in the transactions channel.", ephemeral=True)
            return

        if not can_manage_team(interaction.user):
            await interaction.response.send_message("Only captains, vice captains and court captains can use this command.", ephemeral=True)
            return

        team = await get_management_team(interaction.user)
        if not team:
            await interaction.response.send_message("You are not registered as captain/vice captain/court captain of any team.", ephemeral=True)
            return

        if player.bot:
            await interaction.response.send_message("You cannot add bots.", ephemeral=True)
            return

        existing_team = await get_player_current_team(player.id)
        if existing_team:
            await interaction.response.send_message("This player is already registered on a team.", ephemeral=True)
            return

        pending = await database.fetchone(
            "SELECT * FROM team_transactions WHERE player_discord_id = $1 AND status = 'pending'",
            database.did(player.id),
        )
        if pending:
            await interaction.response.send_message("This player already has a pending transfer.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        profile_data = await get_profile_data_from_member(player)
        await upsert_profile_from_member(interaction.user)
        await upsert_profile_from_member(
            player,
            roblox_username=profile_data.get("username"),
            roblox_user_id=profile_data.get("user_id"),
        )

        transfer = await record_team_transaction(
            season_id=team["season_id"],
            team_id=team["id"],
            team_name=team["country"],
            team_discord_role_id=team["discord_role_id"],
            transaction_type="add_player",
            requested_role=role.value,
            status="pending",
            requester_discord_id=database.did(interaction.user.id),
            requester_discord_username=discord_username_of(interaction.user),
            player_discord_id=database.did(player.id),
            player_discord_username=discord_username_of(player),
            roblox_username=profile_data.get("username"),
            roblox_user_id=database.did(profile_data.get("user_id")) if profile_data.get("user_id") else None,
            discord_channel_id=database.did(interaction.channel_id),
        )

        embed = build_pending_transfer_embed(
            requester=interaction.user,
            player=player,
            team_name=team["country"],
            requested_role=role.value,
            avatar_url=profile_data["avatar_url"]
        )

        view = TransferRequestView(self.bot, transfer["id"], profile_data["profile_url"])

        sent_message = await interaction.channel.send(embed=embed, view=view)

        await database.execute(
            "UPDATE team_transactions SET discord_message_id = $1 WHERE id = $2",
            database.did(sent_message.id), transfer["id"],
        )

        await interaction.followup.send("Transfer request sent successfully.", ephemeral=True)

    @team.command(name="remove", description="Remove a player from the team")
    async def team_remove(self, interaction: discord.Interaction, player: discord.Member):
        if not isinstance(interaction.user, discord.Member):
            return

        if not in_transactions_channel(interaction):
            await interaction.response.send_message("This command can only be used in the transactions channel.", ephemeral=True)
            return

        if not can_manage_team(interaction.user):
            await interaction.response.send_message("Only captains, vice captains and court captains can use this command.", ephemeral=True)
            return

        team = await get_management_team(interaction.user)
        if not team:
            await interaction.response.send_message("You are not registered as captain/vice captain/court captain of any team.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        if database.did(player.id) == team["captain_discord_id"]:
            await interaction.followup.send("You cannot remove the team captain with this command.", ephemeral=True)
            return

        roster_row = await database.fetchone(
            "SELECT * FROM team_players WHERE team_id = $1 AND discord_id = $2",
            team["id"], database.did(player.id),
        )

        if not roster_row:
            await interaction.followup.send("This player is not on your team.", ephemeral=True)
            return

        profile_data = await get_profile_data_from_member(player)

        await database.execute(
            "DELETE FROM team_players WHERE team_id = $1 AND discord_id = $2",
            team["id"], database.did(player.id),
        )
        await record_team_transaction(
            season_id=team["season_id"],
            team_id=team["id"],
            team_name=team["country"],
            team_discord_role_id=team["discord_role_id"],
            transaction_type="remove_player",
            requested_role=roster_row["role"],
            status="accepted",
            requester_discord_id=database.did(interaction.user.id),
            requester_discord_username=discord_username_of(interaction.user),
            handled_by_discord_id=database.did(interaction.user.id),
            handled_by_discord_username=discord_username_of(interaction.user),
            handled_at=datetime.now(timezone.utc),
            player_discord_id=database.did(player.id),
            player_discord_username=discord_username_of(player),
            roblox_username=profile_data.get("username"),
            roblox_user_id=database.did(profile_data.get("user_id")) if profile_data.get("user_id") else None,
        )

        guild = interaction.guild
        if guild is not None:
            team_role = guild.get_role(int(team["discord_role_id"])) if team["discord_role_id"] else None
            extra_role = guild.get_role(extra_role_id_for(roster_row["role"]))
            roles_to_remove = [role for role in (team_role, extra_role) if role]
            if roles_to_remove:
                await player.remove_roles(*roles_to_remove, reason=f"Released by {interaction.user}")

        embed = build_release_embed(interaction.user, player, team["country"])
        await interaction.channel.send(embed=embed)
        await interaction.followup.send("Player removed successfully.", ephemeral=True)

    @team.command(name="leave", description="Leave your own team")
    async def team_leave(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member):
            return

        if not in_self_transactions_channel(interaction):
            await interaction.response.send_message(
                "This command can only be used in the self transactions channel.",
                ephemeral=True
            )
            return

        captain_team = await get_team_by_captain(interaction.user.id)
        if captain_team:
            await interaction.response.send_message(
                "Captains cannot use this command.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        roster_row = await database.fetchone(
            """
            SELECT tp.*, t.country, t.discord_role_id
            FROM team_players tp
            JOIN teams t ON t.id = tp.team_id
            WHERE tp.discord_id = $1
            """,
            database.did(interaction.user.id),
        )

        if not roster_row:
            await interaction.followup.send(
                "You are not registered on any team.",
                ephemeral=True
            )
            return

        profile_data = await get_profile_data_from_member(interaction.user)

        await database.execute(
            "DELETE FROM team_players WHERE team_id = $1 AND discord_id = $2",
            roster_row["team_id"], database.did(interaction.user.id),
        )
        await record_team_transaction(
            season_id=roster_row["season_id"],
            team_id=roster_row["team_id"],
            team_name=roster_row["country"],
            team_discord_role_id=roster_row["discord_role_id"],
            transaction_type="leave_team",
            requested_role=roster_row["role"],
            status="accepted",
            requester_discord_id=database.did(interaction.user.id),
            requester_discord_username=discord_username_of(interaction.user),
            handled_by_discord_id=database.did(interaction.user.id),
            handled_by_discord_username=discord_username_of(interaction.user),
            handled_at=datetime.now(timezone.utc),
            player_discord_id=database.did(interaction.user.id),
            player_discord_username=discord_username_of(interaction.user),
            roblox_username=profile_data.get("username"),
            roblox_user_id=database.did(profile_data.get("user_id")) if profile_data.get("user_id") else None,
        )

        guild = interaction.guild
        if guild is not None:
            team_role = guild.get_role(int(roster_row["discord_role_id"])) if roster_row["discord_role_id"] else None
            extra_role = guild.get_role(extra_role_id_for(roster_row["role"]))
            roles_to_remove = [role for role in (team_role, extra_role) if role]
            if roles_to_remove:
                await interaction.user.remove_roles(
                    *roles_to_remove,
                    reason="Player left their own team"
                )

        embed = build_release_embed(
            requester=interaction.user,
            player=interaction.user,
            team_name=roster_row["country"]
        )

        await interaction.channel.send(embed=embed)
        await interaction.followup.send("You left your team successfully.", ephemeral=True)

    @team.command(name="clear", description="Clear a player's pending transfer")
    async def team_clear(self, interaction: discord.Interaction, player: discord.Member):
        if not isinstance(interaction.user, discord.Member):
            return

        if not can_approve_transfer(interaction.user):
            await interaction.response.send_message(
                "Only Staff/Admin can use this command.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        transfer = await database.fetchone(
            """
            SELECT * FROM team_transactions
            WHERE player_discord_id = $1 AND status = 'pending'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            database.did(player.id),
        )

        if not transfer:
            await interaction.followup.send(
                "This player does not have a pending transfer.",
                ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Guild not found.", ephemeral=True)
            return

        team = await get_team_by_id(transfer["team_id"])
        requester = guild.get_member(int(transfer["requester_discord_id"])) if transfer["requester_discord_id"] else None

        profile_data = await get_profile_data_from_member(player)

        old_message = None
        if transfer["discord_channel_id"] and transfer["discord_message_id"]:
            channel = guild.get_channel(int(transfer["discord_channel_id"]))
            if isinstance(channel, discord.TextChannel):
                try:
                    old_message = await channel.fetch_message(int(transfer["discord_message_id"]))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    old_message = None

        await database.execute("DELETE FROM team_transactions WHERE id = $1", transfer["id"])

        if old_message and team is not None:
            cleared_embed = build_cleared_transfer_embed(
                requester=requester,
                player=player,
                team_name=team["country"],
                cleared_by=interaction.user,
                avatar_url=profile_data["avatar_url"]
            )
            try:
                await old_message.edit(
                    embed=cleared_embed,
                    view=profile_only_view(profile_data["profile_url"])
                )
            except discord.HTTPException:
                pass

        await interaction.followup.send(
            f"Pending transfer for {player.mention} was cleared successfully.",
            ephemeral=True
        )

    # /team captainchange was intentionally removed.
    # Captain changes are handled through the site panel via /api/team-sync,
    # to keep site, Supabase, Discord roles and /team info aligned.

    @team.command(name="staffadd", description="Manually add a player to any roster")
    @app_commands.choices(role=STAFF_ROLE_CHOICES)
    async def team_staffadd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        role: app_commands.Choice[str],
        team: discord.Role
    ):
        if not isinstance(interaction.user, discord.Member):
            return

        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "Only administration can use this command.",
                ephemeral=True
            )
            return

        team_row = await get_team_by_role(team.id)
        if not team_row:
            await interaction.response.send_message(
                "This team is not registered in the database.",
                ephemeral=True
            )
            return

        if user.bot:
            await interaction.response.send_message("You cannot add bots.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Guild not found.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        existing_team = await get_player_current_team(user.id)
        if existing_team:
            await interaction.followup.send(
                "This user is already registered on a team.",
                ephemeral=True
            )
            return

        profile_data = await get_profile_data_from_member(user)
        player_profile = await upsert_profile_from_member(
            user,
            roblox_username=profile_data.get("username"),
            roblox_user_id=profile_data.get("user_id"),
        )

        await database.insert_returning("team_players", {
            "team_id": team_row["id"],
            "season_id": team_row["season_id"],
            "profile_id": player_profile["id"] if player_profile else None,
            "roblox_username": profile_data.get("username") or discord_username_of(user),
            "roblox_user_id": database.did(profile_data.get("user_id") or user.id),
            "discord_username": discord_username_of(user),
            "discord_id": database.did(user.id),
            "role": role.value,
        })
        await record_team_transaction(
            season_id=team_row["season_id"],
            team_id=team_row["id"],
            team_name=team_row["country"],
            team_discord_role_id=team_row["discord_role_id"],
            transaction_type="add_player",
            requested_role=role.value,
            status="accepted",
            requester_discord_id=database.did(interaction.user.id),
            requester_discord_username=discord_username_of(interaction.user),
            handled_by_discord_id=database.did(interaction.user.id),
            handled_by_discord_username=discord_username_of(interaction.user),
            handled_at=datetime.now(timezone.utc),
            player_discord_id=database.did(user.id),
            player_discord_username=discord_username_of(user),
            roblox_username=profile_data.get("username"),
            roblox_user_id=database.did(profile_data.get("user_id")) if profile_data.get("user_id") else None,
        )

        team_role = guild.get_role(int(team_row["discord_role_id"])) if team_row["discord_role_id"] else None
        extra_role = guild.get_role(extra_role_id_for(role.value))

        roles_to_add = [r for r in (team_role, extra_role) if r and r not in user.roles]
        if roles_to_add:
            await user.add_roles(*roles_to_add, reason=f"Manual roster add by {interaction.user}")

        embed = build_staff_add_embed(
            requester=interaction.user,
            player=user,
            team_name=team_row["country"],
            role_text=role.value
        )
        await interaction.followup.send(embed=embed, ephemeral=False)

    @team.command(name="staffremove", description="Manually remove a player from any roster")
    async def team_staffremove(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        team: discord.Role
    ):
        if not isinstance(interaction.user, discord.Member):
            return

        if not is_admin(interaction.user):
            await interaction.response.send_message(
                "Only administration can use this command.",
                ephemeral=True
            )
            return

        team_row = await get_team_by_role(team.id)
        if not team_row:
            await interaction.response.send_message(
                "This team is not registered in the database.",
                ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Guild not found.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        if database.did(user.id) == team_row["captain_discord_id"]:
            await interaction.followup.send(
                "Change the captain through the site panel. This command does not remove the captain.",
                ephemeral=True
            )
            return

        roster_row = await database.fetchone(
            "SELECT * FROM team_players WHERE team_id = $1 AND discord_id = $2",
            team_row["id"], database.did(user.id),
        )

        if not roster_row:
            await interaction.followup.send(
                "This user is not on that team's roster.",
                ephemeral=True
            )
            return

        profile_data = await get_profile_data_from_member(user)

        await database.execute(
            "DELETE FROM team_players WHERE team_id = $1 AND discord_id = $2",
            team_row["id"], database.did(user.id),
        )
        await record_team_transaction(
            season_id=team_row["season_id"],
            team_id=team_row["id"],
            team_name=team_row["country"],
            team_discord_role_id=team_row["discord_role_id"],
            transaction_type="remove_player",
            requested_role=roster_row["role"],
            status="accepted",
            requester_discord_id=database.did(interaction.user.id),
            requester_discord_username=discord_username_of(interaction.user),
            handled_by_discord_id=database.did(interaction.user.id),
            handled_by_discord_username=discord_username_of(interaction.user),
            handled_at=datetime.now(timezone.utc),
            player_discord_id=database.did(user.id),
            player_discord_username=discord_username_of(user),
            roblox_username=profile_data.get("username"),
            roblox_user_id=database.did(profile_data.get("user_id")) if profile_data.get("user_id") else None,
        )

        team_role = guild.get_role(int(team_row["discord_role_id"])) if team_row["discord_role_id"] else None
        extra_role = guild.get_role(extra_role_id_for(roster_row["role"]))
        roles_to_remove = [r for r in (team_role, extra_role) if r and r in user.roles]

        if roles_to_remove:
            await user.remove_roles(*roles_to_remove, reason=f"Manual roster removal by {interaction.user}")

        embed = build_staff_remove_embed(
            requester=interaction.user,
            player=user,
            team_name=team_row["country"]
        )
        await interaction.followup.send(embed=embed, ephemeral=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(TeamCog(bot))
