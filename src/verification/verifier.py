"""Pre-trade verification system - validates every trade before execution."""
import logging
import time

logger = logging.getLogger(__name__)


class TradeVerifier:
    """Multi-check verification gate before any trade is placed."""

    def __init__(self, config: dict, db=None):
        # #20 FIX: lowered from 0.60 to 0.35 -- strategies now output
        # full-range confidence (cap raised to 0.85), so the old 0.60
        # floor blocked almost every trade.
        self.min_confidence = config.get("min_confidence", 0.35)
        self.min_signals_agree = config.get("min_signals_agree", 3)
        self.require_positive_ev = config.get("require_positive_ev", True)
        self.max_spread = config.get("max_spread", 0.05)
        self.min_liquidity = config.get("min_liquidity", 1000)
        self.cross_validate = config.get("cross_validate", True)
        self.db = db

    async def verify(self, decision: dict, market: dict, signals: dict,
                     orderflow: dict = None) -> dict:
        """Run all verification checks. Returns pass/fail with details."""
        checks = {}
        reasons = []

        # 1. Confidence check
        conf = decision.get("confidence", 0)
        checks["confidence_ok"] = conf >= self.min_confidence
        if not checks["confidence_ok"]:
            reasons.append(f"Low confidence: {conf:.2f} < {self.min_confidence}")

        # 2. Signal agreement check
        snap = decision.get("signals_snapshot", {})
        agree_count = sum(1 for k in ["mkt", "btc", "pm", "strat", "learn"]
                         if snap.get(k, 0) > 0)
        checks["signals_agree"] = agree_count >= self.min_signals_agree
        if not checks["signals_agree"]:
            reasons.append(f"Weak agreement: {agree_count}/{self.min_signals_agree} signals agree")

        # 3. Positive EV check
        ev = signals.get("ev", snap.get("ev", 0))
        checks["ev_positive"] = ev > 0 if self.require_positive_ev else True
        if not checks["ev_positive"]:
            reasons.append(f"Negative EV: {ev:.4f}")

        # 4. Spread check (use orderflow if passed, else check signals dict)
        spread = None
        if orderflow:
            spread = orderflow.get("spread")
        if spread is None:
            spread = signals.get("spread")
        if spread is not None:
            checks["spread_ok"] = spread <= self.max_spread
            if not checks["spread_ok"]:
                reasons.append(f"Wide spread: {spread:.4f} > {self.max_spread}")
        else:
            checks["spread_ok"] = True  # No spread data = skip this check

        # 5. Liquidity check
        liq = float(market.get("liquidity", 0))
        checks["liquidity_ok"] = liq >= self.min_liquidity
        if not checks["liquidity_ok"]:
            reasons.append(f"Low liquidity: ${liq:.0f} < ${self.min_liquidity}")

        # 6. Cross-validation: check if multiple data sources confirm
        if self.cross_validate:
            btc_confirms = snap.get("btc", 0) > 0
            pm_confirms = snap.get("pm", 0) > 0
            strat_confirms = snap.get("strat", 0) > 0
            checks["cross_validated"] = sum([btc_confirms, pm_confirms, strat_confirms]) >= 2
            if not checks["cross_validated"]:
                reasons.append("Insufficient cross-validation")

        # 7. Anti-tilt check: no more than 3 consecutive losses
        # #28 FIX: filter to only the current bot's recent trades, not all bots
        if self.db:
            bot_id = decision.get("bot_id", "")
            recent = await self.db.get_recent_trades(bot_id=bot_id, limit=3)
            consec_losses = sum(1 for t in recent if t.get("pnl", 0) < 0)
            checks["not_tilting"] = consec_losses < 3
            if not checks["not_tilting"]:
                reasons.append(f"Tilt guard: {consec_losses} consecutive losses for {bot_id}")

        passed = all(checks.values())
        result = {
            "passed": passed,
            "checks": checks,
            "reasons": reasons,
            "confidence": conf,
            "signals_agree": agree_count,
            "timestamp": time.time()
        }

        if self.db:
            await self.db.log_verification(
                market.get("id", ""), decision.get("bot_id", ""),
                passed, conf, agree_count,
                checks.get("ev_positive", False),
                checks.get("spread_ok", False),
                checks.get("liquidity_ok", False),
                "; ".join(reasons) if reasons else "All checks passed")

        return result
