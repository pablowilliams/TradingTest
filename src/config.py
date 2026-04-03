"""Unified configuration system."""
import json
import logging
import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config.json"


@dataclass
class Config:
    # Mode
    mode: str = "paper"
    paper_balance: float = 1000.0
    live_trade_limit: float = 10.0
    daily_loss_cap: float = 100.0
    max_concurrent_trades: int = 5
    poll_interval_seconds: int = 15
    resolution_check_seconds: int = 60
    evolution_interval_seconds: int = 7200

    # Market types
    market_types: dict = field(default_factory=lambda: {
        "sports": True, "crypto_5min": True, "politics": True, "all_events": True
    })

    # Strategy weights
    strategies: dict = field(default_factory=dict)
    signals: dict = field(default_factory=dict)
    risk: dict = field(default_factory=dict)
    verification: dict = field(default_factory=dict)
    evolution: dict = field(default_factory=dict)
    telegram: dict = field(default_factory=dict)
    dashboard: dict = field(default_factory=dict)
    # #49: sportsbook.min_edge should be present in config;
    # the sportsbook section in config.json includes min_edge, delta_cap, etc.
    sportsbook: dict = field(default_factory=dict)
    soccer: dict = field(default_factory=dict)
    circuit_breaker: dict = field(default_factory=dict)

    # API keys from env
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_private_key: str = ""
    # #45: Add polymarket_api_passphrase field
    polymarket_api_passphrase: str = ""
    simmer_api_key: str = ""
    odds_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    asklivermore_email: str = ""
    asklivermore_password: str = ""
    newsapi_key: str = ""
    coinglass_api_key: str = ""
    polygonio_api_key: str = ""
    cryptoquant_api_key: str = ""
    twitter_bearer_token: str = ""
    alphavantage_key: str = ""

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        path = path or CONFIG_PATH

        # #12: Wrap config.json loading in try/except with helpful error message
        try:
            with open(path) as f:
                data = json.load(f)
        except FileNotFoundError:
            logger.error(
                f"Config file not found at {path}. "
                f"Copy config.json.example to config.json and edit it."
            )
            print(
                f"ERROR: Config file not found at {path}.\n"
                f"  Copy config.json.example to config.json and edit it.",
                file=sys.stderr,
            )
            data = {}
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in config file {path}: {e}")
            print(
                f"ERROR: Invalid JSON in config file {path}: {e}\n"
                f"  Fix the JSON syntax and try again.",
                file=sys.stderr,
            )
            data = {}

        config = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

        # Load ALL API keys from environment
        config.polymarket_api_key = os.getenv("POLYMARKET_API_KEY", "")
        config.polymarket_api_secret = os.getenv("POLYMARKET_API_SECRET", "")
        config.polymarket_private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
        # #45: Read polymarket_api_passphrase from env
        config.polymarket_api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE", "")
        config.simmer_api_key = os.getenv("SIMMER_API_KEY", "")
        config.odds_api_key = os.getenv("ODDS_API_KEY", "")
        config.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        config.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        config.asklivermore_email = os.getenv("ASKLIVERMORE_EMAIL", "")
        config.asklivermore_password = os.getenv("ASKLIVERMORE_PASSWORD", "")
        config.newsapi_key = os.getenv("NEWSAPI_KEY", "")
        config.coinglass_api_key = os.getenv("COINGLASS_API_KEY", "")
        config.polygonio_api_key = os.getenv("POLYGONIO_API_KEY", "")
        config.cryptoquant_api_key = os.getenv("CRYPTOQUANT_API_KEY", "")
        config.twitter_bearer_token = os.getenv("TWITTER_BEARER_TOKEN", "")
        config.alphavantage_key = os.getenv("ALPHAVANTAGE_KEY", "")

        return config

    @property
    def is_live(self) -> bool:
        return self.mode == "live"
