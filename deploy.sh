#!/bin/bash
# TradingTest - One-click deployment script for AWS EC2 (Ubuntu)
# Usage: ssh into your EC2 instance, then run:
#   curl -sL https://raw.githubusercontent.com/pablowilliams/TradingTest/main/deploy.sh | bash

set -e

echo "=== TradingTest Deployment ==="

# 1. System deps
sudo apt update && sudo apt install -y python3-pip python3-venv nginx curl git

# 2. Clone repo
cd /home/ubuntu
if [ -d "TradingTest" ]; then
    cd TradingTest && git pull
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

# 7. Start
sudo systemctl start tradingtest

echo ""
echo "=== Deployment complete! ==="
echo "Dashboard: http://$(curl -s ifconfig.me)/login"
echo "Default login: admin / tradingtest"
echo ""
echo "Next steps:"
echo "  1. Edit .env: nano /home/ubuntu/TradingTest/.env"
echo "  2. Set DASHBOARD_PASSWORD: export DASHBOARD_PASSWORD=your-secure-password"
echo "  3. Restart: sudo systemctl restart tradingtest"
echo "  4. View logs: journalctl -u tradingtest -f"
