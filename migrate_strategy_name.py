import json
import os

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

def backup(path):
    if os.path.exists(path):
        bak = path + ".bak"
        if not os.path.exists(bak):
            with open(path, "rb") as src, open(bak, "wb") as dst:
                dst.write(src.read())

def migrate_qualified():
    qualified = load_json(QUALIFIED_FILE, [])
    if not isinstance(qualified, list):
        raise ValueError(f"{QUALIFIED_FILE} should be a list")

    changed = 0
    for item in qualified:
        if not isinstance(item, dict):
            continue

        if item.get("matched_strategy") == OLD_NAME:
            item["matched_strategy"] = NEW_NAME
            changed += 1

        if item.get("strategy_name") == OLD_NAME:
            item["strategy_name"] = NEW_NAME
            changed += 1

        bk = item.get("bet_key")
        if isinstance(bk, str) and f":{OLD_NAME}:" in bk:
            parts = bk.split(":", 3)
            if len(parts) == 4:
                fixture_id, market, strat, bookmaker = parts
                if strat == OLD_NAME:
                    item["bet_key"] = f"{fixture_id}:{market}:{NEW_NAME}:{bookmaker}"
                    changed += 1

    save_json(QUALIFIED_FILE, qualified)
    return changed, len(qualified)

def migrate_results():
    results = load_json(RESULTS_FILE, {})
    if not isinstance(results, dict):
        raise ValueError(f"{RESULTS_FILE} should be a dict")

    new_results = {}
    changed = 0

    for bet_key, payload in results.items():
        new_key = bet_key

        if isinstance(bet_key, str) and f":{OLD_NAME}:" in bet_key:
            parts = bet_key.split(":", 3)
            if len(parts) == 4:
                fixture_id, market, strat, bookmaker = parts
                if strat == OLD_NAME:
                    new_key = f"{fixture_id}:{market}:{NEW_NAME}:{bookmaker}"
                    changed += 1

        if isinstance(payload, dict):
            if payload.get("matched_strategy") == OLD_NAME:
                payload["matched_strategy"] = NEW_NAME
            if payload.get("strategy_name") == OLD_NAME:
                payload["strategy_name"] = NEW_NAME

        if new_key not in new_results:
            new_results[new_key] = payload

    save_json(RESULTS_FILE, new_results)
    return changed, len(results)

if __name__ == "__main__":
    backup(QUALIFIED_FILE)
    backup(RESULTS_FILE)

    q_changed, q_total = migrate_qualified()
    r_changed, r_total = migrate_results()

    print(f"Qualified migrated: {q_changed} edits across {q_total} rows")
    print(f"Results migrated:   {r_changed} key edits across {r_total} entries")
    print("Done.")
