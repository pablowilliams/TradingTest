"""Twitter/X API - Real-time social sentiment from crypto and sports communities."""
import logging
import time
import re
import aiohttp
from typing import Optional
from collections import Counter

logger = logging.getLogger(__name__)

TWITTER_BASE = "https://api.twitter.com/2"

# Expanded sentiment lexicon for financial/crypto/sports context
BULLISH_WORDS = {
    "bullish", "moon", "pump", "rip", "send", "lfg", "buy", "long", "breakout",
    "rally", "surge", "rocket", "diamond", "hodl", "accumulate", "uptrend",
    "green", "gains", "win", "winning", "cover", "favorite", "underdog",
    "strong", "momentum", "all-time high", "ath", "dip buy", "support"
}
BEARISH_WORDS = {
    "bearish", "dump", "crash", "short", "sell", "rekt", "rug", "scam",
    "drop", "plunge", "red", "loss", "falling", "breakdown", "resistance",
    "weak", "overvalued", "bubble", "lose", "losing", "injury", "out",
    "suspend", "ban", "downtrend", "capitulation", "fear"
}


class TwitterSentiment:
    """Real-time social sentiment analysis from Twitter/X."""

    def __init__(self, bearer_token: str):
        self.bearer_token = bearer_token
        self.session: Optional[aiohttp.ClientSession] = None
        self._cache: dict = {}
        self.cache_ttl = 180  # 3 min cache (API rate limits)

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.bearer_token}"},
            timeout=aiohttp.ClientTimeout(total=15))
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    async def _search(self, query: str, max_results: int = 50) -> list:
        cache_key = f"search:{query}"
        if cache_key in self._cache:
            data, ts = self._cache[cache_key]
            if time.time() - ts < self.cache_ttl:
                return data
        try:
            params = {
                "query": f"{query} -is:retweet lang:en",
                "max_results": min(max_results, 100),
                "tweet.fields": "created_at,public_metrics,text"
            }
            async with self.session.get(f"{TWITTER_BASE}/tweets/search/recent", params=params) as resp:
                if resp.status == 429:
                    logger.warning("Twitter rate limited")
                    return []
                resp.raise_for_status()
                data = await resp.json()
                tweets = data.get("data", [])
                self._cache[cache_key] = (tweets, time.time())
                return tweets
        except Exception as e:
            logger.error(f"Twitter API error: {e}")
            return []

    def _analyze_tweets(self, tweets: list) -> dict:
        """Analyze sentiment from tweet texts."""
        if not tweets:
            return {"score": 0, "bullish_count": 0, "bearish_count": 0,
                    "tweet_count": 0, "signal": 0, "trending_words": []}

        bullish_count = 0
        bearish_count = 0
        total_engagement = 0
        word_freq = Counter()

        for tweet in tweets:
            text = tweet.get("text", "").lower()
            metrics = tweet.get("public_metrics", {})
            engagement = (metrics.get("like_count", 0) + metrics.get("retweet_count", 0) * 2
                          + metrics.get("reply_count", 0))

            # Engagement-weighted sentiment
            weight = max(1, engagement / 10)

            bull = sum(1 for w in BULLISH_WORDS if w in text)
            bear = sum(1 for w in BEARISH_WORDS if w in text)

            bullish_count += bull * weight
            bearish_count += bear * weight
            total_engagement += engagement

            # Track trending words
            words = re.findall(r'\b[a-z]{3,}\b', text)
            word_freq.update(words)

        total = bullish_count + bearish_count
        if total == 0:
            score = 0.0
        else:
            score = (bullish_count - bearish_count) / total

        # Signal strength scales with volume of discussion
        volume_multiplier = min(len(tweets) / 30, 1.5)  # More tweets = stronger signal
        signal = score * 0.5 * volume_multiplier

        return {
            "score": round(score, 4),
            "signal": round(max(-1, min(1, signal)), 4),
            "bullish_count": int(bullish_count),
            "bearish_count": int(bearish_count),
            "tweet_count": len(tweets),
            "total_engagement": total_engagement,
            "trending_words": [w for w, _ in word_freq.most_common(10)
                              if w not in {"the", "and", "for", "that", "this", "with"}]
        }

    async def get_crypto_sentiment(self, symbol: str = "BTC") -> dict:
        """Get crypto-specific sentiment."""
        queries = [f"${symbol}", f"#{symbol}", f"{symbol} crypto"]
        all_tweets = []
        for q in queries:
            tweets = await self._search(q, max_results=30)
            all_tweets.extend(tweets)

        result = self._analyze_tweets(all_tweets)
        result["topic"] = symbol
        result["type"] = "crypto"
        return result

    async def get_sports_sentiment(self, team: str, sport: str = "") -> dict:
        """Get sports team sentiment (injuries, lineup changes, fan confidence)."""
        query = f'"{team}" {sport}'.strip()
        tweets = await self._search(query, max_results=40)
        result = self._analyze_tweets(tweets)
        result["topic"] = team
        result["type"] = "sports"

        # Check for injury/lineup mentions specifically
        injury_count = sum(1 for t in tweets
                          if any(w in t.get("text", "").lower()
                                 for w in ["injury", "injured", "out", "doubtful", "questionable"]))
        result["injury_mentions"] = injury_count
        if injury_count > 3:
            result["signal"] *= 0.5  # Dampen signal if lots of injury talk

        return result

    async def get_fear_greed_proxy(self) -> dict:
        """Estimate fear/greed from overall crypto Twitter sentiment."""
        queries = ["crypto market", "bitcoin", "crypto trading"]
        all_tweets = []
        for q in queries:
            tweets = await self._search(q, max_results=30)
            all_tweets.extend(tweets)

        result = self._analyze_tweets(all_tweets)

        # Map sentiment score to fear/greed scale (0-100)
        fear_greed = int(50 + result["score"] * 50)
        fear_greed = max(0, min(100, fear_greed))

        label = ("extreme_fear" if fear_greed < 20 else "fear" if fear_greed < 40
                 else "neutral" if fear_greed < 60 else "greed" if fear_greed < 80
                 else "extreme_greed")

        # Contrarian signal: extreme fear = buy, extreme greed = sell
        signal = 0.0
        if fear_greed < 20:
            signal = 0.5  # Extreme fear = contrarian buy
        elif fear_greed < 35:
            signal = 0.2
        elif fear_greed > 80:
            signal = -0.5  # Extreme greed = contrarian sell
        elif fear_greed > 65:
            signal = -0.2

        return {
            "fear_greed_index": fear_greed,
            "label": label,
            "signal": signal,
            "raw_sentiment": result
        }
