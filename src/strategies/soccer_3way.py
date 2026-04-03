"""Soccer 3-way strategy - buys undervalued favorite + draw combos."""
import json
import logging
from .base import BaseStrategy

logger = logging.getLogger(__name__)


class Soccer3WayStrategy(BaseStrategy):
    def __init__(self, bot_id: str, params: dict = None, generation: int = 0):
        defaults = {"buy_threshold": 0.95, "sell_profit_threshold": 0.99,
                    "risk_reduce_threshold": 0.13, "confidence_cap": 0.45,
                    "high_price_guard": 0.80, "low_price_guard": 0.20,
                    "learning_weight_min": 0.05, "learning_weight_max": 0.30}
        super().__init__(bot_id, "soccer_3way", {**defaults, **(params or {})}, generation)

    def get_strategy_signal(self, market: dict, signals: dict) -> float:
        return 0.0

    def make_decision(self, market: dict, signals: dict) -> dict:
        outcomes = market.get("outcomes", [])
        prices = market.get("outcomePrices", [])
        if isinstance(outcomes, str):
            try: outcomes = json.loads(outcomes)
            except (json.JSONDecodeError, TypeError): return self._hold("Cannot parse outcomes")
        if isinstance(prices, str):
            try: prices = json.loads(prices)
            except (json.JSONDecodeError, TypeError): return self._hold("Cannot parse prices")

        if len(outcomes) != 3 or len(prices) < 3:
            return self._hold("Not a 3-outcome market")

        pf = [float(p) for p in prices]
        draw_idx = next((i for i, n in enumerate(outcomes) if n.lower() in ("draw", "tie", "empate")), None)
        if draw_idx is None:
            return self._hold("No draw outcome")

        teams = [(i, pf[i]) for i in range(3) if i != draw_idx]
        teams.sort(key=lambda x: x[1], reverse=True)
        fav_idx, fav_price = teams[0]
        dog_idx, dog_price = teams[1]
        draw_price = pf[draw_idx]

        holdings = signals.get("holdings", {})
        entry_combined = signals.get("entry_combined_price", 0)
        combined_ask = fav_price + draw_price

        if not holdings and combined_ask < self.params["buy_threshold"]:
            disc = self.params["buy_threshold"] - combined_ask
            conf = min(disc * 5, self.params.get("confidence_cap", 0.45))
            return {"action": "buy_combo", "outcome": f"{outcomes[fav_idx]}+{outcomes[draw_idx]}",
                    "confidence": round(conf, 4),
                    "reasoning": f"3-way: {outcomes[fav_idx]}({fav_price:.2f})+Draw({draw_price:.2f})={combined_ask:.3f}",
                    "signals_snapshot": {"combined_ask": combined_ask}}

        if holdings and entry_combined > 0:
            current = fav_price + draw_price
            if current >= self.params["sell_profit_threshold"]:
                return {"action": "sell_combo", "outcome": f"{outcomes[fav_idx]}+{outcomes[draw_idx]}",
                        "confidence": 0.9, "reasoning": f"Profit exit: {current:.3f}",
                        "signals_snapshot": {"combined": current}}

            drop = (entry_combined - current) / entry_combined
            if drop >= self.params["risk_reduce_threshold"]:
                return {"action": "flip_team", "outcome": f"flip to {outcomes[dog_idx]}+Draw",
                        "confidence": 0.7, "reasoning": f"Risk exit: dropped {drop:.1%}",
                        "signals_snapshot": {"drop_pct": drop}}

        return self._hold("No 3-way opportunity")
