-- ============================================================================
-- secure-supabase.sql
--
-- PURPOSE: Close the Supabase database to the public internet.
--
-- WHY THIS IS NEEDED:
--   The original setup scripts (supabase_schema.sql / run-this-in-supabase.sql)
--   created every table WITHOUT Row Level Security. In Postgres, RLS is opt-in
--   per table, so "no RLS" means "no row-level gatekeeping at all". Combined
--   with the fact that these tables live in the `public` schema, which Supabase
--   exposes through its PostgREST Data API, anyone holding the project URL plus
--   the anon key could read, modify, or delete league data directly, without
--   ever going through the Flask app.
--
--   The anon key is NOT a secret. It is designed to be shipped to browsers.
--   It is only safe when RLS is the thing actually enforcing access.
--
-- THE MODEL THIS ESTABLISHES:
--   Browser  -> Flask app (the ONLY way in)
--   Flask    -> Supabase, using the service_role key
--   Anyone   -> Supabase directly = DENIED
--
--   This is safe for this project specifically because every database call in
--   app.py happens server-side. Nothing in templates/ or static/ talks to
--   Supabase, so there is no browser code that needs table access.
--
-- HOW service_role STILL WORKS:
--   Supabase's `service_role` database role is created with the BYPASSRLS
--   attribute. RLS policies simply do not apply to it. That is why we can
--   enable RLS and add ZERO policies: the server keeps full access, while
--   `anon` and `authenticated` are left with nothing.
--
-- IMPORTANT: Run this ONLY after switching the app to the service_role key,
-- or the app will lose database access. See the deploy order in the README.
--
-- This script is idempotent. Running it twice is harmless.
-- ============================================================================


-- ── STEP 1: Enable Row Level Security on every table ────────────────────────
--
-- Enabling RLS with no policies attached is a deny-by-default stance: the
-- table becomes invisible and unwritable to every role EXCEPT those that
-- bypass RLS (service_role) or own the table (postgres).
--
-- We deliberately add NO policies. There is no multi-tenant logic to express
-- here, because the application, not the database, decides who may do what.

alter table public.teams       enable row level security;
alter table public.players     enable row level security;
alter table public.roster      enable row level security;
alter table public.tournaments enable row level security;
alter table public.lineups     enable row level security;
alter table public.scores      enable row level security;


-- ── STEP 2: Force RLS even for the table owner ──────────────────────────────
--
-- WHY: plain `enable row level security` is skipped for the role that OWNS the
-- table. `force row level security` closes that gap, so even an accidental
-- connection as the owning role is still subject to policies. service_role is
-- unaffected because BYPASSRLS outranks FORCE.
--
-- This is belt-and-braces hardening, not strictly required, but it removes a
-- surprising edge case where "RLS is on" does not mean what you'd expect.

alter table public.teams       force row level security;
alter table public.players     force row level security;
alter table public.roster      force row level security;
alter table public.tournaments force row level security;
alter table public.lineups     force row level security;
alter table public.scores      force row level security;


-- ── STEP 3: Revoke the table-level grants from the public-facing roles ──────
--
-- WHY BOTHER, if RLS already denies everything?
--   Defense in depth. RLS and SQL grants are two independent gates:
--     - GRANT decides "may this role touch this table at all?"
--     - RLS   decides "which ROWS may it touch?"
--   If RLS were ever accidentally disabled on a table (easy to do from the
--   Supabase dashboard with one click), grants would still be standing between
--   the anon key and your data. Removing the grants means a single mistake is
--   no longer a full breach.
--
-- `anon`          = requests carrying the anon/publishable key (unauthenticated)
-- `authenticated` = requests carrying a logged-in user's JWT
--
-- This app uses neither, so both can safely lose all access.

revoke all on all tables    in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
revoke all on all functions in schema public from anon, authenticated;

-- Also stop Postgres from auto-granting these roles access to any table
-- created LATER. Without this, the next `create table` would hand anon
-- fresh permissions and quietly reopen the hole.
alter default privileges in schema public
  revoke all on tables    from anon, authenticated;
alter default privileges in schema public
  revoke all on sequences from anon, authenticated;
alter default privileges in schema public
  revoke all on functions from anon, authenticated;


-- ── STEP 4: Make sure service_role retained everything it needs ─────────────
--
-- Step 3 used "all tables ... from anon, authenticated" so service_role was
-- never targeted. We re-grant explicitly anyway, so this script is safe to run
-- even if someone previously ran a broader revoke that caught service_role.

grant usage on schema public to service_role;
grant all on all tables    in schema public to service_role;
grant all on all sequences in schema public to service_role;
grant all on all functions in schema public to service_role;

alter default privileges in schema public
  grant all on tables    to service_role;
alter default privileges in schema public
  grant all on sequences to service_role;


-- ── STEP 5: Verify the result ───────────────────────────────────────────────
--
-- Run this and confirm EVERY row shows rls_enabled = true, rls_forced = true,
-- and policy_count = 0. Zero policies is the goal, not a mistake: it is what
-- makes the deny-by-default stance total.

select
  c.relname                                   as table_name,
  c.relrowsecurity                            as rls_enabled,
  c.relforcerowsecurity                       as rls_forced,
  (select count(*) from pg_policies p
     where p.schemaname = 'public'
       and p.tablename  = c.relname)          as policy_count
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public'
  and c.relkind = 'r'
order by c.relname;


-- Confirm the public-facing roles hold no table privileges at all.
-- EXPECTED RESULT: zero rows. Any row returned here is a remaining hole.
select grantee, table_name, privilege_type
from information_schema.role_table_grants
where table_schema = 'public'
  and grantee in ('anon', 'authenticated')
order by table_name, grantee, privilege_type;
