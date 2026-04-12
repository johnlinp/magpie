from __future__ import annotations
import base64
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urljoin, urlparse

from magpie.utils import account_slug_from_url, extract_datetime_from_page, parse_datetime

if TYPE_CHECKING:
    from playwright.sync_api import Page
else:
    Page = Any


@dataclass(frozen=True)
class InstagramFallbackData:
    canonical_url: str
    description: str
    image_url: str
    published_at: Optional[datetime]
    title: str


class InstagramAdapter:
    name = "instagram"
    _DESCRIPTION_DATE_RE = re.compile(
        r"\bon\s+([A-Z][a-z]+ \d{1,2}, \d{4})(?::|$|\s)",
        re.IGNORECASE,
    )
    _ANY_DATE_RE = re.compile(
        r"\b([A-Z][a-z]+ \d{1,2}, \d{4})\b",
        re.IGNORECASE,
    )

    def __init__(self) -> None:
        self._profile_datetimes_by_url: dict[str, datetime] = {}

    def is_supported(self, account_url: str) -> bool:
        host = (urlparse(account_url).hostname or "").lower()
        return host == "instagram.com" or host.endswith(".instagram.com")

    def account_slug(self, account_url: str) -> str:
        return account_slug_from_url(account_url)

    def collect_post_links(self, page: Page, account_url: str) -> list[str]:
        self._profile_datetimes_by_url = {}
        target_user = self._account_user_from_url(account_url)
        links: list[str] = []
        seen: set[str] = set()

        self._append_links_from_page(page, account_url, target_user, seen, links)
        self._append_links_from_profile_api(page, target_user, seen, links)
        if links:
            return links

        if self._dismiss_login_wall(page):
            page.wait_for_timeout(1200)
            page.mouse.wheel(0, 2200)
            page.wait_for_timeout(1200)
            self._append_links_from_page(page, account_url, target_user, seen, links)
            self._append_links_from_profile_api(page, target_user, seen, links)
        return links

    def profile_post_datetime(self, post_url: str) -> Optional[datetime]:
        return self._profile_datetimes_by_url.get(post_url)

    def wait_for_post_ready(self, post_page: Page) -> None:
        if self._has_embed_surface(post_page):
            try:
                post_page.wait_for_function(
                    """
                    () => {
                      const block = document.querySelector('blockquote.instagram-media');
                      const iframe = document.querySelector('iframe[src*="instagram.com"][src*="/embed/"]');
                      const text = (document.body && document.body.innerText || '').trim();
                      if (iframe) {
                        const rect = iframe.getBoundingClientRect();
                        if (rect.width > 200 && rect.height > 300) return true;
                      }
                      if (block) {
                        const rect = block.getBoundingClientRect();
                        const blockText = (block.innerText || '').trim();
                        if (rect.height > 200 || blockText.length > 40) return true;
                      }
                      return (
                        text.includes('View profile') ||
                        text.includes('View more on Instagram') ||
                        text.includes('Like') ||
                        text.includes('comment')
                      );
                    }
                    """,
                    timeout=10000,
                )
            except Exception:
                pass
            post_page.wait_for_timeout(1200)
            return
        try:
            post_page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        self._dismiss_signup_modal(post_page)
        post_page.wait_for_timeout(1200)
        self._dismiss_signup_modal(post_page)

    def is_valid_post_url(self, resolved_url: str, account_url: str) -> bool:
        target_user = self._account_user_from_url(account_url)
        path = urlparse(resolved_url).path
        return self._normalize_permalink(path, target_user) is not None

    def capture_skip_reason(self, post_page: Page, account_url: str) -> Optional[str]:
        del account_url
        if self._has_embed_surface(post_page):
            return None
        if "/accounts/login/" in post_page.url:
            return "redirected to login"
        html = ""
        body_text = ""
        try:
            html = post_page.content().lower()
        except Exception:
            html = ""
        try:
            body_text = post_page.locator("body").inner_text(timeout=2000).lower()
        except Exception:
            body_text = ""

        combined = f"{html}\n{body_text}"
        if "log into instagram" in combined and "forgot password" in combined:
            return "inline login wall"
        if "create new account" in combined and "log in with facebook" in combined:
            return "inline login wall"
        if 'id="login_form"' in combined or 'name="username"' in combined:
            return "inline login wall"
        if "see more from" in combined and "sign up for instagram" in combined:
            return "signup modal overlay"
        return None

    def extract_post_datetime(self, post_page: Page) -> Optional[datetime]:
        embed_block = self._embed_blockquote(post_page)
        if self._has_embed_surface(post_page):
            dt = extract_datetime_from_page(post_page)
            if dt is not None:
                return dt
            dt = self._datetime_from_embed_iframe_src(post_page)
            if dt is not None:
                return dt
            dt = self._datetime_from_accessibility_tree(post_page)
            if dt is not None:
                return dt
            if embed_block is not None:
                try:
                    debug = {
                        "blockquote_text": embed_block.inner_text(timeout=3000)[:500],
                        "blockquote_html": embed_block.evaluate("el => el.innerHTML.slice(0, 1000)"),
                        "time_count": post_page.locator("blockquote.instagram-media time").count(),
                    }
                    print(f"[instagram] DEBUG: embed datetime state: {debug}")
                except Exception:
                    pass
        dt = extract_datetime_from_page(post_page)
        if dt is not None:
            return dt
        return self._datetime_from_meta_description(post_page)

    def render_post_for_capture(self, post_page: Page, post_url: str) -> None:
        html_doc = self.build_capture_html(post_url)
        post_page.set_content(html_doc, wait_until="domcontentloaded")
        try:
            post_page.wait_for_function(
                """
                () => {
                  return !!(
                    window.instgrm &&
                    window.instgrm.Embeds &&
                    typeof window.instgrm.Embeds.process === 'function'
                  );
                }
                """,
                timeout=10000,
            )
            post_page.evaluate(
                """
                () => {
                  window.instgrm.Embeds.process();
                }
                """
            )
            post_page.wait_for_function(
                """
                () => {
                  const block = document.querySelector('blockquote.instagram-media');
                  const iframe = document.querySelector('iframe[src*="instagram.com"][src*="/embed/"]');
                  const bodyText = (document.body && document.body.innerText || '').trim();
                  if (iframe) {
                    const rect = iframe.getBoundingClientRect();
                    if (rect.width > 200 && rect.height > 300) return true;
                  }
                  if (!block) return false;
                  const blockText = (block.innerText || '').trim();
                  const html = block.innerHTML || '';
                  const rect = block.getBoundingClientRect();
                  return (
                    block.classList.contains('instagram-media-rendered') ||
                    html.includes('<iframe') ||
                    blockText.length > 40 ||
                    rect.height > 200 ||
                    bodyText.includes('View profile') ||
                    bodyText.includes('View more on Instagram')
                  );
                }
                """,
                timeout=15000,
            )
        except Exception:
            debug = post_page.evaluate(
                """
                () => {
                  const block = document.querySelector('blockquote.instagram-media');
                  const iframeInBlock = block ? block.querySelector('iframe') : null;
                  const anyIframe = document.querySelector('iframe');
                  return {
                    hasWindowInstgrm: !!window.instgrm,
                    hasEmbeds: !!(window.instgrm && window.instgrm.Embeds),
                    hasProcess: !!(
                      window.instgrm &&
                      window.instgrm.Embeds &&
                      typeof window.instgrm.Embeds.process === 'function'
                    ),
                    blockExists: !!block,
                    blockClass: block ? block.className : null,
                    blockChildCount: block ? block.children.length : null,
                    blockRect: block ? {
                      width: block.getBoundingClientRect().width,
                      height: block.getBoundingClientRect().height
                    } : null,
                    iframeInBlock: !!iframeInBlock,
                    iframeInBlockSrc: iframeInBlock ? iframeInBlock.getAttribute('src') : null,
                    anyIframe: !!anyIframe,
                    anyIframeSrc: anyIframe ? anyIframe.getAttribute('src') : null,
                    bodyText: (document.body && document.body.innerText || '').slice(0, 500),
                    blockText: block ? (block.innerText || '').slice(0, 500) : null,
                    blockHtml: block ? (block.innerHTML || '').slice(0, 1000) : null,
                  };
                }
                """
            )
            print(f"[instagram] DEBUG: embed render state for {post_url}: {debug}")
            raise
        post_page.wait_for_timeout(1200)

    def build_capture_html(self, post_url: str) -> str:
        return self._embed_html(self._embed_permalink(post_url))

    def prepare_capture_fallback(
        self, post_page: Page, resolved_url: str
    ) -> Optional[InstagramFallbackData]:
        try:
            page_html = post_page.content()
        except Exception:
            return None

        image_url = (
            self._meta_content_from_html(page_html, "property", "og:image")
            or self._meta_content_from_html(page_html, "name", "twitter:image")
        )
        description = (
            self._meta_content_from_html(page_html, "name", "description")
            or self._meta_content_from_html(page_html, "property", "og:description")
            or ""
        )
        title = (
            self._meta_content_from_html(page_html, "property", "og:title")
            or self._meta_content_from_html(page_html, "name", "twitter:title")
            or "Instagram post"
        )
        canonical_url = (
            self._meta_content_from_html(page_html, "property", "og:url")
            or resolved_url
        )

        if not image_url:
            return None

        return InstagramFallbackData(
            canonical_url=canonical_url,
            description=html.unescape(description),
            image_url=html.unescape(image_url),
            published_at=self._datetime_from_description(description),
            title=html.unescape(title),
        )

    def render_capture_fallback(
        self, post_page: Page, fallback: InstagramFallbackData
    ) -> bool:
        published_at_iso = fallback.published_at.isoformat() if fallback.published_at else ""
        escaped_title = self._html_escape(fallback.title)
        escaped_description = self._html_escape(fallback.description)
        escaped_url = self._html_escape(fallback.canonical_url)
        embedded_image_url = self._embed_image_data_url(post_page, fallback.image_url)
        escaped_image = self._html_escape(embedded_image_url or fallback.image_url)
        escaped_date = self._html_escape(
            fallback.published_at.strftime("%Y-%m-%d") if fallback.published_at else "Unknown date"
        )
        html = f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta property="article:published_time" content="{published_at_iso}">
    <title>{escaped_title}</title>
    <style>
      :root {{
        color-scheme: light;
        --bg: #fafafa;
        --panel: #ffffff;
        --text: #111111;
        --muted: #6b7280;
        --border: #e5e7eb;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        min-height: 100vh;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        color: var(--text);
        background:
          radial-gradient(circle at top left, #ffe7d6 0, transparent 30%),
          radial-gradient(circle at top right, #e6f0ff 0, transparent 28%),
          var(--bg);
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 32px;
      }}
      .card {{
        width: min(1100px, 100%);
        display: grid;
        grid-template-columns: minmax(0, 1.25fr) minmax(320px, 0.75fr);
        gap: 24px;
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 24px;
        box-shadow: 0 20px 60px rgba(0, 0, 0, 0.08);
        overflow: hidden;
      }}
      .media {{
        background: #f3f4f6;
        min-height: 720px;
        display: flex;
        align-items: center;
        justify-content: center;
      }}
      .media img {{
        width: 100%;
        height: 100%;
        object-fit: contain;
        display: block;
        background: #f3f4f6;
      }}
      .meta {{
        padding: 32px 28px;
        display: flex;
        flex-direction: column;
        gap: 18px;
      }}
      .eyebrow {{
        font-size: 13px;
        font-weight: 700;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: #e1306c;
      }}
      h1 {{
        margin: 0;
        font-size: 30px;
        line-height: 1.15;
      }}
      .date {{
        font-size: 14px;
        color: var(--muted);
      }}
      .description {{
        font-size: 16px;
        line-height: 1.6;
        color: #1f2937;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
      }}
      .url {{
        margin-top: auto;
        font-size: 14px;
        color: var(--muted);
        overflow-wrap: anywhere;
      }}
    </style>
  </head>
  <body>
    <article class="card">
      <section class="media">
        <img src="{escaped_image}" alt="Instagram post preview">
      </section>
      <section class="meta">
        <div class="eyebrow">Instagram Preview</div>
        <h1>{escaped_title}</h1>
        <div class="date">{escaped_date}</div>
        <div class="description">{escaped_description}</div>
        <div class="url">{escaped_url}</div>
      </section>
    </article>
  </body>
</html>
"""
        try:
            post_page.set_content(html, wait_until="domcontentloaded")
            post_page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            return False
        return True

    def _render_embed_capture(
        self, post_page: Page, fallback: InstagramFallbackData
    ) -> bool:
        try:
            self.render_post_for_capture(post_page, fallback.canonical_url)
        except Exception:
            return False
        return True

    def _embed_html(self, post_url: str) -> str:
        escaped_url = self._html_escape(post_url)
        return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      body {{
        margin: 0;
        min-height: 100vh;
        background: #fafafa;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 24px;
      }}
      .frame {{
        width: min(720px, 100%);
        display: flex;
        justify-content: center;
      }}
      blockquote.instagram-media {{
        min-width: 320px !important;
        max-width: 658px !important;
        width: 100% !important;
      }}
    </style>
  </head>
  <body>
    <div class="frame">
      <blockquote
        class="instagram-media"
        data-instgrm-permalink="{escaped_url}"
        data-instgrm-version="14"
      >
        <a href="{escaped_url}">View this post on Instagram</a>
      </blockquote>
    </div>
    <script async src="https://www.instagram.com/embed.js"></script>
  </body>
</html>
"""

    def _embed_permalink(self, post_url: str) -> str:
        parts = [part for part in urlparse(post_url).path.split("/") if part]
        if len(parts) < 2:
            return post_url
        if parts[0].lower() in {"p", "reel"}:
            kind, post_id = parts[0].lower(), parts[1]
        elif len(parts) >= 3 and parts[1].lower() in {"p", "reel"}:
            kind, post_id = parts[1].lower(), parts[2]
        else:
            return post_url
        return f"https://www.instagram.com/{kind}/{post_id}/"

    def _datetime_from_embed_iframe_src(self, post_page: Page) -> Optional[datetime]:
        iframe = post_page.query_selector('iframe[src*="instagram.com"][src*="/embed/"]')
        if iframe is None:
            return None
        iframe_src = iframe.get_attribute("src")
        if not iframe_src:
            return None
        try:
            resp = post_page.context.request.get(
                iframe_src,
                headers={"referer": "https://www.instagram.com/"},
                timeout=15000,
            )
        except Exception:
            return None
        if not resp.ok:
            return None
        try:
            payload = resp.text()
        except Exception:
            return None
        try:
            Path("/tmp/magpie-instagram-last-embed.html").write_text(payload, encoding="utf-8")
        except Exception:
            pass

        meta_candidates = [
            self._meta_content_from_html(payload, "property", "article:published_time"),
            self._meta_content_from_html(payload, "name", "description"),
            self._meta_content_from_html(payload, "property", "og:description"),
        ]
        for value in meta_candidates:
            dt = parse_datetime(value) if value else None
            if dt is not None:
                return dt
            dt = self._datetime_from_description(value or "")
            if dt is not None:
                return dt

        dt = self._datetime_from_serverjs_payload(payload)
        if dt is not None:
            return dt

        match = re.search(r'time datetime=["\\\']([^"\\\']+)["\\\']', payload, re.IGNORECASE)
        if match is not None:
            dt = parse_datetime(match.group(1))
            if dt is not None:
                return dt

        timestamp_patterns = [
            r'"taken_at_timestamp"\s*:\s*(\d{10})',
            r'"taken_at"\s*:\s*(\d{10})',
            r'"datePublished"\s*:\s*"([^"]+)"',
            r'"uploadDate"\s*:\s*"([^"]+)"',
            r'"dateCreated"\s*:\s*"([^"]+)"',
            r'"created_at"\s*:\s*"([^"]+)"',
            r'"created_time"\s*:\s*"([^"]+)"',
        ]
        for pattern in timestamp_patterns:
            match = re.search(pattern, payload, re.IGNORECASE)
            if match is None:
                continue
            raw = match.group(1)
            if raw.isdigit() and len(raw) == 10:
                return datetime.fromtimestamp(int(raw), tz=timezone.utc)
            dt = parse_datetime(raw)
            if dt is not None:
                return dt

        match = self._ANY_DATE_RE.search(payload)
        if match is not None:
            dt = parse_datetime(match.group(1))
            if dt is not None:
                return dt
        print(
            "[instagram] DEBUG: embed iframe fetch had no date "
            f"(status={resp.status}, url={iframe_src}, "
            f"has_article_time={'article:published_time' in payload}, "
            f"has_time_tag={'time datetime' in payload}, "
            f"has_apr_2026={'2026' in payload}, "
            f"has_taken_at={'taken_at' in payload}, "
            f"has_date_published={'datePublished' in payload}, "
            f"serverjs={self._serverjs_debug_summary(payload)})"
        )
        year_idx = payload.find("2026")
        if year_idx >= 0:
            start = max(0, year_idx - 120)
            end = min(len(payload), year_idx + 160)
            print(f"[instagram] DEBUG: embed iframe year snippet: {payload[start:end]}")
        return None

    def _datetime_from_serverjs_payload(self, payload: str) -> Optional[datetime]:
        for block in self._serverjs_script_blocks(payload):
            try:
                data = json.loads(block)
            except Exception:
                continue
            dt = self._walk_serverjs_for_datetime(data)
            if dt is not None:
                return dt
        return None

    def _serverjs_script_blocks(self, payload: str) -> list[str]:
        return re.findall(
            r"<script[^>]*data-sjs[^>]*>(.*?)</script>",
            payload,
            flags=re.IGNORECASE | re.DOTALL,
        )

    def _walk_serverjs_for_datetime(self, value: Any) -> Optional[datetime]:
        if isinstance(value, dict):
            for key, child in value.items():
                dt = self._datetime_from_serverjs_field(str(key), child)
                if dt is not None:
                    return dt
                dt = self._walk_serverjs_for_datetime(child)
                if dt is not None:
                    return dt
            return None
        if isinstance(value, list):
            for child in value:
                dt = self._walk_serverjs_for_datetime(child)
                if dt is not None:
                    return dt
        return None

    def _datetime_from_serverjs_field(
        self, key: str, value: Any
    ) -> Optional[datetime]:
        lowered = key.lower()
        timestampish = {
            "taken_at",
            "taken_at_timestamp",
            "published_at",
            "publish_at",
            "datepublished",
            "uploaddate",
            "datecreated",
            "created_at",
            "created_time",
            "creation_timestamp",
            "video_upload_time",
        }
        if lowered not in timestampish:
            return None
        return self._parse_structured_datetime_value(value)

    def _parse_structured_datetime_value(self, value: Any) -> Optional[datetime]:
        if isinstance(value, str):
            if value.isdigit() and len(value) == 10:
                return datetime.fromtimestamp(int(value), tz=timezone.utc)
            return parse_datetime(value)
        if isinstance(value, int):
            if 1_000_000_000 <= value < 10_000_000_000:
                return datetime.fromtimestamp(value, tz=timezone.utc)
            return None
        if isinstance(value, float):
            int_value = int(value)
            if 1_000_000_000 <= int_value < 10_000_000_000:
                return datetime.fromtimestamp(int_value, tz=timezone.utc)
        return None

    def _serverjs_debug_summary(self, payload: str) -> str:
        blocks = self._serverjs_script_blocks(payload)
        if not blocks:
            return "blocks=0"

        http_error = 0
        embed_urls: list[str] = []
        for block in blocks:
            if "httpErrorPage" in block:
                http_error += 1
            for match in re.finditer(r'"url":"([^"]+/embed/[^"]*)"', block):
                embed_urls.append(match.group(1))

        summary = [f"blocks={len(blocks)}", f"http_error={http_error}"]
        if embed_urls:
            summary.append(f"embed_url={embed_urls[0]}")
        return ",".join(summary)

    def _datetime_from_accessibility_tree(self, post_page: Page) -> Optional[datetime]:
        try:
            snapshot = post_page.accessibility.snapshot()
        except Exception:
            return None
        if not snapshot:
            return None
        text = " ".join(self._iter_accessibility_text(snapshot))
        if not text:
            return None

        match = self._ANY_DATE_RE.search(text)
        if match is not None:
            dt = parse_datetime(match.group(1))
            if dt is not None:
                return dt

        rel_match = re.search(r"\b(\d+)\s*(s|m|h|d|w|mo|y)\b", text, re.IGNORECASE)
        if rel_match is None:
            rel_match = re.search(
                r"\b(\d+)\s*(second|minute|hour|day|week|month|year)s?\s+ago\b",
                text,
                re.IGNORECASE,
            )
        if rel_match is None:
            return None

        amount = int(rel_match.group(1))
        unit = rel_match.group(2).lower()
        if unit in {"second", "s"}:
            delta = timedelta(seconds=amount)
        elif unit in {"minute", "m"}:
            delta = timedelta(minutes=amount)
        elif unit in {"hour", "h"}:
            delta = timedelta(hours=amount)
        elif unit in {"day", "d"}:
            delta = timedelta(days=amount)
        elif unit in {"week", "w"}:
            delta = timedelta(weeks=amount)
        elif unit in {"month", "mo"}:
            delta = timedelta(days=30 * amount)
        elif unit in {"year", "y"}:
            delta = timedelta(days=365 * amount)
        else:
            return None
        return datetime.now(timezone.utc) - delta

    def _iter_accessibility_text(self, node: Any):
        if isinstance(node, dict):
            name = node.get("name")
            if isinstance(name, str) and name.strip():
                yield name.strip()
            children = node.get("children")
            if isinstance(children, list):
                for child in children:
                    yield from self._iter_accessibility_text(child)

    def _account_user_from_url(self, account_url: str) -> Optional[str]:
        parts = [part for part in urlparse(account_url).path.split("/") if part]
        if not parts:
            return None
        return parts[0].lower()

    def _normalize_permalink(self, path: str, target_user: Optional[str]) -> Optional[str]:
        parts = [part for part in path.split("/") if part]
        if len(parts) < 3:
            return None
        owner, kind, post_id = parts[0].lower(), parts[1], parts[2]
        if kind not in {"p", "reel"}:
            return None
        if target_user is not None and owner != target_user:
            return None
        return f"https://www.instagram.com/{owner}/{kind}/{post_id}/"

    def _append_links_from_page(
        self,
        page: Page,
        base_url: str,
        target_user: Optional[str],
        seen: set[str],
        out_links: list[str],
    ) -> None:
        hrefs = page.eval_on_selector_all(
            "a[href]", "els => els.map(el => el.getAttribute('href')).filter(Boolean)"
        )
        for href in hrefs:
            full = urljoin(base_url, href)
            parsed = urlparse(full)
            normalized = self._normalize_permalink(parsed.path, target_user)
            if normalized is None:
                continue
            if normalized not in seen:
                seen.add(normalized)
                out_links.append(normalized)

    def _dismiss_login_wall(self, page: Page) -> bool:
        if page.query_selector("#login_form") is None:
            return False
        selectors = [
            "div[role='dialog'] svg[aria-label='Close']",
            "div[role='dialog'] button[aria-label='Close']",
            "div[role='dialog'] button[type='button']",
        ]
        for selector in selectors:
            node = page.query_selector(selector)
            if node is None:
                continue
            try:
                node.click(timeout=1000)
                return True
            except Exception:
                continue
        try:
            page.evaluate(
                """
                () => {
                  const dialog = document.querySelector("div[role='dialog']");
                  if (dialog) dialog.remove();
                  document.body.style.overflow = "auto";
                }
                """
            )
            return True
        except Exception:
            return False

    def _dismiss_signup_modal(self, page: Page) -> None:
        selectors = [
            "button[aria-label='Close']",
            "svg[aria-label='Close']",
            "div[role='dialog'] button",
        ]
        for selector in selectors:
            node = page.query_selector(selector)
            if node is None:
                continue
            try:
                node.click(timeout=1000)
                page.wait_for_timeout(400)
                return
            except Exception:
                continue
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception:
            pass

    def _append_links_from_profile_api(
        self, page: Page, target_user: Optional[str], seen: set[str], out_links: list[str]
    ) -> None:
        if target_user is None:
            return
        endpoint = (
            f"https://www.instagram.com/api/v1/users/web_profile_info/?username={target_user}"
        )
        try:
            resp = page.context.request.get(
                endpoint,
                headers={
                    "referer": f"https://www.instagram.com/{target_user}/",
                    "x-ig-app-id": "936619743392459",
                },
                timeout=20000,
            )
            if not resp.ok:
                return
            payload = resp.json()
        except Exception:
            return

        for post_url, published_at in self._post_entries_from_profile_payload(payload, target_user):
            if published_at is not None:
                self._profile_datetimes_by_url[post_url] = published_at
            if post_url in seen:
                continue
            seen.add(post_url)
            out_links.append(post_url)

    def _datetime_from_meta_description(self, post_page: Page) -> Optional[datetime]:
        selectors = [
            "meta[name='description']",
            "meta[property='og:description']",
        ]
        for selector in selectors:
            node = post_page.query_selector(selector)
            if node is None:
                continue
            content = node.get_attribute("content") or ""
            match = self._DESCRIPTION_DATE_RE.search(content)
            if match is None:
                continue
            dt = parse_datetime(match.group(1))
            if dt is not None:
                return dt
        return None

    def _datetime_from_description(self, value: str) -> Optional[datetime]:
        match = self._DESCRIPTION_DATE_RE.search(value or "")
        if match is None:
            return None
        return parse_datetime(match.group(1))

    def _meta_content_from_html(self, html: str, attr_name: str, attr_value: str) -> Optional[str]:
        pattern = re.compile(
            rf'<meta[^>]+{attr_name}=["\']{re.escape(attr_value)}["\'][^>]+content=["\']([^"\']+)["\']',
            re.IGNORECASE,
        )
        match = pattern.search(html)
        if match is not None:
            return match.group(1)
        reverse_pattern = re.compile(
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+{attr_name}=["\']{re.escape(attr_value)}["\']',
            re.IGNORECASE,
        )
        match = reverse_pattern.search(html)
        if match is not None:
            return match.group(1)
        return None

    def _html_escape(self, value: str) -> str:
        return (
            value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    def _embed_image_data_url(self, post_page: Page, image_url: str) -> Optional[str]:
        try:
            resp = post_page.context.request.get(image_url, timeout=15000)
        except Exception:
            return None
        if not resp.ok:
            return None
        content_type = resp.headers.get("content-type", "image/jpeg")
        try:
            payload = resp.body()
        except Exception:
            return None
        encoded = base64.b64encode(payload).decode("ascii")
        return f"data:{content_type};base64,{encoded}"

    def _embed_blockquote(self, post_page: Page):
        return post_page.query_selector("blockquote.instagram-media")

    def _has_embed_surface(self, post_page: Page) -> bool:
        try:
            return bool(
                post_page.evaluate(
                    """
                    () => {
                      const block = document.querySelector('blockquote.instagram-media');
                      const iframe = document.querySelector('iframe[src*="instagram.com"][src*="/embed/"]');
                      const text = (document.body && document.body.innerText || '').trim();
                      if (iframe) {
                        const rect = iframe.getBoundingClientRect();
                        if (rect.width > 200 && rect.height > 300) return true;
                      }
                      if (block) {
                        const rect = block.getBoundingClientRect();
                        const blockText = (block.innerText || '').trim();
                        if (rect.height > 200 || blockText.length > 40) return true;
                      }
                      return (
                        text.includes('View profile') ||
                        text.includes('View more on Instagram') ||
                        text.includes('Like') ||
                        text.includes('comment')
                      );
                    }
                    """
                )
            )
        except Exception:
            return False

    def _post_entries_from_profile_payload(
        self, payload: Any, target_user: str
    ) -> list[tuple[str, Optional[datetime]]]:
        entries: list[tuple[str, Optional[datetime]]] = []
        nodes = []

        user = None
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                user = data.get("user")
            if user is None:
                user = payload.get("user")

        if not isinstance(user, dict):
            return entries

        media = user.get("edge_owner_to_timeline_media")
        if isinstance(media, dict):
            edges = media.get("edges")
            if isinstance(edges, list):
                for edge in edges:
                    if isinstance(edge, dict):
                        node = edge.get("node")
                        if isinstance(node, dict):
                            nodes.append(node)

        feed = user.get("xdt_api__v1__feed__user_timeline_graphql_connection")
        if isinstance(feed, dict):
            edges = feed.get("edges")
            if isinstance(edges, list):
                for edge in edges:
                    if isinstance(edge, dict):
                        node = edge.get("node")
                        if isinstance(node, dict):
                            nodes.append(node)

        for node in nodes:
            shortcode = node.get("shortcode") or node.get("code")
            if not isinstance(shortcode, str) or not shortcode:
                continue
            product_type = str(node.get("product_type") or "").lower()
            kind = "reel" if product_type == "clips" else "p"
            post_url = f"https://www.instagram.com/{target_user}/{kind}/{shortcode}/"
            entries.append((post_url, self._datetime_from_profile_node(node)))

        return entries

    def _datetime_from_profile_node(self, node: dict[str, Any]) -> Optional[datetime]:
        timestamp_keys = (
            "taken_at_timestamp",
            "taken_at",
            "published_at",
            "publish_at",
            "creation_timestamp",
            "video_upload_time",
        )
        for key in timestamp_keys:
            value = node.get(key)
            dt = self._parse_structured_datetime_value(value)
            if dt is not None:
                return dt

        for key in ("created_at", "created_time", "datePublished", "uploadDate", "dateCreated"):
            value = node.get(key)
            dt = self._parse_structured_datetime_value(value)
            if dt is not None:
                return dt

        return None
