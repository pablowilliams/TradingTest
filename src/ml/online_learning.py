"""Online learning - continuous model improvement from trade outcomes."""
import logging
import numpy as np
from typing import Optional

logger = logging.getLogger(__name__)


class OnlineLearner:
    """Incrementally update ML model from resolved trades."""

    def __init__(self, scorer, db):
        self.scorer = scorer
        self.db = db
        self._buffer_X = []
        self._buffer_y = []
        self.retrain_threshold = 50  # Retrain after N new outcomes

    async def record_outcome(self, trade: dict, won: bool, pnl: float):
        """Record a trade outcome for learning."""
        features = self.scorer.extract_features(trade.get("signals_snapshot", trade))
        outcome = 1.0 if won else 0.0

        self._buffer_X.append(features.flatten())
        self._buffer_y.append(outcome)

        # Update Bayesian learning in DB
        bot_id = trade.get("bot_id", "unknown")
        for key in self._extract_feature_keys(trade):
            await self.db.update_learning(bot_id, key, won)

        if len(self._buffer_y) >= self.retrain_threshold:
            await self._retrain()

    async def _retrain(self):
        """Retrain model with accumulated data."""
        if not self._buffer_X:
            return
        try:
            X = np.array(self._buffer_X)
            y = np.array(self._buffer_y)
            self.scorer.train(X, y, save=True)
            logger.info(f"Retrained model on {len(y)} samples")
            self._buffer_X.clear()
            self._buffer_y.clear()
        except Exception as e:
            logger.error(f"Retrain failed: {e}")

    def _extract_feature_keys(self, trade: dict) -> list:
        """Extract Bayesian feature bucket keys from a trade."""
        snap = trade.get("signals_snapshot", {})
        keys = []
        price = snap.get("yes_price", snap.get("pm_price", 0.5))
        if price < 0.40: keys.append("price_low")
        elif price < 0.60: keys.append("price_mid")
        else: keys.append("price_high")

        mom = snap.get("btc", snap.get("btc_momentum", 0))
        if mom > 0.5: keys.append("mom_up")
        elif mom < -0.5: keys.append("mom_down")
        else: keys.append("mom_flat")

        return keys
