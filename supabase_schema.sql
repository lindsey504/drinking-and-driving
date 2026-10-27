-- Run this in your Supabase SQL editor to set up the database

create table teams (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  manager text not null,
  wins integer default 0,
  points integer default 0,
  created_at timestamptz default now()
);

create table players (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  world_rank integer,
  country text,
  external_id text  -- ID used by TheSportsDB
);

create table roster (
  id uuid primary key default gen_random_uuid(),
  team_id uuid references teams(id),
  player_id uuid references players(id),
  unique(team_id, player_id)
);

create table tournaments (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  start_date date,
  end_date date,
  active boolean default false,
  external_id text  -- ID used by TheSportsDB
);

create table lineups (
  id uuid primary key default gen_random_uuid(),
  team_id uuid references teams(id),
  tournament_id uuid references tournaments(id),
  player_id uuid references players(id),
  unique(team_id, tournament_id, player_id)
);

create table scores (
  id uuid primary key default gen_random_uuid(),
  tournament_id uuid references tournaments(id),
  player_id uuid references players(id),
  score_to_par integer,
  round integer,
  cut boolean default false,
  updated_at timestamptz default now(),
  unique(tournament_id, player_id)
);

-- Seed the 3 teams
insert into teams (name, manager) values
  ('Team Tricky', 'Tricky'),
  ('Team 2', 'Player 2'),
  ('Team 3', 'Player 3');

-- ── Lock the tables down (REQUIRED, do not skip) ─────────────────────────────
--
-- WHY: tables created without RLS are readable AND writable by anyone holding
-- the anon key, which is a public, non-secret value. Enabling RLS with no
-- policies denies everyone except service_role, which bypasses RLS. The Flask
-- app connects with service_role, so it keeps working; the outside world gets
-- nothing. See secure-supabase.sql for the full explanation and verification
-- queries.

alter table teams       enable row level security;
alter table players     enable row level security;
alter table roster      enable row level security;
alter table tournaments enable row level security;
alter table lineups     enable row level security;
alter table scores      enable row level security;

revoke all on all tables    in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
alter default privileges in schema public revoke all on tables    from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;
