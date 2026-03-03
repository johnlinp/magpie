from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urljoin, urlparse

from magpie.utils import account_slug_from_url, extract_datetime_from_page

if TYPE_CHECKING:
    from playwright.sync_api import Page
else:
    Page = Any


class RedditAdapter:
    name = "reddit"

    def is_supported(self, account_url: str) -> bool:
        host = (urlparse(account_url).hostname or "").lower()
        return host == "reddit.com" or host.endswith(".reddit.com")

    def account_slug(self, account_url: str) -> str:
        return account_slug_from_url(account_url)

    def collect_post_links(self, page: Page, account_url: str) -> list[str]:
        links: list[str] = []
        seen: set[str] = set()
        self._append_links_from_page(page, account_url, seen, links)

        if not links and "/user/" in (urlparse(account_url).path or ""):
            submitted_url = account_url.rstrip("/") + "/submitted/"
            try:
                page.goto(submitted_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1000)
                self._append_links_from_page(page, submitted_url, seen, links)
            except Exception:
                pass

        if not links:
            for fallback_url in self._fallback_account_urls(account_url):
                try:
                    page.goto(fallback_url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(1500)
                    self._append_links_from_page(page, fallback_url, seen, links)
                except Exception:
                    continue
                if links:
                    break

        if not links and self._is_network_block_page(page):
            print(
                "[reddit] WARNING: Reddit returned a network security block page. "
                "No post links could be collected."
            )

        return links

    def wait_for_post_ready(self, post_page: Page) -> None:
        try:
            post_page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        post_page.wait_for_timeout(1000)

    def is_valid_post_url(self, resolved_url: str, account_url: str) -> bool:
        del account_url
        return "/comments/" in urlparse(resolved_url).path

    def capture_skip_reason(self, post_page: Page, account_url: str) -> Optional[str]:
        del post_page, account_url
        return None

    def extract_post_datetime(self, post_page: Page) -> Optional[datetime]:
        return extract_datetime_from_page(post_page)

    def _append_links_from_page(
        self, page: Page, base_url: str, seen: set[str], out_links: list[str]
    ) -> None:
        candidates = page.evaluate(
            """
            () => {
              const values = [];
              for (const a of document.querySelectorAll('a[href]')) {
                const href = a.getAttribute('href');
                if (href) values.push(href);
              }
              for (const el of document.querySelectorAll('[permalink], [data-permalink]')) {
                const p1 = el.getAttribute('permalink');
                const p2 = el.getAttribute('data-permalink');
                if (p1) values.push(p1);
                if (p2) values.push(p2);
              }
              return values;
            }
            """
        )

        for href in candidates:
            full = urljoin(base_url, href)
            parsed = urlparse(full)
            if "/comments/" not in parsed.path:
                continue
            normalized = f"https://www.reddit.com{parsed.path}"
            if normalized not in seen:
                seen.add(normalized)
                out_links.append(normalized)

    def _fallback_account_urls(self, account_url: str) -> list[str]:
        parsed = urlparse(account_url)
        if not parsed.netloc:
            return []

        fallbacks: list[str] = []
        old_url = parsed._replace(netloc="old.reddit.com").geturl()
        if old_url != account_url:
            fallbacks.append(old_url)

        path = parsed.path or ""
        if "/user/" in path and not path.rstrip("/").endswith("/submitted"):
            old_submitted = parsed._replace(
                netloc="old.reddit.com",
                path=path.rstrip("/") + "/submitted/",
            ).geturl()
            if old_submitted not in fallbacks:
                fallbacks.append(old_submitted)
        return fallbacks

    def _is_network_block_page(self, page: Page) -> bool:
        try:
            body_text = page.locator("body").inner_text(timeout=3000)
        except Exception:
            return False
        normalized = " ".join(body_text.split()).lower()
        return "you've been blocked by network security" in normalized
