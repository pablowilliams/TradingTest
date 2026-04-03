"""Orderflow analysis from Polymarket CLOB orderbook."""
import logging

logger = logging.getLogger(__name__)


class OrderflowSignal:
    def __init__(self, clob_client):
        self.clob = clob_client

    async def analyze(self, token_id: str) -> dict:
        try:
            book = await self.clob.get_orderbook(token_id)
            bids = book.get("bids", [])
            asks = book.get("asks", [])

            total_bid = sum(float(b.get("size", 0)) for b in bids)
            total_ask = sum(float(a.get("size", 0)) for a in asks)
            total = total_bid + total_ask
            imbalance = (total_bid - total_ask) / total if total > 0 else 0

            best_bid = float(bids[0]["price"]) if bids else 0
            best_ask = float(asks[0]["price"]) if asks else 1
            spread = best_ask - best_bid

            trades = await self.clob.get_trades(token_id=token_id)
            recent = trades[:20] if trades else []
            buy_vol = sum(float(t.get("size", 0)) for t in recent if t.get("side", "").lower() == "buy")
            sell_vol = sum(float(t.get("size", 0)) for t in recent if t.get("side", "").lower() == "sell")
            tv = buy_vol + sell_vol
            vol_pressure = (buy_vol - sell_vol) / tv if tv > 0 else 0

            return {"imbalance": round(imbalance, 4), "spread": round(spread, 4),
                    "volume_pressure": round(vol_pressure, 4),
                    "total_bid_size": round(total_bid, 2), "total_ask_size": round(total_ask, 2),
                    "best_bid": best_bid, "best_ask": best_ask}
        except Exception as e:
            logger.error(f"Orderflow error: {e}")
            return {"imbalance": 0, "spread": 1, "volume_pressure": 0,
                    "total_bid_size": 0, "total_ask_size": 0, "best_bid": 0, "best_ask": 1}
