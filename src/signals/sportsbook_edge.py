"""Sportsbook vs Polymarket edge detection."""
import json
import logging
import re
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

TEAM_ALIASES = {
    "man utd": "manchester united", "man city": "manchester city",
    "spurs": "tottenham hotspur", "wolves": "wolverhampton",
    "lakers": "los angeles lakers", "celtics": "boston celtics",
    "warriors": "golden state warriors", "niners": "san francisco 49ers",
    "bucs": "tampa bay buccaneers", "pats": "new england patriots",
}


def normalize_team(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r'\b(the|fc|cf|sc|ac)\b', '', name).strip()
    name = re.sub(r'[^a-z0-9\s]', '', name).strip()
    return TEAM_ALIASES.get(name, name)


class SportsbookEdge:
    def __init__(self, delta_cap: float = 0.03, min_edge: float = 0.02,
                 min_liquidity: float = 1000, min_match_confidence: float = 0.80):
        self.delta_cap = delta_cap
        self.min_edge = min_edge
        self.min_liquidity = min_liquidity
        self.min_match_confidence = min_match_confidence

    def find_opportunities(self, pm_markets: list, odds_data: dict) -> list:
        opportunities = []
        for pm in pm_markets:
            liquidity = float(pm.get("liquidity", 0))
            if liquidity < self.min_liquidity:
                continue

            outcomes = pm.get("outcomes", [])
            prices = pm.get("outcomePrices", [])
            if isinstance(outcomes, str):
                try: outcomes = json.loads(outcomes)
                except: continue
            if isinstance(prices, str):
                try: prices = json.loads(prices)
                except: continue

            for sport_key, sport_data in odds_data.items():
                for event in sport_data.get("events", []):
                    match_conf = self._match_event(pm.get("question", ""), outcomes, event)
                    if match_conf < self.min_match_confidence * 100:
                        continue

                    for bk in event.get("bookmakers", []):
                        for mkt in bk.get("markets", []):
                            if mkt["key"] != "h2h":
                                continue
                            for sb_out in mkt.get("outcomes", []):
                                sb_price = sb_out["price"]
                                sb_prob = (100 / (sb_price + 100) if sb_price > 0
                                           else abs(sb_price) / (abs(sb_price) + 100))

                                for i, pm_out in enumerate(outcomes):
                                    if i >= len(prices):
                                        break
                                    pm_price = float(prices[i])
                                    if fuzz.token_sort_ratio(normalize_team(pm_out),
                                                             normalize_team(sb_out["name"])) < 75:
                                        continue

                                    edge = sb_prob - pm_price
                                    if edge >= self.min_edge and abs(edge) <= self.delta_cap:
                                        ev = (sb_prob * (1 / pm_price - 1)) - (1 - sb_prob)
                                        opportunities.append({
                                            "market_id": pm.get("id", ""),
                                            "question": pm.get("question", ""),
                                            "outcome": pm_out, "pm_price": pm_price,
                                            "sb_prob": round(sb_prob, 4),
                                            "edge": round(edge, 4), "ev": round(ev, 4),
                                            "sport": sport_key, "bookmaker": bk["key"],
                                            "match_confidence": round(match_conf / 100, 2),
                                            "liquidity": liquidity})

        opportunities.sort(key=lambda x: x["ev"], reverse=True)
        return opportunities

    def _match_event(self, pm_question: str, pm_outcomes: list, sb_event: dict) -> float:
        sb_teams = [sb_event.get("home_team", ""), sb_event.get("away_team", "")]
        best = 0
        for pm_out in pm_outcomes:
            for sb_team in sb_teams:
                best = max(best, fuzz.token_sort_ratio(normalize_team(pm_out), normalize_team(sb_team)))
        q_score = fuzz.partial_ratio(pm_question.lower(), " vs ".join(sb_teams).lower())
        return max(best, q_score)
