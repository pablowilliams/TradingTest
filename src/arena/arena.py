"""Arena loop - orchestrates multi-bot trading, resolution, and evolution."""
import asyncio
import logging
import random
import time
import uuid
from typing import Optional

from ..strategies.momentum import MomentumStrategy
from ..strategies.mean_reversion import MeanReversionStrategy
from ..strategies.sports_edge import SportsEdgeStrategy
from ..strategies.soccer_3way import Soccer3WayStrategy
from ..strategies.sniper import SniperStrategy
from ..strategies.hybrid import HybridStrategy

logger = logging.getLogger(__name__)

STRATEGY_MAP = {
    "momentum": MomentumStrategy,
    "mean_reversion": MeanReversionStrategy,
    "sports_edge": SportsEdgeStrategy,
    "soccer_3way": Soccer3WayStrategy,
    "sniper": SniperStrategy,
    "hybrid": HybridStrategy,
}


class Arena:
    """Multi-bot trading arena with evolution."""

    def __init__(self, config, db, pm_client, simmer_client, verifier,
                 position_manager, telegram, price_feed, scorer, learner):
        self.config = config
        self.db = db
        self.pm = pm_client
        self.simmer = simmer_client
        self.verifier = verifier
        self.pos_mgr = position_manager
        self.telegram = telegram
        self.price_feed = price_feed
        self.scorer = scorer
        self.learner = learner
        self.bots = {}
        self._running = False

    def create_bots(self):
        """Initialize the bot roster."""
        for name, cls in STRATEGY_MAP.items():
            if self.config.strategies.get(name, {}).get("enabled", True):
                bot_id = f"{name}-v1"
                self.bots[bot_id] = cls(bot_id)
                logger.info(f"Created bot: {bot_id}")

    async def run(self):
        """Main arena loop."""
        self._running = True
        self.create_bots()
        await self.price_feed.start()

        last_trade = 0
        last_resolve = 0
        last_evolve = 0

        logger.info(f"Arena started with {len(self.bots)} bots")

        while self._running:
            now = time.time()

            # Trade cycle
            if now - last_trade >= self.config.poll_interval_seconds:
                last_trade = now
                await self._trade_cycle()

            # Resolution cycle
            if now - last_resolve >= self.config.resolution_check_seconds:
                last_resolve = now
                await self._resolution_cycle()

            # Evolution cycle
            if now - last_evolve >= self.config.evolution_interval_seconds:
                last_evolve = now
                await self._evolution_cycle()

            await asyncio.sleep(1)

    async def stop(self):
        self._running = False
        await self.price_feed.stop()

    async def _trade_cycle(self):
        """Discover markets, gather signals, run bots."""
        try:
            markets = await self.pm.gamma.get_markets(limit=50, active=True)
            price_snap = self.price_feed.get_snapshot()

            for market in markets:
                signals = {
                    "btc_momentum": price_snap.get("momentum_5m", 0),
                    "btc_momentum_5m": price_snap.get("momentum_5m", 0),
                    "ema_bullish": price_snap.get("ema_bullish", False),
                    "pm_direction": "unknown",
                    "pm_momentum": 0,
                    "pm_change_5m": 0,
                    "volume": float(market.get("volume", 0)),
                }

                for bot_id, bot in self.bots.items():
                    try:
                        # Load learning data
                        bot.learning_data = await self.db.get_learning_data(bot_id)
                        bot.total_observations = sum(
                            d.get("obs", 0) for d in bot.learning_data.values())

                        decision = bot.make_decision(market, signals)
                        if decision["action"] == "hold":
                            continue

                        # Verify trade
                        verification = await self.verifier.verify(decision, market, signals)
                        if not verification["passed"]:
                            if self.config.telegram.get("notify_on_verification_fail"):
                                await self.telegram.notify_verification_fail(
                                    bot_id, market.get("question", "?"),
                                    verification["reasons"])
                            continue

                        # Size and place
                        amount = bot.bet_size(
                            decision["confidence"],
                            self.config.paper_balance,
                            self.config.risk.get("max_position_size", 50))

                        if not self.pos_mgr.check_daily_limit():
                            logger.warning("Daily loss cap hit")
                            continue

                        # Place trade (paper or live)
                        if self.config.mode == "paper" and self.simmer:
                            result = await self.simmer.place_bet(
                                market.get("id", ""), decision["outcome"], amount)
                        else:
                            result = {"status": "simulated", "id": str(uuid.uuid4())}

                        trade_id = await self.db.insert_trade(
                            bot_id, market.get("id", ""), market.get("market_type", "unknown"),
                            decision["action"], decision["outcome"], amount,
                            float(market.get("outcomePrices", [0.5])[0] if isinstance(
                                market.get("outcomePrices"), list) else 0.5),
                            decision["confidence"], decision.get("signals_snapshot", {}),
                            verification)

                        if self.config.telegram.get("notify_on_buy"):
                            await self.telegram.notify_buy(
                                bot_id, market.get("question", "?"),
                                decision["outcome"], amount,
                                decision.get("signals_snapshot", {}).get("yes_price", 0),
                                decision["confidence"], decision["reasoning"])

                        logger.info(f"Trade placed: {bot_id} {decision['outcome']} "
                                    f"${amount:.2f} on {market.get('question', '?')[:50]}")

                    except Exception as e:
                        logger.error(f"Bot {bot_id} error: {e}")

        except Exception as e:
            logger.error(f"Trade cycle error: {e}")

    async def _resolution_cycle(self):
        """Check for resolved trades and update stats."""
        try:
            open_trades = await self.db.get_open_trades()
            for trade in open_trades:
                # Check if market resolved (simplified - real impl checks Simmer/PM)
                # For now, mark as resolved after some time (placeholder)
                pass
        except Exception as e:
            logger.error(f"Resolution cycle error: {e}")

    async def _evolution_cycle(self):
        """Kill underperformers, mutate survivors."""
        try:
            stats = await self.db.get_all_bot_stats()
            min_trades = self.config.evolution.get("min_trades_for_eval", 20)
            survival_wr = self.config.evolution.get("survival_win_rate", 0.65)

            eligible = [s for s in stats if s["total_trades"] >= min_trades]
            if not eligible:
                return

            survivors = [s for s in eligible if s["win_rate"] >= survival_wr]
            losers = [s for s in eligible if s["win_rate"] < survival_wr]

            if not survivors:
                survivors = [max(eligible, key=lambda x: x["win_rate"])]
                losers = [s for s in eligible if s["bot_id"] != survivors[0]["bot_id"]]

            for loser in losers:
                parent = random.choice(survivors)
                parent_bot = self.bots.get(parent["bot_id"])
                if not parent_bot:
                    continue

                new_params = parent_bot.mutate()
                new_id = f"{parent_bot.strategy_name}-v{random.randint(2, 99)}"
                cls = STRATEGY_MAP.get(parent_bot.strategy_name)
                if cls:
                    self.bots.pop(loser["bot_id"], None)
                    self.bots[new_id] = cls(new_id, new_params, parent_bot.generation + 1)

                    await self.telegram.notify_evolution(
                        loser["bot_id"], new_id,
                        f"WR {loser['win_rate']:.1%} < {survival_wr:.0%}")

                    logger.info(f"Evolution: {loser['bot_id']} -> {new_id}")

        except Exception as e:
            logger.error(f"Evolution error: {e}")
