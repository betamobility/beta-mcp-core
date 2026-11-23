"""Cache helpers shared by small MCP proxy servers."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote

from cachetools import TTLCache

from beta_mcp_core.analytics import record_cache, upstream_timer

logger = logging.getLogger(__name__)


def ttl_cache(*, maxsize: int = 1024, ttl: int = 300) -> TTLCache:
    return TTLCache(maxsize=maxsize, ttl=ttl)


@dataclass(frozen=True)
class Stamped:
    """A value with the time it was fetched from upstream and whether it came from cache."""

    value: Any
    fetched_at: str
    cached: bool


class Store(Protocol):
    """A shared store behind the in-memory cache, read through on a memory miss."""

    def read(self, key: str) -> tuple[float, Any] | None: ...

    def write(self, key: str, fetched: float, value: Any) -> None: ...


def _storable(value: Any) -> bool:
    """Empty answers and error answers are never cached, so a failure is retried next time
    instead of being served for a whole TTL."""
    if value is None:
        return False
    if isinstance(value, (str, bytes, list, tuple, dict)) and len(value) == 0:
        return False
    if isinstance(value, dict) and (value.get("error") or value.get("error_code")):
        return False
    return True


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


class StampedCache:
    """A TTL cache that remembers when each value was fetched and reports hits and misses.

    Every value is copied on the way in and on the way out, so a caller that trims an answer
    (``envelope.fit`` cuts rows in place) never changes what the next caller gets. An entry is
    served only while its age, counted from when it was fetched, is under ``ttl``; that holds
    for entries read back from ``store`` too, which keep their original fetch time. Safe to
    share between threads.
    """

    def __init__(
        self,
        *,
        maxsize: int = 1024,
        ttl: float = 300,
        store: Store | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.ttl = ttl
        self.store = store
        self._clock = clock
        self._memory: TTLCache = TTLCache(maxsize=maxsize, ttl=ttl, timer=clock)
        self._lock = threading.Lock()

    def _fresh(self, fetched: float) -> bool:
        return self._clock() - fetched < self.ttl

    def _from_memory(self, key: str) -> tuple[float, Any] | None:
        with self._lock:
            entry = self._memory.get(key)
        if entry is not None and self._fresh(entry[0]):
            return entry
        return None

    def _remember(self, key: str, fetched: float, value: Any) -> None:
        with self._lock:
            self._memory[key] = (fetched, value)

    def _from_store(self, key: str) -> tuple[float, Any] | None:
        if self.store is None:
            return None
        try:
            entry = self.store.read(key)
        except Exception:
            logger.warning("cache store read failed; treated as a miss")
            return None
        if entry is None or not self._fresh(entry[0]) or not _storable(entry[1]):
            return None
        self._remember(key, entry[0], entry[1])
        return entry

    def _to_store(self, key: str, fetched: float, value: Any) -> None:
        if self.store is None:
            return
        try:
            self.store.write(key, fetched, value)
        except Exception:
            logger.warning("cache store write failed; the value is cached in memory only")

    def _hit(self, entry: tuple[float, Any]) -> Stamped:
        record_cache("hit")
        return Stamped(copy.deepcopy(entry[1]), _iso(entry[0]), True)

    def get(self, key: str) -> Stamped | None:
        """The cached value as a copy, or ``None``. Counts as a hit or a miss for the call."""
        entry = self._from_memory(key) or self._from_store(key)
        if entry is None:
            record_cache("miss")
            return None
        return self._hit(entry)

    def _remember_if_storable(self, key: str, fetched: float, value: Any) -> bool:
        if not _storable(value):
            return False
        self._remember(key, fetched, copy.deepcopy(value))
        return True

    def put(self, key: str, value: Any, *, fetched: float | None = None) -> Stamped:
        """Store a freshly fetched value, unless it is empty or an error, and stamp it."""
        fetched = self._clock() if fetched is None else fetched
        if self._remember_if_storable(key, fetched, value):
            self._to_store(key, fetched, value)
        return Stamped(value, _iso(fetched), False)

    def get_or_load(self, key: str, load: Callable[[], Any]) -> Stamped:
        """The cached value, or ``load()`` timed as an upstream call and then stored."""
        hit = self.get(key)
        if hit is not None:
            return hit
        with upstream_timer():
            value = load()
        return self.put(key, value)

    async def aget_or_load(self, key: str, load: Callable[[], Awaitable[Any]]) -> Stamped:
        """``get_or_load`` for an async loader. Store reads and writes run off the event loop."""
        entry = self._from_memory(key)
        if entry is None and self.store is not None:
            entry = await asyncio.to_thread(self._from_store, key)
        if entry is not None:
            return self._hit(entry)
        record_cache("miss")
        with upstream_timer():
            value = await load()
        fetched = self._clock()
        if self._remember_if_storable(key, fetched, value) and self.store is not None:
            await asyncio.to_thread(self._to_store, key, fetched, value)
        return Stamped(value, _iso(fetched), False)


class GcsStore:
    """Cache entries as JSON blobs in a Cloud Storage bucket, so they survive a cold start.

    Blob names are ``<prefix>/v<schema_version>/<key>.json``; bump ``schema_version`` when the
    stored format changes, and old blobs are simply never read again. Values that are not
    JSON (raw bytes, for example) are not written. The bucket's lifecycle rule only collects
    garbage: freshness is decided from each blob's ``fetched_at``.
    """

    def __init__(self, bucket: Any, *, prefix: str, schema_version: int = 1) -> None:
        self._bucket = bucket
        self._prefix = f"{prefix.strip('/')}/v{schema_version}"

    def _blob(self, key: str) -> Any:
        return self._bucket.blob(f"{self._prefix}/{quote(key, safe='/-_.')}.json")

    def read(self, key: str) -> tuple[float, Any] | None:
        try:
            text = self._blob(key).download_as_text()
        except FileNotFoundError:
            return None
        except Exception as exc:
            if type(exc).__name__ == "NotFound":
                return None
            raise
        body = json.loads(text)
        fetched = datetime.fromisoformat(body["fetched_at"]).timestamp()
        return fetched, body["value"]

    def write(self, key: str, fetched: float, value: Any) -> None:
        try:
            data = json.dumps({"fetched_at": _iso(fetched), "value": value}, ensure_ascii=False)
        except (TypeError, ValueError):
            return
        self._blob(key).upload_from_string(data, content_type="application/json")


def _gcs_store(bucket_env: str | None, prefix: str, schema_version: int) -> GcsStore | None:
    bucket_name = os.environ.get(bucket_env, "").strip() if bucket_env else ""
    if not bucket_name:
        return None
    try:
        from google.cloud import storage
    except ImportError:
        logger.warning(
            "%s is set but google-cloud-storage is not installed; cache is memory only",
            bucket_env,
        )
        return None
    try:
        bucket = storage.Client().bucket(bucket_name)
    except Exception:
        logger.warning("%s is set but the bucket client failed; cache is memory only", bucket_env)
        return None
    return GcsStore(bucket, prefix=prefix or "cache", schema_version=schema_version)


def stamped_cache(
    *,
    maxsize: int = 1024,
    ttl: float = 300,
    bucket_env: str | None = None,
    prefix: str = "",
    schema_version: int = 1,
    clock: Callable[[], float] = time.time,
) -> StampedCache:
    """A :class:`StampedCache`. With ``bucket_env`` naming an environment variable that holds
    a bucket name, entries are also kept in that Cloud Storage bucket under ``prefix``
    (install ``beta-mcp-core[gcs]``). Without the variable, or without the storage library,
    the cache is memory only."""
    store = _gcs_store(bucket_env, prefix, schema_version)
    return StampedCache(maxsize=maxsize, ttl=ttl, store=store, clock=clock)
