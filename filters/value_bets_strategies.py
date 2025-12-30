# filters/value_bets_strategies.py

VALUE_BET_STRATEGIES = [
    # --- Example strategies (edit/add as you like) ---
    {
        "name": "Home Win V1",
        "markets": ["home_win_probability"],
        "probability": {"min": 50, "max": 100},
        "odds": {"min": 2.5, "max": None},
        "value": {"min": 40, "max": None},
        "predictability": ["high"],  # ["high","good","medium","poor"]
        # Cup/friendly control: True / False / None (None = don't care)
        "is_cup": False,
        "is_friendly": False,
        # Season progress 0-100 (None = don't care)
        "progress": {"min": 0, "max": 100},
        # Bookmakers are Bet365, WilliamHill, Betfair Exchange, Pinnacle, Kambi Group, 1xBet
        "bookmakers": ["Bet365", "WilliamHill"],
        "opening_guard": True
    },
]
