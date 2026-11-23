"""The response envelope Beta MCP servers share.

An envelope is additive: a server keeps its own payload keys and their types at the top level
of the answer and adds these beside them.

``sources``
    ``list[dict]``, one record per source the answer used, built with :func:`source`:
    ``id``, ``name``, ``url``, ``licence``, ``attribution`` (all ``str``), ``fetched_at``
    (ISO 8601 ``str``, when the data was retrieved from upstream) and ``cached`` (``bool``).
``reading_notes``
    ``list[str]``, how to read the answer, built with :func:`reading_notes`.
``truncation``
    ``dict`` present only when something was cut: ``reason`` (``str``), ``cut``
    (``dict[str, int]``, what was cut and how many) and ``how_to_continue`` (``str``).
    Where a server already has a ``truncated`` bool, it stays and is ``True`` whenever
    ``truncation`` is present.
``error_code``
    ``str`` from :data:`ERROR_CODES`, present on every failure next to the server's existing
    ``error`` field, whose type does not change.

The size cap is :data:`MAX_RESPONSE_TOKENS` estimated tokens, counted as JSON characters
divided by three. That is cautious on purpose: Nordic text and JSON punctuation tokenise worse
than English prose, and the cap must stay under Claude Code's ~25k MCP output limit. A server
may pass a lower cap to :func:`fit`, never a higher one.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

MAX_RESPONSE_TOKENS = 20_000

ERROR_CODES: tuple[str, ...] = (
    "invalid_cursor",
    "invalid_dimension",
    "invalid_filters",
    "invalid_identifier",
    "invalid_input",
    "invalid_limit",
    "invalid_operation",
    "invalid_pagination",
    "invalid_query",
    "invalid_request",
    "invalid_selection",
    "invalid_sheet",
    "invalid_source",
    "ambiguous_sheet",
    "not_found",
    "query_too_large",
    "stale_cursor",
    "unsupported_dataset",
    "unsupported_format",
    "unsupported_selection",
    "download_limit",
    "rate_limited",
    "resource_limit",
    "timeout",
    "unsafe_url",
    "schema_changed",
    "source_changed",
    "source_error",
    "upstream_auth_failed",
    "upstream_empty",
    "upstream_encoding",
    "upstream_error",
    "upstream_format",
    "upstream_pagination",
    "upstream_rate_limited",
    "upstream_redirect",
    "upstream_request",
    "upstream_schema",
    "upstream_unavailable",
    "retrieval_unavailable",
    "internal_error",
)

_SIZE_CAP = "size_cap"


def _under_pytest() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ


def error_code(code: str) -> str:
    """``code`` if it is in :data:`ERROR_CODES`.

    An unknown code raises ``ValueError`` while tests run. In a running server it is logged
    and becomes ``internal_error``, so a failure path never turns into a crash.
    """
    if code in ERROR_CODES:
        return code
    if _under_pytest():
        raise ValueError(f"unknown error code {code!r}; add it to ERROR_CODES first")
    logger.warning("unknown error code %r reported as internal_error", code)
    return "internal_error"


def coded_error(code: str, message: str, **fields: Any) -> dict[str, Any]:
    """A failure answer for a server whose ``error`` is a string: the message stays in
    ``error`` exactly as before, the code goes beside it, and any extra fields follow."""
    return {"error": message, "error_code": error_code(code), **fields}


def source(
    *,
    id: str,
    name: str,
    url: str,
    licence: str,
    attribution: str,
    fetched_at: str,
    cached: bool,
) -> dict[str, Any]:
    """One entry of ``sources``. ``fetched_at`` is when the data left upstream, also on a
    cache hit, so a cached figure never looks fresher than it is."""
    if not fetched_at:
        raise ValueError("a source needs fetched_at")
    return {
        "id": id,
        "name": name,
        "url": url,
        "licence": licence,
        "attribution": attribution,
        "fetched_at": fetched_at,
        "cached": bool(cached),
    }


def reading_notes(*notes: str) -> list[str]:
    """Notes in the order given, without blanks or repeats."""
    return list(dict.fromkeys(n.strip() for n in notes if n and n.strip()))


def truncation(*, reason: str, cut: Mapping[str, int], how_to_continue: str) -> dict[str, Any]:
    return {"reason": reason, "cut": dict(cut), "how_to_continue": how_to_continue}


def estimate_tokens(value: Any) -> int:
    return math.ceil(len(json.dumps(value, ensure_ascii=False, default=str)) / 3)


def fit(
    response: dict[str, Any],
    cuts: Sequence[tuple[str, Callable[[dict[str, Any]], bool]]],
    *,
    how_to_continue: str,
    cap: int = MAX_RESPONSE_TOKENS,
) -> dict[str, Any]:
    """Cut ``response`` in place until it fits under ``cap``, and say what was cut.

    ``cuts`` are ``(name, cut)`` pairs applied in order. Each ``cut`` removes one item and
    returns ``False`` when it has nothing left to remove. The count per name goes into
    ``truncation.cut``, beside any cuts an earlier step recorded there. The truncation record
    is written before each measurement, so it counts against the cap itself. An answer that
    already fits is returned untouched.

    Cuts are made in batches that grow only while the heaviest item seen so far says the
    batch cannot overshoot, so items of uneven size never empty the answer. When the cuts run
    out before the answer fits, the answer is returned as it is and a warning is logged; a
    caller whose answers can stay over the cap needs a cut for its largest field. When nothing
    could be cut, the answer keeps its original truncation fields.

    Run it last, after anything computed from the full data, and never on a value that is
    still held in a cache.
    """
    if cap > MAX_RESPONSE_TOKENS:
        raise ValueError(f"cap {cap} is above the shared cap {MAX_RESPONSE_TOKENS}")
    size = estimate_tokens(response)
    if size <= cap:
        return response
    had_truncation, original_truncation = "truncation" in response, response.get("truncation")
    had_truncated, original_truncated = "truncated" in response, response.get("truncated")
    earlier = dict((original_truncation or {}).get("cut") or {})
    made: dict[str, int] = {}

    def note() -> int:
        cut = dict(earlier)
        for name, count in made.items():
            cut[name] = cut.get(name, 0) + count
        response["truncation"] = truncation(
            reason=_SIZE_CAP, cut=cut, how_to_continue=how_to_continue
        )
        if isinstance(original_truncated, bool):
            response["truncated"] = True
        return estimate_tokens(response)

    size = note()
    for name, cut in cuts:
        heaviest_item = 0
        batch = 1
        while size > cap:
            before = size
            removed = 0
            for _ in range(batch):
                if not cut(response):
                    break
                removed += 1
            if not removed:
                break
            made[name] = made.get(name, 0) + removed
            size = note()
            heaviest_item = max(heaviest_item, (before - size) // removed)
            if heaviest_item > 0:
                safe_batch = max(1, (size - cap) // heaviest_item // 2)
                batch = min(batch * 2, safe_batch)
        if size <= cap:
            break

    if not made:
        if had_truncation:
            response["truncation"] = original_truncation
        else:
            response.pop("truncation", None)
        if had_truncated:
            response["truncated"] = original_truncated
        size = estimate_tokens(response)
    if size > cap:
        logger.warning(
            "answer is still over the cap after every cut: %d estimated tokens, cap %d",
            size, cap,
        )
    return response
    earlier = dict((response.get("truncation") or {}).get("cut") or {})
    made: dict[str, int] = {}

    def note() -> int:
        cut = dict(earlier)
        for name, count in made.items():
            cut[name] = cut.get(name, 0) + count
        response["truncation"] = truncation(
            reason=_SIZE_CAP, cut=cut, how_to_continue=how_to_continue
        )
        if isinstance(response.get("truncated"), bool):
            response["truncated"] = True
        return estimate_tokens(response)

    size = note()
    for name, cut in cuts:
        while size > cap:
            before = size
            if not cut(response):
                break
            made[name] = made.get(name, 0) + 1
            size = note()
            tokens_per_item = before - size
            if size > cap and tokens_per_item > 0:
                half_the_excess_in_items = (size - cap) // tokens_per_item // 2
                for _ in range(half_the_excess_in_items):
                    if not cut(response):
                        break
                    made[name] += 1
                size = note()
        if size <= cap:
            break
    return response
