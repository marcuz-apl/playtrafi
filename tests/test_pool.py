"""Unit tests for BrowserContextPool in playtrafi."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from playtrafi.models import PlaytrafiConfig
from playtrafi.pool import BrowserContextPool
from playtrafi.proxy import ProxyManager


@pytest.mark.asyncio
async def test_pool_context_acquisition():
    config = PlaytrafiConfig(max_concurrency=2)
    pool = BrowserContextPool(config)

    # Mock browser and playwright objects
    mock_browser = MagicMock()
    mock_context = MagicMock()
    mock_context.close = AsyncMock()
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_browser.close = AsyncMock()

    pool._browser = mock_browser

    async with pool.acquire_context(user_agent="TestAgent") as (ctx, proxy):
        assert ctx == mock_context
        assert proxy is None
        assert pool._active_contexts == 1

    assert pool._active_contexts == 0
    mock_context.close.assert_awaited_once()

    await pool.close()
    mock_browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_pool_with_proxy_manager():
    config = PlaytrafiConfig(max_concurrency=2)
    pm = ProxyManager(proxies=["http://proxy1:8080", "http://proxy2:8080"])
    pool = BrowserContextPool(config, proxy_manager=pm)

    mock_browser = MagicMock()
    mock_context = MagicMock()
    mock_context.close = AsyncMock()
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    pool._browser = mock_browser

    async with pool.acquire_context(user_agent="TestAgent") as (ctx, proxy):
        assert proxy == "http://proxy1:8080"
        mock_browser.new_context.assert_awaited_with(
            user_agent="TestAgent",
            viewport={"width": 1280, "height": 800},
            ignore_https_errors=True,
            locale="en-US",
            proxy={"server": "http://proxy1:8080"},
        )


@pytest.mark.asyncio
async def test_pool_resurrects_after_browser_crash():
    """A dead browser handle is dropped so the next acquire relaunches it."""
    config = PlaytrafiConfig(max_concurrency=2)
    pool = BrowserContextPool(config)

    dead_browser = MagicMock()
    dead_browser.new_context = AsyncMock(side_effect=RuntimeError("Target page crashed"))
    dead_browser.is_connected = MagicMock(return_value=False)
    pool._browser = dead_browser
    pool._semaphore = asyncio.Semaphore(config.max_concurrency)

    with pytest.raises(RuntimeError, match="crashed"):
        async with pool.acquire_context(user_agent="TestAgent"):
            pass

    assert pool._browser is None

    # Next acquisition relaunches (simulate a fresh, healthy browser).
    healthy = MagicMock()
    healthy.is_connected = MagicMock(return_value=True)
    healthy.new_context = AsyncMock(return_value=MagicMock(close=AsyncMock()))
    pool._browser = healthy

    async with pool.acquire_context(user_agent="TestAgent") as (ctx, _):
        assert ctx is not None


@pytest.mark.asyncio
async def test_pool_keeps_live_browser_on_transient_context_failure():
    """A still-connected browser is not reset when a single context fails."""
    config = PlaytrafiConfig(max_concurrency=2)
    pool = BrowserContextPool(config)

    live_browser = MagicMock()
    live_browser.new_context = AsyncMock(side_effect=RuntimeError("transient"))
    live_browser.is_connected = MagicMock(return_value=True)
    pool._browser = live_browser
    pool._semaphore = asyncio.Semaphore(config.max_concurrency)

    with pytest.raises(RuntimeError, match="transient"):
        async with pool.acquire_context(user_agent="TestAgent"):
            pass

    assert pool._browser is live_browser


@pytest.mark.asyncio
async def test_pool_failed_start_cleans_up_playwright(monkeypatch):
    """A failed launch must stop the half-started driver, not leak it."""
    config = PlaytrafiConfig(max_concurrency=2)
    pool = BrowserContextPool(config)

    playwright_mock = MagicMock()
    playwright_mock.stop = AsyncMock()
    playwright_mock.chromium.launch = AsyncMock(side_effect=RuntimeError("browser missing"))

    class _Manager:
        async def start(self):
            return playwright_mock

    import patchright.async_api as patchright_api

    monkeypatch.setattr(patchright_api, "async_playwright", lambda: _Manager())

    await pool.start()

    assert pool._browser is None
    assert pool._playwright is None
    playwright_mock.stop.assert_awaited_once()
    # A second start() after the cleaned-up failure must also be safe.
    await pool.start()
    assert pool._browser is None
