import os
import json
import random
from google import genai

GAME_DETAILS_PATH = os.path.join("data", "game_details_cache.json")
SEASON_STATS_PATH = os.path.join("data", "season_stats_cache.json")

PREFERRED_MARKETS = [
    "home_win", "draw", "away_win",
    "double_chance_1x", "double_chance_12", "double_chance_x2",
    "over_1_goals", "under_1_goals",
    "over_2_goals", "under_2_goals",
    "over_3_goals", "under_3_goals",
    "btts_yes",
]

###############################################################
# LOAD SEASON STATS ONCE (CACHED)
###############################################################

def load_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return None

SEASON_CACHE = load_json(SEASON_STATS_PATH)


###############################################################
# SAFE NUMBER
###############################################################

def num(x):
    return x if isinstance(x, (int, float)) else -999999


###############################################################
# PRETTY MARKET NAME
###############################################################

def pretty_market_name(market_key):
    mapping = {
        "home_win": "Full-Time Result – Home Win",
        "draw": "Full-Time Result – Draw",
        "away_win": "Full-Time Result – Away Win",
        "double_chance_1x": "Double Chance – 1X",
        "double_chance_12": "Double Chance – 12",
        "double_chance_x2": "Double Chance – X2",
        "over_1_goals": "Over 1.5 Goals",
        "under_1_goals": "Under 1.5 Goals",
        "over_2_goals": "Over 2.5 Goals",
        "under_2_goals": "Under 2.5 Goals",
        "over_3_goals": "Over 3.5 Goals",
        "under_3_goals": "Under 3.5 Goals",
        "btts_yes": "Both Teams To Score – Yes",
    }
    return mapping.get(market_key, market_key)


###############################################################
# LOAD TEAM STATS FROM CACHE
###############################################################

def load_team_stats(season_id, team_id):
    if not SEASON_CACHE:
        return None

    season_data = SEASON_CACHE.get(str(season_id)) or SEASON_CACHE.get(season_id)
    if not season_data:
        return None

    block = season_data.get("data", [])
    if isinstance(block, list):
        for t in block:
            if str(t.get("team_id")) == str(team_id):
                return t

    return None


###############################################################
# EXTRACT ODDS + PROBABILITY
###############################################################

def extract_odds_and_probability(market):
    if not isinstance(market, dict):
        return None, None, None

    prob = market.get("probability")
    implied = market.get("implied_odds")
    book = (
        market.get("actual_odds")
        or market.get("bet365_odds")
        or market.get("latest_odds")
    )

    try:
        prob = float(prob)
    except:
        prob = None

    try:
        implied = float(implied)
    except:
        if prob:
            implied = 100 / prob
        else:
            implied = None

    try:
        book = float(book)
    except:
        book = None

    return prob, implied, book


###############################################################
# VALUE BET CHECK
###############################################################

def is_value_bet(market):
    prob, implied, book = extract_odds_and_probability(market)
    if implied is None or book is None:
        return False, None, prob, implied, book

    if book > implied:
        edge = (book / implied - 1) * 100
        return True, edge, prob, implied, book

    return False, None, prob, implied, book


###############################################################
# BASIC HOME/AWAY STATS
###############################################################

def extract_basic_home_away_stats(team_stats):
    summary = {
        "home_win_pct": None,
        "home_loss_pct": None,
        "home_ppg": None,
        "away_win_pct": None,
        "away_loss_pct": None,
        "away_ppg": None,
        "home_played": None,
        "away_played": None,
        "total_played": None,
    }

    if not isinstance(team_stats, dict):
        return summary

    played = team_stats.get("played", {})
    won = team_stats.get("won", {})
    lost = team_stats.get("lost", {})
    points = team_stats.get("points", {})

    home_p = played.get("home", 0)
    away_p = played.get("away", 0)
    total_p = played.get("total", home_p + away_p)

    summary["home_played"] = home_p
    summary["away_played"] = away_p
    summary["total_played"] = total_p

    if home_p:
        summary["home_win_pct"] = won.get("home", 0) / home_p * 100
        summary["home_loss_pct"] = lost.get("home", 0) / home_p * 100

    if away_p:
        summary["away_win_pct"] = won.get("away", 0) / away_p * 100
        summary["away_loss_pct"] = lost.get("away", 0) / away_p * 100

    summary["home_ppg"] = points.get("home_avg")
    summary["away_ppg"] = points.get("away_avg")

    return summary


###############################################################
# GOAL PROFILE (INCL. HOME/AWAY SPLITS)
###############################################################

def extract_goal_profile(team_stats):
    profile = {
        "avg_goals_total": None,
        "avg_goals_total_home": None,
        "avg_goals_total_away": None,
        "avg_goals_for": None,
        "avg_goals_for_home": None,
        "avg_goals_for_away": None,
        "avg_goals_against": None,
        "avg_goals_against_home": None,
        "avg_goals_against_away": None,
        "over_1_5_pct": None,
        "over_2_5_pct": None,
        "over_3_5_pct": None,
    }

    if not isinstance(team_stats, dict):
        return profile

    total = team_stats.get("goals_total", {})
    gfor = team_stats.get("goals_for", {})
    gagt = team_stats.get("goals_against", {})
    over = team_stats.get("goals_over", {})

    profile["avg_goals_total"] = total.get("total_avg")
    profile["avg_goals_total_home"] = total.get("home_avg")
    profile["avg_goals_total_away"] = total.get("away_avg")

    profile["avg_goals_for"] = gfor.get("total_avg")
    profile["avg_goals_for_home"] = gfor.get("home_avg")
    profile["avg_goals_for_away"] = gfor.get("away_avg")

    profile["avg_goals_against"] = gagt.get("total_avg")
    profile["avg_goals_against_home"] = gagt.get("home_avg")
    profile["avg_goals_against_away"] = gagt.get("away_avg")

    if "o1" in over:
        profile["over_1_5_pct"] = over["o1"].get("total_percentage")
    if "o2" in over:
        profile["over_2_5_pct"] = over["o2"].get("total_percentage")
    if "o3" in over:
        profile["over_3_5_pct"] = over["o3"].get("total_percentage")

    return profile


###############################################################
# STATS PROBABILITY
###############################################################

def compute_stats_probability_for_market(mk, home, away, hg, ag):

    if mk == "home_win":
        return (num(home["home_win_pct"]) + num(away["away_loss_pct"])) / 2

    if mk == "away_win":
        return (num(home["home_loss_pct"]) + num(away["away_win_pct"])) / 2

    keymap = {
        "over_1_goals": "over_1_5_pct",
        "under_1_goals": "over_1_5_pct",
        "over_2_goals": "over_2_5_pct",
        "under_2_goals": "over_2_5_pct",
        "over_3_goals": "over_3_5_pct",
        "under_3_goals": "over_3_5_pct",
    }

    if mk in keymap:
        key = keymap[mk]
        h = num(hg.get(key))
        a = num(ag.get(key))
        if h < 0 or a < 0:
            return None

        if mk.startswith("over"):
            return (h + a) / 2
        else:
            return ((100 - h) + (100 - a)) / 2

    return None


###############################################################
# STRICT SAMPLE SIZE + STATS SUPPORT FILTER
###############################################################

def stats_supports_market(mk, sp, prob, h, a, hg, ag):

    # STRICT SAMPLE SIZE RULES (Option A)
    home_played = h["home_played"]
    away_played = h["away_played"]
    opp_home_played = a["home_played"]
    opp_away_played = a["away_played"]
    total_h = h["total_played"]
    total_a = a["total_played"]

    # HOME / AWAY SPLIT MARKETS REQUIRE 5+ GAMES EACH
    if mk in ["home_win", "away_win", "double_chance_1x", "double_chance_12", "double_chance_x2"]:
        if home_played < 5 or away_played < 5 or opp_home_played < 5 or opp_away_played < 5:
            return False

    # GOALS / BTTS REQUIRE 6+ MATCHES PLAYED TOTAL
    if mk.startswith("over") or mk.startswith("under") or mk == "btts_yes":
        if total_h < 6 or total_a < 6:
            return False

    # USE HOME-AT-HOME + AWAY-AWAY GOALS FOR ENVIRONMENT
    home_total_home = hg["avg_goals_total_home"] if hg["avg_goals_total_home"] is not None else hg["avg_goals_total"]
    away_total_away = ag["avg_goals_total_away"] if ag["avg_goals_total_away"] is not None else ag["avg_goals_total"]

    avg_home_env = num(home_total_home)
    avg_away_env = num(away_total_away)
    combined_goals = (avg_home_env + avg_away_env) / 2

    home_win_pct = num(h["home_win_pct"])
    home_loss_pct = num(h["home_loss_pct"])
    away_win_pct = num(a["away_win_pct"])
    away_loss_pct = num(a["away_loss_pct"])

    # HOME WIN
    if mk == "home_win":
        return home_win_pct >= 50 and away_loss_pct >= 40

    # AWAY WIN
    if mk == "away_win":
        return home_loss_pct >= 40 and away_win_pct >= 40

    # DOUBLE CHANCE
    if mk == "double_chance_1x":
        return home_loss_pct <= 35 and away_win_pct <= 30
    if mk == "double_chance_x2":
        return away_loss_pct <= 35 and home_win_pct <= 30

    # OVER/UNDER rules (still anchored on combined_goals but now based on home/away env)
    if mk == "over_2_goals":
        return num(hg["over_2_5_pct"]) >= 55 and num(ag["over_2_5_pct"]) >= 55 and combined_goals >= 2.5
    if mk == "under_2_goals":
        return num(hg["over_2_5_pct"]) <= 45 and num(ag["over_2_5_pct"]) <= 45 and combined_goals <= 2.4
    if mk == "over_1_goals":
        return num(hg["over_1_5_pct"]) >= 65 and num(ag["over_1_5_pct"]) >= 65 and combined_goals >= 2.0
    if mk == "under_1_goals":
        return num(hg["over_1_5_pct"]) <= 35 and num(ag["over_1_5_pct"]) <= 35 and combined_goals <= 1.8
    if mk == "over_3_goals":
        return combined_goals >= 3.2 and num(hg["over_3_5_pct"]) >= 40
    if mk == "under_3_goals":
        return combined_goals <= 2.8 and num(hg["over_3_5_pct"]) <= 40

    # BTTS – still derived from high combined goal environment
    if mk == "btts_yes":
        return combined_goals >= 2.3

    return False


###############################################################
# MAIN MARKET SELECTION
###############################################################

def load_supported_or_fallback_value_bet():

    data = load_json(GAME_DETAILS_PATH)
    if not data:
        return None

    supported = []
    fallback = []

    for fid, fixture in data.items():
        season = fixture.get("season_id")
        hid = fixture.get("home_id")
        aid = fixture.get("away_id")

        hs = load_team_stats(season, hid)
        as_ = load_team_stats(season, aid)
        if not hs or not as_:
            continue

        home = extract_basic_home_away_stats(hs)
        away = extract_basic_home_away_stats(as_)
        hg = extract_goal_profile(hs)
        ag = extract_goal_profile(as_)

        for mk in PREFERRED_MARKETS:
            m = fixture.get(mk)
            if not isinstance(m, dict):
                continue

            is_val, edge, prob, implied, book = is_value_bet(m)
            if not is_val:
                continue

            stats_prob = compute_stats_probability_for_market(mk, home, away, hg, ag)

            if stats_supports_market(mk, stats_prob, prob, home, away, hg, ag):
                supported.append((fid, fixture, mk, m, edge, prob, implied, book, stats_prob, False))
            else:
                fallback.append((fid, fixture, mk, m, edge, prob, implied, book, stats_prob, True))

    # Supported → random pick
    if supported:
        return random.choice(supported)

    # No supported → fallback ranking
    if not fallback:
        return None

    # Hybrid: prob*0.65 + edge*0.35
    def score(item):
        _, _, _, _, edge, prob, *_ = item
        return (prob * 0.65) + (edge * 0.35)

    fallback.sort(key=score, reverse=True)
    return fallback[0]


###############################################################
# MAIN
###############################################################

def main():

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Missing GEMINI_API_KEY")
        return

    result = load_supported_or_fallback_value_bet()
    if not result:
        print("No value bets found.")
        return

    (
        fid,
        fixture,
        mk,
        market,
        edge,
        prob,
        implied,
        book,
        stats_prob,
        is_fallback
    ) = result

    client = genai.Client(api_key=api_key)

    name = fixture.get("fixture_name", "")
    comp = fixture.get("competition_name", "")
    season = fixture.get("season_id")
    hid = fixture.get("home_id")
    aid = fixture.get("away_id")

    nice = pretty_market_name(mk)

    hs = load_team_stats(season, hid)
    as_ = load_team_stats(season, aid)
    home = extract_basic_home_away_stats(hs)
    away = extract_basic_home_away_stats(as_)
    hg = extract_goal_profile(hs)
    ag = extract_goal_profile(as_)

    # CONFIDENCE RATING
    confidence = (
        "HIGH" if edge > 10 else
        "MEDIUM" if edge > 5 else
        "LOW"
    )

    print("\n=== FINAL VALUE BET (RAW DATA) ===")
    print("Fixture:", name)
    print("Competition:", comp)
    print("Market:", nice)
    print("Fallback:", is_fallback)
    print("Model Prob:", prob)
    print("Stats Prob:", stats_prob)
    print("Edge:", edge)
    print("Confidence:", confidence)
    print("========================\n")

    # ---------------------------------------------------------
    # SHORT, CLEAN RESULT-MARKET PROMPT
    # ---------------------------------------------------------

    prompt = f"""
You are generating a structured and SHORT football betting analysis for Craig.

Your analysis MUST follow strict rules depending on the market type.
You MUST NOT include irrelevant stats.

=============================================================
# 1. Recommended Value Bet (Highest Edge)

| Market | Outcome | Model Probability | Fair Odds | Bookmaker Odds | Value/Edge | Confidence |
|--------|---------|------------------|-----------|----------------|------------|------------|
| {nice} | {mk} | **{prob:.2f}%** | **{implied:.2f}** | **{book:.2f}** | **{edge:.2f}%** | **{confidence}** |

### Write-Up:
Write **2–3 punchy sentences MAX** explaining:
- Why this market is a value bet
- Whether season stats SUPPORT or WEAKEN the bet
- NO fluff, NO filler, NO general football talk

### Key Statistical Context:
Provide **3–5 bullets MAX**, but ONLY USE stats relevant to THIS market type:

=============================================================
RULES FOR EACH MARKET TYPE
=============================================================

### 📌 RESULT MARKETS (home_win, draw, away_win, double chance):
Only use:
- home_win_pct, home_loss_pct, home_ppg
- away_win_pct, away_loss_pct, away_ppg
- goals_for_home_avg, goals_against_home_avg
- goals_for_away_avg, goals_against_away_avg

DO NOT USE:
❌ Over/Under strike rates  
❌ BTTS percentages  
❌ Total averages  

Bullets example tone:
- **Home Strength:** 62% home win rate and 2.10 PPG at home.  
- **Away Weakness:** Opponents average only 0.88 PPG away.  
- **Defensive Edge:** Home concede just 0.9 goals per match at home.

=============================================================

### 📌 OVER/UNDER MARKETS:
Only use:
- The correct goal-line strike rate:
  - Over 1.5: goals_over.o1.home_percentage / away_percentage
  - Over 2.5: goals_over.o2.home_percentage / away_percentage
  - Over 3.5: goals_over.o3.home_percentage / away_percentage
  - Under = 100 - Over %
- goals_total.home_avg
- goals_total.away_avg
- goals_for_home_avg, goals_against_home_avg
- goals_for_away_avg, goals_against_away_avg

DO NOT USE:
❌ Win %, Loss %, Draw %, PPG  
❌ BTTS data  

Bullet examples:
- **High Goal Frequency at Home:** Over 2.5 has landed in 71% of home matches.  
- **Strong Away Contribution:** Over 2.5 hits in 63% of the away team’s away games.  
- **Goal Environment:** Home averages 3.1 goals, away averages 3.4 goals.

=============================================================

### 📌 BTTS YES MARKET:
Only use:
- btts.home_percentage
- btts.away_percentage
- goals_for_home_avg, goals_against_home_avg
- goals_for_away_avg, goals_against_away_avg
- goals_total.home_avg, goals_total.away_avg

DO NOT USE:
❌ PPG  
❌ Win rate  
❌ Loss rate  
❌ Over/Under strike rates  

Bullet examples:
- **BTTS Trend at Home:** BTTS has landed in 60% of home games.  
- **Away Scoring Profile:** They score 1.4 and concede 1.7 per away match.  
- **Goal Environment:** Both sides average 2.8+ goals in their respective splits.

=============================================================

### Final Verdict:
Give Craig a **direct 1-sentence recommendation**, e.g.:
“Craig, this is a strong BTTS pick — stats and pricing are aligned.”

=============================================================

DATA FOR ANALYSIS:

Fixture: {name}
Competition: {comp}
Market: {nice}
Model probability: {prob:.2f}%
Stats probability: {stats_prob}
Fair odds: {implied}
Bookmaker odds: {book}
Value edge: {edge}%
Fallback mode: {is_fallback}

HOME TEAM STATS (home-only):
{home}

HOME GOALS PROFILE:
{hg}

AWAY TEAM STATS (away-only):
{away}

AWAY GOALS PROFILE:
{ag}
"""


    # ---------------------------------------------------------
    # SEND TO GEMINI
    # ---------------------------------------------------------
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=prompt
    )

    print("\n=== GEMINI ANALYSIS (STRUCTURED OUTPUT) ===\n")
    print(response.text)
    print("\n========================\n")


if __name__ == "__main__":
    main()
