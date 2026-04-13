from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urljoin, urlparse

from magpie.utils import account_slug_from_url, extract_datetime_from_page, parse_datetime

if TYPE_CHECKING:
    from playwright.sync_api import Page
else:
    Page = Any


class XAdapter:
    name = "x"
    _STATUS_RE = re.compile(r"^/([^/]+)/status/(\d+)(?:/.*)?$")
    _I_STATUS_RE = re.compile(r"^/i/status/(\d+)(?:/.*)?$")
    _SNOWFLAKE_EPOCH_MS = 1288834974657

    def is_supported(self, account_url: str) -> bool:
        host = (urlparse(account_url).hostname or "").lower()
        return host == "x.com" or host.endswith(".x.com")

    def account_slug(self, account_url: str) -> str:
        return account_slug_from_url(account_url)

    def profile_post_datetime(self, post_url: str) -> Optional[datetime]:
        return self._datetime_from_status_url(post_url)

    def collect_post_links(self, page: Page, account_url: str) -> list[str]:
        target_user = self._account_user_from_url(account_url)
        hrefs = page.eval_on_selector_all(
            "a[href]", "els => els.map(el => el.getAttribute('href')).filter(Boolean)"
        )
        links: list[str] = []
        seen: set[str] = set()
        for href in hrefs:
            full = urljoin(account_url, href)
            parsed = urlparse(full)
            if "/status/" not in parsed.path:
                continue
            normalized = self._normalize_status_url(parsed.path, target_user)
            if normalized is None:
                continue
            if normalized not in seen:
                seen.add(normalized)
                links.append(normalized)
        return links

    def wait_for_post_ready(self, post_page: Page) -> None:
        if self._has_embed_surface(post_page):
            post_page.wait_for_timeout(1200)
            return
        try:
            post_page.wait_for_function(
                """
                () => {
                  const hasTweet = !!document.querySelector(
                    "article [data-testid='tweetText'], article div[lang], article time"
                  );
                  const hasSpinner = document.querySelectorAll("[role='progressbar']").length > 0;
                  return hasTweet && !hasSpinner;
                }
                """,
                timeout=15000,
            )
        except Exception:
            pass
        post_page.wait_for_timeout(1200)

    def is_valid_post_url(self, resolved_url: str, account_url: str) -> bool:
        path = urlparse(resolved_url).path
        target_user = self._account_user_from_url(account_url)
        return self._normalize_status_url(path, target_user) is not None

    def capture_skip_reason(self, post_page: Page, account_url: str) -> Optional[str]:
        del post_page, account_url
        return None

    def extract_post_datetime(self, post_page: Page) -> Optional[datetime]:
        dt = extract_datetime_from_page(post_page)
        if dt is not None:
            return dt
        return self._datetime_from_status_url(post_page.url)

    def build_capture_html(self, post_url: str) -> str:
        embed_url = self._embed_permalink(post_url)
        escaped_url = html.escape(embed_url, quote=True)
        return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      body {{
        margin: 0;
        min-height: 100vh;
        background: #f7f9fa;
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
      blockquote.twitter-tweet {{
        width: 100% !important;
      }}
    </style>
  </head>
  <body>
    <div class="frame">
      <blockquote class="twitter-tweet" data-dnt="true">
        <a href="{escaped_url}">{escaped_url}</a>
      </blockquote>
    </div>
    <script async src="https://platform.twitter.com/widgets.js"></script>
  </body>
</html>
"""

    def render_post_for_capture(self, post_page: Page, post_url: str) -> None:
        post_page.set_content(self.build_capture_html(post_url), wait_until="domcontentloaded")
        post_page.wait_for_function(
            """
            () => !!(
              window.twttr &&
              window.twttr.widgets &&
              typeof window.twttr.widgets.load === 'function'
            )
            """,
            timeout=10000,
        )
        post_page.evaluate("() => window.twttr.widgets.load()")
        post_page.wait_for_function(
            """
            () => {
              const iframe = document.querySelector('iframe[src*="twitter.com"][title], iframe[src*="platform.twitter.com"]');
              if (iframe) {
                const rect = iframe.getBoundingClientRect();
                if (rect.width > 250 && rect.height > 250) return true;
              }
              const text = (document.body && document.body.innerText || '').trim();
              return text.includes('View on X') || text.includes('View on Twitter');
            }
            """,
            timeout=15000,
        )
        post_page.wait_for_timeout(1200)

    def visible_timeline_dates(self, page: Page) -> list[datetime]:
        raw_values = page.eval_on_selector_all(
            "article time[datetime]",
            "els => els.map(el => el.getAttribute('datetime')).filter(Boolean)",
        )
        dates: list[datetime] = []
        for raw in raw_values:
            dt = parse_datetime(raw)
            if dt is not None:
                dates.append(dt)
        return dates

    def _normalize_status_url(self, path: str, target_user: Optional[str]) -> Optional[str]:
        if "/analytics" in path:
            return None
        i_match = self._I_STATUS_RE.match(path)
        if i_match:
            if target_user is not None:
                return None
            status_id = i_match.group(1)
            return f"https://x.com/i/status/{status_id}"
        match = self._STATUS_RE.match(path)
        if match is None:
            return None
        user, status_id = match.groups()
        normalized_user = user.lower()
        if target_user is not None and normalized_user != target_user:
            return None
        return f"https://x.com/{normalized_user}/status/{status_id}"

    def _datetime_from_status_url(self, url: str) -> Optional[datetime]:
        path = urlparse(url).path
        status_id: Optional[str] = None

        i_match = self._I_STATUS_RE.match(path)
        if i_match:
            status_id = i_match.group(1)
        else:
            match = self._STATUS_RE.match(path)
            if match is not None:
                status_id = match.group(2)

        if status_id is None:
            return None

        try:
            raw_id = int(status_id)
        except ValueError:
            return None
        timestamp_ms = (raw_id >> 22) + self._SNOWFLAKE_EPOCH_MS
        return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)

    def _account_user_from_url(self, account_url: str) -> Optional[str]:
        path_parts = [part for part in urlparse(account_url).path.split("/") if part]
        if not path_parts:
            return None
        return path_parts[0].lower()

    def _embed_permalink(self, post_url: str) -> str:
        parsed = urlparse(post_url)
        path = parsed.path
        normalized = self._normalize_status_url(path, None)
        if normalized is None:
            return post_url.replace("x.com", "twitter.com")
        twitter_path = urlparse(normalized).path
        return f"https://twitter.com{twitter_path}"

    def _has_embed_surface(self, post_page: Page) -> bool:
        try:
            return bool(
                post_page.evaluate(
                    """
                    () => {
                      const iframe = document.querySelector('iframe[src*="twitter.com"][title], iframe[src*="platform.twitter.com"]');
                      if (iframe) {
                        const rect = iframe.getBoundingClientRect();
                        if (rect.width > 250 && rect.height > 250) return true;
                      }
                      const text = (document.body && document.body.innerText || '').trim();
                      return text.includes('View on X') || text.includes('View on Twitter');
                    }
                    """
                )
            )
        except Exception:
            return False
