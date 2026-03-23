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
