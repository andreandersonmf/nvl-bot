-- ============================================================
-- CVR South America (CVR SA) — Supabase schema (fresh start)
-- ============================================================
-- Run this once on a brand-new Supabase project. It creates every
-- table used by BOTH cvr-sa-site and cvr-sa-bot. Unlike the old SAVL
-- setup, the bot no longer keeps a local SQLite "shadow" database —
-- it talks to this same schema directly, over a Postgres connection
-- (asyncpg), exactly like the site does over the Supabase client.
-- That removes the old two-schema-with-sync-bridge design entirely:
-- there is now a single source of truth per table.
--
-- Convention: every column that stores a raw Discord snowflake
-- (user/role/channel/message/guild id) is TEXT, never BIGINT/INTEGER.
-- Discord IDs are 64-bit and JavaScript's Number can silently lose
-- precision above 2^53, so the site's Supabase client (and now the
-- bot too) always treats them as opaque text.
-- ============================================================

create extension if not exists "pgcrypto"; -- gen_random_uuid()

-- ============================================================
-- Seasons & league-wide settings
-- ============================================================

create table if not exists seasons (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    slug text not null unique,
    theme_name text,
    status text,
    is_active boolean default false,
    is_archived boolean default false,
    awards_status text,
    created_at timestamptz not null default now()
);

-- Single-row settings table (id is always 1).
create table if not exists league_settings (
    id integer primary key default 1,
    registrations_open boolean not null default true,
    awards_public boolean not null default false,
    leaderboard_public boolean not null default true,
    active_season_id uuid references seasons(id) on delete set null,
    updated_at timestamptz not null default now(),
    constraint league_settings_singleton check (id = 1)
);
insert into league_settings (id) values (1) on conflict (id) do nothing;

-- ============================================================
-- Identity: profiles (Discord OAuth via Supabase Auth) & roles
-- ============================================================

create table if not exists profiles (
    id uuid primary key default gen_random_uuid(),
    auth_user_id uuid unique,
    discord_id text unique,
    discord_username text,
    discord_global_name text,
    roblox_username text,
    roblox_user_id text,
    avatar_url text,
    created_at timestamptz not null default now()
);
create index if not exists idx_profiles_discord_id on profiles(discord_id);

-- Legacy table: kept only so the original email/password admin login
-- keeps working exactly like before ('admin' role tied to the auth uid).
create table if not exists user_roles (
    id uuid primary key,
    role text not null,
    created_at timestamptz not null default now()
);

-- Newer, granular Discord-OAuth-driven roles, additive to user_roles.
create table if not exists site_user_roles (
    profile_id uuid not null references profiles(id) on delete cascade,
    role text not null check (role in ('administrator', 'stat_tracker', 'referee', 'media')),
    granted_by uuid references profiles(id) on delete set null,
    created_at timestamptz not null default now(),
    primary key (profile_id, role)
);

-- ============================================================
-- Teams & roster
-- ============================================================

create table if not exists teams (
    id bigint generated always as identity primary key,
    season_id uuid references seasons(id) on delete set null,
    country text not null,
    code text,
    captain_name text,
    captain_discord text,
    captain_discord_id text,
    captain_roblox_id text,
    discord_role_id text,
    approved boolean not null default false,
    approved_at timestamptz,
    brick_color_name text,
    brick_color_hex text,
    brick_color_number integer,
    group_letter text check (group_letter in ('A', 'B', 'C', 'D')),
    created_at timestamptz not null default now()
);
create index if not exists idx_teams_season on teams(season_id);
create index if not exists idx_teams_captain_discord_id on teams(captain_discord_id);
create index if not exists idx_teams_discord_role_id on teams(discord_role_id);

-- Roster role. "Court Captain" is new for CVR SA: same permissions as
-- Vice Captain everywhere, and a team may have BOTH at the same time.
create table if not exists team_players (
    id bigint generated always as identity primary key,
    team_id bigint not null references teams(id) on delete cascade,
    season_id uuid references seasons(id) on delete set null,
    profile_id uuid references profiles(id) on delete set null,
    roblox_username text not null,
    roblox_user_id text not null,
    discord_username text not null,
    discord_id text,
    role text not null default 'Player' check (role in ('Vice Captain', 'Court Captain', 'Player')),
    created_at timestamptz not null default now()
);
create index if not exists idx_team_players_team on team_players(team_id);
create index if not exists idx_team_players_discord_id on team_players(discord_id);

-- Roster transaction log (adds, removes, leaves, captain changes,
-- transfer approvals/denials). Replaces the bot's old local
-- "transfers" table — /team add now creates a 'pending' row here
-- directly instead of in a separate SQLite table.
create table if not exists team_transactions (
    id uuid primary key default gen_random_uuid(),
    season_id uuid references seasons(id) on delete set null,
    team_id bigint references teams(id) on delete set null,
    team_name text,
    team_discord_role_id text,
    player_profile_id uuid references profiles(id) on delete set null,
    source text not null default 'site' check (source in ('site', 'discord')),
    external_source text,
    external_id text,
    transaction_type text not null check (
        transaction_type in ('add_player', 'remove_player', 'leave_team', 'captain_change', 'staff_adjust')
    ),
    requested_role text,
    status text not null default 'pending' check (status in ('pending', 'accepted', 'denied')),
    reason text,
    requester_discord_id text,
    requester_discord_username text,
    handled_by_discord_id text,
    handled_by_discord_username text,
    player_discord_id text,
    player_discord_username text,
    roblox_username text,
    roblox_user_id text,
    discord_message_id text,
    discord_channel_id text,
    handled_at timestamptz,
    created_at timestamptz not null default now()
);
create index if not exists idx_team_transactions_team on team_transactions(team_id);
create index if not exists idx_team_transactions_status on team_transactions(status);

-- ============================================================
-- Official matches & player stats
-- ============================================================

create table if not exists matches (
    id bigint generated always as identity primary key,
    season_id uuid references seasons(id) on delete set null,
    home_country text not null,
    away_country text not null,
    stage text,
    match_date date,
    match_time text,
    status text not null default 'Scheduled' check (status in ('Scheduled', 'Live', 'Finished')),
    home_score integer not null default 0,
    away_score integer not null default 0,
    winner_country text,
    referee_id bigint,
    media_id bigint,
    stat_tracker_id bigint,
    is_star_match boolean not null default false,
    stats_finalized boolean not null default false,
    stats_submitted_for_review boolean not null default false,
    set1_home integer, set1_away integer,
    set2_home integer, set2_away integer,
    set3_home integer, set3_away integer,
    set4_home integer, set4_away integer,
    set5_home integer, set5_away integer,
    referee_discord_id text,
    media_link text,
    discord_reminder_sent boolean not null default false,
    created_by_discord_id text,
    created_at timestamptz not null default now()
);
create index if not exists idx_matches_season on matches(season_id);
create index if not exists idx_matches_status on matches(status);
create index if not exists idx_matches_date on matches(match_date);

create table if not exists match_player_stats (
    id bigint generated always as identity primary key,
    season_id uuid references seasons(id) on delete set null,
    match_id bigint not null references matches(id) on delete cascade,
    team_country text not null,
    player_key text not null,
    player_name text not null,
    set_number integer not null,
    spiking_errors integer not null default 0,
    ape_kills integer not null default 0,
    ape_attempts integer not null default 0,
    kills integer not null default 0,
    attempts integer not null default 0,
    one_touches integer not null default 0,
    kill_blocks integer not null default 0,
    assists integer not null default 0,
    spike_receives integer,
    serve_bfs integer not null default 0,
    receives integer not null default 0,
    dives integer not null default 0,
    aces integer not null default 0,
    misc_errors integer not null default 0,
    created_at timestamptz not null default now()
);
create index if not exists idx_mps_match on match_player_stats(match_id);
create index if not exists idx_mps_season on match_player_stats(season_id);

-- ============================================================
-- Staff applications (Referee / Media / Stat Tracker)
-- ============================================================

create table if not exists staff_applications (
    id bigint generated always as identity primary key,
    role text not null check (role in ('Referee', 'Media', 'Stat Tracker')),
    email text,
    user_id uuid,
    roblox_username text not null,
    discord_username text not null,
    roblox_user_id text not null,
    commitment_confirmed boolean not null default false,
    rulebook_confirmed boolean not null default false,
    approved boolean not null default false,
    approved_at timestamptz,
    created_at timestamptz not null default now()
);

-- ============================================================
-- VIP / VIP+ (Matchmaking) — Stripe + Pix
-- ============================================================

create table if not exists vip_payments (
    id bigint generated always as identity primary key,
    profile_id uuid references profiles(id) on delete set null,
    discord_id text not null,
    tier text not null check (tier in ('vip', 'vip_plus')),
    amount_cents integer not null,
    provider text not null default 'stripe',
    provider_checkout_id text,
    provider_payment_id text,
    checkout_url text,
    status text not null default 'pending' check (status in ('pending', 'paid', 'failed')),
    paid_at timestamptz,
    created_at timestamptz not null default now()
);
create index if not exists idx_vip_payments_discord_id on vip_payments(discord_id);

create table if not exists vip_subscriptions (
    id bigint generated always as identity primary key,
    profile_id uuid references profiles(id) on delete set null,
    discord_id text not null,
    tier text not null check (tier in ('vip', 'vip_plus')),
    status text not null default 'active' check (status in ('active', 'expired', 'cancelled')),
    source_payment_id bigint references vip_payments(id) on delete set null,
    role_applied boolean not null default false,
    expires_at timestamptz not null,
    created_at timestamptz not null default now()
);
create index if not exists idx_vip_subs_discord_id on vip_subscriptions(discord_id);
create index if not exists idx_vip_subs_status on vip_subscriptions(status);

-- ============================================================
-- Matchmaking (ranked pickup games): queue -> captains -> draft ->
-- match -> ELO. Was local SQLite on the bot; now lives here directly.
-- ============================================================

create table if not exists mm_players (
    discord_id text primary key,
    elo integer not null default 1000,
    matches integer not null default 0,
    wins integer not null default 0,
    losses integer not null default 0,
    win_mvp integer not null default 0,
    loss_mvp integer not null default 0,
    elo_gained_total integer not null default 0,
    elo_lost_total integer not null default 0,
    created_at timestamptz not null default now()
);

create table if not exists mm_seasons (
    number integer primary key,
    is_active boolean not null default false,
    started_at timestamptz,
    ended_at timestamptz
);

create table if not exists mm_season_players (
    id bigint generated always as identity primary key,
    season_number integer not null references mm_seasons(number) on delete cascade,
    discord_id text not null,
    matches integer not null default 0,
    wins integer not null default 0,
    losses integer not null default 0,
    win_mvp integer not null default 0,
    loss_mvp integer not null default 0,
    elo_gained integer not null default 0,
    elo_lost integer not null default 0,
    created_at timestamptz not null default now(),
    unique (season_number, discord_id)
);

create table if not exists mm_matches (
    id bigint generated always as identity primary key,
    match_number integer not null unique,
    season_number integer references mm_seasons(number) on delete set null,
    status text not null,
    created_by_discord_id text not null,
    queue_channel_id text,
    queue_message_id text,
    captain1_discord_id text,
    captain2_discord_id text,
    first_picker_discord_id text,
    private_server_link text,
    text_channel_id text,
    team_a_voice_id text,
    team_b_voice_id text,
    winner_side text,
    loser_side text,
    wmvp_discord_id text,
    lmvp_discord_id text,
    final_score_text text,
    is_special boolean not null default false,
    special_multiplier integer not null default 1,
    created_at timestamptz not null default now(),
    started_at timestamptz,
    finished_at timestamptz
);
create index if not exists idx_mm_matches_status on mm_matches(status);
create index if not exists idx_mm_matches_season on mm_matches(season_number);

create table if not exists mm_match_players (
    id bigint generated always as identity primary key,
    match_number integer not null references mm_matches(match_number) on delete cascade,
    discord_id text not null,
    role_pref text not null,
    team_side text,
    captain boolean not null default false,
    pick_order integer,
    priority_weight integer not null default 0,
    joined_at timestamptz not null default now(),
    unique (match_number, discord_id)
);
create index if not exists idx_mm_match_players_match on mm_match_players(match_number);
create index if not exists idx_mm_match_players_discord_id on mm_match_players(discord_id);

create table if not exists mm_replacements (
    id bigint generated always as identity primary key,
    match_number integer not null references mm_matches(match_number) on delete cascade,
    old_discord_id text not null,
    new_discord_id text not null,
    replaced_by_discord_id text not null,
    penalty_applied boolean not null default false,
    created_at timestamptz not null default now()
);

-- ============================================================
-- Done. Next steps:
--   1. Create the Supabase project, run this file once (SQL Editor).
--   2. Fill cvr-sa-bot/.env and cvr-sa-site/.env.local with the new
--      project's URL + keys (see README_MUDANCAS.md).
--   3. Grant Discord OAuth admin roles to existing staff BEFORE first
--      deploy (site_user_roles), same rule as the old SAVL setup.
-- ============================================================
