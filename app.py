from flask import Flask, render_template, request, redirect, url_for, jsonify
from dotenv import load_dotenv
from supabase import create_client
from apscheduler.schedulers.background import BackgroundScheduler
import os
import requests
import atexit
from config import *

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret")

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)

# ESPN event ID stored on the tournament row as external_id
ESPN_LEADERBOARD_URL = "https://site.web.api.espn.com/apis/site/v2/sports/golf/pga/leaderboard?event={event_id}"

# ── Scheduler — auto-refresh scores every 15 min ─────────────────────────────

def scheduled_score_refresh():
    """Called by APScheduler every 15 minutes."""
    with app.app_context():
        tournament = get_current_tournament()
        if not tournament or not tournament.get("external_id"):
            return
        scores = fetch_live_scores(tournament["external_id"])
        for entry in scores:
            supabase.table("scores").upsert({
                "tournament_id": tournament["id"],
                "player_id": entry["player_id"],
                "score_to_par": entry["score_to_par"],
                "round": entry["round"],
                "cut": entry.get("cut", False)
            }, on_conflict="tournament_id,player_id").execute()
        print(f"[scheduler] refreshed {len(scores)} scores for {tournament['name']}")

scheduler = BackgroundScheduler()
scheduler.add_job(scheduled_score_refresh, "interval", minutes=SCORE_REFRESH_MINUTES)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())

# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/rules")
def rules():
    return render_template("rules.html", league=LEAGUE_NAME)


@app.route("/")
def index():
    """Homepage — season standings."""
    teams = supabase.table("teams").select("*").execute().data
    tournaments = supabase.table("tournaments").select("*").order("start_date", desc=True).execute().data
    return render_template("index.html", teams=teams, tournaments=tournaments, league=LEAGUE_NAME)


@app.route("/roster/<team_id>")
def roster(team_id):
    """View a team's full 15-player roster."""
    team = supabase.table("teams").select("*").eq("id", team_id).single().execute().data
    players = supabase.table("roster").select("*, players(*)").eq("team_id", team_id).execute().data
    return render_template("roster.html", team=team, players=players)


@app.route("/lineup/<team_id>", methods=["GET", "POST"])
def lineup(team_id):
    """Set weekly starters. Locks when tournament starts."""
    team = supabase.table("teams").select("*").eq("id", team_id).single().execute().data
    current_tournament = get_current_tournament()
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


@app.route("/scoreboard")
def scoreboard():
    """Live scores for the current tournament."""
    tournament = get_current_tournament()
    if not tournament:
        return render_template("scoreboard.html", tournament=None, scores=[])

    scores = supabase.table("scores").select("*, players(*)").eq("tournament_id", tournament["id"]).execute().data
    team_totals = compute_team_totals(tournament["id"])
    is_major = tournament["name"] in MAJORS

    return render_template("scoreboard.html", tournament=tournament, scores=scores,
                           team_totals=team_totals, is_major=is_major, multiplier=MAJORS_MULTIPLIER)


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
    """Manually trigger a score refresh. Also called by scheduler."""
    tournament = get_current_tournament()
    if not tournament:
        return jsonify({"status": "no active tournament"})
    if not tournament.get("external_id"):
        return jsonify({"status": "no ESPN event ID set on tournament"})

    scores = fetch_live_scores(tournament["external_id"])
    if not scores:
        return jsonify({"status": "no scores returned (tournament may not have started)"})

    for entry in scores:
        supabase.table("scores").upsert({
            "tournament_id": tournament["id"],
            "player_id": entry["player_id"],
            "score_to_par": entry["score_to_par"],
            "round": entry["round"],
            "cut": entry.get("cut", False)
        }, on_conflict="tournament_id,player_id").execute()

    return jsonify({"status": "ok", "updated": len(scores)})


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_current_tournament():
    """Return the active tournament."""
    result = supabase.table("tournaments").select("*").eq("active", True).limit(1).execute().data
    return result[0] if result else None


def compute_team_totals(tournament_id):
    """Sum each team's starters' scores."""
    lineups = supabase.table("lineups").select("team_id, player_id").eq("tournament_id", tournament_id).execute().data
    scores_raw = supabase.table("scores").select("player_id, score_to_par").eq("tournament_id", tournament_id).execute().data
    score_map = {s["player_id"]: s["score_to_par"] for s in scores_raw}
    teams_raw = supabase.table("teams").select("*").execute().data
    team_map = {t["id"]: t for t in teams_raw}

    totals = {}
    for pick in lineups:
        tid = pick["team_id"]
        pid = pick["player_id"]
        score = score_map.get(pid, 0) or 0
        totals[tid] = totals.get(tid, 0) + score

    return sorted([
        {"team": team_map[tid], "total": total}
        for tid, total in totals.items()
    ], key=lambda x: x["total"])


def fetch_live_scores(espn_event_id):
    """
    Fetch live scores from ESPN API.
    Returns list of dicts: {player_id, score_to_par, round, cut}
    Matches players by name against our players table.
    """
    url = ESPN_LEADERBOARD_URL.format(event_id=espn_event_id)
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"ESPN fetch error: {e}")
        return []

    # Get our player list for name matching
    our_players = supabase.table("players").select("id, name").execute().data
    name_map = {p["name"].lower(): p["id"] for p in our_players}

    results = []
    events = data.get("events", [])
    if not events:
        return []

    competitors = events[0].get("competitions", [{}])[0].get("competitors", [])
    current_round = events[0].get("status", {}).get("period", 1)

    for comp in competitors:
        athlete = comp.get("athlete", {})
        full_name = athlete.get("displayName", "").lower()
        player_id = name_map.get(full_name)
        if not player_id:
            continue  # player not in our pool, skip

        score_val = comp.get("score", {}).get("value", 0)
        try:
            score_to_par = int(score_val) if score_val is not None else 0
        except (ValueError, TypeError):
            score_to_par = 0

        status = comp.get("status", {}).get("type", {}).get("name", "")
        did_cut = "CUT" in status.upper()
        if did_cut:
            score_to_par = 10  # missed cut penalty

        results.append({
            "player_id": player_id,
            "score_to_par": score_to_par,
            "round": current_round,
            "cut": did_cut
        })

    return results


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
