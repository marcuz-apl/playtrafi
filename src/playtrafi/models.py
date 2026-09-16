"""Data schemas and models for Playtrafi."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PlaytrafiConfig(BaseModel):
    """Configuration options for Playtrafi crawler execution."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    headless: bool = Field(
        default=True,
        description="Run Chromium in headless mode."
    )
    browser_timeout_s: float = Field(
        default=25.0,
        ge=2.0,
        le=120.0,
        description="Maximum seconds to wait for page load before fallback or error."
    )
    wait_for: str | None = Field(
        default=None,
        description="Optional CSS selector to wait for before extracting page content."
    )
    wait_until: str = Field(
        default="domcontentloaded",
        description="Navigation lifecycle event: 'domcontentloaded', 'load', or 'networkidle'."
    )
    user_agent: str | None = Field(
        default=None,
        description="Custom User-Agent string. If omitted, uses realistic browser UA."
    )
    http_fallback: bool = Field(
        default=True,
        description="Automatically fall back to direct HTTP request if browser crashes or times out."
    )
    verify_tls: bool = Field(
        default=True,
        description="Verify TLS certificates on the HTTP fallback path. Disable only for known-bad-chain targets."
    )
    extract_schema: bool = Field(
        default=True,
        description="Extract JSON-LD and Next.js __NEXT_DATA__ structured items."
    )
    extract_links: bool = Field(
        default=True,
        description="Extract all valid hyperlinks from the page."
    )
    viewport_width: int = Field(default=1280, ge=320, le=3840)
    viewport_height: int = Field(default=800, ge=240, le=2160)
    proxy: str | None = Field(
        default=None,
        description="Single proxy URL (e.g. 'http://user:pass@host:port')."
    )
    proxies: list[str] | str | None = Field(
        default=None,
        description="List of proxy URLs, comma-separated string, or path to proxy file for rotation."
    )
    proxy_strategy: str = Field(
        default="round-robin",
        description="Proxy rotation strategy: 'round-robin' or 'random'."
    )
    max_concurrency: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Maximum concurrent browser contexts for batch scraping."
    )
    screenshot: bool = Field(
        default=False,
        description="Capture page screenshot."
    )
    full_page_screenshot: bool = Field(
        default=False,
        description="Capture full-page screenshot instead of viewport only."
    )
    screenshot_path: str | None = Field(
        default=None,
        description="Optional file path to automatically save screenshot to."
    )
    pdf: bool = Field(
        default=False,
        description="Generate page PDF (headless Chromium only)."
    )
    pdf_path: str | None = Field(
        default=None,
        description="Optional file path to automatically save PDF to."
    )
    custom_schema: dict[str, Any] | None = Field(
        default=None,
        description="Optional declarative custom CSS selector schema for structured item harvesting."
    )


# Backwards compatibility alias
PatchtroyConfig = PlaytrafiConfig


class LinkItem(BaseModel):
    """Hyperlink extracted from rendered page."""
    href: str
    text: str = ""


class ScrapeResult(BaseModel):
    """Result emitted from a Playtrafi scrape operation."""

    url: str
    status_code: int = 200
    title: str = ""
    markdown: str = ""
    html: str = ""
    structured_data: list[dict[str, Any]] = Field(default_factory=list)
    links: list[LinkItem] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    screenshot_bytes: bytes | None = None
    pdf_bytes: bytes | None = None
    success: bool = True
    error: str | None = None
    engine_used: str = "patchright"
    elapsed_s: float = 0.0

    @property
    def has_content(self) -> bool:
        return bool(self.markdown.strip() or self.structured_data)

    def save_screenshot(self, filepath: str | Path) -> Path:
        """Save screenshot bytes to disk."""
        if not self.screenshot_bytes:
            raise ValueError(f"No screenshot bytes available for {self.url}")
        dest = Path(filepath)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.screenshot_bytes)
        return dest

    def save_pdf(self, filepath: str | Path) -> Path:
        """Save PDF bytes to disk."""
        if not self.pdf_bytes:
            raise ValueError(f"No PDF bytes available for {self.url}")
        dest = Path(filepath)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.pdf_bytes)
        return dest

    def chunk(
        self,
        max_tokens: int = 2048,
        overlap_tokens: int = 100,
    ) -> list[Any]:
        """Split extracted Markdown content into LLM-ready TextChunk records."""
        from playtrafi.chunker import chunk_markdown
        return chunk_markdown(
            self.markdown,
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
            metadata={"url": self.url, "title": self.title},
        )

    def to_csv(self) -> str:
        """Export this ScrapeResult as an RFC 4180 CSV string."""
        from playtrafi.utils import results_to_csv
        return results_to_csv(self)


