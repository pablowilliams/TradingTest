"""Telegram notifications for trades and alerts."""
import logging
import aiohttp
from typing import Optional

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    """Send trade alerts via Telegram."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def send(self, message: str, parse_mode: str = "HTML"):
        if not self.enabled:
            return
        try:
            url = TELEGRAM_API.format(token=self.bot_token)
            session = await self._get_session()
            await session.post(url, json={
                "chat_id": self.chat_id, "text": message,
                "parse_mode": parse_mode, "disable_web_page_preview": True})
        except Exception as e:
            # #98: Don't include bot token in error messages/logs
            logger.error(f"Telegram send failed: {type(e).__name__}: {_sanitize_error(e, self.bot_token)}")

    async def notify_buy(self, bot_id: str, market: str, outcome: str,
                         amount: float, price: float, confidence: float, reasoning: str):
        msg = (f"🟢 <b>BUY SIGNAL</b>\n"
               f"Bot: <code>{bot_id}</code>\n"
               f"Market: {market[:80]}\n"
               f"Outcome: <b>{outcome}</b>\n"
               f"Amount: <b>${amount:.2f}</b> @ {price:.3f}\n"
               f"Confidence: {confidence:.1%}\n"
               f"Reasoning: {reasoning[:100]}")
        await self.send(msg)

    async def notify_sell(self, bot_id: str, market: str, pnl: float, reason: str):
        emoji = "🟢" if pnl >= 0 else "🔴"
        msg = (f"{emoji} <b>SELL/RESOLVE</b>\n"
               f"Bot: <code>{bot_id}</code>\n"
               f"Market: {market[:80]}\n"
               f"P&L: <b>${pnl:+.2f}</b>\n"
               f"Reason: {reason}")
        await self.send(msg)

    async def notify_verification_fail(self, bot_id: str, market: str, reasons: list):
        msg = (f"⚠️ <b>TRADE BLOCKED</b>\n"
               f"Bot: <code>{bot_id}</code>\n"
               f"Market: {market[:80]}\n"
               f"Reasons:\n" + "\n".join(f"  • {r}" for r in reasons[:5]))
        await self.send(msg)

    async def notify_daily_summary(self, stats: dict):
        pnl = stats.get("total_pnl", 0) or 0
        trades = stats.get("trades", 0) or 0
        wins = stats.get("wins", 0) or 0
        wr = wins / trades if trades > 0 else 0
        emoji = "📈" if pnl >= 0 else "📉"
        msg = (f"{emoji} <b>DAILY SUMMARY</b>\n"
               f"Trades: {trades}\n"
               f"Wins: {wins}\n"
               f"P&L: <b>${pnl:+.2f}</b>\n"
               f"Win Rate: {wr:.1%}")
        await self.send(msg)

    async def notify_evolution(self, killed: str, replaced_by: str, reason: str):
        msg = (f"🧬 <b>EVOLUTION</b>\n"
               f"Killed: <code>{killed}</code>\n"
               f"Replaced by: <code>{replaced_by}</code>\n"
               f"Reason: {reason}")
        await self.send(msg)

    async def notify_asklivermore(self, signals: list):
        if not signals:
            return
        header = f"📊 <b>ASKLIVERMORE A+ SIGNALS</b> ({len(signals)} found)\n\n"
        lines = []
        for s in signals[:10]:
            lines.append(f"<b>{s['ticker']}</b> ${s['price']:.2f} — {s['pattern']} [{s['grade']}]")
        await self.send(header + "\n".join(lines))


def _sanitize_error(e: Exception, token: str) -> str:
    """Remove bot token from error strings to avoid leaking it in logs."""
    msg = str(e)
    if token:
        msg = msg.replace(token, "<REDACTED>")
    return msg
