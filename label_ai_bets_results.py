import csv
import os
import sys
import time
import requests

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

# Hard-coded OddAlerts API token
ODDALERTS_API_TOKEN = "jraOCcvLm50fZyB0atU8rS1WBSPClsKvUw34374i1jySpRUM9Y41I34LwPub"

# Max fixture IDs per request as per OddAlerts docs (you said 50)
MAX_IDS_PER_REQUEST = 50


def chunk_list(lst, chunk_size):
    """Yield successive chunk_size-sized chunks from lst."""
    for i in range(0, len(lst), chunk_size):
        yield lst[i:i + chunk_size]


def fetch_results_from_api(fixture_ids):
    """
    Call OddAlerts fixtures/multiple endpoint for the given fixture_ids
    and return a dict:
        fixture_id (str) -> "home_win" / "draw" / "away_win"
    Only label if status == "FT".
    """
    results_map = {}

    if not ODDALERTS_API_TOKEN:
        print("[LABEL] ERROR: ODDALERTS_API_TOKEN is not set.")
        print("        Set it in this script or via environment variable.")
        return results_map

    all_ids = [str(fid) for fid in fixture_ids if fid]

    print(f"[LABEL] Fetching results for {len(all_ids)} fixture IDs from OddAlerts...")

    for chunk in chunk_list(all_ids, MAX_IDS_PER_REQUEST):
        ids_str = ",".join(chunk)
        url = (
            "https://data.oddalerts.com/api/fixtures/multiple"
            f"?ids={ids_str}"
            f"&api_token={ODDALERTS_API_TOKEN}"
            "&include=stats"
        )

        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"[LABEL] Error fetching chunk {ids_str[:60]}...: {e}")
            continue

        fixtures = data.get("data", [])
        print(f"[LABEL] Received {len(fixtures)} fixtures for chunk ({len(chunk)} ids).")

        for fx in fixtures:
            fx_id = str(fx.get("id"))
            status = fx.get("status")
            home_goals = fx.get("home_goals")
            away_goals = fx.get("away_goals")

            # Only label if match is finished
            if status != "FT":
                continue

            # Work out result
            try:
                hg = int(home_goals)
                ag = int(away_goals)
            except (TypeError, ValueError):
                continue

            if hg > ag:
                outcome = "home_win"
            elif ag > hg:
                outcome = "away_win"
            else:
                outcome = "draw"

            results_map[fx_id] = outcome

        # small pause to be gentle with the API (optional)
        time.sleep(0.2)

    print(f"[LABEL] Built results map for {len(results_map)} fixtures (FT only).")
    return results_map


def label_file(input_csv_path, output_csv_path, results_map):
    """
    Read an AI bets CSV, fill 'won' and 'profit_units'
    using the results_map, then write a new labelled CSV.
    """
    if not os.path.exists(input_csv_path):
        print(f"[LABEL] Input file not found: {input_csv_path}")
        return

    rows = []
    with open(input_csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []

        # Ensure 'won' and 'profit_units' exist as columns
        if "won" not in fieldnames:
            fieldnames.append("won")
        if "profit_units" not in fieldnames:
            fieldnames.append("profit_units")

        for row in reader:
            fixture_id = str(row.get("fixture_id", "")).strip()

            # In your AI CSV, market_type should be one of:
            # "home_win", "draw", "away_win"
            market_type = str(row.get("market_type", "")).strip()
            actual_odds_raw = str(row.get("actual_odds", "")).strip()

            # Default blanks
            row["won"] = ""
            row["profit_units"] = ""

            # If we don't have a result for this fixture, leave blanks
            actual_outcome = results_map.get(fixture_id)
            if not actual_outcome:
                rows.append(row)
                continue

            # Only label if this row is one of the three result markets
            if market_type not in ("home_win", "draw", "away_win"):
                rows.append(row)
                continue

            # Determine if bet won
            if market_type == actual_outcome:
                row["won"] = "1"
                try:
                    # 1 unit stake: profit = (odds - 1), e.g. odds 2.5 => +1.5
                    odds = float(actual_odds_raw)
                    row["profit_units"] = f"{odds - 1:.2f}"
                except Exception:
                    # If odds missing/invalid, fall back to +1 unit
                    row["profit_units"] = "1.00"
            else:
                # Lost bet: -1 unit
                row["won"] = "0"
                row["profit_units"] = "-1.00"

            rows.append(row)

    # Write labelled file
    with open(output_csv_path, "w", encoding="utf-8", newline="") as f_out:
        writer = csv.DictWriter(f_out, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    print(f"[LABEL] Wrote labelled file: {output_csv_path}")
    print(f"[LABEL] Rows processed: {len(rows)}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python label_ai_bets_results.py <input_ai_bets_csv>")
        print("Example: python label_ai_bets_results.py data\\2025-12-07-ai-bets.csv")
        return

    input_csv_path = sys.argv[1]
    if not os.path.isabs(input_csv_path):
        input_csv_path = os.path.join(BASE_DIR, input_csv_path)

    if not os.path.exists(input_csv_path):
        print(f"[LABEL] File not found: {input_csv_path}")
        return

    # Suggest an output path: same name but with -labelled
    base_name = os.path.basename(input_csv_path)
    name, ext = os.path.splitext(base_name)
    labelled_name = f"{name}-labelled{ext}"
    output_csv_path = os.path.join(os.path.dirname(input_csv_path), labelled_name)

    # 1) Collect all unique fixture_ids from the AI bets CSV
    fixture_ids = set()
    with open(input_csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            fid = str(row.get("fixture_id", "")).strip()
            if fid:
                fixture_ids.add(fid)

    print(f"[LABEL] Found {len(fixture_ids)} unique fixture_ids in {input_csv_path}")

    if not fixture_ids:
        print("[LABEL] No fixture_ids found in file – nothing to do.")
        return

    # 2) Fetch results from OddAlerts
    results_map = fetch_results_from_api(sorted(fixture_ids))
    if not results_map:
        print("[LABEL] No FT results returned – nothing will be labelled.")
        return

    # 3) Label the AI bets CSV
    label_file(input_csv_path, output_csv_path, results_map)


if __name__ == "__main__":
    main()
