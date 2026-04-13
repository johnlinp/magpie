from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence

from magpie.platforms import get_adapter_for_url
from magpie.utils import ensure_dir, in_date_range, parse_strict_date, sha256_hex, utc_date

NAV_TIMEOUT_MS = 30000
ACCOUNT_PAGE_SCROLL_DELTA_Y = 400
ACCOUNT_PAGE_SCROLL_PAUSE_MS = 1000
ACCOUNT_PAGE_MAX_SCROLLS = 40
ACCOUNT_PAGE_STALL_LIMIT = 3
ACCOUNT_PAGE_POST_BOUNDARY_BUFFER_SCROLLS = 3
HTML_SNAPSHOT_RETRIES = 3
HTML_SNAPSHOT_RETRY_DELAY_MS = 500
SCREENSHOT_WIDTH = 1600
SCREENSHOT_HEIGHT = 900
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page


@dataclass(frozen=True)
class CaptureArgs:
    accounts_path: Path
    output_dir: Path
    start_date: Optional[date]
    end_date: Optional[date]
    max_posts_per_account: int


def run_capture_cli(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    ns = parser.parse_args(argv)
    if ns.command != "capture":
        parser.print_help()
        return 1

    try:
        args = CaptureArgs(
            accounts_path=Path(ns.accounts),
            output_dir=Path(ns.output_dir),
            start_date=parse_strict_date(ns.start_date) if ns.start_date else None,
            end_date=parse_strict_date(ns.end_date) if ns.end_date else None,
            max_posts_per_account=ns.max_posts_per_account,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2

    if args.start_date and args.end_date and args.start_date > args.end_date:
        print("ERROR: --start-date must be on or before --end-date.")
        return 2

    if args.max_posts_per_account <= 0:
        print("ERROR: --max-posts-per-account must be a positive integer.")
        return 2

    if not args.accounts_path.exists():
        print(f"ERROR: Accounts file not found: {args.accounts_path}")
        return 2

    return capture_accounts(args)


def capture_accounts(args: CaptureArgs) -> int:
    accounts = _read_accounts(args.accounts_path)
    if not accounts:
        print("WARNING: No account URLs found in input file.")
        return 0

    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        print("ERROR: Playwright is not installed. Run: pip install -e .")
        return 2

    screenshots_root = args.output_dir / "screenshots"
    html_root = args.output_dir / "html"
    ensure_dir(screenshots_root)
    ensure_dir(html_root)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="en-US",
            timezone_id="UTC",
            user_agent=DEFAULT_USER_AGENT,
            viewport={"width": SCREENSHOT_WIDTH, "height": SCREENSHOT_HEIGHT},
        )
        try:
            for account_url in accounts:
                _process_account(context, account_url, args, screenshots_root, html_root)
        finally:
            context.close()
            browser.close()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="magpie")
    subparsers = parser.add_subparsers(dest="command")

    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("--accounts", required=True, help="Path to accounts file")
    capture_parser.add_argument(
        "--output-dir",
        default="./output",
        help="Directory where screenshots will be written (default: ./output)",
    )
    capture_parser.add_argument(
        "--start-date",
        help="Start date (inclusive), format YYYY/MM/DD in UTC",
    )
    capture_parser.add_argument(
        "--end-date",
        help="End date (inclusive), format YYYY/MM/DD in UTC",
    )
    capture_parser.add_argument(
        "--max-posts-per-account",
        type=int,
        default=50,
        help="Maximum screenshots saved per account (default: 50)",
    )
    return parser


def _read_accounts(accounts_path: Path) -> list[str]:
    urls: list[str] = []
    for raw in accounts_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls


def _process_account(
    context: "BrowserContext",
    account_url: str,
    args: CaptureArgs,
    screenshots_root: Path,
    html_root: Path,
) -> None:
    from playwright.sync_api import Error

    adapter = get_adapter_for_url(account_url)
    if adapter is None:
        print(f"WARNING: Unsupported account URL, skipping: {account_url}")
        return

    account_slug = adapter.account_slug(account_url)
    account_output_dir = screenshots_root / f"{adapter.name}__{account_slug}"
    account_html_dir = html_root / f"{adapter.name}__{account_slug}"
    ensure_dir(account_output_dir)
    ensure_dir(account_html_dir)

    print(f"[{adapter.name}] Account: {account_url}")
    page = context.new_page()
    try:
        page.goto(account_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        _save_html_snapshot(page, account_html_dir / "account.html", adapter.name)
        post_links = _collect_account_post_links(page, adapter, account_url, args.start_date)
    except Error as exc:
        print(f"[{adapter.name}] WARNING: Failed account page load/parse: {account_url} ({exc})")
        page.close()
        return
    finally:
        if not page.is_closed():
            page.close()

    if not post_links:
        print(f"[{adapter.name}] WARNING: No candidate post links found.")
        return

    hashes_seen: set[str] = set()
    visited_urls_seen: set[str] = set()
    saved_count = 0
    visited_count = 0
    skipped_missing_datetime = 0
    skipped_out_of_range = 0
    skipped_duplicate = 0
    post_errors = 0

    for link in post_links:
        if saved_count >= args.max_posts_per_account:
            break

        post_page = context.new_page()
        try:
            visited_count += 1
            resolved_url = link
            build_capture_html = getattr(adapter, "build_capture_html", None)
            render_post_for_capture = getattr(adapter, "render_post_for_capture", None)
            if callable(build_capture_html) and callable(render_post_for_capture):
                generated_html_name = (
                    f"post_{visited_count:03d}__{_url_slug(resolved_url)}__generated.html"
                )
                generated_html_path = account_html_dir / generated_html_name
                generated_html_path.write_text(
                    build_capture_html(resolved_url),
                    encoding="utf-8",
                )
                render_post_for_capture(post_page, resolved_url)
            else:
                post_page.route("**/accounts/login/**", lambda route: route.abort())
                post_page.goto(link, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                resolved_url = post_page.url
            if not adapter.is_valid_post_url(resolved_url, account_url):
                skipped_duplicate += 1
                print(
                    f"[{adapter.name}] WARNING: Skipped post (resolved to non-post/non-account URL): {resolved_url}"
                )
                continue
            adapter.wait_for_post_ready(post_page)
            skip_reason = adapter.capture_skip_reason(post_page, account_url)
            if skip_reason is not None:
                skipped_duplicate += 1
                print(f"[{adapter.name}] WARNING: Skipped post ({skip_reason}): {resolved_url}")
                continue
            post_html_name = f"post_{visited_count:03d}__{_url_slug(resolved_url)}.html"
            _save_html_snapshot(post_page, account_html_dir / post_html_name, adapter.name)
            if resolved_url in visited_urls_seen:
                skipped_duplicate += 1
                print(
                    f"[{adapter.name}] WARNING: Skipped post (duplicate permalink): {resolved_url}"
                )
                continue
            visited_urls_seen.add(resolved_url)
            dt = None
            profile_post_datetime = getattr(adapter, "profile_post_datetime", None)
            if callable(profile_post_datetime):
                dt = profile_post_datetime(resolved_url)
            if dt is None:
                dt = adapter.extract_post_datetime(post_page)
            post_date = utc_date(dt)
            if post_date is None:
                print(
                    f"[{adapter.name}] DEBUG: missing datetime "
                    f"(candidate={link}, resolved={resolved_url})"
                )

            if args.start_date or args.end_date:
                if post_date is None:
                    skipped_missing_datetime += 1
                    print(
                        f"[{adapter.name}] WARNING: Skipped post (missing datetime): {resolved_url}"
                    )
                    continue
                if not in_date_range(post_date, args.start_date, args.end_date):
                    skipped_out_of_range += 1
                    print(
                        f"[{adapter.name}] WARNING: Skipped post "
                        f"(date={post_date}, out of range {args.start_date}..{args.end_date}): "
                        f"{resolved_url}"
                    )
                    continue

            capture_png = getattr(adapter, "capture_png", None)
            if callable(capture_png):
                png_bytes = capture_png(post_page)
            else:
                png_bytes = post_page.screenshot(full_page=False, type="png")
            dedupe_screenshots = getattr(adapter, "dedupe_screenshots", True)
            if dedupe_screenshots:
                digest = sha256_hex(png_bytes)
                if digest in hashes_seen:
                    skipped_duplicate += 1
                    print(f"[{adapter.name}] WARNING: Skipped post (duplicate screenshot): {resolved_url}")
                    continue
                hashes_seen.add(digest)

            if post_date is None:
                date_part = "unknown"
            else:
                date_part = post_date.strftime("%Y%m%d")
            post_url_slug = _url_slug(resolved_url)
            file_name = f"{date_part}__{saved_count + 1:03d}__{post_url_slug}.png"
            out_path = account_output_dir / file_name
            out_path.write_bytes(png_bytes)
            saved_count += 1
            print(f"[{adapter.name}] saved {out_path}")
        except Error as exc:
            post_errors += 1
            print(f"[{adapter.name}] WARNING: Failed post capture: {link} ({exc})")
        finally:
            post_page.close()

    print(
        f"[{adapter.name}] done: saved={saved_count}, candidates={len(post_links)}, "
        f"visited={visited_count}, missing_datetime={skipped_missing_datetime}, "
        f"out_of_range={skipped_out_of_range}, duplicates={skipped_duplicate}, "
        f"errors={post_errors}"
    )


def _collect_account_post_links(
    page: "Page",
    adapter: object,
    account_url: str,
    start_date: Optional[date],
) -> list[str]:
    collect_post_links_since = getattr(adapter, "collect_post_links_since", None)
    if callable(collect_post_links_since):
        return collect_post_links_since(page, account_url, start_date)

    if getattr(adapter, "name", None) != "x":
        return adapter.collect_post_links(page, account_url)

    links: list[str] = []
    seen: set[str] = set()
    stalled_scrolls = 0
    previous_link_count = 0
    crossed_start_date_boundary = start_date is None
    boundary_buffer_scrolls_remaining = 0

    for _ in range(ACCOUNT_PAGE_MAX_SCROLLS + 1):
        for link in adapter.collect_post_links(page, account_url):
            if link not in seen:
                seen.add(link)
                links.append(link)

        if start_date is not None and not crossed_start_date_boundary:
            visible_dates = adapter.visible_timeline_dates(page)
            visible_utc_dates = [utc_date(dt) for dt in visible_dates]
            visible_utc_dates = [d for d in visible_utc_dates if d is not None]
            if visible_utc_dates and min(visible_utc_dates) <= start_date:
                crossed_start_date_boundary = True
                boundary_buffer_scrolls_remaining = ACCOUNT_PAGE_POST_BOUNDARY_BUFFER_SCROLLS

        if len(links) == previous_link_count:
            stalled_scrolls += 1
        else:
            stalled_scrolls = 0
            previous_link_count = len(links)

        if crossed_start_date_boundary:
            if boundary_buffer_scrolls_remaining > 0:
                boundary_buffer_scrolls_remaining -= 1
            elif stalled_scrolls >= ACCOUNT_PAGE_STALL_LIMIT:
                break
        elif stalled_scrolls >= ACCOUNT_PAGE_STALL_LIMIT:
            break

        _scroll_account_page(page)

    return links


def _scroll_account_page(page: "Page") -> None:
    page.evaluate(
        """
        (deltaY) => {
          const scroller =
            document.scrollingElement ||
            document.documentElement ||
            document.body;
          if (!scroller) return;
          scroller.scrollBy(0, deltaY);
          window.scrollBy(0, deltaY);
        }
        """,
        ACCOUNT_PAGE_SCROLL_DELTA_Y,
    )
    page.wait_for_timeout(ACCOUNT_PAGE_SCROLL_PAUSE_MS)


def _save_html_snapshot(page: "Page", path: Path, platform_name: str) -> None:
    last_exc: Optional[Exception] = None
    for attempt in range(HTML_SNAPSHOT_RETRIES):
        try:
            path.write_text(page.content(), encoding="utf-8")
            return
        except Exception as exc:
            last_exc = exc
            if attempt == HTML_SNAPSHOT_RETRIES - 1:
                break
            try:
                page.wait_for_load_state("domcontentloaded", timeout=2000)
            except Exception:
                pass
            page.wait_for_timeout(HTML_SNAPSHOT_RETRY_DELAY_MS)

    if last_exc is not None:
        print(f"[{platform_name}] WARNING: Failed to save HTML snapshot: {path} ({last_exc})")

def _url_slug(url: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", url)
    return slug[:120].strip("._-") or "page"
