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
from pathlib import Path

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
from .api.asklivermore import AskLivermore
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

# #11: Use absolute path for log file based on project dir
_PROJECT_DIR = Path(__file__).parent.parent
_LOG_FILE = _PROJECT_DIR / "trading.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(str(_LOG_FILE))])

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

    # #13: Wrap client initialization in proper try/except
    # #79: Add proper error handling for client setup
    pm_client = None
    simmer = None
    odds_client = None
    coinglass = None
    polygon = None
    cryptoquant = None
    twitter = None
    alphavantage = None
    asklivermore = None

    try:
        pm_client = PolymarketClient()
        await pm_client.__aenter__()
    except Exception as e:
        logger.error(f"Failed to initialize Polymarket client: {e}")
        await db.close()
        raise

    try:
        # Paper trading client
        if config.mode == "paper" and config.simmer_api_key:
            try:
                simmer = SimmerClient(config.simmer_api_key)
                await simmer.__aenter__()
            except Exception as e:
                logger.warning(f"Failed to initialize Simmer client: {e}")
                simmer = None

        # #53: When mode="live", use Polymarket CLOB for order placement
        if config.mode == "live":
            if not pm_client.clob:
                logger.warning(
                    "Live mode requires Polymarket CLOB client. "
                    "Ensure POLYMARKET_API_KEY, POLYMARKET_API_SECRET, "
                    "POLYMARKET_PRIVATE_KEY, and POLYMARKET_API_PASSPHRASE are set."
                )
            else:
                logger.info("Live mode: Polymarket CLOB client ready for order placement")

        # Odds API
        if config.odds_api_key:
            try:
                odds_client = OddsAPIClient(config.odds_api_key)
                await odds_client.__aenter__()
                logger.info("Odds API connected")
            except Exception as e:
                logger.warning(f"Failed to initialize Odds API client: {e}")
                odds_client = None

        # --- 5 NEW ENHANCED API CLIENTS ---

        # 1. Coinglass - derivatives data
        if config.coinglass_api_key:
            try:
                coinglass = CoinglassClient(config.coinglass_api_key)
                await coinglass.__aenter__()
                logger.info("Coinglass connected (funding, liquidations, OI, L/S ratio)")
            except Exception as e:
                logger.warning(f"Failed to initialize Coinglass client: {e}")
                coinglass = None

        # 2. Polygon.io - technicals + enrichment
        if config.polygonio_api_key:
            try:
                polygon = PolygonClient(config.polygonio_api_key)
                await polygon.__aenter__()
                logger.info("Polygon.io connected (RSI, MACD, VWAP, Bollinger)")
            except Exception as e:
                logger.warning(f"Failed to initialize Polygon client: {e}")
                polygon = None

        # 3. CryptoQuant - on-chain analytics
        cq_key = getattr(config, 'cryptoquant_api_key', '') or ''
        if cq_key:
            try:
                cryptoquant = CryptoQuantClient(cq_key)
                await cryptoquant.__aenter__()
                logger.info("CryptoQuant connected (exchange flows, whales, miners)")
            except Exception as e:
                logger.warning(f"Failed to initialize CryptoQuant client: {e}")
                cryptoquant = None

        # 4. Twitter/X - social sentiment
        tw_key = getattr(config, 'twitter_bearer_token', '') or ''
        if tw_key:
            try:
                twitter = TwitterSentiment(tw_key)
                await twitter.__aenter__()
                logger.info("Twitter connected (crypto + sports sentiment, fear/greed)")
            except Exception as e:
                logger.warning(f"Failed to initialize Twitter client: {e}")
                twitter = None

        # 5. Alpha Vantage - additional technicals
        av_key = getattr(config, 'alphavantage_key', '') or ''
        if av_key:
            try:
                alphavantage = AlphaVantageClient(av_key)
                await alphavantage.__aenter__()
                logger.info("Alpha Vantage connected (RSI, MACD, BBands, Stochastic)")
            except Exception as e:
                logger.warning(f"Failed to initialize Alpha Vantage client: {e}")
                alphavantage = None

        # AskLivermore scraper
        if config.asklivermore_email and config.asklivermore_password:
            try:
                asklivermore = AskLivermore(config.asklivermore_email, config.asklivermore_password)
                await asklivermore.__aenter__()
                logger.info("AskLivermore scraper connected")
            except Exception as e:
                logger.warning(f"Failed to initialize AskLivermore scraper: {e}")
                asklivermore = None

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
            sportsbook_edge=sb_edge, pm_momentum=pm_momentum,
            asklivermore=asklivermore)

        # Count active data sources
        sources = sum(bool(x) for x in [coinglass, polygon, cryptoquant, twitter,
                                          alphavantage, odds_client, sentiment,
                                          orderflow, pm_momentum, asklivermore])
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

    finally:
        # Cleanup all clients
        for client in [simmer, odds_client, coinglass, polygon,
                       cryptoquant, twitter, alphavantage, asklivermore]:
            if client:
                try:
                    await client.__aexit__(None, None, None)
                except Exception:
                    pass

        if pm_client:
            try:
                await pm_client.__aexit__(None, None, None)
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
