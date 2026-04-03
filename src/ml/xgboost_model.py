"""XGBoost ML model for scoring and ranking trade opportunities."""
import json
import logging
import numpy as np
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MODEL_PATH = Path(__file__).parent.parent.parent / "models" / "strategy_model.json"


class OpportunityScorer:
    """Score trade opportunities using XGBoost."""

    def __init__(self):
        self.model = None

    def load(self, path: Optional[Path] = None):
        path = path or MODEL_PATH
        if path.exists():
            import xgboost as xgb
            self.model = xgb.XGBRegressor()
            self.model.load_model(str(path))
            logger.info("Loaded XGBoost model")

    def extract_features(self, opp: dict) -> np.ndarray:
        """Extract 12-feature vector from an opportunity."""
        return np.array([
            opp.get("pm_price", 0.5),
            opp.get("sb_prob", 0.5),
            opp.get("edge", 0),
            opp.get("ev", 0),
            abs(opp.get("edge", 0)),  # delta_difference
            opp.get("match_confidence", 0),
            np.log1p(opp.get("liquidity", 0)),
            opp.get("spread", 0.05),
            opp.get("bookmaker_count", 1),
            1 if opp.get("sport") in ("soccer", "nfl", "nba") else 0,  # market_type
            opp.get("btc_momentum", 0),
            opp.get("sentiment", 0),
        ]).reshape(1, -1)

    def score(self, opp: dict) -> float:
        """Score a single opportunity. Higher = better."""
        if self.model is None:
            return self._heuristic_score(opp)
        features = self.extract_features(opp)
        return float(self.model.predict(features)[0])

    def rank(self, opportunities: list) -> list:
        """Rank opportunities by score, best first."""
        scored = [(opp, self.score(opp)) for opp in opportunities]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [{"opportunity": opp, "score": s} for opp, s in scored]

    def train(self, X: np.ndarray, y: np.ndarray, save: bool = True):
        """Train on historical trade outcomes."""
        import xgboost as xgb
        self.model = xgb.XGBRegressor(
            n_estimators=100, max_depth=6, learning_rate=0.1,
            objective="reg:squarederror", subsample=0.8, colsample_bytree=0.8)
        self.model.fit(X, y)
        if save:
            MODEL_PATH.parent.mkdir(exist_ok=True)
            self.model.save_model(str(MODEL_PATH))
            logger.info("Model saved")

    def _heuristic_score(self, opp: dict) -> float:
        """Fallback heuristic when no model is trained."""
        score = 0.0
        score += opp.get("ev", 0) * 3
        score += opp.get("edge", 0) * 5
        score += min(opp.get("match_confidence", 0), 1) * 0.5
        score += min(np.log1p(opp.get("liquidity", 0)) / 10, 1) * 0.3
        score += opp.get("sentiment", 0) * 0.2
        return score
