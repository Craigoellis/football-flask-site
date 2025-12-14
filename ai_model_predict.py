import os
import sys
import numpy as np
import pandas as pd
import joblib
from datetime import datetime

# ---------------------------------------------------------
# CONFIG
# ---------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
MODEL_DIR = os.path.join(BASE_DIR, "models")
MODEL_PATH = os.path.join(MODEL_DIR, "ai_bets_logreg.pkl")


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

    Returns:
        features: DataFrame with shape (n_rows, 46) – ready for model.predict_proba
    """
    df = df_raw.copy()

    # --- Keep only result markets, same as training ---
    df = df[df["market_type"].isin(["home_win", "draw", "away_win"])].copy()
    df.reset_index(drop=True, inplace=True)

    if df.empty:
        print("[PRED] No result-market rows found (home/draw/away).")
        return pd.DataFrame()

    # --- Ensure numeric columns exist and coerce to numeric ---
    for col in NUMERIC_COLS:
        if col not in df.columns:
            df[col] = np.nan

    features = df[NUMERIC_COLS].apply(pd.to_numeric, errors="coerce")

    # --- Market type one-hot flags (same as training) ---
    features["is_home_win_market"] = (df["market_type"] == "home_win").astype(float)
    features["is_draw_market"] = (df["market_type"] == "draw").astype(float)
    features["is_away_win_market"] = (df["market_type"] == "away_win").astype(float)

    # --- Competition predictability one-hot (training ended up with pred_unknown) ---
    pred_series = (
        df.get("competition_predictability")
        .fillna("unknown")
        .astype(str)
        .str.lower()
    )
    pred_dummies = pd.get_dummies(pred_series, prefix="pred")

    # Training run only had pred_unknown, so we must guarantee that column exists
    if "pred_unknown" not in pred_dummies.columns:
        pred_dummies["pred_unknown"] = 0

    # Only keep pred_unknown to match training model's features
    features["pred_unknown"] = pred_dummies["pred_unknown"].astype(float)

    # --- Date features from kickoff_date (year, month, weekday) ---
    dt = pd.to_datetime(df.get("kickoff_date"), errors="coerce")
    features["ko_year"] = dt.dt.year.astype(float)
    features["ko_month"] = dt.dt.month.astype(float)
    # Monday=0, Sunday=6 in pandas
    features["ko_weekday"] = dt.dt.weekday.astype(float)

    # --- Kickoff hour as decimal ---
    ko_hour_dec = df.get("kickoff_hour", "").apply(_parse_ko_hour_to_decimal)
    features["ko_hour"] = ko_hour_dec.astype(float)

    # --- Fill any NaNs with column means (same strategy as training) ---
    features = features.astype(float)
    features = features.fillna(features.mean())

    print(f"[PRED] Built feature matrix for prediction: {features.shape[0]} rows, {features.shape[1]} columns")
    return features


# ---------------------------------------------------------
# MAIN PREDICTION LOGIC
# ---------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python ai_model_predict.py <input_ai_bets_csv>")
        print("Example:")
        print("  python ai_model_predict.py data\\2025-12-10-ai-bets.csv")
        return

    input_csv_path = sys.argv[1]
    if not os.path.isabs(input_csv_path):
        input_csv_path = os.path.join(BASE_DIR, input_csv_path)

    if not os.path.exists(input_csv_path):
        print(f"[PRED] Input CSV not found: {input_csv_path}")
        return

    # Load model
    if not os.path.exists(MODEL_PATH):
        print(f"[PRED] Model file not found: {MODEL_PATH}")
        print("       Run ai_model_training.py first to train and save the model.")
        return

    print(f"[PRED] Loading model from: {MODEL_PATH}")
    model = joblib.load(MODEL_PATH)

    # Load CSV
    print(f"[PRED] Loading AI bets CSV from: {input_csv_path}")
    df = pd.read_csv(input_csv_path)

    print(f"[PRED] Raw input: {len(df)} rows")

    # Build feature matrix
    X = build_features_for_prediction(df)
    if X.empty:
        print("[PRED] No usable rows for prediction after filtering/feature building.")
        return

    # Predict win probabilities (probability that 'won' == 1)
    print("[PRED] Running model.predict_proba...")
    proba = model.predict_proba(X.values)[:, 1]  # column 1 = P(won=1)

    # Attach predictions back onto the rows we actually used
    df_result = df[df["market_type"].isin(["home_win", "draw", "away_win"])].copy()
    df_result.reset_index(drop=True, inplace=True)

    df_result["ai_win_prob"] = proba  # 0–1
    df_result["ai_win_prob_pct"] = (proba * 100).round(2)  # percent

    # Suggest output filename: add -scored before extension
    base_name = os.path.basename(input_csv_path)
    name, ext = os.path.splitext(base_name)
    scored_name = f"{name}-scored{ext}"
    output_csv_path = os.path.join(os.path.dirname(input_csv_path), scored_name)

    df_result.to_csv(output_csv_path, index=False, encoding="utf-8")

    print(f"[PRED] Wrote scored file: {output_csv_path}")
    print("[PRED] Sample predictions (first 5 rows):")
    print(df_result[[
        "fixture_id",
        "fixture_name",
        "market_type",
        "model_probability",
        "actual_odds",
        "edge",
        "ai_win_prob_pct",
    ]].head())


if __name__ == "__main__":
    main()
