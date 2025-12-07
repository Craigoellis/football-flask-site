import os
from datetime import datetime

from flask import Flask, render_template, jsonify
from google import genai

# import functions from your existing script
from test_gemini_first_fixture_with_stats import (
    load_supported_or_fallback_value_bet,
    pretty_market_name,
    load_team_stats,
    extract_basic_home_away_stats,
    extract_goal_profile,
)

app = Flask(__name__)


# ----------------- helper functions -----------------


def ordinal(n: int) -> str:
    """Return 1 -> '1st', 2 -> '2nd', etc."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def format_kickoff_label(fixture: dict) -> str:
    """
    Format the unix timestamp from the fixture:
      - If the fixture is today: '3:00pm'
      - Otherwise: 'Sat 6th Dec 3:00pm'
    """
    ts = fixture.get("kickoff_unix") or fixture.get("unix") or fixture.get("fixture_unix")
    if not ts:
        return ""

    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""

    dt = datetime.fromtimestamp(ts)
    now = datetime.now()

    time_str = dt.strftime("%I:%M%p").lstrip("0").lower()  # 3:00pm

    if dt.date() == now.date():
        return time_str

    day_with_suffix = ordinal(dt.day)
    return f"{dt.strftime('%a')} {day_with_suffix} {dt.strftime('%b')} {time_str}"


def strip_code_fences(text: str) -> str:
    """
    Gemini sometimes returns ```html ... ``` – strip those so we only have pure HTML.
    """
    if not text:
        return ""

    s = text.strip()
    for token in ("```html", "```HTML", "```"):
        if s.startswith(token):
            s = s[len(token):].strip()
    # strip trailing ```
    if s.endswith("```"):
        s = s[:-3].strip()
    return s


# ----------------- routes -----------------


@app.route("/")
def index():
    # AI Bets HTML page
    return render_template("ai_bets.html")


@app.route("/api/generate")
def api_generate():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return jsonify({"error": "Missing GEMINI_API_KEY"}), 500

    # Select the value bet
    result = load_supported_or_fallback_value_bet()
    if not result:
        return jsonify({"error": "No value bets found."}), 404

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
        is_fallback,
    ) = result

    # Extract display data
    name = fixture.get("fixture_name", "")

    # Country + competition
    comp = fixture.get("competition_name", "")
    country = (
        fixture.get("competition_country")
        or fixture.get("country")
        or fixture.get("country_name")
        or ""
    )
    if country and comp:
        competition_full = f"{country} - {comp}"
    else:
        competition_full = comp or country

    kickoff_label = format_kickoff_label(fixture)

    season = fixture.get("season_id")
    hid = fixture.get("home_id")
    aid = fixture.get("away_id")
    nice = pretty_market_name(mk)

    # Load stats for Gemini
    hs = load_team_stats(season, hid)
    as_ = load_team_stats(season, aid)

    home = extract_basic_home_away_stats(hs)
    away = extract_basic_home_away_stats(as_)
    hg = extract_goal_profile(hs)
    ag = extract_goal_profile(as_)

    # Confidence rating
    if edge is None:
        confidence = "LOW"
    else:
        confidence = "HIGH" if edge > 10 else "MEDIUM" if edge > 5 else "LOW"

    # Gemini client
    client = genai.Client(api_key=api_key)

    # ---------- HTML PROMPT ----------

    prompt = f"""
You are generating a professional HTML betting analysis for Craig.

IMPORTANT OUTPUT RULES (MUST FOLLOW EXACTLY):
- Output valid HTML only.
- The HTML must include:
  1) A title line: "<h2>Recommended Value Bet (Highest Edge)</h2>"
  2) A professional HTML table (NOT markdown) with these columns:
       Market | Model Probability | Fair Odds | Bookmaker Odds | Value/Edge | Confidence
  3) A short paragraph (2–3 sentences) explaining why this is a value bet AND whether season stats support it.
  4) A "Key Statistical Context" section using a <ul> with 3–5 bullet points.
  5) A final sentence starting with "Verdict:".

- Do NOT output markdown headings (#, ##, etc).
- Do NOT output markdown tables (no pipe '|' characters).
- Do NOT output code fences or backticks of any kind.
- The table must use <table>, <thead>, <tbody>, <tr>, <th>, <td>.
- ALL numeric fields in the table must use the exact values given below.
- Percentages must include a % sign.

TABLE VALUES (MUST USE THESE EXACT NUMBERS):
- Market: {nice}
- Model Probability: {prob:.2f}% (if available)
- Fair Odds: {implied:.2f} (if available)
- Bookmaker Odds: {book:.2f} (if available)
- Value/Edge: {edge:.2f}% (if available)
- Confidence: {confidence}

ANALYSIS RULES BY MARKET TYPE:

1) OVER/UNDER GOALS MARKETS
   (over_1_goals, under_1_goals, over_2_goals, under_2_goals, over_3_goals, under_3_goals)
   Use ONLY goal-based stats such as:
   - Over line strike rates from goals_over["oX"]["home_percentage"] and ["away_percentage"].
   - goals_total.home_avg and goals_total.away_avg.
   - goals_for.home_avg, goals_against.home_avg.
   - goals_for.away_avg, goals_against.away_avg.
   Do NOT reference points per game or win percentages.
   Mention how often the relevant line has landed for the home team at HOME
   and the away team AWAY.

2) BTTS (btts_yes)
   Use ONLY:
   - home_stats["btts"]["home_percentage"].
   - away_stats["btts"]["away_percentage"].
   - goals_for.home_avg, goals_against.home_avg.
   - goals_for.away_avg, goals_against.away_avg.
   Do NOT mention PPG or win percentages.

3) RESULT MARKETS
   (home_win, draw, away_win, double_chance_1x, double_chance_12, double_chance_x2)
   Use:
   - home_win_pct, home_loss_pct, home_ppg.
   - away_win_pct, away_loss_pct, away_ppg.
   - goals_for.home_avg, goals_against.home_avg.
   - goals_for.away_avg, goals_against.away_avg.
   Emphasise home-at-home vs away-at-away performance.

STRUCTURE YOU MUST FOLLOW:

<h2>Recommended Value Bet (Highest Edge)</h2>

<table class="ai-table">
  <thead>
    <tr>
      <th>Market</th>
      <th>Model Probability</th>
      <th>Fair Odds</th>
      <th>Bookmaker Odds</th>
      <th>Value/Edge</th>
      <th>Confidence</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>{nice}</td>
      <td>{prob:.2f}%</td>
      <td>{implied:.2f}</td>
      <td>{book:.2f}</td>
      <td>{edge:.2f}%</td>
      <td>{confidence}</td>
    </tr>
  </tbody>
</table>

<p>Write 2–3 sentences explaining why this is a value bet and how the season stats support or caution against it.</p>

<ul>
  <li>Bullet 1: key stat, appropriate to the market type.</li>
  <li>Bullet 2.</li>
  <li>Bullet 3.</li>
  <li>Optional bullet 4 if helpful.</li>
</ul>

<p><strong>Verdict:</strong> Short recommendation to Craig.</p>

DATA FOR ANALYSIS:

Fixture: {name}
Competition: {competition_full}
Kick-off (already formatted for Craig): {kickoff_label}
Raw market key: {mk}

Model probability: {prob:.2f}%
Stats probability: {stats_prob}
Fair odds: {implied}
Bookmaker odds: {book}
Value edge: {edge}%
Fallback mode: {is_fallback}

RAW HOME SEASON STATS:
{hs}

RAW AWAY SEASON STATS:
{as_}

Home win/loss/PPG summary:
{home}

Home goals profile:
{hg}

Away win/loss/PPG summary:
{away}

Away goals profile:
{ag}
"""

    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=prompt
    )

    html_output = strip_code_fences(response.text or "")

    return jsonify({
        "html": html_output,
        "fixture": name,
        "competition": competition_full,
        "kickoff": kickoff_label,
        "market_name": nice,
        "model_prob": round(prob, 2) if prob is not None else None,
        "stats_prob": round(stats_prob, 2) if stats_prob is not None else None,
        "fair_odds": round(implied, 2) if implied is not None else None,
        "bookmaker_odds": round(book, 2) if book is not None else None,
        "edge": round(edge, 2) if edge is not None else None,
        "confidence": confidence,
    })


if __name__ == "__main__":
    app.run(debug=True, port=5001)
