"""Base strategy with signal combination, Bayesian learning, and bet sizing."""
import json
import math
import random
import time
from abc import ABC, abstractmethod


class BaseStrategy(ABC):
    # Default signal weights (overridden by config.signals if provided)
    DEFAULT_WEIGHTS = {
        "market_price_weight": 0.50,
        "btc_momentum_weight": 0.15,
        "pm_momentum_weight": 0.10,
        "strategy_weight": 0.15,
        "learning_weight_min": 0.05,
        "learning_weight_max": 0.30,
    }

    def __init__(self, bot_id: str, strategy_name: str, params: dict = None,
                 generation: int = 0, signal_weights: dict = None):
        self.bot_id = bot_id
        self.strategy_name = strategy_name
        self.params = params or {}
        self.generation = generation
        self.learning_data: dict = {}
        self.total_observations: int = 0
        # Merge config weights over defaults
        self.weights = {**self.DEFAULT_WEIGHTS, **(signal_weights or {})}

    def make_decision(self, market: dict, signals: dict) -> dict:
        yes_price = self._get_yes_price(market)
        time_remaining = float(market.get("seconds_remaining", 300))

        # Hard filters
        if yes_price > self.params.get("high_price_guard", 0.72):
            return self._hold("Price too high (>0.72)")
        if yes_price < self.params.get("low_price_guard", 0.35):
            return self._hold("Price too low (<0.35)")

        # Signal components
        mkt_signal = self._market_price_signal(yes_price)
        btc_mom = signals.get("btc_momentum", 0)
        pm_mom = signals.get("pm_momentum", 0)
        strat_signal = self.get_strategy_signal(market, signals)
        learn_bias = self.get_learning_bias(market, signals)

        # Read weights from config (not hardcoded)
        w_mkt = self.weights["market_price_weight"]
        w_btc = self.weights["btc_momentum_weight"]
        w_pm = self.weights["pm_momentum_weight"]
        w_strat = self.weights["strategy_weight"]

        # Dynamic learning weight scales with observations
        min_lw = self.weights["learning_weight_min"]
        max_lw = self.weights["learning_weight_max"]
        lw = min(min_lw + (self.total_observations / 500) * (max_lw - min_lw), max_lw)

        # Normalize base weights to fill remaining after learning
        base_sum = w_mkt + w_btc + w_pm + w_strat
        scale = (1.0 - lw) / base_sum if base_sum > 0 else 1.0

        combined = (mkt_signal * w_mkt * scale + btc_mom * w_btc * scale +
                    pm_mom * w_pm * scale + strat_signal * w_strat * scale +
                    learn_bias * lw)

        if time_remaining < 60:
            combined *= 1.0 + self.params.get("late_window_boost", 0.25)

        confidence = min(abs(combined), self.params.get("confidence_cap", 0.45))

        if combined > 0.1:
            return {"action": "buy", "outcome": "YES", "confidence": round(confidence, 4),
                    "reasoning": f"{self.strategy_name}: combined={combined:.3f}",
                    "signals_snapshot": {"mkt": round(mkt_signal, 3), "btc": round(btc_mom, 3),
                                         "pm": round(pm_mom, 3), "strat": round(strat_signal, 3),
                                         "learn": round(learn_bias, 3), "combined": round(combined, 3),
                                         "yes_price": yes_price}}
        return self._hold(f"No signal (combined={combined:.3f})")

    @abstractmethod
    def get_strategy_signal(self, market: dict, signals: dict) -> float:
        pass

    def get_learning_bias(self, market: dict, signals: dict) -> float:
        if not self.learning_data:
            return 0.0
        buckets = [
            self._bucket_price(self._get_yes_price(market)),
            self._bucket_momentum(signals.get("btc_momentum", 0)),
            self._bucket_time_of_day(),
            self._bucket_volume(signals.get("volume", 0)),
            self._bucket_time_remaining(float(market.get("seconds_remaining", 300)))
        ]
        total_bias, total_weight = 0.0, 0.0
        for key in buckets:
            if key in self.learning_data:
                d = self.learning_data[key]
                w = math.sqrt(d.get("obs", 0))
                total_bias += (d.get("wr", 0.5) - 0.5) * 2 * w
                total_weight += w
        return max(-1.0, min(1.0, total_bias / total_weight)) if total_weight else 0.0

    def bet_size(self, confidence: float, balance: float, max_pos: float = 50.0) -> float:
        if confidence <= 0.5:
            return 0.0  # Sub-breakeven confidence = no bet
        kelly = (2 * confidence - 1) * 0.25  # Quarter-Kelly
        size = balance * kelly
        if size < 1.0:
            return 0.0  # Too small to be worth it
        return min(size, max_pos)

    def mutate(self, mutation_range: float = 0.15) -> dict:
        new = dict(self.params)
        mutable = [k for k, v in new.items() if isinstance(v, (int, float))]
        if not mutable:
            return new
        for key in random.sample(mutable, min(random.randint(2, 3), len(mutable))):
            new[key] = new[key] * (1 + random.uniform(-mutation_range, mutation_range))
        return new

    def _hold(self, reason: str) -> dict:
        return {"action": "hold", "outcome": None, "confidence": 0,
                "reasoning": reason, "signals_snapshot": {}}

    def _get_yes_price(self, market: dict) -> float:
        p = market.get("yes_price")
        if p is not None:
            return float(p)
        prices = market.get("outcomePrices", [0.5])
        if isinstance(prices, str):
            try: prices = json.loads(prices)
            except (json.JSONDecodeError, TypeError): return 0.5
        return float(prices[0]) if prices else 0.5

    def _market_price_signal(self, p: float) -> float:
        if 0.55 < p <= 0.65: return 0.3
        if 0.65 < p <= 0.72: return 0.5
        if 0.40 <= p < 0.45: return -0.2
        if 0.35 <= p < 0.40: return -0.4
        return 0.0

    def _bucket_price(self, p): return ("price_vlow" if p < 0.25 else "price_low" if p < 0.40 else "price_mid" if p < 0.60 else "price_high" if p < 0.75 else "price_vhigh")
    def _bucket_momentum(self, m): return ("mom_sdown" if m < -2 else "mom_down" if m < -0.5 else "mom_flat" if m < 0.5 else "mom_up" if m < 2 else "mom_sup")
    def _bucket_time_of_day(self):
        h = time.gmtime().tm_hour
        return "tod_morn" if 6 <= h < 12 else "tod_aftn" if 12 <= h < 18 else "tod_eve" if 18 <= h < 24 else "tod_night"
    def _bucket_volume(self, v): return "vol_low" if v < 1000 else "vol_med" if v < 10000 else "vol_high"
    def _bucket_time_remaining(self, s): return "tr_early" if s > 180 else "tr_mid" if s > 60 else "tr_late"
