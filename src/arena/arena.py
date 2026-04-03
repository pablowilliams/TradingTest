"""Arena loop - orchestrates multi-bot trading with full signal pipeline.

Integrates ALL data sources:
- Binance BTC price feed (momentum, EMA crossovers)
- Coinglass derivatives (funding, liquidations, OI, L/S ratio)
- Polygon.io technicals (RSI, MACD, VWAP, Bollinger Bands)
- CryptoQuant on-chain (exchange flows, whale txns, miner outflow, stablecoin reserves)
- Twitter social sentiment (crypto + sports)
- Alpha Vantage technicals (RSI, MACD, Stochastic, BBands)
- NewsAPI sentiment
- Sportsbook edge detection (Odds API)
- Polymarket in-market momentum
- Orderflow analysis

PDF optimizations applied:
- ADX/regime filter (block mean-reversion in trending markets)
- Quarter-Kelly with 5% max bet cap + 70% balance floor
- Consecutive-loss circuit breaker (4 losses = 20-bar suspension)
- Confidence threshold > 0.58 for passive, > 0.53 for aggressive
- 8% daily loss limit hard cap
"""
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
    """Multi-bot trading arena with full signal integration."""

    def __init__(self, config, db, pm_client, simmer_client, verifier,
                 position_manager, telegram, price_feed, scorer, learner,
                 # New API clients
                 coinglass=None, polygon=None, cryptoquant=None,
                 twitter=None, alphavantage=None, odds_client=None,
                 sentiment_scorer=None, orderflow_signal=None,
                 sportsbook_edge=None, pm_momentum=None):
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

        # Enhanced data sources
        self.coinglass = coinglass
        self.polygon = polygon
        self.cryptoquant = cryptoquant
        self.twitter = twitter
        self.alphavantage = alphavantage
        self.odds_client = odds_client
        self.sentiment_scorer = sentiment_scorer
        self.orderflow = orderflow_signal
        self.sportsbook_edge = sportsbook_edge
        self.pm_momentum = pm_momentum

        self.bots = {}
        self._running = False

        # Cached enriched signals (updated periodically, not per-market)
        self._global_signals: dict = {}
        self._last_global_update: float = 0
        self._global_update_interval: float = 60  # Update every 60s

        # Circuit breaker state
        self._consecutive_losses: dict = {}  # bot_id -> count
        self._suspended_until: dict = {}  # bot_id -> timestamp

    def create_bots(self):
        for name, cls in STRATEGY_MAP.items():
            if self.config.strategies.get(name, {}).get("enabled", True):
                bot_id = f"{name}-v1"
                self.bots[bot_id] = cls(bot_id)
                logger.info(f"Created bot: {bot_id}")

    async def run(self):
        self._running = True
        self.create_bots()
        await self.price_feed.start()

        last_trade, last_resolve, last_evolve = 0, 0, 0
        logger.info(f"Arena started with {len(self.bots)} bots, all data sources active")

        while self._running:
            now = time.time()
            if now - last_trade >= self.config.poll_interval_seconds:
                last_trade = now
                await self._trade_cycle()
            if now - last_resolve >= self.config.resolution_check_seconds:
                last_resolve = now
                await self._resolution_cycle()
            if now - last_evolve >= self.config.evolution_interval_seconds:
                last_evolve = now
                await self._evolution_cycle()
            await asyncio.sleep(1)

    async def stop(self):
        self._running = False
        await self.price_feed.stop()

    async def _gather_global_signals(self):
        """Gather expensive API signals that don't change per-market."""
        now = time.time()
        if now - self._last_global_update < self._global_update_interval:
            return

        self._last_global_update = now
        signals = {}

        # --- Coinglass: Derivatives data ---
        if self.coinglass:
            try:
                cg = await self.coinglass.get_full_snapshot("BTC")
                signals["coinglass_composite"] = cg.get("composite_signal", 0)
                signals["funding_signal"] = cg["funding"].get("signal", 0)
                signals["funding_rate"] = cg["funding"].get("avg_rate", 0)
                signals["liquidation_signal"] = cg["liquidations"].get("signal", 0)
                signals["oi_signal"] = cg["open_interest"].get("signal", 0)
                signals["oi_change_1h"] = cg["open_interest"].get("change_1h_pct", 0)
                signals["long_short_signal"] = cg["long_short_ratio"].get("signal", 0)
                signals["long_pct"] = cg["long_short_ratio"].get("long_pct", 50)
                signals["derivatives_sentiment"] = cg["long_short_ratio"].get("sentiment", "balanced")
                logger.debug(f"Coinglass: composite={cg.get('composite_signal', 0):.3f}")
            except Exception as e:
                logger.warning(f"Coinglass fetch failed: {e}")

        # --- Polygon.io: Technicals ---
        if self.polygon:
            try:
                tech = await self.polygon.get_full_technicals("X:BTCUSD")
                signals["polygon_composite"] = tech.get("composite_signal", 0)
                signals["polygon_rsi"] = tech["rsi"].get("rsi", 50)
                signals["polygon_rsi_signal"] = tech["rsi"].get("signal", 0)
                signals["polygon_macd_signal"] = tech["macd"].get("signal", 0)
                signals["polygon_macd_hist"] = tech["macd"].get("histogram", 0)
                signals["polygon_vwap_signal"] = tech["snapshot"].get("vwap_signal", 0)
                signals["polygon_trend"] = tech.get("trend", "neutral")
                signals["btc_vwap"] = tech["snapshot"].get("vwap", 0)
                signals["btc_daily_change"] = tech["snapshot"].get("change_pct", 0)
                logger.debug(f"Polygon: composite={tech.get('composite_signal', 0):.3f}")
            except Exception as e:
                logger.warning(f"Polygon fetch failed: {e}")

        # --- CryptoQuant: On-chain ---
        if self.cryptoquant:
            try:
                onchain = await self.cryptoquant.get_full_onchain()
                signals["onchain_composite"] = onchain.get("composite_signal", 0)
                signals["exchange_netflow_signal"] = onchain["exchange_netflow"].get("signal", 0)
                signals["whale_signal"] = onchain["whale_activity"].get("signal", 0)
                signals["whale_alert"] = onchain.get("whale_alert", False)
                signals["miner_signal"] = onchain["miner_flow"].get("signal", 0)
                signals["stablecoin_signal"] = onchain["stablecoin_reserves"].get("signal", 0)
                signals["stablecoin_interpretation"] = onchain["stablecoin_reserves"].get("interpretation", "stable")
                logger.debug(f"CryptoQuant: composite={onchain.get('composite_signal', 0):.3f}")
            except Exception as e:
                logger.warning(f"CryptoQuant fetch failed: {e}")

        # --- Twitter: Social sentiment ---
        if self.twitter:
            try:
                crypto_sent = await self.twitter.get_crypto_sentiment("BTC")
                signals["twitter_crypto_signal"] = crypto_sent.get("signal", 0)
                signals["twitter_crypto_score"] = crypto_sent.get("score", 0)
                signals["twitter_tweet_count"] = crypto_sent.get("tweet_count", 0)
                signals["twitter_trending"] = crypto_sent.get("trending_words", [])

                fear_greed = await self.twitter.get_fear_greed_proxy()
                signals["fear_greed_index"] = fear_greed.get("fear_greed_index", 50)
                signals["fear_greed_signal"] = fear_greed.get("signal", 0)
                signals["fear_greed_label"] = fear_greed.get("label", "neutral")
                logger.debug(f"Twitter: sentiment={crypto_sent.get('signal', 0):.3f}, F&G={fear_greed.get('fear_greed_index', 50)}")
            except Exception as e:
                logger.warning(f"Twitter fetch failed: {e}")

        # --- Alpha Vantage: Additional technicals ---
        if self.alphavantage:
            try:
                av = await self.alphavantage.get_full_crypto_technicals("BTC")
                signals["av_composite"] = av.get("composite_signal", 0)
                signals["av_rsi"] = av["rsi"].get("rsi", 50)
                signals["av_rsi_signal"] = av["rsi"].get("signal", 0)
                signals["av_macd_signal"] = av["macd"].get("signal", 0)
                signals["av_stoch_signal"] = av["stoch"].get("signal", 0)
                signals["av_bbands_squeeze"] = av["bbands"].get("squeeze", False)
                signals["av_bbands_bandwidth"] = av["bbands"].get("bandwidth", 0)
                logger.debug(f"AlphaVantage: composite={av.get('composite_signal', 0):.3f}")
            except Exception as e:
                logger.warning(f"Alpha Vantage fetch failed: {e}")

        # --- NewsAPI sentiment ---
        if self.sentiment_scorer:
            try:
                news_sent = await self.sentiment_scorer.get_sentiment("bitcoin crypto")
                signals["news_sentiment"] = news_sent
            except Exception as e:
                logger.warning(f"NewsAPI fetch failed: {e}")

        # --- Sportsbook odds (for sports markets) ---
        if self.odds_client:
            try:
                signals["sportsbook_odds"] = await self.odds_client.get_all_sport_odds()
            except Exception as e:
                logger.warning(f"Odds API fetch failed: {e}")

        # --- MEGA COMPOSITE: Combine all global signals ---
        mega_composite = (
            signals.get("coinglass_composite", 0) * 0.20 +
            signals.get("polygon_composite", 0) * 0.15 +
            signals.get("onchain_composite", 0) * 0.15 +
            signals.get("twitter_crypto_signal", 0) * 0.10 +
            signals.get("fear_greed_signal", 0) * 0.10 +
            signals.get("av_composite", 0) * 0.15 +
            signals.get("news_sentiment", 0) * 0.05 +
            (0.1 if signals.get("polygon_trend") == "bullish" else -0.1 if signals.get("polygon_trend") == "bearish" else 0) * 0.10
        )
        signals["mega_composite"] = round(mega_composite, 4)

        # Regime detection (from PDF: ADX-based)
        # Use RSI extremes + trend as proxy
        avg_rsi = (signals.get("polygon_rsi", 50) + signals.get("av_rsi", 50)) / 2
        if avg_rsi > 65 or avg_rsi < 35:
            signals["regime"] = "trending"
        elif signals.get("av_bbands_squeeze", False):
            signals["regime"] = "squeeze"  # Breakout imminent
        else:
            signals["regime"] = "ranging"

        self._global_signals = signals
        logger.info(f"Global signals updated: mega_composite={mega_composite:.4f}, "
                     f"regime={signals.get('regime', '?')}, F&G={signals.get('fear_greed_index', '?')}")

    async def _trade_cycle(self):
        """Discover markets, gather ALL signals, run bots."""
        try:
            # Update global (expensive) signals
            await self._gather_global_signals()

            markets = await self.pm.gamma.get_markets(limit=50, active=True)
            price_snap = self.price_feed.get_snapshot()

            for market in markets:
                # Build rich signal dict combining everything
                signals = dict(self._global_signals)  # Copy global signals

                # Per-market BTC signals
                signals.update({
                    "btc_momentum": price_snap.get("momentum_5m", 0),
                    "btc_momentum_5m": price_snap.get("momentum_5m", 0),
                    "btc_momentum_15m": price_snap.get("momentum_15m", 0),
                    "ema_bullish": price_snap.get("ema_bullish", False),
                    "btc_price": price_snap.get("price", 0),
                    "volume": float(market.get("volume", 0)),
                })

                # Per-market Polymarket momentum
                if self.pm_momentum:
                    try:
                        tokens = market.get("clobTokenIds", [])
                        if isinstance(tokens, str):
                            import json
                            try: tokens = json.loads(tokens)
                            except: tokens = []
                        if tokens:
                            mom = await self.pm_momentum.get_momentum(tokens[0])
                            signals["pm_direction"] = mom.get("direction", "unknown")
                            signals["pm_momentum"] = mom.get("strength", 0)
                            signals["pm_change_5m"] = mom.get("change_5m", 0)
                            signals["pm_change_15m"] = mom.get("change_15m", 0)
                    except Exception:
                        signals.setdefault("pm_direction", "unknown")

                # Per-market orderflow
                if self.orderflow:
                    try:
                        tokens = market.get("clobTokenIds", [])
                        if isinstance(tokens, str):
                            import json
                            try: tokens = json.loads(tokens)
                            except: tokens = []
                        if tokens:
                            of = await self.orderflow.analyze(tokens[0])
                            signals["orderflow_imbalance"] = of.get("imbalance", 0)
                            signals["spread"] = of.get("spread", 1)
                            signals["volume_pressure"] = of.get("volume_pressure", 0)
                    except Exception:
                        pass

                # Sports-specific: sportsbook edge
                if self.sportsbook_edge and signals.get("sportsbook_odds"):
                    try:
                        opps = self.sportsbook_edge.find_opportunities(
                            [market], signals["sportsbook_odds"])
                        if opps:
                            best = opps[0]
                            signals["sportsbook_edge"] = best.get("edge", 0)
                            signals["sportsbook_ev"] = best.get("ev", 0)
                            signals["bookmaker_count"] = best.get("bookmaker_count", 0)
                    except Exception:
                        pass

                # Sports-specific: Twitter sentiment for teams
                if self.twitter and "sport" in market.get("market_type", "").lower():
                    try:
                        outcomes = market.get("outcomes", [])
                        if isinstance(outcomes, str):
                            import json
                            try: outcomes = json.loads(outcomes)
                            except: outcomes = []
                        if outcomes and len(outcomes) >= 2:
                            team_sent = await self.twitter.get_sports_sentiment(outcomes[0])
                            signals["team1_twitter_signal"] = team_sent.get("signal", 0)
                            signals["team1_injury_mentions"] = team_sent.get("injury_mentions", 0)
                    except Exception:
                        pass

                # --- Run each bot ---
                for bot_id, bot in self.bots.items():
                    try:
                        # Circuit breaker check (PDF: 4 consecutive losses = suspension)
                        if bot_id in self._suspended_until:
                            if time.time() < self._suspended_until[bot_id]:
                                continue
                            del self._suspended_until[bot_id]
                            self._consecutive_losses[bot_id] = 0

                        # Load learning data
                        bot.learning_data = await self.db.get_learning_data(bot_id)
                        bot.total_observations = sum(
                            d.get("obs", 0) for d in bot.learning_data.values())

                        # Regime filter (PDF: block mean-reversion in trending)
                        if (bot.strategy_name == "mean_reversion"
                                and signals.get("regime") == "trending"):
                            continue

                        # Inject mega composite as additional signal
                        signals["mega_signal"] = signals.get("mega_composite", 0)

                        decision = bot.make_decision(market, signals)
                        if decision["action"] == "hold":
                            continue

                        # Verify trade (7-check gate)
                        verification = await self.verifier.verify(decision, market, signals)
                        if not verification["passed"]:
                            if self.config.telegram.get("notify_on_verification_fail"):
                                await self.telegram.notify_verification_fail(
                                    bot_id, market.get("question", "?"),
                                    verification["reasons"])
                            continue

                        # Balance floor check (PDF: 70% of peak equity)
                        balance = self.config.paper_balance
                        total_pnl = await self.db.get_total_pnl()
                        peak = max(balance, balance + total_pnl)
                        current_equity = balance + total_pnl
                        if current_equity < peak * 0.70:
                            logger.warning(f"Balance floor hit: {current_equity:.2f} < 70% of peak {peak:.2f}")
                            continue

                        # Size with quarter-Kelly, 5% max cap (PDF recommendation)
                        amount = bot.bet_size(
                            decision["confidence"], current_equity,
                            min(self.config.risk.get("max_position_size", 50),
                                current_equity * 0.05))

                        if not self.pos_mgr.check_daily_limit():
                            logger.warning("Daily loss cap hit")
                            continue

                        # Place trade
                        if self.config.mode == "paper" and self.simmer:
                            result = await self.simmer.place_bet(
                                market.get("id", ""), decision["outcome"], amount)
                        else:
                            result = {"status": "simulated", "id": str(uuid.uuid4())}

                        # Enrich decision with all signals for learning
                        enriched_snapshot = dict(decision.get("signals_snapshot", {}))
                        enriched_snapshot.update({
                            "mega_composite": signals.get("mega_composite", 0),
                            "coinglass": signals.get("coinglass_composite", 0),
                            "onchain": signals.get("onchain_composite", 0),
                            "twitter": signals.get("twitter_crypto_signal", 0),
                            "fear_greed": signals.get("fear_greed_index", 50),
                            "polygon_rsi": signals.get("polygon_rsi", 50),
                            "regime": signals.get("regime", "unknown"),
                        })

                        trade_id = await self.db.insert_trade(
                            bot_id, market.get("id", ""), market.get("market_type", "unknown"),
                            decision["action"], decision["outcome"], amount,
                            float(market.get("outcomePrices", [0.5])[0] if isinstance(
                                market.get("outcomePrices"), list) else 0.5),
                            decision["confidence"], enriched_snapshot, verification)

                        if self.config.telegram.get("notify_on_buy"):
                            await self.telegram.notify_buy(
                                bot_id, market.get("question", "?"),
                                decision["outcome"], amount,
                                enriched_snapshot.get("yes_price", 0),
                                decision["confidence"], decision["reasoning"])

                        logger.info(f"TRADE: {bot_id} {decision['outcome']} ${amount:.2f} "
                                    f"conf={decision['confidence']:.2f} on {market.get('question', '?')[:50]}")

                    except Exception as e:
                        logger.error(f"Bot {bot_id} error: {e}")

        except Exception as e:
            logger.error(f"Trade cycle error: {e}")

    async def _resolution_cycle(self):
        """Check for resolved trades, update stats, feed learning."""
        try:
            open_trades = await self.db.get_open_trades()
            for trade in open_trades:
                # Check market status via Polymarket
                try:
                    market = await self.pm.gamma.get_market(trade["market_id"])
                    if not market or market.get("active", True):
                        continue

                    # Determine win/loss
                    resolved_outcome = market.get("outcome", "")
                    won = (trade["outcome"] == "YES" and resolved_outcome == "Yes") or \
                          (trade["outcome"] == "NO" and resolved_outcome == "No")
                    pnl = (trade["amount"] * (1 / trade["price"] - 1)) if won else -trade["amount"]

                    await self.db.resolve_trade(trade["id"], pnl)
                    await self.db.update_bot_stats(trade["bot_id"], won, pnl)
                    self.pos_mgr.record_trade_result(pnl)

                    # Feed online learning
                    if self.learner:
                        await self.learner.record_outcome(trade, won, pnl)

                    # Circuit breaker tracking
                    if not won:
                        self._consecutive_losses[trade["bot_id"]] = \
                            self._consecutive_losses.get(trade["bot_id"], 0) + 1
                        if self._consecutive_losses[trade["bot_id"]] >= 4:
                            # PDF: 4 consecutive losses = 20-bar suspension (~5 min)
                            self._suspended_until[trade["bot_id"]] = time.time() + 300
                            logger.warning(f"Circuit breaker: {trade['bot_id']} suspended for 5 min")
                    else:
                        self._consecutive_losses[trade["bot_id"]] = 0

                    # Telegram notification
                    if self.config.telegram.get("notify_on_sell"):
                        await self.telegram.notify_sell(
                            trade["bot_id"], trade.get("market_type", "?"),
                            pnl, "resolved" if won else "loss")

                except Exception as e:
                    logger.debug(f"Resolution check for {trade['market_id']}: {e}")

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
