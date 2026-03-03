from __future__ import annotations
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urljoin, urlparse

from magpie.utils import account_slug_from_url, extract_datetime_from_page

if TYPE_CHECKING:
    from playwright.sync_api import Page
else:
    Page = Any


class InstagramAdapter:
    name = "instagram"

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
        return extract_datetime_from_page(post_page)

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
