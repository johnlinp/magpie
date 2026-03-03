from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional, Protocol

from magpie.platforms.instagram import InstagramAdapter
from magpie.platforms.reddit import RedditAdapter
from magpie.platforms.x import XAdapter

if TYPE_CHECKING:
    from playwright.sync_api import Page
else:
    Page = Any


class PlatformAdapter(Protocol):
    name: str

    def is_supported(self, account_url: str) -> bool: ...

    def account_slug(self, account_url: str) -> str: ...

    def collect_post_links(self, page: Page, account_url: str) -> list[str]: ...

    def is_valid_post_url(self, resolved_url: str, account_url: str) -> bool: ...

    def wait_for_post_ready(self, post_page: Page) -> None: ...

    def capture_skip_reason(self, post_page: Page, account_url: str) -> Optional[str]: ...

    def extract_post_datetime(self, post_page: Page) -> Optional[datetime]: ...


@dataclass(frozen=True)
class AdapterRegistry:
    adapters: tuple[PlatformAdapter, ...]

    def by_url(self, account_url: str) -> Optional[PlatformAdapter]:
        for adapter in self.adapters:
            if adapter.is_supported(account_url):
                return adapter
        return None


REGISTRY = AdapterRegistry(
    adapters=(
        XAdapter(),
        InstagramAdapter(),
        RedditAdapter(),
    )
)


def get_adapter_for_url(account_url: str) -> Optional[PlatformAdapter]:
    return REGISTRY.by_url(account_url)
