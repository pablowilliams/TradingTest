"""FastAPI web dashboard - deployable 24/7."""
import json
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .db.database import Database
from .config import Config


def create_app(config: Config = None) -> FastAPI:
    app = FastAPI(title="TradingTest Dashboard")
    db = Database()

    @app.on_event("startup")
    async def startup():
        await db.connect()

    @app.on_event("shutdown")
    async def shutdown():
        await db.close()

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        stats = await db.get_all_bot_stats()
        recent = await db.get_recent_trades(limit=20)
        summary = await db.get_daily_summary()
        total_pnl = await db.get_total_pnl()

        bot_rows = ""
        for s in stats:
            color = "#4CAF50" if s["win_rate"] >= 0.55 else "#f44336" if s["win_rate"] < 0.50 else "#ff9800"
            bot_rows += f"""<tr>
                <td>{s['bot_id']}</td>
                <td>{s['total_trades']}</td>
                <td>{s['wins']}</td>
                <td style="color:{color}">{s['win_rate']:.1%}</td>
                <td style="color:{'#4CAF50' if s['total_pnl']>=0 else '#f44336'}">${s['total_pnl']:+.2f}</td>
            </tr>"""

        trade_rows = ""
        for t in recent:
            pnl_color = "#4CAF50" if t.get("pnl", 0) >= 0 else "#f44336"
            trade_rows += f"""<tr>
                <td>{t['bot_id']}</td>
                <td>{t.get('market_type','?')}</td>
                <td>{t['outcome']}</td>
                <td>${t['amount']:.2f}</td>
                <td>{t['confidence']:.1%}</td>
                <td style="color:{pnl_color}">${t.get('pnl',0):+.2f}</td>
                <td>{t['status']}</td>
            </tr>"""

        daily_pnl = summary.get("total_pnl", 0) or 0
        daily_trades = summary.get("trades", 0) or 0
        daily_wins = summary.get("wins", 0) or 0
        daily_wr = daily_wins / daily_trades if daily_trades > 0 else 0

        return f"""<!DOCTYPE html>
<html><head><title>TradingTest Dashboard</title>
<meta http-equiv="refresh" content="10">
<style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #1a1a2e; color: #eee; margin: 0; padding: 20px; }}
    h1 {{ color: #00d4ff; }} h2 {{ color: #7c83ff; margin-top: 30px; }}
    .stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; margin: 20px 0; }}
    .stat-card {{ background: #16213e; padding: 20px; border-radius: 10px; text-align: center; }}
    .stat-value {{ font-size: 2em; font-weight: bold; }}
    .positive {{ color: #4CAF50; }} .negative {{ color: #f44336; }}
    table {{ width: 100%; border-collapse: collapse; margin: 15px 0; }}
    th, td {{ padding: 10px; text-align: left; border-bottom: 1px solid #333; }}
    th {{ background: #16213e; color: #7c83ff; }}
    tr:hover {{ background: #16213e44; }}
    .mode {{ display: inline-block; padding: 4px 12px; border-radius: 12px; font-size: 0.8em;
             background: {'#4CAF50' if (config and config.mode == 'paper') else '#f44336'}; }}
</style></head>
<body>
    <h1>TradingTest <span class="mode">{'PAPER' if (config and config.mode == 'paper') else 'LIVE'}</span></h1>
    <div class="stats">
        <div class="stat-card"><div>Total P&L</div><div class="stat-value {'positive' if total_pnl>=0 else 'negative'}">${total_pnl:+.2f}</div></div>
        <div class="stat-card"><div>Today P&L</div><div class="stat-value {'positive' if daily_pnl>=0 else 'negative'}">${daily_pnl:+.2f}</div></div>
        <div class="stat-card"><div>Today Trades</div><div class="stat-value">{daily_trades}</div></div>
        <div class="stat-card"><div>Today Win Rate</div><div class="stat-value">{daily_wr:.1%}</div></div>
    </div>
    <h2>Bot Leaderboard</h2>
    <table><tr><th>Bot</th><th>Trades</th><th>Wins</th><th>Win Rate</th><th>P&L</th></tr>{bot_rows}</table>
    <h2>Recent Trades</h2>
    <table><tr><th>Bot</th><th>Type</th><th>Outcome</th><th>Amount</th><th>Confidence</th><th>P&L</th><th>Status</th></tr>{trade_rows}</table>
    <p style="color:#666;font-size:0.8em;">Last refresh: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}</p>
</body></html>"""

    @app.get("/api/stats")
    async def api_stats():
        return {"bots": await db.get_all_bot_stats(),
                "daily": await db.get_daily_summary(),
                "total_pnl": await db.get_total_pnl()}

    @app.get("/api/trades")
    async def api_trades(limit: int = 50):
        return await db.get_recent_trades(limit)

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "mode": config.mode if config else "unknown",
                "uptime": time.time()}

    return app
