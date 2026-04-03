"""TradingTest - Unified Polymarket Trading Bot with full data integration.

Usage:
    python -m src.main              # Paper trading + dashboard
    python -m src.main --live       # Live trading mode
    python -m src.main --dashboard  # Dashboard only
    python -m src.main --bot-only   # Bot only, no dashboard
"""
import argparse
import asyncio
import logging
import sys

from .config import Config
from .db.database import Database
from .api.polymarket import PolymarketClient
from .api.simmer import SimmerClient
from .api.odds_api import OddsAPIClient
from .api.coinglass import CoinglassClient
from .api.polygonio import PolygonClient
from .api.cryptoquant import CryptoQuantClient
from .api.twitter_sentiment import TwitterSentiment
from .api.alphavantage import AlphaVantageClient
from .signals.price_feed import PriceFeed
from .signals.sentiment import SentimentScorer
from .signals.orderflow import OrderflowSignal
from .signals.sportsbook_edge import SportsbookEdge
from .signals.polymarket_momentum import PolymarketMomentum
from .verification.verifier import TradeVerifier
from .risk.position_manager import PositionManager
from .notifications.telegram import TelegramNotifier
from .ml.xgboost_model import OpportunityScorer
from .ml.online_learning import OnlineLearner
from .arena.arena import Arena

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("trading.log")])

logger = logging.getLogger(__name__)


async def run_bot(config: Config):
    """Run the trading arena with all data sources."""
    db = Database()
    await db.connect()

    price_feed = PriceFeed()
    scorer = OpportunityScorer()
    scorer.load()

    telegram = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)
    verifier = TradeVerifier(config.verification, db)
    pos_mgr = PositionManager(config.risk)
    sentiment = SentimentScorer(config.newsapi_key) if config.newsapi_key else None

    async with PolymarketClient() as pm_client:
        # Paper trading client
        simmer = None
        if config.mode == "paper" and config.simmer_api_key:
            simmer = SimmerClient(config.simmer_api_key)
            await simmer.__aenter__()

        # Odds API
        odds_client = None
        if config.odds_api_key:
            odds_client = OddsAPIClient(config.odds_api_key)
            await odds_client.__aenter__()
            logger.info("Odds API connected")

        # --- 5 NEW ENHANCED API CLIENTS ---

        # 1. Coinglass - derivatives data
        coinglass = None
        if config.coinglass_api_key:
            coinglass = CoinglassClient(config.coinglass_api_key)
            await coinglass.__aenter__()
            logger.info("Coinglass connected (funding, liquidations, OI, L/S ratio)")

        # 2. Polygon.io - technicals + enrichment
        polygon = None
        if config.polygonio_api_key:
            polygon = PolygonClient(config.polygonio_api_key)
            await polygon.__aenter__()
            logger.info("Polygon.io connected (RSI, MACD, VWAP, Bollinger)")

        # 3. CryptoQuant - on-chain analytics
        cryptoquant = None
        cq_key = getattr(config, 'cryptoquant_api_key', '') or ''
        if cq_key:
            cryptoquant = CryptoQuantClient(cq_key)
            await cryptoquant.__aenter__()
            logger.info("CryptoQuant connected (exchange flows, whales, miners)")

        # 4. Twitter/X - social sentiment
        twitter = None
        tw_key = getattr(config, 'twitter_bearer_token', '') or ''
        if tw_key:
            twitter = TwitterSentiment(tw_key)
            await twitter.__aenter__()
            logger.info("Twitter connected (crypto + sports sentiment, fear/greed)")

        # 5. Alpha Vantage - additional technicals
        alphavantage = None
        av_key = getattr(config, 'alphavantage_key', '') or ''
        if av_key:
            alphavantage = AlphaVantageClient(av_key)
            await alphavantage.__aenter__()
            logger.info("Alpha Vantage connected (RSI, MACD, BBands, Stochastic)")

        # Signal processors
        orderflow = OrderflowSignal(pm_client.clob) if pm_client.clob else None
        pm_momentum = PolymarketMomentum(pm_client.clob) if pm_client.clob else None
        sb_edge = SportsbookEdge(
            delta_cap=config.sportsbook.get("delta_cap", 0.03),
            min_edge=config.sportsbook.get("min_edge", 0.02),
            min_liquidity=config.sportsbook.get("min_liquidity", 1000))

        learner = OnlineLearner(scorer, db)

        # Create arena with ALL data sources
        arena = Arena(
            config, db, pm_client, simmer, verifier, pos_mgr, telegram,
            price_feed, scorer, learner,
            coinglass=coinglass, polygon=polygon, cryptoquant=cryptoquant,
            twitter=twitter, alphavantage=alphavantage, odds_client=odds_client,
            sentiment_scorer=sentiment, orderflow_signal=orderflow,
            sportsbook_edge=sb_edge, pm_momentum=pm_momentum)

        # Count active data sources
        sources = sum(bool(x) for x in [coinglass, polygon, cryptoquant, twitter,
                                          alphavantage, odds_client, sentiment,
                                          orderflow, pm_momentum])
        logger.info(f"Starting TradingTest in {config.mode} mode with {sources} data sources")
        await telegram.send(
            f"🚀 <b>TradingTest started</b>\n"
            f"Mode: {config.mode}\n"
            f"Data sources: {sources} active\n"
            f"Coinglass: {'✅' if coinglass else '❌'}\n"
            f"Polygon.io: {'✅' if polygon else '❌'}\n"
            f"CryptoQuant: {'✅' if cryptoquant else '❌'}\n"
            f"Twitter: {'✅' if twitter else '❌'}\n"
            f"Alpha Vantage: {'✅' if alphavantage else '❌'}\n"
            f"Odds API: {'✅' if odds_client else '❌'}\n"
            f"NewsAPI: {'✅' if sentiment else '❌'}")

        try:
            await arena.run()
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Shutting down...")
        finally:
            try:
                await arena.stop()
            except Exception:
                pass

            # Cleanup all clients
            for client in [simmer, odds_client, coinglass, polygon,
                           cryptoquant, twitter, alphavantage]:
                if client:
                    try:
                        await client.__aexit__(None, None, None)
                    except Exception:
                        pass

            try:
                summary = await db.get_daily_summary()
                await telegram.notify_daily_summary(summary)
                await telegram.close()
            except Exception:
                pass
            await db.close()


async def run_dashboard(config: Config):
    import uvicorn
    from .dashboard_app import create_app
    app = create_app(config)
    uv_config = uvicorn.Config(
        app, host=config.dashboard.get("host", "0.0.0.0"),
        port=config.dashboard.get("port", 8080), log_level="info")
    server = uvicorn.Server(uv_config)
    await server.serve()


async def run_all(config: Config):
    await asyncio.gather(run_bot(config), run_dashboard(config))


def main():
    parser = argparse.ArgumentParser(description="TradingTest - Unified Polymarket Bot")
    parser.add_argument("--live", action="store_true", help="Run in live mode")
    parser.add_argument("--dashboard", action="store_true", help="Dashboard only")
    parser.add_argument("--bot-only", action="store_true", help="Bot only, no dashboard")
    args = parser.parse_args()

    config = Config.load()
    if args.live:
        config.mode = "live"

    try:
        if args.dashboard:
            asyncio.run(run_dashboard(config))
        elif args.bot_only:
            asyncio.run(run_bot(config))
        else:
            asyncio.run(run_all(config))
    except KeyboardInterrupt:
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()
