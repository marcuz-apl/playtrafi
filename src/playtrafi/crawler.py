"""Core crawler implementations for Playtrafi."""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from playtrafi.extractors import (
    extract_links,
    extract_markdown_and_metadata,
    extract_structured_data,
)
from playtrafi.models import LinkItem, PlaytrafiConfig, ScrapeResult
from playtrafi.pool import BrowserContextPool
from playtrafi.proxy import ProxyManager
from playtrafi.utils import (
    STEALTH_INJECTION_SCRIPT,
    get_random_user_agent,
    is_valid_url,
    silence_windows_proactor_bug,
)

logger = logging.getLogger("playtrafi.crawler")

def _browser_headers(user_agent: str) -> dict[str, str]:
    """Browser-grade navigation headers for HTTP fallback.

    A bare User-Agent + Accept-Language pair is a classic automation
    fingerprint (bot managers like Cloudflare challenge it outright), so the
    fallback must present the full top-of-navigation header set. Client hints
    are derived from the UA string itself so the brand version never
    contradicts it.
    """
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
    chrome = re.search(r"Chrome/(\d+)", user_agent)
    if chrome and "Edg/" not in user_agent:
        if "Windows NT" in user_agent:
            platform = '"Windows"'
        elif "Macintosh" in user_agent:
            platform = '"macOS"'
        else:
            platform = '"Linux"'
        v = chrome.group(1)
        headers.update(
            {
                "Sec-Ch-Ua": f'"Google Chrome";v="{v}", "Chromium";v="{v}", "Not_A Brand";v="24"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": platform,
            }
        )
    return headers


class AsyncPlaytrafi:
    """Asynchronous stealth web crawler powered by Patchright, Trafilatura, and Context Pooling."""

    def __init__(self, config: PlaytrafiConfig | dict[str, Any] | None = None) -> None:
        if isinstance(config, dict):
            self.config = PlaytrafiConfig(**config)
        elif isinstance(config, PlaytrafiConfig):
            self.config = config
        else:
            self.config = PlaytrafiConfig()

        # Initialize ProxyManager if configured
        self.proxy_manager: ProxyManager | None = None
        if self.config.proxies:
            self.proxy_manager = ProxyManager(
                proxies=self.config.proxies,
                strategy=self.config.proxy_strategy,
            )

        # Initialize BrowserContextPool
        self._pool = BrowserContextPool(self.config, proxy_manager=self.proxy_manager)

    @property
    def _browser(self) -> Any:
        """Compatibility accessor for underlying browser."""
        return self._pool._browser

    @property
    def _playwright(self) -> Any:
        """Compatibility accessor for underlying playwright."""
        return self._pool._playwright

    async def __aenter__(self) -> AsyncPlaytrafi:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def start(self) -> None:
        """Launch underlying stealth Patchright Chromium browser process via pool."""
        await self._pool.start()

    async def close(self) -> None:
        """Gracefully terminate browser pool and all contexts."""
        await self._pool.close()

    async def scrape(
        self,
        url: str,
        wait_for: str | None = None,
        custom_schema: dict[str, Any] | None = None,
    ) -> ScrapeResult:
        """Scrape a target URL using stealth browser automation, with optional HTTP fallback."""
        if not is_valid_url(url):
            return ScrapeResult(
                url=url,
                status_code=400,
                success=False,
                error=f"Invalid HTTP/HTTPS URL: {url}",
            )

        start_time = time.perf_counter()
        wait_for_selector = wait_for or self.config.wait_for
        ua = self.config.user_agent or get_random_user_agent()

        # 1. Attempt stealth browser navigation
        try:
            return await self._scrape_with_browser(url, ua, wait_for_selector, custom_schema, start_time)
        except Exception as exc:
            logger.info("Browser scrape encountered issue: %s", exc)
            if self.config.http_fallback:
                logger.info("Engaging HTTP fallback for %s", url)
                return await self._scrape_with_http(url, ua, custom_schema, start_time, str(exc))
            else:
                elapsed = time.perf_counter() - start_time
                return ScrapeResult(
                    url=url,
                    status_code=500,
                    success=False,
                    error=str(exc),
                    elapsed_s=round(elapsed, 3),
                )

    async def _scrape_with_browser(
        self,
        url: str,
        user_agent: str,
        wait_for_selector: str | None,
        custom_schema: dict[str, Any] | None,
        start_time: float,
    ) -> ScrapeResult:
        """Execute in-browser rendering with stealth injection, context pooling, and media capture."""
        timeout_ms = int(self.config.browser_timeout_s * 1000)

        async with self._pool.acquire_context(user_agent=user_agent) as (context, used_proxy):
            page = await context.new_page()
            try:
                # Apply stealth browser configuration
                await page.add_init_script(STEALTH_INJECTION_SCRIPT)

                resp = await page.goto(
                    url,
                    timeout=timeout_ms,
                    wait_until=self.config.wait_until,
                )

                status_code = resp.status if resp else 200
                # A rendered bot challenge (403/5xx) must not masquerade as a
                # success: the page *rendered*, but the request was refused.
                success_flag = True if resp is None else bool(200 <= resp.status < 400)

                if wait_for_selector:
                    try:
                        await page.wait_for_selector(wait_for_selector, timeout=7000)
                    except Exception as wait_exc:
                        logger.debug("wait_for selector '%s' timeout: %s", wait_for_selector, wait_exc)

                # Media captures: Screenshot & PDF
                screenshot_bytes: bytes | None = None
                if self.config.screenshot or self.config.screenshot_path:
                    screenshot_bytes = await page.screenshot(full_page=self.config.full_page_screenshot)
                    if self.config.screenshot_path:
                        dest = Path(self.config.screenshot_path)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(screenshot_bytes)

                pdf_bytes: bytes | None = None
                if self.config.pdf or self.config.pdf_path:
                    try:
                        pdf_bytes = await page.pdf()
                        if self.config.pdf_path:
                            dest = Path(self.config.pdf_path)
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            dest.write_bytes(pdf_bytes)
                    except Exception as pdf_exc:
                        logger.warning("PDF export failed (requires headless Chromium): %s", pdf_exc)

                html = await page.content()
                final_url = page.url or url

                # Record proxy success
                if used_proxy and self.proxy_manager:
                    self.proxy_manager.report_success(used_proxy)

                # Content extractions
                markdown, title, metadata = extract_markdown_and_metadata(html, base_url=final_url)
                structured_data = []
                if self.config.extract_schema:
                    schema_to_use = custom_schema or self.config.custom_schema
                    structured_data = extract_structured_data(html, source_url=final_url, custom_schema=schema_to_use)

                links = []
                if self.config.extract_links:
                    raw_links = extract_links(html, base_url=final_url)
                    links = [LinkItem(**link_dict) for link_dict in raw_links]

                elapsed = time.perf_counter() - start_time
                return ScrapeResult(
                    url=final_url,
                    status_code=status_code,
                    title=title,
                    markdown=markdown,
                    html=html,
                    structured_data=structured_data,
                    links=links,
                    metadata=metadata,
                    screenshot_bytes=screenshot_bytes,
                    pdf_bytes=pdf_bytes,
                    success=success_flag,
                    engine_used="patchright",
                    elapsed_s=round(elapsed, 3),
                )
            except Exception:
                if used_proxy and self.proxy_manager:
                    self.proxy_manager.report_failure(used_proxy)
                raise
            finally:
                await page.close()

    async def _scrape_with_http(
        self,
        url: str,
        user_agent: str,
        custom_schema: dict[str, Any] | None,
        start_time: float,
        browser_error: str,
    ) -> ScrapeResult:
        """Fast direct HTTP fallback when browser engine is unavailable or timed out."""
        headers = _browser_headers(user_agent)
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=self.config.browser_timeout_s,
                verify=self.config.verify_tls,
            ) as client:
                resp = await client.get(url, headers=headers)
                html = resp.text
                final_url = str(resp.url)
                status_code = resp.status_code

            markdown, title, metadata = extract_markdown_and_metadata(html, base_url=final_url)
            structured_data = []
            if self.config.extract_schema:
                schema_to_use = custom_schema or self.config.custom_schema
                structured_data = extract_structured_data(html, source_url=final_url, custom_schema=schema_to_use)

            links = []
            if self.config.extract_links:
                raw_links = extract_links(html, base_url=final_url)
                links = [LinkItem(**link_dict) for link_dict in raw_links]

            elapsed = time.perf_counter() - start_time
            return ScrapeResult(
                url=final_url,
                status_code=status_code,
                title=title,
                markdown=markdown,
                html=html,
                structured_data=structured_data,
                links=links,
                metadata=metadata,
                success=resp.is_success,
                error=f"HTTP fallback (browser failed: {browser_error})",
                engine_used="http_fallback",
                elapsed_s=round(elapsed, 3),
            )
        except Exception as http_exc:
            elapsed = time.perf_counter() - start_time
            return ScrapeResult(
                url=url,
                status_code=500,
                success=False,
                error=f"Both browser and HTTP fallback failed. Browser: {browser_error} | HTTP: {http_exc}",
                engine_used="failed",
                elapsed_s=round(elapsed, 3),
            )

    async def scrape_many(
        self,
        urls: list[str],
        wait_for: str | None = None,
        custom_schema: dict[str, Any] | None = None,
    ) -> list[ScrapeResult]:
        """Scrape multiple URLs concurrently bounded by ``max_concurrency``.

        The browser pool itself caps live contexts, but with ``http_fallback``
        enabled a failing batch would otherwise run one unconditional HTTP
        request per URL simultaneously — a burst bot managers punish.
        """
        sem = asyncio.Semaphore(max(1, self.config.max_concurrency))

        async def _one(target: str) -> ScrapeResult:
            async with sem:
                return await self.scrape(target, wait_for=wait_for, custom_schema=custom_schema)

        return await asyncio.gather(*(_one(u) for u in urls))

    @classmethod
    async def crawl(
        cls,
        url: str,
        headless: bool = True,
        wait_for: str | None = None,
        custom_schema: dict[str, Any] | None = None,
        screenshot: bool = False,
        pdf: bool = False,
    ) -> ScrapeResult:
        """One-shot convenience coroutine for instant single-URL scraping."""
        config = PlaytrafiConfig(
            headless=headless,
            wait_for=wait_for,
            screenshot=screenshot,
            pdf=pdf,
        )
        async with cls(config) as client:
            return await client.scrape(url, wait_for=wait_for, custom_schema=custom_schema)

    @classmethod
    async def crawl_many(
        cls,
        urls: list[str],
        headless: bool = True,
        max_concurrency: int = 5,
        wait_for: str | None = None,
        custom_schema: dict[str, Any] | None = None,
    ) -> list[ScrapeResult]:
        """One-shot convenience coroutine for instant concurrent batch scraping."""
        config = PlaytrafiConfig(
            headless=headless,
            max_concurrency=max_concurrency,
            wait_for=wait_for,
            custom_schema=custom_schema,
        )
        async with cls(config) as client:
            return await client.scrape_many(urls, wait_for=wait_for, custom_schema=custom_schema)


# Backwards compatibility alias
AsyncPatchtroy = AsyncPlaytrafi


def _run_sync(coro: Any) -> Any:
    """Execute coroutine synchronously with Windows proactor event loop protections."""
    silence_windows_proactor_bug()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    return asyncio.run(coro)


class Playtrafi:
    """Synchronous wrapper for Playtrafi crawler execution."""

    def __init__(self, config: PlaytrafiConfig | dict[str, Any] | None = None) -> None:
        self.config = (
            PlaytrafiConfig(**config)
            if isinstance(config, dict)
            else (config or PlaytrafiConfig())
        )
        silence_windows_proactor_bug()
        self._async_crawler: AsyncPlaytrafi | None = None

    def _get_crawler(self) -> AsyncPlaytrafi:
        if self._async_crawler is None:
            self._async_crawler = AsyncPlaytrafi(self.config)
        return self._async_crawler

    def scrape(
        self,
        url: str,
        wait_for: str | None = None,
        custom_schema: dict[str, Any] | None = None,
    ) -> ScrapeResult:
        """Execute scrape synchronously in an event loop."""
        crawler = self._get_crawler()
        return _run_sync(crawler.scrape(url, wait_for=wait_for, custom_schema=custom_schema))

    def scrape_many(
        self,
        urls: list[str],
        wait_for: str | None = None,
        custom_schema: dict[str, Any] | None = None,
    ) -> list[ScrapeResult]:
        """Execute concurrent batch scraping synchronously."""
        crawler = self._get_crawler()
        return _run_sync(crawler.scrape_many(urls, wait_for=wait_for, custom_schema=custom_schema))

    def close(self) -> None:
        """Close browser pool and cleanup resources."""
        if self._async_crawler is not None:
            try:
                _run_sync(self._async_crawler.close())
            finally:
                self._async_crawler = None

    def __enter__(self) -> Playtrafi:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    @classmethod
    def crawl(
        cls,
        url: str,
        headless: bool = True,
        wait_for: str | None = None,
        custom_schema: dict[str, Any] | None = None,
        screenshot: bool = False,
        pdf: bool = False,
    ) -> ScrapeResult:
        """Synchronous one-shot convenience function for scraping."""
        return _run_sync(
            AsyncPlaytrafi.crawl(
                url,
                headless=headless,
                wait_for=wait_for,
                custom_schema=custom_schema,
                screenshot=screenshot,
                pdf=pdf,
            )
        )

    @classmethod
    def crawl_many(
        cls,
        urls: list[str],
        headless: bool = True,
        max_concurrency: int = 5,
        wait_for: str | None = None,
        custom_schema: dict[str, Any] | None = None,
    ) -> list[ScrapeResult]:
        """Synchronous one-shot convenience function for concurrent batch crawling."""
        return _run_sync(
            AsyncPlaytrafi.crawl_many(
                urls,
                headless=headless,
                max_concurrency=max_concurrency,
                wait_for=wait_for,
                custom_schema=custom_schema,
            )
        )


# Backwards compatibility alias
Patchtroy = Playtrafi
