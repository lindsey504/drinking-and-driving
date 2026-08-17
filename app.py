from flask import Flask, render_template, request, redirect, url_for, jsonify
from dotenv import load_dotenv
from supabase import create_client
from apscheduler.schedulers.background import BackgroundScheduler
import os
import requests
import atexit
from datetime import date, datetime, timedelta, timezone
from config import *
import logging

# Set up proper logging so we can actually see what the scheduler is doing
# instead of silent failures disappearing into the void
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("drinking-and-driving")

# Track when we last refreshed scores so we can refresh on page load
# if the data is stale (critical for Replit autoscale, which kills the
# background scheduler when the app sleeps)
_last_score_refresh = None
STALE_THRESHOLD_MINUTES = 10  # if scores are older than this, refresh on page load

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret")

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)

# ESPN event ID stored on the tournament row as external_id
ESPN_LEADERBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/golf/pga/scoreboard?event={event_id}"
ESPN_SCHEDULE_URL = "https://site.api.espn.com/apis/site/v2/sports/golf/pga/scoreboard"

# ── Course info — keyed by tournament name ────────────────────────────────────
COURSE_INFO = {
    "Texas Children's Houston Open": {
        "course": "Memorial Park Golf Course",
        "location": "Houston, TX",
        "par": 70,
        "yards": "7,475",
        "notes": "Renovated 2019 (Tom Doak) · Est. 1912 · Municipal course",
        "purse": "$9,900,000",
    },
    "Valero Texas Open": {
        "course": "TPC San Antonio (AT&T Oaks Course)",
        "location": "San Antonio, TX",
        "par": 72,
        "yards": "7,494",
        "notes": "Designed by Pete Dye & Greg Norman (2010)",
        "purse": "$9,200,000",
    },
    "Masters Tournament": {
        "course": "Augusta National Golf Club",
        "location": "Augusta, GA",
        "par": 72,
        "yards": "7,510",
        "notes": "Founded 1933 by Bobby Jones & Clifford Roberts · No cut line",
        "purse": "$21,000,000",
    },
    "RBC Heritage": {
        "course": "Harbour Town Golf Links",
        "location": "Hilton Head Island, SC",
        "par": 71,
        "yards": "7,121",
        "notes": "Designed by Pete Dye & Jack Nicklaus (1969) · Signature Event",
        "purse": "$20,000,000",
    },
    "Zurich Classic of New Orleans": {
        "course": "TPC Louisiana",
        "location": "Avondale, LA",
        "par": 72,
        "yards": "7,425",
        "notes": "2-man team event · Foursome (alternate shot) + Fourball format",
        "purse": "$9,800,000",
    },
    "Cadillac Championship": {
        "course": "Trump National Doral (Blue Monster)",
        "location": "Doral, FL",
        "par": 72,
        "yards": "7,529",
        "notes": "Redesigned by Gil Hanse (2024) · Signature Event",
        "purse": "$25,000,000",
    },
    "PGA Championship": {
        "course": "Quail Hollow Club",
        "location": "Charlotte, NC",
        "par": 71,
        "yards": "7,521",
        "notes": "Designed by George Cobb (1961) · Hosted 2017 PGA Championship",
        "purse": "$18,500,000",
    },
    "U.S. Open": {
        "course": "Oakmont Country Club",
        "location": "Oakmont, PA",
        "par": 70,
        "yards": "7,255",
        "notes": "Founded 1903 · One of the most difficult courses in the world",
        "purse": "$21,500,000",
    },
}

# ── Scheduler — auto-refresh scores + fetch tournaments ───────────────────────

def do_score_refresh(source="scheduler"):
    """
    Core score refresh logic. Pulls live scores from ESPN and writes them
    to Supabase. Called by both the background scheduler AND on page load
    when data is stale (to survive Replit autoscale sleeping the app).

    'source' is just a label for logging so we can tell what triggered it.
    Returns (tournament, count) tuple or (None, 0) if nothing happened.
    """
    global _last_score_refresh

    tournament = get_current_tournament()
    if not tournament:
        logger.info(f"[{source}] no active tournament found, skipping score refresh")
        return None, 0

    if not tournament.get("external_id"):
        # Try to auto-match the tournament to an ESPN event before giving up.
        # This handles the case where fetch_and_sync_tournaments created the
        # row but a manually-added tournament doesn't have an ESPN ID.
        logger.warning(f"[{source}] active tournament '{tournament['name']}' has no ESPN external_id, trying auto-sync first")
        fetch_and_sync_tournaments()
        # Re-fetch in case sync found a match
        tournament = get_current_tournament()
        if not tournament or not tournament.get("external_id"):
            logger.warning(f"[{source}] still no external_id after sync, cannot refresh scores")
            return None, 0

    scores = fetch_live_scores(tournament["external_id"])
    if not scores:
        logger.info(f"[{source}] ESPN returned 0 matched scores for '{tournament['name']}' (event {tournament['external_id']})")
        return tournament, 0

    for entry in scores:
        row = {
            "tournament_id": tournament["id"],
            "player_id": entry["player_id"],
            "score_to_par": entry["score_to_par"],
            "round": entry["round"],
            "cut": entry.get("cut", False)
        }
        if entry.get("total_strokes") is not None:
            row["total_strokes"] = entry["total_strokes"]
        supabase.table("scores").upsert(row, on_conflict="tournament_id,player_id").execute()

    _last_score_refresh = datetime.now(timezone.utc)
    logger.info(f"[{source}] refreshed {len(scores)} scores for '{tournament['name']}'")
    return tournament, len(scores)


def refresh_if_stale():
    """
    Check if scores are stale and refresh them if needed.
    This is the key fix for Replit autoscale: instead of relying solely on
    APScheduler (which dies when the app sleeps), we also refresh on page
    load whenever the data is older than STALE_THRESHOLD_MINUTES.
    """
    global _last_score_refresh
    now = datetime.now(timezone.utc)

    if _last_score_refresh is None or (now - _last_score_refresh) > timedelta(minutes=STALE_THRESHOLD_MINUTES):
        logger.info("[on-demand] scores are stale, triggering refresh on page load")
        do_score_refresh(source="on-demand")


def scheduled_score_refresh():
    """Called by APScheduler every 15 minutes (when the app is awake)."""
    with app.app_context():
        do_score_refresh(source="scheduler")


def fetch_and_sync_tournaments():
    """
    Fetch upcoming tournaments from ESPN and sync to database.
    Also backfills external_id on existing tournaments that are missing it
    (handles the case where a tournament was manually added without an ESPN ID).
    """
    with app.app_context():
        try:
            resp = requests.get(ESPN_SCHEDULE_URL, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"[fetch_tournaments] ESPN fetch error: {e}")
            return

        # Get upcoming events
        events = data.get("events", [])
        if not events:
            logger.info("[fetch_tournaments] no events found on ESPN")
            return

        # Get current tournaments from DB to avoid dupes
        existing = supabase.table("tournaments").select("id, name, external_id").execute().data
        existing_ids = {t["external_id"] for t in existing if t.get("external_id")}
        # Build a name lookup for tournaments missing external_id
        missing_ext_id = {t["name"].lower(): t["id"] for t in existing if not t.get("external_id")}

        added = 0
        backfilled = 0
        for event in events:
            event_id = event.get("id")
            if not event_id:
                continue

            name = event.get("name", "Unknown Tournament")
            start_date = event.get("date", None)
            # Events have dates like "2026-06-11T00:00Z" -- extract just YYYY-MM-DD
            if start_date:
                start_date = start_date.split("T")[0]

            # No built-in end_date in ESPN, so estimate as +3 days
            if start_date:
                sd = date.fromisoformat(start_date)
                ed = sd + timedelta(days=3)
                end_date = ed.isoformat()
            else:
                continue

            # If this ESPN event already exists in our DB, skip it
            if event_id in existing_ids:
                continue

            # Check if we have a tournament with the same name but no external_id
            # (manually added). If so, backfill the ESPN ID instead of creating a dupe.
            if name.lower() in missing_ext_id:
                db_id = missing_ext_id[name.lower()]
                supabase.table("tournaments").update({"external_id": event_id}).eq("id", db_id).execute()
                logger.info(f"[fetch_tournaments] backfilled external_id '{event_id}' on existing tournament '{name}'")
                backfilled += 1
                continue

            try:
                supabase.table("tournaments").insert({
                    "name": name,
                    "external_id": event_id,
                    "start_date": start_date,
                    "end_date": end_date,
                    "active": False
                }).execute()
                logger.info(f"[fetch_tournaments] added: {name} ({start_date}, ESPN ID: {event_id})")
                added += 1
            except Exception as e:
                logger.error(f"[fetch_tournaments] insert error for {name}: {e}")

        logger.info(f"[fetch_tournaments] done: {added} added, {backfilled} backfilled")


# Configure scheduler with Replit-safe settings
scheduler = BackgroundScheduler(
    daemon=True,  # Let the app shut down gracefully
    timezone='UTC'
)

# Score refresh every 15 minutes (when tournament is active)
scheduler.add_job(scheduled_score_refresh, "interval", minutes=SCORE_REFRESH_MINUTES)

# Tournament fetch daily at 6 AM UTC (was weekly, but that caused gaps
# when a tournament appeared on ESPN after the Monday sync window)
scheduler.add_job(fetch_and_sync_tournaments, "cron", hour=6, minute=0)

try:
    scheduler.start()
    logger.info("[scheduler] started successfully")
except Exception as e:
    logger.info(f"[scheduler] startup error: {e}")

atexit.register(lambda: scheduler.shutdown(wait=False))

# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return "ok", 200


@app.route("/rules")
def rules():
    return render_template("rules.html", league=LEAGUE_NAME)


@app.route("/")
def index():
    """Homepage -- season standings + trophy case."""
    # Refresh scores on homepage load too, so standings are never stale
    refresh_if_stale()
    teams = supabase.table("teams").select("*").execute().data
    tournaments = supabase.table("tournaments").select("*").order("start_date", desc=True).execute().data

    # Build trophy case and compute season standings from completed tournaments
    trophy_case = []
    completed = [t for t in tournaments if not t.get("active")]

    team_wins = {t["id"]: 0 for t in teams}
    team_points = {t["id"]: 0 for t in teams}

    for t in completed:
        totals = compute_team_totals(t["id"])
        if not totals:
            continue

        # Trophy case — record winner
        winner = totals[0]
        score = winner["total"]
        score_str = f"+{score}" if score > 0 else str(score)
        trophy_case.append({
            "tournament": t["name"],
            "team": winner["team"]["name"],
            "manager": winner["team"]["manager"],
            "score": score_str
        })

        # Season points — award based on finish position
        for i, result in enumerate(totals):
            tid = result["team"]["id"]
            pts = FINISH_POINTS[i] if i < len(FINISH_POINTS) else 0
            team_points[tid] = team_points.get(tid, 0) + pts
            if i == 0:
                team_wins[tid] = team_wins.get(tid, 0) + 1

    # Build standings sorted by points desc, then wins desc
    standings = sorted([
        {
            "id": t["id"],
            "name": t["name"],
            "manager": t["manager"],
            "wins": team_wins.get(t["id"], 0),
            "points": team_points.get(t["id"], 0),
        }
        for t in teams
    ], key=lambda x: (-x["points"], -x["wins"]))

    today = date.today().isoformat()
    upcoming = [t for t in tournaments if t.get("start_date", "") > today and not t.get("active")]
    upcoming = list(reversed(upcoming))  # chronological order

    return render_template("index.html", standings=standings, tournaments=tournaments,
                           trophy_case=trophy_case, upcoming=upcoming, league=LEAGUE_NAME)


@app.route("/roster/<team_id>")
def roster(team_id):
    """View a team's full 15-player roster."""
    team = supabase.table("teams").select("*").eq("id", team_id).single().execute().data
    players = supabase.table("roster").select("*, players(*)").eq("team_id", team_id).execute().data
    return render_template("roster.html", team=team, players=players)


@app.route("/lineup/<team_id>", methods=["GET", "POST"])
def lineup(team_id):
    """Set weekly starters. Uses active tournament or falls back to the next upcoming one."""
    team = supabase.table("teams").select("*").eq("id", team_id).single().execute().data
    current_tournament = get_current_tournament() or get_next_tournament()
    roster = supabase.table("roster").select("*, players(*)").eq("team_id", team_id).execute().data

    if request.method == "POST":
        starters = request.form.getlist("starters")
        if len(starters) != STARTERS_PER_WEEK:
            return f"Pick exactly {STARTERS_PER_WEEK} starters.", 400

        if current_tournament:
            supabase.table("lineups").delete().eq("team_id", team_id).eq("tournament_id", current_tournament["id"]).execute()
            for player_id in starters:
                supabase.table("lineups").insert({
                    "team_id": team_id,
                    "tournament_id": current_tournament["id"],
                    "player_id": player_id
                }).execute()
        return redirect(url_for("scoreboard"))

    current_lineup = []
    if current_tournament:
        current_lineup = [
            row["player_id"]
            for row in supabase.table("lineups").select("player_id").eq("team_id", team_id).eq("tournament_id", current_tournament["id"]).execute().data
        ]

    return render_template("lineup.html", team=team, roster=roster, current_lineup=current_lineup,
                           starters_needed=STARTERS_PER_WEEK, tournament=current_tournament)


def fetch_tee_times(event_id):
    """Fetch tee times from ESPN core API. Returns dict of player_name -> {tee_time, hole, group}."""
    try:
        url = f"http://sports.core.api.espn.com/v2/sports/golf/leagues/pga/events/{event_id}/competitions/{event_id}/competitors?limit=200&lang=en&region=us"
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        data = r.json()
        items = data.get("items", [])
        logger.info(f"[tee times] ESPN returned {len(items)} competitors for event {event_id}")
        
        tee_times = {}
        for item in items:
            # Each item is a $ref — fetch the competitor detail
            ref = item.get("$ref", "")
            if not ref:
                continue
            cr = requests.get(ref, timeout=3).json()
            athlete = cr.get("athlete", {})
            name = athlete.get("fullName") or athlete.get("displayName")
            tee_time = cr.get("teeTime")
            hole = cr.get("startHole")
            group = cr.get("groupId")
            if name and tee_time:
                tee_times[name] = {"tee_time": tee_time, "hole": hole, "group": group}
        
        logger.info(f"[tee times] extracted {len(tee_times)} tee times from ESPN")
        return tee_times
    except Exception as e:
        logger.info(f"[tee times] error fetching from ESPN: {e}")
        return {}


@app.route("/scoreboard")
@app.route("/scoreboard/<tournament_id>")
def scoreboard(tournament_id=None):
    """Live scores for the current tournament, or final scores for a past one."""
    # If viewing the current/live scoreboard (not a specific past tournament),
    # refresh scores on page load if they are stale. This is the main fix for
    # Replit autoscale killing the background scheduler.
    if not tournament_id:
        refresh_if_stale()

    # Determine which tournament to show
    if tournament_id:
        result = supabase.table("tournaments").select("*").eq("id", tournament_id).limit(1).execute().data
        tournament = result[0] if result else None
    else:
        tournament = get_current_tournament()
        if not tournament:
            today = date.today().isoformat()
            # Show next upcoming tournament as a preview
            upcoming_next = (
                supabase.table("tournaments")
                .select("*")
                .eq("active", False)
                .gt("start_date", today)
                .order("start_date", desc=False)
                .limit(1)
                .execute()
                .data
            )
            if upcoming_next:
                tournament = upcoming_next[0]
                tournament["_preview"] = True
            else:
                # Nothing upcoming — fall back to most recently completed
                past = (
                    supabase.table("tournaments")
                    .select("*")
                    .eq("active", False)
                    .lt("end_date", today)
                    .order("end_date", desc=True)
                    .limit(1)
                    .execute()
                    .data
                )
                tournament = past[0] if past else None

    if not tournament:
        return render_template("scoreboard.html", tournament=None, scores=[], is_final=False, is_preview=False)

    is_preview = tournament.get("_preview", False)
    is_final = not tournament.get("active", False) and not is_preview
    scores = supabase.table("scores").select("*, players(*)").eq("tournament_id", tournament["id"]).execute().data
    team_totals = compute_team_totals(tournament["id"])
    is_major = tournament["name"] in MAJORS

    # Past results nav — only truly completed tournaments (end_date in the past)
    today = date.today().isoformat()
    past_tournaments = (
        supabase.table("tournaments")
        .select("id, name, end_date")
        .eq("active", False)
        .lt("end_date", today)
        .order("end_date", desc=True)
        .execute()
        .data
    )

    # Tee times — fetch during preview (before tournament) and live (during tournament)
    # Don't fetch after tournament is final
    tee_times_raw = {}
    team_tee_times = []
    if not is_final and tournament.get("external_id"):
        tee_times_raw = fetch_tee_times(tournament["external_id"])
        teams = supabase.table("teams").select("*").execute().data
        for team in teams:
            lineup_rows = supabase.table("lineups").select("player_id, players(name)").eq("team_id", team["id"]).eq("tournament_id", tournament["id"]).execute().data
            starters = []
            for row in lineup_rows:
                pname = row["players"]["name"] if row.get("players") else None
                tt = tee_times_raw.get(pname, {})
                starters.append({
                    "name": pname,
                    "tee_time": tt.get("tee_time"),
                    "hole": tt.get("hole"),
                })
            if starters:
                team_tee_times.append({"team": team, "starters": starters})

    course = COURSE_INFO.get(tournament["name"]) if tournament else None

    return render_template("scoreboard.html", tournament=tournament, scores=scores,
                           team_totals=team_totals, is_major=is_major, multiplier=MAJORS_MULTIPLIER,
                           team_tee_times=team_tee_times, tee_times_available=bool(tee_times_raw),
                           is_final=is_final, is_preview=is_preview,
                           past_tournaments=past_tournaments, course=course)


@app.route("/scoreboard/live")
def scoreboard_live():
    """HTMX partial — live team score widget, refreshes every 30s."""
    tournament = get_current_tournament()
    if not tournament or not tournament.get("active"):
        return '<p class="muted">No active tournament.</p>'
    team_totals = compute_team_totals(tournament["id"])
    is_major = tournament["name"] in MAJORS
    return render_template("_live_scores.html", team_totals=team_totals,
                           is_major=is_major, multiplier=MAJORS_MULTIPLIER)


@app.route("/player/<player_id>")
def player_stats(player_id):
    """Player profile — scoring history and stats."""
    player = supabase.table("players").select("*").eq("id", player_id).single().execute().data

    # Find which team drafted this player (if any)
    roster_row = supabase.table("roster").select("team_id").eq("player_id", player_id).limit(1).execute().data
    team = None
    if roster_row:
        team = supabase.table("teams").select("*").eq("id", roster_row[0]["team_id"]).single().execute().data

    # Get score history across all tournaments
    scores_raw = supabase.table("scores").select("*, tournaments(name, start_date)").eq("player_id", player_id).order("tournaments(start_date)", desc=True).execute().data

    history = []
    for s in scores_raw:
        score = s["score_to_par"] or 0
        history.append({
            "tournament": s["tournaments"]["name"] if s.get("tournaments") else "Unknown",
            "score_to_par": score,
            "score_str": f"+{score}" if score > 0 else str(score),
            "round": s["round"],
            "cut": s["cut"]
        })

    tournaments_played = len(history)
    cuts_made = sum(1 for h in history if not h["cut"])
    scores_only = [h["score_to_par"] for h in history if not h["cut"]]
    avg_score = round(sum(scores_only) / len(scores_only), 1) if scores_only else "—"
    best_score = min(scores_only) if scores_only else "—"
    if isinstance(best_score, int):
        best_score = f"+{best_score}" if best_score > 0 else str(best_score)

    return render_template("player.html", player=player, team=team, history=history,
                           tournaments_played=tournaments_played, cuts_made=cuts_made,
                           avg_score=avg_score, best_score=best_score)


@app.route("/roster/<team_id>/drop/<roster_id>", methods=["POST"])
def drop_player(team_id, roster_id):
    """Drop a player from a team's roster."""
    supabase.table("roster").delete().eq("id", roster_id).eq("team_id", team_id).execute()
    return redirect(url_for("roster", team_id=team_id))


@app.route("/draft")
def draft():
    """Draft board — shows available players."""
    all_players = supabase.table("players").select("*").order("world_rank").execute().data
    drafted = {row["player_id"] for row in supabase.table("roster").select("player_id").execute().data}
    available = [p for p in all_players if p["id"] not in drafted]
    teams = supabase.table("teams").select("*").execute().data
    return render_template("draft.html", available=available, teams=teams, roster_size=ROSTER_SIZE)


@app.route("/draft/pick", methods=["POST"])
def draft_pick():
    """Record a draft pick."""
    team_id = request.form["team_id"]
    player_id = request.form["player_id"]
    roster_count = supabase.table("roster").select("id", count="exact").eq("team_id", team_id).execute().count
    if roster_count >= ROSTER_SIZE:
        return "Roster full.", 400
    supabase.table("roster").insert({"team_id": team_id, "player_id": player_id}).execute()
    return redirect(url_for("draft"))


# ── API ──────────────────────────────────────────────────────────────────────

@app.route("/api/refresh-scores")
def refresh_scores():
    """Manually trigger a score refresh. Uses the same core logic as the scheduler."""
    tournament, count = do_score_refresh(source="manual-api")
    if not tournament:
        return jsonify({"status": "no active tournament (or no ESPN ID)", "updated": 0})
    return jsonify({"status": "ok", "tournament": tournament["name"], "updated": count})


@app.route("/api/sync-tournaments")
def sync_tournaments():
    """Manually trigger tournament sync."""
    fetch_and_sync_tournaments()
    return jsonify({"status": "tournament sync triggered"})


@app.route("/api/debug")
def debug_status():
    """
    Debug endpoint: shows the current state of the score pipeline so you
    can see exactly why scores might not be updating. Hit this URL in your
    browser to diagnose issues without needing server logs.
    """
    today = date.today().isoformat()

    # What tournament does the app think is active?
    active_tournament = get_current_tournament()

    # How many tournaments are in the DB total?
    all_tournaments = supabase.table("tournaments").select("id, name, start_date, end_date, active, external_id").order("start_date", desc=True).limit(10).execute().data

    # How many players in the pool?
    player_count = supabase.table("players").select("id", count="exact").execute().count

    # When did we last refresh?
    last_refresh_str = _last_score_refresh.isoformat() if _last_score_refresh else "never (since last app restart)"

    # Check ESPN API health
    espn_ok = False
    espn_events = []
    try:
        r = requests.get(ESPN_SCHEDULE_URL, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
        espn_ok = r.status_code == 200
        if espn_ok:
            for e in r.json().get("events", []):
                espn_events.append({
                    "name": e.get("name"),
                    "id": e.get("id"),
                    "status": e.get("status", {}).get("type", {}).get("name"),
                    "date": e.get("date")
                })
    except Exception:
        pass

    return jsonify({
        "today": today,
        "active_tournament": {
            "name": active_tournament["name"],
            "id": active_tournament["id"],
            "external_id": active_tournament.get("external_id"),
            "active": active_tournament.get("active"),
            "start_date": active_tournament.get("start_date"),
            "end_date": active_tournament.get("end_date"),
        } if active_tournament else None,
        "last_score_refresh": last_refresh_str,
        "stale_threshold_minutes": STALE_THRESHOLD_MINUTES,
        "player_count": player_count,
        "recent_tournaments_in_db": all_tournaments,
        "espn_api_ok": espn_ok,
        "espn_current_events": espn_events,
        "scheduler_running": scheduler.running,
    })


@app.route("/api/finalize-tournament/<tournament_id>", methods=["POST"])
def finalize_tournament(tournament_id):
    """Mark a tournament complete (active=False) and return final standings."""
    result = supabase.table("tournaments").select("*").eq("id", tournament_id).limit(1).execute().data
    if not result:
        return jsonify({"status": "tournament not found"}), 404
    tournament = result[0]

    supabase.table("tournaments").update({"active": False}).eq("id", tournament_id).execute()

    totals = compute_team_totals(tournament_id)
    results = [
        {"rank": i + 1, "team": r["team"]["name"], "manager": r["team"]["manager"],
         "score_to_par": r["total"], "total_strokes": r["total_strokes"]}
        for i, r in enumerate(totals)
    ]

    return jsonify({
        "status": "finalized",
        "tournament": tournament["name"],
        "results": results
    })


@app.route("/api/refresh-scores/<tournament_id>")
def refresh_scores_for_tournament(tournament_id):
    """
    Manually refresh scores for a specific tournament (even if it is not active).
    Use this to backfill scores for a past tournament that was missed.
    Example: /api/refresh-scores/3b40026c-7b30-4bdc-b92d-893dcf4a9fc2
    """
    result = supabase.table("tournaments").select("*").eq("id", tournament_id).limit(1).execute().data
    if not result:
        return jsonify({"status": "tournament not found"}), 404

    tournament = result[0]
    if not tournament.get("external_id"):
        # Try to find the ESPN event ID by syncing
        fetch_and_sync_tournaments()
        tournament = supabase.table("tournaments").select("*").eq("id", tournament_id).limit(1).execute().data[0]
        if not tournament.get("external_id"):
            return jsonify({"status": "no ESPN external_id on this tournament", "tournament": tournament["name"]}), 400

    scores = fetch_live_scores(tournament["external_id"])
    if not scores:
        return jsonify({"status": "ESPN returned 0 matched scores", "tournament": tournament["name"], "external_id": tournament["external_id"], "updated": 0})

    for entry in scores:
        row = {
            "tournament_id": tournament["id"],
            "player_id": entry["player_id"],
            "score_to_par": entry["score_to_par"],
            "round": entry["round"],
            "cut": entry.get("cut", False)
        }
        if entry.get("total_strokes") is not None:
            row["total_strokes"] = entry["total_strokes"]
        supabase.table("scores").upsert(row, on_conflict="tournament_id,player_id").execute()

    logger.info(f"[backfill] refreshed {len(scores)} scores for '{tournament['name']}'")
    return jsonify({"status": "ok", "tournament": tournament["name"], "updated": len(scores)})


@app.route("/admin/set-lineups/<tournament_id>/<team_id>", methods=["POST"])
def admin_set_lineups(tournament_id, team_id):
    """Admin endpoint: Set a team's lineup for a tournament. POST with player_ids in JSON body."""
    data = request.get_json()
    player_ids = data.get("player_ids", [])
    
    if len(player_ids) != STARTERS_PER_WEEK:
        return jsonify({"status": f"Must provide exactly {STARTERS_PER_WEEK} player IDs"}), 400
    
    # Clear existing lineups for this team/tournament
    supabase.table("lineups").delete().eq("team_id", team_id).eq("tournament_id", tournament_id).execute()
    
    # Insert new lineups
    for player_id in player_ids:
        supabase.table("lineups").insert({
            "team_id": team_id,
            "tournament_id": tournament_id,
            "player_id": player_id
        }).execute()
    
    return jsonify({"status": "lineups set", "team_id": team_id, "count": len(player_ids)})


@app.route("/admin/finalize/<tournament_id>", methods=["POST"])
def admin_finalize(tournament_id):
    """Admin endpoint: Mark tournament complete and finalize standings."""
    result = supabase.table("tournaments").select("*").eq("id", tournament_id).limit(1).execute().data
    if not result:
        return jsonify({"status": "tournament not found"}), 404
    
    tournament = result[0]
    
    # Mark as inactive (finalized)
    supabase.table("tournaments").update({"active": False}).eq("id", tournament_id).execute()
    
    # Get final standings
    team_totals = compute_team_totals(tournament_id)
    
    results = [
        {
            "rank": i + 1,
            "team": t["team"]["name"],
            "manager": t["team"]["manager"],
            "score_to_par": t["total"],
            "total_strokes": t["total_strokes"]
        }
        for i, t in enumerate(team_totals)
    ]
    
    return jsonify({
        "status": "tournament finalized",
        "tournament": tournament["name"],
        "results": results
    })


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_current_tournament():
    """Return the active tournament. Auto-activates by date if none is manually set."""
    today = date.today().isoformat()
    
    # Auto-deactivate: mark any tournament with end_date < today as inactive
    expired = (
        supabase.table("tournaments")
        .select("id, name, end_date")
        .eq("active", True)
        .lt("end_date", today)
        .execute()
        .data
    )
    
    for t in expired:
        supabase.table("tournaments").update({"active": False}).eq("id", t["id"]).execute()
        logger.info(f"[auto-deactivate] deactivated tournament: {t['name']}")
        
        # Auto-activate the next upcoming tournament
        next_tournament = (
            supabase.table("tournaments")
            .select("*")
            .eq("active", False)
            .gt("start_date", today)
            .order("start_date", desc=False)
            .limit(1)
            .execute()
            .data
        )
        if next_tournament:
            nt = next_tournament[0]
            supabase.table("tournaments").update({"active": True}).eq("id", nt["id"]).execute()
            logger.info(f"[auto-advance] auto-activated next tournament: {nt['name']} ({nt['start_date']})")
    
    # Check for active tournament (after deactivation + auto-advance)
    result = supabase.table("tournaments").select("*").eq("active", True).limit(1).execute().data
    if result:
        return result[0]

    # Auto-activate: find a tournament whose date window contains today
    candidates = (
        supabase.table("tournaments")
        .select("*")
        .lte("start_date", today)
        .gte("end_date", today)
        .limit(1)
        .execute()
        .data
    )
    if candidates:
        t = candidates[0]
        supabase.table("tournaments").update({"active": True}).eq("id", t["id"]).execute()
        t["active"] = True
        logger.info(f"[auto-activate] activated tournament: {t['name']}")
        return t

    return None


def get_next_tournament():
    """Return the next upcoming tournament (soonest start_date in the future)."""
    today = date.today().isoformat()
    result = (
        supabase.table("tournaments")
        .select("*")
        .eq("active", False)
        .gt("start_date", today)
        .order("start_date", desc=False)
        .limit(1)
        .execute()
        .data
    )
    return result[0] if result else None


def compute_team_totals(tournament_id):
    """Sum each team's starters' scores. Applies majors multiplier if applicable. Returns score-to-par + total strokes + player breakdown."""
    # Get tournament to check if it's a major
    tournament = supabase.table("tournaments").select("name").eq("id", tournament_id).single().execute().data
    is_major = tournament.get("name") in MAJORS
    multiplier = MAJORS_MULTIPLIER if is_major else 1.0
    
    lineups = supabase.table("lineups").select("team_id, player_id, players(name)").eq("tournament_id", tournament_id).execute().data
    scores_raw = supabase.table("scores").select("player_id, score_to_par, total_strokes, round, cut").eq("tournament_id", tournament_id).execute().data
    score_map = {s["player_id"]: s for s in scores_raw}
    teams_raw = supabase.table("teams").select("*").execute().data
    team_map = {t["id"]: t for t in teams_raw}

    totals = {}
    for pick in lineups:
        tid = pick["team_id"]
        pid = pick["player_id"]
        s = score_map.get(pid, {})
        stp = (s.get("score_to_par") or 0) * multiplier  # Apply multiplier
        strokes = s.get("total_strokes") or 0
        pname = pick.get("players", {}).get("name", "Unknown") if pick.get("players") else "Unknown"
        if tid not in totals:
            totals[tid] = {"score_to_par": 0, "total_strokes": 0, "players": []}
        totals[tid]["score_to_par"] += stp
        totals[tid]["total_strokes"] += strokes
        totals[tid]["players"].append({
            "name": pname,
            "score_to_par": int(round(stp)),
            "total_strokes": strokes,
            "round": s.get("round", 0),
            "cut": s.get("cut", False)
        })

    return sorted([
        {"team": team_map[tid], "total": int(round(data["score_to_par"])),
         "total_strokes": data["total_strokes"], "players": data["players"]}
        for tid, data in totals.items()
        if tid in team_map
    ], key=lambda x: (x["total"], x["total_strokes"]))


def fetch_live_scores(espn_event_id):
    """
    Fetch live scores from ESPN API.
    Returns list of dicts: {player_id, score_to_par, round, cut, total_strokes}
    Matches players by name against our players table.
    """
    url = ESPN_LEADERBOARD_URL.format(event_id=espn_event_id)
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"ESPN fetch error for event {espn_event_id}: {e}")
        return []

    # Get our player list for name matching
    our_players = supabase.table("players").select("id, name").execute().data
    name_map = {p["name"].lower(): p["id"] for p in our_players}

    results = []
    events = data.get("events", [])
    if not events:
        logger.warning(f"ESPN returned no events for event ID {espn_event_id}")
        return []

    competitors = events[0].get("competitions", [{}])[0].get("competitors", [])
    current_round = events[0].get("status", {}).get("period", 1)
    logger.info(f"ESPN returned {len(competitors)} competitors, round {current_round}")

    matched = 0
    unmatched = 0
    for comp in competitors:
        athlete = comp.get("athlete", {})
        full_name = athlete.get("displayName", "").lower()
        player_id = name_map.get(full_name)
        if not player_id:
            unmatched += 1
            continue  # player not in our pool, skip
        matched += 1

        score_str = comp.get("score", "0")
        try:
            # ESPN returns score-to-par as a string like "-3", "+2", "E", or "0"
            # Handle decimal values (e.g., "67.5") by rounding to nearest integer
            if score_str in (None, "E", ""):
                score_to_par = 0
            else:
                score_to_par = int(round(float(score_str)))
        except (ValueError, TypeError):
            score_to_par = 0

        # Total strokes = sum of round scores from linescores
        # Each linescore entry has a "value" that is the strokes for that round
        total_strokes = 0
        for rnd in comp.get("linescores", []):
            try:
                val = rnd.get("value", 0)
                total_strokes += int(round(float(val)))
            except (ValueError, TypeError):
                pass

        # Cut detection: ESPN has moved this field around over time.
        # Check multiple possible locations to be safe.
        status_name = comp.get("status", {}).get("type", {}).get("name", "") or ""
        # Also check the top-level "type" field (ESPN sometimes puts "CUT" here)
        comp_type = comp.get("type", "") or ""
        did_cut = "CUT" in status_name.upper() or "CUT" in str(comp_type).upper()

        # If a player has 0 linescores but the tournament is past round 2,
        # they likely missed the cut (ESPN sometimes just omits their data)
        if not did_cut and current_round > 2 and len(comp.get("linescores", [])) <= 2:
            did_cut = True

        if did_cut:
            score_to_par = 0  # missed cut = no score, no penalty
            total_strokes = 0

        results.append({
            "player_id": player_id,
            "score_to_par": score_to_par,
            "total_strokes": total_strokes,
            "round": current_round,
            "cut": did_cut
        })

    logger.info(f"Name matching: {matched} matched, {unmatched} unmatched out of {len(competitors)} ESPN competitors")
    if matched == 0 and len(competitors) > 0:
        # Log some ESPN names so we can debug name mismatches
        sample_names = [c.get("athlete", {}).get("displayName", "?") for c in competitors[:5]]
        sample_db_names = list(name_map.keys())[:5]
        logger.warning(f"Zero name matches! ESPN sample: {sample_names}, DB sample: {sample_db_names}")

    return results


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
