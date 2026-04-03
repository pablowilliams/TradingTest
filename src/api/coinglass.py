"""Coinglass API - Funding rates, liquidations, open interest, long/short ratios."""
import logging
import time
import aiohttp
from typing import Optional

logger = logging.getLogger(__name__)

COINGLASS_BASE = "https://open-api.coinglass.com/public/v2"


class CoinglassClient:
    """Coinglass derivatives data for crypto edge detection."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session: Optional[aiohttp.ClientSession] = None
        self._cache: dict = {}
        self.cache_ttl = 60  # 1 minute cache

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            headers={"coinglassSecret": self.api_key,
                     "Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=15))
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
        try:
            async with self.session.get(f"{COINGLASS_BASE}{path}", params=params) as resp:
                resp.raise_for_status()
                result = await resp.json()
                self._cache[cache_key] = (result, time.time())
                return result
        except Exception as e:
            logger.error(f"Coinglass API error: {e}")
            return {}

    async def get_funding_rates(self, symbol: str = "BTC") -> dict:
        """Get current funding rates across exchanges."""
        data = await self._get("/funding", {"symbol": symbol})
        rates = data.get("data", [])
        if not rates:
            return {"avg_rate": 0, "max_rate": 0, "min_rate": 0, "sentiment": "neutral"}

        values = [float(r.get("rate", 0)) for r in rates if r.get("rate")]
        if not values:
            return {"avg_rate": 0, "max_rate": 0, "min_rate": 0, "sentiment": "neutral"}

        avg = sum(values) / len(values)
        return {
            "avg_rate": round(avg, 6),
            "max_rate": round(max(values), 6),
            "min_rate": round(min(values), 6),
            "num_exchanges": len(values),
            # High positive funding = overleveraged longs = bearish signal
            # High negative funding = overleveraged shorts = bullish signal
            "sentiment": "bearish" if avg > 0.01 else "bullish" if avg < -0.01 else "neutral",
            "signal": round(-avg * 100, 4)  # Contrarian: negative when longs overleveraged
        }

    async def get_liquidations(self, symbol: str = "BTC", time_type: int = 1) -> dict:
        """Get recent liquidation data. time_type: 1=1h, 2=4h, 3=12h, 4=24h."""
        data = await self._get("/liquidation_chart", {"symbol": symbol, "time_type": time_type})
        liq_data = data.get("data", [])
        if not liq_data:
            return {"long_liq": 0, "short_liq": 0, "ratio": 0, "signal": 0}

        # Sum up liquidations
        long_liq = sum(float(d.get("longVolUsd", 0)) for d in liq_data)
        short_liq = sum(float(d.get("shortVolUsd", 0)) for d in liq_data)
        total = long_liq + short_liq

        ratio = long_liq / short_liq if short_liq > 0 else 10
        # Heavy long liquidations = price dropping = potential bottom (bullish reversal)
        # Heavy short liquidations = price rising = potential top (bearish reversal)
        signal = 0.0
        if ratio > 3:  # Way more longs liquidated
            signal = 0.5  # Contrarian bullish (capitulation)
        elif ratio > 1.5:
            signal = 0.2
        elif ratio < 0.33:  # Way more shorts liquidated
            signal = -0.5  # Contrarian bearish (euphoria top)
        elif ratio < 0.66:
            signal = -0.2

        return {
            "long_liq_usd": round(long_liq, 2),
            "short_liq_usd": round(short_liq, 2),
            "total_usd": round(total, 2),
            "long_short_ratio": round(ratio, 2),
            "signal": signal
        }

    async def get_open_interest(self, symbol: str = "BTC") -> dict:
        """Get aggregated open interest across exchanges."""
        data = await self._get("/open_interest", {"symbol": symbol})
        oi_data = data.get("data", [])
        if not oi_data:
            return {"total_oi": 0, "change_1h": 0, "change_4h": 0, "signal": 0}

        total_oi = sum(float(d.get("openInterest", 0)) for d in oi_data)
        # OI changes in the response
        changes = data.get("data", [{}])
        change_1h = float(changes[0].get("h1OiChangePercent", 0)) if changes else 0
        change_4h = float(changes[0].get("h4OiChangePercent", 0)) if changes else 0

        # Rising OI + rising price = bullish trend confirmation
        # Rising OI + falling price = bearish trend confirmation
        # Falling OI = trend weakening
        signal = 0.0
        if change_1h > 5:
            signal = 0.3  # Rapidly increasing OI = strong conviction
        elif change_1h < -5:
            signal = -0.2  # Rapidly decreasing OI = weakening

        return {
            "total_oi_usd": round(total_oi, 2),
            "change_1h_pct": round(change_1h, 2),
            "change_4h_pct": round(change_4h, 2),
            "signal": signal
        }

    async def get_long_short_ratio(self, symbol: str = "BTC") -> dict:
        """Get global long/short ratio from top traders."""
        data = await self._get("/long_short", {"symbol": symbol})
        ls_data = data.get("data", [])
        if not ls_data:
            return {"ratio": 1.0, "long_pct": 50, "short_pct": 50, "signal": 0}

        # Average across exchanges
        ratios = [float(d.get("longRate", 50)) for d in ls_data]
        avg_long_pct = sum(ratios) / len(ratios) if ratios else 50

        # Extreme positioning = contrarian signal
        signal = 0.0
        if avg_long_pct > 65:
            signal = -0.4  # Too many longs = bearish contrarian
        elif avg_long_pct > 55:
            signal = -0.15
        elif avg_long_pct < 35:
            signal = 0.4  # Too many shorts = bullish contrarian
        elif avg_long_pct < 45:
            signal = 0.15

        return {
            "long_pct": round(avg_long_pct, 1),
            "short_pct": round(100 - avg_long_pct, 1),
            "ratio": round(avg_long_pct / (100 - avg_long_pct) if avg_long_pct < 100 else 10, 2),
            "signal": signal,
            "sentiment": "crowded_long" if avg_long_pct > 60 else "crowded_short" if avg_long_pct < 40 else "balanced"
        }

    async def get_full_snapshot(self, symbol: str = "BTC") -> dict:
        """Get all derivatives data in one call."""
        funding = await self.get_funding_rates(symbol)
        liquidations = await self.get_liquidations(symbol)
        oi = await self.get_open_interest(symbol)
        ls_ratio = await self.get_long_short_ratio(symbol)

        # Composite signal: weighted average of all derivatives signals
        composite = (
            funding.get("signal", 0) * 0.30 +
            liquidations.get("signal", 0) * 0.25 +
            oi.get("signal", 0) * 0.20 +
            ls_ratio.get("signal", 0) * 0.25
        )

        return {
            "funding": funding,
            "liquidations": liquidations,
            "open_interest": oi,
            "long_short_ratio": ls_ratio,
            "composite_signal": round(composite, 4),
            "timestamp": time.time()
        }
