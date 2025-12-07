import os
import json

GAME_DETAILS_PATH = os.path.join("data", "game_details_cache.json")
SEASON_STATS_PATH = os.path.join("data", "season_stats_cache.json")

# Use the same fixture we saw earlier
TARGET_FIXTURE_ID = "420455951"  # Swindon Town vs Peterborough United


def load_json(path):
    if not os.path.exists(path):
        print(f"File not found: {path}")
        return None
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            print(f"JSON decode error in {path}: {e}")
            return None


def find_team_stats_in_block(block, team_id):
    """
    Given a 'block' that could be:
      - list of team dicts
      - dict keyed by team_id (or str(team_id))
    try to find the stats for the given team_id.
    """
    if block is None:
        return None

    # Dict keyed by team id
    for key in (team_id, str(team_id)):
        if isinstance(block, dict) and key in block:
            return block[key]

    # List of team dicts with team_id field
    if isinstance(block, list):
        for team in block:
            if isinstance(team, dict) and str(team.get("team_id")) == str(team_id):
                return team

    # Dict with "teams" list inside
    if isinstance(block, dict) and isinstance(block.get("teams"), list):
        for team in block["teams"]:
            if str(team.get("team_id")) == str(team_id):
                return team

    return None


def main():
    game_details = load_json(GAME_DETAILS_PATH)
    if not game_details:
        return

    fixture = game_details.get(TARGET_FIXTURE_ID)
    if not fixture:
        print(f"Fixture {TARGET_FIXTURE_ID} not found in game details cache.")
        return

    season_id = fixture.get("season_id")
    home_id = fixture.get("home_id")
    away_id = fixture.get("away_id")
    fixture_name = fixture.get("fixture_name")

    print(f"Fixture: {fixture_name}")
    print(f"season_id: {season_id}, home_id: {home_id}, away_id: {away_id}\n")

    season_stats_all = load_json(SEASON_STATS_PATH)
    if not season_stats_all:
        return

    # Season keys might be int or string
    season_data = season_stats_all.get(str(season_id)) or season_stats_all.get(season_id)
    if not season_data:
        print(f"No season stats found for season_id {season_id}")
        print(f"Available season keys: {list(season_stats_all.keys())[:10]}")
        return

    print(f"Season data type: {type(season_data)}")
    print("Season data keys:", list(season_data.keys()))

    # The interesting part is usually under "data"
    block = season_data.get("data")
    print("\nInner 'data' block type:", type(block))

    if isinstance(block, list):
        print(f"Length of data list: {len(block)}")
        if block:
            print("First item keys:", list(block[0].keys()))
    elif isinstance(block, dict):
        print("Keys of data dict:", list(block.keys()))

    home_stats = find_team_stats_in_block(block, home_id)
    away_stats = find_team_stats_in_block(block, away_id)

    print("\n--- Home team stats raw ---")
    if home_stats is None:
        print("Home team stats NOT found.")
    else:
        print(json.dumps(home_stats, indent=2))

    print("\n--- Away team stats raw ---")
    if away_stats is None:
        print("Away team stats NOT found.")
    else:
        print(json.dumps(away_stats, indent=2))


if __name__ == "__main__":
    main()
