import os
from dotenv import load_dotenv

load_dotenv(override=True)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

# Official match channels (used by schedule.py and match-notify)
MATCH_SCHEDULE_CHANNEL_ID = int(os.getenv("MATCH_SCHEDULE_CHANNEL_ID", "1546600993721155675"))
MATCH_LINKS_CHANNEL_ID    = int(os.getenv("MATCH_LINKS_CHANNEL_ID", "1544780635384578061"))
STREAM_ALERT_CHANNEL_ID   = int(os.getenv("STREAM_ALERT_CHANNEL_ID", "1545074917639323768"))
STREAM_ALERT_ROLE_ID      = int(os.getenv("STREAM_ALERT_ROLE_ID", "1544780634411765886"))
MATCH_RESULTS_CHANNEL_ID  = int(os.getenv("MATCH_RESULTS_CHANNEL_ID", "1544780635384578062"))

# Staff Discord role IDs (applied automatically on approval)
STAFF_REFEREE_ROLE_ID     = int(os.getenv("STAFF_REFEREE_ROLE_ID", "1544780634441121827"))
STAFF_STREAMER_ROLE_ID    = int(os.getenv("STAFF_STREAMER_ROLE_ID", "1544780634441121826"))
STAFF_STAT_TRACKER_ROLE_ID = int(os.getenv("STAFF_STAT_TRACKER_ROLE_ID", "1548018986204139611"))

TRANSACTIONS_CHANNEL_ID = int(os.getenv("TRANSACTIONS_CHANNEL_ID", "0"))
SELF_TRANSACTIONS_CHANNEL_ID = int(os.getenv("SELF_TRANSACTIONS_CHANNEL_ID", "0"))
SCRIM_CHANNEL_ID = int(os.getenv("SCRIM_CHANNEL_ID", "0"))

CAPTAIN_ROLE_ID = int(os.getenv("CAPTAIN_ROLE_ID", "0"))
VICE_CAPTAIN_ROLE_ID = int(os.getenv("VICE_CAPTAIN_ROLE_ID", "0"))
# New for NVL: Court Captain has the exact same permissions as Vice
# Captain everywhere in the bot (team management commands, roster
# checks, etc). A team can have a Vice Captain AND a Court Captain
# at the same time.
COURT_CAPTAIN_ROLE_ID = int(os.getenv("COURT_CAPTAIN_ROLE_ID", "0"))
REFEREE_ROLE_ID = int(os.getenv("REFEREE_ROLE_ID", "0"))
STREAMER_ROLE_ID = int(os.getenv("STREAMER_ROLE_ID", "0"))
PLAYER_ROLE_ID = int(os.getenv("PLAYER_ROLE_ID", "0"))

_staff_ids_raw = os.getenv("STAFF_APPROVER_ROLE_IDS", "")
STAFF_APPROVER_ROLE_IDS = [
    int(role_id.strip())
    for role_id in _staff_ids_raw.split(",")
    if role_id.strip().isdigit()
]

# Official match scheduling/results (cogs/schedule.py, cogs/match.py).
# Kept for parity with the old project; these two cogs are not loaded
# by bot.py by default (see EXTENSIONS) - only enable them if you
# actually want /schedule and /match result active on Discord.
MATCH_ORGANIZER_ROLE_ID = int(os.getenv("MATCH_ORGANIZER_ROLE_ID", "0"))
MATCHMAKING_CATEGORY_ID = int(os.getenv("MATCHMAKING_CATEGORY_ID", "0"))
MM_RESULTS_CHANNEL_ID = int(os.getenv("MM_RESULTS_CHANNEL_ID", "0"))
# Bugfix carried over from the NVL source project: this was never wired
# to an env var there, so it silently always fell back to a hardcoded
# old-server channel ID. Fixed here - set it in your .env.
ELO_UPDATE_CHANNEL_ID = int(os.getenv("ELO_UPDATE_CHANNEL_ID", "0"))

# ------------------------------------------------------------
# Database (Supabase Postgres)
# ------------------------------------------------------------
# NVL no longer keeps a local SQLite file. The bot connects
# directly to the same Supabase Postgres database the site uses, via
# a plain Postgres connection string (asyncpg) rather than the REST
# API. Use Supabase's "Session pooler" connection string (Database
# Settings -> Connection string -> Session pooler) - it works over
# IPv4, which most bot hosts (including Discloud) require, unlike the
# direct connection host which is IPv6-only on the free tier.
#
# Example:
# DATABASE_URL=postgresql://postgres.xxxxxxxx:PASSWORD@aws-0-region.pooler.supabase.com:5432/postgres
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Matchmaking VIP/VIP+ Discord roles, applied by the site's payment webhook
# and removed automatically by cogs/vip.py when a subscription expires.
VIP_ROLE_ID = int(os.getenv("VIP_ROLE_ID", "0"))
VIP_PLUS_ROLE_ID = int(os.getenv("VIP_PLUS_ROLE_ID", "0"))

# Used to build "buy VIP" links in Discord embeds (e.g. https://nvl-site.vercel.app).
NVL_SITE_URL = os.getenv("NVL_SITE_URL", "https://nvl-site.vercel.app").rstrip("/")
