"""Sniper strategy - pure rule-based, high selectivity."""
from .base import BaseStrategy


class SniperStrategy(BaseStrategy):
    def __init__(self, bot_id: str, params: dict = None, generation: int = 0):
        defaults = {"high_price_guard": 0.78, "low_price_guard": 0.35, "confidence_cap": 0.45,
                    "late_window_boost": 0.25, "learning_weight_min": 0.0,
                    "learning_weight_max": 0.0, "cheap_zone_low": 0.40, "cheap_zone_high": 0.48,
                    "strong_zone_low": 0.58, "strong_zone_high": 0.78}
        super().__init__(bot_id, "sniper", {**defaults, **(params or {})}, generation)

    def get_strategy_signal(self, market: dict, signals: dict) -> float:
        yes_price = self._get_yes_price(market)
        btc_mom = signals.get("btc_momentum", signals.get("btc_momentum_5m", 0))
        edge = signals.get("sportsbook_edge", 0)
        pm_dir = signals.get("pm_direction", "unknown")
        mt = market.get("market_type", "")

        in_cheap = self.params["cheap_zone_low"] <= yes_price <= self.params["cheap_zone_high"]
        in_strong = self.params["strong_zone_low"] <= yes_price <= self.params["strong_zone_high"]

        if not (in_cheap or in_strong):
            return 0.0

        if "crypto" in mt or "btc" in mt.lower():
            confirmed = btc_mom > 0.3 and pm_dir in ("up", "consolidating")
        elif "sport" in mt.lower():
            confirmed = edge > 0.01
        else:
            confirmed = btc_mom > 0 or edge > 0 or pm_dir == "up"

        if not confirmed:
            return 0.0
        return 0.7 if in_cheap else 0.9
