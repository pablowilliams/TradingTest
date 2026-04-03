"""Market discovery - finds and categorizes all tradeable Polymarket markets."""
import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class MarketDiscovery:
    """Discovers, categorizes, and filters Polymarket markets."""

    def __init__(self, pm_client, config: dict = None):
        self.pm = pm_client
        self.config = config or {}
        self._cache: list = []
        self._cache_ts: float = 0
        self.cache_ttl = 120  # 2 min

    async def discover_all(self, force: bool = False) -> list:
        """Get all active markets, categorized."""
        if not force and self._cache and time.time() - self._cache_ts < self.cache_ttl:
            return self._cache

        raw = await self.pm.gamma.get_all_markets(active=True)
        categorized = []

        for m in raw:
            market = dict(m)
            market["market_type"] = self._categorize(market)
            market["yes_price"] = self._extract_price(market)
            market["seconds_remaining"] = self._estimate_time_remaining(market)
            categorized.append(market)

        self._cache = categorized
        self._cache_ts = time.time()
        logger.info(f"Discovered {len(categorized)} active markets")
        return categorized

    async def get_by_type(self, market_type: str) -> list:
        """Get markets of a specific type."""
        all_markets = await self.discover_all()
        return [m for m in all_markets if m["market_type"] == market_type]

    async def get_sports(self) -> list:
        return await self.get_by_type("sports")

    async def get_crypto(self) -> list:
        return await self.get_by_type("crypto")

    async def get_politics(self) -> list:
        return await self.get_by_type("politics")

    async def get_soccer_3way(self) -> list:
        """Get soccer markets with exactly 3 outcomes."""
        sports = await self.get_sports()
        results = []
        for m in sports:
            outcomes = m.get("outcomes", [])
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except Exception:
                    continue
            if len(outcomes) == 3:
                has_draw = any(o.lower() in ("draw", "tie", "empate") for o in outcomes)
                if has_draw:
                    results.append(m)
        return results

    async def get_high_liquidity(self, min_liquidity: float = 5000) -> list:
        """Get markets with high liquidity."""
        all_markets = await self.discover_all()
        return [m for m in all_markets if float(m.get("liquidity", 0)) >= min_liquidity]

    def _categorize(self, market: dict) -> str:
        """Categorize a market by type."""
        q = (market.get("question", "") + " " + market.get("description", "")).lower()
        tags = set(t.lower() for t in (market.get("tags") or []))

        # #94: Handle "football" tag correctly for soccer detection.
        # "football" in tags is ambiguous - it could be American football (NFL) or soccer.
        # If "football" tag is present but "nfl" is not, and the question/description
        # mentions soccer-related terms, treat it as soccer (which maps to sports).
        # The key issue: "football" was already in sport_tags, so it already matches sports.
        # The fix ensures we don't mis-categorize: "football" without NFL context
        # that has soccer indicators (league names, team patterns) stays as sports.
        sport_tags = {"nfl", "nba", "mlb", "nhl", "soccer", "mma", "ufc",
                      "ncaa", "tennis", "sports", "basketball"}
        # "football" is intentionally handled separately below
        soccer_indicators = {"premier league", "la liga", "bundesliga", "serie a",
                             "champions league", "world cup", "fifa", "uefa",
                             "epl", "ligue 1", "eredivisie", "mls soccer"}

        crypto_kw = {"btc", "bitcoin", "eth", "ethereum", "crypto", "solana", "sol"}
        politics_kw = {"election", "president", "congress", "senate", "vote", "governor",
                       "trump", "biden", "political", "government"}

        # Check for "football" tag: if present, determine if it's soccer or American football
        has_football_tag = "football" in tags
        has_nfl_tag = "nfl" in tags
        has_soccer_tag = "soccer" in tags
        has_soccer_context = has_soccer_tag or any(kw in q for kw in soccer_indicators)

        # If "football" tag is present, it's a sport regardless
        if has_football_tag or tags & sport_tags or any(kw in q for kw in sport_tags):
            return "sports"
        if any(kw in q for kw in crypto_kw):
            return "crypto"
        if any(kw in q for kw in politics_kw):
            return "politics"
        return "other"

    def _extract_price(self, market: dict) -> float:
        prices = market.get("outcomePrices", [0.5])
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except Exception:
                return 0.5
        return float(prices[0]) if prices else 0.5

    def _estimate_time_remaining(self, market: dict) -> float:
        end_str = market.get("endDate", market.get("expirationTime", ""))
        if not end_str:
            return 86400  # Default 24h
        try:
            from datetime import datetime
            end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            remaining = (end - datetime.now(end.tzinfo)).total_seconds()
            return max(0, remaining)
        except Exception:
            return 86400


class AskLivermoreXRef:
    """Cross-reference AskLivermore stock signals with Polymarket markets."""

    def __init__(self, polygon_client=None, alphavantage_client=None):
        self.polygon = polygon_client
        self.av = alphavantage_client

    async def enrich_signals(self, signals: list) -> list:
        """Enrich AskLivermore A+ signals with real market data."""
        enriched = []
        for sig in signals:
            ticker = sig.get("ticker", "")
            if not ticker:
                continue

            entry = dict(sig)

            # Get live data from Polygon
            if self.polygon:
                try:
                    snap = await self.polygon.get_stock_snapshot(ticker)
                    entry["live_price"] = snap.get("price", 0)
                    entry["change_pct"] = snap.get("change_pct", 0)
                    entry["volume"] = snap.get("volume", 0)
                except Exception:
                    pass

            # Get fundamentals from Alpha Vantage
            if self.av:
                try:
                    overview = await self.av.get_stock_overview(ticker)
                    entry["sector"] = overview.get("sector", "")
                    entry["pe_ratio"] = overview.get("pe_ratio", 0)
                    entry["market_cap"] = overview.get("market_cap", 0)
                    entry["analyst_target"] = overview.get("analyst_target", 0)
                    entry["beta"] = overview.get("beta", 1)
                except Exception:
                    pass

            enriched.append(entry)
        return enriched

    def find_polymarket_correlations(self, signals: list, pm_markets: list) -> list:
        """Find PM markets that might correlate with stock signals."""
        correlations = []

        # Map sectors to PM market keywords
        sector_map = {
            "Technology": ["tech", "ai", "chip", "semiconductor", "apple", "google", "microsoft"],
            "Energy": ["oil", "gas", "energy", "opec", "climate"],
            "Finance": ["bank", "fed", "interest rate", "inflation"],
            "Healthcare": ["health", "fda", "drug", "pharma", "vaccine"],
        }

        for sig in signals:
            sector = sig.get("sector", "")
            if sector not in sector_map:
                continue

            keywords = sector_map[sector]
            for market in pm_markets:
                q = market.get("question", "").lower()
                if any(kw in q for kw in keywords):
                    correlations.append({
                        "stock_signal": sig,
                        "pm_market": market,
                        "correlation_type": f"{sector} sector link",
                        "stock_grade": sig.get("grade", ""),
                        "stock_trend": "bullish" if sig.get("change_pct", 0) > 0 else "bearish"
                    })

        return correlations
