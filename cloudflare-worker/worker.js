/**
 * Golf Score Pusher -- Cloudflare Worker
 *
 * Runs every 15 minutes via Cloudflare Cron Triggers.
 * Fetches live PGA scores from the PGA Tour official GraphQL API
 * and pushes them to the Drinking & Driving app on Railway.
 *
 * WHY THIS EXISTS:
 * ESPN blocks requests from cloud provider IPs (Railway, GitHub Actions,
 * even Cloudflare Workers). The PGA Tour's own GraphQL API does NOT block
 * cloud IPs, so we use it instead. It also gives us cleaner data in a
 * single request (no need for dozens of sub-requests like ESPN's core API).
 *
 * HOW IT WORKS:
 * 1. Query PGA Tour schedule to find the current IN_PROGRESS tournament
 * 2. Query the leaderboard for that tournament
 * 3. Look up the tournament in the Drinking & Driving app by name
 * 4. Push the scores to the app via /api/push-scores
 *
 * ENVIRONMENT VARIABLES (set via wrangler secret):
 *   APP_SECRET_KEY -- must match FLASK_SECRET_KEY on Railway
 */

// -- Config ------------------------------------------------------------------

// Where the Drinking & Driving app lives (hosted on Railway)
const APP_URL = "https://drinking-and-driving.up.railway.app";

// PGA Tour official GraphQL API endpoint and key
// This is a public API used by pgatour.com itself
const PGA_GRAPHQL_URL = "https://orchestrator.pgatour.com/graphql";
const PGA_API_KEY = "da2-gsrx5bibzbb4njvhl7t37wqyl4";

// Browser-like user agent for requests to the app
const USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";


// -- PGA Tour API helpers ----------------------------------------------------

/**
 * Send a GraphQL query to the PGA Tour API.
 * Returns the parsed JSON response, or throws on network/HTTP errors.
 */
async function pgaQuery(query) {
  const resp = await fetch(PGA_GRAPHQL_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": PGA_API_KEY,
    },
    body: JSON.stringify({ query }),
  });

  if (!resp.ok) {
    throw new Error(`PGA Tour API returned HTTP ${resp.status}`);
  }

  const json = await resp.json();

  // GraphQL can return 200 but still have errors
  if (json.errors && json.errors.length > 0) {
    throw new Error(`GraphQL error: ${json.errors[0].message}`);
  }

  return json.data;
}


/**
 * Find the current in-progress PGA Tour tournament.
 *
 * Queries the PGA Tour schedule for the current season and looks for
 * a tournament with tournamentStatus === "IN_PROGRESS".
 *
 * Returns { id, name } or null if nothing is in progress.
 */
async function findCurrentTournament() {
  // The PGA Tour season year. The 2025-2026 season is "2026".
  // We check the current year and the next year to handle the
  // season boundary (PGA season runs ~Sept to ~Aug).
  const now = new Date();
  const currentYear = now.getFullYear();
  // PGA "season year" is typically the year the season ends.
  // e.g., the 2025-2026 season is called "2026".
  const yearsToCheck = [String(currentYear), String(currentYear + 1)];

  for (const year of yearsToCheck) {
    const query = `{
      schedule(tourCode: "R", year: "${year}") {
        upcoming {
          tournaments {
            id
            tournamentName
            tournamentStatus
          }
        }
      }
    }`;

    try {
      const data = await pgaQuery(query);
      const sections = data?.schedule?.upcoming || [];

      for (const section of sections) {
        for (const t of section.tournaments || []) {
          if (t.tournamentStatus === "IN_PROGRESS") {
            return { id: t.id, name: t.tournamentName };
          }
        }
      }
    } catch (e) {
      console.log(`[pga] Schedule query for ${year} failed: ${e.message}`);
    }
  }

  return null;
}


/**
 * Fetch the full leaderboard for a given PGA Tour tournament.
 *
 * Uses the leaderboardV2 query with an inline fragment on PlayerRowV2
 * (because LeaderboardRowV2 is a GraphQL union type that can be either
 * a PlayerRowV2 or an InformationRow).
 *
 * Returns an array of score objects ready to push to the app:
 *   { player_name, score_to_par, total_strokes, round, cut }
 */
async function fetchLeaderboard(tournamentId) {
  const query = `{
    leaderboardV2(id: "${tournamentId}") {
      players {
        ... on PlayerRowV2 {
          player {
            displayName
          }
          total
          totalStrokes
          currentRound
          playerState
        }
      }
    }
  }`;

  const data = await pgaQuery(query);
  const players = data?.leaderboardV2?.players || [];
  const scores = [];

  for (const p of players) {
    // Skip InformationRow entries (they won't have player data)
    if (!p.player?.displayName) continue;

    const name = p.player.displayName;

    // Parse score-to-par from the "total" field
    // Examples: "-17", "+3", "E" (even par), "--" (not started)
    let scoreToPar = 0;
    const totalStr = (p.total || "").trim();
    if (totalStr === "E" || totalStr === "" || totalStr === "--") {
      scoreToPar = 0;
    } else {
      scoreToPar = Math.round(parseFloat(totalStr)) || 0;
    }

    // Parse total strokes (cumulative strokes across all rounds)
    // This is a string like "263" or "--"
    let totalStrokes = 0;
    const strokesStr = (p.totalStrokes || "").trim();
    if (strokesStr && strokesStr !== "--") {
      totalStrokes = Math.round(parseFloat(strokesStr)) || 0;
    }

    // Detect if the player missed the cut, withdrew, or was disqualified
    // PlayerState enum: ACTIVE, COMPLETE, CUT, WITHDRAWN, DISQUALIFIED,
    //                   NOT_STARTED, BETWEEN_ROUNDS
    const state = (p.playerState || "").toUpperCase();
    const didCut = state === "CUT" || state === "WITHDRAWN" || state === "DISQUALIFIED";

    // Zero out scores for cut/withdrawn/DQ players
    // (the app treats these as inactive with no score impact)
    if (didCut) {
      scoreToPar = 0;
      totalStrokes = 0;
    }

    scores.push({
      player_name: name,
      score_to_par: scoreToPar,
      total_strokes: totalStrokes,
      round: p.currentRound || 1,
      cut: didCut,
    });
  }

  return scores;
}


// -- App communication -------------------------------------------------------

/**
 * Look up a tournament in the Drinking & Driving app by its PGA Tour ID
 * or by name substring.
 *
 * The app stores an "external_id" for each tournament (the ESPN event ID),
 * but for PGA Tour IDs (like "R2026060") we need to search by name instead.
 *
 * Returns the tournament object { id, name } or null if not found.
 */
async function findTournamentInApp(pgaTournamentName) {
  // First try: search by name using the find-tournament endpoint
  // The PGA Tour tournament name (e.g., "TOUR Championship") should
  // match what the app has stored (synced from ESPN).
  // The endpoint returns { found: true, tournaments: [...] } when it
  // finds matches (note: "tournaments" plural, not singular).
  try {
    const resp = await fetch(
      `${APP_URL}/api/find-tournament?name=${encodeURIComponent(pgaTournamentName)}`,
      { headers: { "User-Agent": USER_AGENT } }
    );
    const data = await resp.json();
    if (data.found) {
      // Could be data.tournament (singular) or data.tournaments (array)
      // depending on how the endpoint was called. Handle both.
      if (data.tournament) return data.tournament;
      if (data.tournaments && data.tournaments.length > 0) return data.tournaments[0];
    }
  } catch (e) {
    console.log(`[app] find-tournament by name failed: ${e.message}`);
  }

  // Second try: check the debug endpoint for the active tournament
  // (the active tournament is whichever one the app is currently tracking)
  try {
    const resp = await fetch(
      `${APP_URL}/api/debug`,
      { headers: { "User-Agent": USER_AGENT } }
    );
    const data = await resp.json();
    const active = data.active_tournament;
    if (active) {
      // Check if the names are similar enough to match
      // PGA Tour might say "TOUR Championship" while the app says
      // "TOUR Championship presented by XYZ" or similar
      const pgaName = pgaTournamentName.toLowerCase();
      const appName = (active.name || "").toLowerCase();
      if (appName.includes(pgaName) || pgaName.includes(appName)) {
        return active;
      }
    }
  } catch (e) {
    console.log(`[app] debug endpoint failed: ${e.message}`);
  }

  return null;
}


/**
 * Push scores to the Drinking & Driving app via the /api/push-scores endpoint.
 * This endpoint is authenticated with the APP_SECRET_KEY (must match the
 * FLASK_SECRET_KEY environment variable on Railway).
 */
async function pushScores(tournamentId, scores, secretKey) {
  const resp = await fetch(`${APP_URL}/api/push-scores`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Api-Key": secretKey,
    },
    body: JSON.stringify({
      tournament_id: tournamentId,
      scores: scores,
    }),
  });

  // Railway might return a 502 if the app was sleeping and timed out.
  // Handle non-JSON responses gracefully instead of crashing.
  if (!resp.ok) {
    const text = await resp.text();
    return { status: "error", http_status: resp.status, message: text.slice(0, 200) };
  }

  return await resp.json();
}


// -- Main handler ------------------------------------------------------------

export default {
  /**
   * Cron trigger handler -- runs every 15 minutes.
   *
   * This is the main automation loop:
   * 1. Find the current in-progress PGA tournament
   * 2. Fetch its leaderboard
   * 3. Match it to a tournament in the app
   * 4. Push the scores
   */
  async scheduled(event, env, ctx) {
    console.log(`[cron] triggered at ${new Date().toISOString()}`);

    // Validate that we have the secret key for authenticating with the app
    const secretKey = env.APP_SECRET_KEY;
    if (!secretKey) {
      console.error("[cron] APP_SECRET_KEY not set. Run: npx wrangler secret put APP_SECRET_KEY");
      return;
    }

    // Step 1: Find the current in-progress tournament
    const tournament = await findCurrentTournament();
    if (!tournament) {
      console.log("[cron] No PGA tournament currently in progress");
      return;
    }
    console.log(`[cron] Current tournament: ${tournament.name} (${tournament.id})`);

    // Step 2: Fetch the leaderboard
    const scores = await fetchLeaderboard(tournament.id);
    if (!scores.length) {
      console.log("[cron] No scores returned from PGA Tour API");
      return;
    }
    console.log(`[cron] Got ${scores.length} player scores`);

    // Step 3: Find the matching tournament in the app
    const appTournament = await findTournamentInApp(tournament.name);
    if (!appTournament) {
      console.log(`[cron] Tournament "${tournament.name}" not found in app database`);
      return;
    }
    console.log(`[cron] Matched to app tournament: ${appTournament.name} (${appTournament.id})`);

    // Step 4: Push scores to the app
    const pushResult = await pushScores(appTournament.id, scores, secretKey);
    console.log(`[cron] Push result: updated=${pushResult.updated}, unmatched=${(pushResult.unmatched_players || []).length}`);
  },

  /**
   * HTTP handler -- lets you trigger manually or check status.
   *
   * Routes:
   *   GET /       -- health check (is the worker running?)
   *   GET /push   -- manually trigger a score push (same as cron)
   *   GET /test   -- just fetch PGA scores without pushing (for debugging)
   */
  async fetch(request, env) {
    const url = new URL(request.url);

    // Health check -- confirms the worker is deployed and configured
    if (url.pathname === "/") {
      return new Response(JSON.stringify({
        service: "golf-score-pusher",
        status: "ok",
        source: "pga-tour-graphql",
        app_url: APP_URL,
        has_secret: !!env.APP_SECRET_KEY,
      }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    // Test endpoint -- fetch scores without pushing (for debugging)
    if (url.pathname === "/test") {
      try {
        // Find current tournament
        const tournament = await findCurrentTournament();
        if (!tournament) {
          return jsonResponse({ status: "no tournament in progress" });
        }

        // Fetch leaderboard
        const scores = await fetchLeaderboard(tournament.id);

        return jsonResponse({
          tournament: tournament.name,
          pga_id: tournament.id,
          player_count: scores.length,
          // Show first 10 players as a sample
          sample_scores: scores.slice(0, 10),
        });
      } catch (e) {
        return jsonResponse({ error: e.message, stack: e.stack }, 500);
      }
    }

    // Manual trigger -- does the full fetch + push cycle
    if (url.pathname === "/push") {
      try {
        const secretKey = env.APP_SECRET_KEY;
        if (!secretKey) {
          return jsonResponse({ error: "APP_SECRET_KEY not configured" }, 500);
        }

        // Find current tournament
        const tournament = await findCurrentTournament();
        if (!tournament) {
          return jsonResponse({ status: "no PGA tournament in progress" });
        }

        // Fetch leaderboard
        const scores = await fetchLeaderboard(tournament.id);
        if (!scores.length) {
          return jsonResponse({ status: "no scores from PGA Tour API" });
        }

        // Find in app
        const appTournament = await findTournamentInApp(tournament.name);
        if (!appTournament) {
          return jsonResponse({
            error: "tournament not found in app",
            pga_tournament: tournament.name,
            pga_id: tournament.id,
          });
        }

        // Push scores
        const pushResult = await pushScores(appTournament.id, scores, secretKey);
        return jsonResponse({
          tournament: tournament.name,
          pga_id: tournament.id,
          app_tournament: appTournament.name,
          player_count: scores.length,
          ...pushResult,
        });
      } catch (e) {
        return jsonResponse({ error: "worker crashed", message: e.message, stack: e.stack }, 500);
      }
    }

    return new Response("Not found", { status: 404 });
  },
};


/**
 * Helper to return a JSON response with proper headers.
 * Keeps the route handlers cleaner.
 */
function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
