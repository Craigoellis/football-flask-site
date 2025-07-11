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

# =========================
# Fixtures Fetch & Cache
# =========================
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
                        "competition_predictability": item.get('competition_predictability', 'Unknown'),  # ✅ Add here
                        "competition_id": item.get('competition_id'),  # ✅ Add this line
                        "home_id": item.get('home_id'),   # ✅ Add this
                        "away_id": item.get('away_id'),    # ✅ Add this
                        "home_position": item.get("home_position"),
                        "away_position": item.get("away_position"),
                        "competition_country": country,            # ✅ ADD THIS
                        "competition_name": league  
                    })

            url = data.get('info', {}).get('next_page_url')
            retries = 0
            time.sleep(0.8)

        except requests.RequestException:
            break

    cached_fixtures = fixtures_by_date
    with open(FIXTURES_CACHE_FILE, 'w') as f:
        json.dump(fixtures_by_date, f)

    unique_season_ids = set()
    for date_data in fixtures_by_date.values():
        for country_data in date_data.values():
            for league_fixtures in country_data.values():
                for fixture in league_fixtures:
                    season_id = fixture.get('season_id')
                    if season_id:
                        unique_season_ids.add(season_id)

    print(f"[CACHE] Fetched {len(unique_season_ids)} unique season IDs.")
    return fixtures_by_date, unique_season_ids

def refresh_fixtures_cache():
    print("[CACHE] Starting full cache refresh...")

    # Step 1: Fetch Fixtures and Season IDs
    print("[CACHE] Refreshing Fixtures Cache...")
    fixtures_data, unique_season_ids = fetch_fixtures_grouped_by_structure(force_refresh=True)
    print("[CACHE] Fixtures Cache Updated.")

    # ✅ Step 1.5: Update Predictability Cache Immediately After Fixtures Are Updated
    update_predictability_cache_from_fixtures(fixtures_data)
    print("[CACHE] Predictability Cache Updated from Fixtures.")

    # Step 2: Fetch Season Stats
    print("[CACHE] Fetching Season Stats...")
    fetch_season_stats(unique_season_ids, API_TOKEN)
    print("[CACHE] Season Stats Cache Updated.")

    # Step 3: Fetch Game Details
    print("[CACHE] Fetching Game Details...")
    fetch_and_cache_all_game_details()
    print("[CACHE] Game Details Cache Updated.")

    print("[CACHE] Full Cache Refresh Completed Successfully.\n")


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
    with open(VALUE_BETS_CACHE_FILE, 'w') as f:
        json.dump(all_value_bets, f)

    return all_value_bets

def refresh_value_bets_cache():
    print("[CACHE] Refreshing Value Bets Cache...")
    fetch_value_bets(force_refresh=True)
    print("[CACHE] Value Bets Cache Updated.")

# =========================
# Game Details
# =========================

def fetch_season_stats(season_ids, api_token):
    cache_file = SEASON_STATS_CACHE_FILE
    cache_expiry_days = 3
    season_stats = {}

    # Load existing cache if it exists
    if os.path.exists(cache_file):
        with open(cache_file, 'r') as f:
            try:
                season_stats = json.load(f)
                print(f"[CACHE] {len(season_stats)} Season Stats already cached.")
            except json.JSONDecodeError:
                season_stats = {}

    current_time = datetime.utcnow().replace(tzinfo=timezone.utc)

    for season_id in season_ids:
        season_id_str = str(season_id)
        season_entry = season_stats.get(season_id_str)

        # Check if cache exists and is still valid
        if season_entry:
            last_updated_str = season_entry.get("last_updated")
            if last_updated_str:
                last_updated = datetime.fromisoformat(last_updated_str)
                if (current_time - last_updated).days < cache_expiry_days:
                    continue  # Cache still valid, skip fetch

        # Fetch fresh data if no cache or expired
        retries = 0
        url = f"https://data.oddalerts.com/api/stats/season/{season_id}?api_token={api_token}&include_frozen=false"

        while retries < 5:
            try:
                response = requests.get(url, headers=HEADERS)
                if response.status_code == 429:
                    time.sleep(15)
                    retries += 1
                    continue

                response.raise_for_status()
                data = response.json()

                # Store with timestamp
                season_stats[season_id_str] = {
                    "last_updated": current_time.isoformat(),
                    "data": data.get('data', [])
                }

                # Handle pagination if needed
                info = data.get('info', {})
                current_page = info.get('page', 1)
                total_pages = info.get('pages', 1)

                while current_page < total_pages:
                    current_page += 1
                    paginated_url = f"{url}&page={current_page}"
                    paginated_response = requests.get(paginated_url, headers=HEADERS)
                    paginated_response.raise_for_status()
                    paginated_data = paginated_response.json()
                    season_stats[season_id_str]["data"].extend(paginated_data.get('data', []))

                # ✅ Immediately save updated cache after each season_id fetch
                with open(cache_file, 'w') as f:
                    json.dump(season_stats, f)

                print(f"[CACHE] Cached Season ID {season_id}")
                break  # Successful fetch, exit retry loop

            except requests.RequestException:
                print(f"[ERROR] Failed to fetch season stats for Season ID {season_id}. Retry {retries + 1}/5.")
                retries += 1
                time.sleep(5)

    # ✅ Clean up any season IDs no longer needed
    existing_ids = set(str(sid) for sid in season_ids)
    cached_ids = set(season_stats.keys())
    unused_ids = cached_ids - existing_ids

    if unused_ids:
        for unused_id in unused_ids:
            del season_stats[unused_id]
            print(f"[CACHE] Removed stale Season ID {unused_id}")
        with open(cache_file, 'w') as f:
            json.dump(season_stats, f)

    print(f"[CACHE] Fetched and cached season stats for {len(season_stats)} Season IDs.")
    return season_stats

API_TOKEN = "jraOCcvLm50fZyB0atU8rS1WBSPClsKvUw34374i1jySpRUM9Y41I34LwPub"
GAME_DETAILS_CACHE_FILE = '/data/game_details_cache.json'

def fetch_and_cache_all_game_details():
    print(f"[CACHE] Refreshing Game Details Cache at {datetime.now().strftime('%H:%M:%S')}...")
    global cached_fixtures

    all_fixtures = {
        str(f.get("fixture_id"))
        for date_fixtures in cached_fixtures.values()
        for country_fixtures in date_fixtures.values()
        for league_fixtures in country_fixtures.values()
        for f in league_fixtures
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

    # ✅ Reset stale cache before fetching fresh data
    combined_data = {}

    def fetch_bookmaker_odds(bookmaker_id):
        url = f"https://data.oddalerts.com/api/fixtures/upcoming?api_token={API_TOKEN}&include=odds&bookmaker={bookmaker_id}"
        odds_map = {}
        retries = 5
        wait = 5

        for attempt in range(retries):
            try:
                res = requests.get(url)
                if res.status_code == 429:
                    print(f"[RETRY] Bookmaker {bookmaker_id} rate-limited. Waiting {wait} seconds...")
                    time.sleep(wait)
                    wait *= 2
                    continue

                res.raise_for_status()
                data = res.json().get("data", [])
                odds_map = {
                    str(f.get("id")): f.get("odds", {})
                    for f in data
                }
                break
            except Exception as e:
                print(f"[RETRY ERROR] Bookmaker {bookmaker_id} attempt {attempt + 1} failed: {e}")
                time.sleep(wait)
                wait *= 2
        else:
            print(f"[ERROR] Failed to fetch Bookmaker {bookmaker_id} odds after multiple retries.")
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
                # ✅ Add fixture-level data from cached_fixtures
                for date_fixtures in cached_fixtures.values():
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
                            "actual_odds": odds.get("ft_result", {}).get(base, "N/A")
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
                            "actual_odds": odds.get("ht_result", {}).get(base, "N/A")
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
                            "actual_odds": odds.get("double_chance", {}).get(label, "N/A")
                        }
                        add_all_bookmaker_odds(key, "double_chance", label)

                # BTTS
                btts_yes = probs.get("btts")
                if btts_yes is not None:
                    market_data["btts_yes"] = {
                        "probability": round(btts_yes, 2),
                        "implied_odds": round(100 / btts_yes, 2),
                        "actual_odds": odds.get("btts", {}).get("yes", "N/A")
                    }
                    add_all_bookmaker_odds("btts_yes", "btts", "yes")

                btts_no = probs.get("btts_no")
                if btts_no is not None:
                    market_data["btts_no"] = {
                        "probability": round(btts_no, 2),
                        "implied_odds": round(100 / btts_no, 2),
                        "actual_odds": odds.get("btts", {}).get("no", "N/A")
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
                            "actual_odds": odds.get("total_goals", {}).get(odds_over, "N/A")
                        }
                        add_all_bookmaker_odds(mkt_over, "total_goals", odds_over)

                        under_prob = round(100 - over_prob, 2)
                        market_data[mkt_under] = {
                            "probability": under_prob,
                            "implied_odds": round(100 / under_prob, 2),
                            "actual_odds": odds.get("total_goals", {}).get(odds_under, "N/A")
                        }
                        add_all_bookmaker_odds(mkt_under, "total_goals", odds_under)

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
                                "actual_odds": odds.get(f"{team_type}_goals", {}).get(odds_key, "N/A")
                            }
                            add_all_bookmaker_odds(market_key, f"{team_type}_goals", odds_key)

                            under_market = market_key.replace("_o", "_u")
                            under_prob = round(100 - prob, 2)
                            under_odds_key = f"under_{key_suffix}"
                            market_data[under_market] = {
                                "probability": under_prob,
                                "implied_odds": round(100 / under_prob, 2),
                                "actual_odds": odds.get(f"{team_type}_goals", {}).get(under_odds_key, "N/A")
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
                            "actual_odds": odds.get("total_corners", {}).get(odds_over_key, "N/A")
                        }
                        add_all_bookmaker_odds(market_over, "total_corners", odds_over_key)

                        under_prob = round(100 - over_prob, 2)
                        market_data[market_under] = {
                            "probability": under_prob,
                            "implied_odds": round(100 / under_prob, 2),
                            "actual_odds": odds.get("total_corners", {}).get(odds_under_key, "N/A")
                        }
                        add_all_bookmaker_odds(market_under, "total_corners", odds_under_key)

            url = data.get("info", {}).get("next_page_url")
            time.sleep(0.8)

        except requests.RequestException as e:
            print(f"[ERROR] Failed to fetch game details: {e}")
            break

    with open(GAME_DETAILS_CACHE_FILE, "w") as f:
        json.dump(combined_data, f)

    duration = round((time.time() - start_time) / 60, 2)
    print(f"[CACHE COMPLETE] Game details updated in {duration} minutes ✅")
    return combined_data


def load_game_details_cache():
    if not os.path.exists(GAME_DETAILS_CACHE_FILE):
        return fetch_and_cache_all_game_details()

    with open(GAME_DETAILS_CACHE_FILE, "r") as f:
        try:
            data = json.load(f)
            return data if data else fetch_and_cache_all_game_details()
        except json.JSONDecodeError:
            return fetch_and_cache_all_game_details()

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

                print(f"[DEBUG] Processing League: {league} | Predictability: {predictability} | Competition ID: {competition_id}")

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
    return render_template('home.html')

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
    fixtures_by_date, _ = fetch_fixtures_grouped_by_structure()  # ✅ Unpack the tuple correctly
    fixtures_by_date = sort_fixtures_structure(fixtures_by_date)  # ✅ Now this works on the correct data
    dates = sorted(fixtures_by_date.keys())
    formatted_dates = [(date, datetime.strptime(date, '%Y-%m-%d').strftime('%a %d %b')) for date in dates]
    today_date = datetime.utcnow().strftime('%Y-%m-%d')
    fixtures = fixtures_by_date.get(today_date, {})
    return render_template('fixtures.html', fixtures=fixtures, dates=formatted_dates, selected_date=today_date, today_date=today_date)

@app.route('/fixtures/<selected_date>')
def fixtures_by_date(selected_date):
    fixtures_by_date, _ = fetch_fixtures_grouped_by_structure()  # ✅ Unpack correctly
    fixtures_by_date = sort_fixtures_structure(fixtures_by_date)
    dates = sorted(fixtures_by_date.keys())
    formatted_dates = [(date, datetime.strptime(date, '%Y-%m-%d').strftime('%a %d %b')) for date in dates]
    fixtures = fixtures_by_date.get(selected_date, {})
    return render_template('fixtures.html', fixtures=fixtures, dates=formatted_dates, selected_date=selected_date, today_date=datetime.utcnow().strftime('%Y-%m-%d'))

@app.template_filter('datetimeformat')
def datetimeformat(value):
    london_tz = pytz.timezone('Europe/London')
    dt = datetime.fromtimestamp(value, pytz.utc).astimezone(london_tz)
    return dt.strftime('%d/%m/%Y %H:%M')

@app.route('/game/<int:fixture_id>')
def game_details(fixture_id):
    fixture_name = None
    kick_off_time = None
    home_team = None
    away_team = None
    season_id = None
    home_id = None
    away_id = None
    home_position = None
    away_position = None  # ✅ Add these

    # ✅ Search once and grab all required details
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
                        home_position = game.get("home_position")  # ✅
                        away_position = game.get("away_position")  # ✅
                        if " vs " in fixture_name:
                            home_team, away_team = fixture_name.split(" vs ")
                        break

    if fixture_name is None:
        return f"No data found for Fixture ID: {fixture_id}", 404

    # Load cached game details data
    all_game_data = load_game_details_cache()
    game_data = all_game_data.get(str(fixture_id), {})

    # ✅ Match season stats by team_id
    home_stats = {}
    away_stats = {}
    if season_id:
        season_stats_data = season_stats_cache.get(str(season_id), {}).get("data", [])
        for team_data in season_stats_data:
            team_id = team_data.get("team_id")
            if team_id == home_id:
                home_stats = team_data
            elif team_id == away_id:
                away_stats = team_data

    return render_template(
        'game_details.html',
        fixture_name=fixture_name,
        kick_off_time=kick_off_time,
        home_team=home_team,
        away_team=away_team,
        home_position=home_position,  # ✅ Pass it in
        away_position=away_position,  # ✅ Pass it in
        game_data=game_data,
        home_stats=home_stats,
        away_stats=away_stats,
        fixture_id=fixture_id,            # ✅ Add this
        api_token=API_TOKEN    
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

    try:
        with open(GAME_DETAILS_CACHE_FILE, 'r') as f:
            game_details = json.load(f)
    except json.JSONDecodeError as e:
        print("JSONDecodeError in game_details_cache.json:", e)
        game_details = {}

    with open(FIXTURES_CACHE_FILE, 'r') as f:
        fixtures_data = json.load(f)
        

    fixture_lookup = {}
    for date, countries in fixtures_data.items():
        for country, leagues in countries.items():
            for league, fixtures in leagues.items():
                for fixture in fixtures:
                    fixture_id = str(fixture["fixture_id"])
                    kick_off = datetime.fromtimestamp(fixture["unix"]).strftime('%Y-%m-%d')
                    fixture_lookup[fixture_id] = {
                        "name": fixture["fixture_name"],
                        "kick_off": kick_off,
                        "kickoff_unix": fixture["unix"],
                        "league": league,
                        "country": country
                    }

    results = []
    for fixture_id, markets in game_details.items():
        info = fixture_lookup.get(fixture_id)
        if info and info["kick_off"] == selected_date:
            market_data = markets.get(selected_market)
            if market_data and isinstance(market_data, dict):
                actual_odds = market_data.get("actual_odds")
                implied_odds = market_data.get("implied_odds")

                # Only apply actual odds filter to markets that are expected to have them
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

                results.append({
                    "fixture_id": fixture_id,
                    "fixture_name": info["name"],
                    "kickoff": datetime.fromtimestamp(info["kickoff_unix"]).strftime('%H:%M'),
                    "league": info["league"],
                    "country": info["country"],
                    "probability": probability,
                    "implied_odds": implied_odds,
                    "actual_odds": actual_odds
                })

    results.sort(key=lambda x: x["probability"], reverse=True)

    available_markets = [
        "home_win", "draw", "away_win",
        "home_win_ht", "draw_ht", "away_win_ht",
        "double_chance_1x", "double_chance_x2", "double_chance_12",
        "btts_yes", "btts_no",
        "over_1_goals", "over_2_goals", "over_3_goals",
        "under_1_goals", "under_2_goals", "under_3_goals",
        "home_o05", "home_o15", "home_o25",
        "home_u05", "home_u15", "home_u25",
        "away_o05", "away_o15", "away_o25",
        "away_u05", "away_u15", "away_u25",
        "over_7_corners", "over_8_corners", "over_9_corners", "over_10_corners", "over_11_corners",
        "under_7_corners", "under_8_corners", "under_9_corners", "under_10_corners", "under_11_corners",
        "home_score_first", "draw_score_first", "away_score_first",
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
        "home_o05": "Home Over 0.5", "home_o15": "Home Over 1.5", "home_o25": "Home Over 2.5",
        "home_u05": "Home Under 0.5", "home_u15": "Home Under 1.5", "home_u25": "Home Under 2.5",
        "away_o05": "Away Over 0.5", "away_o15": "Away Over 1.5", "away_o25": "Away Over 2.5",
        "away_u05": "Away Under 0.5", "away_u15": "Away Under 1.5", "away_u25": "Away Under 2.5",
        "over_7_corners": "Over 7.5 Corners", "over_8_corners": "Over 8.5 Corners", "over_9_corners": "Over 9.5 Corners",
        "over_10_corners": "Over 10.5 Corners", "over_11_corners": "Over 11.5 Corners",
        "under_7_corners": "Under 7.5 Corners", "under_8_corners": "Under 8.5 Corners", "under_9_corners": "Under 9.5 Corners",
        "under_10_corners": "Under 10.5 Corners", "under_11_corners": "Under 11.5 Corners",
        "home_score_first": "Home Scores First", "draw_score_first": "No Goals First", "away_score_first": "Away Scores First",


    }

    return render_template(
        'probability_rankings.html',
        selected_date=selected_date,
        selected_market=selected_market,
        market_rows=results,
        available_markets=available_markets,
        odds_filter=odds_filter,
        value_filter=value_filter,
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
            "fixture_id": bet["id"]
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
        "home_u05": "Home Under 0.5 Goals", "home_u05": "Home Under 1.5 Goals", "home_u05": "Home Under 2.5 Goals",
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
        with open(VALUE_BETS_CACHE_FILE, "r") as f:
            filtered_bets = json.load(f)
        
        print(f"Filter route cache loaded: {len(filtered_bets)} bets")

        selected_bookmakers = request_data.get("bookmakers", [])
        selected_predictability = request_data.get("predictability", [])
        exclude_cups = request_data.get("exclude_cups", False)
        exclude_friendlies = request_data.get("exclude_friendlies", False)

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

        home_win_ht_filters = request_data.get("home_win_ht_filters", {})
        include_home_win_ht = home_win_ht_filters.get("include", True)
        home_win_ht_prob_min = home_win_ht_filters.get("probability_min", 0)
        home_win_ht_prob_max = home_win_ht_filters.get("probability_max", 100)
        home_win_ht_odds_min = home_win_ht_filters.get("odds_min", 1.00)
        home_win_ht_odds_max = home_win_ht_filters.get("odds_max", 10.00)
        home_win_ht_value_min = home_win_ht_filters.get("value_min", 0)
        home_win_ht_value_max = home_win_ht_filters.get("value_max", 100)

        draw_ht_filters = request_data.get("draw_ht_filters", {})
        include_draw_ht = draw_ht_filters.get("include", True)
        draw_ht_prob_min = draw_ht_filters.get("probability_min", 0)
        draw_ht_prob_max = draw_ht_filters.get("probability_max", 100)
        draw_ht_odds_min = draw_ht_filters.get("odds_min", 1.00)
        draw_ht_odds_max = draw_ht_filters.get("odds_max", 10.00)
        draw_ht_value_min = draw_ht_filters.get("value_min", 0)
        draw_ht_value_max = draw_ht_filters.get("value_max", 100)

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

        btts_filters = request_data.get("btts_filters", {})
        include_btts = btts_filters.get("include", True)
        btts_prob_min = btts_filters.get("probability_min", 0)
        btts_prob_max = btts_filters.get("probability_max", 100)
        btts_odds_min = btts_filters.get("odds_min", 1.00)
        btts_odds_max = btts_filters.get("odds_max", 10.00)
        btts_value_min = btts_filters.get("value_min", 0)
        btts_value_max = btts_filters.get("value_max", 100)

        home_o15_filters = request_data.get("home_o15_filters", {})
        include_home_o15 = home_o15_filters.get("include", True)
        home_o15_prob_min = home_o15_filters.get("probability_min", 0)
        home_o15_prob_max = home_o15_filters.get("probability_max", 100)
        home_o15_odds_min = home_o15_filters.get("odds_min", 1.00)
        home_o15_odds_max = home_o15_filters.get("odds_max", 10.00)
        home_o15_value_min = home_o15_filters.get("value_min", 0)
        home_o15_value_max = home_o15_filters.get("value_max", 100)

        away_o15_filters = request_data.get("away_o15_filters", {})
        include_away_o15 = away_o15_filters.get("include", True)
        away_o15_prob_min = away_o15_filters.get("probability_min", 0)
        away_o15_prob_max = away_o15_filters.get("probability_max", 100)
        away_o15_odds_min = away_o15_filters.get("odds_min", 1.00)
        away_o15_odds_max = away_o15_filters.get("odds_max", 10.00)
        away_o15_value_min = away_o15_filters.get("value_min", 0)
        away_o15_value_max = away_o15_filters.get("value_max", 100)

        o85_filters = request_data.get("o85_filters", {})
        include_o85 = o85_filters.get("include", True)
        o85_prob_min = o85_filters.get("probability_min", 0)
        o85_prob_max = o85_filters.get("probability_max", 100)
        o85_odds_min = o85_filters.get("odds_min", 1.00)
        o85_odds_max = o85_filters.get("odds_max", 10.00)
        o85_value_min = o85_filters.get("value_min", 0)
        o85_value_max = o85_filters.get("value_max", 100)

        if not selected_bookmakers and not selected_predictability and not selected_markets and not exclude_cups and not exclude_friendlies:
            return jsonify([])  # Return empty list if no filters are applied

        # Get cached data
        value_bets_data = fetch_value_bets()

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

            # Filter odds based on selected bookmakers and remove negative values
            filtered_odds = [odd for odd in bet["odds"] if "bookmaker_name" in odd and odd["bookmaker_name"] in selected_bookmakers and float(odd.get("value", 0)) >= 0]

            # If no odds match the filter after removing negatives, skip this bet
            if not filtered_odds:
                continue

            # Find the bookmaker with the highest latest odds from selected bookmakers
            best_odds = max(filtered_odds, key=lambda x: float(x.get("latest", 0)))
            bookmaker_name = best_odds.get("bookmaker_name", "N/A")
            latest_odds = float(best_odds.get("latest", 0))
            value_percentage = float(best_odds.get("value", 0))

            # Get predictability, ensure it's always a string
            predictability = str(bet["competition"].get("predictability", "Unknown")).capitalize()

            # Apply predictability filter if selected
            if selected_predictability and predictability not in selected_predictability:
                continue  # Skip if predictability doesn't match

            # Apply Home Win FT Result filters
            if include_home_win and bet["market"] == "home_win_probability":
                probability = bet.get("probability", 0)
                if not (home_win_prob_min <= probability <= home_win_prob_max):
                    continue
                if not (home_win_odds_min <= latest_odds <= home_win_odds_max):
                    continue
                if not (home_win_value_min <= value_percentage <= home_win_value_max):
                    continue
            elif not include_home_win and bet["market"] == "home_win_probability":
                continue  # Skip this market entirely if not included

            # Apply Draw FT Result filters
            if include_draw and bet["market"] == "draw_probability":
                probability = bet.get("probability", 0)
                if not (draw_prob_min <= probability <= draw_prob_max):
                    continue
                if not (draw_odds_min <= latest_odds <= draw_odds_max):
                    continue
                if not (draw_value_min <= value_percentage <= draw_value_max):
                    continue
            elif not include_draw and bet["market"] == "draw_probability":
                continue  # Skip draw market entirely if not included

            # Apply Away Win FT Result filters
            if include_away_win and bet["market"] == "away_win_probability":
                probability = bet.get("probability", 0)
                if not (away_win_prob_min <= probability <= away_win_prob_max):
                    continue
                if not (away_win_odds_min <= latest_odds <= away_win_odds_max):
                    continue
                if not (away_win_value_min <= value_percentage <= away_win_value_max):
                    continue
            elif not include_away_win and bet["market"] == "away_win_probability":
                continue  # Skip away win market entirely if not included

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
                "fixture_id": bet["id"]  # ✅ Add this line if not already present
            })

        return jsonify(table_data)

    except Exception as e:
        print(f"🚨 Error in /filter_value_bets: {e}")
        return jsonify({"error": str(e)}), 500

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
            # Include pagination parameter if supported (e.g., page=page)
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
                        print(f"429 Too Many Requests. Retrying in {retry_delay} seconds... (Attempt {attempt + 1}/{max_retries})")
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
            combined_probability = 1  # For calculating true combined probability

            for selection in selections:
                probability = selection.get('probability', 0)
                implied_odds = round(1 / (probability / 100), 2) if probability > 0 else "N/A"
                selection['implied_odds'] = implied_odds
                selection['fixture_id'] = selection.get('fixture_id')  # ✅ Add this line
                combined_probability *= (probability / 100)

            true_combined_probability = round(combined_probability * 100, 2)
            implied_odds_combined = round(1 / (true_combined_probability / 100), 2) if true_combined_probability > 0 else "N/A"
            value_percentage = round(((total_odds - implied_odds_combined) / abs(implied_odds_combined)) * 100, 2) if isinstance(total_odds, (int, float)) else "N/A"

            processed_betslips.append({
                "selections": selections,
                "total_odds": total_odds,
                "true_combined_probability": f"{true_combined_probability}%",
                "implied_odds_combined": implied_odds_combined,
                "value_percentage": f"{value_percentage}%"
            })

        # Sort processed betslips by true combined probability in descending order
        processed_betslips.sort(key=lambda x: x['true_combined_probability'], reverse=True)

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
scheduler.add_job(refresh_fixtures_cache, 'interval', minutes=1)
scheduler.add_job(refresh_value_bets_cache, 'interval', minutes=10)
scheduler.start()

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)
