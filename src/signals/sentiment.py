"""Sentiment scoring from NewsAPI headlines."""
import logging
import time
import aiohttp
from typing import Optional

logger = logging.getLogger(__name__)

NEWSAPI_URL = "https://newsapi.org/v2/everything"

POSITIVE = {"rally", "surge", "bullish", "wins", "breakout", "soars", "jumps",
            "beats", "record", "strong", "gains", "profit", "boom", "uptick", "outperform"}
NEGATIVE = {"crash", "dump", "bearish", "loses", "falls", "drops", "plunges",
            "misses", "weak", "decline", "sells", "loss", "bust", "downturn", "underperform"}


class SentimentScorer:
    def __init__(self, api_key: str, session: Optional[aiohttp.ClientSession] = None):
        self.api_key = api_key
        self._external_session = session
        self._own_session: Optional[aiohttp.ClientSession] = None
        self._cache: dict = {}
        self.cache_ttl = 300

    @property
    def _session(self) -> aiohttp.ClientSession:
        """Return the shared session, or create a fallback one."""
        if self._external_session is not None and not self._external_session.closed:
            return self._external_session
        if self._own_session is None or self._own_session.closed:
            self._own_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self._own_session

    async def close(self):
        """Close only the internally-created session (not the shared one)."""
        if self._own_session and not self._own_session.closed:
            await self._own_session.close()

    async def get_sentiment(self, topic: str) -> float:
        if topic in self._cache:
            score, ts = self._cache[topic]
            if time.time() - ts < self.cache_ttl:
                return score
        score = await self._fetch_and_score(topic)
        self._cache[topic] = (score, time.time())
        return score

    async def _fetch_and_score(self, topic: str) -> float:
        try:
            params = {"q": topic, "sortBy": "publishedAt",
                      "pageSize": 20, "apiKey": self.api_key, "language": "en"}
            async with self._session.get(NEWSAPI_URL, params=params) as resp:
                if resp.status != 200:
                    return 0.0
                data = await resp.json()

            pos, neg = 0, 0
            for article in data.get("articles", []):
                text = f"{article.get('title', '')} {article.get('description', '')}".lower()
                pos += sum(1 for w in POSITIVE if w in text)
                neg += sum(1 for w in NEGATIVE if w in text)

            total = pos + neg
            if total == 0:
                return 0.0
            return max(-1.0, min(1.0, (pos - neg) / total))
        except Exception as e:
            logger.error(f"Sentiment error: {e}")
            return 0.0
