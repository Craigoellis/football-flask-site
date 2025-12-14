import csv
import os
import glob

# ------------------------------------------------------
# CONFIG
# ------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

# Pattern for labelled daily files
LABELLED_PATTERN = os.path.join(DATA_DIR, "*-ai-bets-labelled.csv")

# Output master dataset
MASTER_CSV = os.path.join(DATA_DIR, "ai_bets_training_master.csv")


def build_master_dataset():
    labelled_files = sorted(glob.glob(LABELLED_PATTERN))
    if not labelled_files:
        print(f"[MASTER] No labelled files found matching: {LABELLED_PATTERN}")
        return

    print(f"[MASTER] Found {len(labelled_files)} labelled files.")

    all_rows = []
    master_fieldnames = None

    for path in labelled_files:
        print(f"[MASTER] Reading: {os.path.basename(path)}")
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []

            # Initialise master_fieldnames from the first file
            if master_fieldnames is None:
                master_fieldnames = fieldnames
            else:
                # If new columns appear later, extend the master header
                for fn in fieldnames:
                    if fn not in master_fieldnames:
                        master_fieldnames.append(fn)

            for row in reader:
                # Only keep rows where 'won' is set (match result known)
                won_val = (row.get("won") or "").strip()
                if won_val == "":
                    continue
                all_rows.append(row)

    if not all_rows:
        print("[MASTER] No labelled rows with 'won' values found.")
        return

    # Write master CSV
    print(f"[MASTER] Writing {len(all_rows)} rows to {MASTER_CSV}")
    with open(MASTER_CSV, "w", encoding="utf-8", newline="") as f_out:
        writer = csv.DictWriter(f_out, fieldnames=master_fieldnames)
        writer.writeheader()
        for row in all_rows:
            writer.writerow(row)

    print("[MASTER] Done. Master training file ready.")


if __name__ == "__main__":
    build_master_dataset()
