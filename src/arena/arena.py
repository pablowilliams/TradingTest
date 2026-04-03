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
import json
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
from ..market_discovery.discovery import MarketDiscovery, AskLivermoreXRef

logger = logging.getLogger(__name__)

# Try to import Rust engine, fall back to pure Python
try:
    from trading_engine import OrderbookProcessor, combine_signals, check_soccer_3way
    RUST_AVAILABLE = True
    logger.info("Rust engine loaded")
except ImportError:
    RUST_AVAILABLE = False
    logger.info("Rust engine not available, using pure Python")

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
                 # API clients
                 coinglass=None, polygon=None, cryptoquant=None,
                 twitter=None, alphavantage=None, odds_client=None,
                 sentiment_scorer=None, orderflow_signal=None,
                 sportsbook_edge=None, pm_momentum=None,
                 asklivermore=None):
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
        self.asklivermore = asklivermore

        # Market discovery + AskLivermore cross-reference
        self.discovery = MarketDiscovery(pm_client, config.__dict__ if hasattr(config, '__dict__') else {})
        self.al_xref = AskLivermoreXRef(polygon, alphavantage)

        # Rust orderbook processor (if available)
        self.rust_orderbook = OrderbookProcessor() if RUST_AVAILABLE else None

        self.bots = {}
        self._running = False

        # Cached enriched signals
        self._global_signals: dict = {}
        self._last_global_update: float = 0
        self._global_update_interval: float = 60

        # AskLivermore signals cache
        self._al_signals: list = []
        self._last_al_scrape: float = 0
        self._al_scrape_interval: float = 3600  # Scrape once per hour

        # Circuit breaker state
        self._consecutive_losses: dict = {}
        self._suspended_until: dict = {}

        # FIX #71: Peak equity tracked persistently as instance var
        self._peak_equity: float = getattr(config, 'paper_balance', 1000.0)

        # FIX #73: Per-cycle learning data cache
        self._learning_cache: dict = {}
        self._learning_cache_cycle: int = 0

    # ------------------------------------------------------------------
    # FIX #15 / #80: Shared helpers for JSON field parsing & price parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json_field(value, default=None):
        """Safely parse a field that might be JSON string or already a list/dict."""
        if default is None:
            default = []
        if value is None:
            return default
        if isinstance(value, (list, dict)):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, (list, dict)) else default
            except (json.JSONDecodeError, TypeError):
                return default
        return default

    @staticmethod
    def _parse_price(market) -> float:
        """FIX #33: Shared price parser for outcomePrices (string or list).
        Returns the YES price as a float, defaulting to 0.5."""
        raw = market.get("outcomePrices")
        if raw is None:
            return 0.5
        # Already a list
        if isinstance(raw, list):
            try:
                return float(raw[0]) if raw else 0.5
            except (ValueError, TypeError, IndexError):
                return 0.5
        # String - could be JSON array like '["0.65","0.35"]' or bare number
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list) and parsed:
                    return float(parsed[0])
                return float(parsed)
            except (json.JSONDecodeError, TypeError, ValueError, IndexError):
                try:
                    return float(raw)
                except (ValueError, TypeError):
                    return 0.5
        try:
            return float(raw)
        except (ValueError, TypeError):
            return 0.5

    # ------------------------------------------------------------------
    # Bot creation
    # ------------------------------------------------------------------

    async def create_bots(self):
        """Initialize bots - restore from DB if available, else create fresh."""
        signal_weights = self.config.signals if hasattr(self.config, 'signals') else {}

        # Try to restore from DB first
        saved_configs = await self.db.get_all_bot_configs()
        if saved_configs:
            for cfg in saved_configs:
                strategy = cfg["strategy"]
                cls = STRATEGY_MAP.get(strategy)
                if cls:
                    params = json.loads(cfg["params"]) if isinstance(cfg["params"], str) else cfg["params"]
                    bot = cls(cfg["bot_id"], params, cfg.get("generation", 0))
                    bot.weights = {**bot.DEFAULT_WEIGHTS, **signal_weights}
                    self.bots[cfg["bot_id"]] = bot
                    logger.info(f"Restored bot: {cfg['bot_id']} (gen {cfg.get('generation', 0)})")
            if self.bots:
                logger.info(f"Restored {len(self.bots)} bots from database")
                return

        # Create fresh bots
        for name, cls in STRATEGY_MAP.items():
            if self.config.strategies.get(name, {}).get("enabled", True):
                bot_id = f"{name}-v1"
                bot = cls(bot_id)
                bot.weights = {**bot.DEFAULT_WEIGHTS, **signal_weights}
                self.bots[bot_id] = bot
                await self.db.save_bot_config(bot_id, name, bot.params, 0)
                logger.info(f"Created bot: {bot_id}")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self):
        self._running = True
        await self.create_bots()
        await self.price_feed.start()

        last_trade, last_resolve, last_evolve, last_al = 0, 0, 0, 0
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
            if now - last_al >= self._al_scrape_interval:
                last_al = now
                await self._asklivermore_cycle()
            await asyncio.sleep(1)

    async def stop(self):
        self._running = False
        await self.price_feed.stop()

    # ------------------------------------------------------------------
    # FIX #43: Global signals gathered concurrently with asyncio.gather
    # ------------------------------------------------------------------

    async def _gather_global_signals(self):
        """Gather expensive API signals that don't change per-market."""
        now = time.time()
        if now - self._last_global_update < self._global_update_interval:
            return

        self._last_global_update = now
        signals = {}

        # Build tasks for concurrent fetching
        async def _fetch_coinglass():
            if not self.coinglass:
                return
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

        async def _fetch_polygon():
            if not self.polygon:
                return
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

        async def _fetch_cryptoquant():
            if not self.cryptoquant:
                return
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

        async def _fetch_twitter():
            if not self.twitter:
                return
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
                logger.debug(f"Twitter: sentiment={crypto_sent.get('signal', 0):.3f}, "
                             f"F&G={fear_greed.get('fear_greed_index', 50)}")
            except Exception as e:
                logger.warning(f"Twitter fetch failed: {e}")

        async def _fetch_alphavantage():
            if not self.alphavantage:
                return
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

        async def _fetch_news():
            if not self.sentiment_scorer:
                return
            try:
                news_sent = await self.sentiment_scorer.get_sentiment("bitcoin crypto")
                signals["news_sentiment"] = news_sent
            except Exception as e:
                logger.warning(f"NewsAPI fetch failed: {e}")

        async def _fetch_sportsbook():
            if not self.odds_client:
                return
            try:
                signals["sportsbook_odds"] = await self.odds_client.get_all_sport_odds()
            except Exception as e:
                logger.warning(f"Odds API fetch failed: {e}")

        # FIX #43: Run all global fetches concurrently
        await asyncio.gather(
            _fetch_coinglass(),
            _fetch_polygon(),
            _fetch_cryptoquant(),
            _fetch_twitter(),
            _fetch_alphavantage(),
            _fetch_news(),
            _fetch_sportsbook(),
            return_exceptions=True,
        )

        # --- MEGA COMPOSITE: Combine all global signals ---
        # FIX #25: trend term now contributes its actual 0.10 weight, not 0.01
        trend = signals.get("polygon_trend")
        trend_value = 1.0 if trend == "bullish" else (-1.0 if trend == "bearish" else 0.0)

        mega_composite = (
            signals.get("coinglass_composite", 0) * 0.20 +
            signals.get("polygon_composite", 0) * 0.15 +
            signals.get("onchain_composite", 0) * 0.15 +
            signals.get("twitter_crypto_signal", 0) * 0.10 +
            signals.get("fear_greed_signal", 0) * 0.10 +
            signals.get("av_composite", 0) * 0.15 +
            signals.get("news_sentiment", 0) * 0.05 +
            trend_value * 0.10
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

    # ------------------------------------------------------------------
    # FIX #78: _trade_cycle split into smaller methods
    # ------------------------------------------------------------------

    async def _trade_cycle(self):
        """Discover markets, gather ALL signals, run bots."""
        try:
            # Update global (expensive) signals
            await self._gather_global_signals()

            # FIX #89: Force-refresh AskLivermore market data at start of trade cycle
            if self.asklivermore and self._al_signals:
                try:
                    self._al_signals = await self.al_xref.enrich_signals(self._al_signals)
                except Exception:
                    pass

            # Use MarketDiscovery for categorized, enriched markets
            all_markets = await self.discovery.discover_all()

            # FIX #36/#37: Limit to top 50 markets by liquidity instead of all
            all_markets = self._filter_top_markets(all_markets, limit=50)

            price_snap = self.price_feed.get_snapshot()

            # FIX #41: Query open_trades ONCE outside the market loop
            open_trades = await self.db.get_open_trades()

            # FIX #27: Read max_concurrent_trades from risk dict only
            max_concurrent = self.config.risk.get("max_concurrent_trades", 5)

            # FIX #57: Track already-traded market IDs this cycle
            traded_market_ids_this_cycle: set = set()

            # FIX #73: Invalidate learning cache for new cycle
            self._learning_cache_cycle += 1
            self._learning_cache = {}

            for market in all_markets:
                # FIX #61: Skip if we already placed a bet on this market this cycle
                market_id = market.get("id", "")
                if market_id in traded_market_ids_this_cycle:
                    continue

                # FIX #41: Check capacity using pre-fetched open_trades
                if len(open_trades) + len(traded_market_ids_this_cycle) >= max_concurrent:
                    break  # At capacity, stop processing markets

                # Build rich signal dict combining everything
                signals = await self._build_market_signals(market, price_snap)

                # --- Run each bot ---
                for bot_id, bot in list(self.bots.items()):
                    # FIX #61: Also skip if this market already traded this cycle
                    if market_id in traded_market_ids_this_cycle:
                        break

                    try:
                        trade_placed = await self._run_bot_on_market(
                            bot, bot_id, market, signals, open_trades,
                            traded_market_ids_this_cycle)
                        if trade_placed:
                            traded_market_ids_this_cycle.add(market_id)
                    except Exception as e:
                        logger.error(f"Bot {bot_id} error: {e}")

        except Exception as e:
            logger.error(f"Trade cycle error: {e}")

    def _filter_top_markets(self, markets, limit=50):
        """FIX #36/#37: Return top N markets sorted by volume/liquidity."""
        try:
            return sorted(
                markets,
                key=lambda m: float(m.get("volume", 0) or 0) + float(m.get("liquidity", 0) or 0),
                reverse=True
            )[:limit]
        except (ValueError, TypeError):
            return markets[:limit]

    async def _build_market_signals(self, market, price_snap):
        """Build per-market signal dict from global signals + per-market data."""
        signals = dict(self._global_signals)  # Copy global signals

        # Per-market BTC signals
        signals.update({
            "btc_momentum": price_snap.get("momentum_5m", 0),
            "btc_momentum_5m": price_snap.get("momentum_5m", 0),
            "btc_momentum_15m": price_snap.get("momentum_15m", 0),
            "ema_bullish": price_snap.get("ema_bullish", False),
            "btc_price": price_snap.get("price", 0),
            "volume": float(market.get("volume", 0) or 0),
        })

        # FIX #80 / #15: Use _parse_json_field for token parsing everywhere
        tokens = self._parse_json_field(market.get("clobTokenIds"))

        # Per-market Polymarket momentum
        if self.pm_momentum and tokens:
            try:
                mom = await self.pm_momentum.get_momentum(tokens[0])
                signals["pm_direction"] = mom.get("direction", "unknown")
                signals["pm_momentum"] = mom.get("strength", 0)
                signals["pm_change_5m"] = mom.get("change_5m", 0)
                signals["pm_change_15m"] = mom.get("change_15m", 0)
            except Exception:
                signals.setdefault("pm_direction", "unknown")

        # Per-market orderflow (use Rust engine if available)
        if self.orderflow and tokens:
            try:
                of = await self.orderflow.analyze(tokens[0])
                signals["orderflow_imbalance"] = of.get("imbalance", 0)
                signals["spread"] = of.get("spread", 1)
                signals["volume_pressure"] = of.get("volume_pressure", 0)

                # Use Rust for faster orderbook metrics if available
                if self.rust_orderbook and of.get("best_bid") and of.get("best_ask"):
                    book = await self.pm.clob.get_orderbook(tokens[0])
                    bids = [(float(b["price"]), float(b["size"])) for b in book.get("bids", [])]
                    asks = [(float(a["price"]), float(a["size"])) for a in book.get("asks", [])]
                    self.rust_orderbook.update(bids, asks)
                    signals["spread"] = self.rust_orderbook.spread()
                    signals["orderflow_imbalance"] = self.rust_orderbook.imbalance()
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
                outcomes = self._parse_json_field(market.get("outcomes"))
                if outcomes and len(outcomes) >= 2:
                    team_sent = await self.twitter.get_sports_sentiment(outcomes[0])
                    signals["team1_twitter_signal"] = team_sent.get("signal", 0)
                    signals["team1_injury_mentions"] = team_sent.get("injury_mentions", 0)
            except Exception:
                pass

        # FIX #87: Store the correct market ID (may differ between Gamma/CLOB)
        signals["_market_id"] = market.get("id", "")
        signals["_condition_id"] = market.get("conditionId", market.get("id", ""))

        return signals

    async def _run_bot_on_market(self, bot, bot_id, market, signals,
                                 open_trades, traded_market_ids_this_cycle):
        """Run a single bot against a single market. Returns True if a trade was placed."""
        # FIX #46: Circuit breaker reads thresholds from config.risk
        cb_losses = self.config.risk.get("circuit_breaker_losses", 4)
        cb_suspend_secs = self.config.risk.get("circuit_breaker_suspend_seconds", 300)

        if bot_id in self._suspended_until:
            if time.time() < self._suspended_until[bot_id]:
                return False
            del self._suspended_until[bot_id]
            self._consecutive_losses[bot_id] = 0

        # FIX #73: Load learning data from cache (per-cycle)
        if bot_id not in self._learning_cache:
            self._learning_cache[bot_id] = await self.db.get_learning_data(bot_id)
        bot.learning_data = self._learning_cache[bot_id]
        bot.total_observations = sum(
            d.get("obs", 0) for d in bot.learning_data.values())

        # Regime filter (PDF: block mean-reversion in trending)
        if (bot.strategy_name == "mean_reversion"
                and signals.get("regime") == "trending"):
            return False

        # Inject mega composite
        signals["mega_signal"] = signals.get("mega_composite", 0)

        decision = bot.make_decision(market, signals)
        if decision["action"] == "hold":
            return False

        # FIX #51: Soccer combo actions - log with structured data, don't silently discard
        if decision["action"] in ("buy_combo", "sell_combo", "flip_team"):
            logger.info(
                "Soccer 3-way signal",
                extra={
                    "action": decision["action"],
                    "bot_id": bot_id,
                    "market_id": market.get("id", ""),
                    "question": market.get("question", "")[:100],
                    "confidence": decision.get("confidence", 0),
                    "reasoning": decision.get("reasoning", ""),
                    "outcomes": self._parse_json_field(market.get("outcomes")),
                    "prices": self._parse_json_field(market.get("outcomePrices")),
                }
            )
            if self.config.telegram.get("notify_on_buy"):
                await self.telegram.send(
                    f"<b>SOCCER 3-WAY</b>\n"
                    f"Action: {decision['action']}\n"
                    f"Market: {market.get('question', '')[:80]}\n"
                    f"Confidence: {decision.get('confidence', 0):.2f}\n"
                    f"{decision.get('reasoning', '')[:100]}")
            return False  # TODO: implement combo execution for live CLOB

        # Verify trade (pass orderflow signals for spread check)
        orderflow_data = {
            "spread": signals.get("spread"),
            "imbalance": signals.get("orderflow_imbalance", 0)
        } if signals.get("spread") is not None else None

        verification = await self.verifier.verify(
            decision, market, signals, orderflow=orderflow_data)
        if not verification["passed"]:
            if self.config.telegram.get("notify_on_verification_fail"):
                await self.telegram.notify_verification_fail(
                    bot_id, market.get("question", "?"),
                    verification["reasons"])
            return False

        # FIX #71: Balance floor check using persistent peak equity
        balance = self.config.paper_balance
        total_pnl = await self.db.get_total_pnl()
        current_equity = balance + total_pnl
        self._peak_equity = max(self._peak_equity, current_equity)
        floor_pct = self.config.risk.get("balance_floor_pct", 0.70)
        if current_equity < self._peak_equity * floor_pct:
            logger.warning(f"Balance floor hit: {current_equity:.2f} < "
                           f"{floor_pct:.0%} of peak {self._peak_equity:.2f}")
            return False

        # Size with quarter-Kelly, max bet % cap
        max_bet_pct = self.config.risk.get("max_bet_pct", 0.05)
        amount = bot.bet_size(
            decision["confidence"], current_equity,
            min(self.config.risk.get("max_position_size", 50),
                current_equity * max_bet_pct))

        if amount <= 0:
            return False  # Confidence too low for any bet

        if not self.pos_mgr.check_daily_limit():
            logger.warning("Daily loss cap hit")
            return False

        # FIX #87: Use the correct market_id for the API
        market_id = market.get("id", "")

        # Place trade
        if self.config.mode == "paper" and self.simmer:
            result = await self.simmer.place_bet(
                market_id, decision["outcome"], amount)
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

        # FIX #33: Use shared _parse_price helper
        entry_price = self._parse_price(market)

        trade_id = await self.db.insert_trade(
            bot_id, market_id, market.get("market_type", "unknown"),
            decision["action"], decision["outcome"], amount,
            entry_price,
            decision["confidence"], enriched_snapshot, verification)

        if self.config.telegram.get("notify_on_buy"):
            await self.telegram.notify_buy(
                bot_id, market.get("question", "?"),
                decision["outcome"], amount,
                enriched_snapshot.get("yes_price", 0),
                decision["confidence"], decision["reasoning"])

        logger.info(f"TRADE: {bot_id} {decision['outcome']} ${amount:.2f} "
                    f"conf={decision['confidence']:.2f} on {market.get('question', '?')[:50]}")

        return True

    # ------------------------------------------------------------------
    # Resolution cycle
    # ------------------------------------------------------------------

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

                    # FIX #23: Case-insensitive comparison for resolution
                    resolved_outcome = (market.get("outcome", "") or "").lower()
                    trade_outcome = (trade["outcome"] or "").lower()
                    won = (trade_outcome == "yes" and resolved_outcome == "yes") or \
                          (trade_outcome == "no" and resolved_outcome == "no")
                    pnl = (trade["amount"] * (1 / trade["price"] - 1)) if won else -trade["amount"]

                    await self.db.resolve_trade(trade["id"], pnl)
                    await self.db.update_bot_stats(trade["bot_id"], won, pnl)
                    self.pos_mgr.record_trade_result(pnl)

                    # FIX #71: Update peak equity after resolution
                    total_pnl = await self.db.get_total_pnl()
                    current_equity = self.config.paper_balance + total_pnl
                    self._peak_equity = max(self._peak_equity, current_equity)

                    # Feed online learning
                    if self.learner:
                        await self.learner.record_outcome(trade, won, pnl)

                    # FIX #46: Circuit breaker reads from config.risk
                    cb_losses = self.config.risk.get("circuit_breaker_losses", 4)
                    cb_suspend_secs = self.config.risk.get("circuit_breaker_suspend_seconds", 300)

                    if not won:
                        self._consecutive_losses[trade["bot_id"]] = \
                            self._consecutive_losses.get(trade["bot_id"], 0) + 1
                        if self._consecutive_losses[trade["bot_id"]] >= cb_losses:
                            self._suspended_until[trade["bot_id"]] = time.time() + cb_suspend_secs
                            logger.warning(
                                f"Circuit breaker: {trade['bot_id']} suspended "
                                f"for {cb_suspend_secs}s after {cb_losses} consecutive losses")
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

    # ------------------------------------------------------------------
    # Evolution cycle
    # ------------------------------------------------------------------

    async def _evolution_cycle(self):
        """Kill underperformers, mutate survivors."""
        try:
            stats = await self.db.get_all_bot_stats()
            min_trades = self.config.evolution.get("min_trades_for_eval", 20)
            # FIX #69: Lower survival WR from 0.65 to 0.55
            survival_wr = self.config.evolution.get("survival_win_rate", 0.55)

            eligible = [s for s in stats if s["total_trades"] >= min_trades]
            if not eligible:
                return

            survivors = [s for s in eligible if s["win_rate"] >= survival_wr]
            losers = [s for s in eligible if s["win_rate"] < survival_wr]

            if not survivors:
                survivors = [max(eligible, key=lambda x: x["win_rate"])]
                losers = [s for s in eligible if s["bot_id"] != survivors[0]["bot_id"]]

            # FIX #95: Preserve at least one bot of each active strategy type
            strategy_coverage: dict = {}  # strategy_name -> best bot stats
            for s in eligible:
                bot = self.bots.get(s["bot_id"])
                if bot:
                    sname = bot.strategy_name
                    if sname not in strategy_coverage or s["win_rate"] > strategy_coverage[sname]["win_rate"]:
                        strategy_coverage[sname] = s

            # Protect the best bot of each strategy from being killed
            protected_ids = {s["bot_id"] for s in strategy_coverage.values()}
            losers = [s for s in losers if s["bot_id"] not in protected_ids]

            for loser in losers:
                parent = random.choice(survivors)
                parent_bot = self.bots.get(parent["bot_id"])
                if not parent_bot:
                    continue

                # FIX #29: Copy parent_id BEFORE mutation loop modifies parent_bot
                parent_id = parent_bot.bot_id
                parent_strategy = parent_bot.strategy_name
                parent_gen = parent_bot.generation

                new_params = parent_bot.mutate()
                gen = parent_gen + 1
                new_id = f"{parent_strategy}-g{gen}-{int(time.time()) % 10000}"
                cls = STRATEGY_MAP.get(parent_strategy)
                if cls:
                    # Remove old bot from memory and DB
                    self.bots.pop(loser["bot_id"], None)
                    await self.db.delete_bot_config(loser["bot_id"])

                    # Create and persist new bot
                    new_bot = cls(new_id, new_params, gen)
                    signal_weights = self.config.signals if hasattr(self.config, 'signals') else {}
                    new_bot.weights = {**new_bot.DEFAULT_WEIGHTS, **signal_weights}
                    self.bots[new_id] = new_bot
                    await self.db.save_bot_config(
                        new_id, parent_strategy, new_params, gen, parent_id)

                    await self.telegram.notify_evolution(
                        loser["bot_id"], new_id,
                        f"WR {loser['win_rate']:.1%} < {survival_wr:.0%}")
                    logger.info(f"Evolution: {loser['bot_id']} -> {new_id} (parent={parent_id})")

            # Save daily stats
            import datetime
            today = datetime.date.today().isoformat()
            best = max(eligible, key=lambda x: x["win_rate"]) if eligible else None
            summary = await self.db.get_daily_summary()
            await self.db.save_daily_stats(
                today, summary.get("trades", 0), summary.get("total_pnl", 0),
                best["bot_id"] if best else "", best["win_rate"] if best else 0)

        except Exception as e:
            logger.error(f"Evolution error: {e}")

    # ------------------------------------------------------------------
    # AskLivermore cycle
    # ------------------------------------------------------------------

    async def _asklivermore_cycle(self):
        """Scrape AskLivermore A+ signals and cross-reference with Polymarket."""
        if not self.asklivermore:
            return
        try:
            logger.info("Scraping AskLivermore signals...")
            if not await self.asklivermore.login():
                logger.warning("AskLivermore login failed")
                return

            signals = await self.asklivermore.get_a_plus_signals()
            if not signals:
                logger.info("No A+ signals found")
                return

            # Persist to DB
            await self.db.save_asklivermore_signals(signals)

            # Enrich with live market data
            enriched = await self.al_xref.enrich_signals(signals)

            # Find Polymarket correlations
            pm_markets = await self.discovery.discover_all()
            correlations = self.al_xref.find_polymarket_correlations(enriched, pm_markets)

            # Store correlations as global signals
            if correlations:
                bullish_sectors = sum(1 for c in correlations if c["stock_trend"] == "bullish")
                bearish_sectors = sum(1 for c in correlations if c["stock_trend"] == "bearish")
                self._global_signals["al_bullish_sectors"] = bullish_sectors
                self._global_signals["al_bearish_sectors"] = bearish_sectors
                self._global_signals["al_sector_signal"] = (
                    0.2 if bullish_sectors > bearish_sectors else
                    -0.2 if bearish_sectors > bullish_sectors else 0)

            self._al_signals = enriched

            # Telegram notification
            await self.telegram.notify_asklivermore(signals)
            logger.info(f"AskLivermore: {len(signals)} A+ signals, {len(correlations)} PM correlations")

        except Exception as e:
            logger.error(f"AskLivermore cycle error: {e}")
