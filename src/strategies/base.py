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

        # #18/#19 FIX: confidence_cap raised to 0.85 -- strategies output raw
        # confidence and the cap is a safety net, not an everyday limiter.
        confidence = min(abs(combined), self.params.get("confidence_cap", 0.85))

        # #21 FIX: support both YES and NO outcomes
        if combined > 0.1:
            return {"action": "buy", "outcome": "YES", "confidence": round(confidence, 4),
                    "reasoning": f"{self.strategy_name}: combined={combined:.3f}",
                    "signals_snapshot": {"mkt": round(mkt_signal, 3), "btc": round(btc_mom, 3),
                                         "pm": round(pm_mom, 3), "strat": round(strat_signal, 3),
                                         "learn": round(learn_bias, 3), "combined": round(combined, 3),
                                         "yes_price": yes_price}}
        elif combined < -0.1:
            return {"action": "buy", "outcome": "NO", "confidence": round(confidence, 4),
                    "reasoning": f"{self.strategy_name}: contrarian combined={combined:.3f}",
                    "signals_snapshot": {"mkt": round(mkt_signal, 3), "btc": round(btc_mom, 3),
                                         "pm": round(pm_mom, 3), "strat": round(strat_signal, 3),
                                         "learn": round(learn_bias, 3), "combined": round(combined, 3),
                                         "yes_price": yes_price}}
        return self._hold(f"No signal (combined={combined:.3f})")

    def make_exit_decision(self, position: dict, market: dict, signals: dict) -> dict:
        """Decide whether to sell/exit an open position.

        Args:
            position: dict with keys entry_price, outcome ("YES"/"NO"),
                      contracts, entry_time
            market: current market data
            signals: current signal data

        Returns:
            dict with action "sell" or "hold", plus reasoning
        """
        yes_price = self._get_yes_price(market)
        entry_price = float(position.get("entry_price", yes_price))
        outcome = position.get("outcome", "YES")
        time_remaining = float(market.get("seconds_remaining", 300))
        entry_time = float(position.get("entry_time", time.time()))
        hold_seconds = time.time() - entry_time

        current_value = yes_price if outcome == "YES" else (1.0 - yes_price)
        pnl_pct = (current_value - entry_price) / entry_price if entry_price > 0 else 0.0

        # Take profit: lock in gains above 12%
        if pnl_pct >= 0.12:
            return {"action": "sell", "reasoning": f"Take profit: {pnl_pct:.1%} gain",
                    "pnl_pct": round(pnl_pct, 4)}

        # Stop loss: cut losses beyond 13%
        stop_loss = self.params.get("stop_loss_pct", 0.13)
        if pnl_pct <= -stop_loss:
            return {"action": "sell", "reasoning": f"Stop loss: {pnl_pct:.1%} loss",
                    "pnl_pct": round(pnl_pct, 4)}

        # Time-based exit: if held > 10 min and market is about to close
        if time_remaining < 30 and hold_seconds > 600:
            return {"action": "sell",
                    "reasoning": f"Expiry exit: {time_remaining:.0f}s left, held {hold_seconds:.0f}s",
                    "pnl_pct": round(pnl_pct, 4)}

        # Signal reversal: re-evaluate current signals
        strat_signal = self.get_strategy_signal(market, signals)
        if outcome == "YES" and strat_signal < -0.3:
            return {"action": "sell",
                    "reasoning": f"Signal reversal: strat={strat_signal:.3f} against YES",
                    "pnl_pct": round(pnl_pct, 4)}
        if outcome == "NO" and strat_signal > 0.3:
            return {"action": "sell",
                    "reasoning": f"Signal reversal: strat={strat_signal:.3f} against NO",
                    "pnl_pct": round(pnl_pct, 4)}

        return {"action": "hold", "reasoning": f"Holding: pnl={pnl_pct:.1%}, strat={strat_signal:.3f}",
                "pnl_pct": round(pnl_pct, 4)}

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
        # #24 FIX: return 0 for very low confidence (<0.3) but allow bets
        # above 0.3 (not 0.5).  Quarter-Kelly still applies.
        if confidence <= 0.3:
            return 0.0
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

    # #76 FIX: expanded bucket methods to readable if/elif chains

    def _bucket_price(self, p):
        if p < 0.25:
            return "price_vlow"
        elif p < 0.40:
            return "price_low"
        elif p < 0.60:
            return "price_mid"
        elif p < 0.75:
            return "price_high"
        else:
            return "price_vhigh"

    def _bucket_momentum(self, m):
        if m < -2:
            return "mom_sdown"
        elif m < -0.5:
            return "mom_down"
        elif m < 0.5:
            return "mom_flat"
        elif m < 2:
            return "mom_up"
        else:
            return "mom_sup"

    def _bucket_time_of_day(self):
        h = time.gmtime().tm_hour
        if 6 <= h < 12:
            return "tod_morn"
        elif 12 <= h < 18:
            return "tod_aftn"
        elif 18 <= h < 24:
            return "tod_eve"
        else:
            return "tod_night"

    def _bucket_volume(self, v):
        if v < 1000:
            return "vol_low"
        elif v < 10000:
            return "vol_med"
        else:
            return "vol_high"

    def _bucket_time_remaining(self, s):
        if s > 180:
            return "tr_early"
        elif s > 60:
            return "tr_mid"
        else:
            return "tr_late"
