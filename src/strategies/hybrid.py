"""Hybrid ensemble strategy - requires multi-strategy agreement."""
from .base import BaseStrategy
from .momentum import MomentumStrategy
from .mean_reversion import MeanReversionStrategy
from .sports_edge import SportsEdgeStrategy
from .sniper import SniperStrategy


class HybridStrategy(BaseStrategy):
    def __init__(self, bot_id: str, params: dict = None, generation: int = 0):
        defaults = {"high_price_guard": 0.72, "low_price_guard": 0.35, "confidence_cap": 0.45,
                    "late_window_boost": 0.25, "learning_weight_min": 0.05,
                    "learning_weight_max": 0.30, "min_agreement": 3}
        super().__init__(bot_id, "hybrid", {**defaults, **(params or {})}, generation)
        self.subs = [MomentumStrategy(f"{bot_id}_m"), MeanReversionStrategy(f"{bot_id}_mr"),
                     SportsEdgeStrategy(f"{bot_id}_se"), SniperStrategy(f"{bot_id}_sn")]
        self._w = [0.3, 0.2, 0.3, 0.2]

    def get_strategy_signal(self, market: dict, signals: dict) -> float:
        sigs = [s.get_strategy_signal(market, signals) for s in self.subs]
        bullish = sum(1 for s in sigs if s > 0.1)
        min_agree = self.params.get("min_agreement", 3)

        if bullish >= min_agree:
            ws = sum(s * w for s, w in zip(sigs, self._w) if s > 0.1)
            wt = sum(w for s, w in zip(sigs, self._w) if s > 0.1)
            return ws / wt if wt > 0 else 0.0
        return 0.0
