"""TradingTest - Unified Polymarket Trading Bot.

Usage:
    python -m src.main              # Run in paper trading mode
    python -m src.main --live       # Run in live mode (real USDC)
    python -m src.main --dashboard  # Run dashboard only
"""
import argparse
import asyncio
import logging
import sys

from .config import Config
from .db.database import Database
from .api.polymarket import PolymarketClient
from .api.simmer import SimmerClient
from .signals.price_feed import PriceFeed
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
    """Run the trading arena."""
    db = Database()
    await db.connect()

    price_feed = PriceFeed()
    scorer = OpportunityScorer()
    scorer.load()

    telegram = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)

    verifier = TradeVerifier(config.verification, db)
    pos_mgr = PositionManager(config.risk)

    async with PolymarketClient() as pm_client:
        simmer = None
        if config.mode == "paper" and config.simmer_api_key:
            simmer = SimmerClient(config.simmer_api_key)
            await simmer.__aenter__()

        learner = OnlineLearner(scorer, db)

        arena = Arena(config, db, pm_client, simmer, verifier,
                      pos_mgr, telegram, price_feed, scorer, learner)

        logger.info(f"Starting TradingTest in {config.mode} mode...")
        await telegram.send(f"🚀 <b>TradingTest started</b> in {config.mode} mode")

        try:
            await arena.run()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            await arena.stop()
            if simmer:
                await simmer.__aexit__(None, None, None)
            summary = await db.get_daily_summary()
            await telegram.notify_daily_summary(summary)
            await db.close()


async def run_dashboard(config: Config):
    """Run the web dashboard."""
    import uvicorn
    from .dashboard_app import create_app

    app = create_app(config)
    uv_config = uvicorn.Config(
        app, host=config.dashboard.get("host", "0.0.0.0"),
        port=config.dashboard.get("port", 8080), log_level="info")
    server = uvicorn.Server(uv_config)
    await server.serve()


async def run_all(config: Config):
    """Run both bot and dashboard concurrently."""
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

    if args.dashboard:
        asyncio.run(run_dashboard(config))
    elif args.bot_only:
        asyncio.run(run_bot(config))
    else:
        asyncio.run(run_all(config))


if __name__ == "__main__":
    main()
