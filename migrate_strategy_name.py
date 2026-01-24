# migrate_strategy_name.py
import json
import os

# Server paths (your app uses the files in /data)
QUALIFIED_FILE = "data/filtered_value_bets_qualified.json"
RESULTS_FILE   = "data/filtered_value_bets_results.json"

OLD_NAME = "Home Win V1"
NEW_NAME = "Home Win V1 (League Only)"


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def backup_file(path):
    if os.path.exists(path):
        bak = path + ".bak"
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        with open(bak, "w", encoding="utf-8") as f:
            f.write(content)


def migrate_qualified():
    """
    Your qualified file is a DICT keyed by bet_key -> item.
    We rename:
      - the key bet_key (strategy segment)
      - any embedded matched_strategy/strategy_name fields
    """
    qualified = load_json(QUALIFIED_FILE, {})
    if not isinstance(qualified, dict):
        raise ValueError(f"{QUALIFIED_FILE} should be a dict")

    backup_file(QUALIFIED_FILE)

    changed_keys = 0
    changed_fields = 0
    new_qualified = {}

    for bet_key, item in qualified.items():
        new_key = bet_key

        # bet_key format: fixture_id:market:strategy_name:bookmaker
        if isinstance(bet_key, str) and f":{OLD_NAME}:" in bet_key:
            parts = bet_key.split(":", 3)
            if len(parts) == 4:
                fixture_id, market, strat, bookmaker = parts
                if strat == OLD_NAME:
                    new_key = f"{fixture_id}:{market}:{NEW_NAME}:{bookmaker}"
                    changed_keys += 1

        if isinstance(item, dict):
            if item.get("matched_strategy") == OLD_NAME:
                item["matched_strategy"] = NEW_NAME
                changed_fields += 1
            if item.get("strategy_name") == OLD_NAME:
                item["strategy_name"] = NEW_NAME
                changed_fields += 1

        # If collisions happen, prefer the existing entry already under new_key
        if new_key not in new_qualified:
            new_qualified[new_key] = item

    save_json(QUALIFIED_FILE, new_qualified)
    return changed_keys, changed_fields, len(new_qualified)


def migrate_results():
    """
    Results file is a DICT keyed by bet_key -> payload.
    We rename:
      - the key bet_key (strategy segment)
      - any embedded matched_strategy/strategy_name fields if present
    """
    results = load_json(RESULTS_FILE, {})
    if not isinstance(results, dict):
        raise ValueError(f"{RESULTS_FILE} should be a dict")

    backup_file(RESULTS_FILE)

    changed_keys = 0
    changed_fields = 0
    new_results = {}

    for bet_key, payload in results.items():
        new_key = bet_key

        if isinstance(bet_key, str) and f":{OLD_NAME}:" in bet_key:
            parts = bet_key.split(":", 3)
            if len(parts) == 4:
                fixture_id, market, strat, bookmaker = parts
                if strat == OLD_NAME:
                    new_key = f"{fixture_id}:{market}:{NEW_NAME}:{bookmaker}"
                    changed_keys += 1

        if isinstance(payload, dict):
            if payload.get("matched_strategy") == OLD_NAME:
                payload["matched_strategy"] = NEW_NAME
                changed_fields += 1
            if payload.get("strategy_name") == OLD_NAME:
                payload["strategy_name"] = NEW_NAME
                changed_fields += 1

        if new_key not in new_results:
            new_results[new_key] = payload

    save_json(RESULTS_FILE, new_results)
    return changed_keys, changed_fields, len(new_results)


if __name__ == "__main__":
    q_key_edits, q_field_edits, q_total = migrate_qualified()
    r_key_edits, r_field_edits, r_total = migrate_results()

    print(f"Qualified migrated: {q_key_edits} key edits, {q_field_edits} field edits across {q_total} rows")
    print(f"Results migrated:   {r_key_edits} key edits, {r_field_edits} field edits across {r_total} entries")
    print("Done.")
