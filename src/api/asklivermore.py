"""AskLivermore scraper for stock pattern signals."""
import json
import logging
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Store cookies in a safe, non-repo location (temp dir or .claude dir).
_CLAUDE_DIR = Path.home() / ".claude"
if _CLAUDE_DIR.is_dir():
    COOKIE_PATH = _CLAUDE_DIR / "asklivermore_cookies.json"
else:
    COOKIE_PATH = Path(tempfile.gettempdir()) / "asklivermore_cookies.json"

# Playwright is an optional dependency. Import errors are caught so the rest
# of the codebase can load even when playwright is not installed.
try:
    from playwright.async_api import async_playwright
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False
    logger.info(
        "playwright is not installed; AskLivermore scraper will be unavailable. "
        "Install with: pip install playwright && playwright install chromium"
    )


class AskLivermore:
    """Scrape A+ stock signals from AskLivermore dashboard."""

    def __init__(self, email: str, password: str):
        if not _HAS_PLAYWRIGHT:
            raise RuntimeError(
                "playwright is required for AskLivermore. "
                "Install with: pip install playwright && playwright install chromium"
            )
        self.email = email
        self.password = password
        self.browser = None
        self.page = None
        self._pw = None
        self.context = None

    async def __aenter__(self):
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(headless=True)
        context_opts = {}
        if COOKIE_PATH.exists():
            try:
                cookies = json.loads(COOKIE_PATH.read_text())
                context_opts["storage_state"] = {"cookies": cookies}
            except Exception:
                pass
        self.context = await self.browser.new_context(**context_opts)
        self.page = await self.context.new_page()
        return self

    async def __aexit__(self, *args):
        if self.browser:
            await self.browser.close()
        if self._pw:
            await self._pw.stop()

    async def login(self) -> bool:
        try:
            await self.page.goto("https://asklivermore.com/login", timeout=30000)
            await self.page.wait_for_load_state("networkidle")
            if "/dashboard" in self.page.url:
                logger.info("Already logged in via cookies")
                return True
            await self.page.fill('input[type="email"], input[name="email"]', self.email)
            await self.page.fill('input[type="password"], input[name="password"]', self.password)
            await self.page.click('button[type="submit"]')
            await self.page.wait_for_url("**/dashboard**", timeout=15000)
            cookies = await self.context.cookies()
            COOKIE_PATH.write_text(json.dumps(cookies))
            logger.info("Login successful")
            return True
        except Exception as e:
            logger.error(f"Login failed: {e}")
            return False

    async def scrape_signals(self) -> list:
        signals = []
        try:
            await self.page.goto("https://asklivermore.com/dashboard", timeout=30000)
            await self.page.wait_for_load_state("networkidle")
            await self.page.wait_for_timeout(3000)

            rows = await self.page.query_selector_all(
                "table tbody tr, .signal-row, .scan-result, "
                "[class*='result'], [class*='signal']")
            if not rows:
                rows = await self.page.query_selector_all(
                    "tr, .card, [class*='stock'], [class*='ticker']")

            for row in rows:
                try:
                    text = await row.inner_text()
                    cells = [c.strip() for c in text.split("\n") if c.strip()]
                    if len(cells) >= 3:
                        signal = {
                            "ticker": cells[0],
                            "pattern": cells[1] if len(cells) > 1 else "",
                            "grade": cells[2] if len(cells) > 2 else "",
                            "price": self._extract_price(cells),
                            "details": " | ".join(cells[3:]) if len(cells) > 3 else ""
                        }
                        if (signal["ticker"].isalpha() and len(signal["ticker"]) <= 5
                                and signal["grade"] in ("A+", "A", "A-", "B+", "B")):
                            signals.append(signal)
                except Exception:
                    continue
            logger.info(f"Scraped {len(signals)} signals")
        except Exception as e:
            logger.error(f"Scrape failed: {e}")
        return signals

    async def get_a_plus_signals(self) -> list:
        signals = await self.scrape_signals()
        return [s for s in signals if s["grade"] == "A+"]

    async def get_strong_signals(self) -> list:
        signals = await self.scrape_signals()
        return [s for s in signals if s["grade"] in ("A+", "A")]

    @staticmethod
    def _extract_price(cells: list) -> float:
        for cell in cells:
            cell = cell.replace("$", "").replace(",", "").strip()
            try:
                val = float(cell)
                if 0.01 < val < 100000:
                    return val
            except ValueError:
                continue
        return 0.0
