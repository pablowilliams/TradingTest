"""Polygon.io API - Stock and crypto market data enrichment."""
import logging
import time
import aiohttp
from typing import Optional

logger = logging.getLogger(__name__)

POLYGON_BASE = "https://api.polygon.io"


class PolygonClient:
    """Polygon.io for enriched market data, technicals, and news."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session: Optional[aiohttp.ClientSession] = None
        self._cache: dict = {}
        self.cache_ttl = 120

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    async def _get(self, path: str, params: dict = None) -> dict:
        cache_key = f"{path}:{params}"
        if cache_key in self._cache:
            data, ts = self._cache[cache_key]
            if time.time() - ts < self.cache_ttl:
                return data
        params = params or {}
        params["apiKey"] = self.api_key
        try:
            async with self.session.get(f"{POLYGON_BASE}{path}", params=params) as resp:
                resp.raise_for_status()
                result = await resp.json()
                self._cache[cache_key] = (result, time.time())
                return result
        except Exception as e:
            logger.error(f"Polygon API error: {e}")
            return {}

    async def get_crypto_snapshot(self, symbol: str = "X:BTCUSD") -> dict:
        """Get real-time crypto snapshot with VWAP, volume, change."""
        data = await self._get(f"/v2/snapshot/locale/global/markets/crypto/tickers/{symbol}")
        ticker = data.get("ticker", {})
        day = ticker.get("day", {})
        prev = ticker.get("prevDay", {})
        min_data = ticker.get("min", {})

        current = float(day.get("c", min_data.get("c", 0)))
        prev_close = float(prev.get("c", 0))
        change_pct = ((current - prev_close) / prev_close * 100) if prev_close else 0

        return {
            "price": current,
            "vwap": float(day.get("vw", 0)),
            "volume": float(day.get("v", 0)),
            "change_pct": round(change_pct, 2),
            "high": float(day.get("h", 0)),
            "low": float(day.get("l", 0)),
            "prev_close": prev_close,
            # Price above VWAP = bullish, below = bearish
            "vwap_signal": 0.3 if current > float(day.get("vw", current)) else -0.3,
            "timestamp": time.time()
        }

    async def get_stock_snapshot(self, ticker: str) -> dict:
        """Get stock snapshot for AskLivermore cross-reference."""
        data = await self._get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}")
        t = data.get("ticker", {})
        day = t.get("day", {})
        prev = t.get("prevDay", {})

        current = float(day.get("c", t.get("lastTrade", {}).get("p", 0)))
        prev_close = float(prev.get("c", 0))
        change = ((current - prev_close) / prev_close * 100) if prev_close else 0

        return {
            "ticker": ticker,
            "price": current,
            "change_pct": round(change, 2),
            "volume": float(day.get("v", 0)),
            "vwap": float(day.get("vw", 0)),
            "high": float(day.get("h", 0)),
            "low": float(day.get("l", 0)),
        }

    async def get_crypto_aggregates(self, symbol: str = "X:BTCUSD",
                                     timespan: str = "minute", limit: int = 60) -> list:
        """Get historical bars for technical analysis."""
        from_ts = int((time.time() - 86400) * 1000)
        to_ts = int(time.time() * 1000)
        data = await self._get(
            f"/v2/aggs/ticker/{symbol}/range/1/{timespan}/{from_ts}/{to_ts}",
            {"limit": limit, "sort": "desc"})
        results = data.get("results", [])
        return [{"open": r["o"], "high": r["h"], "low": r["l"], "close": r["c"],
                 "volume": r["v"], "vwap": r.get("vw", 0), "timestamp": r["t"]}
                for r in results]

    async def get_market_news(self, ticker: str = None, limit: int = 10) -> list:
        """Get latest market news for sentiment overlay."""
        params = {"limit": limit, "order": "desc", "sort": "published_utc"}
        if ticker:
            params["ticker"] = ticker
        data = await self._get("/v2/reference/news", params)
        articles = data.get("results", [])
        return [{"title": a.get("title", ""),
                 "description": a.get("description", ""),
                 "published": a.get("published_utc", ""),
                 "tickers": a.get("tickers", []),
                 "sentiment": a.get("insights", [{}])[0].get("sentiment", "neutral")
                 if a.get("insights") else "neutral"}
                for a in articles]

    async def get_sma(self, symbol: str = "X:BTCUSD", window: int = 20,
                      timespan: str = "day") -> dict:
        """Get Simple Moving Average."""
        data = await self._get(f"/v1/indicators/sma/{symbol}",
                               {"timespan": timespan, "window": window, "limit": 1})
        results = data.get("results", {}).get("values", [])
        if results:
            return {"sma": float(results[0].get("value", 0)), "window": window}
        return {"sma": 0, "window": window}

    async def get_rsi(self, symbol: str = "X:BTCUSD", window: int = 14,
                      timespan: str = "day") -> dict:
        """Get RSI indicator."""
        data = await self._get(f"/v1/indicators/rsi/{symbol}",
                               {"timespan": timespan, "window": window, "limit": 1})
        results = data.get("results", {}).get("values", [])
        if results:
            rsi = float(results[0].get("value", 50))
            signal = 0.0
            if rsi > 70: signal = -0.5  # Overbought
            elif rsi > 60: signal = -0.2
            elif rsi < 30: signal = 0.5  # Oversold
            elif rsi < 40: signal = 0.2
            return {"rsi": round(rsi, 2), "signal": signal}
        return {"rsi": 50, "signal": 0}

    async def get_macd(self, symbol: str = "X:BTCUSD", timespan: str = "day") -> dict:
        """Get MACD indicator."""
        data = await self._get(f"/v1/indicators/macd/{symbol}",
                               {"timespan": timespan, "limit": 1})
        results = data.get("results", {}).get("values", [])
        if results:
            val = results[0]
            macd_val = float(val.get("value", 0))
            signal_line = float(val.get("signal", 0))
            histogram = float(val.get("histogram", 0))
            signal = 0.3 if histogram > 0 else -0.3
            return {"macd": round(macd_val, 4), "signal_line": round(signal_line, 4),
                    "histogram": round(histogram, 4), "signal": signal}
        return {"macd": 0, "signal_line": 0, "histogram": 0, "signal": 0}

    async def get_full_technicals(self, symbol: str = "X:BTCUSD") -> dict:
        """Get all technical indicators in one call."""
        snapshot = await self.get_crypto_snapshot(symbol)
        rsi = await self.get_rsi(symbol)
        macd = await self.get_macd(symbol)
        sma20 = await self.get_sma(symbol, 20)
        sma50 = await self.get_sma(symbol, 50)

        # Composite technical signal
        composite = (
            snapshot.get("vwap_signal", 0) * 0.20 +
            rsi.get("signal", 0) * 0.30 +
            macd.get("signal", 0) * 0.25 +
            (0.25 if snapshot["price"] > sma20["sma"] > 0 else -0.25) * 0.25
        )

        return {
            "snapshot": snapshot,
            "rsi": rsi,
            "macd": macd,
            "sma_20": sma20,
            "sma_50": sma50,
            "composite_signal": round(composite, 4),
            "trend": "bullish" if snapshot["price"] > sma50["sma"] > 0 else "bearish"
        }
