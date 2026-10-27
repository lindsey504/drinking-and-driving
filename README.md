# ⛳ Drinking & Driving

A private fantasy golf league app for 3 friends.

## How it works

- Snake draft to build your 15-player pool at season start
- Pick 5 starters before each PGA Tour tournament
- Scores update live during tournaments (every 15 min)
- Lowest combined score wins the week
- Majors count 1.5x

## Stack

- Python + Flask (backend)
- Supabase (database)
- Plain HTML + HTMX (frontend)
- Railway (hosting + cron)
- TheSportsDB (golf data, free)

## Setup

1. Clone the repo
2. Copy `.env.example` to `.env` and fill in your keys
3. `pip install -r requirements.txt`
4. `python app.py`

## Making changes with Claude Code

Just describe what you want to change in plain English. Examples:
- "Add a column showing each player's world ranking"
- "Change majors to count 2x instead of 1.5x"
- "Add a trade deadline feature"

## Config

Edit `config.py` to change:
- Scoring rules
- Number of starters per week
- Majors multiplier
- Number of teams

## Database security

The Supabase database is **closed to the public internet**. Access works like this:

```
Browser  ->  Flask app        (the only way in)
Flask    ->  Supabase         (using the service_role key)
Anyone   ->  Supabase direct  (DENIED)
```

Every table has Row Level Security enabled with **zero policies**, which denies
all access, and the `anon` and `authenticated` roles have had their table grants
revoked. The Flask app connects with the `service_role` key, which bypasses RLS.

This is safe because all database calls are server-side. Nothing in `templates/`
or `static/` touches Supabase, so the admin-level key never reaches a browser.

### Applying the lockdown

Order matters. Doing step 2 before step 1 takes the app down until step 1 lands.

1. **Add the service role key to Railway.**
   Supabase dashboard > Project Settings > API Keys > copy `service_role`.
   In Railway, add variable `SUPABASE_SERVICE_ROLE_KEY` with that value.
   Leave the old `SUPABASE_KEY` in place for now. Let the app redeploy and
   confirm the site still loads.

2. **Run the lockdown SQL.**
   Paste `secure-supabase.sql` into the Supabase SQL Editor and run it. The
   script ends with two verification queries. Confirm that every table shows
   `rls_enabled = true`, `rls_forced = true`, `policy_count = 0`, and that the
   grants query returns **zero rows**.

3. **Remove the old variable.**
   Delete `SUPABASE_KEY` from Railway so nothing can silently fall back to the
   anon key.

4. **Rotate the anon key.** Because it had unrestricted access while RLS was
   off, treat it as exposed. Supabase dashboard > Project Settings > API Keys.

### Verifying it worked

With the anon key, this should now return an empty array or a permission error
rather than your league data:

```bash
curl "$SUPABASE_URL/rest/v1/teams?select=*" \
  -H "apikey: $SUPABASE_ANON_KEY"
```

