from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlparse


def parse_strict_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y/%m/%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid date '{value}'. Expected format: YYYY/MM/DD") from exc


def parse_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        from dateutil import parser as date_parser

        dt = date_parser.parse(value)
    except ModuleNotFoundError:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    except (ValueError, TypeError, OverflowError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utc_date(dt: Optional[datetime]) -> Optional[date]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.date()
    return dt.astimezone(timezone.utc).date()


def in_date_range(
    value: Optional[date], start_date: Optional[date], end_date: Optional[date]
) -> bool:
    if value is None:
        return False
    if start_date is not None and value < start_date:
        return False
    if end_date is not None and value > end_date:
        return False
    return True


def account_slug_from_url(account_url: str) -> str:
    parsed = urlparse(account_url)
    host = parsed.hostname or "unknown"
    path = parsed.path.strip("/")
    combined = f"{host}_{path}" if path else host
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", combined)
    slug = slug.strip("._-")
    return slug or "account"


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_datetime_from_page(page: Any) -> Optional[datetime]:
    time_el = page.query_selector("time[datetime]")
    if time_el is not None:
        time_value = time_el.get_attribute("datetime")
        dt = parse_datetime(time_value)
        if dt is not None:
            return dt

    meta_el = page.query_selector("meta[property='article:published_time']")
    if meta_el is not None:
        meta_value = meta_el.get_attribute("content")
        dt = parse_datetime(meta_value)
        if dt is not None:
            return dt

    jsonld_values = []
    for node in page.query_selector_all("script[type='application/ld+json']"):
        text = node.text_content()
        if text:
            jsonld_values.append(text)

    for raw in jsonld_values:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for obj in _iter_json_objects(payload):
            dt = _parse_first_datetime_key(
                obj, keys=("datePublished", "uploadDate", "dateCreated")
            )
            if dt is not None:
                return dt

    return None


def _parse_first_datetime_key(
    data: dict[str, Any], keys: Iterable[str]
) -> Optional[datetime]:
    for key in keys:
        dt = parse_datetime(data.get(key))
        if dt is not None:
            return dt
    return None


def _iter_json_objects(value: Any):
    if isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _iter_json_objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_json_objects(item)
