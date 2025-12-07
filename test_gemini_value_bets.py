import json, os, random, math
from datetime import datetime
import google.generativeai as genai

# ==========================
# CONFIG
# ==========================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)
MODEL = genai.GenerativeModel("gemini-1.5-flash")

GAME_DETAILS_PATH = "game_details_cache.json"
SEASON_STATS_PATH = "season_stats_cache.json"

SUPPORTED_MARKETS = {
    "home_win": "Full-Time Result – Home Win",
    "draw": "Full-Time Result – Draw",
    "away_win": "Full-Time Result – Away Win",
    "over_1_5": "Over 1.5 Goals",
    "over_2_5": "Over 2.5 Goals",
    "over_3_5": "Over 3.5 Goals",
    "under_1_5": "Under 1.5 Goals",
    "under_2_5": "Under 2.5 Goals",
    "under_3_5": "Under 3.5 Goals",
}

# ==========================
# LOAD CACHES
# ==========================
def load_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def load_game_details():
    data = load_json(GAME_DETAILS_PATH)
    return data.get("data", {})

def load_season_stats():
    data = load_json(SEASON_STATS_PATH)
    return data.get("data", [])

# ==========================
# FIND STATS FOR TEAMS
# ==========================
def find_team_stats(season_id, team_id, season_stats):
    for entry in season_stats:
        if entry.get("season_id") == season_id and entry.get("team_id") == team_id:
            return entry
    return None

# ==========================
# EXTRACT HOME/AWAY SUMMARIES
# ==========================
def extract_home_summary(stats):
    hp = stats["played"]["home"]
    wins = stats["won"]["home"]
    losses = stats["lost"]["home"]
    ppg = stats["points"]["home_avg"]
    return {
        "home_played": hp,
        "home_wins": wins,
        "home_win_pct": (wins / hp) * 100 if hp > 0 else None,
        "home_loss_pct": (losses / hp) * 100 if hp > 0 else None,
        "home_ppg": ppg,
    }

def extract_away_summary(stats):
    ap = stats["played"]["away"]
    wins = stats["won"]["away"]
    losses = stats["lost"]["away"]
    ppg = stats["points"]["away_avg"]
    return {
        "away_played": ap,
        "away_wins": wins,
        "away_win_pct": (wins / ap) * 100 if ap > 0 else None,
        "away_loss_pct": (losses / ap) * 100 if ap > 0 else None,
        "away_ppg": ppg,
    }

# ==========================
# GOALS PROFILE (HOME/AWAY)
# ==========================
def extract_goals_profile(stats, is_home=True):
    side = "home" if is_home else "away"
    total_avg = stats["goals_total"][f"{side}_avg"]
    gf_avg = stats["goals_for"][f"{side}_avg"]
    ga_avg = stats["goals_against"][f"{side}_avg"]

    played = stats["played"][side]
    over15 = stats["goals_over"]["o1"][side] if played > 0 else None
    over25 = stats["goals_over"]["o2"][side] if played > 0 else None
    over35 = stats["goals_over"]["o3"][side] if played > 0 else None

    pct15 = (over15 / played) * 100 if played > 0 else None
    pct25 = (over25 / played) * 100 if played > 0 else None
    pct35 = (over35 / played) * 100 if played > 0 else None

    return {
        "avg_goals_total": total_avg,
        "avg_goals_for": gf_avg,
        "avg_goals_against": ga_avg,
        "over_1_5_pct": pct15,
        "over_2_5_pct": pct25,
        "over_3_5_pct": pct35,
    }

# ==========================
# STAT SUPPORT CHECKS
# ==========================
def stats_supports_market(key, stats_prob, model_prob, home_summary, away_summary, home_goals, away_goals):

    # strict sample size requirement
    if home_summary["home_played"] < 5 or away_summary["away_played"] < 5:
        return False

    if key == "home_win":
        if home_summary["home_win_pct"] is None or away_summary["away_loss_pct"] is None:
            return False
        score = (
            home_summary["home_win_pct"] * 0.6 +
            home_summary["home_ppg"] * 10 +
            away_summary["away_loss_pct"] * 0.4
        )
        return score >= 100

    if key == "away_win":
        if away_summary["away_win_pct"] is None or home_summary["home_loss_pct"] is None:
            return False
        score = (
            away_summary["away_win_pct"] * 0.6 +
            away_summary["away_ppg"] * 10 +
            home_summary["home_loss_pct"] * 0.4
        )
        return score >= 100

    if key == "draw":
        diff = abs(home_summary["home_ppg"] - away_summary["away_ppg"])
        return diff <= 0.4

    if key in ("over_1_5", "over_2_5", "over_3_5"):
        threshold = {"over_1_5": 65, "over_2_5": 55, "over_3_5": 40}[key]
        pct_home = home_goals[f"over_{key[-3:]}_pct"]
        pct_away = away_goals[f"over_{key[-3:]}_pct"]
        if pct_home is None or pct_away is None:
            return False
        return (pct_home + pct_away) / 2 >= threshold

    if key.startswith("under_"):
        threshold = {"under_1_5": 40, "under_2_5": 50, "under_3_5": 60}[key]
        over_home = home_goals[f"over_{key[-3:]}_pct"]
        over_away = away_goals[f"over_{key[-3:]}_pct"]
        if over_home is None or over_away is None:
            return False
        under_combined = 100 - ((over_home + over_away) / 2)
        return under_combined >= threshold

    return False

# ==========================
# CONFIDENCE RATING
# ==========================
def confidence_rating(edge, agreement):
    if edge > 12 and agreement:
        return "HIGH"
    if edge > 6:
        return "MEDIUM"
    return "LOW"

# ==========================
# FORMAT MARKET RESULT SUMMARY
# ==========================
def build_market_markdown_selection(selection):
    return f"""
# 1. Recommended Value Bet (Highest Edge)

| Market | Outcome | Model Probability | Fair Odds | Bookmaker Odds | Value/Edge | Confidence |
|--------|---------|------------------|-----------|----------------|------------|------------|
| {selection['market_name']} | {selection['market_key']} | **{selection['model_prob']:.2f}%** | **{selection['fair_odds']:.2f}** | **{selection['bookmaker_odds']:.2f}** | **{selection['edge']:.2f}%** | **{selection['confidence']}** |

### Write-Up
{selection['write_up']}

### Key Statistical Context
- **Home strength:** {selection['stat_home']}
- **Away strength:** {selection['stat_away']}
- **Goals environment:** {selection['stat_goals']}
"""

# ==========================
# GEMINI WRITE-UP GENERATOR
# ==========================
def generate_gemini_writeup(fixture_name, comp, mk, model_prob, stats_prob, stat_home, stat_away, stat_goals, bookmaker_odds, fair_odds):
    prompt = f"""
Produce a structured betting write-up like the format used in this example:

1. Start with a clear recommendation sentence.
2. Then give a short paragraph analysis.
3. Then provide bullet points for:
   - Home strength
   - Away strength
   - Goals environment

Use these inputs:

Fixture: {fixture_name}
Competition: {comp}
Market: {mk}
Model probability: {model_prob:.2f}%
Stats probability: {stats_prob:.2f}%
Fair odds: {fair_odds:.2f}
Bookmaker odds: {bookmaker_odds}
Home stats: {stat_home}
Away stats: {stat_away}
Goals stats: {stat_goals}

Do NOT mention that you are an AI model. Keep tone confident and practical.
"""
    out = MODEL.generate_content(prompt)
    return out.text.strip()

# ==========================
# MAIN VALUE BET SELECTION
# ==========================
def main():

    game_data = load_game_details()
    season_stats = load_season_stats()

    candidates = []

    for fix_id, fix in game_data.items():
        season_id = fix.get("season_id")
        home_id = fix.get("home_id")
        away_id = fix.get("away_id")

        home_stats = find_team_stats(season_id, home_id, season_stats)
        away_stats = find_team_stats(season_id, away_id, season_stats)
        if not home_stats or not away_stats:
            continue

        home_summary = extract_home_summary(home_stats)
        away_summary = extract_away_summary(away_stats)
        home_goals = extract_goals_profile(home_stats, True)
        away_goals = extract_goals_profile(away_stats, False)

        for key, name in SUPPORTED_MARKETS.items():
            if key not in fix:
                continue

            m = fix[key]
            if not m:
                continue

            model_prob = m.get("probability")
            bookmaker_odds = m.get("actual_odds")
            if not model_prob or not bookmaker_odds:
                continue

            fair_odds = 100 / model_prob
            edge = ((bookmaker_odds - fair_odds) / fair_odds) * 100

            stats_prob = model_prob
            agree = stats_supports_market(key, stats_prob, model_prob, home_summary, away_summary, home_goals, away_goals)
            if not agree:
                continue

            stat_home = f"{home_summary['home_win_pct']:.1f}% win rate, {home_summary['home_ppg']:.2f} PPG at home"
            stat_away = f"{away_summary['away_win_pct']:.1f}% away win rate, {away_summary['away_ppg']:.2f} PPG away"
            stat_goals = f"Home avg goals: {home_goals['avg_goals_total']:.2f}, Away avg goals: {away_goals['avg_goals_total']:.2f}"

            candidates.append({
                "fixture": fix.get("fixture_name"),
                "competition": fix.get("competition_name"),
                "market_key": key,
                "market_name": name,
                "model_prob": model_prob,
                "stats_prob": stats_prob,
                "fair_odds": fair_odds,
                "bookmaker_odds": bookmaker_odds,
                "edge": edge,
                "conf": agree,
                "stat_home": stat_home,
                "stat_away": stat_away,
                "stat_goals": stat_goals,
                "fixture_id": fix_id
            })

    if not candidates:
        print("No value bets found.")
        return

    best = max(candidates, key=lambda x: x["edge"])

    best["confidence"] = confidence_rating(best["edge"], True)

    best["write_up"] = generate_gemini_writeup(
        best["fixture"],
        best["competition"],
        best["market_name"],
        best["model_prob"],
        best["stats_prob"],
        best["stat_home"],
        best["stat_away"],
        best["stat_goals"],
        best["bookmaker_odds"],
        best["fair_odds"]
    )

    print(build_market_markdown_selection(best))


if __name__ == "__main__":
    main()
