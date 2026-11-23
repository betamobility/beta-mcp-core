from __future__ import annotations

import threading
import time

import pytest

from beta_mcp_core.analytics import _CallStats, _call_stats
from beta_mcp_core.cache import StampedCache, stamped_cache, ttl_cache


def test_ttl_cache_expires_values():
    cache = ttl_cache(maxsize=10, ttl=0.01)
    cache["key"] = "value"
    assert cache["key"] == "value"
    time.sleep(0.02)
    assert "key" not in cache






class Clock:
    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def stats():
    holder = _CallStats()
    return holder, _call_stats.set(holder)


def test_miss_then_hit_with_the_same_fetch_time():
    clock = Clock()
    cache = stamped_cache(ttl=60, clock=clock)
    first = cache.get_or_load("k", lambda: {"rows": [1, 2]})
    clock.now += 10
    second = cache.get_or_load("k", lambda: pytest.fail("loaded twice"))
    assert first.cached is False and second.cached is True
    assert first.fetched_at == second.fetched_at
    assert first.fetched_at.endswith("+00:00")
    assert second.value == {"rows": [1, 2]}


def test_a_hit_returns_a_copy():
    cache = stamped_cache()
    cache.put("k", {"rows": [1, 2, 3]})
    cache.get("k").value["rows"].clear()
    assert cache.get("k").value == {"rows": [1, 2, 3]}


def test_cutting_the_value_a_miss_returned_does_not_change_the_next_hit():
    cache = stamped_cache()
    first = cache.get_or_load("k", lambda: {"rows": [1, 2, 3]})
    first.value["rows"].pop()
    assert cache.get("k").value == {"rows": [1, 2, 3]}


def test_entries_expire_after_the_ttl():
    clock = Clock()
    cache = stamped_cache(ttl=60, clock=clock)
    cache.put("k", {"a": 1})
    clock.now += 61
    assert cache.get("k") is None


@pytest.mark.parametrize("value", [
    None, {}, [], "", b"", {"error": "Tabellen finnes ikke"},
    {"error": {"code": "not_found", "message": "gone"}}, {"error_code": "not_found"},
])
def test_empty_and_error_values_are_not_stored(value):
    cache = stamped_cache()
    stamped = cache.get_or_load("k", lambda: value)
    assert stamped.value == value and stamped.cached is False
    assert cache.get("k") is None


def test_a_falsy_error_key_does_not_block_storing():
    cache = stamped_cache()
    cache.put("k", {"error": None, "rows": [1]})
    assert cache.get("k").value == {"error": None, "rows": [1]}


def test_a_load_that_raises_stores_nothing():
    cache = stamped_cache()
    with pytest.raises(RuntimeError):
        cache.get_or_load("k", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert cache.get("k") is None


async def test_async_load_is_stamped_and_reused():
    cache = stamped_cache()
    calls = []

    async def load():
        calls.append(1)
        return {"x": 1}

    first = await cache.aget_or_load("k", load)
    second = await cache.aget_or_load("k", load)
    assert (first.cached, second.cached, len(calls)) == (False, True, 1)


def test_lookups_report_miss_then_hit_to_the_call():
    cache = stamped_cache()
    holder, token = stats()
    try:
        cache.get_or_load("k", lambda: {"x": 1})
        assert holder.cache == "miss"
    finally:
        _call_stats.reset(token)
    holder, token = stats()
    try:
        cache.get_or_load("k", lambda: {"x": 1})
        assert holder.cache == "hit"
        assert holder.upstream_seconds is None
    finally:
        _call_stats.reset(token)


def test_one_miss_makes_the_whole_call_a_miss():
    cache = stamped_cache()
    cache.put("a", {"x": 1})
    holder, token = stats()
    try:
        cache.get("a")
        cache.get("b")
        cache.get("a")
        assert holder.cache == "miss"
    finally:
        _call_stats.reset(token)


def test_a_load_is_timed_as_upstream():
    cache = stamped_cache()
    holder, token = stats()
    try:
        cache.get_or_load("k", lambda: {"x": 1})
        assert holder.upstream_seconds is not None and holder.upstream_seconds >= 0
    finally:
        _call_stats.reset(token)


def test_concurrent_threads_share_one_cache_safely():
    cache = StampedCache(maxsize=50, ttl=60)
    errors = []

    def work(n):
        try:
            for i in range(300):
                cache.put(f"k{(n * i) % 80}", {"i": i})
                cache.get(f"k{i % 80}")
        except Exception as exc:  # pragma: no cover - the assertion reports it
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []




class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    def download_as_text(self):
        if self.bucket.fail_reads:
            raise OSError("bucket unavailable")
        if self.name not in self.bucket.blobs:
            raise FileNotFoundError(self.name)
        return self.bucket.blobs[self.name]

    def upload_from_string(self, data, content_type=None):
        self.bucket.blobs[self.name] = data


class FakeBucket:
    def __init__(self):
        self.blobs, self.fail_reads = {}, False

    def blob(self, name):
        return FakeBlob(self, name)


def gcs_cache(bucket, clock, ttl=86_400):
    from beta_mcp_core.cache import GcsStore

    return StampedCache(ttl=ttl, clock=clock, store=GcsStore(bucket, prefix="ssb"))


def test_a_blob_survives_a_cold_start_with_its_original_fetch_time():
    bucket, clock = FakeBucket(), Clock()
    first = gcs_cache(bucket, clock).get_or_load("meta/07459/no", lambda: {"vars": [1]})
    clock.now += 3600
    restarted = gcs_cache(bucket, clock)
    again = restarted.get_or_load("meta/07459/no", lambda: pytest.fail("refetched"))
    assert again.cached is True
    assert again.fetched_at == first.fetched_at
    assert again.value == {"vars": [1]}
    assert all(name.startswith("ssb/v1/") for name in bucket.blobs)


def test_an_expired_blob_is_a_miss_and_is_rewritten():
    bucket, clock = FakeBucket(), Clock()
    gcs_cache(bucket, clock).put("m", {"old": True})
    clock.now += 86_401
    fresh = gcs_cache(bucket, clock).get_or_load("m", lambda: {"old": False})
    assert fresh.cached is False and fresh.value == {"old": False}
    clock.now += 1
    assert gcs_cache(bucket, clock).get("m").value == {"old": False}


def test_a_bucket_read_failure_is_a_miss(caplog):
    bucket, clock = FakeBucket(), Clock()
    gcs_cache(bucket, clock).put("m", {"a": 1})
    bucket.fail_reads = True
    result = gcs_cache(bucket, clock).get_or_load("m", lambda: {"a": 2})
    assert result.cached is False and result.value == {"a": 2}
    assert "bucket unavailable" not in caplog.text


def test_a_missing_blob_is_a_miss():
    bucket, clock = FakeBucket(), Clock()
    assert gcs_cache(bucket, clock).get("nothing") is None


def test_empty_values_are_never_written_to_the_bucket():
    bucket, clock = FakeBucket(), Clock()
    gcs_cache(bucket, clock).get_or_load("m", lambda: {})
    gcs_cache(bucket, clock).get_or_load("e", lambda: {"error": "nope"})
    assert bucket.blobs == {}


def test_a_value_the_bucket_cannot_hold_still_lives_in_memory():
    bucket, clock = FakeBucket(), Clock()
    cache = gcs_cache(bucket, clock)
    cache.put("raw", b"\x00bytes")
    assert bucket.blobs == {}
    assert cache.get("raw").value == b"\x00bytes"


async def test_async_loads_read_and_write_the_bucket():
    bucket, clock = FakeBucket(), Clock()

    async def load():
        return {"a": 1}

    await gcs_cache(bucket, clock).aget_or_load("m", load)
    again = await gcs_cache(bucket, clock).aget_or_load("m", load)
    assert again.cached is True


def test_no_bucket_means_memory_only(monkeypatch):
    monkeypatch.delenv("METADATA_CACHE_BUCKET", raising=False)
    assert stamped_cache(bucket_env="METADATA_CACHE_BUCKET").store is None


def test_a_bucket_without_the_storage_library_falls_back_to_memory(monkeypatch, caplog):
    import builtins

    real_import = builtins.__import__

    def no_google(name, *args, **kwargs):
        if name.startswith("google"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_google)
    monkeypatch.setenv("METADATA_CACHE_BUCKET", "beta-mcp-cache")
    cache = stamped_cache(bucket_env="METADATA_CACHE_BUCKET", prefix="ssb")
    assert cache.store is None
    assert "memory only" in caplog.text


def test_fitting_a_hit_leaves_the_full_rows_for_the_next_hit():
    from beta_mcp_core.envelope import fit

    cache = stamped_cache()
    rows = [{"value": "x" * 100} for _ in range(50)]
    cache.put("k", {"rows": rows})
    answer = cache.get("k").value
    fit(answer, [("rows", lambda a: bool(a["rows"]) and a["rows"].pop() is not None)],
        how_to_continue="x", cap=500)
    assert len(answer["rows"]) < 50
    assert len(cache.get("k").value["rows"]) == 50
