"""Alpha Vantage API - Technical indicators and fundamental data."""
import logging
import time
import aiohttp
from typing import Optional

logger = logging.getLogger(__name__)

AV_BASE = "https://www.alphavantage.co/query"


class AlphaVantageClient:
    """Technical indicators + fundamentals for cross-market intelligence."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session: Optional[aiohttp.ClientSession] = None
        self._cache: dict = {}
        self.cache_ttl = 300  # 5 min

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    async def _get(self, params: dict) -> dict:
        cache_key = str(sorted(params.items()))
        if cache_key in self._cache:
            data, ts = self._cache[cache_key]
            if time.time() - ts < self.cache_ttl:
                return data
        params["apikey"] = self.api_key
        try:
            async with self.session.get(AV_BASE, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()
                if "Error Message" in data or "Note" in data:
                    logger.warning(f"Alpha Vantage: {data.get('Error Message', data.get('Note', ''))}")
                    return {}
                self._cache[cache_key] = (data, time.time())
                return data
        except Exception as e:
            logger.error(f"Alpha Vantage error: {e}")
            return {}

    # --- Crypto Technical Indicators ---
    # NOTE: Alpha Vantage DIGITAL_CURRENCY / crypto technical indicator functions
    # expect just the crypto symbol (e.g. "BTC"), NOT a pair like "BTCUSD".
    # The market is specified separately via the "market" parameter.

    async def get_crypto_rsi(self, symbol: str = "BTC", interval: str = "60min",
                              period: int = 14) -> dict:
        """RSI for crypto."""
        data = await self._get({
            "function": "RSI", "symbol": symbol,
            "interval": interval, "time_period": period,
            "series_type": "close", "datatype": "json"
        })
        values = list((data.get("Technical Analysis: RSI") or {}).values())
        if values:
            rsi = float(values[0].get("RSI", 50))
            return {"rsi": round(rsi, 2),
                    "signal": -0.5 if rsi > 70 else 0.5 if rsi < 30 else -0.2 if rsi > 60 else 0.2 if rsi < 40 else 0}
        return {"rsi": 50, "signal": 0}

    async def get_crypto_macd(self, symbol: str = "BTC", interval: str = "60min") -> dict:
        """MACD for crypto."""
        data = await self._get({
            "function": "MACD", "symbol": symbol,
            "interval": interval, "series_type": "close"
        })
        values = list((data.get("Technical Analysis: MACD") or {}).values())
        if values:
            v = values[0]
            macd = float(v.get("MACD", 0))
            signal_line = float(v.get("MACD_Signal", 0))
            hist = float(v.get("MACD_Hist", 0))
            return {"macd": round(macd, 4), "signal_line": round(signal_line, 4),
                    "histogram": round(hist, 4),
                    "signal": 0.3 if hist > 0 else -0.3}
        return {"macd": 0, "signal_line": 0, "histogram": 0, "signal": 0}

    async def get_crypto_bbands(self, symbol: str = "BTC", interval: str = "60min",
                                 period: int = 20) -> dict:
        """Bollinger Bands - detect squeeze/breakout."""
        data = await self._get({
            "function": "BBANDS", "symbol": symbol,
            "interval": interval, "time_period": period,
            "series_type": "close", "nbdevup": 2, "nbdevdn": 2
        })
        values = list((data.get("Technical Analysis: BBANDS") or {}).values())
        if values:
            v = values[0]
            upper = float(v.get("Real Upper Band", 0))
            middle = float(v.get("Real Middle Band", 0))
            lower = float(v.get("Real Lower Band", 0))
            bandwidth = (upper - lower) / middle * 100 if middle else 0

            # Narrow bands = squeeze incoming = expect big move
            # Price near lower band = oversold, near upper = overbought
            signal = 0.0
            if bandwidth < 3:  # Very tight squeeze
                signal = 0.1  # Volatility expansion coming
            return {"upper": round(upper, 2), "middle": round(middle, 2),
                    "lower": round(lower, 2), "bandwidth": round(bandwidth, 2),
                    "signal": signal, "squeeze": bandwidth < 3}
        return {"upper": 0, "middle": 0, "lower": 0, "bandwidth": 0, "signal": 0, "squeeze": False}

    async def get_crypto_stoch(self, symbol: str = "BTC", interval: str = "60min") -> dict:
        """Stochastic oscillator."""
        data = await self._get({
            "function": "STOCH", "symbol": symbol, "interval": interval
        })
        values = list((data.get("Technical Analysis: STOCH") or {}).values())
        if values:
            k = float(values[0].get("SlowK", 50))
            d = float(values[0].get("SlowD", 50))
            signal = 0.0
            if k < 20 and d < 20: signal = 0.4  # Oversold
            elif k > 80 and d > 80: signal = -0.4  # Overbought
            elif k > d: signal = 0.15  # Bullish crossover
            elif k < d: signal = -0.15  # Bearish crossover
            return {"slowK": round(k, 2), "slowD": round(d, 2), "signal": signal}
        return {"slowK": 50, "slowD": 50, "signal": 0}

    # --- Stock Fundamentals (for AskLivermore cross-ref) ---

    async def get_stock_overview(self, symbol: str) -> dict:
        """Company fundamentals for stock signals."""
        data = await self._get({"function": "OVERVIEW", "symbol": symbol})
        if not data:
            return {}
        return {
            "ticker": symbol,
            "name": data.get("Name", ""),
            "sector": data.get("Sector", ""),
            "pe_ratio": float(data.get("PERatio", 0) or 0),
            "market_cap": float(data.get("MarketCapitalization", 0) or 0),
            "52w_high": float(data.get("52WeekHigh", 0) or 0),
            "52w_low": float(data.get("52WeekLow", 0) or 0),
            "beta": float(data.get("Beta", 1) or 1),
            "eps": float(data.get("EPS", 0) or 0),
            "dividend_yield": float(data.get("DividendYield", 0) or 0),
            "analyst_target": float(data.get("AnalystTargetPrice", 0) or 0),
        }

    async def get_full_crypto_technicals(self, symbol: str = "BTC") -> dict:
        """All crypto technicals in one call."""
        rsi = await self.get_crypto_rsi(symbol)
        macd = await self.get_crypto_macd(symbol)
        bbands = await self.get_crypto_bbands(symbol)
        stoch = await self.get_crypto_stoch(symbol)

        composite = (
            rsi.get("signal", 0) * 0.30 +
            macd.get("signal", 0) * 0.25 +
            bbands.get("signal", 0) * 0.15 +
            stoch.get("signal", 0) * 0.30
        )

        return {
            "rsi": rsi, "macd": macd, "bbands": bbands, "stoch": stoch,
            "composite_signal": round(composite, 4),
            "squeeze_alert": bbands.get("squeeze", False),
            "timestamp": time.time()
        }
