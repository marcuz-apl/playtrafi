import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import playtrafi.crawler as crawler_mod
from playtrafi.crawler import (
    AsyncPatchtroy,
    AsyncPlaytrafi,
    Patchtroy,
    Playtrafi,
    _browser_headers,
)
from playtrafi.models import PatchtroyConfig, PlaytrafiConfig, ScrapeResult


@pytest.mark.asyncio
async def test_invalid_url_handling():
    crawler = AsyncPlaytrafi()
    res = await crawler.scrape("not-a-valid-url")
    assert res.success is False
    assert "Invalid HTTP/HTTPS URL" in res.error
    # Test backwards compatibility alias
    assert AsyncPatchtroy is AsyncPlaytrafi
    assert PatchtroyConfig is PlaytrafiConfig


def test_sync_invalid_url():
    crawler = Playtrafi()
    res = crawler.scrape("ftp://invalid-scheme.com")
    assert res.success is False
    assert "Invalid HTTP/HTTPS URL" in res.error
    # Test backwards compatibility alias
    assert Patchtroy is Playtrafi


@pytest.mark.asyncio
async def test_async_scrape_many():
    crawler = AsyncPlaytrafi(PlaytrafiConfig(max_concurrency=3))
    mock_res = ScrapeResult(url="https://example.com", title="Example", success=True)

    with patch.object(crawler, "scrape", new_callable=AsyncMock, return_value=mock_res) as mock_scrape:
        urls = ["https://example.com/1", "https://example.com/2", "https://example.com/3"]
        results = await crawler.scrape_many(urls)
        assert len(results) == 3
        assert mock_scrape.call_count == 3
        assert all(r.success for r in results)


@pytest.mark.asyncio
async def test_async_scrape_many_respects_max_concurrency():
    """The public batch API must never fire more than max_concurrency scrapes at once."""
    crawler = AsyncPlaytrafi(PlaytrafiConfig(max_concurrency=1))
    in_flight = 0
    peak = 0

    async def _probe(url, wait_for=None, custom_schema=None):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return ScrapeResult(url=url, success=True)

    with patch.object(crawler, "scrape", new_callable=AsyncMock, side_effect=_probe):
        results = await crawler.scrape_many([f"https://example.com/{i}" for i in range(6)])

    assert len(results) == 6
    assert all(r.success for r in results)
    assert peak == 1


def test_sync_scrape_many():
    crawler = Playtrafi(PlaytrafiConfig(max_concurrency=2))
    urls = ["ftp://bad-url-1", "ftp://bad-url-2"]
    results = crawler.scrape_many(urls)
    assert len(results) == 2
    assert all(not r.success for r in results)
    crawler.close()


def test_sync_context_manager():
    with Playtrafi() as crawler:
        res = crawler.scrape("ftp://bad-url")
        assert res.success is False


def test_silence_windows_proactor_bug():
    from playtrafi.utils import silence_windows_proactor_bug

    # Should execute cleanly across all platforms
    silence_windows_proactor_bug()


def test_browser_headers_match_real_navigation():
    ua_win = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    h = _browser_headers(ua_win)
    assert h["Accept"].startswith("text/html")
    assert h["Sec-Fetch-Mode"] == "navigate"
    assert h["Sec-Fetch-Site"] == "none"
    assert 'v="131"' in h["Sec-Ch-Ua"]
    assert h["Sec-Ch-Ua-Platform"] == '"Windows"'

    ua_mac = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    assert _browser_headers(ua_mac)["Sec-Ch-Ua-Platform"] == '"macOS"'

    # Non-Chromium UA (Edge reuses Chromium's token): no client hints at all —
    # sending Chrome-branded hints next to an Edg/ UA would be a mismatch signal.
    assert "Sec-Ch-Ua" not in _browser_headers(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
    )


@pytest.mark.asyncio
async def test_http_fallback_sends_browser_headers(monkeypatch):
    crawler = AsyncPlaytrafi(PlaytrafiConfig(headless=True))
    captured = {}

    class _FakeResponse:
        text = "<html><body><h1>ok</h1></body></html>"
        status_code = 200
        is_success = True
        url = "https://target.test/"

    class _FakeClient:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def get(self, _url, headers=None):
            captured["headers"] = headers
            return _FakeResponse()

    monkeypatch.setattr(crawler_mod.httpx, "AsyncClient", _FakeClient)
    res = await crawler._scrape_with_http(
        "https://target.test/",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        None,
        0.0,
        "browser failed",
    )

    assert res.success is True
    h = captured["headers"]
    assert h["Accept"].startswith("text/html")
    assert '"Google Chrome";v="131"' in h["Sec-Ch-Ua"]
    assert h["Upgrade-Insecure-Requests"] == "1"

