-- ============================================================================
-- rollback-supabase-lockdown.sql
--
-- EMERGENCY ROLLBACK for secure-supabase.sql.
--
-- WHEN TO USE: the app lost database access after the lockdown was applied.
-- That almost always means the server is still authenticating with the ANON
-- key rather than the service_role key, so RLS is correctly denying it.
--
-- WHAT THIS DOES: reopens the database to the anon key, which also reopens it
-- to the public internet. This is a deliberate, temporary retreat to restore
-- service, NOT a fix. Set SUPABASE_SERVICE_ROLE_KEY in Railway, confirm the
-- app is using it, then re-run secure-supabase.sql.
-- ============================================================================

-- Turn RLS back off.
alter table public.teams       disable row level security;
alter table public.players     disable row level security;
alter table public.roster      disable row level security;
alter table public.tournaments disable row level security;
alter table public.lineups     disable row level security;
alter table public.scores      disable row level security;

-- Restore the grants that the lockdown revoked. Without this, disabling RLS
-- alone is not enough: the anon role would still hold no table privileges.
grant usage on schema public to anon, authenticated;
grant select, insert, update, delete on all tables in schema public to anon, authenticated;
grant usage, select on all sequences in schema public to anon, authenticated;
