#!/bin/bash
# TradingTest - Deployment script for AWS EC2 (Ubuntu)
#
# Usage:
#   #81: Do NOT pipe curl to bash. Instead, clone first then run:
#     git clone https://github.com/pablowilliams/TradingTest.git
#     cd TradingTest
#     bash deploy.sh
#
# Or if already cloned:
#     cd /home/ubuntu/TradingTest && bash deploy.sh

set -e

echo "=== TradingTest Deployment ==="

# #82: Warn about secret exposure
echo ""
echo "WARNING: Never pass secrets via command-line arguments or environment"
echo "         variables in shared shells. Use .env file for API keys."
echo ""

# 1. System deps
sudo apt update && sudo apt install -y python3-pip python3-venv nginx curl git

# 2. Clone or update repo
# #81: Don't pipe curl to bash - assume user has already cloned
# #85: Handle git pull conflicts gracefully
cd /home/ubuntu
if [ -d "TradingTest" ]; then
    cd TradingTest
    echo "Updating existing repo..."
    if ! git pull --ff-only 2>/dev/null; then
        echo "WARNING: git pull failed (possible local changes or conflicts)."
        echo "Stashing local changes and retrying..."
        git stash
        if ! git pull --ff-only 2>/dev/null; then
            echo "ERROR: Cannot fast-forward. Please resolve conflicts manually:"
            echo "  cd /home/ubuntu/TradingTest"
            echo "  git status"
            echo "  git merge origin/main  (or git rebase)"
            echo "Continuing deployment with current code..."
        fi
    fi
else
    git clone https://github.com/pablowilliams/TradingTest.git
    cd TradingTest
fi

# 3. Python env
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 4. Prompt for .env if not exists
if [ ! -f .env ]; then
    echo ""
    echo "=== No .env file found. Creating from template... ==="
    cp .env.example .env
    echo "IMPORTANT: Edit /home/ubuntu/TradingTest/.env with your API keys!"
    echo "Run: nano /home/ubuntu/TradingTest/.env"
    echo ""
fi

# #82: Check for default/missing secrets
if grep -q "change-me" .env 2>/dev/null || grep -q "tradingtest" .env 2>/dev/null; then
    echo ""
    echo "!!! SECURITY WARNING !!!"
    echo "Your .env file appears to contain default/placeholder values."
    echo "Please update all API keys and passwords before running in production."
    echo ""
fi

# 5. Systemd service
sudo tee /etc/systemd/system/tradingtest.service > /dev/null << 'EOF'
[Unit]
Description=TradingTest Trading Bot + Dashboard
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/TradingTest
ExecStart=/home/ubuntu/TradingTest/venv/bin/python -m src.main
Restart=always
RestartSec=10
Environment=PATH=/home/ubuntu/TradingTest/venv/bin:/usr/bin
EnvironmentFile=/home/ubuntu/TradingTest/.env

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable tradingtest

# 6. Nginx reverse proxy
sudo tee /etc/nginx/sites-available/tradingtest > /dev/null << 'EOF'
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300;
        proxy_connect_timeout 300;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/tradingtest /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl restart nginx

# #84: Add DB backup cron (daily at 3 AM)
echo "Setting up daily DB backup cron..."
BACKUP_DIR="/home/ubuntu/TradingTest/backups"
mkdir -p "$BACKUP_DIR"
CRON_JOB="0 3 * * * cp /home/ubuntu/TradingTest/trading.db ${BACKUP_DIR}/trading_\$(date +\%Y\%m\%d).db && find ${BACKUP_DIR} -name 'trading_*.db' -mtime +7 -delete"
(crontab -l 2>/dev/null | grep -v "trading.db" ; echo "$CRON_JOB") | crontab -

# 7. Start
sudo systemctl start tradingtest

echo ""
echo "=== Deployment complete! ==="
echo "Dashboard: http://$(curl -s ifconfig.me)/login"
echo "Default login: admin / tradingtest"
echo ""
echo "Next steps:"
echo "  1. Edit .env: nano /home/ubuntu/TradingTest/.env"
echo "  2. Set DASHBOARD_PASSWORD: (add to .env file, NOT export)"
echo "  3. Restart: sudo systemctl restart tradingtest"
echo "  4. View logs: journalctl -u tradingtest -f"
echo ""

# #8: Optional HTTPS/certbot setup
echo "=== Optional: Set up HTTPS with Let's Encrypt ==="
echo "To enable HTTPS, run the following after setting your domain's DNS:"
echo ""
echo "  sudo apt install -y certbot python3-certbot-nginx"
echo "  sudo certbot --nginx -d your-domain.com"
echo "  sudo systemctl restart nginx"
echo ""
echo "Certbot will auto-renew via systemd timer."
