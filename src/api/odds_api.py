"""The Odds API client for sportsbook odds comparison."""
import logging
import aiohttp
from typing import Optional

logger = logging.getLogger(__name__)

ODDS_BASE = "https://api.the-odds-api.com/v4"


def american_to_implied_prob(odds: int) -> float:
    if odds > 0:
        return 100 / (odds + 100)
    return abs(odds) / (abs(odds) + 100)


def decimal_to_implied_prob(odds: float) -> float:
    return 1 / odds if odds > 0 else 0


class OddsAPIClient:
    """The Odds API for sportsbook odds."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base = ODDS_BASE
        self.session: Optional[aiohttp.ClientSession] = None
        self.requests_remaining: int = 500
        self.requests_used: int = 0

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    async def _get(self, path: str, params: dict = None) -> list:
        params = params or {}
        params["apiKey"] = self.api_key
        async with self.session.get(f"{self.base}{path}", params=params) as resp:
            self.requests_remaining = int(resp.headers.get("x-requests-remaining", 500))
            self.requests_used = int(resp.headers.get("x-requests-used", 0))
            resp.raise_for_status()
            return await resp.json()

    async def get_sports(self) -> list:
        return await self._get("/sports")

    async def get_odds(self, sport_key: str, regions: list = None,
                       markets: list = None) -> list:
        params = {"regions": ",".join(regions or ["us"]),
                  "markets": ",".join(markets or ["h2h"]),
                  "oddsFormat": "american"}
        return await self._get(f"/sports/{sport_key}/odds", params)

    async def get_event_odds(self, sport_key: str, event_id: str,
                             regions: list = None, markets: list = None) -> dict:
        params = {"regions": ",".join(regions or ["us"]),
                  "markets": ",".join(markets or ["h2h"]),
                  "oddsFormat": "american"}
        return await self._get(f"/sports/{sport_key}/events/{event_id}/odds", params)

    async def get_all_sport_odds(self) -> dict:
        sports = await self.get_sports()
        active = [s for s in sports if s.get("active") and not s.get("has_outrights")]
        all_odds = {}
        for sport in active[:10]:
            try:
                odds = await self.get_odds(sport["key"])
                all_odds[sport["key"]] = {"title": sport.get("title", sport["key"]),
                                          "events": odds}
            except Exception as e:
                logger.warning(f"Failed odds for {sport['key']}: {e}")
        return all_odds

    def extract_implied_probs(self, event: dict) -> dict:
        outcomes = {}
        for bk in event.get("bookmakers", []):
            for mkt in bk.get("markets", []):
                if mkt["key"] != "h2h":
                    continue
                for out in mkt.get("outcomes", []):
                    name = out["name"]
                    prob = american_to_implied_prob(out["price"])
                    if name not in outcomes:
                        outcomes[name] = {"probs": [], "bookmakers": []}
                    outcomes[name]["probs"].append(prob)
                    outcomes[name]["bookmakers"].append(bk["key"])
        for data in outcomes.values():
            p = data["probs"]
            data["avg_prob"] = sum(p) / len(p) if p else 0
            data["min_prob"] = min(p) if p else 0
            data["max_prob"] = max(p) if p else 0
            data["bookmaker_count"] = len(set(data["bookmakers"]))
        return outcomes
