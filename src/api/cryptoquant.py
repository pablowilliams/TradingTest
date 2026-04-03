"""CryptoQuant API - On-chain metrics, whale tracking, exchange flows."""
import logging
import time
import aiohttp
from typing import Optional

logger = logging.getLogger(__name__)

CRYPTOQUANT_BASE = "https://api.cryptoquant.com/v1"


class CryptoQuantClient:
    """On-chain analytics for crypto market intelligence."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session: Optional[aiohttp.ClientSession] = None
        self._cache: dict = {}
        self.cache_ttl = 300  # 5 min cache (on-chain data updates slowly)

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.api_key}"},
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
            async with self.session.get(f"{CRYPTOQUANT_BASE}{path}", params=params) as resp:
                resp.raise_for_status()
                result = await resp.json()
                self._cache[cache_key] = (result, time.time())
                return result
        except Exception as e:
            logger.error(f"CryptoQuant API error: {e}")
            return {}

    async def get_exchange_netflow(self, asset: str = "btc", window: str = "day") -> dict:
        """Get exchange net inflow/outflow. Inflow = selling pressure, outflow = accumulation."""
        data = await self._get(f"/btc/exchange-flows/netflow",
                               {"window": window, "limit": 1})
        results = data.get("result", {}).get("data", [])
        if not results:
            return {"netflow": 0, "signal": 0, "interpretation": "no_data"}

        netflow = float(results[-1].get("netflow_total", 0))

        # Large inflow to exchanges = people depositing to sell = bearish
        # Large outflow from exchanges = people withdrawing to hold = bullish
        signal = 0.0
        if netflow > 5000:  # >5000 BTC net inflow
            signal = -0.5  # Bearish: selling pressure
        elif netflow > 1000:
            signal = -0.2
        elif netflow < -5000:  # >5000 BTC net outflow
            signal = 0.5  # Bullish: accumulation
        elif netflow < -1000:
            signal = 0.2

        return {
            "netflow_btc": round(netflow, 2),
            "signal": signal,
            "interpretation": "selling_pressure" if netflow > 1000 else "accumulation" if netflow < -1000 else "neutral"
        }

    async def get_whale_transactions(self, asset: str = "btc") -> dict:
        """Track large whale transactions."""
        data = await self._get(f"/btc/network-data/transactions-count-large",
                               {"window": "day", "limit": 2})
        results = data.get("result", {}).get("data", [])
        if len(results) < 2:
            return {"whale_txns": 0, "change_pct": 0, "signal": 0}

        current = float(results[-1].get("transactions_count_large", 0))
        previous = float(results[-2].get("transactions_count_large", 1))
        change = ((current - previous) / previous * 100) if previous else 0

        # Spike in whale transactions often precedes big moves
        signal = 0.0
        if change > 50:  # 50%+ increase in whale activity
            signal = 0.3  # Heightened activity (direction unknown but volatility coming)
        elif change > 20:
            signal = 0.15

        return {
            "whale_txns_today": int(current),
            "whale_txns_yesterday": int(previous),
            "change_pct": round(change, 1),
            "signal": signal,
            "alert": change > 50
        }

    async def get_miner_outflow(self, asset: str = "btc") -> dict:
        """Track miner selling pressure."""
        data = await self._get(f"/btc/miner-flows/outflow",
                               {"window": "day", "limit": 2})
        results = data.get("result", {}).get("data", [])
        if len(results) < 2:
            return {"outflow": 0, "change_pct": 0, "signal": 0}

        current = float(results[-1].get("outflow_total", 0))
        previous = float(results[-2].get("outflow_total", 1))
        change = ((current - previous) / previous * 100) if previous else 0

        # Miners selling heavily = bearish pressure
        signal = 0.0
        if change > 100:  # Miners doubled their selling
            signal = -0.4
        elif change > 30:
            signal = -0.15
        elif change < -30:  # Miners holding back
            signal = 0.15

        return {
            "miner_outflow_btc": round(current, 2),
            "change_pct": round(change, 1),
            "signal": signal
        }

    async def get_stablecoin_exchange_reserve(self) -> dict:
        """Track stablecoin reserves on exchanges - buying power indicator."""
        data = await self._get("/stablecoin/exchange-flows/reserve",
                               {"window": "day", "limit": 2})
        results = data.get("result", {}).get("data", [])
        if len(results) < 2:
            return {"reserve": 0, "change_pct": 0, "signal": 0}

        current = float(results[-1].get("reserve_usd", 0))
        previous = float(results[-2].get("reserve_usd", 1))
        change = ((current - previous) / previous * 100) if previous else 0

        # Rising stablecoin reserves = dry powder ready to buy = bullish
        signal = 0.0
        if change > 5:
            signal = 0.4  # Significant inflow of buying power
        elif change > 1:
            signal = 0.15
        elif change < -5:
            signal = -0.3  # Stablecoins leaving exchanges = less buying power
        elif change < -1:
            signal = -0.1

        return {
            "reserve_usd": round(current, 2),
            "change_pct": round(change, 2),
            "signal": signal,
            "interpretation": "buying_power_rising" if change > 1 else "buying_power_falling" if change < -1 else "stable"
        }

    async def get_full_onchain(self) -> dict:
        """Get complete on-chain snapshot."""
        netflow = await self.get_exchange_netflow()
        whales = await self.get_whale_transactions()
        miners = await self.get_miner_outflow()
        stables = await self.get_stablecoin_exchange_reserve()

        composite = (
            netflow.get("signal", 0) * 0.35 +
            whales.get("signal", 0) * 0.20 +
            miners.get("signal", 0) * 0.20 +
            stables.get("signal", 0) * 0.25
        )

        return {
            "exchange_netflow": netflow,
            "whale_activity": whales,
            "miner_flow": miners,
            "stablecoin_reserves": stables,
            "composite_signal": round(composite, 4),
            "whale_alert": whales.get("alert", False),
            "timestamp": time.time()
        }
