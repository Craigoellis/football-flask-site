import time
import requests
import json
import os
import pytz
import random
import csv
import io
import smtplib
import joblib
import numpy as np
import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from filters.value_bets_strategies import VALUE_BET_STRATEGIES
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from google import genai
from zoneinfo import ZoneInfo
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders

print(f"Flask process PID: {os.getpid()}")

# ----------------------------------------------------
# Base directory + DATA_DIR (SINGLE SOURCE OF TRUTH)
# ----------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 1) Explicit override always wins (optional, for Render or future use)
DATA_DIR = os.environ.get("DATA_DIR")

# 2) If no override:
if not DATA_DIR:
    # On Render / Linux, use the persistent disk
    if os.name != "nt" and os.path.exists("/data"):
        DATA_DIR = "/data"
    else:
        # Local development (Windows & others)
        DATA_DIR = os.path.join(BASE_DIR, "data")

# Ensure directory exists
os.makedirs(DATA_DIR, exist_ok=True)

# ----------------------------------------------------
# Cache file paths (ALL must use DATA_DIR)
# ----------------------------------------------------
AI_BETS_CACHE_FILE = os.path.join(DATA_DIR, "ai_bets_cache.json")
AI_BETS_CARDS_FILE = os.path.join(DATA_DIR, "ai_bets_latest_cards.json")

FIXTURES_CACHE_FILE = os.path.join(DATA_DIR, "fixtures_cache.json")
GAME_DETAILS_CACHE_FILE = os.path.join(DATA_DIR, "game_details_cache.json")
GAME_DETAILS_CACHE_TIME_FILE = os.path.join(DATA_DIR, "game_details_cache_time.txt")

VALUE_BETS_CACHE_FILE = os.path.join(DATA_DIR, "value_bets_cache.json")
FILTERED_VALUE_BETS_ACTIVE_FILE = os.path.join(DATA_DIR, "filtered_value_bets_active.json")
FILTERED_VALUE_BETS_RESULTS_FILE = os.path.join(DATA_DIR, "filtered_value_bets_results.json")  # for later

SEASON_STATS_CACHE_FILE = os.path.join(DATA_DIR, "season_stats_cache.json")
SEASON_STATS_CACHE_FILE_LAST25 = os.path.join(DATA_DIR, "season_stats_last25.json")
SEASON_STATS_CACHE_TIME_FILE = os.path.join(DATA_DIR, "season_stats_cache_time.txt")
SEASON_STATS_LAST25_CACHE_TIME_FILE = os.path.join(DATA_DIR, "season_stats_last25_time.txt")

PREDICTABILITY_CACHE_FILE = os.path.join(DATA_DIR, "predictability_cache.json")

ODDALERTS_VALUE_RESULTS_URL = "https://data.oddalerts.com/api/value/results"
FILTERED_VALUE_BETS_RESULTS_FILE = os.path.join(DATA_DIR, "filtered_value_bets_results.json")

# Append-only log of all bets that ever qualified (used by results page)
FILTERED_VALUE_BETS_QUALIFIED_FILE = os.path.join(DATA_DIR, "filtered_value_bets_qualified.json")

def load_json_file(filepath, default):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json_atomic(filepath, data):
    tmp_path = filepath + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, filepath)

# ----------------------------------------------------
# Debug paths (TEMP)
# ----------------------------------------------------
print("[PATH] CWD =", os.getcwd())
print("[PATH] __file__ =", __file__)
print("[PATH] BASE_DIR =", os.path.abspath(BASE_DIR))
print("[PATH] DATA_DIR =", os.path.abspath(DATA_DIR))
print("[PATH] AI_BETS_CARDS_FILE =", os.path.abspath(AI_BETS_CARDS_FILE))
print("[PATH] AI_BETS_CACHE_FILE =", os.path.abspath(AI_BETS_CACHE_FILE))

# ---------------------------------------------------------
# FEATURE BUILDING – must mirror training logic
# ---------------------------------------------------------
NUMERIC_COLS = [
    "model_probability",
    "actual_odds",
    "implied_prob_bookmaker",
    "edge",
    "home_points_pg",
    "away_points_pg",
    "points_pg_diff",
    "home_goals_for_pg",
    "away_goals_for_pg",
    "goals_for_pg_diff",
    "home_goals_against_pg",
    "away_goals_against_pg",
    "goals_against_pg_diff",
    "home_shots_pg",
    "away_shots_pg",
    "shots_pg_diff",
    "home_sot_pg",
    "away_sot_pg",
    "sot_pg_diff",
    "home_xg_pg",
    "away_xg_pg",
    "xg_pg_diff",
    "home_dangerous_attacks_pg",
    "away_dangerous_attacks_pg",
    "dangerous_attacks_diff",
    "home_scored_first_pct",
    "away_scored_first_pct",
    "scored_first_diff",
    "home_clean_sheet_pct",
    "away_clean_sheet_pct",
    "clean_sheet_diff",
    "home_possession_pct",
    "away_possession_pct",
    "possession_diff",
    "home_games_played_home",
    "home_games_played_away",
    "away_games_played_home",
    "away_games_played_away",
]

def _parse_ko_hour_to_decimal(s):
    """
    Convert 'HH:MM' to decimal hour, e.g. '19:45' -> 19.75
    """
    try:
        text = str(s)
        if ":" not in text:
            return np.nan
        h_str, m_str = text.split(":", 1)
        h = int(h_str)
        m = int(m_str)
        return h + (m / 60.0)
    except Exception:
        return np.nan

def build_features_for_prediction(df_raw: pd.DataFrame) -> pd.DataFrame:
    """
    Build the SAME feature matrix used in training, but WITHOUT needing 'won'.
    Returns a DataFrame ready for model.predict_proba.
    """
    df = df_raw.copy()

    # Keep only result markets, same as training
    df = df[df["market_type"].isin(["home_win", "draw", "away_win"])].copy()
    df.reset_index(drop=True, inplace=True)

    if df.empty:
        print("[PRED] No result-market rows found (home/draw/away).")
        return pd.DataFrame()

    # Ensure numeric columns exist and coerce to numeric
    for col in NUMERIC_COLS:
        if col not in df.columns:
            df[col] = np.nan

    features = df[NUMERIC_COLS].apply(pd.to_numeric, errors="coerce")

    # Market one-hot flags
    features["is_home_win_market"] = (df["market_type"] == "home_win").astype(float)
    features["is_draw_market"] = (df["market_type"] == "draw").astype(float)
    features["is_away_win_market"] = (df["market_type"] == "away_win").astype(float)

    # Predictability dummies (training ended up with pred_unknown only)
    pred_series = df.get("competition_predictability").fillna("unknown").astype(str).str.lower()
    pred_dummies = pd.get_dummies(pred_series, prefix="pred")
    if "pred_unknown" not in pred_dummies.columns:
        pred_dummies["pred_unknown"] = 0
    features["pred_unknown"] = pred_dummies["pred_unknown"].astype(float)

    # Date features
    dt = pd.to_datetime(df.get("kickoff_date"), errors="coerce")
    features["ko_year"] = dt.dt.year.astype(float)
    features["ko_month"] = dt.dt.month.astype(float)
    features["ko_weekday"] = dt.dt.weekday.astype(float)

    # Kickoff hour as decimal
    features["ko_hour"] = df.get("kickoff_hour", "").apply(_parse_ko_hour_to_decimal).astype(float)

    # Fill NaNs
    features = features.astype(float)
    features = features.fillna(features.mean())
    features = features.fillna(0.0)

    print(f"[PRED] Built feature matrix for prediction: {features.shape[0]} rows, {features.shape[1]} columns")
    return features

# ---------------------------------------------------------
# Model loading (uses your repo-relative models folder by default)
# ---------------------------------------------------------
def load_ai_bets_model():
    explicit = os.environ.get("AI_BETS_MODEL_PATH")
    if explicit:
        model_path = explicit
    else:
        model_path = os.path.join(BASE_DIR, "models", "ai_bets_logreg.pkl")

    print(f"[AI_MODEL] Looking for model at: {model_path}")
    print(f"[AI_MODEL] Exists? {os.path.exists(model_path)}")

    if not os.path.exists(model_path):
        return None

    try:
        model = joblib.load(model_path)
        print("[AI_MODEL] Loaded successfully.")
        return model
    except Exception as e:
        print("[AI_MODEL] Failed to load model:", e)
        return None

app = Flask(__name__)

_ai_bets_model = load_ai_bets_model()
print("[AI_MODEL] Startup model object is None?", _ai_bets_model is None)

# ---------------------------------------------------------
# Gemini client (for AI Bets page)
# ---------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    print("[WARNING] GEMINI_API_KEY not set – /api/generate will fail until you set it.")
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# =========================
# API Configuration
# =========================
API_TOKEN = "jraOCcvLm50fZyB0atU8rS1WBSPClsKvUw34374i1jySpRUM9Y41I34LwPub"
HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}

FIXTURES_MULTIPLE_URL = "https://data.oddalerts.com/api/fixtures/multiple"
FIXTURES_API_URL = "https://data.oddalerts.com/api/probability/ft_result"
VALUE_BETS_API_URL = "https://data.oddalerts.com/api/value/upcoming"

BETSLIP_GENERATOR_URL = f"https://data.oddalerts.com/api/betslips?api_token={API_TOKEN}"

SEASON_STATS_SOURCES = {
    "season": {
        "label": "Season Stats",
        "url": "https://data.oddalerts.com/api/stats/season/{season_id}?api_token={api_token}&include_frozen=false",
    },
    "last25": {
        "label": "Last 25 Games",
        "url": "https://data.oddalerts.com/api/stats/season/{season_id}?api_token={api_token}&last_x=25_overall",
    },
}

# Set the secret key (needed for session management and flash messages)
app.secret_key = 'dev_secret_key'  # Replace 'dev_secret_key' with any string you like for local development

# =========================
# Load Cached Data at Startup
# =========================
if os.path.exists(FIXTURES_CACHE_FILE):
    with open(FIXTURES_CACHE_FILE, 'r', encoding='utf-8') as f:
        try:
            cached_fixtures = json.load(f)
        except json.JSONDecodeError:
            cached_fixtures = {}

if os.path.exists(VALUE_BETS_CACHE_FILE):
    with open(VALUE_BETS_CACHE_FILE, 'r', encoding='utf-8') as f:
        try:
            cached_value_bets = json.load(f)
        except json.JSONDecodeError:
            cached_value_bets = []

if os.path.exists(PREDICTABILITY_CACHE_FILE):
    with open(PREDICTABILITY_CACHE_FILE, 'r', encoding='utf-8') as f:
        try:
            predictability_cache = json.load(f)
        except json.JSONDecodeError:
            predictability_cache = {"timestamp": None, "data": {}}
else:
    predictability_cache = {"timestamp": None, "data": {}}

if os.path.exists(SEASON_STATS_CACHE_FILE):
    with open(SEASON_STATS_CACHE_FILE, 'r', encoding='utf-8') as f:
        try:
            season_stats_cache = json.load(f)
        except json.JSONDecodeError:
            season_stats_cache = {}
else:
    season_stats_cache = {}


# ---- Global Cache ----
cached_fixtures = {}

# ---- Fetch & Cache Functions ----
POST_KICKOFF_KEEP_SECONDS = 2 * 60 * 60  # 2 hours

def _now_utc_ts() -> int:
    return int(datetime.utcnow().timestamp())

def prune_fixtures_cache(fixtures_by_date: dict) -> dict:
    """
    Remove fixtures only after kickoff + 2 hours.
    """
    now_ts = _now_utc_ts()
    pruned = {}

    for date_key, countries in (fixtures_by_date or {}).items():
        for country, leagues in (countries or {}).items():
            for league, games in (leagues or {}).items():
                kept = []
                for g in (games or []):
                    unix_time = g.get("unix")
                    if not unix_time:
                        continue
                    if now_ts < int(unix_time) + POST_KICKOFF_KEEP_SECONDS:
                        kept.append(g)

                if kept:
                    pruned.setdefault(date_key, {}).setdefault(country, {}).setdefault(league, []).extend(kept)

    return pruned

def fetch_fixtures_grouped_by_structure(force_refresh=False):
    global cached_fixtures

    if not force_refresh and cached_fixtures:
        return cached_fixtures, set()

    fixtures_by_date = {}
    url = FIXTURES_API_URL
    retries = 0
    london_tz = pytz.timezone('Europe/London')

    while url:
        try:
            params = {"outcome": "home", "include": "odds", "bookmaker": 2}
            response = requests.get(url, headers=HEADERS, params=params)

            if response.status_code == 429:
                time.sleep(15)
                retries += 1
                continue

            response.raise_for_status()
            data = response.json()

            for item in data.get('data', []):
                if item.get('odds'):
                    unix_time = item.get('unix')
                    fixture_date = datetime.fromtimestamp(unix_time, pytz.utc).astimezone(london_tz).strftime('%Y-%m-%d')
                    country = item.get('competition_country', 'Unknown')
                    league = item.get('competition_name', 'Unknown League')
                    fixture_name = item.get('fixture_name')

                    fixtures_by_date.setdefault(fixture_date, {}).setdefault(country, {}).setdefault(league, []).append({
                        "fixture_name": fixture_name,
                        "unix": unix_time,
                        "fixture_id": item.get('id'),
                        "season_id": item.get('season_id'),
                        "competition_predictability": item.get('competition_predictability', 'Unknown'),
                        "competition_id": item.get('competition_id'),
                        "home_id": item.get('home_id'),
                        "away_id": item.get('away_id'),
                        "home_position": item.get("home_position"),
                        "away_position": item.get("away_position"),
                        "competition_country": country,
                        "competition_name": league
                    })

            url = data.get('info', {}).get('next_page_url')
            retries = 0
            time.sleep(0.8)

        except requests.RequestException:
            break

    # ✅ MERGE + PRUNE ONCE (after pagination)
    existing = load_fixtures_cache_from_disk() or {}

    existing_lookup = {}
    for date_data in existing.values():
        for country_data in date_data.values():
            for league_games in country_data.values():
                for g in league_games:
                    fid = g.get("fixture_id")
                    if fid is not None:
                        existing_lookup[str(fid)] = g

    for date_data in fixtures_by_date.values():
        for country_data in date_data.values():
            for league_games in country_data.values():
                for g in league_games:
                    fid = g.get("fixture_id")
                    if fid is not None:
                        existing_lookup[str(fid)] = g

    merged = {}
    for fid, g in existing_lookup.items():
        unix_time = g.get("unix")
        if not unix_time:
            continue

        fixture_date = datetime.fromtimestamp(int(unix_time), pytz.utc).astimezone(london_tz).strftime('%Y-%m-%d')
        country = g.get("competition_country", "Unknown")
        league = g.get("competition_name", "Unknown League")
        merged.setdefault(fixture_date, {}).setdefault(country, {}).setdefault(league, []).append(g)

    merged = prune_fixtures_cache(merged)

    cached_fixtures = merged
    save_fixtures_cache_to_disk()

    # ✅ Build season ids from what you're actually caching/serving
    unique_season_ids = set()
    for date_data in merged.values():
        for country_data in date_data.values():
            for league_fixtures in country_data.values():
                for fixture in league_fixtures:
                    season_id = fixture.get('season_id')
                    if season_id:
                        unique_season_ids.add(season_id)

    print(f"[CACHE] Fetched {len(unique_season_ids)} unique season IDs.")

    return merged or {}, unique_season_ids

def save_fixtures_cache_to_disk():
    os.makedirs(os.path.dirname(FIXTURES_CACHE_FILE), exist_ok=True)
    with open(FIXTURES_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cached_fixtures, f, indent=2, ensure_ascii=False)

def load_fixtures_cache_from_disk():
    global cached_fixtures
    if os.path.exists(FIXTURES_CACHE_FILE):
        with open(FIXTURES_CACHE_FILE, "r", encoding="utf-8") as f:
            try:
                cached_fixtures = json.load(f)
            except json.JSONDecodeError:
                cached_fixtures = {}
    else:
        cached_fixtures = {}
    
    return cached_fixtures  # ✅ This was missing

# ---- Always load from disk at startup ----
load_fixtures_cache_from_disk()

def refresh_fixtures_cache():
    global cached_fixtures
    print("[CACHE] Starting full cache refresh...")

    # Step 1: Fetch Fixtures and Season IDs
    print("[CACHE] Refreshing Fixtures Cache...")
    fixtures_data, unique_season_ids = fetch_fixtures_grouped_by_structure(force_refresh=True)
    print("[CACHE] Fixtures Cache Updated.")

    # Step 1.5: Update in-memory cache only
    # Disk is already saved inside fetch_fixtures_grouped_by_structure()
    cached_fixtures = fixtures_data

    # Step 1.6: Update Predictability Cache
    update_predictability_cache_from_fixtures(cached_fixtures)
    print("[CACHE] Predictability Cache Updated from Fixtures.")

    # Step 2: Fetch Season Stats
    print("[CACHE] Fetching Season Stats...")
    fetch_season_stats(unique_season_ids, API_TOKEN)
    # 🔁 Ensure in-memory season stats are refreshed from disk immediately
    load_season_stats_cache_from_disk()
    print("[CACHE] Season Stats Cache Updated and Reloaded in Memory.")

    # Step 2.5: Fetch "Last 25 Games" Season Stats
    print("[CACHE] Fetching Last 25 Games Season Stats...")
    fetch_season_stats_last25(unique_season_ids, API_TOKEN)
    load_season_stats_cache_last25_from_disk()
    print("[CACHE] Last 25 Games Season Stats Cache Updated and Reloaded in Memory.")

    # Step 3: Fetch Game Details
    print("[CACHE] Fetching Game Details...")
    fetch_and_cache_all_game_details()
    print("[CACHE] Game Details Cache Updated.")

    print("[CACHE] Full Cache Refresh Completed Successfully.\n")


# ---- Debug Endpoint ----

@app.route('/debug/fixtures-cache')
def debug_fixtures_cache():
    if os.path.exists(FIXTURES_CACHE_FILE):
        with open(FIXTURES_CACHE_FILE, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                return jsonify(data)
            except json.JSONDecodeError:
                return jsonify({})
    return jsonify({})

# =========================
# Value Bets Fetch & Cache
# =========================

MARKET_NAME_MAPPING = {
    "away_goals_15_probability": "Away Over 1.5 Goals",
    "away_win_ht_probability": "HT Result Away Win",
    "away_win_probability": "FT Result Away Win",
    "btts_probability": "Both Teams To Score Yes",
    "draw_ht_probability": "HT Result Draw",
    "draw_probability": "FT Result Draw",
    "home_goals_15_probability": "Home Over 1.5 Goals",
    "home_win_ht_probability": "HT Result Home Win",
    "home_win_probability": "FT Result Home Win",
    "o15_probability": "Over 1.5 Goals",
    "o25_probability": "Over 2.5 Goals",
    "o35_probability": "Over 3.5 Goals",
    "o45_probability": "Over 4.5 Goals",
    "o85_corners_probability": "Over 8.5 Corners",
    "u15_probability": "Under 1.5 Goals",
    "u25_probability": "Under 2.5 Goals",
    "u35_probability": "Under 3.5 Goals",
    "u45_probability": "Under 4.5 Goals",
}

def fetch_value_bets(force_refresh=False):
    global cached_value_bets

    if not force_refresh and cached_value_bets:
        return cached_value_bets

    all_value_bets = []
    page = 1

    while True:
        url = f"{VALUE_BETS_API_URL}?page={page}&api_token={API_TOKEN}"
        try:
            response = requests.get(url)
            if response.status_code == 429:
                time.sleep(15)
                continue

            response.raise_for_status()
            json_data = response.json()

            if not json_data.get('data'):
                break

            all_value_bets.extend(json_data['data'])
            page += 1
            time.sleep(0.8)

        except requests.RequestException:
            break

    cached_value_bets = all_value_bets

        # --- ADD THIS LINE BELOW! ---
    print(f"Saving value bets to: {VALUE_BETS_CACHE_FILE}")

    with open(VALUE_BETS_CACHE_FILE, 'w') as f:
        json.dump(all_value_bets, f)

    return all_value_bets

def refresh_value_bets_cache():
    print("[CACHE] Refreshing Value Bets Cache...")
    fetch_value_bets(force_refresh=True)
    print("[CACHE] Value Bets Cache Updated.")

    # ✅ NEW: rebuild filtered active + qualified after value bets refresh
    run_filtered_value_bets_matching()

@app.route('/debug/value-bets-cache')
def debug_value_bets_cache():
    if os.path.exists(VALUE_BETS_CACHE_FILE):
        with open(VALUE_BETS_CACHE_FILE, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                return jsonify(data)
            except json.JSONDecodeError:
                return jsonify({})
    return jsonify({})

# H2H cache config
H2H_CACHE_DIR = os.path.join(DATA_DIR, "h2h")
H2H_EXPIRY_DAYS = 3  # days to keep cache


os.makedirs(H2H_CACHE_DIR, exist_ok=True)

def _h2h_cache_path(fixture_id: int) -> str:
    """Return the full cache file path for a given fixture id."""
    return os.path.join(H2H_CACHE_DIR, f"{fixture_id}.json")

def _is_file_expired(filepath: str, days: int) -> bool:
    """Return True if file doesn't exist or is older than N days."""
    try:
        age_seconds = time.time() - os.path.getmtime(filepath)
        return age_seconds > (days * 86400)
    except FileNotFoundError:
        return True

def _load_h2h_from_cache(fixture_id: int):
    """Load cached JSON if present and not expired; otherwise None."""
    fp = _h2h_cache_path(fixture_id)
    if not os.path.exists(fp):
        return None
    if _is_file_expired(fp, H2H_EXPIRY_DAYS):
        # remove expired cache file so others don’t read it
        try:
            os.remove(fp)
        except OSError:
            pass
        return None
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # if corrupt, delete it and treat as cache miss
        try:
            os.remove(fp)
        except OSError:
            pass
        return None

def _save_h2h_to_cache(fixture_id: int, data) -> None:
    """Write raw JSON to the cache."""
    os.makedirs(H2H_CACHE_DIR, exist_ok=True)
    fp = _h2h_cache_path(fixture_id)
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

def _cleanup_h2h_cache() -> None:
    """Best-effort cleanup: delete any expired cache files."""
    if not os.path.isdir(H2H_CACHE_DIR):
        return
    for name in os.listdir(H2H_CACHE_DIR):
        if not name.endswith(".json"):
            continue
        fp = os.path.join(H2H_CACHE_DIR, name)
        if _is_file_expired(fp, H2H_EXPIRY_DAYS):
            try:
                os.remove(fp)
            except OSError:
                pass

@app.route("/api/fixture-search")
def api_fixture_search():
    q = (request.args.get("q") or "").strip().lower()

    # Don’t return anything for empty/very short queries
    if len(q) < 2:
        return jsonify([])

    # Load cached fixtures (same structure you already use)
    fixtures_by_date = load_fixtures_cache_from_disk()
    if not fixtures_by_date:
        return jsonify([])

    results = []

    # cached_fixtures structure: date -> country -> league -> [fixtures...]
    for date_key, countries in fixtures_by_date.items():
        for country_name, leagues in countries.items():
            for league_name, fixtures in leagues.items():
                for fx in fixtures:
                    fixture_name = (fx.get("fixture_name") or "")
                    if q in fixture_name.lower():
                        fixture_id = fx.get("fixture_id")
                        if fixture_id is None:
                            continue

                        results.append({
                            "fixture_id": fixture_id,
                            "fixture_name": fixture_name,
                            "unix": fx.get("unix"),
                            "competition_country": country_name,
                            "competition_name": league_name,
                            "details_url": url_for("game_details", fixture_id=fixture_id),
                        })

    # Sort by kickoff time (soonest first), then limit results
    results.sort(key=lambda x: (x["unix"] is None, x["unix"]))
    return jsonify(results[:15])


@app.route("/api/h2h/<int:fixture_id>")
def api_h2h(fixture_id: int):
    """
    Returns raw H2H JSON for a fixture.
    - Serves from cache if available and not expired.
    - Otherwise fetches from OddAlerts, caches for 3 days, and returns.
    """
    # 1) Try cache first
    cached = _load_h2h_from_cache(fixture_id)
    if cached is not None:
        _cleanup_h2h_cache()  # opportunistic cleanup
        return jsonify(cached)

    # 2) Fetch fresh from OddAlerts
    # Uses your existing API_TOKEN constant/variable in app.py.
    # If you don't have API_TOKEN defined, define it like your other endpoints.
    base_url = f"https://data.oddalerts.com/api/fixtures/{fixture_id}"
    url = f"{base_url}?api_token={API_TOKEN}&include=h2h"

    attempts = 0
    last_exc = None
    while attempts < 5:
        try:
            resp = requests.get(url, timeout=20)
            if resp.status_code == 429:
                # basic exponential backoff: 1,2,4,8,16s
                time.sleep(2 ** attempts or 1)
                attempts += 1
                continue
            resp.raise_for_status()
            data = resp.json()

            # save + return
            _save_h2h_to_cache(fixture_id, data)
            _cleanup_h2h_cache()
            return jsonify(data)
        except Exception as e:
            last_exc = e
            time.sleep(1.0)
            attempts += 1

    # 3) If fetch fails and no cache to fall back on
    return jsonify({
        "error": "Failed to fetch H2H data",
        "fixture_id": fixture_id,
        "detail": str(last_exc) if last_exc else "unknown error"
    }), 502

# =========================
# Game Details
# =========================

game_details_cache = {}

def fetch_season_stats(season_ids, api_token):
    cache_file = SEASON_STATS_CACHE_FILE
    cache_expiry_days = 3
    season_stats = {}

    # Ensure directory exists for the cache
    os.makedirs(os.path.dirname(cache_file), exist_ok=True)

    # Load existing cache if present
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                season_stats = json.load(f)
            print(f"[CACHE] {len(season_stats)} Season Stats already cached.")
        except json.JSONDecodeError:
            # Corrupt/partial file—start clean
            season_stats = {}

    current_time = datetime.utcnow().replace(tzinfo=timezone.utc)

    for season_id in season_ids:
        sid = str(season_id)
        entry = season_stats.get(sid)

        # TTL check (3 days)
        if entry and entry.get("last_updated"):
            try:
                last = datetime.fromisoformat(entry["last_updated"])
                if (current_time - last).days < cache_expiry_days:
                    continue
            except Exception:
                pass  # fall through and refetch

        # Fetch fresh
        retries = 0
        url = f"https://data.oddalerts.com/api/stats/season/{season_id}?api_token={api_token}&include_frozen=false"
        while retries < 5:
            try:
                resp = requests.get(url, headers=HEADERS, timeout=30)
                if resp.status_code == 429:
                    time.sleep(15); retries += 1; continue
                resp.raise_for_status()
                data = resp.json()

                season_stats[sid] = {
                    "last_updated": current_time.isoformat(),
                    "data": data.get("data", [])
                }

                # Pagination
                info = data.get("info", {})
                page = info.get("page", 1)
                pages = info.get("pages", 1)
                while page < pages:
                    page += 1
                    purl = f"{url}&page={page}"
                    pr = requests.get(purl, headers=HEADERS, timeout=30)
                    pr.raise_for_status()
                    season_stats[sid]["data"].extend(pr.json().get("data", []))

                # ATOMIC SAVE after this season
                tmp = cache_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(season_stats, f)
                os.replace(tmp, cache_file)

                print(f"[CACHE] Cached Season ID {season_id}")
                break
            except requests.RequestException:
                retries += 1
                print(f"[ERROR] Season ID {season_id} failed (retry {retries}/5).")
                time.sleep(5)

    # Remove stale seasons no longer in use
    wanted = set(map(str, season_ids))
    stale = set(season_stats.keys()) - wanted
    if stale:
        for sid in stale:
            del season_stats[sid]
            print(f"[CACHE] Removed stale Season ID {sid}")
        tmp = cache_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(season_stats, f)
        os.replace(tmp, cache_file)

    print(f"[CACHE] Fetched and cached season stats for {len(season_stats)} Season IDs.")
    return season_stats

def fetch_season_stats_last25(season_ids, api_token):
    """Fetch and cache 'Last 25 Games' season stats for each season."""
    cache_file = SEASON_STATS_CACHE_FILE_LAST25
    cache_expiry_days = 3
    season_stats = {}

    # Ensure directory exists
    os.makedirs(os.path.dirname(cache_file), exist_ok=True)

    # Load existing cache if present
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                season_stats = json.load(f)
            print(f"[CACHE] {len(season_stats)} 'Last 25 Games' seasons already cached.")
        except json.JSONDecodeError:
            season_stats = {}

    current_time = datetime.utcnow().replace(tzinfo=timezone.utc)

    for season_id in season_ids:
        sid = str(season_id)
        entry = season_stats.get(sid)

        # TTL check (3 days)
        if entry and entry.get("last_updated"):
            try:
                last = datetime.fromisoformat(entry["last_updated"])
                if (current_time - last).days < cache_expiry_days:
                    continue
            except Exception:
                pass

        # Fetch fresh
        retries = 0
        url = f"https://data.oddalerts.com/api/stats/season/{season_id}?api_token={api_token}&last_x=25_overall"
        while retries < 5:
            try:
                resp = requests.get(url, headers=HEADERS, timeout=30)
                if resp.status_code == 429:
                    time.sleep(15)
                    retries += 1
                    continue
                resp.raise_for_status()
                data = resp.json()

                season_stats[sid] = {
                    "last_updated": current_time.isoformat(),
                    "data": data.get("data", [])
                }

                # Handle pagination
                info = data.get("info", {})
                page = info.get("page", 1)
                pages = info.get("pages", 1)
                while page < pages:
                    page += 1
                    purl = f"{url}&page={page}"
                    pr = requests.get(purl, headers=HEADERS, timeout=30)
                    pr.raise_for_status()
                    season_stats[sid]["data"].extend(pr.json().get("data", []))

                # Atomic save
                tmp = cache_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(season_stats, f)
                os.replace(tmp, cache_file)

                print(f"[CACHE] Cached 'Last 25 Games' Season ID {season_id}")
                break
            except requests.RequestException:
                retries += 1
                print(f"[ERROR] 'Last 25 Games' Season ID {season_id} failed (retry {retries}/5).")
                time.sleep(5)

    print(f"[CACHE] Fetched and cached 'Last 25 Games' stats for {len(season_stats)} seasons.")

    # Save to memory
    global season_stats_cache_last25
    season_stats_cache_last25 = season_stats


# --- Save Season Stats Cache ---
def save_season_stats_cache_to_disk():
    try:
        os.makedirs(os.path.dirname(SEASON_STATS_CACHE_FILE), exist_ok=True)
        tmp = SEASON_STATS_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(season_stats_cache, f, indent=2)
        os.replace(tmp, SEASON_STATS_CACHE_FILE)

        # UPDATED: timestamp now uses DATA_DIR path
        with open(SEASON_STATS_CACHE_TIME_FILE, "w", encoding="utf-8") as t:
            t.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    except Exception as e:
        print(f"[ERROR] Failed to save season stats cache: {e}")


# --- Load Season Stats Cache ---
def load_season_stats_cache_from_disk():
    global season_stats_cache
    if os.path.exists(SEASON_STATS_CACHE_FILE):
        with open(SEASON_STATS_CACHE_FILE, "r", encoding="utf-8") as f:
            try:
                season_stats_cache = json.load(f)
            except json.JSONDecodeError:
                season_stats_cache = {}
    else:
        season_stats_cache = {}


def load_season_stats_cache_last25_from_disk():
    """Load the 'Last 25 Games' season stats cache from disk into memory."""
    global season_stats_cache_last25
    try:
        if os.path.exists(SEASON_STATS_CACHE_FILE_LAST25):
            with open(SEASON_STATS_CACHE_FILE_LAST25, "r", encoding="utf-8") as f:
                season_stats_cache_last25 = json.load(f)
            print(f"[CACHE] Loaded Last 25 Games cache with {len(season_stats_cache_last25)} seasons.")
        else:
            season_stats_cache_last25 = {}
            print("[CACHE] No Last 25 Games cache file found; starting empty.")
    except Exception as e:
        print(f"[ERROR] Failed to load Last 25 Games cache: {e}")
        season_stats_cache_last25 = {}


def save_season_stats_cache_last25_to_disk():
    """Save the in-memory 'Last 25 Games' season stats cache to disk (atomic)."""
    try:
        os.makedirs(os.path.dirname(SEASON_STATS_CACHE_FILE_LAST25), exist_ok=True)
        tmp = SEASON_STATS_CACHE_FILE_LAST25 + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(season_stats_cache_last25, f, indent=2)
        os.replace(tmp, SEASON_STATS_CACHE_FILE_LAST25)

        # UPDATED: timestamp now uses DATA_DIR path
        with open(SEASON_STATS_LAST25_CACHE_TIME_FILE, "w", encoding="utf-8") as t:
            t.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    except Exception as e:
        print(f"[ERROR] Failed to save Last 25 Games cache: {e}")

@app.route('/debug/season-stats-cache')
def debug_season_stats_cache():
    if os.path.exists(SEASON_STATS_CACHE_FILE):
        with open(SEASON_STATS_CACHE_FILE, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                return jsonify(data)
            except json.JSONDecodeError:
                return jsonify({})
    return jsonify({})


API_TOKEN = "jraOCcvLm50fZyB0atU8rS1WBSPClsKvUw34374i1jySpRUM9Y41I34LwPub"
GAME_DETAILS_CACHE_FILE = os.path.join(DATA_DIR, "game_details_cache.json")

# --- Save Game Details Cache ---
def save_game_details_cache_to_disk():
    try:
        with open(GAME_DETAILS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(game_details_cache, f, indent=2)

        # UPDATED: timestamp file now uses DATA_DIR
        with open(GAME_DETAILS_CACHE_TIME_FILE, "w", encoding="utf-8") as t:
            t.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    except Exception as e:
        print(f"[ERROR] Failed to save game details cache: {e}")


# --- Load Game Details Cache ---
def load_game_details_cache_from_disk():
    global game_details_cache
    if os.path.exists(GAME_DETAILS_CACHE_FILE):
        with open(GAME_DETAILS_CACHE_FILE, "r", encoding="utf-8") as f:
            try:
                game_details_cache = json.load(f)
            except json.JSONDecodeError:
                game_details_cache = {}
    else:
        game_details_cache = {}

def prune_game_details_cache_after_2h(combined_data: dict) -> dict:
    """
    Keep game details until kickoff + 2 hours.
    Uses 'unix' stored on each game_details entry.
    """
    now_ts = int(datetime.utcnow().timestamp())
    pruned = {}

    for fixture_id, payload in (combined_data or {}).items():
        kickoff_unix = None
        if isinstance(payload, dict):
            kickoff_unix = payload.get("unix")  # ✅ you already store this per fixture

        # If no kickoff time stored, keep it (failsafe)
        if kickoff_unix is None:
            pruned[fixture_id] = payload
            continue

        if now_ts < int(kickoff_unix) + POST_KICKOFF_KEEP_SECONDS:
            pruned[fixture_id] = payload

    return pruned

# --- Main Function ---
def fetch_and_cache_all_game_details():
    print(f"[CACHE] Refreshing Game Details Cache at {datetime.now().strftime('%H:%M:%S')}...")
    global game_details_cache, fixtures_cache

    # ✅ Load fixtures cache from disk
    fixtures_cache = load_fixtures_cache_from_disk()

    all_fixtures = {
        str(fixture.get("fixture_id"))
        for date in fixtures_cache.values()
        for country in date.values()
        for league in country.values()
        for fixture in league
    }

    if not all_fixtures:
        print("No fixtures found to update game details.")
        return {}

    if os.path.exists(GAME_DETAILS_CACHE_FILE):
        with open(GAME_DETAILS_CACHE_FILE, "r") as f:
            try:
                combined_data = json.load(f)
            except json.JSONDecodeError:
                combined_data = {}
    else:
        combined_data = {}

    def fetch_bookmaker_odds(bookmaker_id):
        url = (
            f"https://data.oddalerts.com/api/fixtures/upcoming"
            f"?api_token={API_TOKEN}&include=odds&bookmaker={bookmaker_id}"
        )
        odds_map = {}
        wait = 5

        while url:
            try:
                res = requests.get(url)
                if res.status_code == 429:
                    time.sleep(wait)
                    wait = min(wait * 2, 60)
                    continue

                res.raise_for_status()
                payload = res.json()
                data = payload.get("data", [])

                for fx in data:
                    fid = str(fx.get("id"))
                    if fid not in all_fixtures:
                        continue

                    raw = fx.get("odds", {})
                    if isinstance(raw, list):
                        norm = {}
                        for m in raw:
                            mname = m.get("market_name")
                            if mname:
                                norm[mname] = {k: v for k, v in m.items() if k != "market_name"}
                        odds_map[fid] = norm
                    elif isinstance(raw, dict):
                        odds_map[fid] = raw
                    else:
                        odds_map[fid] = {}

                url = payload.get("info", {}).get("next_page_url")
                time.sleep(0.5)

            except Exception as e:
                print(f"[ERROR] Bookmaker {bookmaker_id} page fetch failed: {e}")
                break

        return odds_map

    pinnacle_odds_map = fetch_bookmaker_odds(1)
    onexbet_odds_map = fetch_bookmaker_odds(3)
    williamhill_odds_map = fetch_bookmaker_odds(4)
    betfair_odds_map = fetch_bookmaker_odds(5)

    url = f"https://data.oddalerts.com/api/fixtures/upcoming?api_token={API_TOKEN}&include=probability,odds&bookmaker=2"
    start_time = time.time()

    while url:
        try:
            response = requests.get(url)
            if response.status_code == 429:
                time.sleep(15)
                continue

            response.raise_for_status()
            data = response.json()

            for item in data.get("data", []):
                fixture_id = str(item.get("id"))
                if fixture_id not in all_fixtures:
                    continue

                market_data = combined_data.setdefault(fixture_id, {})
                # Add fixture-level data from cached fixtures
                for date_fixtures in fixtures_cache.values():
                    for country_fixtures in date_fixtures.values():
                        for league_fixtures in country_fixtures.values():
                            for game in league_fixtures:
                                if str(game.get("fixture_id")) == fixture_id:
                                    for key in [
                                        "fixture_name", "unix", "season_id", "competition_predictability",
                                        "competition_id", "home_id", "away_id", "home_position", "away_position",
                                        "competition_country", "competition_name"
                                    ]:
                                        market_data[key] = game.get(key)
                                    break

                probs = item.get("probability", {})
                odds = item.get("odds", {})
                odds_dict = {od.get("market_name"): od for od in odds if "market_name" in od} if isinstance(odds, list) else odds

                pinnacle_odds = pinnacle_odds_map.get(fixture_id, {})
                onexbet_odds = onexbet_odds_map.get(fixture_id, {})
                williamhill_odds = williamhill_odds_map.get(fixture_id, {})
                betfair_odds = betfair_odds_map.get(fixture_id, {})

                def add_alt_odds(market_key, market_type, option_key, source_odds, label):
                    if market_key in market_data and isinstance(source_odds, dict):
                        group = source_odds.get(market_type)
                        if isinstance(group, dict):
                            value = group.get(option_key)
                            if isinstance(value, (int, float)):
                                market_data[market_key][label] = value

                def add_all_bookmaker_odds(market_key, market_type, option_key):
                    add_alt_odds(market_key, market_type, option_key, pinnacle_odds, "pinnacle_odds")
                    add_alt_odds(market_key, market_type, option_key, onexbet_odds, "onexbet_odds")
                    add_alt_odds(market_key, market_type, option_key, williamhill_odds, "williamhill_odds")
                    add_alt_odds(market_key, market_type, option_key, betfair_odds, "betfair_exchange_odds")

                # Full-Time Result
                for key in ["home_win", "draw", "away_win"]:
                    prob = probs.get(key)
                    if prob is not None:
                        base = key.split("_")[0]
                        market_data[key] = {
                            "probability": round(prob, 2),
                            "implied_odds": round(100 / prob, 2),
                            "actual_odds": odds_dict.get("ft_result", {}).get(base, "N/A")
                        }
                        add_all_bookmaker_odds(key, "ft_result", base)

                # Half-Time Result
                for key in ["home_win_ht", "draw_ht", "away_win_ht"]:
                    prob = probs.get(key)
                    if prob is not None:
                        base = key.split("_")[0]
                        market_data[key] = {
                            "probability": round(prob, 2),
                            "implied_odds": round(100 / prob, 2),
                            "actual_odds": odds_dict.get("ht_result", {}).get(base, "N/A")
                        }
                        add_all_bookmaker_odds(key, "ht_result", base)

                # Team to Score First
                for key in ["home_score_first", "draw_score_first", "away_score_first"]:
                    prob = probs.get(key)
                    if prob is not None:
                        market_data[key] = {
                            "probability": round(prob, 2),
                            "implied_odds": round(100 / prob, 2)
                        }

                # Double Chance
                dc_map = {
                    "double_chance_1x": "home_draw",
                    "double_chance_12": "home_away",
                    "double_chance_x2": "draw_away"
                }
                for key, label in dc_map.items():
                    prob = probs.get(key)
                    if prob is not None:
                        market_data[key] = {
                            "probability": round(prob, 2),
                            "implied_odds": round(100 / prob, 2),
                            "actual_odds": odds_dict.get("double_chance", {}).get(label, "N/A")
                        }
                        add_all_bookmaker_odds(key, "double_chance", label)

                # BTTS
                btts_yes = probs.get("btts")
                if btts_yes is not None:
                    market_data["btts_yes"] = {
                        "probability": round(btts_yes, 2),
                        "implied_odds": round(100 / btts_yes, 2),
                        "actual_odds": odds_dict.get("btts", {}).get("yes", "N/A")
                    }
                    add_all_bookmaker_odds("btts_yes", "btts", "yes")

                btts_no = probs.get("btts_no")
                if btts_no is not None:
                    market_data["btts_no"] = {
                        "probability": round(btts_no, 2),
                        "implied_odds": round(100 / btts_no, 2),
                        "actual_odds": odds_dict.get("btts", {}).get("no", "N/A")
                    }
                    add_all_bookmaker_odds("btts_no", "btts", "no")

                # Over/Under Goals
                for prob_key, odds_over, odds_under, mkt_over, mkt_under in [
                    ("15", "over_15", "under_15", "over_1_goals", "under_1_goals"),
                    ("25", "over_25", "under_25", "over_2_goals", "under_2_goals"),
                    ("35", "over_35", "under_35", "over_3_goals", "under_3_goals")
                ]:
                    over_prob = probs.get(f"o{prob_key}")
                    if over_prob is not None:
                        market_data[mkt_over] = {
                            "probability": round(over_prob, 2),
                            "implied_odds": round(100 / over_prob, 2),
                            "actual_odds": odds_dict.get("total_goals", {}).get(odds_over, "N/A")
                        }
                        add_all_bookmaker_odds(mkt_over, "total_goals", odds_over)

                        under_prob = round(100 - over_prob, 2)
                        market_data[mkt_under] = {
                            "probability": under_prob,
                            "implied_odds": round(100 / under_prob, 2),
                            "actual_odds": odds_dict.get("total_goals", {}).get(odds_under, "N/A")
                        }
                        add_all_bookmaker_odds(mkt_under, "total_goals", odds_under)

                # Over/Under Half Goals (First Half)
                over_half = probs.get("o0_1h_goals")
                if over_half is not None:
                    under_half = round(100 - over_half, 2)

                    # Over 0.5 1H
                    market_data["over_0_5_half_goals"] = {
                        "probability": round(over_half, 2),
                        "implied_odds": round(100 / over_half, 2),
                        "actual_odds": odds_dict.get("total_goals_1h", {}).get("over_05", "N/A"),
                        "pinnacle_odds": odds_dict.get("total_goals_1h", {}).get("over_05", "N/A"),
                        "onexbet_odds": odds_dict.get("total_goals_1h", {}).get("over_05", "N/A"),
                        "williamhill_odds": odds_dict.get("total_goals_1h", {}).get("over_05", "N/A"),
                        "betfair_exchange_odds": odds_dict.get("total_goals_1h", {}).get("over_05", "N/A"),
                    }

                    # Under 0.5 1H
                    market_data["under_0_5_half_goals"] = {
                        "probability": under_half,
                        "implied_odds": round(100 / under_half, 2),
                        "actual_odds": odds_dict.get("total_goals_1h", {}).get("under_05", "N/A"),
                        "pinnacle_odds": odds_dict.get("total_goals_1h", {}).get("under_05", "N/A"),
                        "onexbet_odds": odds_dict.get("total_goals_1h", {}).get("under_05", "N/A"),
                        "williamhill_odds": odds_dict.get("total_goals_1h", {}).get("under_05", "N/A"),
                        "betfair_exchange_odds": odds_dict.get("total_goals_1h", {}).get("under_05", "N/A"),
                    }


                # Team Goals
                for team_type in ["home", "away"]:
                    for line in ["0.5", "1.5"]:
                        key_suffix = line.replace('.', '')
                        prob_key = f"o{key_suffix}_{team_type}_goals"
                        market_key = f"{team_type}_o{key_suffix}"
                        prob = probs.get(prob_key)
                        odds_key = f"over_{key_suffix}"
                        if prob is not None:
                            market_data[market_key] = {
                                "probability": round(prob, 2),
                                "implied_odds": round(100 / prob, 2),
                                "actual_odds": odds_dict.get(f"{team_type}_goals", {}).get(odds_key, "N/A")
                            }
                            add_all_bookmaker_odds(market_key, f"{team_type}_goals", odds_key)

                            under_market = market_key.replace("_o", "_u")
                            under_prob = round(100 - prob, 2)
                            under_odds_key = f"under_{key_suffix}"
                            market_data[under_market] = {
                                "probability": under_prob,
                                "implied_odds": round(100 / under_prob, 2),
                                "actual_odds": odds_dict.get(f"{team_type}_goals", {}).get(under_odds_key, "N/A")
                            }
                            add_all_bookmaker_odds(under_market, f"{team_type}_goals", under_odds_key)

                # Corners
                for over_key, under_key, odds_over_key, odds_under_key, market_over, market_under in [
                    ("o7_corners", "u7_corners", "over_75", "under_75", "over_7_corners", "under_7_corners"),
                    ("o8_corners", "u8_corners", "over_85", "under_85", "over_8_corners", "under_8_corners"),
                    ("o9_corners", "u9_corners", "over_95", "under_95", "over_9_corners", "under_9_corners"),
                    ("o10_corners", "u10_corners", "over_105", "under_105", "over_10_corners", "under_10_corners"),
                    ("o11_corners", "u11_corners", "over_115", "under_115", "over_11_corners", "under_11_corners"),
                ]:
                    over_prob = probs.get(over_key)
                    if over_prob is not None:
                        market_data[market_over] = {
                            "probability": round(over_prob, 2),
                            "implied_odds": round(100 / over_prob, 2),
                            "actual_odds": odds_dict.get("total_corners", {}).get(odds_over_key, "N/A")
                        }
                        add_all_bookmaker_odds(market_over, "total_corners", odds_over_key)

                        under_prob = round(100 - over_prob, 2)
                        market_data[market_under] = {
                            "probability": under_prob,
                            "implied_odds": round(100 / under_prob, 2),
                            "actual_odds": odds_dict.get("total_corners", {}).get(odds_under_key, "N/A")
                        }
                        add_all_bookmaker_odds(market_under, "total_corners", odds_under_key)

            url = data.get("info", {}).get("next_page_url")
            time.sleep(0.8)

        except requests.RequestException as e:
            print(f"[ERROR] Failed to fetch game details: {e}")
            break

    combined_data = prune_game_details_cache_after_2h(combined_data)

    game_details_cache = combined_data
    save_game_details_cache_to_disk()


    duration = round((time.time() - start_time) / 60, 2)
    print(f"[CACHE COMPLETE] Game details updated in {duration} minutes ✅")
    return combined_data


# --- At module level: LOAD at startup (do NOT SAVE here!) ---
load_game_details_cache_from_disk()

def get_game_details_cache_last_updated():
    try:
        with open(GAME_DETAILS_CACHE_TIME_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return "Unknown"


@app.route('/debug/game-details-cache')
def debug_game_details_cache():
    last_updated = get_game_details_cache_last_updated()
    return jsonify({
        "last_updated": last_updated,
        "game_details_cache": game_details_cache
    })

# =========================
# Betslip Generator
# =========================

def update_predictability_cache_from_fixtures(fixtures_data):
    global predictability_cache
    grouped = {}

    print("[DEBUG] Starting to update predictability cache...")

    for date, date_data in fixtures_data.items():
        for country, leagues in date_data.items():
            for league, fixtures in leagues.items():
                predictability = None
                competition_id = None

                for fixture in fixtures:
                    predictability = fixture.get("competition_predictability")
                    competition_id = fixture.get("competition_id")
                    if predictability and competition_id:
                        break  # Found valid data

                if predictability and competition_id:
                    grouped.setdefault(country, {}).update({
                        competition_id: {
                            "name": league,
                            "predictability": predictability
                        }
                    })

    if not grouped:
        print("[DEBUG] No data found to store in predictability cache!")

    predictability_cache = {
        "timestamp": datetime.now().isoformat(),
        "data": grouped
    }
    with open(PREDICTABILITY_CACHE_FILE, 'w') as f:
        json.dump(predictability_cache, f, indent=4)

    print(f"[CACHE] Predictability Cache Updated from Fixtures. Countries: {len(grouped)}")

# =========================
# Flask Routes
# =========================

@app.context_processor
def inject_now():
    return {'now': datetime.utcnow()}

@app.route('/')
def home():
    london_time = datetime.now(pytz.timezone('Europe/London')).strftime('%Y-%m-%d')
    return render_template('home.html', current_date=london_time)

def sort_fixtures_structure(fixtures_by_date):
    for date, countries in fixtures_by_date.items():
        sorted_countries = {}
        for country, leagues in sorted(countries.items()):  # Sort countries
            sorted_leagues = {}
            for league, fixtures in sorted(leagues.items()):  # Sort leagues
                # Sort fixtures by 'unix' timestamp (kick-off time)
                sorted_fixtures = sorted(fixtures, key=lambda x: x.get('unix', 0))
                sorted_leagues[league] = sorted_fixtures
            sorted_countries[country] = sorted_leagues
        fixtures_by_date[date] = sorted_countries
    return fixtures_by_date

@app.route('/fixtures')
def fixtures_page():
    fixtures_by_date = load_fixtures_cache_from_disk()

    # ✅ SAFETY CHECK
    if not fixtures_by_date:
        return "Error: No fixture data available.", 500

    fixtures_by_date = sort_fixtures_structure(fixtures_by_date)

    dates = sorted(fixtures_by_date.keys())
    formatted_dates = [
        (date, datetime.strptime(date, '%Y-%m-%d').strftime('%a %d %b'))
        for date in dates
    ]

    today_date = datetime.utcnow().strftime('%Y-%m-%d')
    fixtures = fixtures_by_date.get(today_date, {})

    return render_template(
        'fixtures.html',
        fixtures=fixtures,
        dates=formatted_dates,
        selected_date=today_date,
        today_date=today_date
    )


@app.route('/fixtures/<selected_date>')
def fixtures_by_date_route(selected_date):
    fixtures_by_date = load_fixtures_cache_from_disk()

    # ✅ SAFETY CHECK
    if not fixtures_by_date:
        return "Error: No fixture data available.", 500

    fixtures_by_date = sort_fixtures_structure(fixtures_by_date)

    dates = sorted(fixtures_by_date.keys())
    formatted_dates = [
        (date, datetime.strptime(date, '%Y-%m-%d').strftime('%a %d %b'))
        for date in dates
    ]

    fixtures = fixtures_by_date.get(selected_date, {})

    return render_template(
        'fixtures.html',
        fixtures=fixtures,
        dates=formatted_dates,
        selected_date=selected_date,
        today_date=datetime.utcnow().strftime('%Y-%m-%d')
    )


@app.template_filter('datetimeformat')
def datetimeformat(value):
    london_tz = pytz.timezone('Europe/London')
    dt = datetime.fromtimestamp(value, pytz.utc).astimezone(london_tz)
    return dt.strftime('%d/%m/%Y %H:%M')

@app.route('/game/<int:fixture_id>')
def game_details(fixture_id):
    fixture_id_str = str(fixture_id)

    # Always load latest fixtures and both season stats caches
    load_fixtures_cache_from_disk()
    load_season_stats_cache_from_disk()
    load_season_stats_cache_last25_from_disk()

    # 🔍 Check if game data for this fixture is in memory
    if fixture_id_str not in game_details_cache:
        # 🧠 Try loading the latest game details cache from disk
        if os.path.exists(GAME_DETAILS_CACHE_FILE):
            try:
                with open(GAME_DETAILS_CACHE_FILE, "r", encoding="utf-8") as f:
                    disk_data = json.load(f)
                    # If this fixture exists on disk, update memory
                    if fixture_id_str in disk_data:
                        game_details_cache[fixture_id_str] = disk_data[fixture_id_str]
                    else:
                        return f"No data found for Fixture ID: {fixture_id}", 404
            except Exception as e:
                return f"Error loading game details cache: {e}", 500
        else:
            return f"No data found for Fixture ID: {fixture_id}", 404

    fixture_name = None
    kick_off_time = None
    home_team = None
    away_team = None
    season_id = None
    home_id = None
    away_id = None
    home_position = None
    away_position = None

    # 🔍 Find the fixture info from the fixture cache
    for date_fixtures in cached_fixtures.values():
        for country_fixtures in date_fixtures.values():
            for league_fixtures in country_fixtures.values():
                for game in league_fixtures:
                    if game.get("fixture_id") == fixture_id:
                        fixture_name = game.get("fixture_name")
                        kick_off_time = game.get("unix")
                        season_id = game.get("season_id")
                        home_id = game.get("home_id")
                        away_id = game.get("away_id")
                        home_position = game.get("home_position")
                        away_position = game.get("away_position")
                        if fixture_name and " vs " in fixture_name:
                            home_team, away_team = fixture_name.split(" vs ")
                        break

    if fixture_name is None:
        return f"No data found for Fixture ID: {fixture_id}", 404

    # ✅ Get the game data from memory (now guaranteed to exist)
    game_data = game_details_cache.get(fixture_id_str, {})

    # 📊 Load season stats for each team (supports toggle between Season & Last 25)
    home_stats = {}
    away_stats = {}

    # Determine which stats type to show: "season" or "last25"
    stats_type = request.args.get("stats", "season")

    if season_id:
        if stats_type == "last25":
            season_stats_data = season_stats_cache_last25.get(str(season_id), {}).get("data", [])
        else:
            season_stats_data = season_stats_cache.get(str(season_id), {}).get("data", [])

        for team_data in season_stats_data:
            team_id = team_data.get("team_id")
            if team_id == home_id:
                home_stats = team_data
            elif team_id == away_id:
                away_stats = team_data

    # 🧾 Render the page with all available data
    return render_template(
        'game_details.html',
        fixture_name=fixture_name,
        kick_off_time=kick_off_time,
        home_team=home_team,
        away_team=away_team,
        home_position=home_position,
        away_position=away_position,
        game_data=game_data,
        home_stats=home_stats,
        away_stats=away_stats,
        fixture_id=fixture_id,
        api_token=API_TOKEN,
        stats_type=stats_type  # 👈 Pass current stats mode to template
    )


@app.template_filter('ordinal')
def ordinal(value):
    try:
        value = int(value)
        if 10 <= value % 100 <= 20:
            suffix = 'th'
        else:
            suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(value % 10, 'th')
        return f"{value}{suffix}"
    except (ValueError, TypeError):
        return value

from datetime import datetime, timedelta
import json
from flask import render_template, request

@app.route('/simulate/<fixture_id>')
def simulate_game(fixture_id):
    api_token = 'jraOCcvLm50fZyB0atU8rS1WBSPClsKvUw34374i1jySpRUM9Y41I34LwPub'
    url = f"https://data.oddalerts.com/api/predictions/generate/{fixture_id}?api_token={api_token}"

    try:
        response = requests.get(url)
        print(f"URL Requested: {url}")
        print(f"Status Code: {response.status_code}")
        print(f"Response Body: {response.text}")

        if response.status_code != 200:
            return jsonify({"error": f"API error: {response.status_code}", "message": response.text}), response.status_code

        data = response.json()

        # ✅ Extract the actual simulation data using the fixture_id as a string
        sim_data = data.get(str(fixture_id))
        if not sim_data:
            return jsonify({"error": "Simulation data not found in response"}), 400

        return jsonify({"simulations": sim_data})

    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Request failed: {str(e)}"}), 500

@app.route('/probability-rankings')
def probability_rankings():
    selected_date = request.args.get('date')
    selected_market = request.args.get('market', 'home_win')
    odds_filter = request.args.get('odds') == 'on'
    value_filter = request.args.get('value') == 'on'
    selected_predictabilities = request.args.getlist('predictability')

    london_tz = pytz.timezone('Europe/London')

    try:
        with open(GAME_DETAILS_CACHE_FILE, 'r') as f:
            game_details = json.load(f)
    except json.JSONDecodeError as e:
        print("JSONDecodeError in game_details_cache.json:", e)
        game_details = {}

    try:
        with open(FIXTURES_CACHE_FILE, 'r', encoding='utf-8') as f:
            fixtures_data = json.load(f)
    except Exception as e:
        print("Error loading fixtures cache:", e)
        fixtures_data = {}

    if not selected_date:
        today_london = datetime.now(pytz.utc).astimezone(london_tz).strftime('%Y-%m-%d')
        selected_date = today_london

    fixture_lookup = {}
    for date, countries in fixtures_data.items():
        for country, leagues in countries.items():
            for league, fixtures in leagues.items():
                for fixture in fixtures:
                    fixture_id = str(fixture["fixture_id"])
                    kick_off = datetime.fromtimestamp(fixture["unix"], pytz.utc).astimezone(london_tz).strftime('%Y-%m-%d')
                    fixture_lookup[fixture_id] = {
                        "name": fixture["fixture_name"],
                        "kick_off": kick_off,
                        "kickoff_unix": fixture["unix"],
                        "league": league,
                        "country": country,
                        "predictability": (fixture.get("competition_predictability") or "").lower()
                    }

    results = []
    for fixture_id, markets in game_details.items():
        info = fixture_lookup.get(fixture_id)
        if info and info["kick_off"] == selected_date:
            if selected_predictabilities and info["predictability"] not in selected_predictabilities:
                continue

            market_data = markets.get(selected_market)
            if market_data and isinstance(market_data, dict):
                actual_odds = market_data.get("actual_odds")
                implied_odds = market_data.get("implied_odds")

                if odds_filter and selected_market not in [
                    "home_score_first", "draw_score_first", "away_score_first",
                    "home_win_ht", "draw_ht", "away_win_ht"
                ]:
                    if not actual_odds or str(actual_odds).strip().upper() == "N/A":
                        continue

                try:
                    actual_float = float(actual_odds)
                    implied_float = float(implied_odds)
                    if value_filter and actual_float <= implied_float:
                        continue
                except (TypeError, ValueError):
                    if value_filter:
                        continue

                try:
                    probability = float(market_data.get("probability", 0))
                except (ValueError, TypeError):
                    probability = 0

                kickoff_time = datetime.fromtimestamp(info["kickoff_unix"], pytz.utc).astimezone(london_tz).strftime('%H:%M')

                try:
                    actual_float = float(actual_odds)
                    implied_float = float(implied_odds)
                    value_change = ((actual_float - implied_float) / abs(implied_float)) * 100
                    value_percentage = round(value_change, 2)
                except (ValueError, ZeroDivisionError, TypeError):
                    value_percentage = 'N/A'

                results.append({
                    "fixture_id": fixture_id,
                    "fixture_name": info["name"],
                    "kickoff": kickoff_time,
                    "league": info["league"],
                    "country": info["country"],
                    "probability": probability,
                    "implied_odds": implied_odds,
                    "actual_odds": actual_odds,
                    "value_percentage": value_percentage,
                    "predictability": info["predictability"]
                })

    results.sort(key=lambda x: x["probability"], reverse=True)

    available_markets = [
        "home_win", "draw", "away_win",
        "home_win_ht", "draw_ht", "away_win_ht",
        "double_chance_1x", "double_chance_x2", "double_chance_12",
        "btts_yes", "btts_no",
        "over_1_goals", "over_2_goals", "over_3_goals",
        "under_1_goals", "under_2_goals", "under_3_goals",
        "home_o05", "home_o15", 
        "home_u05", "home_u15",
        "away_o05", "away_o15", 
        "away_u05", "away_u15",
        "over_7_corners", "over_8_corners", "over_9_corners", "over_10_corners", "over_11_corners",
        "under_7_corners", "under_8_corners", "under_9_corners", "under_10_corners", "under_11_corners",
        "home_score_first", "draw_score_first", "away_score_first",
        "over_0_5_half_goals", "under_0_5_half_goals"
    ]

    market_labels = {
        "home_win": "Home Win", "draw": "Draw", "away_win": "Away Win",
        "home_win_ht": "HT Home Win", "draw_ht": "HT Draw", "away_win_ht": "HT Away Win",
        "double_chance_1x": "Home or Draw",
        "double_chance_x2": "Draw or Away",
        "double_chance_12": "Home or Away",
        "btts_yes": "BTTS: Yes", "btts_no": "BTTS: No",
        "over_1_goals": "Over 1.5 Goals", "over_2_goals": "Over 2.5 Goals", "over_3_goals": "Over 3.5 Goals",
        "under_1_goals": "Under 1.5 Goals", "under_2_goals": "Under 2.5 Goals", "under_3_goals": "Under 3.5 Goals",
        "home_o05": "Home Over 0.5", "home_o15": "Home Over 1.5",
        "home_u05": "Home Under 0.5", "home_u15": "Home Under 1.5",
        "away_o05": "Away Over 0.5", "away_o15": "Away Over 1.5",
        "away_u05": "Away Under 0.5", "away_u15": "Away Under 1.5",
        "over_7_corners": "Over 7.5 Corners", "over_8_corners": "Over 8.5 Corners", "over_9_corners": "Over 9.5 Corners",
        "over_10_corners": "Over 10.5 Corners", "over_11_corners": "Over 11.5 Corners",
        "under_7_corners": "Under 7.5 Corners", "under_8_corners": "Under 8.5 Corners", "under_9_corners": "Under 9.5 Corners",
        "under_10_corners": "Under 10.5 Corners", "under_11_corners": "Under 11.5 Corners",
        "home_score_first": "Home Scores First", "draw_score_first": "No Goals First", "away_score_first": "Away Scores First",
        "over_0_5_half_goals": "Over 0.5 First Half Goals",
        "under_0_5_half_goals": "Under 0.5 First Half Goals"
    }

    return render_template(
        'probability_rankings.html',
        selected_date=selected_date,
        selected_market=selected_market,
        market_rows=results,
        available_markets=available_markets,
        odds_filter=odds_filter,
        value_filter=value_filter,
        selected_predictabilities=selected_predictabilities,
        now=datetime.utcnow(),
        timedelta=timedelta,
        market_labels=market_labels
    )

@app.route('/value_bets')
def value_bets():
    # Load value bets from the mounted cache file
    with open(VALUE_BETS_CACHE_FILE, 'r') as f:
        value_bets_data = json.load(f)

    table_data = []
    for bet in value_bets_data:
        if "odds" in bet and bet["odds"]:
            best_odds = max(bet["odds"], key=lambda x: float(x["latest"]))
            bookmaker_name = best_odds.get("bookmaker_name", "N/A")
            latest_odds = float(best_odds.get("latest", 0))
            value_percentage = float(best_odds.get("value", 0))
        else:
            bookmaker_name = "N/A"
            latest_odds = "N/A"
            value_percentage = "N/A"

        table_data.append({
            "market": bet["market"],
            "home_name": bet["home_name"],
            "away_name": bet["away_name"],
            "ko_human": bet["ko_human"],
            "competition_country": bet["competition"]["country"],
            "competition_name": bet["competition"]["name"],
            "competition_predictability": bet["competition"]["predictability"],
            "probability": round(bet["probability"], 2) if bet["probability"] is not None else "N/A",
            "implied_odds": round(1 / (bet["probability"] / 100), 2) if bet["probability"] > 0 else "N/A",
            "bookmaker": bookmaker_name,
            "latest_odds": latest_odds,
            "value_percentage": value_percentage,
            "fixture_id": bet["id"],
            "home_played": bet.get("home_played"),
            "away_played": bet.get("away_played"),
            "competition_progress": bet.get("competition", {}).get("progress"),
        })

    return render_template('value_bets.html', value_bets=table_data, market_name_mapping=MARKET_NAME_MAPPING)

def run_filtered_value_bets_matching():
    # ========= helpers =========
    def _to_float(x):
        try:
            return float(x)
        except Exception:
            return None

    def _in_range(val, r):
        if r is None:
            return True
        if val is None:
            return False
        mn = r.get("min", None)
        mx = r.get("max", None)
        if mn is not None and val < mn:
            return False
        if mx is not None and val > mx:
            return False
        return True

    def _build_fixture_name(bet: dict) -> str:
        home = bet.get("home_name") or bet.get("home_team") or bet.get("home") or ""
        away = bet.get("away_name") or bet.get("away_team") or bet.get("away") or ""
        home = str(home).strip()
        away = str(away).strip()
        if home and away:
            return f"{home} vs {away}"

        fx = bet.get("fixture_name") or bet.get("fixture") or bet.get("name") or bet.get("fixture_label")
        if fx:
            return str(fx)
        return "Unknown Fixture"

    def _match_strategy(bet, best_odds_row, strategy):
        comp = bet.get("competition") or {}

        market = bet.get("market")
        if strategy.get("markets") and market not in strategy["markets"]:
            return False

        allowed_preds = [p.lower() for p in (strategy.get("predictability") or [])]
        bet_pred = (comp.get("predictability") or "").lower()
        if allowed_preds and bet_pred not in allowed_preds:
            return False

        s_is_cup = strategy.get("is_cup", None)
        s_is_friendly = strategy.get("is_friendly", None)
        b_is_cup = comp.get("is_cup", None)
        b_is_friendly = comp.get("is_friendly", None)

        if s_is_cup is not None and b_is_cup is not None and b_is_cup != s_is_cup:
            return False
        if s_is_friendly is not None and b_is_friendly is not None and b_is_friendly != s_is_friendly:
            return False

        progress = _to_float(comp.get("progress"))
        if not _in_range(progress, strategy.get("progress")):
            return False

        prob = _to_float(bet.get("probability"))
        if not _in_range(prob, strategy.get("probability")):
            return False

        latest_odds = _to_float(best_odds_row.get("latest"))
        if not _in_range(latest_odds, strategy.get("odds")):
            return False

        value_pct = _to_float(best_odds_row.get("value"))
        if not _in_range(value_pct, strategy.get("value")):
            return False

        return True

    def _kelly_stake_10(prob_pct, odds, bankroll=100.0):
        if prob_pct is None or prob_pct <= 0:
            return 0.0
        if odds is None or odds <= 1:
            return 0.0

        p = prob_pct / 100.0
        q = 1.0 - p
        b = odds - 1.0

        f = (b * p - q) / b
        f = max(0.0, f)

        stake = bankroll * f * 0.10
        return round(stake, 2)

    def save_json_atomic(filepath, data):
        tmp_path = filepath + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, filepath)

    # ========= load cache (disk only) =========
    try:
        with open(VALUE_BETS_CACHE_FILE, "r", encoding="utf-8") as f:
            all_bets = json.load(f) or []
    except Exception:
        all_bets = []

    # ========= load QUALIFIED (so we can dedupe across runs) =========
    qualified_payload = load_json_file(FILTERED_VALUE_BETS_QUALIFIED_FILE, default={})
    qualified_rows = qualified_payload.get("rows", []) or []

    # bookmaker-agnostic dedupe key: fixture_id + market + strategy
    existing_base_keys = set(
        f"{b.get('fixture_id')}:{b.get('market')}:{b.get('matched_strategy')}"
        for b in qualified_rows
        if b.get("fixture_id") and b.get("market") and b.get("matched_strategy")
    )

    # ✅ run-only dedupe for the live/front page
    seen_this_run = set()

    # ========= apply strategies =========
    results = []

    for bet in all_bets:
        odds_list = bet.get("odds") or []
        if not odds_list:
            continue

        for strat in VALUE_BET_STRATEGIES:
            strat_name = strat.get("name", "Unnamed Strategy")

            strat_books = strat.get("bookmakers") or []
            strat_books = [b.lower() for b in strat_books if b]

            opening_guard = strat.get("opening_guard", False)

            best = None
            best_latest = None

            for o in odds_list:
                book_name = (o.get("bookmaker_name") or "").lower()
                if strat_books and book_name not in strat_books:
                    continue

                latest = _to_float(o.get("latest"))
                if latest is None:
                    continue

                if opening_guard:
                    opening = _to_float(o.get("opening"))
                    if opening is None:
                        continue
                    if latest < opening:
                        continue

                if best_latest is None or latest > best_latest:
                    best_latest = latest
                    best = o

            if best is None:
                continue

            if not _match_strategy(bet, best, strat):
                continue

            fixture_id = bet.get("id")
            if fixture_id is None:
                continue

            market = bet.get("market")

            comp = bet.get("competition") or {}

            bookmaker_name = best.get("bookmaker_name")
            latest_odds = _to_float(best.get("latest"))
            opening_odds = _to_float(best.get("opening"))
            value_pct = _to_float(best.get("value"))

            # keep full bet_key (with bookmaker) for traceability
            bet_key = f"{fixture_id}:{market}:{strat_name}:{bookmaker_name}"
            # dedupe key ignores bookmaker (only one entry per fixture+market+strategy)
            base_key = f"{fixture_id}:{market}:{strat_name}"

            # ✅ Only dedupe within this run (live/front page)
            if base_key in seen_this_run:
                continue
            seen_this_run.add(base_key)

            prob = _to_float(bet.get("probability"))
            implied_odds = round(100.0 / prob, 2) if prob and prob > 0 else None

            min_value = (strat.get("value") or {}).get("min", 0) or 0
            min_required_odds = None
            if prob and prob > 0:
                probability_decimal = prob / 100.0
                min_required_odds = round((1.0 / probability_decimal) * (1.0 + (min_value / 100.0)), 2)

            kelly_stake_10 = _kelly_stake_10(prob, latest_odds, bankroll=100.0)

            results.append({
                "bet_key": bet_key,
                "matched_strategy": strat_name,

                "fixture_id": fixture_id,
                "fixture_name": _build_fixture_name(bet),
                "ko_human": bet.get("ko_human"),
                "unix": bet.get("unix"),

                "competition_country": comp.get("country"),
                "competition_name": comp.get("name"),
                "predictability": comp.get("predictability"),
                "is_cup": comp.get("is_cup"),
                "is_friendly": comp.get("is_friendly"),
                "progress": comp.get("progress"),

                "market": market,
                "probability": round(prob, 2) if prob is not None else None,
                "implied_odds": implied_odds,
                "min_required_odds": min_required_odds,
                "kelly_stake_10": kelly_stake_10,

                "bookmaker": bookmaker_name,
                "latest_odds": latest_odds,
                "opening_odds": opening_odds,
                "value_percentage": value_pct,
            })

    # ========= write ACTIVE filtered results (snapshot, overwrite) =========
    active_payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "count": len(results),
        "rows": results,
    }
    save_json_atomic(FILTERED_VALUE_BETS_ACTIVE_FILE, active_payload)

    # ========= append-only QUALIFIED log (never delete) =========
    qualified_map = {row.get("bet_key"): row for row in qualified_rows if row.get("bet_key")}

    qualified_base_keys = set(
        f"{r.get('fixture_id')}:{r.get('market')}:{r.get('matched_strategy')}"
        for r in qualified_rows
        if r.get("fixture_id") and r.get("market") and r.get("matched_strategy")
    )

    newly_added = 0
    for row in results:
        bk = row.get("bet_key")
        if not bk:
            continue

        base_key = f"{row.get('fixture_id')}:{row.get('market')}:{row.get('matched_strategy')}"

        # ✅ only store the first-ever occurrence (bookmaker-agnostic)
        if base_key in qualified_base_keys:
            continue

        qualified_base_keys.add(base_key)
        qualified_map[bk] = row
        newly_added += 1


    new_qualified_rows = list(qualified_map.values())
    new_qualified_rows.sort(key=lambda x: (x.get("unix") is not None, x.get("unix") or 0), reverse=False)

    qualified_out = {
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "count": len(new_qualified_rows),
        "newly_added": newly_added,
        "rows": new_qualified_rows,
    }
    save_json_atomic(FILTERED_VALUE_BETS_QUALIFIED_FILE, qualified_out)

    # ========= group for display =========
    grouped_results = {}
    for r in results:
        grouped_results.setdefault(r["matched_strategy"], []).append(r)

    for strat_name, rows in grouped_results.items():
        rows.sort(
            key=lambda x: (x["value_percentage"] is not None, x["value_percentage"]),
            reverse=True
        )

    sorted_strategies = sorted(grouped_results.items(), key=lambda x: x[0].lower())
    return sorted_strategies

@app.route("/filtered-value-bets")
def filtered_value_bets():
    sorted_strategies = run_filtered_value_bets_matching()

    return render_template(
        "filtered_value_bets.html",
        grouped_results=sorted_strategies,
        market_name_mapping=MARKET_NAME_MAPPING
    )


def fetch_all_value_results(api_token: str, timeout: int = 30, max_pages: int = 200) -> list:
    """
    Fetch ALL pages from /api/value/results and return a flat list of result rows.
    Pagination is stored in payload["info"]["next_page_url"] and payload["info"]["has_more"].
    """
    if not api_token:
        return []

    url = f"{ODDALERTS_VALUE_RESULTS_URL}?api_token={api_token}"
    out = []
    pages = 0

    while url and pages < max_pages:
        pages += 1

        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code != 200:
                break
            payload = r.json() or {}
        except Exception:
            break

        # Data is under top-level "data"
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            rows = []

        out.extend(rows)

        # Pagination is under "info"
        info = payload.get("info") if isinstance(payload, dict) else {}
        if not isinstance(info, dict):
            info = {}

        next_url = info.get("next_page_url")
        has_more = info.get("has_more")

        if has_more and isinstance(next_url, str) and next_url.strip():
            url = next_url
        else:
            url = None

    return out

LONDON_TZ = ZoneInfo("Europe/London")

def _ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")

def format_day_time_from_unix(unix_ts: int) -> str:
    dt = datetime.fromtimestamp(int(unix_ts), tz=LONDON_TZ)
    day = dt.day
    return f"{dt.strftime('%a')} {day}{_ordinal(day)}, {dt.strftime('%H:%M')}"


def index_value_results(rows: list) -> dict:
    """
    Build an index for quick matching:
      key = (market, fixture_id)
      value = {"result": bool|None, "score": str|None}
    Expects rows shaped like:
      {
        "market": "home_win_probability",
        "id": 420463716,          # fixture id
        "result": {"result": true/false, "score": "2-0"}
      }
    """
    idx = {}

    if not isinstance(rows, list):
        return idx

    for r in rows:
        if not isinstance(r, dict):
            continue

        market = r.get("market")
        fixture_id = r.get("id")  # API uses "id" for fixture/game id

        if not market or fixture_id is None:
            continue

        rr = r.get("result") or {}
        if not isinstance(rr, dict):
            rr = {}

        idx[(str(market), int(fixture_id))] = {
            "result": rr.get("result"),  # True/False/None
            "score": rr.get("score"),    # "2-0"
        }

    return idx

def update_filtered_value_bets_results(api_token: str):
    """
    Reads FILTERED_VALUE_BETS_QUALIFIED_FILE (append-only list of all qualified bets),
    looks up results from OddAlerts /api/value/results with pagination,
    and writes/updates FILTERED_VALUE_BETS_RESULTS_FILE with result status + score.
    """

    import json
    import os
    import time
    import requests
    from datetime import datetime

    def _utc_now():
        return datetime.utcnow().isoformat() + "Z"

    def _to_int(x):
        try:
            return int(x)
        except Exception:
            return None

    def _safe_load_json(path, default):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def _save_json_atomic(path, data):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)

    # -------------------------
    # Load QUALIFIED bets
    # -------------------------
    qualified_payload = _safe_load_json(FILTERED_VALUE_BETS_QUALIFIED_FILE, default={}) or {}
    qualified_rows = qualified_payload.get("rows", []) or []

    # Keep only rows with the required keys
    wanted = []
    for r in qualified_rows:
        bet_key = r.get("bet_key")
        fixture_id = _to_int(r.get("fixture_id"))
        market = r.get("market")
        if bet_key and fixture_id and market:
            wanted.append((bet_key, fixture_id, market))

    # Deduplicate by bet_key (one result per bet_key)
    wanted_map = {}
    for bet_key, fixture_id, market in wanted:
        wanted_map[bet_key] = (fixture_id, market)

    wanted_bets = list(wanted_map.items())  # [(bet_key, (fixture_id, market)), ...]

    # -------------------------
    # Load existing results file
    # -------------------------
    results_payload = _safe_load_json(FILTERED_VALUE_BETS_RESULTS_FILE, default={}) or {}
    results_map = results_payload.get("results", {}) or {}

    # -------------------------
    # Build quick lookup: (fixture_id, market) -> [bet_key, ...]
    # (a single game could have multiple strategies/books -> multiple bet_keys)
    # -------------------------
    target_lookup = {}
    for bet_key, (fixture_id, market) in wanted_bets:
        target_lookup.setdefault((fixture_id, market), []).append(bet_key)

    # -------------------------
    # Fetch API pages + match
    # -------------------------
    base_url = f"https://data.oddalerts.com/api/value/results?api_token={api_token}"

    active_rows_count = len(wanted_bets)
    api_rows_seen = 0
    matched = 0
    updated = 0

    next_url = base_url
    visited_pages = 0
    max_pages_guard = 50  # safety

    # Only bother looking for bets that aren't already settled
    # (still allow re-checks, but this makes it faster)
    unresolved_targets = set(target_lookup.keys())
    for (fixture_id, market), bet_keys in list(target_lookup.items()):
        # if ANY bet_key is still pending/missing, keep it in unresolved
        # if ALL bet_keys are settled (win/loss/void), remove it
        all_settled = True
        for bk in bet_keys:
            existing = results_map.get(bk)
            if not isinstance(existing, dict):
                all_settled = False
                break
            if existing.get("status") not in ("win", "loss", "void"):
                all_settled = False
                break
        if all_settled:
            unresolved_targets.discard((fixture_id, market))

    session = requests.Session()

    while next_url and visited_pages < max_pages_guard and unresolved_targets:
        visited_pages += 1

        try:
            res = session.get(next_url, timeout=30)
            if res.status_code != 200:
                break
            payload = res.json() or {}
        except Exception:
            break

        info = payload.get("info") or {}
        data_rows = payload.get("data") or []
        api_rows_seen += len(data_rows)

        # Match within this page
        for api_row in data_rows:
            api_id = _to_int(api_row.get("id"))
            api_market = api_row.get("market")

            if not api_id or not api_market:
                continue

            key = (api_id, api_market)
            if key not in unresolved_targets:
                continue

            # We found a fixture_id+market match; grab result block
            result_block = api_row.get("result") or {}
            result_bool = result_block.get("result", None)
            score = result_block.get("score", None)

            # Translate to status
            status = "pending"
            if result_bool is True:
                status = "win"
            elif result_bool is False:
                status = "loss"

            bet_keys = target_lookup.get(key, [])
            if bet_keys:
                matched += len(bet_keys)

            for bk in bet_keys:
                prev = results_map.get(bk)
                prev_status = prev.get("status") if isinstance(prev, dict) else None
                prev_score = prev.get("score") if isinstance(prev, dict) else None

                # Update if new or changed
                if (prev_status != status) or (prev_score != score):
                    results_map[bk] = {
                        "status": status,
                        "score": score,
                        "fixture_id": api_id,
                        "market": api_market,
                        "updated_at": _utc_now(),
                    }
                    updated += 1
                else:
                    # Touch updated_at so you know it was checked
                    if isinstance(prev, dict):
                        prev["updated_at"] = _utc_now()
                        results_map[bk] = prev

            # If now all bet_keys for this (fixture_id,market) are settled, remove from unresolved
            # (pending stays unresolved)
            if status in ("win", "loss", "void"):
                unresolved_targets.discard(key)

        # next page
        next_url = info.get("next_page_url")
        # tiny pause to be polite
        time.sleep(0.15)

    out_payload = {
        "updated_at": _utc_now(),
        "results": results_map
    }
    _save_json_atomic(FILTERED_VALUE_BETS_RESULTS_FILE, out_payload)

    return {
        "active_rows": active_rows_count,
        "api_rows": api_rows_seen,
        "matched": matched,
        "updated": updated
    }

@app.route("/debug/update-filtered-results")
def debug_update_filtered_results():
    api_token = API_TOKEN

    summary = update_filtered_value_bets_results(api_token)
    return summary

@app.route("/debug/value-results-sample")
def debug_value_results_sample():
    rows = fetch_all_value_results(API_TOKEN)

    sample = rows[:5] if isinstance(rows, list) else []
    # Return keys from the first row to see actual field names
    first_keys = list(sample[0].keys()) if sample and isinstance(sample[0], dict) else []

    return {
        "total_rows_returned": len(rows) if isinstance(rows, list) else 0,
        "first_row_keys": first_keys,
        "sample_rows": sample
    }

@app.route("/debug/value-results-pagination")
def debug_value_results_pagination():
    url = f"{ODDALERTS_VALUE_RESULTS_URL}?api_token={API_TOKEN}"
    r = requests.get(url, timeout=30)
    payload = r.json() if r.status_code == 200 else {}

    return {
        "status_code": r.status_code,
        "top_level_keys": list(payload.keys()) if isinstance(payload, dict) else "payload_not_dict",
        "next_page_url": payload.get("next_page_url") if isinstance(payload, dict) else None,
        "links": payload.get("links") if isinstance(payload, dict) else None,
        "meta": payload.get("meta") if isinstance(payload, dict) else None,
    }
@app.route("/filtered-value-bets-results")
def filtered_value_bets_results_page():
    # =========================
    # Helpers
    # =========================
    def _to_float(x):
        try:
            return float(x)
        except Exception:
            return None

    def _month_label_from_unix(unix_val):
        if not unix_val:
            return "Unknown"
        try:
            dt = datetime.fromtimestamp(int(unix_val), tz=LONDON_TZ)
            return dt.strftime("%B %Y")  # e.g. "December 2025"
        except Exception:
            return "Unknown"

    def _month_sort_key(month_label):
        try:
            return datetime.strptime(month_label, "%B %Y")
        except Exception:
            return datetime.min

    def load_json_file(filepath, default=None):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    # =========================
    # Load QUALIFIED bets (append-only log)
    # =========================
    qualified_payload = load_json_file(FILTERED_VALUE_BETS_QUALIFIED_FILE, default={}) or {}
    qualified_rows = qualified_payload.get("rows", []) or []

    # =========================
    # Load results map (bet_key -> status/score)
    # =========================
    results_payload = load_json_file(FILTERED_VALUE_BETS_RESULTS_FILE, default={}) or {}
    results_map = results_payload.get("results", {}) or {}

    # =========================
    # Merge + compute P/L
    # =========================
    merged = []

    for r in qualified_rows:
        bet_key = r.get("bet_key")

        latest_odds = _to_float(r.get("latest_odds"))
        kelly_stake = _to_float(r.get("kelly_stake_10"))

        res = results_map.get(bet_key) if bet_key else None

        status = "pending"
        score = None

        pl_1u = None
        pl_kelly = None

        if isinstance(res, dict):
            status = res.get("status") or "pending"
            score = res.get("score")

            # Only calculate P/L if settled
            if status in ("win", "loss", "void") and latest_odds is not None:
                if status == "win":
                    pl_1u = round((1.0 * latest_odds) - 1.0, 2)
                    if kelly_stake is not None:
                        pl_kelly = round((kelly_stake * latest_odds) - kelly_stake, 2)
                elif status == "loss":
                    pl_1u = -1.0
                    if kelly_stake is not None:
                        pl_kelly = round(-kelly_stake, 2)
                elif status == "void":
                    pl_1u = 0.0
                    if kelly_stake is not None:
                        pl_kelly = 0.0

        out = dict(r)

        u = out.get("unix")
        out["kickoff"] = format_day_time_from_unix(u) if u else ""

        print("DEBUG kickoff:", out.get("kickoff"), "unix:", out.get("unix"))

        out["result_status"] = status
        # ✅ aliases for templates that expect different keys
        out["status"] = status
        out["result"] = status
        out["score"] = score
        out["stake_1u"] = 1.0
        out["stake_kelly"] = kelly_stake
        out["pl_1u"] = pl_1u
        out["pl_kelly"] = pl_kelly

        # ✅ aliases for template compatibility
        out["one_unit_pl"] = pl_1u
        out["kelly_pl"] = pl_kelly

        merged.append(out)

    # =========================
    # Group strategy -> month, with totals
    # =========================
    grouped = defaultdict(lambda: defaultdict(lambda: {
        "rows": [],
        "total_1u_pl": 0.0,
        "total_kelly_pl": 0.0,
        "count": 0,
        "settled_count": 0
    }))

    strategy_totals = defaultdict(lambda: {
        "total_1u_pl": 0.0,
        "total_kelly_pl": 0.0,
        "count": 0,
        "settled_count": 0
    })

    for row in merged:
        strat = row.get("matched_strategy") or "Unknown Strategy"
        month = _month_label_from_unix(row.get("unix"))

        pl1 = row.get("pl_1u")
        plk = row.get("pl_kelly")

        pl1_num = float(pl1) if pl1 is not None else 0.0
        plk_num = float(plk) if plk is not None else 0.0

        grouped[strat][month]["rows"].append(row)
        grouped[strat][month]["total_1u_pl"] += pl1_num
        grouped[strat][month]["total_kelly_pl"] += plk_num
        grouped[strat][month]["count"] += 1
        if row.get("result_status") != "pending":
            grouped[strat][month]["settled_count"] += 1

        strategy_totals[strat]["total_1u_pl"] += pl1_num
        strategy_totals[strat]["total_kelly_pl"] += plk_num
        strategy_totals[strat]["count"] += 1
        if row.get("result_status") != "pending":
            strategy_totals[strat]["settled_count"] += 1

    # Sort months newest -> oldest (so Dec 2025 then Jan 2026, etc.)
    sorted_grouped = []
    for strat_name in sorted(grouped.keys(), key=lambda x: x.lower()):
        months_dict = grouped[strat_name]

        sorted_months = []
        for month_label in sorted(months_dict.keys(), key=_month_sort_key, reverse=True):
            months_dict[month_label]["rows"].sort(key=lambda x: x.get("unix") or 0)
            sorted_months.append((month_label, months_dict[month_label]))

        sorted_grouped.append((
            strat_name,
            {
                "totals": strategy_totals[strat_name],
                "months": sorted_months
            }
        ))

    return render_template(
        "filtered_value_bets_results.html",
        grouped=sorted_grouped,
        market_name_mapping=MARKET_NAME_MAPPING
    )

@app.template_filter("format_kickoff")
def format_kickoff_filter(value):
    if not value:
        return "N/A"

    london_tz = pytz.timezone('Europe/London')
    dt = datetime.fromtimestamp(value, pytz.utc).astimezone(london_tz)
    today = datetime.now(london_tz).date()

    if dt.date() == today:
        return dt.strftime("%H:%M")  # e.g., 20:00

    # Add day suffix
    day = dt.day
    if 11 <= day <= 13:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')

    return dt.strftime(f"%a {day}{suffix}, %H:%M")



@app.route("/pinnacle")
def pinnacle_comparisons():
    with open(GAME_DETAILS_CACHE_FILE, "r") as f:
        game_data = json.load(f)

    print("Fixtures loaded:", list(game_data.keys()))


    comparisons = []

    valid_bookmakers = {
        "williamhill_odds": "William Hill",
        "onexbet_odds": "1xBet",
        "betfair_exchange_odds": "Betfair Exchange",
        "actual_odds": "Bet365"
    }

    market_label_mapping = {
        "home_win": "Home Win", "draw": "Draw", "away_win": "Away Win",
        "home_win_ht": "HT Home Win", "draw_ht": "HT Draw", "away_win_ht": "HT Away Win",
        "double_chance_1x": "Home or Draw", "double_chance_x2": "Draw or Away", "double_chance_12": "Home or Away",
        "btts_yes": "BTTS: Yes", "btts_no": "BTTS: No",
        "over_1_goals": "Over 1.5 Goals", "over_2_goals": "Over 2.5 Goals", "over_3_goals": "Over 3.5 Goals",
        "under_1_goals": "Under 1.5 Goals", "under_2_goals": "Under 2.5 Goals", "under_3_goals": "Under 3.5 Goals",
        "home_o05": "Home Over 0.5 Goals", "home_o15": "Home Over 1.5 Goals", "home_o25": "Home Over 2.5 Goals",
        "home_u05": "Home Under 0.5 Goals", "home_u15": "Home Under 1.5 Goals", "home_u05": "Home Under 2.5 Goals",
        "away_o05": "Away Over 0.5 Goals", "away_o15": "Away Over 1.5 Goals", "away_o25": "Away Over 2.5 Goals",
        "away_u05": "Away Under 0.5 Goals", "away_u15": "Away Under 1.5 Goals", "away_u25": "Away Under 2.5 Goals",
        "over_7_corners": "Over 7.5 Corners", "over_8_corners": "Over 8.5 Corners", "over_9_corners": "Over 9.5 Corners",
        "over_10_corners": "Over 10.5 Corners", "over_11_corners": "Over 11.5 Corners",
        "under_7_corners": "Under 7.5 Corners", "under_8_corners": "Under 8.5 Corners", "under_9_corners": "Under 9.5 Corners",
        "under_10_corners": "Under 10.5 Corners", "under_11_corners": "Under 11.5 Corners",
        "home_score_first": "Home Scores First", "draw_score_first": "No Goals First", "away_score_first": "Away Scores First"
    }

    for fixture_id, fixture_data in game_data.items():
        fixture_name = fixture_data.get("fixture_name", fixture_id)
        predictability = fixture_data.get("competition_predictability", "N/A")
        competition_name = fixture_data.get("competition_name", "N/A")
        competition_country = fixture_data.get("competition_country", "N/A")
        

        for market_key, data in fixture_data.items():
            if not isinstance(data, dict):
                continue

            pinnacle_odds = data.get("pinnacle_odds")
            if pinnacle_odds is None:
                continue

            try:
                pinnacle_odds_val = float(pinnacle_odds)
            except:
                continue

            for bookmaker_key, bookmaker_label in valid_bookmakers.items():
                odds = data.get(bookmaker_key)
                if odds is None:
                    continue
                try:
                    bookmaker_odds = float(odds)
                    if bookmaker_odds > pinnacle_odds_val:
                        comparisons.append({
                            "fixture_id": int(fixture_id),
                            "fixture": fixture_name,
                            "predictability": predictability,
                            "competition_name": competition_name,
                            "competition_country": competition_country,
                            "market": market_label_mapping.get(market_key, market_key),
                            "probability": data.get("probability"),
                            "implied_odds": data.get("implied_odds"),
                            "pinnacle_odds": pinnacle_odds_val,
                            "bookmaker": bookmaker_label,
                            "bookmaker_odds": bookmaker_odds,
                            "kickoff": fixture_data.get("unix"),
                            "odds_difference": round(((bookmaker_odds - pinnacle_odds_val) / abs(pinnacle_odds_val)) * 100, 2)
                        })
                except:
                    continue

    return render_template("pinnacle.html", comparisons=comparisons)


@app.route('/filter_value_bets', methods=['POST'])
def filter_value_bets():
    """Filters cached value bets based on selected bookmakers, predictability levels, markets, and exclusions."""
    try:
        request_data = request.get_json()

        # ✅ Always load the most recent value bets from the cache file
        print(f"Loading value bets from: {VALUE_BETS_CACHE_FILE}")
        with open(VALUE_BETS_CACHE_FILE, "r") as f:
            filtered_bets = json.load(f)
        print(f"First bet in cache: {filtered_bets[0] if filtered_bets else 'EMPTY'}")
        print(f"Filter route cache loaded: {len(filtered_bets)} bets")

        selected_bookmakers = request_data.get("bookmakers", [])
        selected_predictability = request_data.get("predictability", [])
        exclude_cups = request_data.get("exclude_cups", False)
        exclude_friendlies = request_data.get("exclude_friendlies", False)
                # Season Progress (global range)
        sp_cfg = (request_data.get("season_progress") or {})
        sp_min = sp_cfg.get("min", 0)
        sp_max = sp_cfg.get("max", 100)

        # clamp & sanitize
        try:
            sp_min = max(0.0, min(100.0, float(sp_min)))
        except Exception:
            sp_min = 0.0
        try:
            sp_max = max(0.0, min(100.0, float(sp_max)))
        except Exception:
            sp_max = 100.0

        # ensure min <= max
        if sp_min > sp_max:
            sp_min, sp_max = sp_max, sp_min

        selected_markets = request_data.get("markets", [])   

        # Load home win FT filter settings
        home_win_filters = request_data.get("home_win_filters", {})
        include_home_win = home_win_filters.get("include", True)
        home_win_prob_min = home_win_filters.get("probability_min", 0)
        home_win_prob_max = home_win_filters.get("probability_max", 100)
        home_win_odds_min = home_win_filters.get("odds_min", 1.00)
        home_win_odds_max = home_win_filters.get("odds_max", 10.00)
        home_win_value_min = home_win_filters.get("value_min", 0)
        home_win_value_max = home_win_filters.get("value_max", 100)

        # Load Draw FT filter settings
        draw_filters = request_data.get("draw_filters", {})
        include_draw = draw_filters.get("include", True)
        draw_prob_min = draw_filters.get("probability_min", 0)
        draw_prob_max = draw_filters.get("probability_max", 100)
        draw_odds_min = draw_filters.get("odds_min", 1.00)
        draw_odds_max = draw_filters.get("odds_max", 10.00)
        draw_value_min = draw_filters.get("value_min", 0)
        draw_value_max = draw_filters.get("value_max", 100)

        # Load Away Win FT Filter settings
        away_win_filters = request_data.get("away_win_filters", {})
        include_away_win = away_win_filters.get("include", True)
        away_win_prob_min = away_win_filters.get("probability_min", 0)
        away_win_prob_max = away_win_filters.get("probability_max", 100)
        away_win_odds_min = away_win_filters.get("odds_min", 1.00)
        away_win_odds_max = away_win_filters.get("odds_max", 10.00)
        away_win_value_min = away_win_filters.get("value_min", 0)
        away_win_value_max = away_win_filters.get("value_max", 100)

        # Half Time Home Win
        home_win_ht_filters = request_data.get("home_win_ht_filters", {})
        include_home_win_ht = home_win_ht_filters.get("include", True)
        home_win_ht_prob_min = home_win_ht_filters.get("probability_min", 0)
        home_win_ht_prob_max = home_win_ht_filters.get("probability_max", 100)
        home_win_ht_odds_min = home_win_ht_filters.get("odds_min", 1.00)
        home_win_ht_odds_max = home_win_ht_filters.get("odds_max", 10.00)
        home_win_ht_value_min = home_win_ht_filters.get("value_min", 0)
        home_win_ht_value_max = home_win_ht_filters.get("value_max", 100)

        # Half Time Draw
        draw_ht_filters = request_data.get("draw_ht_filters", {})
        include_draw_ht = draw_ht_filters.get("include", True)
        draw_ht_prob_min = draw_ht_filters.get("probability_min", 0)
        draw_ht_prob_max = draw_ht_filters.get("probability_max", 100)
        draw_ht_odds_min = draw_ht_filters.get("odds_min", 1.00)
        draw_ht_odds_max = draw_ht_filters.get("odds_max", 10.00)
        draw_ht_value_min = draw_ht_filters.get("value_min", 0)
        draw_ht_value_max = draw_ht_filters.get("value_max", 100)

        # Half Time Away Win
        away_win_ht_filters = request_data.get("away_win_ht_filters", {})
        include_away_win_ht = away_win_ht_filters.get("include", True)
        away_win_ht_prob_min = away_win_ht_filters.get("probability_min", 0)
        away_win_ht_prob_max = away_win_ht_filters.get("probability_max", 100)
        away_win_ht_odds_min = away_win_ht_filters.get("odds_min", 1.00)
        away_win_ht_odds_max = away_win_ht_filters.get("odds_max", 10.00)
        away_win_ht_value_min = away_win_ht_filters.get("value_min", 0)
        away_win_ht_value_max = away_win_ht_filters.get("value_max", 100)

        # Over 1.5 Goals
        o15_filters = request_data.get("o15_filters", {})
        include_o15 = o15_filters.get("include", True)
        o15_prob_min = o15_filters.get("probability_min", 0)
        o15_prob_max = o15_filters.get("probability_max", 100)
        o15_odds_min = o15_filters.get("odds_min", 1.00)
        o15_odds_max = o15_filters.get("odds_max", 10.00)
        o15_value_min = o15_filters.get("value_min", 0)
        o15_value_max = o15_filters.get("value_max", 100)

        # Over 2.5 Goals
        o25_filters = request_data.get("o25_filters", {})
        include_o25 = o25_filters.get("include", True)
        o25_prob_min = o25_filters.get("probability_min", 0)
        o25_prob_max = o25_filters.get("probability_max", 100)
        o25_odds_min = o25_filters.get("odds_min", 1.00)
        o25_odds_max = o25_filters.get("odds_max", 10.00)
        o25_value_min = o25_filters.get("value_min", 0)
        o25_value_max = o25_filters.get("value_max", 100)

        # Over 3.5 Goals
        o35_filters = request_data.get("o35_filters", {})
        include_o35 = o35_filters.get("include", True)
        o35_prob_min = o35_filters.get("probability_min", 0)
        o35_prob_max = o35_filters.get("probability_max", 100)
        o35_odds_min = o35_filters.get("odds_min", 1.00)
        o35_odds_max = o35_filters.get("odds_max", 10.00)
        o35_value_min = o35_filters.get("value_min", 0)
        o35_value_max = o35_filters.get("value_max", 100)

        # Over 4.5 Goals
        o45_filters = request_data.get("o45_filters", {})
        include_o45 = o45_filters.get("include", True)
        o45_prob_min = o45_filters.get("probability_min", 0)
        o45_prob_max = o45_filters.get("probability_max", 100)
        o45_odds_min = o45_filters.get("odds_min", 1.00)
        o45_odds_max = o45_filters.get("odds_max", 10.00)
        o45_value_min = o45_filters.get("value_min", 0)
        o45_value_max = o45_filters.get("value_max", 100)

        # Under 1.5 Goals
        u15_filters = request_data.get("u15_filters", {})
        include_u15 = u15_filters.get("include", True)
        u15_prob_min = u15_filters.get("probability_min", 0)
        u15_prob_max = u15_filters.get("probability_max", 100)
        u15_odds_min = u15_filters.get("odds_min", 1.00)
        u15_odds_max = u15_filters.get("odds_max", 10.00)
        u15_value_min = u15_filters.get("value_min", 0)
        u15_value_max = u15_filters.get("value_max", 100)

        # Under 2.5 Goals
        u25_filters = request_data.get("u25_filters", {})
        include_u25 = u25_filters.get("include", True)
        u25_prob_min = u25_filters.get("probability_min", 0)
        u25_prob_max = u25_filters.get("probability_max", 100)
        u25_odds_min = u25_filters.get("odds_min", 1.00)
        u25_odds_max = u25_filters.get("odds_max", 10.00)
        u25_value_min = u25_filters.get("value_min", 0)
        u25_value_max = u25_filters.get("value_max", 100)

        # Under 3.5 Goals
        u35_filters = request_data.get("u35_filters", {})
        include_u35 = u35_filters.get("include", True)
        u35_prob_min = u35_filters.get("probability_min", 0)
        u35_prob_max = u35_filters.get("probability_max", 100)
        u35_odds_min = u35_filters.get("odds_min", 1.00)
        u35_odds_max = u35_filters.get("odds_max", 10.00)
        u35_value_min = u35_filters.get("value_min", 0)
        u35_value_max = u35_filters.get("value_max", 100)

        # Under 4.5 Goals
        u45_filters = request_data.get("u45_filters", {})
        include_u45 = u45_filters.get("include", True)
        u45_prob_min = u45_filters.get("probability_min", 0)
        u45_prob_max = u45_filters.get("probability_max", 100)
        u45_odds_min = u45_filters.get("odds_min", 1.00)
        u45_odds_max = u45_filters.get("odds_max", 10.00)
        u45_value_min = u45_filters.get("value_min", 0)
        u45_value_max = u45_filters.get("value_max", 100)

        # BTTS
        btts_filters = request_data.get("btts_filters", {})
        include_btts = btts_filters.get("include", True)
        btts_prob_min = btts_filters.get("probability_min", 0)
        btts_prob_max = btts_filters.get("probability_max", 100)
        btts_odds_min = btts_filters.get("odds_min", 1.00)
        btts_odds_max = btts_filters.get("odds_max", 10.00)
        btts_value_min = btts_filters.get("value_min", 0)
        btts_value_max = btts_filters.get("value_max", 100)

        # Home Over 1.5 Goals
        home_o15_filters = request_data.get("home_o15_filters", {})
        include_home_o15 = home_o15_filters.get("include", True)
        home_o15_prob_min = home_o15_filters.get("probability_min", 0)
        home_o15_prob_max = home_o15_filters.get("probability_max", 100)
        home_o15_odds_min = home_o15_filters.get("odds_min", 1.00)
        home_o15_odds_max = home_o15_filters.get("odds_max", 10.00)
        home_o15_value_min = home_o15_filters.get("value_min", 0)
        home_o15_value_max = home_o15_filters.get("value_max", 100)

        # Away Over 1.5 Goals
        away_o15_filters = request_data.get("away_o15_filters", {})
        include_away_o15 = away_o15_filters.get("include", True)
        away_o15_prob_min = away_o15_filters.get("probability_min", 0)
        away_o15_prob_max = away_o15_filters.get("probability_max", 100)
        away_o15_odds_min = away_o15_filters.get("odds_min", 1.00)
        away_o15_odds_max = away_o15_filters.get("odds_max", 10.00)
        away_o15_value_min = away_o15_filters.get("value_min", 0)
        away_o15_value_max = away_o15_filters.get("value_max", 100)

        # Over 8.5 Corners
        o85_filters = request_data.get("o85_filters", {})
        include_o85 = o85_filters.get("include", True)
        o85_prob_min = o85_filters.get("probability_min", 0)
        o85_prob_max = o85_filters.get("probability_max", 100)
        o85_odds_min = o85_filters.get("odds_min", 1.00)
        o85_odds_max = o85_filters.get("odds_max", 10.00)
        o85_value_min = o85_filters.get("value_min", 0)
        o85_value_max = o85_filters.get("value_max", 100)

        # Return empty if no filters at all
        if not selected_bookmakers and not selected_predictability and not selected_markets and not exclude_cups and not exclude_friendlies:
            return jsonify([])  # Return empty list if no filters are applied

        # Get cached data
        value_bets_data = filtered_bets

        table_data = []
        for bet in value_bets_data:
            # Ensure "odds" key exists and has valid data
            if "odds" not in bet or not isinstance(bet["odds"], list) or len(bet["odds"]) == 0:
                continue  # Skip if no valid odds data

            # Apply exclusion filters
            if exclude_cups and bet["competition"].get("is_cup", False):
                continue  # Skip cup games
            if exclude_friendlies and bet["competition"].get("is_friendly", False):
                continue  # Skip friendly games

            # 🟢 Apply Season Progress filter
            comp_progress = bet.get("competition", {}).get("progress")
            if comp_progress is not None:
                try:
                    comp_progress = float(comp_progress)
                except Exception:
                    continue  # skip if not numeric
                if not (sp_min <= comp_progress <= sp_max):
                    continue  # outside range → skip this bet

            # Filter odds based on selected bookmakers and remove negative values
            filtered_odds = [
                odd for odd in bet["odds"]
                if "bookmaker_name" in odd
                and odd["bookmaker_name"] in selected_bookmakers
                and float(odd.get("value", 0)) >= 0
            ]
            if not filtered_odds:
                continue

            # Find the bookmaker with the highest latest odds from selected bookmakers
            best_odds = max(filtered_odds, key=lambda x: float(x.get("latest", 0)))
            bookmaker_name = best_odds.get("bookmaker_name", "N/A")
            latest_odds = float(best_odds.get("latest", 0))
            value_percentage = float(best_odds.get("value", 0))

            predictability = str(bet["competition"].get("predictability", "Unknown")).capitalize()
            if selected_predictability and predictability not in selected_predictability:
                continue

            # Filtering per market
            if include_home_win and bet["market"] == "home_win_probability":
                probability = bet.get("probability", 0)
                if not (home_win_prob_min <= probability <= home_win_prob_max):
                    continue
                if not (home_win_odds_min <= latest_odds <= home_win_odds_max):
                    continue
                if not (home_win_value_min <= value_percentage <= home_win_value_max):
                    continue
            elif not include_home_win and bet["market"] == "home_win_probability":
                continue

            if include_draw and bet["market"] == "draw_probability":
                probability = bet.get("probability", 0)
                if not (draw_prob_min <= probability <= draw_prob_max):
                    continue
                if not (draw_odds_min <= latest_odds <= draw_odds_max):
                    continue
                if not (draw_value_min <= value_percentage <= draw_value_max):
                    continue
            elif not include_draw and bet["market"] == "draw_probability":
                continue

            if include_away_win and bet["market"] == "away_win_probability":
                probability = bet.get("probability", 0)
                if not (away_win_prob_min <= probability <= away_win_prob_max):
                    continue
                if not (away_win_odds_min <= latest_odds <= away_win_odds_max):
                    continue
                if not (away_win_value_min <= value_percentage <= away_win_value_max):
                    continue
            elif not include_away_win and bet["market"] == "away_win_probability":
                continue

            # HT Home Win
            if include_home_win_ht and bet["market"] == "home_win_ht_probability":
                probability = bet.get("probability", 0)
                if not (home_win_ht_prob_min <= probability <= home_win_ht_prob_max):
                    continue
                if not (home_win_ht_odds_min <= latest_odds <= home_win_ht_odds_max):
                    continue
                if not (home_win_ht_value_min <= value_percentage <= home_win_ht_value_max):
                    continue
            elif not include_home_win_ht and bet["market"] == "home_win_ht_probability":
                continue

            # HT Draw
            if include_draw_ht and bet["market"] == "draw_ht_probability":
                probability = bet.get("probability", 0)
                if not (draw_ht_prob_min <= probability <= draw_ht_prob_max):
                    continue
                if not (draw_ht_odds_min <= latest_odds <= draw_ht_odds_max):
                    continue
                if not (draw_ht_value_min <= value_percentage <= draw_ht_value_max):
                    continue
            elif not include_draw_ht and bet["market"] == "draw_ht_probability":
                continue

            # HT Away Win
            if include_away_win_ht and bet["market"] == "away_win_ht_probability":
                probability = bet.get("probability", 0)
                if not (away_win_ht_prob_min <= probability <= away_win_ht_prob_max):
                    continue
                if not (away_win_ht_odds_min <= latest_odds <= away_win_ht_odds_max):
                    continue
                if not (away_win_ht_value_min <= value_percentage <= away_win_ht_value_max):
                    continue
            elif not include_away_win_ht and bet["market"] == "away_win_ht_probability":
                continue

            # Over 1.5 Goals
            if include_o15 and bet["market"] == "o15_probability":
                probability = bet.get("probability", 0)
                if not (o15_prob_min <= probability <= o15_prob_max):
                    continue
                if not (o15_odds_min <= latest_odds <= o15_odds_max):
                    continue
                if not (o15_value_min <= value_percentage <= o15_value_max):
                    continue
            elif not include_o15 and bet["market"] == "o15_probability":
                continue

            # Over 2.5 Goals
            if include_o25 and bet["market"] == "o25_probability":
                probability = bet.get("probability", 0)
                if not (o25_prob_min <= probability <= o25_prob_max):
                    continue
                if not (o25_odds_min <= latest_odds <= o25_odds_max):
                    continue
                if not (o25_value_min <= value_percentage <= o25_value_max):
                    continue
            elif not include_o25 and bet["market"] == "o25_probability":
                continue

            # Over 3.5 Goals
            if include_o35 and bet["market"] == "o35_probability":
                probability = bet.get("probability", 0)
                if not (o35_prob_min <= probability <= o35_prob_max):
                    continue
                if not (o35_odds_min <= latest_odds <= o35_odds_max):
                    continue
                if not (o35_value_min <= value_percentage <= o35_value_max):
                    continue
            elif not include_o35 and bet["market"] == "o35_probability":
                continue

            # Over 4.5 Goals
            if include_o45 and bet["market"] == "o45_probability":
                probability = bet.get("probability", 0)
                if not (o45_prob_min <= probability <= o45_prob_max):
                    continue
                if not (o45_odds_min <= latest_odds <= o45_odds_max):
                    continue
                if not (o45_value_min <= value_percentage <= o45_value_max):
                    continue
            elif not include_o45 and bet["market"] == "o45_probability":
                continue

            # Under 1.5 Goals
            if include_u15 and bet["market"] == "u15_probability":
                probability = bet.get("probability", 0)
                if not (u15_prob_min <= probability <= u15_prob_max):
                    continue
                if not (u15_odds_min <= latest_odds <= u15_odds_max):
                    continue
                if not (u15_value_min <= value_percentage <= u15_value_max):
                    continue
            elif not include_u15 and bet["market"] == "u15_probability":
                continue

            # Under 2.5 Goals
            if include_u25 and bet["market"] == "u25_probability":
                probability = bet.get("probability", 0)
                if not (u25_prob_min <= probability <= u25_prob_max):
                    continue
                if not (u25_odds_min <= latest_odds <= u25_odds_max):
                    continue
                if not (u25_value_min <= value_percentage <= u25_value_max):
                    continue
            elif not include_u25 and bet["market"] == "u25_probability":
                continue

            # Under 3.5 Goals
            if include_u35 and bet["market"] == "u35_probability":
                probability = bet.get("probability", 0)
                if not (u35_prob_min <= probability <= u35_prob_max):
                    continue
                if not (u35_odds_min <= latest_odds <= u35_odds_max):
                    continue
                if not (u35_value_min <= value_percentage <= u35_value_max):
                    continue
            elif not include_u35 and bet["market"] == "u35_probability":
                continue

            # Under 4.5 Goals
            if include_u45 and bet["market"] == "u45_probability":
                probability = bet.get("probability", 0)
                if not (u45_prob_min <= probability <= u45_prob_max):
                    continue
                if not (u45_odds_min <= latest_odds <= u45_odds_max):
                    continue
                if not (u45_value_min <= value_percentage <= u45_value_max):
                    continue
            elif not include_u45 and bet["market"] == "u45_probability":
                continue

            # BTTS
            if include_btts and bet["market"] == "btts_probability":
                probability = bet.get("probability", 0)
                if not (btts_prob_min <= probability <= btts_prob_max):
                    continue
                if not (btts_odds_min <= latest_odds <= btts_odds_max):
                    continue
                if not (btts_value_min <= value_percentage <= btts_value_max):
                    continue
            elif not include_btts and bet["market"] == "btts_probability":
                continue

            # Home Over 1.5
            if include_home_o15 and bet["market"] == "home_goals_15_probability":
                probability = bet.get("probability", 0)
                if not (home_o15_prob_min <= probability <= home_o15_prob_max):
                    continue
                if not (home_o15_odds_min <= latest_odds <= home_o15_odds_max):
                    continue
                if not (home_o15_value_min <= value_percentage <= home_o15_value_max):
                    continue
            elif not include_home_o15 and bet["market"] == "home_goals_15_probability":
                continue

            # Away Over 1.5
            if include_away_o15 and bet["market"] == "away_goals_15_probability":
                probability = bet.get("probability", 0)
                if not (away_o15_prob_min <= probability <= away_o15_prob_max):
                    continue
                if not (away_o15_odds_min <= latest_odds <= away_o15_odds_max):
                    continue
                if not (away_o15_value_min <= value_percentage <= away_o15_value_max):
                    continue
            elif not include_away_o15 and bet["market"] == "away_goals_15_probability":
                continue

            # Over 8.5 Corners
            if include_o85 and bet["market"] == "o85_corners_probability":
                probability = bet.get("probability", 0)
                if not (o85_prob_min <= probability <= o85_prob_max):
                    continue
                if not (o85_odds_min <= latest_odds <= o85_odds_max):
                    continue
                if not (o85_value_min <= value_percentage <= o85_value_max):
                    continue
            elif not include_o85 and bet["market"] == "o85_corners_probability":
                continue

            # Format data for table
            table_data.append({
                "market": bet["market"],
                "home_name": bet["home_name"],
                "away_name": bet["away_name"],
                "ko_human": bet["ko_human"],
                "competition_country": bet["competition"]["country"],
                "competition_name": bet["competition"]["name"],
                "competition_predictability": predictability,
                "probability": round(bet["probability"], 2) if bet["probability"] is not None else "N/A",
                "implied_odds": round(1 / (bet["probability"] / 100), 2) if bet["probability"] > 0 else "N/A",
                "bookmaker": bookmaker_name,
                "latest_odds": latest_odds,
                "value_percentage": value_percentage,
                "fixture_id": bet["id"],
                "home_played": bet.get("home_played"),
                "away_played": bet.get("away_played"),
                "competition_progress": bet.get("competition", {}).get("progress"),
            })

        # 🟢 Debug print statements at the end, just before returning:
        print(f"[DEBUG] Returning {len(table_data)} bets in filter_value_bets")
        if table_data:
            print("[DEBUG] First bet returned:", table_data[0])
        else:
            print("[DEBUG] No bets returned")
        return jsonify(table_data)

    except Exception as e:
        print(f"🚨 Error in /filter_value_bets: {e}")
        return jsonify({"error": str(e)}), 500

# ---------- Best Bets ----------

@app.route('/best-bets')
def best_bets():
    # markets to check (now includes Over 1.5 Goals)
    markets = request.args.getlist('market') or ['over_1_goals', 'over_2_goals', 'over_3_goals', 'home_win', 'over_0_5_half_goals']

    # value-only toggle (?value_only=1)
    def _as_bool(v):
        return str(v).lower() in ("1", "true", "on", "yes")
    value_only = _as_bool(request.args.get('value_only', '0'))

    # choose which season cache to use (default: season)
    stats_mode = request.args.get('stats', 'season')  # 'season' or 'last25'
    season_cache = _load_season_cache(stats_mode)

    # time window (default next 72 hours)
    london_tz = pytz.timezone('Europe/London')
    now_london = datetime.now(london_tz)
    hours = request.args.get('hours')
    days = request.args.get('days')
    window_delta = timedelta(hours=72)
    try:
        if days is not None:
            window_delta = timedelta(days=int(days))
        if hours is not None:
            window_delta = timedelta(hours=int(hours))
    except Exception:
        pass
    end_london = now_london + window_delta

    # always read the latest cache from disk
    try:
        with open(GAME_DETAILS_CACHE_FILE, 'r', encoding='utf-8') as f:
            game_data = json.load(f)
    except Exception as e:
        return f"Failed to load game details cache: {e}", 500

    results = []
    for fid, fd in game_data.items():
        if not isinstance(fd, dict):
            continue

        ko_unix = fd.get('unix')
        try:
            ko_time = datetime.fromtimestamp(ko_unix, pytz.utc).astimezone(london_tz)
        except Exception:
            continue

        if not (now_london <= ko_time <= end_london):
            continue

        # ✅ Skip anything not "High" or "Good" predictability
        predict = (fd.get("competition_predictability") or "").strip().lower()
        if predict not in ("high", "good"):
            continue

        season_id = fd.get('season_id')
        home_id = fd.get('home_id')
        away_id = fd.get('away_id')

        for m in markets:
            mdata = fd.get(m)
            if not isinstance(mdata, dict):
                continue
            prob = mdata.get('probability')
            if not isinstance(prob, (int, float)):
                continue

            hrow = _find_team_row(season_cache, season_id, home_id)
            arow = _find_team_row(season_cache, season_id, away_id)

            # convert odds safely (needs to happen before the 1H gate)
            def _to_float(v):
                if v in (None, "", "N/A", "NA", "NaN", "-", "—"):
                    return None
                try:
                    return float(v)
                except Exception:
                    return None

            implied_val = _to_float(mdata.get("implied_odds"))
            actual_val = _to_float(mdata.get("actual_odds"))
            if actual_val is None:
                continue

            # ✅ Market-specific stat gates
            if m == 'over_1_goals':
                passes_stats = _passes_over15_gate(hrow, arow, prob)
            elif m == 'over_2_goals':
                passes_stats = _passes_over25_gate(hrow, arow, prob)
            elif m == 'over_3_goals':
                passes_stats = _passes_over35_gate(hrow, arow, prob)
            elif m == 'home_win':
                passes_stats = _passes_homewin_gate(hrow, arow, prob)
            elif m == "over_0_5_half_goals":
                passes_stats = _passes_1h_over05_gate(hrow, arow, prob, actual_val)
            else:
                passes_stats = False

            if not passes_stats:
                continue

            if value_only:
                if implied_val is None or not (actual_val > implied_val):
                    continue


            row = {
                "fixture_id": int(fid),
                "fixture_name": fd.get("fixture_name", fid),
                "competition_name": fd.get("competition_name", "N/A"),
                "competition_country": fd.get("competition_country", "N/A"),
                "predictability": fd.get("competition_predictability", "N/A").capitalize(),
                "market": m,
                "probability": round(prob, 2),
                "implied_odds": implied_val,
                "actual_odds": actual_val,
                "kickoff_unix": ko_unix,
            }

            # ✅ Extract relevant season stats depending on market
            if m == "over_1_goals":
                row["season_stats"] = _extract_over_stats(hrow, arow, over_key="o1")
            elif m == "over_2_goals":
                row["season_stats"] = _extract_over_stats(hrow, arow, over_key="o2")
            elif m == "over_3_goals":
                row["season_stats"] = _extract_over_stats(hrow, arow, over_key="o3")
            elif m == "home_win":
                row["season_stats"] = _extract_homewin_stats(hrow, arow)
            elif m == "over_0_5_half_goals":
                row["season_stats"] = _extract_1h_over_stats(hrow, arow)

            results.append(row)

    # sort by earliest kickoff, then probability
    results.sort(key=lambda x: (x.get("kickoff_unix", 0), -x.get("probability", 0)))

    # ✅ Split into four tables by market
    results_by_market = {
        "over_1_goals": [],
        "over_2_goals": [],
        "over_3_goals": [],
        "home_win": [],
        "over_0_5_half_goals": [],
    }

    for r in results:
        if r["market"] in results_by_market:
            results_by_market[r["market"]].append(r)

    # Sort each market section again
    for k in results_by_market:
        results_by_market[k].sort(key=lambda x: (x.get("kickoff_unix", 0), -x.get("probability", 0)))

    # ✅ Market display names
    market_labels = {
        "over_1_goals": "Over 1.5 Goals",
        "over_2_goals": "Over 2.5 Goals",
        "over_3_goals": "Over 3.5 Goals",
        "home_win": "Home Win",
        "over_0_5_half_goals": "First Half Goals (Over 0.5)",
    }

    return render_template(
        'best_bets.html',
        rows_by_market=results_by_market,
        market_labels=market_labels,
        value_only=value_only
    )

# --- Season stats helpers for Best Bets ---

def _load_season_cache(stats_mode="season"):
    cache_file = SEASON_STATS_CACHE_FILE if stats_mode == "season" else SEASON_STATS_CACHE_FILE_LAST25
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def _find_team_row(season_cache: dict, season_id: int, team_id: int):
    if not isinstance(season_cache, dict):
        return None
    bucket = season_cache.get(str(season_id)) or season_cache.get(season_id)
    if not bucket or not isinstance(bucket, dict):
        return None
    rows = bucket.get("data")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and str(row.get("team_id")) == str(team_id):
            return row
    return None

def _get_nested(d, path, default=None):
    cur = d
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur

def save_json_atomic(filepath, data):
    tmp_path = filepath + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, filepath)


# ---------------- Gates (probability + stats inside each) ---------------- #

def _passes_1h_over05_gate(home_row, away_row, prob, actual_odds):
    """
    First Half Goals (Over 0.5) gate.
    Requirements you specified:
      - prob >= 70%
      - home team (home split) >= 70% with >= 5 home games
      - away team (away split) >= 70% with >= 5 away games
      - BOTH teams overall >= 70%
      - actual odds >= 1.25
    Season stats source:
      home_row["goals_1h_over"]["o0"] and away_row["goals_1h_over"]["o0"]
    """
    prob_threshold = 70.0
    home_home_threshold = 70.0
    away_away_threshold = 70.0
    overall_threshold = 70.0
    min_home_games = 5
    min_away_games = 5
    min_odds = 1.25

    # probability + odds checks
    if prob is None or prob < prob_threshold:
        return False
    if actual_odds is None or actual_odds < min_odds:
        return False

    if not home_row or not away_row:
        return False

    # sample sizes (from goals_1h_over.o0 counts)
    h_home_played = _get_nested(home_row, "goals_1h_over.o0.home", 0)
    a_away_played = _get_nested(away_row, "goals_1h_over.o0.away", 0)
    if h_home_played < min_home_games or a_away_played < min_away_games:
        return False

    # % splits + overall
    h_home_pct = float(_get_nested(home_row, "goals_1h_over.o0.home_percentage", 0) or 0)
    a_away_pct = float(_get_nested(away_row, "goals_1h_over.o0.away_percentage", 0) or 0)
    h_total_pct = float(_get_nested(home_row, "goals_1h_over.o0.total_percentage", 0) or 0)
    a_total_pct = float(_get_nested(away_row, "goals_1h_over.o0.total_percentage", 0) or 0)

    return (
        h_home_pct >= home_home_threshold and
        a_away_pct >= away_away_threshold and
        h_total_pct >= overall_threshold and
        a_total_pct >= overall_threshold
    )


def _passes_over15_gate(home_row, away_row, prob):
    """Over 1.5 Goals gate: internal thresholds."""
    prob_threshold = 70.0
    home_home_threshold = 80.0
    away_away_threshold = 80.0
    overall_threshold = 70.0
    min_home_away_games = 5
    min_overall_games = 10

    if prob < prob_threshold:
        return False
    if not home_row or not away_row:
        return False

    h_played_home = _get_nested(home_row, "played.home", 0)
    a_played_away = _get_nested(away_row, "played.away", 0)
    h_played_total = _get_nested(home_row, "played.total", 0)
    a_played_total = _get_nested(away_row, "played.total", 0)

    if h_played_home < min_home_away_games or a_played_away < min_home_away_games:
        return False
    if h_played_total < min_overall_games or a_played_total < min_overall_games:
        return False

    h_home_o15 = float(_get_nested(home_row, "goals_over.o1.home_percentage", 0))
    a_away_o15 = float(_get_nested(away_row, "goals_over.o1.away_percentage", 0))
    h_total_o15 = float(_get_nested(home_row, "goals_over.o1.total_percentage", 0))
    a_total_o15 = float(_get_nested(away_row, "goals_over.o1.total_percentage", 0))

    return (
        h_home_o15 >= home_home_threshold and
        a_away_o15 >= away_away_threshold and
        h_total_o15 >= overall_threshold and
        a_total_o15 >= overall_threshold
    )


def _passes_over25_gate(home_row, away_row, prob):
    """Over 2.5 Goals gate: internal thresholds."""
    prob_threshold = 65.0
    home_home_threshold = 70.0
    away_away_threshold = 70.0
    overall_threshold = 60.0
    min_home_away_games = 5
    min_overall_games = 10

    if prob < prob_threshold:
        return False
    if not home_row or not away_row:
        return False

    h_played_home = _get_nested(home_row, "played.home", 0)
    a_played_away = _get_nested(away_row, "played.away", 0)
    h_played_total = _get_nested(home_row, "played.total", 0)
    a_played_total = _get_nested(away_row, "played.total", 0)

    if h_played_home < min_home_away_games or a_played_away < min_home_away_games:
        return False
    if h_played_total < min_overall_games or a_played_total < min_overall_games:
        return False

    h_home_o25 = float(_get_nested(home_row, "goals_over.o2.home_percentage", 0))
    a_away_o25 = float(_get_nested(away_row, "goals_over.o2.away_percentage", 0))
    h_total_o25 = float(_get_nested(home_row, "goals_over.o2.total_percentage", 0))
    a_total_o25 = float(_get_nested(away_row, "goals_over.o2.total_percentage", 0))

    return (
        h_home_o25 >= home_home_threshold and
        a_away_o25 >= away_away_threshold and
        h_total_o25 >= overall_threshold and
        a_total_o25 >= overall_threshold
    )


def _passes_over35_gate(home_row, away_row, prob):
    """Over 3.5 Goals gate: internal thresholds."""
    prob_threshold = 55.0
    home_home_threshold = 50.0
    away_away_threshold = 50.0
    overall_threshold = 50.0
    min_home_away_games = 5
    min_overall_games = 10

    if prob < prob_threshold:
        return False
    if not home_row or not away_row:
        return False

    h_played_home = _get_nested(home_row, "played.home", 0)
    a_played_away = _get_nested(away_row, "played.away", 0)
    h_played_total = _get_nested(home_row, "played.total", 0)
    a_played_total = _get_nested(away_row, "played.total", 0)

    if h_played_home < min_home_away_games or a_played_away < min_home_away_games:
        return False
    if h_played_total < min_overall_games or a_played_total < min_overall_games:
        return False

    h_home_o35 = float(_get_nested(home_row, "goals_over.o3.home_percentage", 0))
    a_away_o35 = float(_get_nested(away_row, "goals_over.o3.away_percentage", 0))
    h_total_o35 = float(_get_nested(home_row, "goals_over.o3.total_percentage", 0))
    a_total_o35 = float(_get_nested(away_row, "goals_over.o3.total_percentage", 0))

    return (
        h_home_o35 >= home_home_threshold and
        a_away_o35 >= away_away_threshold and
        h_total_o35 >= overall_threshold and
        a_total_o35 >= overall_threshold
    )


def _passes_homewin_gate(home_row, away_row, prob):
    """Home Win gate: internal thresholds."""
    prob_threshold = 60.0
    home_win_home_threshold = 60.0
    away_loss_away_threshold = 50.0
    min_home_away_games = 5

    if prob < prob_threshold:
        return False
    if not home_row or not away_row:
        return False

    h_played_home = _get_nested(home_row, "played.home", 0)
    a_played_away = _get_nested(away_row, "played.away", 0)
    if h_played_home < min_home_away_games or a_played_away < min_home_away_games:
        return False

    h_home_win = float(_get_nested(home_row, "won.home_percentage", 0))
    a_away_loss = float(_get_nested(away_row, "lost.away_percentage", 0))

    return (
        h_home_win >= home_win_home_threshold and
        a_away_loss >= away_loss_away_threshold
    )


# -------- Optional: season stats extractors (handy for rendering the columns) --------

def _extract_1h_over_stats(home_row, away_row):
    """
    First Half Goals (Over 0.5) season stats extractor.
    Returns the SAME keys your tables already use:
      home_played_home, away_played_away, home_home_pct, away_away_pct,
      home_total_pct, away_total_pct
    But sourced from goals_1h_over.o0.*
    """
    if not home_row or not away_row:
        return {}

    stats = {
        # sample sizes from the goals_1h_over.o0 bucket
        "home_played_home": _get_nested(home_row, "goals_1h_over.o0.home", 0),
        "away_played_away": _get_nested(away_row, "goals_1h_over.o0.away", 0),
        "home_played_total": _get_nested(home_row, "goals_1h_over.o0.total", 0),
        "away_played_total": _get_nested(away_row, "goals_1h_over.o0.total", 0),

        # percentages
        "home_home_pct": _get_nested(home_row, "goals_1h_over.o0.home_percentage", 0) or 0,
        "away_away_pct": _get_nested(away_row, "goals_1h_over.o0.away_percentage", 0) or 0,
        "home_total_pct": _get_nested(home_row, "goals_1h_over.o0.total_percentage", 0) or 0,
        "away_total_pct": _get_nested(away_row, "goals_1h_over.o0.total_percentage", 0) or 0,
    }

    for k in ("home_home_pct", "away_away_pct", "home_total_pct", "away_total_pct"):
        try:
            stats[k] = round(float(stats[k]), 2)
        except Exception:
            stats[k] = 0.0

    return stats

def _extract_over_stats(home_row, away_row, over_key="o2"):
    """
    over_key: 'o2' = Over 2.5, 'o3' = Over 3.5
    Returns split + overall % and sample sizes used by the gates.
    """
    if not home_row or not away_row:
        return {}
    stats = {
        "home_played_home": _get_nested(home_row, "played.home", 0),
        "away_played_away": _get_nested(away_row, "played.away", 0),
        "home_played_total": _get_nested(home_row, "played.total", 0),
        "away_played_total": _get_nested(away_row, "played.total", 0),
        "home_home_pct": _get_nested(home_row, f"goals_over.{over_key}.home_percentage", 0) or 0,
        "away_away_pct": _get_nested(away_row, f"goals_over.{over_key}.away_percentage", 0) or 0,
        "home_total_pct": _get_nested(home_row, f"goals_over.{over_key}.total_percentage", 0) or 0,
        "away_total_pct": _get_nested(away_row, f"goals_over.{over_key}.total_percentage", 0) or 0,
    }
    # round numbers for display
    for k in ("home_home_pct","away_away_pct","home_total_pct","away_total_pct"):
        try: stats[k] = round(float(stats[k]), 2)
        except: stats[k] = 0.0
    return stats

def _extract_homewin_stats(home_row, away_row):
    """Home Win: home win% (home split) + away loss% (away split) and samples."""
    if not home_row or not away_row:
        return {}
    stats = {
        "home_played_home": _get_nested(home_row, "played.home", 0),
        "away_played_away": _get_nested(away_row, "played.away", 0),
        "home_win_home_pct": _get_nested(home_row, "won.home_percentage", 0) or 0,
        "away_loss_away_pct": _get_nested(away_row, "lost.away_percentage", 0) or 0,
    }
    for k in ("home_win_home_pct","away_loss_away_pct"):
        try: stats[k] = round(float(stats[k]), 2)
        except: stats[k] = 0.0
    return stats

@app.route("/ai-bets")
def ai_bets_page():
    """Render the AI Bets UI (ai_bets.html)."""
    return render_template("ai_bets.html")

# ---------------------------------
# Config – uses your Gmail + app password
# ---------------------------------
EMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "craigelliscorby@gmail.com")
EMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")  # app password you created
RECIPIENT_ADDRESS = EMAIL_ADDRESS  # send to yourself

# Labels for the three result markets
RESULT_MARKET_LABELS = {
    "home_win": "Home Win",
    "draw": "Draw",
    "away_win": "Away Win",
}


def _base_data_dir():
    """
    Single source of truth for data directory.
    Uses the same DATA_DIR everywhere (local + Render).
    """
    return DATA_DIR


def _load_game_details_cache():
    """
    Load game_details_cache.json from the same /data folder your app uses.
    """
    cache_path = os.path.join(_base_data_dir(), "game_details_cache.json")

    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[AI CSV] Could not load game_details_cache.json: {e}")
        return {}


def _load_season_stats_cache():
    """
    Load season_stats cache from data folder.

    Assumes shape:
        { "<season_id>": { "<team_id>": stats_dict } }
    """
    cache_path = os.path.join(_base_data_dir(), "season_stats_cache.json")
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[AI CSV] Could not load season_stats_cache.json: {e}")
        return {}


def _safe_get(d, *keys):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _diff(a, b):
    if a is None or b is None:
        return ""
    try:
        return round(float(a) - float(b), 2)
    except Exception:
        return ""


def _extract_team_features(stats):
    """
    Given one team's season stats dict, return all the per-game features we need.
    If stats is missing, everything returns None so CSV will get empty strings.
    """
    if not isinstance(stats, dict):
        stats = {}

    points_pg = _safe_get(stats, "points", "total_avg")
    gf_pg = _safe_get(stats, "goals_for", "total_avg")
    ga_pg = _safe_get(stats, "goals_against", "total_avg")
    shots_pg = _safe_get(stats, "shots_for", "total_avg")
    sot_pg = _safe_get(stats, "shots_on_for", "total_avg")
    xg_pg = _safe_get(stats, "xg_for", "total_avg")
    da_pg = _safe_get(stats, "dangerous_attacks_for", "total_avg")
    scored_first_pct = _safe_get(stats, "scored_first", "total_percentage")
    clean_sheet_pct = _safe_get(stats, "clean_sheet", "total_percentage")
    possession_pct = _safe_get(stats, "possession_for", "total_avg")

    return {
        "points_pg": points_pg,
        "goals_for_pg": gf_pg,
        "goals_against_pg": ga_pg,
        "shots_pg": shots_pg,
        "sot_pg": sot_pg,
        "xg_pg": xg_pg,
        "dangerous_attacks_pg": da_pg,
        "scored_first_pct": scored_first_pct,
        "clean_sheet_pct": clean_sheet_pct,
        "possession_pct": possession_pct,
    }


def _build_result_rows_from_bets(bets):
    """
    Take today's 6 AI bets, work out which fixtures they are,
    then for each fixture create up to 3 rows:
      home_win, draw, away_win
    using data from game_details_cache.json + season_stats_cache.json
    """
    game_details = _load_game_details_cache()

    # use the existing season cache helper from app.py
    season_cache = _load_season_cache("season")

    london_tz = pytz.timezone("Europe/London")
    rows = []
    seen_fixtures = set()

    def _safe_float(v):
        try:
            return float(v)
        except Exception:
            return None

    def _safe_round(v, nd=2):
        try:
            return round(float(v), nd)
        except Exception:
            return ""

    for bet in bets:
        fixture_id = str(bet.get("fixture_id"))
        if not fixture_id or fixture_id in seen_fixtures:
            continue  # only once per fixture
        seen_fixtures.add(fixture_id)

        fixture_name = bet.get("fixture_name")
        competition_name = bet.get("competition_name")
        competition_country = bet.get("competition_country")
        competition_predictability = bet.get("competition_predictability")
        unix_ts = bet.get("unix")
        season_id = bet.get("season_id")
        home_id = bet.get("home_id")
        away_id = bet.get("away_id")

        # --- kickoff date / hour in London ---
        kickoff_date = ""
        kickoff_hour = ""
        if unix_ts:
            try:
                dt = datetime.fromtimestamp(unix_ts, pytz.utc).astimezone(london_tz)
                kickoff_date = dt.strftime("%Y-%m-%d")
                kickoff_hour = dt.strftime("%H:%M")
            except Exception:
                pass

        # --- season stats rows for this fixture's teams ---
        home_stats = _find_team_row(season_cache, season_id, home_id) if season_id and home_id else None
        away_stats = _find_team_row(season_cache, season_id, away_id) if season_id and away_id else None

        # pull the per-game stats only once per fixture
        if home_stats and away_stats:

            # games played (home / away)
            h_played_home = _safe_float(_get_nested(home_stats, "played.home"))
            h_played_away = _safe_float(_get_nested(home_stats, "played.away"))
            a_played_home = _safe_float(_get_nested(away_stats, "played.home"))
            a_played_away = _safe_float(_get_nested(away_stats, "played.away"))

            # percentage won,drew,lost
            a_aw = _safe_float(_get_nested(away_stats, "won.away_percentage"))
            a_ad = _safe_float(_get_nested(away_stats, "drawn.away_percentage"))
            a_al = _safe_float(_get_nested(away_stats, "lost.away_percentage"))

            # points per game (home at home, away away)
            hp = _safe_float(_get_nested(home_stats, "points.home_avg"))
            ap = _safe_float(_get_nested(away_stats, "points.away_avg"))

            # goals for per game
            hgf = _safe_float(_get_nested(home_stats, "goals_for.home_avg"))
            agf = _safe_float(_get_nested(away_stats, "goals_for.away_avg"))

            # goals against per game
            hga = _safe_float(_get_nested(home_stats, "goals_against.home_avg"))
            aga = _safe_float(_get_nested(away_stats, "goals_against.away_avg"))

            # shots per game
            hshots = _safe_float(_get_nested(home_stats, "shots_for.home_avg"))
            ashots = _safe_float(_get_nested(away_stats, "shots_for.away_avg"))

            # shots on target per game
            hsot = _safe_float(_get_nested(home_stats, "shots_on_for.home_avg"))
            asot = _safe_float(_get_nested(away_stats, "shots_on_for.away_avg"))

            # xG per game
            hxg = _safe_float(_get_nested(home_stats, "xg_for.home_avg"))
            axg = _safe_float(_get_nested(away_stats, "xg_for.away_avg"))

            # dangerous attacks per game
            hda = _safe_float(_get_nested(home_stats, "dangerous_attacks_for.home_avg"))
            ada = _safe_float(_get_nested(away_stats, "dangerous_attacks_for.away_avg"))

            # scored first %
            hsf = _safe_float(_get_nested(home_stats, "scored_first.home_percentage"))
            asf = _safe_float(_get_nested(away_stats, "scored_first.away_percentage"))

            # clean sheet %
            hcs = _safe_float(_get_nested(home_stats, "clean_sheet.home_percentage"))
            acs = _safe_float(_get_nested(away_stats, "clean_sheet.away_percentage"))

            # possession %
            hpos = _safe_float(_get_nested(home_stats, "possession_for.home_avg"))
            apos = _safe_float(_get_nested(away_stats, "possession_for.away_avg"))

            h_hw = _safe_float(_get_nested(home_stats, "won.home_percentage"))
            h_hd = _safe_float(_get_nested(home_stats, "drawn.home_percentage"))
            h_hl = _safe_float(_get_nested(home_stats, "lost.home_percentage"))

            # diffs (home – away)
            def _diff(a, b):
                if a is None or b is None:
                    return ""
                return round(a - b, 2)

            stats_block = {


                # NEW: games played
                "home_games_played_home": _safe_round(h_played_home, 0),
                "home_games_played_away": _safe_round(h_played_away, 0),
                "away_games_played_home": _safe_round(a_played_home, 0),
                "away_games_played_away": _safe_round(a_played_away, 0),

                # NEW: raw W/D/L percentages
                "home_home_win_pct": _safe_round(h_hw),
                "home_home_draw_pct": _safe_round(h_hd),
                "home_home_lose_pct": _safe_round(h_hl),
                "away_away_win_pct": _safe_round(a_aw),
                "away_away_draw_pct": _safe_round(a_ad),
                "away_away_lose_pct": _safe_round(a_al),

                "home_points_pg": _safe_round(hp),
                "away_points_pg": _safe_round(ap),
                "points_pg_diff": _diff(hp, ap),

                "home_goals_for_pg": _safe_round(hgf),
                "away_goals_for_pg": _safe_round(agf),
                "goals_for_pg_diff": _diff(hgf, agf),

                "home_goals_against_pg": _safe_round(hga),
                "away_goals_against_pg": _safe_round(aga),
                "goals_against_pg_diff": _diff(hga, aga),

                "home_shots_pg": _safe_round(hshots),
                "away_shots_pg": _safe_round(ashots),
                "shots_pg_diff": _diff(hshots, ashots),

                "home_sot_pg": _safe_round(hsot),
                "away_sot_pg": _safe_round(asot),
                "sot_pg_diff": _diff(hsot, asot),

                "home_xg_pg": _safe_round(hxg),
                "away_xg_pg": _safe_round(axg),
                "xg_pg_diff": _diff(hxg, axg),

                "home_dangerous_attacks_pg": _safe_round(hda),
                "away_dangerous_attacks_pg": _safe_round(ada),
                "dangerous_attacks_diff": _diff(hda, ada),

                "home_scored_first_pct": _safe_round(hsf),
                "away_scored_first_pct": _safe_round(asf),
                "scored_first_diff": _diff(hsf, asf),

                "home_clean_sheet_pct": _safe_round(hcs),
                "away_clean_sheet_pct": _safe_round(acs),
                "clean_sheet_diff": _diff(hcs, acs),

                "home_possession_pct": _safe_round(hpos),
                "away_possession_pct": _safe_round(apos),
                "possession_diff": _diff(hpos, apos),

            }
        else:
            # no season stats – leave everything blank
            stats_block = {k: "" for k in [
                "home_games_played_home", "home_games_played_away",
                "away_games_played_home", "away_games_played_away",
                "home_home_win_pct", "home_home_draw_pct", "home_home_lose_pct",
                "away_away_win_pct", "away_away_draw_pct", "away_away_lose_pct",
                "home_points_pg", "away_points_pg", "points_pg_diff",
                "home_goals_for_pg", "away_goals_for_pg", "goals_for_pg_diff",
                "home_goals_against_pg", "away_goals_against_pg", "goals_against_pg_diff",
                "home_shots_pg", "away_shots_pg", "shots_pg_diff",
                "home_sot_pg", "away_sot_pg", "sot_pg_diff",
                "home_xg_pg", "away_xg_pg", "xg_pg_diff",
                "home_dangerous_attacks_pg", "away_dangerous_attacks_pg", "dangerous_attacks_diff",
                "home_scored_first_pct", "away_scored_first_pct", "scored_first_diff",
                "home_clean_sheet_pct", "away_clean_sheet_pct", "clean_sheet_diff",
                "home_possession_pct", "away_possession_pct", "possession_diff",
            ]}

        markets_for_fixture = game_details.get(fixture_id, {})

        for mk in ("home_win", "draw", "away_win"):
            md = markets_for_fixture.get(mk)
            if not isinstance(md, dict):
                continue

            prob = md.get("probability")
            model_probability = _safe_round(prob)

            # choose a bookmaker odds field
            book_odds_raw = (
                md.get("actual_odds")
                or md.get("onexbet_odds")
                or md.get("betfair_exchange_odds")
                or md.get("pinnacle_odds")
            )
            book_odds = _safe_float(book_odds_raw)

            implied_prob_book = ""
            edge = ""
            if model_probability != "" and book_odds and book_odds > 0:
                implied_prob_book_f = 100.0 / book_odds
                implied_prob_book = round(implied_prob_book_f, 2)
                edge = round(float(model_probability) - implied_prob_book_f, 2)

            row = {
                "fixture_id": fixture_id,
                "fixture_name": fixture_name,
                "competition_country": competition_country,
                "competition_name": competition_name,
                "competition_predictability": competition_predictability,
                "market_type": mk,  # keep simple for now
                "model_probability": model_probability,
                "actual_odds": book_odds,
                "implied_prob_bookmaker": implied_prob_book,
                "edge": edge,
                "kickoff_date": kickoff_date,
                "kickoff_hour": kickoff_hour,
                "won": "",
                "profit_units": "",
            }

            # merge in season stats columns
            row.update(stats_block)

            rows.append(row)

    return rows


def _build_csv_bytes(rows):
    """
    Turn a list of dict rows into CSV bytes in memory.
    """
    fieldnames = [
        "fixture_id",
        "fixture_name",
        "competition_country",
        "competition_name",
        "competition_predictability",
        "market_type",
        "model_probability",
        "actual_odds",
        "implied_prob_bookmaker",
        "edge",
        "home_games_played_home",
        "home_games_played_away",
        "away_games_played_home",
        "away_games_played_away",
        "home_home_win_pct",
        "home_home_draw_pct",
        "home_home_lose_pct",
        "away_away_win_pct",
        "away_away_draw_pct",
        "away_away_lose_pct",
        "home_points_pg",
        "away_points_pg",
        "points_pg_diff",
        "home_goals_for_pg",
        "away_goals_for_pg",
        "goals_for_pg_diff",
        "home_goals_against_pg",
        "away_goals_against_pg",
        "goals_against_pg_diff",
        "home_shots_pg",
        "away_shots_pg",
        "shots_pg_diff",
        "home_sot_pg",
        "away_sot_pg",
        "sot_pg_diff",
        "home_xg_pg",
        "away_xg_pg",
        "xg_pg_diff",
        "home_dangerous_attacks_pg",
        "away_dangerous_attacks_pg",
        "dangerous_attacks_diff",
        "home_scored_first_pct",
        "away_scored_first_pct",
        "scored_first_diff",
        "home_clean_sheet_pct",
        "away_clean_sheet_pct",
        "clean_sheet_diff",
        "home_possession_pct",
        "away_possession_pct",
        "possession_diff",
        "kickoff_date",
        "kickoff_hour",
        "won",
        "profit_units",
    ]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)

    csv_str = output.getvalue()
    output.close()
    return csv_str.encode("utf-8")

def send_ai_bets_csv_email(bets):
    """
    Public function you call from Python / inside the AI Bets route.
    """
    if not EMAIL_APP_PASSWORD:
        print("[AI CSV] EMAIL_APP_PASSWORD (GMAIL_APP_PASSWORD) is not set.")
        return

    # 1) Build Home/Draw/Away rows with season stats attached
    rows = _build_result_rows_from_bets(bets)
    if not rows:
        print("[AI CSV] No rows generated – nothing to send.")
        return

    # 2) Build CSV in memory
    csv_bytes = _build_csv_bytes(rows)

    # 3) Build email
    today_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"{today_str}-ai-bets.csv"

    msg = MIMEMultipart()
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = RECIPIENT_ADDRESS
    msg["Subject"] = f"AI Bets CSV – {today_str}"

    body_text = (
        f"Attached is today's AI Bets training CSV.\n\n"
        f"- Fixtures: {len({r['fixture_id'] for r in rows})}\n"
        f"- Rows: {len(rows)} (Home/Draw/Away per fixture where available)\n"
        f"- Columns include model probability, odds, edge, and season stats.\n"
        f"- 'won' and 'profit_units' are intentionally left blank.\n"
    )
    msg.attach(MIMEText(body_text, "plain"))

    part = MIMEBase("application", "octet-stream")
    part.set_payload(csv_bytes)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
    msg.attach(part)

    # 4) Send via Gmail SMTP
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
            server.send_message(msg)
        print(f"[AI CSV] Sent {len(rows)} rows in {filename} to {RECIPIENT_ADDRESS}")
    except Exception as e:
        print(f"[AI CSV] Failed to send email: {e}")

# ------------------------
# AI Bets – helper to pick best value market
# ------------------------

AI_MARKET_LABELS = {
    "home_win": "Home Win",
    "draw": "Draw",
    "away_win": "Away Win",
    "double_chance_1x": "Double Chance 1X",
    "double_chance_12": "Double Chance 12",
    "double_chance_x2": "Double Chance X2",
    "over_1_goals": "Over 1.5 Goals",
    "over_2_goals": "Over 2.5 Goals",
    "over_3_goals": "Over 3.5 Goals",
    "under_1_goals": "Under 1.5 Goals",
    "under_2_goals": "Under 2.5 Goals",
    "under_3_goals": "Under 3.5 Goals",
    "btts_yes": "Both Teams To Score – Yes",
}

AI_MARKET_TYPE = {
    # result markets
    "home_win": "result",
    "draw": "result",
    "away_win": "result",
    "double_chance_1x": "result",
    "double_chance_12": "result",
    "double_chance_x2": "result",
    # goal lines
    "over_1_goals": "goals",
    "over_2_goals": "goals",
    "over_3_goals": "goals",
    "under_1_goals": "goals",
    "under_2_goals": "goals",
    "under_3_goals": "goals",
    # BTTS
    "btts_yes": "btts",
}


def _safe_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def pick_six_random_value_bets():
    """
    Returns up to 6 unique candidate value bets from the cache.

    Rules:
    - Only includes fixtures being played TODAY (London time).
    - No duplicates.
    - Only includes markets with:
        * bookmaker odds >= 1.70
        * value/edge >= 10%
    - Only uses result markets: home_win, draw, away_win.
    - Tries to include at least one Home Win, one Draw, and one Away Win
      if candidates for those markets exist.
    """
    global game_details_cache

    if not game_details_cache:
        load_game_details_cache_from_disk()

    # Work out "today" in London
    london_tz = pytz.timezone("Europe/London")
    now_london = datetime.now(london_tz)
    today_london = now_london.date()

    candidates = []

    for fixture_id, fd in game_details_cache.items():
        if not isinstance(fd, dict):
            continue

        fixture_name = fd.get("fixture_name")
        comp_name = fd.get("competition_name")
        comp_country = fd.get("competition_country")
        unix_ts = fd.get("unix")
        season_id = fd.get("season_id")
        home_id = fd.get("home_id")
        away_id = fd.get("away_id")

        # Only keep fixtures whose KO date is TODAY (London)
        if not unix_ts:
            continue

        try:
            ko_dt_utc = datetime.fromtimestamp(int(unix_ts), pytz.utc)
            ko_dt_london = ko_dt_utc.astimezone(london_tz)
        except Exception:
            continue

        if ko_dt_london.date() != today_london:
            continue

        # For this fixture (which IS today), scan ONLY the 1X2 result markets
        for mk, mdata in fd.items():
            if mk not in ("home_win", "draw", "away_win"):
                continue

            if not isinstance(mdata, dict):
                continue

            prob = _safe_float(mdata.get("probability"))
            fair_odds = _safe_float(mdata.get("implied_odds"))
            book_odds = _safe_float(mdata.get("actual_odds"))

            if not prob or not fair_odds or not book_odds:
                continue
            if fair_odds <= 0:
                continue

            edge = ((book_odds - fair_odds) / abs(fair_odds)) * 100.0

            # Constraints
            if book_odds < 1.70:
                continue
            if edge < 10.0:
                continue

            candidates.append({
                "fixture_id": fixture_id,
                "fixture_name": fixture_name,
                "competition_name": comp_name,
                "competition_country": comp_country,
                "unix": unix_ts,
                "season_id": season_id,
                "home_id": home_id,
                "away_id": away_id,
                "market_key": mk,
                "market_nice": AI_MARKET_LABELS.get(mk, mk),
                "market_type": AI_MARKET_TYPE.get(mk, "result"),
                "prob": prob,
                "fair_odds": fair_odds,
                "book_odds": book_odds,
                "edge": edge,
            })

    if not candidates:
        return []

    # Ensure at least one of each market if possible
    home_candidates = [b for b in candidates if b["market_key"] == "home_win"]
    draw_candidates = [b for b in candidates if b["market_key"] == "draw"]
    away_candidates = [b for b in candidates if b["market_key"] == "away_win"]

    final = []
    if home_candidates:
        final.append(random.choice(home_candidates))
    if draw_candidates:
        final.append(random.choice(draw_candidates))
    if away_candidates:
        final.append(random.choice(away_candidates))

    # Fill remaining slots up to 6 with other candidates remaining
    remaining = [b for b in candidates if b not in final]
    random.shuffle(remaining)
    final.extend(remaining[:max(0, 6 - len(final))])

    # Return up to 6 items (not index 6)
    return final[:6]

def build_ai_bets_candidate_pool():
    """
    Returns ALL candidate value bets from the cache that pass your existing rules.

    Rules (same as pick_six_random_value_bets):
    - Only includes fixtures being played TODAY (London time)
    - Only includes result markets: home_win, draw, away_win
    - bookmaker odds >= 1.70
    - value/edge >= 10%
    """

    global game_details_cache

    if not game_details_cache:
        load_game_details_cache_from_disk()

    london_tz = pytz.timezone("Europe/London")
    today_london = datetime.now(london_tz).date()

    candidates = []

    # DEBUG counters
    today_fixtures = 0
    result_market_dicts = 0

    for fixture_id, fd in game_details_cache.items():
        if not isinstance(fd, dict):
            continue

        unix_ts = fd.get("unix")
        if not unix_ts:
            continue

        try:
            ko_dt_utc = datetime.fromtimestamp(int(unix_ts), pytz.utc)
            ko_dt_london = ko_dt_utc.astimezone(london_tz)
        except Exception:
            continue

        # ONLY today (London)
        if ko_dt_london.date() != today_london:
            continue

        today_fixtures += 1

        fixture_name = fd.get("fixture_name")
        comp_name = fd.get("competition_name")
        comp_country = fd.get("competition_country")
        season_id = fd.get("season_id")
        home_id = fd.get("home_id")
        away_id = fd.get("away_id")

        # Scan ONLY result markets
        for mk in ("home_win", "draw", "away_win"):
            mdata = fd.get(mk)
            if not isinstance(mdata, dict):
                continue

            result_market_dicts += 1

            prob = _safe_float(mdata.get("probability"))
            fair_odds = _safe_float(mdata.get("implied_odds"))
            book_odds = _safe_float(mdata.get("actual_odds"))

            if not prob or not fair_odds or not book_odds:
                continue

            if fair_odds <= 0:
                continue

            edge = ((book_odds - fair_odds) / abs(fair_odds)) * 100.0

            # Constraints
            if book_odds < 1.70:
                continue
            if edge < 10.0:
                continue

            candidates.append({
                "fixture_id": fixture_id,
                "fixture_name": fixture_name,
                "competition_name": comp_name,
                "competition_country": comp_country,
                "unix": unix_ts,
                "season_id": season_id,
                "home_id": home_id,
                "away_id": away_id,
                "market_key": mk,
                "market_nice": AI_MARKET_LABELS.get(mk, mk),
                "market_type": AI_MARKET_TYPE.get(mk, "result"),
                "prob": prob,
                "fair_odds": fair_odds,
                "book_odds": book_odds,
                "edge": edge,
            })

    print(
        "[AI_POOL] today_fixtures =", today_fixtures,
        "| result_market_dicts =", result_market_dicts,
        "| candidates =", len(candidates)
    )

    return candidates

# ------------------------
# AI Bets – API endpoint for front-end
# ------------------------

@app.route("/api/generate")
def api_generate_ai_bet():
    """
    Generates AI Bets cards + CSV email.
    Now uses Case B:
      - build full candidate pool (existing rules)
      - score pool with trained model
      - take top bucket
      - random sample 6 from bucket
    """
    if GEMINI_API_KEY is None:
        return jsonify({"error": "GEMINI_API_KEY is not configured."}), 500

    # 1) Build the FULL candidate pool using existing rules
    pool = build_ai_bets_candidate_pool()
    print("[AI_POOL] Candidates in pool:", len(pool))

    if not pool:
        return jsonify({"error": "No Available Bets"}), 404

    # 2) Case B selection: score pool -> top bucket -> sample 6
    if _ai_bets_model:
        try:
            df_rows = []
            for b in pool:
                unix_ts = int(b.get("unix", 0) or 0)
                dt_utc = datetime.fromtimestamp(unix_ts, tz=timezone.utc) if unix_ts else None

                df_rows.append({
                    "market_type": b.get("market_key"),
                    "competition_predictability": b.get("competition_predictability", "unknown"),
                    "kickoff_date": dt_utc.strftime("%Y-%m-%d") if dt_utc else None,
                    "kickoff_hour": dt_utc.strftime("%H:%M") if dt_utc else None,
                    "model_probability": float(b.get("prob", 0.0)),
                    "actual_odds": float(b.get("book_odds", 0.0)),
                    "edge": float(b.get("edge", 0.0)),
                    "implied_prob_bookmaker": np.nan,
                })

            df_raw = pd.DataFrame(df_rows)
            X = build_features_for_prediction(df_raw)

            probs = _ai_bets_model.predict_proba(X)[:, 1]
            for b, p in zip(pool, probs):
                b["ai_score"] = float(p)

            pool_sorted = sorted(pool, key=lambda x: x.get("ai_score", 0.0), reverse=True)

            # Pool is tiny today (7). Keep bucket = all for now.
            top_k = min(max(10, 3 * 6), len(pool_sorted))  # bucket is at least 10, otherwise 18, capped by pool
            bucket = pool_sorted[:top_k]

            bets = bucket if len(bucket) <= 6 else random.sample(bucket, 6)

            print("[AI_MODEL] Case B selection complete. Using bets:", len(bets), "from pool:", len(pool))
        except Exception as e:
            print("[AI_MODEL] Case B failed, fallback to first 6:", e)
            bets = pool[:6]
    else:
        # No model loaded: fallback
        bets = pool[:6]

    # 1b) Store these bets in the AI Bets cache for today's date (DD/MM/YYYY)
    store_ai_bets_for_today_from_selected_bets(bets)

    # 1c) Email these bets to yourself as a CSV (best-effort; don't break the API if it fails)
    try:
        send_ai_bets_csv_email(bets)
    except Exception as e:
        print(f"[AI BETS EMAIL] Failed to send CSV: {e}")

    cards = []

    # Make sure season stats cache is loaded once
    load_season_stats_cache_from_disk()

    for bet in bets:
        name = bet["fixture_name"]
        country = bet["competition_country"]
        comp = bet["competition_name"]
        comp_full = f"{country} - {comp}"
        mk = bet["market_key"]
        nice = bet["market_nice"]
        market_type = bet["market_type"]
        prob = bet["prob"]
        implied = bet["fair_odds"]
        book = bet["book_odds"]
        edge = bet["edge"]
        season_id = bet["season_id"]
        home_id = bet["home_id"]
        away_id = bet["away_id"]
        unix_ts = bet["unix"]
        ai_score = bet.get("ai_score")
        ai_prob_pct = round(ai_score * 100, 2) if ai_score is not None else None


        # Confidence label
        if prob >= 70 and edge >= 5:
            confidence = "HIGH"
        elif prob >= 60 and edge >= 2:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        # Kickoff string
        kickoff_str = format_kickoff_filter(unix_ts) if unix_ts else "N/A"

        # ---- Season stats for THIS fixture ----
        home_stats = {}
        away_stats = {}
        home_goals_profile = {}
        away_goals_profile = {}
        stats_prob = None  # optional stats-based probability

        if season_id and season_stats_cache:
            bucket = season_stats_cache.get(str(season_id), {}).get("data", [])
            for row in bucket:
                if row.get("team_id") == home_id:
                    home_stats = row
                    home_goals_profile = row.get("goals_over", {})
                elif row.get("team_id") == away_id:
                    away_stats = row
                    away_goals_profile = row.get("goals_over", {})

            # derive a simple stats_prob depending on market type (optional)
            try:
                if market_type == "btts":
                    h = float(home_stats.get("btts", {}).get("home_percentage", 0))
                    a = float(away_stats.get("btts", {}).get("away_percentage", 0))
                    stats_prob = round((h + a) / 2, 2)
                elif market_type == "goals":
                    if mk in ("over_1_goals", "under_1_goals"):
                        key = "o1"
                    elif mk in ("over_2_goals", "under_2_goals"):
                        key = "o2"
                    else:
                        key = "o3"
                    h = float(home_goals_profile.get(key, {}).get("total_percentage", 0))
                    a = float(away_goals_profile.get(key, {}).get("total_percentage", 0))
                    stats_prob = round((h + a) / 2, 2)
            except Exception:
                stats_prob = None

        is_fallback = stats_prob is None

        # ---------- PROMPT (unchanged logic, but per bet) ----------
        prompt = f"""
You are generating a professional HTML betting analysis for Craig.

IMPORTANT OUTPUT RULES (MUST FOLLOW EXACTLY):
- Output valid HTML only.
- The HTML must include:
  1) A professional HTML table (NOT markdown) with these columns:
        Market | Model Probability | Fair Odds | Bookmaker Odds | Value/Edge | Confidence
  2) A short paragraph (2–3 sentences) explaining why this is a value bet AND whether season stats support it.
  3) A "Key Statistical Context" section using a <ul> with 3–5 bullet points.
  4) A final sentence starting with "Verdict:".

- Do NOT output markdown (#, ##, *, |).
- Do NOT output code fences (```).
- Do NOT invent any extra text outside the required structure.

TABLE RULES:
- Table must use <table>, <thead>, <tbody>, <tr>, <th>, <td>.
- ALL numeric fields must be filled using the actual values provided.
- Percentages must include % sign.

ANALYSIS RULES BY MARKET TYPE:

1) OVER/UNDER GOALS MARKETS
(over_1_goals, under_1_goals, over_2_goals, under_2_goals, over_3_goals, under_3_goals)
Use ONLY goal-based stats:
- Over line strike rates:
    home: goals_over["oX"]["home_percentage"]
    away: goals_over["oX"]["away_percentage"]
- Also, include how many times the line has landed at HOME and AWAY, using:
    goals_over["oX"]["home"] and goals_over["oX"]["away"]
    plus home played["home"] and away played["away"] to form "X times from Y games".
- goals_total.home_avg, goals_total.away_avg
- goals_for.home_avg, goals_against.home_avg
- goals_for.away_avg, goals_against.away_avg
- You may also reference xG-style stats if present (e.g. expected_goals_for, expected_goals_against).
Do NOT reference points per game or win percentages. Mention how often the line has landed home/away.

2) BTTS (btts_yes)
Use ONLY:
- home_stats["btts"]["home_percentage"] and underlying count home_stats["btts"]["home"]
- away_stats["btts"]["away_percentage"] and underlying count away_stats["btts"]["away"]
- played["home"] and played["away"] so you can say "X times from Y home/away games".
- goals_for.home_avg, goals_against.home_avg
- goals_for.away_avg, goals_against.away_avg
- You may also include xG-based stats if present (e.g. xG for / xG against).
Do NOT mention PPG or win%.

3) RESULT MARKETS
(home_win, draw, away_win, double_chance_1x, double_chance_12, double_chance_x2)
Use:
- home_win_pct, home_loss_pct, home_ppg
- away_win_pct, away_loss_pct, away_ppg
- goals_for.home_avg, goals_against.home_avg
- goals_for.away_avg, goals_against.away_avg
- You may reference total xG for/against if present, but keep the focus on win/loss/PPG.
Focus on home-at-home vs away-at-away performance.

STRUCTURE YOU MUST FOLLOW:

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

<p>[Your explanation here]</p>

<ul>
  <li>[Bullet 1 based on correct market type, including strike rates AND counts]</li>
  <li>[Bullet 2]</li>
  <li>[Bullet 3]</li>
  <li>[Optional bullet 4]</li>
</ul>

<p><strong>Verdict:</strong> [Short recommendation to Craig]</p>

DATA FOR ANALYSIS:

Fixture: {name}
Competition: {comp_full}
Market: {nice}

Model probability: {prob:.2f}%
Stats probability: {stats_prob if stats_prob is not None else "N/A"}
Fair odds: {implied:.2f}
Bookmaker odds: {book:.2f}
Value edge: {edge:.2f}%
Fallback mode: {is_fallback}

Home team stats:
{home_stats}

Home goals profile:
{home_goals_profile}

Away team stats:
{away_stats}

Away goals profile:
{away_goals_profile}
"""

        # Call Gemini once per bet
        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )

        raw_html = (response.text or "").strip()
        clean_html = (
            raw_html
            .replace("```html", "")
            .replace("```", "")
            .strip()
        )

        cards.append({
            "fixture": name,
            "competition": comp_full,
            "kickoff": kickoff_str,
            "market_key": mk,
            "market_name": nice,
            "model_prob": round(prob, 2),
            "stats_prob": round(stats_prob, 2) if isinstance(stats_prob, (int, float)) else None,
            "fair_odds": round(implied, 2),
            "bookmaker_odds": round(book, 2),
            "edge": round(edge, 2),
            "confidence": confidence,
            "ai_prob_pct": ai_prob_pct,
            "html": clean_html,
        })

    # For backwards-compatibility with current frontend:
    primary = cards[0]

    # NEW: persist latest cards so the AI Bets page can reload them
    save_ai_bets_cards(cards)

    return jsonify({
        "cards": cards,      # NEW: full list of up to 6 bets
        **primary            # OLD fields so your existing JS still works for now
    })

@app.route("/api/ai-bets-latest")
def api_ai_bets_latest():
    data = load_ai_bets_cards()

    # 🔒 Defensive handling
    if isinstance(data, list):
        cards = data
    elif isinstance(data, dict):
        cards = data.get("cards", [])
    else:
        cards = []

    return jsonify({"cards": cards})

def settle_market_from_score(market: str, home_goals: int, away_goals: int) -> str:
    """
    Given a market name and the final score, return:
        'won'  -> bet wins
        'lost' -> bet loses
        'void' -> (placeholder for future use, e.g. postponed)
    
    This function assumes the match is finished and home_goals/away_goals are final.
    """
    hg = home_goals
    ag = away_goals
    total = hg + ag

    m = market.strip().lower()

    # 👉 NEW: normalise dashes and extra spaces
    m = m.replace("–", " ").replace("-", " ")
    m = " ".join(m.split())

    # ---------- FULL-TIME RESULT ----------
    if m in ("home win", "1"):
        return "won" if hg > ag else "lost"

    if m in ("draw", "x"):
        return "won" if hg == ag else "lost"

    if m in ("away win", "2"):
        return "won" if ag > hg else "lost"

    # ---------- DOUBLE CHANCE ----------
    # 1X: home or draw  -> hg >= ag
    if m in ("double chance 1x", "1x"):
        return "won" if hg >= ag else "lost"

    # X2: away or draw  -> ag >= hg
    if m in ("double chance x2", "x2"):
        return "won" if ag >= hg else "lost"

    # 12: home or away (no draw) -> hg != ag
    if m in ("double chance 12", "12"):
        return "won" if hg != ag else "lost"

    # ---------- BTTS ----------
    # BTTS Yes: both teams score at least 1
    if m in ("btts yes", "both teams to score yes", "btts"):
        return "won" if (hg >= 1 and ag >= 1) else "lost"

    # BTTS No: at least one team scores 0
    if m in ("btts no", "both teams to score no"):
        return "won" if (hg == 0 or ag == 0) else "lost"

    # ---------- OVER GOALS ----------
    if m in ("over 1.5 goals", "over 1.5"):
        return "won" if total >= 2 else "lost"

    if m in ("over 2.5 goals", "over 2.5"):
        return "won" if total >= 3 else "lost"

    if m in ("over 3.5 goals", "over 3.5"):
        return "won" if total >= 4 else "lost"

    # ---------- UNDER GOALS ----------
    if m in ("under 1.5 goals", "under 1.5"):
        return "won" if total < 2 else "lost"

    if m in ("under 2.5 goals", "under 2.5"):
        return "won" if total < 3 else "lost"

    if m in ("under 3.5 goals", "under 3.5"):
        return "won" if total < 4 else "lost"

    # If we don't recognise the market, default to lost for now (can change later)
    return "lost"

def load_ai_bets_cache() -> dict:
    """
    Load the AI bets cache from disk.

    Structure:
    {
        "DD-MM-YYYY": [
            {
                "fixture_id": int,
                "fixture_name": str,
                "kickoff_iso": str,
                "competition_country": bet["competition_country"],  # e.g. "Scotland"
                "competition_name": bet["competition_name"],        # e.g. "League One"
                "market": str,
                "probability": float,
                "implied_odds": float,
                "actual_odds": float,
                "edge_percent": float,
                "status": "pending" | "won" | "lost",
                "profit_units": float | None,
                "last_result_check": str | None,
            },
            ...
        ],
        ...
    }
    """
    if not os.path.exists(AI_BETS_CACHE_FILE):
        return {}

    try:
        with open(AI_BETS_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
            # If file is corrupted or wrong type, start fresh
            return {}
    except Exception:
        # If anything goes wrong reading/parsing, return empty and let future saves fix it
        return {}


def save_ai_bets_cache(cache: dict) -> None:
    """
    Save the AI bets cache back to disk.

    Expects the same structure as returned by load_ai_bets_cache().
    """
    try:
        with open(AI_BETS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        # For now we just print/log; you can hook this into your logger if you want.
        print(f"Error saving AI bets cache: {e}")


def get_today_date_str() -> str:
    """
    Return today's date in DD/MM/YYYY format (London time).
    """
    now_london = datetime.now(ZoneInfo("Europe/London"))
    return now_london.strftime("%d/%m/%Y")

def make_ai_bet_entry(
    fixture_id: int,
    fixture_name: str,
    kickoff_iso: str,
    market: str,
    probability: float,
    implied_odds: float,
    actual_odds: float,
    edge_percent: float,
    competition_country: str | None = None,
    competition_name: str | None = None,
) -> dict:
    """
    Create a normalised AI bet entry dict for storage in the AI bets cache.

    All monetary/odds logic (P/L etc.) will be based on a 1-unit stake.
    """
    return {
        "fixture_id": fixture_id,
        "fixture_name": fixture_name,
        "kickoff_iso": kickoff_iso,      # ISO datetime string
        "competition_country": competition_country,
        "competition_name": competition_name,
        "market": market,
        "probability": float(probability) if probability is not None else None,
        "implied_odds": float(implied_odds) if implied_odds is not None else None,
        "actual_odds": float(actual_odds) if actual_odds is not None else None,
        "edge_percent": float(edge_percent) if edge_percent is not None else None,
        # Result-related fields (to be filled later when we check scores)
        "status": "pending",             # "pending" | "won" | "lost"
        "profit_units": None,            # e.g. 2.20 or -1.0 when settled
        "last_result_check": None,       # ISO timestamp of last result check
    }


def get_ai_bets_for_date(date_str: str) -> list:
    """
    Return the list of AI bets for a given date string (DD/MM/YYYY)
    from the cache. Returns an empty list if none exist.
    """
    cache = load_ai_bets_cache()
    bets = cache.get(date_str)
    if isinstance(bets, list):
        return bets
    return []

def set_ai_bets_for_date(date_str: str, bets: list) -> None:
    """
    Store up to 6 AI bets for the given date string (DD/MM/YYYY).

    This will REPLACE any existing bets for that date.
    """
    cache = load_ai_bets_cache()

    # Ensure we only store a list and cap at 6 bets
    if not isinstance(bets, list):
        bets = []

    cache[date_str] = bets[:9]

    save_ai_bets_cache(cache)

def _now_iso_london() -> str:
    """
    Return current time in ISO format in London time.
    Used for last_result_check on AI bets.
    """
    return datetime.now(ZoneInfo("Europe/London")).isoformat()

def settle_ai_bet_entry(bet: dict, home_goals: int, away_goals: int) -> dict:
    """
    Given a single AI bet entry dict (from the cache) and the final score,
    update and return the bet with:
        - status: "won" or "lost"
        - profit_units: odds - 1 for win, -1 for loss
        - last_result_check: ISO timestamp (London time)

    If actual_odds is missing or invalid, profit_units will stay None.
    """
    market = bet.get("market", "")
    status = settle_market_from_score(market, home_goals, away_goals)

    bet["status"] = status
    bet["last_result_check"] = _now_iso_london()

    actual_odds = bet.get("actual_odds")

    # Only compute profit if we have a valid numeric actual_odds
    try:
        if actual_odds is not None:
            actual_odds = float(actual_odds)
        else:
            actual_odds = None
    except (TypeError, ValueError):
        actual_odds = None

    if actual_odds is not None:
        if status == "won":
            # 1-unit stake → net profit = odds - 1
            bet["profit_units"] = round(actual_odds - 1.0, 4)
        elif status == "lost":
            bet["profit_units"] = -1.0
        else:
            # If we ever add "void" etc., leave as None for now
            bet["profit_units"] = None
    else:
        # No usable odds, leave profit_units as None
        bet["profit_units"] = None

    return bet

def settle_ai_bets_with_results(results_by_fixture: dict) -> int:
    """
    Given a mapping of fixture_id -> (home_goals, away_goals),
    settle all *pending* AI bets that match those fixture IDs.

    Example of results_by_fixture:
        {
            365992470: (1, 1),
            365992467: (4, 5),
        }

    Returns:
        count of bets that were settled (status changed from pending to won/lost).
    """
    cache = load_ai_bets_cache()
    if not isinstance(cache, dict):
        return 0

    settled_count = 0
    changed = False

    for date_str, bets in cache.items():
        if not isinstance(bets, list):
            continue

        for bet in bets:
            if not isinstance(bet, dict):
                continue

            # Skip if already settled
            current_status = bet.get("status")
            if current_status in ("won", "lost"):
                continue

            fixture_id = bet.get("fixture_id")
            if fixture_id is None:
                continue

            # Normalise fixture_id to int if possible
            try:
                fixture_id_int = int(fixture_id)
            except (TypeError, ValueError):
                continue

            result = results_by_fixture.get(fixture_id_int)
            if not result:
                # No result info for this fixture in this batch
                continue

            # Expecting result to be (home_goals, away_goals)
            try:
                home_goals, away_goals = result
                home_goals = int(home_goals)
                away_goals = int(away_goals)
            except (TypeError, ValueError, ValueError):
                continue

            # Settle this bet using our existing logic
            settle_ai_bet_entry(bet, home_goals, away_goals)
            settled_count += 1
            changed = True

    if changed:
        save_ai_bets_cache(cache)

    return settled_count

def store_ai_bets_for_today_from_selected_bets(bets: list) -> None:
    """
    Take the list returned by pick_six_random_value_bets() and
    APPEND up to 6 of them into the AI bets cache for today's date
    (DD/MM/YYYY), using the standard AI bet entry structure.

    This does NOT remove or overwrite any existing bets for that date.
    """
    if not isinstance(bets, list):
        return

    date_key = get_today_date_str()  # e.g. "04/12/2025"

    entries = []
    for bet in bets[:6]:
        if not isinstance(bet, dict):
            continue

        fixture_id = bet.get("fixture_id")
        fixture_name = bet.get("fixture_name") or ""
        unix_ts = bet.get("unix")
        market_label = bet.get("market_nice") or ""
        prob = bet.get("prob")
        implied = bet.get("fair_odds")
        actual = bet.get("book_odds")
        edge = bet.get("edge")

        # Country & league (if present on the bet dict)
        competition_country = bet.get("competition_country") or None
        competition_name = bet.get("competition_name") or None

        # Convert kickoff unix → ISO string (UTC). Safe fallback to empty string.
        kickoff_iso = ""
        if unix_ts is not None:
            try:
                ts_int = int(unix_ts)
                kickoff_iso = datetime.fromtimestamp(ts_int, timezone.utc).isoformat()
            except Exception:
                kickoff_iso = ""

        try:
            fixture_id_int = int(fixture_id)
        except (TypeError, ValueError):
            # Skip if fixture_id is unusable
            continue

        entry = make_ai_bet_entry(
            fixture_id=fixture_id_int,
            fixture_name=fixture_name,
            kickoff_iso=kickoff_iso,
            market=market_label,
            probability=prob,
            implied_odds=implied,
            actual_odds=actual,
            edge_percent=edge,
            competition_country=competition_country,
            competition_name=competition_name,
        )
        entries.append(entry)

    if not entries:
        return

    # 🔹 Load existing cache and append, don't overwrite
    cache = load_ai_bets_cache()
    if not isinstance(cache, dict):
        cache = {}

    existing_for_day = cache.get(date_key)
    if not isinstance(existing_for_day, list):
        existing_for_day = []

    # Append new entries at the end
    existing_for_day.extend(entries)
    cache[date_key] = existing_for_day

    save_ai_bets_cache(cache)

def generate_ai_bets_for_today_if_missing() -> bool:
    """
    If there are no AI bets stored for today's date (DD/MM/YYYY) in the
    AI bets cache, pick up to 6 random value bets for TODAY (limited to
    Home Win / Draw / Away Win), store them, email them as a CSV, and
    also generate + save the AI cards so the AI Bets page can show
    them immediately (even before any user clicks Generate).

    Returns True if new bets were generated, False if today's bets
    already existed or no suitable bets were found.
    """
    cache = load_ai_bets_cache()
    if not isinstance(cache, dict):
        cache = {}

    today_key = get_today_date_str()  # "DD/MM/YYYY"

    existing = cache.get(today_key)
    if isinstance(existing, list) and len(existing) > 0:
        # Today's bets already exist; do nothing
        return False

    # Build pool using existing rules
    pool = build_ai_bets_candidate_pool()
    print("[AI_POOL] Auto-gen candidates in pool:", len(pool))

    if not pool:
        print("[AI BETS] No available value bets for today; nothing generated.")
        return False

    # Case B selection (same as /api/generate)
    if _ai_bets_model:
        try:
            df_rows = []
            for b in pool:
                unix_ts = int(b.get("unix", 0) or 0)
                dt_utc = datetime.fromtimestamp(unix_ts, tz=timezone.utc) if unix_ts else None

                df_rows.append({
                    "market_type": b.get("market_key"),
                    "competition_predictability": b.get("competition_predictability", "unknown"),
                    "kickoff_date": dt_utc.strftime("%Y-%m-%d") if dt_utc else None,
                    "kickoff_hour": dt_utc.strftime("%H:%M") if dt_utc else None,
                    "model_probability": float(b.get("prob", 0.0)),
                    "actual_odds": float(b.get("book_odds", 0.0)),
                    "edge": float(b.get("edge", 0.0)),
                    "implied_prob_bookmaker": np.nan,
                })

            df_raw = pd.DataFrame(df_rows)
            X = build_features_for_prediction(df_raw)

            probs = _ai_bets_model.predict_proba(X)[:, 1]
            for b, p in zip(pool, probs):
                b["ai_score"] = float(p)

            pool_sorted = sorted(pool, key=lambda x: x.get("ai_score", 0.0), reverse=True)

            top_k = min(max(10, 18), len(pool_sorted))
            bucket = pool_sorted[:top_k]

            bets = bucket if len(bucket) <= 6 else random.sample(bucket, 6)

            print("[AI_MODEL] Auto-gen Case B selection complete. Using bets:", len(bets), "from pool:", len(pool))
        except Exception as e:
            print("[AI_MODEL] Auto-gen Case B failed, fallback to first 6:", e)
            bets = pool[:6]
    else:
        bets = pool[:6]


    # 🔒 Limit to ONLY Home Win / Draw / Away Win markets
    allowed_result_markets = {"home_win", "draw", "away_win"}
    filtered_bets = []
    for b in bets:
        mk = str(b.get("market_key") or "").strip()
        if mk in allowed_result_markets:
            filtered_bets.append(b)

    if not filtered_bets:
        print("[AI BETS] Bets were found, but none were Home/Draw/Away result markets.")
        return False

    # If we filtered out anything, log it
    if len(filtered_bets) != len(bets):
        print(f"[AI BETS] Filtered bets from {len(bets)} to {len(filtered_bets)} result markets only.")

    bets = filtered_bets

    # Store them in the AI bets cache
    store_ai_bets_for_today_from_selected_bets(bets)
    print(f"[AI BETS] Stored {len(bets)} AI bets for {today_key} in ai_bets_cache.json")

    # 📨 Also email the raw result-market CSV for training
    try:
        send_ai_bets_csv_email(bets)
        print("[AI BETS] Sent daily AI bets CSV via email.")
    except Exception as e:
        print(f"[AI BETS] Failed to send AI bets CSV email: {e}")


    # Also generate AI cards (Gemini HTML) and save them so the AI Bets page
    # can show them immediately on first load.
    if GEMINI_API_KEY is None:
        print("[AI BETS] GEMINI_API_KEY not configured; skipping card generation.")
        return True

    # Make sure season stats cache is loaded once
    load_season_stats_cache_from_disk()

    cards: list[dict] = []

    for bet in bets:
        name = bet["fixture_name"]
        country = bet["competition_country"]
        comp = bet["competition_name"]
        comp_full = f"{country} - {comp}" if country and comp else (country or comp or "")
        mk = bet["market_key"]           # 'home_win', 'draw', 'away_win'
        nice = bet["market_nice"]        # pretty label
        market_type = bet["market_type"] # likely 'result'
        prob = bet["prob"]
        implied = bet["fair_odds"]
        book = bet["book_odds"]
        edge = bet["edge"]
        season_id = bet["season_id"]
        home_id = bet["home_id"]
        away_id = bet["away_id"]
        unix_ts = bet["unix"]

        # Confidence label (same rules as /api/generate)
        if prob >= 70 and edge >= 5:
            confidence = "HIGH"
        elif prob >= 60 and edge >= 2:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        kickoff_str = format_kickoff_filter(unix_ts) if unix_ts else "N/A"

        # ---- Season stats for THIS fixture ----
        home_stats = {}
        away_stats = {}
        home_goals_profile = {}
        away_goals_profile = {}
        stats_prob = None

        if season_id and season_stats_cache:
            bucket = season_stats_cache.get(str(season_id), {}).get("data", [])
            for row in bucket:
                if row.get("team_id") == home_id:
                    home_stats = row
                    home_goals_profile = row.get("goals_over", {})
                elif row.get("team_id") == away_id:
                    away_stats = row
                    away_goals_profile = row.get("goals_over", {})

            # NOTE: this block is mainly for BTTS / Goals markets.
            # For result markets, stats_prob will usually stay as None,
            # which is fine (we show 'N/A' in the prompt).
            try:
                if market_type == "btts":
                    h = float(home_stats.get("btts", {}).get("home_percentage", 0))
                    a = float(away_stats.get("btts", {}).get("away_percentage", 0))
                    stats_prob = round((h + a) / 2, 2)
                elif market_type == "goals":
                    if mk in ("over_1_goals", "under_1_goals"):
                        key = "o1"
                    elif mk in ("over_2_goals", "under_2_goals"):
                        key = "o2"
                    else:
                        key = "o3"
                    h = float(home_goals_profile.get(key, {}).get("total_percentage", 0))
                    a = float(away_goals_profile.get(key, {}).get("total_percentage", 0))
                    stats_prob = round((h + a) / 2, 2)
            except Exception:
                stats_prob = None

        is_fallback = stats_prob is None

        # ---------- PROMPT (same as before, but now effectively used for result markets) ----------
        prompt = f"""
You are generating a professional HTML betting analysis for Craig.

IMPORTANT OUTPUT RULES (MUST FOLLOW EXACTLY):
- Output valid HTML only.
- The HTML must include:
  1) A professional HTML table (NOT markdown) with these columns:
        Market | Model Probability | Fair Odds | Bookmaker Odds | Value/Edge | Confidence
  2) A short paragraph (2–3 sentences) explaining why this is a value bet AND whether season stats support it.
  3) A "Key Statistical Context" section using a <ul> with 3–5 bullet points.
  4) A final sentence starting with "Verdict:".

- Do NOT output markdown (#, ##, *, |).
- Do NOT output code fences (```).
- Do NOT invent any extra text outside the required structure.

TABLE RULES:
- Table must use <table>, <thead>, <tbody>, <tr>, <th>, <td>.
- ALL numeric fields must be filled using the actual values provided.
- Percentages must include % sign.

ANALYSIS RULES BY MARKET TYPE:

1) OVER/UNDER GOALS MARKETS
(over_1_goals, under_1_goals, over_2_goals, under_2_goals, over_3_goals, under_3_goals)
Use ONLY goal-based stats:
- Over line strike rates:
    home: goals_over["oX"]["home_percentage"]
    away: goals_over["oX"]["away_percentage"]
- Also, include how many times the line has landed at HOME and AWAY, using:
    goals_over["oX"]["home"] and goals_over["oX"]["away"]
    plus home played["home"] and away played["away"] to form "X times from Y games".
- goals_total.home_avg, goals_total.away_avg
- goals_for.home_avg, goals_against.home_avg
- goals_for.away_avg, goals_against.away_avg
- You may also reference xG-style stats if present (e.g. expected_goals_for, expected_goals_against).
Do NOT reference points per game or win percentages. Mention how often the line has landed home/away.

2) BTTS (btts_yes)
Use ONLY:
- home_stats["btts"]["home_percentage"] and underlying count home_stats["btts"]["home"]
- away_stats["btts"]["away_percentage"] and underlying count away_stats["btts"]["away"]
- played["home"] and played["away"] so you can say "X times from Y home/away games".
- goals_for.home_avg, goals_against.home_avg
- goals_for.away_avg, goals_against.away_avg
- You may also include xG-based stats if present (e.g. xG for / xG against).
Do NOT mention PPG or win%.

3) RESULT MARKETS
(home_win, draw, away_win, double_chance_1x, double_chance_12, double_chance_x2)
Use:
- home_win_pct, home_loss_pct, home_ppg
- away_win_pct, away_loss_pct, away_ppg
- goals_for.home_avg, goals_against.home_avg
- goals_for.away_avg, goals_against.away_avg
- You may reference total xG for/against if present, but keep the focus on win/loss/PPG.
Focus on home-at-home vs away-at-away performance.

STRUCTURE YOU MUST FOLLOW:

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

<p>[Your explanation here]</p>

<ul>
  <li>[Bullet 1 based on correct market type, including strike rates AND counts]</li>
  <li>[Bullet 2]</li>
  <li>[Bullet 3]</li>
  <li>[Optional bullet 4]</li>
</ul>

<p><strong>Verdict:</strong> [Short recommendation to Craig]</p>

DATA FOR ANALYSIS:

Fixture: {name}
Competition: {comp_full}
Market: {nice}

Model probability: {prob:.2f}%
Stats probability: {stats_prob if stats_prob is not None else "N/A"}
Fair odds: {implied:.2f}
Bookmaker odds: {book:.2f}
Value edge: {edge:.2f}%
Fallback mode: {is_fallback}

Home team stats:
{home_stats}

Home goals profile:
{home_goals_profile}

Away team stats:
{away_stats}

Away goals profile:
{away_goals_profile}
"""

        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )

        raw_html = (response.text or "").strip()
        clean_html = (
            raw_html
            .replace("```html", "")
            .replace("```", "")
            .strip()
        )
        
        ai_prob_pct = round(float(bet.get("ai_score", 0.0)) * 100.0, 2) if bet.get("ai_score") is not None else None

        cards.append({
            "fixture": name,
            "competition": comp_full,
            "kickoff": kickoff_str,
            "market_key": mk,
            "market_name": nice,
            "model_prob": round(prob, 2),
            "stats_prob": round(stats_prob, 2) if isinstance(stats_prob, (int, float)) else None,
            "fair_odds": round(implied, 2),
            "bookmaker_odds": round(book, 2),
            "edge": round(edge, 2),
            "confidence": confidence,
            "ai_prob_pct": ai_prob_pct,
            "html": clean_html,
        })

    # Save the cards so /api/ai-bets-latest and the AI Bets page can use them
    save_ai_bets_cards(cards)
    print(f"[AI BETS] Generated and cached {len(cards)} AI bet cards for {today_key}")

    return True

def refresh_ai_bets_results_once(max_ids: int = 50) -> int:
    """
    1) Look at ai_bets_cache.json and collect up to `max_ids` fixture_ids
       for bets that are still pending.
    2) Call OddAlerts /fixtures/multiple for those IDs.
    3) Build a mapping fixture_id -> (home_goals, away_goals) for finished games.
    4) Pass that mapping into settle_ai_bets_with_results().
    5) Return the number of bets that were settled.

    This does NOT loop over multiple pages of 50; it just processes
    the first batch of up to `max_ids` pending bets. Given you only
    have up to 6 bets per day, this is sufficient for now.
    """

    # 1) Load cache and collect pending fixture IDs
    cache = load_ai_bets_cache()
    if not isinstance(cache, dict) or not cache:
        return 0

    pending_ids = []

    for date_str, bets in cache.items():
        if not isinstance(bets, list):
            continue

        for bet in bets:
            if not isinstance(bet, dict):
                continue

            status = bet.get("status")
            if status in ("won", "lost"):
                continue

            fixture_id = bet.get("fixture_id")
            if fixture_id is None:
                continue

            try:
                fid_int = int(fixture_id)
            except (TypeError, ValueError):
                continue

            if fid_int not in pending_ids:
                pending_ids.append(fid_int)

            if len(pending_ids) >= max_ids:
                break

        if len(pending_ids) >= max_ids:
            break

    if not pending_ids:
        # Nothing to do
        return 0

    ids_str = ",".join(str(fid) for fid in pending_ids)

    # 2) Call OddAlerts multiple fixtures endpoint
    params = {
        "ids": ids_str,
        "api_token": API_TOKEN,
        "include": "stats",
    }

    try:
        url = FIXTURES_MULTIPLE_URL
    except NameError:
        # Fallback in case the constant wasn't defined
        url = "https://data.oddalerts.com/api/fixtures/multiple"

    retries = 0
    max_retries = 5
    results_by_fixture = {}

    while retries <= max_retries:
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
            if resp.status_code == 429:
                # Rate limited – wait a bit and retry
                time.sleep(15)
                retries += 1
                continue

            resp.raise_for_status()
            payload = resp.json()
            data = payload.get("data", [])

            # 3) Build fixture_id -> (home_goals, away_goals) for finished games
            for item in data:
                fid = item.get("id")
                status = item.get("status")
                hg = item.get("home_goals")
                ag = item.get("away_goals")

                # Only settle when the game is clearly finished
                if status != "FT":
                    continue

                try:
                    fid_int = int(fid)
                    hg_int = int(hg) if hg is not None else None
                    ag_int = int(ag) if ag is not None else None
                except (TypeError, ValueError):
                    continue

                if hg_int is None or ag_int is None:
                    continue

                results_by_fixture[fid_int] = (hg_int, ag_int)

            break  # Successful response, exit retry loop

        except requests.RequestException as e:
            print(f"[AI BETS] Error fetching fixture results: {e}")
            retries += 1
            if retries > max_retries:
                return 0
            time.sleep(5)

    if not results_by_fixture:
        return 0

    # 4) Settle bets using our existing helper
    settled_count = settle_ai_bets_with_results(results_by_fixture)
    if settled_count:
        print(f"[AI BETS] Settled {settled_count} AI bets from API results.")

    return settled_count


@app.route("/ai-bets-results")
def ai_bets_results():
    """
    Read ai_bets_cache.json and prepare data for the AI Bets Results page.

    - Group bets by date (DD/MM/YYYY)
    - Compute daily P/L (sum of profit_units for settled bets)
    - Sort dates descending (most recent first)
    - Sort bets within each day by kickoff_iso (if available)
    """
    cache = load_ai_bets_cache()
    if not isinstance(cache, dict):
        cache = {}

    days = []

    for date_str, bets in cache.items():
        if not isinstance(bets, list):
            continue

        # Daily P/L: sum of profit_units where it's numeric
        daily_pl = 0.0
        has_any_profit = False

        for bet in bets:
            if not isinstance(bet, dict):
                continue
            profit = bet.get("profit_units")
            if isinstance(profit, (int, float)):
                daily_pl += float(profit)
                has_any_profit = True

        # Sort bets by kickoff_iso if present, otherwise fixture_name
        def bet_sort_key(b):
            kickoff_iso = b.get("kickoff_iso") or ""
            fixture_name = b.get("fixture_name") or ""
            return (kickoff_iso, fixture_name)

        sorted_bets = sorted(
            [b for b in bets if isinstance(b, dict)],
            key=bet_sort_key
        )

        # Try to convert date_str "DD/MM/YYYY" to a datetime for sorting
        try:
            sort_dt = datetime.strptime(date_str, "%d/%m/%Y")
        except ValueError:
            # If parsing fails, stick something neutral
            sort_dt = datetime.min

        days.append({
            "date_str": date_str,
            "sort_dt": sort_dt,
            "daily_pl": daily_pl if has_any_profit else None,
            "bets": sorted_bets,
        })

    # Sort days by date descending (most recent first)
    days.sort(key=lambda d: d["sort_dt"], reverse=True)

    return render_template("ai_bets_results.html", days=days)

def save_ai_bets_cards(cards: list) -> None:
    """
    Save the most recently generated AI bet cards (including HTML)
    so the AI Bets page can reload them after a refresh.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(AI_BETS_CARDS_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "cards": cards,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception as e:
        print(f"[AI BETS] Failed to save latest cards cache: {e}")


def load_ai_bets_cards() -> dict:
    """
    Load the most recently generated AI bet cards.

    Returns:
    {
        "generated_at": "...",
        "cards": [ ... ]
    }
    or {} if none exist.
    """
    if not os.path.exists(AI_BETS_CARDS_FILE):
        return {}
    try:
        with open(AI_BETS_CARDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[AI BETS] Failed to load latest cards cache: {e}")
        return {}


@app.template_filter("kickoff_time")
def kickoff_time_filter(kickoff_iso: str):
    """
    Extract the time (HH:MM) from an ISO datetime string.
    """
    if not kickoff_iso:
        return "-"
    try:
        dt = datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00"))
        # Convert to London time for display
        dt_london = dt.astimezone(ZoneInfo("Europe/London"))
        return dt_london.strftime("%H:%M")
    except Exception:
        return "-"

@app.route('/betslip_generator')
def betslip_generator():
    # Refresh predictability cache if expired or empty
    timestamp = predictability_cache.get("timestamp")
    if not timestamp or (datetime.now() - datetime.fromisoformat(timestamp)).total_seconds() > 36000:
        update_predictability_cache_from_fixtures(cached_fixtures)

    grouped_competitions = predictability_cache.get("data", {})
    return render_template('betslip_generator.html', grouped_competitions=grouped_competitions)

@app.route('/generate_betslip', methods=['POST'])
def generate_betslip():
    try:
        # Collect form data
        target_odds = float(request.form.get("target_odds", 2))
        items_per_slip = int(request.form.get("items_per_slip", 2))
        value_bets_only = request.form.get("value_bets_only", "false") == "true"
        max_betslips = int(request.form.get("max_betslips", 10))

        # Time duration in hours and convert to seconds
        time_duration_hours = int(request.form.get("time_duration", 24))
        time_duration_seconds = time_duration_hours * 3600

        # Collect selected markets and probabilities
        markets = []
        for market in request.form.getlist("markets"):
            min_prob = int(request.form.get(f"probability_min[{market}]", 0))
            max_prob = int(request.form.get(f"probability_max[{market}]", 100))
            market_id, outcome = market.split('|')
            markets.append({
                "id": market_id,
                "outcome": outcome,
                "range": [min_prob, max_prob]
            })

        # Collect selected competition IDs
        selected_competitions = request.form.getlist("competitions")
        competition_ids = [int(comp_id) for comp_id in selected_competitions]

        # Prepare payload
        payload = {
            "markets": markets,
            "value_bets_only": value_bets_only,
            "duration": time_duration_seconds,
            "target_odds": target_odds,
            "items_per_slip": items_per_slip,
            "odds_per_item": [
                float(request.form.get("odds_per_item_min", 1.3)),
                float(request.form.get("odds_per_item_max", 1.8))
            ],
            "competitions": competition_ids,  # Include selected competition IDs
        }

        # Initialize results list and process betslip results with pagination
        betslip_results = []
        page = 1

        while len(betslip_results) < max_betslips:
            # Include pagination parameter if supported (e.g. page=page)
            max_retries = 25
            retry_delay = 2  # seconds

            for attempt in range(max_retries):
                try:
                    response = requests.post(f"{BETSLIP_GENERATOR_URL}&page={page}", json=payload)
                    response.raise_for_status()
                    page_data = response.json().get('data', [])
                    break  # successful, exit loop
                except requests.exceptions.HTTPError as http_err:
                    if response.status_code == 429:
                        print(f"429 Too Many Requests. Retrying in {retry_delay} seconds. (Attempt {attempt + 1}/{max_retries})")
                        time.sleep(retry_delay)
                    else:
                        raise http_err  # other errors are not retried
            else:
                raise Exception("Max retries exceeded for betslip generator request")

            if not page_data:
                break  # Stop if there are no more results

            betslip_results.extend(page_data)
            page += 1

        # Limit the results to max_betslips after collecting all pages
        betslip_results = betslip_results[:max_betslips]

        # Process the betslip data
        processed_betslips = []
        for betslip in betslip_results:
            selections = betslip.get('selections', [])
            total_odds = betslip.get('total_odds', 'N/A')
            combined_probability = 1.0  # For calculating true combined probability

            for selection in selections:
                raw_prob = selection.get('probability', 0)

                # Safely convert probability to a float; fall back to 0 if it's invalid
                try:
                    probability = float(raw_prob)
                except (TypeError, ValueError):
                    probability = 0.0

                implied_odds = round(1 / (probability / 100), 2) if probability > 0 else "N/A"

                # Normalise probability to numeric for downstream use (templates etc.)
                selection['probability'] = probability
                selection['implied_odds'] = implied_odds
                selection['fixture_id'] = selection.get('fixture_id')

                # Use the numeric probability when building the combined probability
                if probability > 0:
                    combined_probability *= (probability / 100)

            true_combined_probability = round(combined_probability * 100, 2)
            implied_odds_combined = (
                round(1 / (true_combined_probability / 100), 2)
                if true_combined_probability > 0
                else "N/A"
            )

            if isinstance(total_odds, (int, float)) and isinstance(implied_odds_combined, (int, float)) and implied_odds_combined != 0:
                value_percentage = round(
                    ((total_odds - implied_odds_combined) / abs(implied_odds_combined)) * 100,
                    2
                )
                value_percentage_str = f"{value_percentage}%"
            else:
                value_percentage_str = "N/A"

            processed_betslips.append({
                "selections": selections,
                "total_odds": total_odds,
                "true_combined_probability": f"{true_combined_probability}%",
                "implied_odds_combined": implied_odds_combined,
                "value_percentage": value_percentage_str
            })

        # Sort processed betslips by true combined probability in descending order
        # Note: strip '%' and sort numerically to avoid string-sorting issues
        processed_betslips.sort(
            key=lambda x: float(str(x['true_combined_probability']).rstrip('%') or 0),
            reverse=True
        )

        return render_template('betslip_results.html', betslips=processed_betslips)

    except Exception as e:
        print(f"Error generating betslip: {e}")
        flash(f"Error generating betslip: {e}")
        return redirect(url_for('betslip_generator'))

@app.template_filter('custom_date')
def custom_date(value):
    from datetime import datetime
    try:
        # Try parsing as a formatted date string first
        date = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        try:
            # Try parsing as a Unix timestamp if the above fails
            date = datetime.fromtimestamp(int(value))
        except (ValueError, TypeError):
            return value  # Return original if both fail
    return date.strftime('%A %d %B %H:%M')
    
# somewhere temporary, just for testing
@app.route("/debug/settle-ai-bets-once")
def debug_settle_ai_bets_once():
    settled = refresh_ai_bets_results_once()
    return jsonify({"settled": settled})

def update_filtered_value_bets_results_job():
    print("[RESULTS] Updating filtered value bets results...")
    update_filtered_value_bets_results(API_TOKEN)
    print("[RESULTS] Filtered results updated.")


# =========================
# Scheduler Setup
# =========================
# Only run the scheduler if explicitly enabled via environment variable.
# e.g. RUN_SCHEDULER=1 in your environment (Render / local).
if os.environ.get("RUN_SCHEDULER") == "1":
    print("[SCHEDULER] RUN_SCHEDULER=1 → starting background jobs.")
    scheduler = BackgroundScheduler()

    # 🔁 Refresh fixtures cache regularly
    scheduler.add_job(
        refresh_fixtures_cache,
        "interval",
        minutes=30
    )

    # 🔁 Refresh value bets cache regularly
    scheduler.add_job(
        refresh_value_bets_cache,
        "interval",
        minutes=5
    )

    # ✅ Result qualified filtered bets regularly (safe + immediate first run)
    scheduler.add_job(
        update_filtered_value_bets_results_job,
        "interval",
        minutes=120,
        next_run_time=datetime.utcnow() + timedelta(minutes=2),
        id="filtered_results_settle",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # 🔁 Regularly refresh AI bet results (settle finished games)
    # This will look at ai_bets_cache.json, find pending bets,
    # call the OddAlerts fixtures/multiple API, and update statuses + profit.
    scheduler.add_job(
        refresh_ai_bets_results_once,
        "interval",
        minutes=5
    )

    # 🕖 Daily AI Bets generation at 07:00 London time
    # This ensures a fresh set of up to 6 bets each morning.
    scheduler.add_job(
        generate_ai_bets_for_today_if_missing,
        "cron",
        hour=7,
        minute=0,
        timezone=pytz.timezone("Europe/London"),
    )

    scheduler.start()

    # 🔁 Fail-safe: also attempt once at startup
    # If it's after 07:00 and today's date has no bets yet, generate them now.
    try:
        generated = generate_ai_bets_for_today_if_missing()
        if generated:
            print("[AI BETS] Generated today's AI bets at startup (fail-safe).")
    except Exception as e:
        print(f"[AI BETS] Error during startup fail-safe generate: {e}")

else:
    print("[SCHEDULER] RUN_SCHEDULER!=1 → scheduler disabled in this process.")

    # Even without the scheduler (e.g. local dev without RUN_SCHEDULER=1),
    # we can still try once at startup to ensure today's bets exist after 07:00.
    try:
        generated = generate_ai_bets_for_today_if_missing()
        if generated:
            print("[AI BETS] Generated today's AI bets at startup (no scheduler).")
    except Exception as e:
        print(f"[AI BETS] Error during startup AI bets generate (no scheduler): {e}")

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)
