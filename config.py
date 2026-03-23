# config.py — change these to adjust league rules

LEAGUE_NAME = "Drinking & Driving"
NUM_TEAMS = 3
ROSTER_SIZE = 15       # players per team pool
STARTERS_PER_WEEK = 5  # active starters per tournament

# Scoring
MAJORS_MULTIPLIER = 1.5   # score weight for major tournaments (Masters, US Open, The Open, PGA Championship)
LOWER_IS_BETTER = True    # stroke play — lower score wins

# Data
SCORE_REFRESH_MINUTES = 15  # how often to pull live scores during a tournament

# Majors list (used to apply multiplier)
MAJORS = [
    "Masters Tournament",
    "US Open",
    "The Open Championship",
    "PGA Championship",
]
