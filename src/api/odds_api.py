"""The Odds API client for sportsbook odds comparison."""
import logging
import aiohttp
from typing import Optional

logger = logging.getLogger(__name__)

ODDS_BASE = "https://api.the-odds-api.com/v4"

# Sports the bot actively trades, in priority order.
# These are fetched first; remaining active sports follow after.
PRIORITY_SPORTS = [
    "americanfootball_nfl",
    "basketball_nba",
    "soccer_epl", "soccer_usa_mls", "soccer_spain_la_liga",
    "soccer_germany_bundesliga", "soccer_italy_serie_a", "soccer_france_ligue_one",
    "baseball_mlb",
    "icehockey_nhl",
]


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
        # NOTE: The Odds API does not support header-based authentication;
        # the API key must be passed as a query parameter (?apiKey=...).
        # See: https://the-odds-api.com/liveapi/guides/v4/
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
        """Fetch odds for active sports, prioritising the ones the bot trades."""
        sports = await self.get_sports()
        active_by_key = {s["key"]: s for s in sports if s.get("active")}

        # Build ordered list: priority sports first, then the rest.
        # Outright sports (futures / season-winner markets) are included but
        # placed after head-to-head sports so they don't crowd out match odds.
        ordered_keys: list[str] = []
        for key in PRIORITY_SPORTS:
            if key in active_by_key:
                ordered_keys.append(key)

        # Append remaining active sports (head-to-head first, outrights last)
        for s in sports:
            if not s.get("active"):
                continue
            if s["key"] in ordered_keys:
                continue
            if not s.get("has_outrights"):
                ordered_keys.append(s["key"])
        for s in sports:
            if not s.get("active"):
                continue
            if s["key"] in ordered_keys:
                continue
            # has_outrights == True: still include, just at the end
            ordered_keys.append(s["key"])

        all_odds = {}
        for key in ordered_keys[:15]:  # cap API usage
            try:
                odds = await self.get_odds(key)
                all_odds[key] = {"title": active_by_key[key].get("title", key),
                                 "events": odds}
            except Exception as e:
                logger.warning(f"Failed odds for {key}: {e}")
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
