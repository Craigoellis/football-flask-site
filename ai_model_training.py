import os
import pandas as pd

from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report, roc_auc_score

import joblib

# ------------------------------------------------------
# PATHS
# ------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
MASTER_CSV = os.path.join(DATA_DIR, "ai_bets_training_master.csv")

MODELS_DIR = os.path.join(BASE_DIR, "models")
os.makedirs(MODELS_DIR, exist_ok=True)
MODEL_PATH = os.path.join(MODELS_DIR, "ai_bets_logreg.pkl")


# ------------------------------------------------------
# STEP 1 – LOAD RAW DATA
# ------------------------------------------------------
def load_training_data():
    """
    Load the master AI bets training CSV into a pandas DataFrame.

    - Checks the file exists.
    - Loads all columns.
    - Converts 'won' and 'profit_units' to numeric where possible.
    - Returns the DataFrame.
    """
    if not os.path.exists(MASTER_CSV):
        print(f"[LOAD] ERROR: Master training file not found:\n  {MASTER_CSV}")
        print("[LOAD] Make sure you've run build_ai_bets_training_dataset.py first.")
        return None

    print(f"[LOAD] Loading training data from:\n  {MASTER_CSV}")
    df = pd.read_csv(MASTER_CSV)

    print(f"[LOAD] Raw shape: {df.shape[0]} rows, {df.shape[1]} columns")

    # Ensure 'won' is numeric (0/1) and 'profit_units' is numeric
    if "won" in df.columns:
        df["won"] = pd.to_numeric(df["won"], errors="coerce")
    else:
        print("[LOAD] WARNING: 'won' column not found in dataset.")

    if "profit_units" in df.columns:
        df["profit_units"] = pd.to_numeric(df["profit_units"], errors="coerce")
    else:
        print("[LOAD] WARNING: 'profit_units' column not found in dataset.")

    # Print some basic info
    print("\n[LOAD] Columns:")
    for col in df.columns:
        print(f"  - {col}")

    if "won" in df.columns:
        print("\n[LOAD] 'won' value counts:")
        print(df["won"].value_counts(dropna=False))

    # Just show the first few rows so you can visually inspect
    print("\n[LOAD] Head (first 5 rows):")
    print(df.head())

    return df


# ------------------------------------------------------
# STEP 2 – BUILD FEATURES (X) AND TARGET (y)
# ------------------------------------------------------
def prepare_features(df: pd.DataFrame):
    """
    Take the raw training DataFrame and build:

      X – feature DataFrame
      y – target Series (won: 0/1)

    - Filters to rows with a known 'won' value.
    - Keeps only result markets (home_win/draw/away_win).
    - Adds numeric features from season stats + market info.
    - Encodes market_type and competition_predictability.
    - Extracts date-based features from kickoff_date.
    """

    if "won" not in df.columns:
        print("[FEAT] ERROR: 'won' column missing – cannot build target.")
        return None, None

    # Start from a copy so we don't mutate the original
    df_work = df.copy()

    # 1) Filter to rows where 'won' is 0 or 1 (drop NaNs)
    before = len(df_work)
    df_work = df_work[df_work["won"].isin([0, 1])]
    after = len(df_work)
    print(f"[FEAT] Filtered rows with valid 'won': {before} -> {after}")

    # 2) Keep only the three result markets
    if "market_type" in df_work.columns:
        valid_markets = ("home_win", "draw", "away_win")
        before = len(df_work)
        df_work = df_work[df_work["market_type"].isin(valid_markets)]
        after = len(df_work)
        print(f"[FEAT] Filtered to result markets (home/draw/away): {before} -> {after}")
    else:
        print("[FEAT] WARNING: 'market_type' column missing – cannot filter markets cleanly.")

    # Target vector
    y = df_work["won"]

    # 3) Candidate numeric feature columns (actual column names)
    numeric_candidates = [
        # Market / model info
        "model_probability",
        "actual_odds",
        "implied_prob_bookmaker",
        "edge",

        # Season strength + stats
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

        # game count summary
        "home_games_played_home",
        "home_games_played_away",
        "away_games_played_home",
        "away_games_played_away",
    ]

    # Build feature DataFrame
    X_parts = []

    # 3a) Add numeric columns that actually exist
    numeric_used = []
    for col in numeric_candidates:
        if col in df_work.columns:
            X_col = pd.to_numeric(df_work[col], errors="coerce")
            X_parts.append(X_col.rename(col))
            numeric_used.append(col)

    if numeric_used:
        print(f"[FEAT] Using numeric features ({len(numeric_used)}):")
        for col in numeric_used:
            print(f"  - {col}")
    else:
        print("[FEAT] WARNING: No numeric candidate columns found in dataset.")

    # 3b) Special handling for percentage columns – scale from 0–100 to 0–1
    pct_cols = [
        "home_scored_first_pct", "away_scored_first_pct", "scored_first_diff",
        "home_clean_sheet_pct", "away_clean_sheet_pct", "clean_sheet_diff",
        "home_possession_pct", "away_possession_pct", "possession_diff",
    ]
    for col in pct_cols:
        if col in df_work.columns:
            scaled = pd.to_numeric(df_work[col], errors="coerce") / 100.0
            # override if already present
            replaced = False
            for i, series in enumerate(X_parts):
                if series.name == col:
                    X_parts[i] = scaled.rename(col)
                    replaced = True
                    break
            if not replaced:
                X_parts.append(scaled.rename(col))

    # 4) Encode market_type as 3 binary flags
    if "market_type" in df_work.columns:
        mt = df_work["market_type"].fillna("")
        X_parts.append((mt == "home_win").astype(int).rename("is_home_win_market"))
        X_parts.append((mt == "draw").astype(int).rename("is_draw_market"))
        X_parts.append((mt == "away_win").astype(int).rename("is_away_win_market"))
        print("[FEAT] Added one-hot flags for market_type.")
    else:
        print("[FEAT] WARNING: 'market_type' missing – no market flags added.")

    # 5) Encode competition_predictability as one-hot
    if "competition_predictability" in df_work.columns:
        pred_dummies = pd.get_dummies(
            df_work["competition_predictability"].fillna("unknown"),
            prefix="pred",
            drop_first=False,
        )
        X_parts.append(pred_dummies)
        print("[FEAT] Added one-hot encoding for competition_predictability:")
        for col in pred_dummies.columns:
            print(f"  - {col}")
    else:
        print("[FEAT] WARNING: 'competition_predictability' missing – no predictability features added.")

    # 6) Date/time features from kickoff_date and kickoff_hour
    if "kickoff_date" in df_work.columns:
        ko_dt = pd.to_datetime(df_work["kickoff_date"], errors="coerce")
        X_parts.append(ko_dt.dt.year.rename("ko_year"))
        X_parts.append(ko_dt.dt.month.rename("ko_month"))
        X_parts.append(ko_dt.dt.weekday.rename("ko_weekday"))  # 0=Mon ... 6=Sun
        print("[FEAT] Added date features from kickoff_date (year, month, weekday).")
    else:
        print("[FEAT] WARNING: 'kickoff_date' missing – no date features added.")

    # kickoff_hour is like "19:45" – convert to hour as decimal
    if "kickoff_hour" in df_work.columns:
        ko_raw = df_work["kickoff_hour"].astype(str)

        def parse_time_to_hour_decimal(s: str):
            s = s.strip()
            if ":" in s:
                try:
                    hh, mm = s.split(":")[:2]
                    hh_i = int(hh)
                    mm_i = int(mm)
                    return hh_i + mm_i / 60.0
                except Exception:
                    return None
            else:
                try:
                    return float(s)
                except Exception:
                    return None

        ko_hour_decimal = ko_raw.apply(parse_time_to_hour_decimal)
        X_parts.append(pd.to_numeric(ko_hour_decimal, errors="coerce").rename("ko_hour"))
        print("[FEAT] Added kickoff_hour (decimal) feature.")
    else:
        print("[FEAT] WARNING: 'kickoff_hour' missing – no hour feature added.")

    # 7) Combine all feature parts into one DataFrame
    if not X_parts:
        print("[FEAT] ERROR: No features built – X_parts is empty.")
        return None, None

    X = pd.concat(X_parts, axis=1)

    # 8) Handle missing values: fill NaNs with column median (or 0 if needed)
    numeric_cols = X.select_dtypes(include=["float64", "int64", "bool"]).columns
    for col in numeric_cols:
        col_series = X[col].astype(float) if X[col].dtype == bool else X[col]
        if col_series.isna().any():
            median_val = col_series.median()
            if pd.isna(median_val):
                median_val = 0.0
            X[col] = col_series.fillna(median_val)

    # After filling NaNs, we should have all numeric values
    print(f"[FEAT] After filling NaNs, any nulls left? {X.isna().any().any()}")

    # Align target
    y_aligned = y.loc[X.index]

    print(f"[FEAT] Final feature matrix shape: {X.shape[0]} rows, {X.shape[1]} columns")

    print("\n[FEAT] Feature head (first 5 rows):")
    print(X.head())

    return X, y_aligned


# ------------------------------------------------------
# STEP 3 – TRAIN A SIMPLE MODEL
# ------------------------------------------------------
def train_logistic_model(X, y):
    """
    Train a basic Logistic Regression model on the features X and target y.

    - Splits into train/test.
    - Uses a Pipeline: StandardScaler -> LogisticRegression.
    - Prints accuracy, classification report, AUC (if possible).
    - Saves the fitted model to disk.
    """
    if X is None or y is None or len(X) == 0:
        print("[TRAIN] ERROR: X or y is empty – cannot train model.")
        return

    print("\n[TRAIN] Starting train/test split...")

    # Small dataset -> keep test_size modest but non-zero
    X_train, X_test, y_train, y_test = train_test_split(
        X,
        y,
        test_size=0.3,
        random_state=42,
        stratify=y,
    )

    print(f"[TRAIN] Train size: {X_train.shape[0]}, Test size: {X_test.shape[0]}")

    # Pipeline: scale then logistic regression
    pipe = Pipeline(
        steps=[
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(max_iter=1000)),
        ]
    )

    print("[TRAIN] Fitting LogisticRegression model...")
    pipe.fit(X_train, y_train)

    # Evaluate
    print("[TRAIN] Evaluating on test set...")
    y_pred = pipe.predict(X_test)
    try:
        y_proba = pipe.predict_proba(X_test)[:, 1]
    except Exception:
        y_proba = None

    print("\n[TRAIN] Classification report:")
    print(classification_report(y_test, y_pred, digits=3))

    if y_proba is not None and len(set(y_test)) > 1:
        try:
            auc = roc_auc_score(y_test, y_proba)
            print(f"[TRAIN] ROC AUC: {auc:.3f}")
        except Exception as e:
            print(f"[TRAIN] Could not compute AUC: {e}")
    else:
        print("[TRAIN] Not enough variation or proba to compute AUC.")

    # Save model
    joblib.dump(pipe, MODEL_PATH)
    print(f"[TRAIN] Model saved to: {MODEL_PATH}")


# ------------------------------------------------------
# MAIN – run all steps
# ------------------------------------------------------
if __name__ == "__main__":
    # Step 1: load data
    df = load_training_data()
    if df is None:
        print("[MAIN] Failed to load training data.")
    else:
        # Step 2: build features + target
        X, y = prepare_features(df)
        if X is None or y is None:
            print("[MAIN] Failed to build feature matrix.")
        else:
            print("\n[MAIN] Feature matrix (X) and target (y) are ready for modelling.")
            print(f"[MAIN] X shape: {X.shape}")
            print(f"[MAIN] y length: {len(y)}")

            # Step 3: train simple model
            train_logistic_model(X, y)
