from __future__ import annotations
import base64
import html
import re
from dataclasses import dataclass
from datetime import datetime
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

    def is_supported(self, account_url: str) -> bool:
        host = (urlparse(account_url).hostname or "").lower()
        return host == "instagram.com" or host.endswith(".instagram.com")

    def account_slug(self, account_url: str) -> str:
        return account_slug_from_url(account_url)

    def collect_post_links(self, page: Page, account_url: str) -> list[str]:
        target_user = self._account_user_from_url(account_url)
        links: list[str] = []
        seen: set[str] = set()

        self._append_links_from_page(page, account_url, target_user, seen, links)
        if links:
            return links

        self._append_links_from_profile_api(page, target_user, seen, links)
        if links:
            return links

        if self._dismiss_login_wall(page):
            page.wait_for_timeout(1200)
            page.mouse.wheel(0, 2200)
            page.wait_for_timeout(1200)
            self._append_links_from_page(page, account_url, target_user, seen, links)
            if links:
                return links
            self._append_links_from_profile_api(page, target_user, seen, links)
        return links

    def wait_for_post_ready(self, post_page: Page) -> None:
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
        dt = extract_datetime_from_page(post_page)
        if dt is not None:
            return dt
        return self._datetime_from_meta_description(post_page)

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

        for post_url in self._post_urls_from_profile_payload(payload, target_user):
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

    def _post_urls_from_profile_payload(
        self, payload: Any, target_user: str
    ) -> list[str]:
        urls: list[str] = []
        nodes = []

        user = None
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                user = data.get("user")
            if user is None:
                user = payload.get("user")

        if not isinstance(user, dict):
            return urls

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
            urls.append(f"https://www.instagram.com/{target_user}/{kind}/{shortcode}/")

        return urls
