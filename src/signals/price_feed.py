"""Real-time BTC price feed via Binance WebSocket."""
import asyncio
import json
import logging
import time
from collections import deque
from typing import Optional

import websockets

logger = logging.getLogger(__name__)

BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@kline_1m"


class PriceFeed:
    """BTC price feed with momentum and EMA calculations."""

    def __init__(self):
        self.prices = deque(maxlen=100)
        self.current_price: float = 0.0
        self.ws = None
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._connect())
        logger.info("Price feed started")

    async def stop(self):
        self._running = False
        if self.ws:
            await self.ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _connect(self):
        while self._running:
            try:
                async with websockets.connect(BINANCE_WS) as ws:
                    self.ws = ws
                    logger.info("Connected to Binance WS")
                    async for msg in ws:
                        if not self._running:
                            break
                        data = json.loads(msg)
                        kline = data.get("k", {})
                        close = float(kline.get("c", 0))
                        if close > 0:
                            self.current_price = close
                            if kline.get("x"):
                                self.prices.append(close)
            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                logger.warning(f"WS disconnected: {e}, reconnecting...")
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Price feed error: {e}")
                await asyncio.sleep(5)

    def _ema(self, period: int) -> float:
        if len(self.prices) < period:
            return self.current_price
        data = list(self.prices)[-period * 2:]
        multiplier = 2 / (period + 1)
        ema = data[0]
        for price in data[1:]:
            ema = (price - ema) * multiplier + ema
        return ema

    def _momentum(self, bars_back: int) -> float:
        if len(self.prices) < bars_back or self.current_price == 0:
            return 0.0
        old_price = list(self.prices)[-bars_back]
        if old_price == 0:
            return 0.0
        return (self.current_price - old_price) / old_price * 100

    def get_snapshot(self) -> dict:
        return {
            "price": self.current_price,
            "momentum_1m": self._momentum(1),
            "momentum_5m": self._momentum(5),
            "momentum_15m": self._momentum(15),
            "ema_9": self._ema(9),
            "ema_21": self._ema(21),
            "ema_bullish": self._ema(9) > self._ema(21),
            "bars_available": len(self.prices),
            "timestamp": time.time()
        }
