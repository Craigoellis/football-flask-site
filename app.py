import time
import requests
import json
import os
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from datetime import datetime, timedelta, timezone

print(f"Flask process PID: {os.getpid()}")

app = Flask(__name__)

# =========================
# API Configuration
# =========================
API_TOKEN = "jraOCcvLm50fZyB0atU8rS1WBSPClsKvUw34374i1jySpRUM9Y41I34LwPub"  # Replace with your actual token
HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}

# Fixtures API
FIXTURES_API_URL = "https://data.oddalerts.com/api/probability/ft_result"
FIXTURES_CACHE_FILE = '/data/fixtures_cache.json'

# Value Bets API
VALUE_BETS_API_URL = "https://data.oddalerts.com/api/value/upcoming"
VALUE_BETS_CACHE_FILE = '/data/value_bets_cache.json'

# Season Stats API Cache
SEASON_STATS_CACHE_FILE = '/data/season_stats_cache.json'

# NEW: separate cache file + in-memory cache for "Last 25 Games"
SEASON_STATS_CACHE_FILE_LAST25 = "/data/season_stats_last25.json"
season_stats_cache_last25 = {}

# NEW: season stats sources (labels + URL templates)
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


# Betslip Generator API and Cache
BETSLIP_GENERATOR_URL = f"https://data.oddalerts.com/api/betslips?api_token={API_TOKEN}"
PREDICTABILITY_CACHE_FILE = '/data/predictability_cache.json'

# Set the secret key (needed for session management and flash messages)
app.secret_key = 'dev_secret_key'  # Replace 'dev_secret_key' with any string you like for local development

# =========================
# Load Cached Data at Startup
# =========================
if os.path.exists(FIXTURES_CACHE_FILE):
    with open(FIXTURES_CACHE_FILE, 'r') as f:
        try:
            cached_fixtures = json.load(f)
        except json.JSONDecodeError:
            cached_fixtures = {}

if os.path.exists(VALUE_BETS_CACHE_FILE):
    with open(VALUE_BETS_CACHE_FILE, 'r') as f:
        try:
            cached_value_bets = json.load(f)
        except json.JSONDecodeError:
            cached_value_bets = []

if os.path.exists(PREDICTABILITY_CACHE_FILE):
    with open(PREDICTABILITY_CACHE_FILE, 'r') as f:
        try:
            predictability_cache = json.load(f)
        except json.JSONDecodeError:
            predictability_cache = {"timestamp": None, "data": {}}
else:
    predictability_cache = {"timestamp": None, "data": {}}


if os.path.exists(SEASON_STATS_CACHE_FILE):
    with open(SEASON_STATS_CACHE_FILE, 'r') as f:
        try:
            season_stats_cache = json.load(f)
        except json.JSONDecodeError:
            season_stats_cache = {}
else:
    season_stats_cache = {}

# ---- Global Cache ----
cached_fixtures = {}

# ---- Fetch & Cache Functions ----

# ---- Global Cache ----
cached_fixtures = {}  # Only use this globally

# ---- Fetch & Cache Functions ----

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

    cached_fixtures = fixtures_by_date
    save_fixtures_cache_to_disk()  # Always save updated cache to disk

    unique_season_ids = set()
    for date_data in fixtures_by_date.values():
        for country_data in date_data.values():
            for league_fixtures in country_data.values():
                for fixture in league_fixtures:
                    season_id = fixture.get('season_id')
                    if season_id:
                        unique_season_ids.add(season_id)

    print(f"[CACHE] Fetched {len(unique_season_ids)} unique season IDs.")

    # ✅ Ensure a valid empty structure is always returned
    return fixtures_by_date or {}, unique_season_ids

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

    # Step 1.5: Update in-memory and disk cache
    cached_fixtures = fixtures_data
    save_fixtures_cache_to_disk()

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
H2H_CACHE_DIR = "/data/h2h"
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
        with open("/data/season_stats_cache_time.txt", "w", encoding="utf-8") as t:
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
        # optional timestamp file (mirrors your existing saver style)
        with open("/data/season_stats_last25_time.txt", "w", encoding="utf-8") as t:
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
GAME_DETAILS_CACHE_FILE = '/data/game_details_cache.json'

# --- Save Game Details Cache ---
def save_game_details_cache_to_disk():
    try:
        with open(GAME_DETAILS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(game_details_cache, f, indent=2)
        with open("/data/game_details_cache_time.txt", "w", encoding="utf-8") as t:
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

    # Reset stale cache before fetching fresh data
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

    game_details_cache = combined_data
    save_game_details_cache_to_disk()

    duration = round((time.time() - start_time) / 60, 2)
    print(f"[CACHE COMPLETE] Game details updated in {duration} minutes ✅")
    return combined_data


# --- At module level: LOAD at startup (do NOT SAVE here!) ---
load_game_details_cache_from_disk()

def get_game_details_cache_last_updated():
    try:
        with open("/data/game_details_cache_time.txt", "r", encoding="utf-8") as f:
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
        with open(FIXTURES_CACHE_FILE, 'r') as f:
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
    markets = request.args.getlist('market') or ['over_1_goals', 'over_2_goals', 'over_3_goals', 'home_win']

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

            # ✅ Market-specific stat gates
            if m == 'over_1_goals':
                passes_stats = _passes_over15_gate(hrow, arow, prob)
            elif m == 'over_2_goals':
                passes_stats = _passes_over25_gate(hrow, arow, prob)
            elif m == 'over_3_goals':
                passes_stats = _passes_over35_gate(hrow, arow, prob)
            elif m == 'home_win':
                passes_stats = _passes_homewin_gate(hrow, arow, prob)
            else:
                passes_stats = False
            if not passes_stats:
                continue

            # convert odds safely
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

            results.append(row)

    # sort by earliest kickoff, then probability
    results.sort(key=lambda x: (x.get("kickoff_unix", 0), -x.get("probability", 0)))

    # ✅ Split into four tables by market
    results_by_market = {
        "over_1_goals": [],
        "over_2_goals": [],
        "over_3_goals": [],
        "home_win": [],
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


# ---------------- Gates (probability + stats inside each) ---------------- #

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
    
# =========================
# Scheduler Setup
# =========================
scheduler = BackgroundScheduler()
scheduler.add_job(refresh_fixtures_cache, 'interval', minutes=10)
scheduler.add_job(refresh_value_bets_cache, 'interval', minutes=10)
scheduler.start()

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)
