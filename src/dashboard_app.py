"""FastAPI web dashboard with login auth - deployable 24/7."""
import hashlib
import html
import json
import os
import secrets
import time
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, Request, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from .db.database import Database
from .config import Config

# #1: Generate random SECRET_KEY on first run if not set
_default_secret = "change-me-to-a-random-string"
SECRET = os.getenv("SECRET_KEY", "")
if not SECRET or SECRET == _default_secret:
    SECRET = secrets.token_hex(32)

# #2: Warn loudly if using default password
ADMIN_USER = os.getenv("DASHBOARD_USER", "admin")
_raw_password = os.getenv("DASHBOARD_PASSWORD", "tradingtest")
ADMIN_PASS_HASH = hashlib.sha256(_raw_password.encode()).hexdigest()
if _raw_password == "tradingtest":
    import logging
    _logger = logging.getLogger(__name__)
    _logger.warning(
        "SECURITY WARNING: Using default dashboard password 'tradingtest'. "
        "Set DASHBOARD_PASSWORD env var to a strong password!"
    )

# #5: Per-session random nonce for auth token
_session_nonce = secrets.token_hex(16)

# #59: Basic rate limiting store: IP -> list of attempt timestamps
_login_attempts: dict[str, list[float]] = defaultdict(list)
_MAX_LOGIN_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 60


def _check_auth(request: Request) -> bool:
    token = request.cookies.get("auth_token", "")
    expected = hashlib.sha256(
        f"{SECRET}:{ADMIN_PASS_HASH}:{_session_nonce}".encode()
    ).hexdigest()
    return token == expected


def _auth_token() -> str:
    return hashlib.sha256(
        f"{SECRET}:{ADMIN_PASS_HASH}:{_session_nonce}".encode()
    ).hexdigest()


def _is_rate_limited(ip: str) -> bool:
    """#59: Check if IP has exceeded login attempt limit."""
    now = time.time()
    attempts = _login_attempts[ip]
    # Prune old attempts outside the window
    _login_attempts[ip] = [t for t in attempts if now - t < _LOGIN_WINDOW_SECONDS]
    return len(_login_attempts[ip]) >= _MAX_LOGIN_ATTEMPTS


def _record_login_attempt(ip: str):
    _login_attempts[ip].append(time.time())


# #58: CSRF token generation and validation
_csrf_tokens: dict[str, float] = {}


def _generate_csrf_token() -> str:
    token = secrets.token_hex(32)
    _csrf_tokens[token] = time.time()
    # Clean old tokens (older than 1 hour)
    cutoff = time.time() - 3600
    for k in list(_csrf_tokens):
        if _csrf_tokens[k] < cutoff:
            del _csrf_tokens[k]
    return token


def _validate_csrf_token(token: str) -> bool:
    if token in _csrf_tokens:
        del _csrf_tokens[token]
        return True
    return False


def create_app(config: Config = None) -> FastAPI:
    app = FastAPI(title="TradingTest Dashboard")
    db = Database()
    _db_connected = False

    @app.on_event("startup")
    async def startup():
        nonlocal _db_connected
        await db.connect()
        _db_connected = True

    @app.on_event("shutdown")
    async def shutdown():
        nonlocal _db_connected
        await db.close()
        _db_connected = False

    # --- AUTH ---

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        csrf = _generate_csrf_token()
        return f"""<!DOCTYPE html>
<html><head><title>TradingTest Login</title>
<style>
    body {{ font-family: -apple-system, sans-serif; background: #1a1a2e; color: #eee;
           display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }}
    .login-box {{ background: #16213e; padding: 40px; border-radius: 12px; width: 320px; }}
    h1 {{ color: #00d4ff; text-align: center; margin-bottom: 30px; }}
    input {{ width: 100%; padding: 12px; margin: 8px 0 16px; border: 1px solid #333;
            border-radius: 6px; background: #0f3460; color: #eee; box-sizing: border-box; }}
    button {{ width: 100%; padding: 12px; background: #00d4ff; color: #1a1a2e; border: none;
             border-radius: 6px; font-size: 16px; font-weight: bold; cursor: pointer; }}
    button:hover {{ background: #00b4d8; }}
    .error {{ color: #f44336; text-align: center; }}
</style></head>
<body><div class="login-box">
    <h1>TradingTest</h1>
    <form method="POST" action="/login">
        <input type="hidden" name="csrf_token" value="{csrf}">
        <input type="text" name="username" placeholder="Username" required>
        <input type="password" name="password" placeholder="Password" required>
        <button type="submit">Login</button>
    </form>
</div></body></html>"""

    @app.post("/login")
    async def do_login(request: Request, username: str = Form(...),
                       password: str = Form(...), csrf_token: str = Form("")):
        client_ip = request.client.host if request.client else "unknown"

        # #59: Rate limiting check
        if _is_rate_limited(client_ip):
            return HTMLResponse("""<!DOCTYPE html><html><head><title>Rate Limited</title>
<style>body{font-family:sans-serif;background:#1a1a2e;color:#eee;display:flex;justify-content:center;align-items:center;height:100vh}
.box{background:#16213e;padding:40px;border-radius:12px;text-align:center}
a{color:#00d4ff}</style></head><body><div class="box">
<h2 style="color:#f44336">Too many login attempts. Try again later.</h2>
<a href="/login">Back</a></div></body></html>""", status_code=429)

        _record_login_attempt(client_ip)

        # #58: CSRF validation
        if not _validate_csrf_token(csrf_token):
            return HTMLResponse("""<!DOCTYPE html><html><head><title>Invalid Request</title>
<style>body{font-family:sans-serif;background:#1a1a2e;color:#eee;display:flex;justify-content:center;align-items:center;height:100vh}
.box{background:#16213e;padding:40px;border-radius:12px;text-align:center}
a{color:#00d4ff}</style></head><body><div class="box">
<h2 style="color:#f44336">Invalid or expired form. Please try again.</h2>
<a href="/login">Back</a></div></body></html>""", status_code=403)

        pw_hash = hashlib.sha256(password.encode()).hexdigest()
        if username == ADMIN_USER and pw_hash == ADMIN_PASS_HASH:
            resp = RedirectResponse(url="/", status_code=302)
            # #3/#4: Set secure and samesite on cookie
            is_https = request.headers.get("x-forwarded-proto") == "https"
            resp.set_cookie(
                "auth_token", _auth_token(), httponly=True, max_age=86400,
                secure=is_https, samesite="Lax"
            )
            return resp
        return HTMLResponse("""<!DOCTYPE html><html><head><title>Login Failed</title>
<style>body{font-family:sans-serif;background:#1a1a2e;color:#eee;display:flex;justify-content:center;align-items:center;height:100vh}
.box{background:#16213e;padding:40px;border-radius:12px;text-align:center}
a{color:#00d4ff}</style></head><body><div class="box">
<h2 style="color:#f44336">Invalid credentials</h2><a href="/login">Try again</a>
</div></body></html>""", status_code=401)

    @app.get("/logout")
    async def logout():
        resp = RedirectResponse(url="/login", status_code=302)
        resp.delete_cookie("auth_token")
        return resp

    # --- DASHBOARD ---

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        if not _check_auth(request):
            return RedirectResponse(url="/login", status_code=302)

        # #86: Handle case where DB isn't connected yet
        if not _db_connected or db.db is None:
            return HTMLResponse(
                "<html><body style='background:#1a1a2e;color:#eee;font-family:sans-serif;"
                "display:flex;justify-content:center;align-items:center;height:100vh'>"
                "<h2>Database is connecting, please refresh in a moment...</h2>"
                "</body></html>", status_code=503
            )

        stats = await db.get_all_bot_stats()
        recent = await db.get_recent_trades(limit=20)
        summary = await db.get_daily_summary()
        total_pnl = await db.get_total_pnl()

        # #6: HTML-escape all user data before rendering
        bot_rows = ""
        for s in stats:
            color = "#4CAF50" if s["win_rate"] >= 0.55 else "#f44336" if s["win_rate"] < 0.50 else "#ff9800"
            safe_bot_id = html.escape(str(s['bot_id']))
            bot_rows += f"""<tr>
                <td>{safe_bot_id}</td><td>{s['total_trades']}</td><td>{s['wins']}</td>
                <td style="color:{color}">{s['win_rate']:.1%}</td>
                <td style="color:{'#4CAF50' if s['total_pnl']>=0 else '#f44336'}">${s['total_pnl']:+.2f}</td></tr>"""

        trade_rows = ""
        for t in recent:
            pc = "#4CAF50" if t.get("pnl", 0) >= 0 else "#f44336"
            safe_bot_id = html.escape(str(t['bot_id']))
            safe_market_type = html.escape(str(t.get('market_type', '?')))
            safe_outcome = html.escape(str(t['outcome']))
            safe_status = html.escape(str(t['status']))
            trade_rows += f"""<tr>
                <td>{safe_bot_id}</td><td>{safe_market_type}</td><td>{safe_outcome}</td>
                <td>${t['amount']:.2f}</td><td>{t['confidence']:.1%}</td>
                <td style="color:{pc}">${t.get('pnl',0):+.2f}</td><td>{safe_status}</td></tr>"""

        dp = summary.get("total_pnl", 0) or 0
        dt = summary.get("trades", 0) or 0
        dw = summary.get("wins", 0) or 0
        dwr = dw / dt if dt > 0 else 0
        mode = html.escape(config.mode) if config else "unknown"

        return f"""<!DOCTYPE html>
<html><head><title>TradingTest Dashboard</title>
<meta http-equiv="refresh" content="10">
<style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #1a1a2e; color: #eee; margin: 0; padding: 20px; }}
    .header {{ display: flex; justify-content: space-between; align-items: center; }}
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
             background: {'#4CAF50' if mode == 'paper' else '#f44336'}; }}
    .logout {{ color: #888; text-decoration: none; padding: 8px 16px; border: 1px solid #333; border-radius: 6px; }}
    .logout:hover {{ color: #eee; border-color: #666; }}
</style></head>
<body>
    <div class="header">
        <h1>TradingTest <span class="mode">{mode.upper()}</span></h1>
        <a href="/logout" class="logout">Logout</a>
    </div>
    <div class="stats">
        <div class="stat-card"><div>Total P&amp;L</div><div class="stat-value {'positive' if total_pnl>=0 else 'negative'}">${total_pnl:+.2f}</div></div>
        <div class="stat-card"><div>Today P&amp;L</div><div class="stat-value {'positive' if dp>=0 else 'negative'}">${dp:+.2f}</div></div>
        <div class="stat-card"><div>Today Trades</div><div class="stat-value">{dt}</div></div>
        <div class="stat-card"><div>Today Win Rate</div><div class="stat-value">{dwr:.1%}</div></div>
    </div>
    <h2>Bot Leaderboard</h2>
    <table><tr><th>Bot</th><th>Trades</th><th>Wins</th><th>Win Rate</th><th>P&amp;L</th></tr>{bot_rows}</table>
    <h2>Recent Trades</h2>
    <table><tr><th>Bot</th><th>Type</th><th>Outcome</th><th>Amount</th><th>Confidence</th><th>P&amp;L</th><th>Status</th></tr>{trade_rows}</table>
    <p style="color:#666;font-size:0.8em;">Last refresh: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}</p>
</body></html>"""

    # --- API ENDPOINTS ---

    # #7: Return proper 401 status on unauthorized API calls
    @app.get("/api/stats")
    async def api_stats(request: Request):
        if not _check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        # #86: Handle DB not connected
        if not _db_connected or db.db is None:
            return JSONResponse({"error": "database not ready"}, status_code=503)
        return {"bots": await db.get_all_bot_stats(),
                "daily": await db.get_daily_summary(),
                "total_pnl": await db.get_total_pnl()}

    @app.get("/api/trades")
    async def api_trades(request: Request, limit: int = 50):
        if not _check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not _db_connected or db.db is None:
            return JSONResponse({"error": "database not ready"}, status_code=503)
        return await db.get_recent_trades(limit)

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "mode": config.mode if config else "unknown",
                "db_connected": _db_connected}

    return app
