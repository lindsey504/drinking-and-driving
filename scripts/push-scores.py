#!/usr/bin/env python3
"""
Score pusher script -- fetches live scores from ESPN and pushes them
to the Drinking & Driving app on Railway.

WHY THIS EXISTS:
ESPN blocks requests from Railway's cloud IPs. This script runs from
a non-blocked location (your laptop, GitHub Actions, cron-job.org, etc.)
and pushes the scores into the app via the /api/push-scores endpoint.

USAGE:
    # One-time run (fetch + push current tournament scores):
    python3 scripts/push-scores.py

    # Backfill a specific tournament by ESPN event ID:
    python3 scripts/push-scores.py --event 401811962

    # Run every 15 minutes via cron (macOS):
    # crontab -e, then add:
    # */15 * * * * cd /Users/lindsey/Projects/drinking-and-driving && python3 scripts/push-scores.py >> /tmp/score-push.log 2>&1

ENVIRONMENT:
    Set these in .env or export them:
    - APP_URL: Railway app URL (default: https://drinking-and-driving.up.railway.app)
    - APP_SECRET_KEY: must match FLASK_SECRET_KEY on the server
"""

import requests
import json
import os
import sys
from datetime import datetime

# -- Configuration ----------------------------------------------------------

# Where the app lives
APP_URL = os.getenv("APP_URL", "https://drinking-and-driving.up.railway.app")

# Must match the FLASK_SECRET_KEY on the server for authentication
APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "")

# ESPN endpoints
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/golf/pga/scoreboard"
ESPN_EVENT_URL = "https://site.api.espn.com/apis/site/v2/sports/golf/pga/scoreboard?event={event_id}"

# Fake a browser so ESPN doesn't block us
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def fetch_espn_scores(event_id=None):
    """
    Fetch scores from ESPN. If event_id is provided, fetch that specific
    event. Otherwise, fetch whatever ESPN is currently showing (the active
    or most recent tournament).

    Returns (event_name, event_id, list_of_score_dicts) or (None, None, [])
    """
    url = ESPN_EVENT_URL.format(event_id=event_id) if event_id else ESPN_SCOREBOARD
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching ESPN: {url}")

    try:
        resp = requests.get(url, timeout=10, headers=HEADERS)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  ESPN fetch failed: {e}")
        return None, None, []

    events = data.get("events", [])
    if not events:
        print("  No events returned from ESPN")
        return None, None, []

    event = events[0]
    event_name = event.get("name", "Unknown")
    event_id = event.get("id", "")
    status = event.get("status", {}).get("type", {}).get("name", "unknown")
    print(f"  Event: {event_name} (ID: {event_id}, status: {status})")

    competitors = event.get("competitions", [{}])[0].get("competitors", [])
    current_round = event.get("status", {}).get("period", 1)
    print(f"  Competitors: {len(competitors)}, Round: {current_round}")

    scores = []
    for comp in competitors:
        athlete = comp.get("athlete", {})
        name = athlete.get("displayName", "")
        if not name:
            continue

        # Parse score-to-par
        score_str = comp.get("score", "0")
        try:
            if score_str in (None, "E", ""):
                score_to_par = 0
            else:
                score_to_par = int(round(float(score_str)))
        except (ValueError, TypeError):
            score_to_par = 0

        # Total strokes from linescores
        total_strokes = 0
        for rnd in comp.get("linescores", []):
            try:
                val = rnd.get("value", 0)
                total_strokes += int(round(float(val)))
            except (ValueError, TypeError):
                pass

        # Cut detection
        status_name = comp.get("status", {}).get("type", {}).get("name", "") or ""
        comp_type = comp.get("type", "") or ""
        did_cut = "CUT" in status_name.upper() or "CUT" in str(comp_type).upper()

        # If past round 2 and only has 2 or fewer linescores, likely missed cut
        if not did_cut and current_round > 2 and len(comp.get("linescores", [])) <= 2:
            did_cut = True

        if did_cut:
            score_to_par = 0
            total_strokes = 0

        scores.append({
            "player_name": name,
            "score_to_par": score_to_par,
            "total_strokes": total_strokes,
            "round": current_round,
            "cut": did_cut,
        })

    return event_name, event_id, scores


def get_tournament_id_from_app(espn_event_id):
    """
    Look up the tournament in the app's database by its ESPN event ID.
    Returns the app's internal tournament UUID, or None if not found.
    """
    print(f"  Looking up ESPN event {espn_event_id} in app database...")
    try:
        resp = requests.get(f"{APP_URL}/api/debug", timeout=10)
        data = resp.json()
    except Exception as e:
        print(f"  Could not reach app: {e}")
        return None

    # Check active tournament first
    active = data.get("active_tournament")
    if active and active.get("external_id") == str(espn_event_id):
        print(f"  Found as active tournament: {active['name']} ({active['id']})")
        return active["id"]

    # Check recent tournaments list
    for t in data.get("recent_tournaments_in_db", []):
        if t.get("external_id") == str(espn_event_id):
            print(f"  Found in recent tournaments: {t['name']} ({t['id']})")
            return t["id"]

    print(f"  ESPN event {espn_event_id} not found in app database")
    return None


def push_scores_to_app(tournament_id, scores):
    """
    Push scores to the app via the /api/push-scores endpoint.
    Requires APP_SECRET_KEY to authenticate.
    """
    if not APP_SECRET_KEY:
        print("  ERROR: APP_SECRET_KEY not set. Cannot push scores.")
        print("  Set it in your environment: export APP_SECRET_KEY='your-flask-secret'")
        return False

    print(f"  Pushing {len(scores)} scores to {APP_URL}/api/push-scores ...")
    try:
        resp = requests.post(
            f"{APP_URL}/api/push-scores",
            json={"tournament_id": tournament_id, "scores": scores},
            headers={"X-Api-Key": APP_SECRET_KEY},
            timeout=30,
        )
        result = resp.json()
        print(f"  Result: {json.dumps(result, indent=2)}")
        return result.get("status") == "ok"
    except Exception as e:
        print(f"  Push failed: {e}")
        return False


def main():
    # Check for --event flag for backfilling a specific tournament
    event_id = None
    if "--event" in sys.argv:
        idx = sys.argv.index("--event")
        if idx + 1 < len(sys.argv):
            event_id = sys.argv[idx + 1]
            print(f"Backfill mode: fetching ESPN event {event_id}")

    # Step 1: Fetch scores from ESPN
    event_name, espn_id, scores = fetch_espn_scores(event_id)
    if not scores:
        print("No scores to push. Done.")
        return

    print(f"  Got {len(scores)} player scores from ESPN")

    # Step 2: Find the matching tournament in the app
    tournament_id = get_tournament_id_from_app(espn_id)
    if not tournament_id:
        print(f"Could not find tournament for ESPN event {espn_id} in the app.")
        print("You may need to run /api/sync-tournaments on the app first.")
        return

    # Step 3: Push scores to the app
    success = push_scores_to_app(tournament_id, scores)
    if success:
        print(f"Done! Scores for {event_name} pushed successfully.")
    else:
        print("Score push failed. Check the output above for details.")


if __name__ == "__main__":
    main()
