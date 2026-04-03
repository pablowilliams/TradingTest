"""SQLite database layer for trades, bots, evolution, and stats."""
import aiosqlite
import json
import time
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent.parent.parent / "trading.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    market_type TEXT NOT NULL,
    side TEXT NOT NULL,
    outcome TEXT NOT NULL,
    amount REAL NOT NULL,
    price REAL NOT NULL,
    confidence REAL NOT NULL,
    signals_snapshot TEXT,
    verification_result TEXT,
    status TEXT DEFAULT 'open',
    pnl REAL DEFAULT 0.0,
    resolved_at REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_configs (
    bot_id TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
    params TEXT NOT NULL,
    generation INTEGER DEFAULT 0,
    parent_id TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_stats (
    bot_id TEXT NOT NULL,
    total_trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    total_pnl REAL DEFAULT 0.0,
    win_rate REAL DEFAULT 0.0,
    avg_confidence REAL DEFAULT 0.0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (bot_id)
);

CREATE TABLE IF NOT EXISTS evolution_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    killed_bot TEXT NOT NULL,
    replaced_by TEXT NOT NULL,
    parent_bot TEXT NOT NULL,
    reason TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT NOT NULL,
    total_trades INTEGER DEFAULT 0,
    total_pnl REAL DEFAULT 0.0,
    best_bot TEXT,
    best_win_rate REAL DEFAULT 0.0,
    PRIMARY KEY (date)
);

CREATE TABLE IF NOT EXISTS bayesian_learning (
    bot_id TEXT NOT NULL,
    feature_key TEXT NOT NULL,
    observations INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0.5,
    PRIMARY KEY (bot_id, feature_key)
);

CREATE TABLE IF NOT EXISTS verification_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    bot_id TEXT NOT NULL,
    passed INTEGER NOT NULL,
    confidence REAL,
    signals_agree INTEGER,
    ev_positive INTEGER,
    spread_ok INTEGER,
    liquidity_ok INTEGER,
    reason TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS asklivermore_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    pattern TEXT NOT NULL,
    grade TEXT NOT NULL,
    price REAL,
    details TEXT,
    scraped_at REAL NOT NULL
);
"""


class Database:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self.db: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    async def close(self):
        if self.db:
            await self.db.close()

    # --- Trades ---
    async def insert_trade(self, bot_id: str, market_id: str, market_type: str,
                           side: str, outcome: str, amount: float, price: float,
                           confidence: float, signals: dict, verification: dict) -> int:
        cursor = await self.db.execute(
            """INSERT INTO trades (bot_id, market_id, market_type, side, outcome, amount,
               price, confidence, signals_snapshot, verification_result, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (bot_id, market_id, market_type, side, outcome, amount, price,
             confidence, json.dumps(signals), json.dumps(verification), time.time())
        )
        await self.db.commit()
        return cursor.lastrowid

    async def resolve_trade(self, trade_id: int, pnl: float):
        await self.db.execute(
            "UPDATE trades SET status='resolved', pnl=?, resolved_at=? WHERE id=?",
            (pnl, time.time(), trade_id)
        )
        await self.db.commit()

    async def get_open_trades(self, bot_id: Optional[str] = None) -> list:
        if bot_id:
            cursor = await self.db.execute(
                "SELECT * FROM trades WHERE status='open' AND bot_id=?", (bot_id,))
        else:
            cursor = await self.db.execute("SELECT * FROM trades WHERE status='open'")
        return [dict(row) for row in await cursor.fetchall()]

    async def get_recent_trades(self, limit: int = 50) -> list:
        cursor = await self.db.execute(
            "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(row) for row in await cursor.fetchall()]

    # --- Bot Stats ---
    async def update_bot_stats(self, bot_id: str, won: bool, pnl: float):
        row = await self.db.execute_fetchall(
            "SELECT * FROM bot_stats WHERE bot_id=?", (bot_id,))
        if row:
            stats = dict(row[0])
            total = stats["total_trades"] + 1
            wins = stats["wins"] + (1 if won else 0)
            await self.db.execute(
                """UPDATE bot_stats SET total_trades=?, wins=?, losses=?,
                   total_pnl=?, win_rate=?, updated_at=? WHERE bot_id=?""",
                (total, wins, total - wins, stats["total_pnl"] + pnl,
                 wins / total if total > 0 else 0, time.time(), bot_id)
            )
        else:
            await self.db.execute(
                """INSERT INTO bot_stats (bot_id, total_trades, wins, losses,
                   total_pnl, win_rate, updated_at)
                   VALUES (?, 1, ?, ?, ?, ?, ?)""",
                (bot_id, 1 if won else 0, 0 if won else 1, pnl,
                 1.0 if won else 0.0, time.time())
            )
        await self.db.commit()

    async def get_all_bot_stats(self) -> list:
        cursor = await self.db.execute(
            "SELECT * FROM bot_stats ORDER BY win_rate DESC")
        return [dict(row) for row in await cursor.fetchall()]

    async def get_bot_stats(self, bot_id: str) -> Optional[dict]:
        cursor = await self.db.execute(
            "SELECT * FROM bot_stats WHERE bot_id=?", (bot_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    # --- Bayesian Learning ---
    async def update_learning(self, bot_id: str, feature_key: str, won: bool):
        row = await self.db.execute_fetchall(
            "SELECT * FROM bayesian_learning WHERE bot_id=? AND feature_key=?",
            (bot_id, feature_key))
        if row:
            r = dict(row[0])
            obs = r["observations"] + 1
            w = r["wins"] + (1 if won else 0)
            await self.db.execute(
                """UPDATE bayesian_learning SET observations=?, wins=?, win_rate=?
                   WHERE bot_id=? AND feature_key=?""",
                (obs, w, (w + 1) / (obs + 2), bot_id, feature_key))  # Laplace smoothing
        else:
            await self.db.execute(
                """INSERT INTO bayesian_learning (bot_id, feature_key, observations, wins, win_rate)
                   VALUES (?, ?, 1, ?, ?)""",
                (bot_id, feature_key, 1 if won else 0, (2 if won else 1) / 3))
        await self.db.commit()

    async def get_learning_data(self, bot_id: str) -> dict:
        cursor = await self.db.execute(
            "SELECT feature_key, observations, wins, win_rate FROM bayesian_learning WHERE bot_id=?",
            (bot_id,))
        rows = await cursor.fetchall()
        return {row[0]: {"obs": row[1], "wins": row[2], "wr": row[3]} for row in rows}

    # --- Verification Log ---
    async def log_verification(self, market_id: str, bot_id: str, passed: bool,
                                confidence: float, signals_agree: int,
                                ev_positive: bool, spread_ok: bool,
                                liquidity_ok: bool, reason: str):
        await self.db.execute(
            """INSERT INTO verification_log (market_id, bot_id, passed, confidence,
               signals_agree, ev_positive, spread_ok, liquidity_ok, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (market_id, bot_id, int(passed), confidence, signals_agree,
             int(ev_positive), int(spread_ok), int(liquidity_ok), reason, time.time())
        )
        await self.db.commit()

    # --- Bot Configs (persist across restarts) ---
    async def save_bot_config(self, bot_id: str, strategy: str, params: dict,
                               generation: int = 0, parent_id: str = None):
        await self.db.execute(
            """INSERT OR REPLACE INTO bot_configs (bot_id, strategy, params, generation,
               parent_id, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
            (bot_id, strategy, json.dumps(params), generation, parent_id, time.time()))
        await self.db.commit()

    async def get_all_bot_configs(self) -> list:
        cursor = await self.db.execute("SELECT * FROM bot_configs")
        return [dict(row) for row in await cursor.fetchall()]

    async def delete_bot_config(self, bot_id: str):
        await self.db.execute("DELETE FROM bot_configs WHERE bot_id=?", (bot_id,))
        await self.db.commit()

    # --- AskLivermore Signals ---
    async def save_asklivermore_signals(self, signals: list):
        for s in signals:
            await self.db.execute(
                """INSERT INTO asklivermore_signals (ticker, pattern, grade, price, details, scraped_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (s.get("ticker", ""), s.get("pattern", ""), s.get("grade", ""),
                 s.get("price", 0), s.get("details", ""), time.time()))
        await self.db.commit()

    async def get_latest_asklivermore_signals(self, grade: str = None) -> list:
        if grade:
            cursor = await self.db.execute(
                "SELECT * FROM asklivermore_signals WHERE grade=? ORDER BY scraped_at DESC LIMIT 50",
                (grade,))
        else:
            cursor = await self.db.execute(
                "SELECT * FROM asklivermore_signals ORDER BY scraped_at DESC LIMIT 50")
        return [dict(row) for row in await cursor.fetchall()]

    # --- Daily Stats ---
    async def save_daily_stats(self, date: str, trades: int, pnl: float,
                                best_bot: str, best_wr: float):
        await self.db.execute(
            """INSERT OR REPLACE INTO daily_stats (date, total_trades, total_pnl,
               best_bot, best_win_rate) VALUES (?, ?, ?, ?, ?)""",
            (date, trades, pnl, best_bot, best_wr))
        await self.db.commit()

    # --- Summary ---
    async def get_daily_summary(self) -> dict:
        cursor = await self.db.execute(
            """SELECT COUNT(*) as trades, SUM(pnl) as total_pnl,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins
               FROM trades WHERE created_at > ? AND status='resolved'""",
            (time.time() - 86400,))
        row = await cursor.fetchone()
        return dict(row) if row else {"trades": 0, "total_pnl": 0, "wins": 0}

    async def get_total_pnl(self) -> float:
        cursor = await self.db.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE status='resolved'")
        row = await cursor.fetchone()
        return row[0]
