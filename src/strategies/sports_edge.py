"""Sports edge strategy - exploits sportsbook vs Polymarket price gaps."""
from .base import BaseStrategy


class SportsEdgeStrategy(BaseStrategy):
    def __init__(self, bot_id: str, params: dict = None, generation: int = 0):
        defaults = {"high_price_guard": 0.72, "low_price_guard": 0.30, "confidence_cap": 0.45,
                    "late_window_boost": 0.25, "learning_weight_min": 0.05,
                    "learning_weight_max": 0.30, "min_edge": 0.02, "consensus_boost": 0.1}
        super().__init__(bot_id, "sports_edge", {**defaults, **(params or {})}, generation)

    def get_strategy_signal(self, market: dict, signals: dict) -> float:
        mt = market.get("market_type", "")
        sports = {"sport", "nfl", "nba", "soccer", "mlb", "nhl", "mma"}
        if not any(s in mt.lower() for s in sports):
            return 0.0

        edge = signals.get("sportsbook_edge", 0)
        if edge < self.params.get("min_edge", 0.02):
            return 0.0

        signal = min(edge * 10, 1.0)
        if signals.get("bookmaker_count", 0) >= 3:
            signal = min(signal + self.params.get("consensus_boost", 0.1), 1.0)
        return signal
