import os
import json

GAME_DETAILS_PATH = os.path.join("data", "game_details_cache.json")


def main():
    if not os.path.exists(GAME_DETAILS_PATH):
        print(f"File not found: {GAME_DETAILS_PATH}")
        return

    with open(GAME_DETAILS_PATH, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            print(f"JSON decode error: {e}")
            return

    if not data:
        print("game_details_cache.json is empty (no top-level items).")
        return

    print(f"Top-level type: {type(data)}")
    if isinstance(data, dict):
        print(f"Number of fixtures (dict keys): {len(data)}")

        # Get first few fixtures
        fixture_ids = list(data.keys())[:3]
        print("\nFirst fixture IDs:", fixture_ids)

        for fid in fixture_ids:
            fixture = data[fid]
            print(f"\n=== Fixture ID: {fid} ===")
            if isinstance(fixture, dict):
                print("Fixture keys:", list(fixture.keys()))
                # Try some common possibilities
                if "markets" in fixture:
                    print(" -> 'markets' key found. Number of markets:",
                          len(fixture["markets"]) if isinstance(fixture["markets"], dict) else "not a dict")
                else:
                    print(" -> 'markets' key NOT found in this fixture.")

                # Show a small sample of the fixture dict
                for k, v in list(fixture.items())[:5]:
                    print(f"   {k}: {type(v)}")
            else:
                print(f"Fixture is not a dict, it is: {type(fixture)}")

    elif isinstance(data, list):
        print(f"Number of items in list: {len(data)}")
        first = data[0]
        print("\nFirst item type:", type(first))
        if isinstance(first, dict):
            print("First item keys:", list(first.keys()))
        print("\nFirst item sample:", first)
    else:
        print("Unexpected top-level JSON type:", type(data))


if __name__ == "__main__":
    main()
