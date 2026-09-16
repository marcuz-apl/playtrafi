"""Playtrafi — Undetected stealth web scraper & markdown extractor for LLMs."""

from playtrafi.chunker import TextChunk, chunk_markdown, count_tokens
from playtrafi.crawler import (
    AsyncPatchtroy,
    AsyncPlaytrafi,
    Patchtroy,
    Playtrafi,
)
from playtrafi.models import (
    LinkItem,
    PatchtroyConfig,
    PlaytrafiConfig,
    ScrapeResult,
)
from playtrafi.pool import BrowserContextPool
from playtrafi.proxy import ProxyItem, ProxyManager

__version__ = "0.7.0"
__author__ = "marcuz-apl"
__license__ = "Apache-2.0"
__copyright__ = "Copyright 2026 Alfazen Inc."

__all__ = [
    "AsyncPatchtroy",
    "AsyncPlaytrafi",
    "BrowserContextPool",
    "LinkItem",
    "Patchtroy",
    "PatchtroyConfig",
    "Playtrafi",
    "PlaytrafiConfig",
    "ProxyItem",
    "ProxyManager",
    "ScrapeResult",
    "TextChunk",
    "chunk_markdown",
    "count_tokens",
]

