#!/usr/bin/env python3
"""
update.py — Pool Bérubé Roy data updater
Fetches NHL game scores + player stats and updates all JSON files.

Sources:
  - Live games : ESPN scoreboard JSON API
  - Skater stats : ESPN stats page (playerStats embedded JSON)
  - Goalie stats : NHL API (no CORS when called server-side)

Usage:
  python update.py             # full update (games + all stats)
  python update.py --live-only # only refresh json/live.games.json (fast)

Dependencies: pip install requests
"""

import json
import os
import re
import sys
import time
import unicodedata

try:
    import requests
except ImportError:
    print("Missing dependency. Run: pip install requests")
    sys.exit(1)

BASE = os.path.dirname(os.path.abspath(__file__))
JSON_DIR = os.path.join(BASE, "json")

# ESPN season: year the season ENDS  (2025 = 2024-25 season)
ESPN_SEASON = 2025
# NHL API season code matching ESPN_SEASON above
NHL_SEASON = "20242025"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# Scoring: 1 pt/skater goal, 1 pt/skater assist, 1 pt/goalie win, 2 pt/shutout
GOALIE_WIN_PTS = 1
GOALIE_SO_PTS = 2


# ─── helpers ────────────────────────────────────────────────────────────────

def norm(name):
    """Lowercase + strip accents for fuzzy name matching."""
    s = unicodedata.normalize("NFD", str(name))
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower().strip()


def norm_short(name):
    """First-initial + last-name key, e.g. 'Sam Montembeault' → 's.montembeault'."""
    parts = norm(name).split()
    if len(parts) >= 2:
        return parts[0][0] + "." + " ".join(parts[1:])
    return norm(name)


def lookup(stats_dict, player_name):
    """Find a player in a stats dict; try full name first, then first-initial fallback."""
    key = norm(player_name)
    if key in stats_dict:
        return stats_dict[key]
    short = norm_short(player_name)
    for v in stats_dict.values():
        if norm_short(v.get("raw_name", "")) == short:
            return v
    return None


def load_json(filename):
    with open(os.path.join(JSON_DIR, filename), encoding="utf-8") as f:
        return json.load(f)


def save_json(filename, data):
    path = os.path.join(JSON_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"  Saved {filename}")


def safe_int(val, default=0):
    try:
        return int(str(val).replace("--", "0").strip() or "0")
    except (ValueError, TypeError):
        return default


def _extract_array(text, key):
    """
    Extract a JSON array value for a given key from raw JS/JSON text.
    Uses bracket counting to handle nested structures robustly.
    """
    marker = f'"{key}":'
    idx = text.find(marker)
    if idx < 0:
        return None
    start = idx + len(marker)
    # Skip whitespace
    while start < len(text) and text[start] in " \t\n\r":
        start += 1
    if start >= len(text) or text[start] != "[":
        return None
    depth, end = 0, start
    for i, ch in enumerate(text[start:], start):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError:
        return None


# ─── Part 1: live games ─────────────────────────────────────────────────────

def fetch_live_games():
    """Fetch tonight's NHL games from ESPN scoreboard API → json/live.games.json."""
    print("Fetching live games from ESPN scoreboard...")
    url = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    espn = r.json()

    games = []
    for event in espn.get("events", []):
        comp = event.get("competitions", [{}])[0]
        competitors = comp.get("competitors", [])
        status = comp.get("status", {})
        state = status.get("type", {}).get("state", "pre")  # pre / in / post

        away = next((c for c in competitors if c.get("homeAway") == "away"), {})
        home = next((c for c in competitors if c.get("homeAway") == "home"), {})

        def abbrev(c):
            return c.get("team", {}).get("abbreviation") or c.get("abbreviation", "?")

        game_state = {"pre": "PRE", "in": "LIVE", "post": "FINAL"}.get(state, "PRE")

        games.append({
            "awayTeam": {
                "abbrev": abbrev(away),
                "score": safe_int(away.get("score", 0)),
            },
            "homeTeam": {
                "abbrev": abbrev(home),
                "score": safe_int(home.get("score", 0)),
            },
            "gameState": game_state,
            "clock": {"timeRemaining": status.get("displayClock", "")},
            "period": status.get("period", 0),
            "startTimeUTC": comp.get("date") or event.get("date", ""),
        })

    save_json("live.games.json", {"games": games})
    print(f"  {len(games)} game(s) found")


# ─── Part 2: skater stats from ESPN ─────────────────────────────────────────

def _parse_espn_page(html):
    """
    Extract playerStats from an ESPN stats page.
    ESPN embeds data as:
      window['__CONFIG__'] = { ... "playerStats": [{athlete:{...}, stats:[...]}, ...] ... }
    Each entry: athlete.name + stats array with {name, value} objects.
    Returns list of {name, goals, assists, points} dicts.
    """
    # The big script tag contains window['__CONFIG__'] with playerStats inside
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
    for script in scripts:
        if "playerStats" not in script:
            continue
        rows = _extract_array(script, "playerStats")
        if not rows:
            continue
        results = []
        for entry in rows:
            athlete = entry.get("athlete", {})
            name = athlete.get("name") or athlete.get("displayName", "")
            if not name:
                continue
            stats = {s["name"]: s["value"] for s in entry.get("stats", []) if "name" in s}
            results.append({
                "name": name,
                "goals": safe_int(stats.get("goals", 0)),
                "assists": safe_int(stats.get("assists", 0)),
                "points": safe_int(stats.get("points", 0)),
            })
        if results:
            return results
    return []


def fetch_espn_skater_stats():
    """
    Scrape skater season stats from ESPN stats page (all pages).
    Returns dict: norm(name) → {goals, assists, points, raw_name}
    """
    print(f"Fetching skater stats from ESPN (season {ESPN_SEASON})...")
    stats = {}

    for page in range(1, 30):
        url = (
            f"https://www.espn.com/nhl/stats/player/_/season/{ESPN_SEASON}"
            f"/seasontype/2/page/{page}"
        )
        r = requests.get(url, headers=HEADERS, timeout=20)
        players = _parse_espn_page(r.text)

        if not players:
            print(f"  Page {page}: no data — stopping")
            break

        for p in players:
            stats[norm(p["name"])] = {
                "goals": p["goals"],
                "assists": p["assists"],
                "points": p["points"],
                "raw_name": p["name"],
            }

        print(f"  Page {page}: {len(players)} skaters")
        if len(players) < 50:
            break  # last page
        time.sleep(0.4)

    print(f"  Total skaters fetched: {len(stats)}")
    return stats


# ─── Part 3: goalie stats from NHL API (server-side, no CORS) ───────────────

def fetch_nhl_goalie_stats():
    """
    Fetch goalie wins + shutouts from the NHL stats API.
    Returns dict: norm(name) → {wins, shutouts, raw_name}
    """
    print(f"Fetching goalie stats from NHL API (season {NHL_SEASON})...")
    url = (
        f"https://api-web.nhle.com/v1/goalie-stats-leaders/{NHL_SEASON}/2"
        f"?categories=wins,shutouts&limit=100"
    )
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()

    stats = {}

    # Build wins lookup by player ID
    wins_by_id = {}
    for g in data.get("wins", []):
        wins_by_id[g["id"]] = safe_int(g["value"])
        name = g["firstName"]["default"] + " " + g["lastName"]["default"]
        stats[norm(name)] = {"wins": safe_int(g["value"]), "shutouts": 0, "raw_name": name}

    # Merge shutouts
    for g in data.get("shutouts", []):
        name = g["firstName"]["default"] + " " + g["lastName"]["default"]
        key = norm(name)
        if key in stats:
            stats[key]["shutouts"] = safe_int(g["value"])
        else:
            stats[key] = {"wins": 0, "shutouts": safe_int(g["value"]), "raw_name": name}

    print(f"  Total goalies fetched: {len(stats)}")
    return stats


# ─── Part 4: update teams.json ───────────────────────────────────────────────

def update_teams(skater_stats, goalie_stats):
    """Update player stats in teams.json and recalculate team totals."""
    print("Updating teams.json...")
    data = load_json("teams.json")
    not_found = []

    for team in data.get("teams", []):
        team_goals = team_passes = team_wins = team_shutouts = 0

        # teams.json stores all skaters (forwards + defensemen) in 'forwards'
        for player in team.get("forwards", []) + team.get("defenses", []):
            s = lookup(skater_stats, player["fullName"])
            if s:
                player["diffGoals"] = s["goals"] - player.get("goals", 0)
                player["diffPasses"] = s["assists"] - player.get("passes", 0)
                player["diffPoints"] = s["points"] - player.get("points", 0)
                player["goals"] = s["goals"]
                player["passes"] = s["assists"]
                player["points"] = s["points"]
            else:
                not_found.append(player["fullName"])
            team_goals += player.get("goals", 0)
            team_passes += player.get("passes", 0)

        for goalie in team.get("goalies", []):
            g = lookup(goalie_stats, goalie["fullName"])
            if g:
                new_pts = g["wins"] * GOALIE_WIN_PTS + g["shutouts"] * GOALIE_SO_PTS
                goalie["diffWins"] = g["wins"] - goalie.get("wins", 0)
                goalie["diffShutOuts"] = g["shutouts"] - goalie.get("shutOuts", 0)
                goalie["diffPoints"] = new_pts - goalie.get("points", 0)
                goalie["wins"] = g["wins"]
                goalie["shutOuts"] = g["shutouts"]
                goalie["points"] = new_pts
            else:
                not_found.append(goalie["fullName"])
            team_wins += goalie.get("wins", 0)
            team_shutouts += goalie.get("shutOuts", 0)

        old_goals = team.get("goals", 0)
        old_passes = team.get("passes", 0)
        old_pts = team.get("points", 0)
        new_pts = (
            team_goals + team_passes
            + team_wins * GOALIE_WIN_PTS
            + team_shutouts * GOALIE_SO_PTS
        )
        team["diffGoals"] = team_goals - old_goals
        team["diffPasses"] = team_passes - old_passes
        team["diffPoints"] = new_pts - old_pts
        team["goals"] = team_goals
        team["passes"] = team_passes
        team["wins"] = team_wins
        team["shutOuts"] = team_shutouts
        team["points"] = new_pts

    if not_found:
        unique = sorted(set(not_found))
        print(f"  {len(unique)} pool player(s) not found — stats unchanged:")
        for name in unique:
            print(f"    - {name}")

    save_json("teams.json", data)
    return data


# ─── Part 5: update best.*.json ─────────────────────────────────────────────

def update_best_skaters(skater_stats, filename):
    """Update + re-sort the pool skater leaderboard in a best.*.json file."""
    print(f"Updating {filename}...")
    data = load_json(filename)
    players = data.get("skaters", [])
    not_found = []

    for p in players:
        s = lookup(skater_stats, p["fullName"])
        if s:
            p["diffGoals"] = s["goals"] - p.get("goals", 0)
            p["diffPasses"] = s["assists"] - p.get("passes", 0)
            p["diffPoints"] = s["points"] - p.get("points", 0)
            p["goals"] = s["goals"]
            p["passes"] = s["assists"]
            p["points"] = s["points"]
        else:
            not_found.append(p["fullName"])

    players.sort(key=lambda x: (-x.get("points", 0), -x.get("goals", 0), x["fullName"]))
    for i, p in enumerate(players):
        p["order"] = i + 1
        p["isBest"] = i == 0
        p["isWorst"] = i == len(players) - 1

    if not_found:
        print(f"  Not found in ESPN: {', '.join(not_found)}")

    save_json(filename, {"skaters": players})


def update_best_goalies(goalie_stats):
    """Update + re-sort the pool goalie leaderboard in best.goalies.json."""
    print("Updating best.goalies.json...")
    data = load_json("best.goalies.json")
    goalies = data.get("goalies", [])
    not_found = []

    for g in goalies:
        s = lookup(goalie_stats, g["fullName"])
        if s:
            new_pts = s["wins"] * GOALIE_WIN_PTS + s["shutouts"] * GOALIE_SO_PTS
            g["diffWins"] = s["wins"] - g.get("wins", 0)
            g["diffShutOuts"] = s["shutouts"] - g.get("shutOuts", 0)
            g["diffPoints"] = new_pts - g.get("points", 0)
            g["wins"] = s["wins"]
            g["shutOuts"] = s["shutouts"]
            g["points"] = new_pts
        else:
            not_found.append(g["fullName"])

    goalies.sort(key=lambda x: (-x.get("points", 0), -x.get("wins", 0), x["fullName"]))
    for i, g in enumerate(goalies):
        g["order"] = i + 1
        g["isBest"] = i == 0
        g["isWorst"] = i == len(goalies) - 1

    if not_found:
        print(f"  Not found in NHL API: {', '.join(not_found)}")

    save_json("best.goalies.json", {"goalies": goalies})


# ─── Part 6: recalculate leaders.json ───────────────────────────────────────

def recalculate_leaders(teams_data):
    """Rebuild leaders.json standings from updated team totals."""
    print("Recalculating leaders.json...")
    old = load_json("leaders.json")
    old_by_name = {l.get("teamUserName"): l for l in old.get("leaders", [])}

    leaders = []
    for team in teams_data.get("teams", []):
        pooler = team.get("poolerName", "")
        old_entry = old_by_name.get(pooler, {})
        goals = team.get("goals", 0)
        passes = team.get("passes", 0)
        wins = team.get("wins", 0)
        shutouts = team.get("shutOuts", 0)
        pts = goals + passes + wins * GOALIE_WIN_PTS + shutouts * GOALIE_SO_PTS

        leaders.append({
            "teamName": team.get("name", ""),
            "teamUserName": pooler,
            "userLogo": team.get("userLogo", ""),
            "bigLogo": team.get("bigLogo", ""),
            "smallLogo": team.get("smallLogo", ""),
            "goals": goals,
            "passes": passes,
            "goaliesWins": wins,
            "goaliesShutOuts": shutouts,
            "points": pts,
            "goalsDiff": goals - old_entry.get("goals", goals),
            "passesDiff": passes - old_entry.get("passes", passes),
            "goaliesWinDiff": wins - old_entry.get("goaliesWins", wins),
            "goaliesShutOutsDiff": shutouts - old_entry.get("goaliesShutOuts", shutouts),
            "pointsDiff": pts - old_entry.get("points", pts),
            "playForCash": team.get("playForCash", False),
            "hasPaid": team.get("hasPaid", False),
        })

    leaders.sort(key=lambda x: (-x["points"], -x["goals"]))

    old_pos = {l.get("teamUserName"): l.get("pos", 0) for l in old.get("leaders", [])}
    for i, l in enumerate(leaders):
        l["pos"] = i + 1
        l["diffPosition"] = old_pos.get(l["teamUserName"], i + 1) - (i + 1)

    top = leaders[0] if leaders else {}
    low = leaders[-1] if leaders else {}

    result = {
        "topTotalScore": top.get("points", 0),
        "lowTotalScore": low.get("points", 0),
        "topScoreYesterday": old.get("topScoreYesterday", 0),
        "lowScoreYesterday": old.get("lowScoreYesterday", 0),
        "leaders": leaders,
        # Historical award windows need snapshot logs — preserve as-is
        "bestLast7Days": old.get("bestLast7Days", {}),
        "bestLast28Days": old.get("bestLast28Days", {}),
        "bestLast14Days": old.get("bestLast14Days", {}),
        "bestYesterday": old.get("bestYesterday", {}),
        "bestTonight": old.get("bestTonight", {}),
        "worstLast7Days": old.get("worstLast7Days", {}),
        "worstLast28Days": old.get("worstLast28Days", {}),
        "worstLast14Days": old.get("worstLast14Days", {}),
        "worstYesterday": old.get("worstYesterday", {}),
    }

    save_json("leaders.json", result)


# ─── main ────────────────────────────────────────────────────────────────────

def main():
    live_only = "--live-only" in sys.argv

    fetch_live_games()

    if live_only:
        print("Done (live-only mode).")
        return

    skater_stats = fetch_espn_skater_stats()
    if not skater_stats:
        print(
            "\nERROR: Could not fetch skater stats from ESPN.\n"
            "Live games JSON was still updated."
        )
        sys.exit(1)

    goalie_stats = fetch_nhl_goalie_stats()

    teams_data = update_teams(skater_stats, goalie_stats)
    update_best_skaters(skater_stats, "best.forwards.json")
    update_best_skaters(skater_stats, "best.defenses.json")
    update_best_goalies(goalie_stats)
    recalculate_leaders(teams_data)

    print("\nAll done!")


if __name__ == "__main__":
    main()
