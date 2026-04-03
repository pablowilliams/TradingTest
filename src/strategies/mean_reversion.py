"""Mean reversion strategy - bets against recent extremes."""
from .base import BaseStrategy


class MeanReversionStrategy(BaseStrategy):
    def __init__(self, bot_id: str, params: dict = None, generation: int = 0):
        defaults = {"high_price_guard": 0.72, "low_price_guard": 0.35, "confidence_cap": 0.45,
                    "late_window_boost": 0.25, "learning_weight_min": 0.05,
                    "learning_weight_max": 0.30, "extreme_threshold": 5.0,
                    "rsi_overbought": 70, "rsi_oversold": 30}
        super().__init__(bot_id, "mean_reversion", {**defaults, **(params or {})}, generation)

    def get_strategy_signal(self, market: dict, signals: dict) -> float:
        change_5m = signals.get("pm_change_5m", signals.get("change_5m", 0))
        change_15m = signals.get("pm_change_15m", signals.get("change_15m", 0))
        thresh = self.params.get("extreme_threshold", 5.0)

        signal = 0.0
        if change_5m > thresh: signal -= 0.7
        elif change_5m < -thresh: signal += 0.7
        elif change_5m > thresh * 0.6: signal -= 0.3
        elif change_5m < -thresh * 0.6: signal += 0.3

        if abs(change_15m) > 0:
            rsi = max(0, min(100, 50 + change_15m * 5))
            if rsi > self.params.get("rsi_overbought", 70): signal -= 0.4
            elif rsi < self.params.get("rsi_oversold", 30): signal += 0.4

        return max(-1.0, min(1.0, signal))
