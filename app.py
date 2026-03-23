from flask import Flask, render_template, request, redirect, url_for, jsonify
from dotenv import load_dotenv
from supabase import create_client
import os
import requests
from config import *

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret")

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)

# ── Routes ──────────────────────────────────────────────────────────────────

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

        # Clear old lineup for this tournament
        if current_tournament:
            supabase.table("lineups").delete().eq("team_id", team_id).eq("tournament_id", current_tournament["id"]).execute()
            for player_id in starters:
                supabase.table("lineups").insert({
                    "team_id": team_id,
                    "tournament_id": current_tournament["id"],
                    "player_id": player_id
                }).execute()
        return redirect(url_for("scoreboard"))

    # Get current lineup
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

    scores = supabase.table("scores").select("*, players(*), teams(*)").eq("tournament_id", tournament["id"]).execute().data
    team_totals = compute_team_totals(tournament["id"])
    is_major = tournament["name"] in MAJORS

    return render_template("scoreboard.html", tournament=tournament, scores=scores,
                           team_totals=team_totals, is_major=is_major, multiplier=MAJORS_MULTIPLIER)


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
    """Pull live scores from TheSportsDB. Called by cron."""
    tournament = get_current_tournament()
    if not tournament:
        return jsonify({"status": "no active tournament"})

    scores = fetch_live_scores(tournament["external_id"])
    if not scores:
        return jsonify({"status": "no scores returned"})

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
    """Return the active or most recent tournament."""
    result = supabase.table("tournaments").select("*").eq("active", True).limit(1).execute().data
    return result[0] if result else None


def compute_team_totals(tournament_id):
    """Sum each team's 5 starters' scores for the current tournament."""
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
    ], key=lambda x: x["total"])  # lower is better


def fetch_live_scores(external_tournament_id):
    """Fetch scores from TheSportsDB. Returns list of score dicts."""
    api_key = os.getenv("SPORTSDB_API_KEY", "3")
    url = f"https://www.thesportsdb.com/api/v1/json/{api_key}/eventsseason.php?id={external_tournament_id}"
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        # TheSportsDB returns events; map to our score format
        # This will need adjustment once you see the actual API response shape
        return []  # TODO: map API response to score dicts
    except Exception as e:
        print(f"Score fetch error: {e}")
        return []


if __name__ == "__main__":
    app.run(debug=True)
