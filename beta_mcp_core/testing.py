"""Contract checks every server that uses the envelope runs in its own test suite.

Import these in a server's tests so every server checks the contract with the same code:

- :func:`assert_valid_envelope` on each tool's answer, with the answer the unmodified server
  gave as ``baseline`` to prove no existing field lost its name or type;
- :func:`assert_tool_surface` once, to keep tool names, schemas and descriptions from changing
  or growing unnoticed.

Plain ``assert`` only, so this module needs no test framework at runtime.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from beta_mcp_core.envelope import ERROR_CODES, MAX_RESPONSE_TOKENS, estimate_tokens

SOURCE_FIELDS = {
    "id": str,
    "name": str,
    "url": str,
    "licence": str,
    "attribution": str,
    "fetched_at": str,
    "cached": bool,
}


def _answer(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structured_content", result)
    assert isinstance(structured, dict), f"the answer is not a JSON object: {type(structured)}"
    return structured


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _same_shape(baseline: Any, current: Any, path: str) -> None:
    # A null on either side is a nullable field, not a type change.
    if baseline is None or current is None:
        return
    before, after = _kind(baseline), _kind(current)
    assert before == after, f"{path or 'answer'} changed type from {before} to {after}"
    if before == "object":
        for key, value in baseline.items():
            where = f"{path}.{key}" if path else key
            assert key in current, f"{where} is missing; the baseline answer had it"
            _same_shape(value, current[key], where)
    elif before == "array" and baseline and current:
        _same_shape(baseline[0], current[0], f"{path}[0]")


def assert_valid_envelope(
    result: Any, baseline: Any = None, *, cap: int = MAX_RESPONSE_TOKENS
) -> None:
    """Check one tool answer against the envelope contract.

    ``result`` is a tool's structured answer, or a call result carrying it as
    ``structured_content``. ``baseline`` is the answer the server gave to the same call before
    it adopted the envelope; every key in it must still exist with the same JSON type (a
    recorded ``null`` matches anything, and int and float are one number type).
    """
    answer = _answer(result)
    assert estimate_tokens(answer) <= cap, (
        f"answer is {estimate_tokens(answer)} estimated tokens, over the cap of {cap}"
    )

    if "error_code" in answer:
        assert answer["error_code"] in ERROR_CODES, (
            f"error_code {answer['error_code']!r} is not in ERROR_CODES"
        )
        assert answer.get("error"), "error_code is set but error is missing or empty"
    elif not answer.get("error"):
        for key in ("sources", "reading_notes"):
            assert isinstance(answer.get(key), list), f"{key} must be a list on every answer"
        for i, record in enumerate(answer["sources"]):
            assert isinstance(record, dict), f"sources[{i}] is not an object"
            for field, kind in SOURCE_FIELDS.items():
                assert field in record, f"sources[{i}] has no {field}"
                assert isinstance(record[field], kind), (
                    f"sources[{i}].{field} must be {kind.__name__}"
                )
            datetime.fromisoformat(record["fetched_at"])
        assert all(isinstance(n, str) for n in answer["reading_notes"]), (
            "reading_notes must be strings"
        )
    else:
        raise AssertionError("an answer with error must also carry error_code")

    if "truncation" in answer:
        record = answer["truncation"]
        assert isinstance(record, dict), "truncation must be an object"
        assert isinstance(record.get("reason"), str), "truncation.reason must be a string"
        assert isinstance(record.get("cut"), dict) and all(
            isinstance(v, int) for v in record["cut"].values()
        ), "truncation.cut must map names to counts"
        assert isinstance(record.get("how_to_continue"), str), (
            "truncation.how_to_continue must be a string"
        )
        if "truncated" in answer:
            assert answer["truncated"] is True, "truncation is present but truncated is not true"

    if baseline is not None:
        _same_shape(_answer(baseline), answer, "")


def _surface(server: Any) -> dict[str, Any]:
    from fastmcp import Client

    async def read() -> dict[str, Any]:
        async with Client(server) as client:
            tools = await client.list_tools()
        out = {}
        for tool in sorted(tools, key=lambda t: t.name):
            # MCP SDK v2 (FastMCP 4) renamed inputSchema to input_schema.
            schema = getattr(tool, "input_schema", None)
            if schema is None:
                schema = tool.inputSchema
            entry = {"description": tool.description, "input_schema": schema}
            if tool.annotations is not None:
                entry["annotations"] = tool.annotations.model_dump(exclude_none=True)
            out[tool.name] = entry
        return out

    # Run in a thread of its own, so the check also works from an async test.
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = asyncio.run(read())
        except BaseException as exc:  # handed to the caller below
            box["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def assert_tool_surface(
    server: Any, snapshot_path: str | Path, *, max_chars: int | None = None
) -> None:
    """Check the server's tool list against a recorded snapshot.

    Run once with ``UPDATE_TOOL_SNAPSHOT=1`` to (re)record the snapshot, and review its diff.
    ``max_chars`` caps the size of the whole tool list in characters, so a server can hold its
    tool descriptions to the size they had before a change.
    """
    current = _surface(server)
    size = len(json.dumps(current, ensure_ascii=False, sort_keys=True))
    if max_chars is not None:
        assert size <= max_chars, f"tool list is {size} characters, over the limit of {max_chars}"
    path = Path(snapshot_path)
    if os.environ.get("UPDATE_TOOL_SNAPSHOT") == "1":
        path.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n")
    assert path.exists(), f"no snapshot at {path}; run with UPDATE_TOOL_SNAPSHOT=1 to record it"
    recorded = json.loads(path.read_text())
    assert current == recorded, (
        "the tool surface changed; if on purpose, re-record with UPDATE_TOOL_SNAPSHOT=1 and "
        "review the diff"
    )
