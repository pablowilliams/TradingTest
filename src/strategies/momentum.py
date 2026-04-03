"""Momentum strategy - follows BTC and market trends."""
from .base import BaseStrategy


class MomentumStrategy(BaseStrategy):
    def __init__(self, bot_id: str, params: dict = None, generation: int = 0):
        defaults = {"high_price_guard": 0.72, "low_price_guard": 0.35, "confidence_cap": 0.45,
                    "late_window_boost": 0.25, "learning_weight_min": 0.05,
                    "learning_weight_max": 0.30, "ema_weight": 0.4, "momentum_threshold": 0.5}
        super().__init__(bot_id, "momentum", {**defaults, **(params or {})}, generation)

    def get_strategy_signal(self, market: dict, signals: dict) -> float:
        btc_mom = signals.get("btc_momentum_5m", signals.get("btc_momentum", 0))
        pm_dir = signals.get("pm_direction", "unknown")
        ema_bull = signals.get("ema_bullish", False)

        signal = 0.0
        thresh = self.params.get("momentum_threshold", 0.5)

        if btc_mom > thresh: signal += 0.4
        elif btc_mom < -thresh: signal -= 0.4

        if pm_dir == "up": signal += 0.3
        elif pm_dir == "down": signal -= 0.3

        ew = self.params.get("ema_weight", 0.4)
        if ema_bull: signal += ew
        else: signal -= ew * 0.5

        return max(-1.0, min(1.0, signal))
