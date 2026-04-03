# TradingTest - Unified Polymarket Trading System

Multi-strategy automated trading bot for Polymarket prediction markets (sports, crypto, politics). Combines sportsbook edge detection, Bayesian learning, evolutionary bot selection, and ML scoring into one system.

## Architecture

**Python + Rust hybrid** — Python for strategy/ML/orchestration, Rust (PyO3) for high-speed orderbook processing and signal combination.

### Strategies
| Strategy | Source | Description |
|----------|--------|-------------|
| **Momentum** | bot-arena | Follows BTC and market price trends, EMA crossovers |
| **Mean Reversion** | bot-arena | Bets against sharp moves, RSI-like overbought/oversold |
| **Sports Edge** | CrewSX | Exploits sportsbook vs Polymarket price gaps with XGBoost |
| **Soccer 3-Way** | crellios | Buys undervalued Favorite+Draw combos in soccer markets |
| **Sniper** | bot-arena | Pure rule-based, only trades in optimal price zones |
| **Hybrid** | Ensemble | Requires 3+ strategies to agree before trading |

### Key Features
- **Pre-trade verification**: Every trade passes 7 checks before execution
- **Bayesian learning**: Bots learn from outcomes across 5 feature dimensions
- **Evolutionary selection**: Underperforming bots get killed and replaced every 2 hours
- **Telegram alerts**: Real-time notifications for buys, sells, blocks, and daily summaries
- **AskLivermore integration**: A+ stock signals for cross-market intelligence
- **24/7 web dashboard**: Real-time P&L, bot leaderboard, trade history

## Quick Start

```bash
# 1. Clone
git clone https://github.com/pablowilliams/TradingTest.git
cd TradingTest

# 2. Install Python deps
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env with your API keys

# 4. Run (paper trading)
python -m src.main

# 5. Dashboard only
python -m src.main --dashboard

# 6. Bot only (no dashboard)
python -m src.main --bot-only
```

### Optional: Build Rust Engine
```bash
cd rust_engine
pip install maturin
maturin develop --release
```

## API Keys Required

| Key | Required | Source |
|-----|----------|--------|
| SIMMER_API_KEY | For paper trading | simmer.trade |
| ODDS_API_KEY | For sports edge | the-odds-api.com |
| TELEGRAM_BOT_TOKEN | For notifications | @BotFather on Telegram |
| TELEGRAM_CHAT_ID | For notifications | Your chat ID |
| NEWSAPI_KEY | For sentiment | newsapi.org |
| ASKLIVERMORE_EMAIL | For stock signals | asklivermore.com |
| POLYMARKET_PRIVATE_KEY | For live trading | Your wallet |

### Recommended Additional APIs
| Key | Purpose |
|-----|---------|
| COINGLASS_API_KEY | Funding rates, liquidations, open interest |
| POLYGONIO_API_KEY | Stock/crypto data enrichment |
| TWITTER_BEARER_TOKEN | Social sentiment tracking |
| CRYPTOQUANT_API_KEY | On-chain metrics (whale movements) |
| ALPHAVANTAGE_KEY | Technical indicators + fundamental data |

## Deploy 24/7 (AWS EC2)

```bash
# 1. Launch Ubuntu EC2 (t3.small minimum)
# 2. SSH in and setup
sudo apt update && sudo apt install -y python3-pip python3-venv nginx

# 3. Clone and install
git clone https://github.com/pablowilliams/TradingTest.git
cd TradingTest
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 4. Create systemd service
sudo tee /etc/systemd/system/tradingtest.service << 'EOF'
[Unit]
Description=TradingTest Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/TradingTest
ExecStart=/home/ubuntu/TradingTest/venv/bin/python -m src.main
Restart=always
RestartSec=10
Environment=PATH=/home/ubuntu/TradingTest/venv/bin:/usr/bin

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable tradingtest
sudo systemctl start tradingtest

# 5. Nginx reverse proxy (so dashboard is on port 80)
sudo tee /etc/nginx/sites-available/tradingtest << 'EOF'
server {
    listen 80;
    server_name _;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
EOF
sudo ln -sf /etc/nginx/sites-available/tradingtest /etc/nginx/sites-enabled/default
sudo systemctl restart nginx

# Dashboard now live at http://YOUR_EC2_IP/
```

## Configuration

Edit `config.json` to tune:
- Strategy weights and enables
- Risk limits (daily loss cap, max position size)
- Signal weights
- Verification thresholds
- Evolution parameters

## Disclaimer

This is experimental software. Trading prediction markets involves risk. Past performance does not guarantee future results. Use paper trading mode to test before risking real funds.
