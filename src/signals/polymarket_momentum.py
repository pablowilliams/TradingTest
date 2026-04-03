"""In-market Polymarket YES price momentum."""
import logging
import time

logger = logging.getLogger(__name__)


class PolymarketMomentum:
    def __init__(self, clob_client):
        self.clob = clob_client

    async def get_momentum(self, token_id: str) -> dict:
        try:
            history = await self.clob.get_price_history(
                token_id, fidelity=1, start_ts=int(time.time()) - 900)

            if not history or len(history) < 2:
                return {"direction": "unknown", "strength": 0.0,
                        "change_5m": 0.0, "change_15m": 0.0, "current_price": 0.0}

            prices = []
            for entry in history:
                if isinstance(entry, dict):
                    prices.append(float(entry.get("p", entry.get("price", 0))))
                elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    prices.append(float(entry[1]))
                else:
                    try: prices.append(float(entry))
                    except (ValueError, TypeError): continue

            if not prices:
                return {"direction": "unknown", "strength": 0.0,
                        "change_5m": 0.0, "change_15m": 0.0, "current_price": 0.0}

            current = prices[-1]
            idx_5m = max(0, len(prices) - 5)
            change_5m = (current - prices[idx_5m]) / prices[idx_5m] * 100 if prices[idx_5m] > 0 else 0
            change_15m = (current - prices[0]) / prices[0] * 100 if prices[0] > 0 else 0

            if change_5m > 0.5:
                direction = "up"
            elif change_5m < -0.5:
                direction = "down"
            else:
                direction = "consolidating"

            return {"direction": direction, "strength": round(min(abs(change_5m) / 5.0, 1.0), 4),
                    "change_5m": round(change_5m, 4), "change_15m": round(change_15m, 4),
                    "current_price": current}
        except Exception as e:
            logger.error(f"Momentum error: {e}")
            return {"direction": "unknown", "strength": 0.0,
                    "change_5m": 0.0, "change_15m": 0.0, "current_price": 0.0}
