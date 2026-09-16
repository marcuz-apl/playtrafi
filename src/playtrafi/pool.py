"""Browser context pool for high-throughput concurrent scraping in Playtrafi."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from playtrafi.models import PlaytrafiConfig
from playtrafi.proxy import ProxyManager

logger = logging.getLogger("playtrafi.pool")


class BrowserContextPool:
    """Manages a single browser process and leases isolated browser contexts concurrently."""

    def __init__(
        self,
        config: PlaytrafiConfig,
        proxy_manager: ProxyManager | None = None,
    ) -> None:
        self.config = config
        self.proxy_manager = proxy_manager
        self.max_concurrency = config.max_concurrency
        self._semaphore: asyncio.Semaphore | None = None
        self._playwright: Any = None
        self._browser: Any = None
        self._active_contexts: int = 0
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        """Check if underlying browser is launched and active."""
        return self._browser is not None

    async def start(self) -> None:
        """Launch the shared browser instance."""
        if self._browser is not None:
            return

        async with self._lock:
            if self._browser is not None:
                return

            if self._semaphore is None:
                self._semaphore = asyncio.Semaphore(self.max_concurrency)

            try:
                try:
                    from patchright.async_api import async_playwright
                except ImportError:
                    from playwright.async_api import async_playwright

                self._playwright = await async_playwright().start()
                launch_args = [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ]
                self._browser = await self._playwright.chromium.launch(
                    headless=self.config.headless,
                    args=launch_args,
                )
                logger.info(
                    "BrowserContextPool started with concurrency limit %d",
                    self.max_concurrency,
                )
            except Exception as exc:
                logger.warning("Failed to initialize BrowserContextPool browser: %s", exc)
                self._browser = None
                # A half-started driver would leak here: without stopping it,
                # the next start() would spawn a second playwright instance.
                if self._playwright is not None:
                    try:
                        await self._playwright.stop()
                    except Exception:
                        pass
                    self._playwright = None

    async def close(self) -> None:
        """Terminate all pooled contexts and close the browser."""
        async with self._lock:
            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None

            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None

            # Allow event loop to process subprocess termination callbacks before shutdown
            await asyncio.sleep(0.05)

    @asynccontextmanager
    async def acquire_context(
        self,
        user_agent: str,
        proxy: str | None = None,
    ) -> AsyncIterator[tuple[Any, str | None]]:
        """Acquire a managed browser context bounded by the concurrency semaphore."""
        if self._browser is None:
            await self.start()
        if self._browser is None:
            raise RuntimeError("BrowserContextPool has no available browser instance.")

        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrency)

        async with self._semaphore:
            chosen_proxy = proxy
            if not chosen_proxy and self.proxy_manager and self.proxy_manager.has_proxies:
                chosen_proxy = self.proxy_manager.get_proxy()
            elif not chosen_proxy and self.config.proxy:
                chosen_proxy = self.config.proxy

            context_kwargs: dict[str, Any] = {
                "user_agent": user_agent,
                "viewport": {
                    "width": self.config.viewport_width,
                    "height": self.config.viewport_height,
                },
                "ignore_https_errors": True,
                "locale": "en-US",
            }
            if chosen_proxy:
                context_kwargs["proxy"] = {"server": chosen_proxy}

            try:
                context = await self._browser.new_context(**context_kwargs)
            except Exception:
                # A crashed browser process must be resurrectable: drop the
                # stale handle so the next acquire_context relaunches cleanly
                # instead of failing forever on the dead object.
                connected = False
                try:
                    connected = bool(self._browser.is_connected())
                except Exception:
                    connected = False
                if not connected:
                    async with self._lock:
                        self._browser = None
                raise
            self._active_contexts += 1
            try:
                yield context, chosen_proxy
            finally:
                self._active_contexts -= 1
                try:
                    await context.close()
                except Exception:
                    pass
