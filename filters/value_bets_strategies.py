# filters/value_bets_strategies.py

VALUE_BET_STRATEGIES = [
    {
        "name": "Home Win V1 (League Only)",
        "markets": ["home_win_probability"],
        "probability": {"min": 50, "max": 100},
        "odds": {"min": 2.5, "max": None},
        "value": {"min": 40, "max": None},
        "predictability": ["high"],

        # League games = not cup AND not friendly (derived via matcher)
        "is_league": True,
        "is_cup": False,
        "is_friendly": False,

        "progress": {"min": 0, "max": 100},
        "bookmakers": ["Bet365", "WilliamHill"],
        "opening_guard": True
    },

    {
        "name": "Home Win V1 (Cups + Friendlies)",
        "markets": ["home_win_probability"],
        "probability": {"min": 50, "max": 100},
        "odds": {"min": 2.5, "max": None},
        "value": {"min": 40, "max": None},
        "predictability": ["high"],

        # Cups + Friendlies only (no leagues)
        "is_league": False,   # exclude leagues
        "is_cup": None,       # allow cups (don't require)
        "is_friendly": None,  # allow friendlies (don't require)

        "progress": {"min": 0, "max": 100},
        "bookmakers": ["Bet365", "WilliamHill"],
        "opening_guard": True
    },
]
