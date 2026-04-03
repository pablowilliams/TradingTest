"""Simmer paper trading client.

NOTE: The SIMMER_BASE URL (https://api.simmer.trade) may need updating if the
Simmer platform changes its domain or API versioning. Check the Simmer docs
for the latest base URL before deploying.
"""
import logging
import aiohttp
from typing import Optional

logger = logging.getLogger(__name__)

SIMMER_BASE = "https://api.simmer.trade"


class SimmerClient:
    """Paper trading via Simmer platform."""

    def __init__(self, api_key: str, base_url: str = None):
        self.api_key = api_key
        self.base = base_url or SIMMER_BASE
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=15))
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    async def _get(self, path: str, params: dict = None) -> dict:
        async with self.session.get(f"{self.base}{path}", params=params) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _post(self, path: str, data: dict = None) -> dict:
        async with self.session.post(f"{self.base}{path}", json=data) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_active_markets(self) -> list:
        data = await self._get("/markets", {"status": "active"})
        return data if isinstance(data, list) else data.get("markets", [])

    async def place_bet(self, market_id: str, outcome: str, amount: float) -> dict:
        logger.info(f"Paper bet: {market_id} {outcome} ${amount:.2f}")
        return await self._post("/bets", {
            "market_id": market_id, "outcome": outcome, "amount": amount})

    async def get_positions(self) -> list:
        data = await self._get("/positions")
        return data if isinstance(data, list) else data.get("positions", [])

    async def get_balance(self) -> float:
        data = await self._get("/balance")
        return float(data.get("balance", 0))

    async def get_resolved_markets(self) -> list:
        data = await self._get("/markets", {"status": "resolved"})
        return data if isinstance(data, list) else data.get("markets", [])

    async def get_trade_history(self) -> list:
        data = await self._get("/trades")
        return data if isinstance(data, list) else data.get("trades", [])
