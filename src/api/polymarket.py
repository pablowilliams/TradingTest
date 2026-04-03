"""Async Polymarket API client - Gamma, CLOB, and Data APIs."""
import asyncio
import logging
import aiohttp
from typing import Optional

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"


class _BaseClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str):
        self.session = session
        self.base = base_url

    async def _get(self, path: str, params: dict = None, retries: int = 3):
        for attempt in range(retries):
            try:
                async with self.session.get(f"{self.base}{path}", params=params) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == retries - 1:
                    logger.error(f"API error {self.base}{path}: {e}")
                    raise
                await asyncio.sleep(2 ** attempt)


class GammaClient(_BaseClient):
    """Market data from Gamma API."""

    def __init__(self, session: aiohttp.ClientSession):
        super().__init__(session, GAMMA_BASE)

    async def get_markets(self, limit: int = 100, offset: int = 0,
                          active: bool = True, closed: bool = False) -> list:
        params = {"limit": limit, "offset": offset,
                  "active": str(active).lower(), "closed": str(closed).lower()}
        data = await self._get("/markets", params)
        return data if isinstance(data, list) else data.get("data", data.get("markets", []))

    async def get_market(self, market_id: str) -> dict:
        return await self._get(f"/markets/{market_id}")

    async def get_events(self, tag: str = None, limit: int = 100, offset: int = 0) -> list:
        params = {"limit": limit, "offset": offset}
        if tag:
            params["tag"] = tag
        data = await self._get("/events", params)
        return data if isinstance(data, list) else data.get("data", data.get("events", []))

    async def get_all_markets(self, active: bool = True) -> list:
        all_markets, offset = [], 0
        while True:
            batch = await self.get_markets(limit=100, offset=offset, active=active)
            if not batch:
                break
            all_markets.extend(batch)
            offset += len(batch)
            if len(batch) < 100:
                break
        return all_markets

    async def get_sports_markets(self) -> list:
        markets = await self.get_all_markets()
        sport_tags = {"nfl", "nba", "mlb", "nhl", "soccer", "mma", "ufc", "ncaa",
                      "tennis", "football", "basketball", "baseball", "hockey", "sports"}
        results = []
        for m in markets:
            tags = set(t.lower() for t in (m.get("tags") or []))
            if tags & sport_tags or "sport" in m.get("category", "").lower():
                results.append(m)
        return results

    async def get_crypto_markets(self) -> list:
        markets = await self.get_all_markets()
        crypto_kw = {"btc", "bitcoin", "eth", "ethereum", "crypto", "sol", "solana"}
        return [m for m in markets
                if any(kw in (m.get("question", "") + " " + m.get("description", "")).lower()
                       for kw in crypto_kw)]


class CLOBClient(_BaseClient):
    """Order book data from CLOB API."""

    def __init__(self, session: aiohttp.ClientSession):
        super().__init__(session, CLOB_BASE)

    async def get_orderbook(self, token_id: str) -> dict:
        return await self._get("/book", {"token_id": token_id})

    async def get_best_ask(self, token_id: str) -> float:
        data = await self._get("/price", {"token_id": token_id, "side": "sell"})
        return float(data.get("price", 0))

    async def get_best_bid(self, token_id: str) -> float:
        data = await self._get("/price", {"token_id": token_id, "side": "buy"})
        return float(data.get("price", 0))

    async def get_midpoint(self, token_id: str) -> float:
        data = await self._get("/midpoint", {"token_id": token_id})
        return float(data.get("mid", 0))

    async def get_price_history(self, token_id: str, fidelity: int = 1,
                                start_ts: int = None) -> list:
        params = {"market": token_id, "interval": "max", "fidelity": fidelity}
        if start_ts:
            params["startTs"] = start_ts
        data = await self._get("/prices-history", params)
        return data.get("history", data) if isinstance(data, dict) else data

    async def get_last_trade_price(self, token_id: str) -> float:
        data = await self._get("/last-trade-price", {"token_id": token_id})
        return float(data.get("price", 0))

    async def get_trades(self, token_id: str = None, maker: str = None) -> list:
        params = {}
        if token_id:
            params["asset_id"] = token_id
        if maker:
            params["maker_address"] = maker
        data = await self._get("/trades", params)
        return data if isinstance(data, list) else data.get("data", [])


class DataClient(_BaseClient):
    """User/trading data from Data API."""

    def __init__(self, session: aiohttp.ClientSession):
        super().__init__(session, DATA_BASE)

    async def get_positions(self, address: str) -> list:
        data = await self._get("/positions", {"user": address})
        return data if isinstance(data, list) else data.get("positions", [])

    async def get_trades(self, address: str = None, market_id: str = None) -> list:
        params = {}
        if address:
            params["user"] = address
        if market_id:
            params["market"] = market_id
        data = await self._get("/trades", params)
        return data if isinstance(data, list) else data.get("trades", [])

    async def get_portfolio_value(self, address: str) -> float:
        data = await self._get("/value", {"user": address})
        return float(data.get("value", 0))


class PolymarketClient:
    """Unified Polymarket client."""

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.gamma: Optional[GammaClient] = None
        self.clob: Optional[CLOBClient] = None
        self.data: Optional[DataClient] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self.gamma = GammaClient(self.session)
        self.clob = CLOBClient(self.session)
        self.data = DataClient(self.session)
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
