"""Position management + scale-out logic (from covering-queen-calculator)."""
import logging
import time

logger = logging.getLogger(__name__)


class PositionManager:
    """Manages position sizing, scale-outs, and risk limits."""

    def __init__(self, config: dict):
        self.max_position = config.get("max_position_size", 50.0)
        self.daily_loss_cap = config.get("daily_loss_cap", 100.0)
        self.stop_loss_pct = config.get("stop_loss_pct", 0.13)
        self.scale_out_method = config.get("scale_out_method", "proportional")
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self._day_start = time.time()

    def check_daily_limit(self) -> bool:
        """Check if daily loss cap has been hit."""
        if time.time() - self._day_start > 86400:
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self._day_start = time.time()
        return self.daily_pnl > -self.daily_loss_cap

    def calculate_position_size(self, confidence: float, balance: float) -> float:
        """Quarter-Kelly position sizing."""
        if not self.check_daily_limit():
            return 0.0
        kelly = max(0, (2 * confidence - 1) * 0.25)
        size = balance * kelly
        return max(1.0, min(size, self.max_position))

    def calculate_scale_out(self, entry_price: float, contracts: int,
                            stop_loss_points: float, value_per_point: float = 1.0) -> list:
        """Calculate scale-out targets (covering queen logic)."""
        targets = []
        for close_n in range(1, contracts):
            remaining = contracts - close_n
            if self.scale_out_method == "proportional":
                points = (remaining * stop_loss_points) / close_n
            else:  # full_recovery
                total_risk = contracts * stop_loss_points * value_per_point
                points = total_risk / (close_n * value_per_point)

            targets.append({
                "close_contracts": close_n,
                "remaining": remaining,
                "target_points": round(points, 4),
                "target_price": round(entry_price + points, 4),
                "risk_free_after": self.scale_out_method == "proportional"
            })
        return targets

    def should_stop_loss(self, entry_price: float, current_price: float) -> bool:
        """Check if stop loss should trigger."""
        if entry_price == 0:
            return False
        loss_pct = (entry_price - current_price) / entry_price
        return loss_pct >= self.stop_loss_pct

    def record_trade_result(self, pnl: float):
        """Track daily P&L."""
        self.daily_pnl += pnl
        self.daily_trades += 1

    def get_status(self) -> dict:
        return {
            "daily_pnl": round(self.daily_pnl, 2),
            "daily_trades": self.daily_trades,
            "daily_limit_ok": self.check_daily_limit(),
            "remaining_budget": round(self.daily_loss_cap + self.daily_pnl, 2)
        }
