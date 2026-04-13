from __future__ import annotations

import html
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlencode, urljoin, urlparse

from magpie.utils import account_slug_from_url, extract_datetime_from_page

if TYPE_CHECKING:
    from playwright.sync_api import Page
else:
    Page = Any


class RedditAdapter:
    name = "reddit"
    dedupe_screenshots = False

    def __init__(self) -> None:
        self._listing_datetimes_by_url: dict[str, datetime] = {}

    def is_supported(self, account_url: str) -> bool:
        host = (urlparse(account_url).hostname or "").lower()
        return host == "reddit.com" or host.endswith(".reddit.com")

    def account_slug(self, account_url: str) -> str:
        return account_slug_from_url(account_url)

    def collect_post_links(self, page: Page, account_url: str) -> list[str]:
        self._listing_datetimes_by_url = {}
        links = self._collect_post_links_from_listing_api(page, account_url, None)
        if links:
            return links

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

    def collect_post_links_since(
        self, page: Page, account_url: str, start_date: Optional[date]
    ) -> list[str]:
        self._listing_datetimes_by_url = {}
        links = self._collect_post_links_from_listing_api(page, account_url, start_date)
        if links:
            return links
        return self.collect_post_links(page, account_url)

    def profile_post_datetime(self, post_url: str) -> Optional[datetime]:
        return self._listing_datetimes_by_url.get(post_url)

    def wait_for_post_ready(self, post_page: Page) -> None:
        if self._has_embed_surface(post_page):
            post_page.wait_for_timeout(1200)
            return
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

    def build_capture_html(self, post_url: str) -> str:
        escaped_url = html.escape(post_url, quote=True)
        return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      body {{
        margin: 0;
        min-height: 100vh;
        background: #dae0e6;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 24px;
      }}
      .frame {{
        width: min(760px, 100%);
        display: flex;
        justify-content: center;
      }}
      blockquote.reddit-embed-bq {{
        width: 100% !important;
      }}
    </style>
  </head>
  <body>
    <div class="frame">
      <blockquote class="reddit-embed-bq" data-embed-height="740">
        <a href="{escaped_url}">{escaped_url}</a>
      </blockquote>
    </div>
    <script async src="https://embed.reddit.com/widgets.js" charset="UTF-8"></script>
  </body>
</html>
"""

    def render_post_for_capture(self, post_page: Page, post_url: str) -> None:
        post_page.set_content(self.build_capture_html(post_url), wait_until="domcontentloaded")
        post_page.wait_for_function(
            """
            () => {
              const hasScript = !!document.querySelector('script[src*="embed.reddit.com/widgets.js"]');
              if (!hasScript) return false;
              const iframe = document.querySelector('iframe[src*="redditmedia.com"], iframe[src*="reddit.com"]');
              if (iframe) {
                const rect = iframe.getBoundingClientRect();
                if (rect.width > 250 && rect.height > 250) return true;
              }
              const text = (document.body && document.body.innerText || '').trim();
              return text.includes('View on Reddit') || text.includes('Comment as');
            }
            """,
            timeout=15000,
        )
        iframe_src = self._embed_iframe_src(post_page)
        if iframe_src:
            post_page.goto(iframe_src, wait_until="domcontentloaded", timeout=30000)
            try:
                post_page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            try:
                post_page.wait_for_function(
                    """
                    () => {
                      const body = document.body;
                      if (!body) return false;
                      const text = (body.innerText || '').trim();
                      if (text.length < 80) return false;
                      return (
                        !!document.querySelector('h1, article, shreddit-post, faceplate-screen-reader-content') ||
                        text.includes('comments') ||
                        text.includes('Posted by')
                      );
                    }
                    """,
                    timeout=15000,
                )
            except Exception:
                pass
            post_page.wait_for_timeout(3000)
            return

        self._wait_for_embed_frame_content(post_page)
        post_page.wait_for_timeout(5000)

    def capture_png(self, post_page: Page) -> bytes:
        frame = post_page.locator(".frame")
        try:
            frame.wait_for(state="visible", timeout=10000)
            return frame.screenshot(type="png")
        except Exception:
            return post_page.screenshot(full_page=False, type="png")

    def _collect_post_links_from_listing_api(
        self, page: Page, account_url: str, start_date: Optional[date]
    ) -> list[str]:
        endpoint = self._listing_json_endpoint(account_url)
        if endpoint is None:
            return []

        links: list[str] = []
        seen: set[str] = set()
        after: Optional[str] = None

        for _ in range(25):
            params = {"limit": "100", "raw_json": "1"}
            if after:
                params["after"] = after
            url = f"{endpoint}?{urlencode(params)}"
            try:
                resp = page.context.request.get(
                    url,
                    headers={"referer": account_url, "accept": "application/json"},
                    timeout=20000,
                )
                if not resp.ok:
                    break
                payload = resp.json()
            except Exception:
                break

            data = payload.get("data") if isinstance(payload, dict) else None
            children = data.get("children") if isinstance(data, dict) else None
            if not isinstance(children, list) or not children:
                break

            oldest_date_on_page: Optional[date] = None
            page_added = 0
            for child in children:
                child_data = child.get("data") if isinstance(child, dict) else None
                if not isinstance(child_data, dict):
                    continue
                permalink = child_data.get("permalink")
                if not isinstance(permalink, str) or "/comments/" not in permalink:
                    continue
                normalized = self._normalize_permalink(permalink)
                created = child_data.get("created_utc")
                dt = self._datetime_from_created_utc(created)
                if dt is not None:
                    self._listing_datetimes_by_url[normalized] = dt
                    created_date = dt.date()
                    if oldest_date_on_page is None or created_date < oldest_date_on_page:
                        oldest_date_on_page = created_date
                if normalized in seen:
                    continue
                seen.add(normalized)
                links.append(normalized)
                page_added += 1

            after = data.get("after") if isinstance(data, dict) else None
            if not after or page_added == 0:
                break
            if start_date is not None and oldest_date_on_page is not None and oldest_date_on_page < start_date:
                break

        return links

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

    def _listing_json_endpoint(self, account_url: str) -> Optional[str]:
        parsed = urlparse(account_url)
        path = parsed.path.rstrip("/")
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2 and parts[0] == "r":
            subreddit = parts[1]
            return f"https://www.reddit.com/r/{subreddit}/new.json"
        if len(parts) >= 2 and parts[0] == "user":
            username = parts[1]
            return f"https://www.reddit.com/user/{username}/submitted.json"
        return None

    def _normalize_permalink(self, permalink: str) -> str:
        parsed = urlparse(urljoin("https://www.reddit.com", permalink))
        return f"https://www.reddit.com{parsed.path}"

    def _datetime_from_created_utc(self, value: Any) -> Optional[datetime]:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        return None

    def _wait_for_embed_frame_content(self, post_page: Page) -> None:
        iframe = post_page.wait_for_selector(
            'iframe[src*="embed.reddit.com"], iframe[src*="redditmedia.com"], iframe[src*="reddit.com"]',
            timeout=15000,
        )
        if iframe is None:
            return

        frame = None
        for _ in range(20):
            try:
                frame = iframe.content_frame()
            except Exception:
                frame = None
            if frame is not None and frame.url and frame.url != "about:blank":
                break
            post_page.wait_for_timeout(500)

        if frame is None:
            return

        try:
            frame.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        try:
            frame.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        try:
            frame.wait_for_function(
                """
                () => {
                  const body = document.body;
                  if (!body) return false;
                  const text = (body.innerText || '').trim();
                  if (text.length < 80) return false;
                  const hasPostText =
                    !!document.querySelector('h1, article, shreddit-post, faceplate-screen-reader-content');
                  return hasPostText || text.includes('comments') || text.includes('Posted by');
                }
                """,
                timeout=15000,
            )
        except Exception:
            pass

    def _embed_iframe_src(self, post_page: Page) -> Optional[str]:
        iframe = post_page.query_selector(
            'iframe[src*="embed.reddit.com"], iframe[src*="redditmedia.com"], iframe[src*="reddit.com"]'
        )
        if iframe is None:
            return None
        return iframe.get_attribute("src")

    def _has_embed_surface(self, post_page: Page) -> bool:
        try:
            return bool(
                post_page.evaluate(
                    """
                    () => {
                      const iframe = document.querySelector('iframe[src*="redditmedia.com"], iframe[src*="reddit.com"]');
                      if (iframe) {
                        const rect = iframe.getBoundingClientRect();
                        if (rect.width > 250 && rect.height > 250) return true;
                      }
                      const text = (document.body && document.body.innerText || '').trim();
                      return text.includes('View on Reddit') || text.includes('Comment as');
                    }
                    """
                )
            )
        except Exception:
            return False

    def _is_network_block_page(self, page: Page) -> bool:
        try:
            body_text = page.locator("body").inner_text(timeout=3000)
        except Exception:
            return False
        normalized = " ".join(body_text.split()).lower()
        return "you've been blocked by network security" in normalized
