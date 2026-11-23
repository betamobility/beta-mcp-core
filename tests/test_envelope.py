from __future__ import annotations

import time

import pytest

from beta_mcp_core.envelope import (
    ERROR_CODES,
    MAX_RESPONSE_TOKENS,
    coded_error,
    error_code,
    estimate_tokens,
    fit,
    reading_notes,
    source,
    truncation,
)

# Every code uk-stats-mcp raised on origin/main at 51fd382 (StatsError calls and literal
# {"code": ...} errors in lib/ and mcp_server.py), read from its source with an AST scan.
# Core CI cannot import uk-stats, so the list is frozen here; a server that adds a code
# needs a core release that adds it to ERROR_CODES first.
UK_STATS_CODES = {
    "ambiguous_sheet", "download_limit", "internal_error", "invalid_cursor",
    "invalid_dimension", "invalid_filters", "invalid_identifier", "invalid_input",
    "invalid_limit", "invalid_operation", "invalid_pagination", "invalid_query",
    "invalid_selection", "invalid_sheet", "invalid_source", "not_found", "query_too_large",
    "rate_limited", "resource_limit", "schema_changed", "source_changed", "source_error",
    "timeout", "unsafe_url", "unsupported_dataset", "unsupported_format",
    "unsupported_selection", "upstream_encoding", "upstream_format", "upstream_pagination",
    "upstream_redirect", "upstream_request", "upstream_schema", "upstream_unavailable",
}


def rows(n: int, width: int = 60) -> list[dict]:
    return [{"period": f"{2000 + i % 25}", "region": f"R{i}", "value": "x" * width}
            for i in range(n)]


def cut_last(key: str):
    def cut(response: dict) -> bool:
        if not response[key]:
            return False
        response[key].pop()
        return True
    return cut




def test_cell_limit_error_keeps_the_string_and_adds_the_code():
    message = "Spørringen er for stor: 1 200 000 celler, grensen er 800 000."
    answer = coded_error("query_too_large", message)
    assert answer == {"error": message, "error_code": "query_too_large"}


def test_coded_error_carries_extra_fields_beside_the_pair():
    answer = coded_error("not_found", "Table 99999 does not exist", hint="Search first")
    assert answer["hint"] == "Search first"
    assert answer["error_code"] == "not_found"


def test_unknown_code_raises_under_test():
    with pytest.raises(ValueError, match="no_such_code"):
        error_code("no_such_code")
    with pytest.raises(ValueError):
        coded_error("no_such_code", "boom")


def test_unknown_code_maps_to_internal_error_in_production(monkeypatch, caplog):
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    assert error_code("no_such_code") == "internal_error"
    answer = coded_error("no_such_code", "the original text")
    assert answer == {"error": "the original text", "error_code": "internal_error"}
    assert "no_such_code" in caplog.text


def test_every_uk_stats_code_is_in_the_closed_list():
    assert UK_STATS_CODES <= set(ERROR_CODES), UK_STATS_CODES - set(ERROR_CODES)


def test_the_codes_the_pxweb_and_genesis_servers_need_are_in_the_closed_list():
    needed = {
        "upstream_error", "upstream_rate_limited", "upstream_empty", "upstream_auth_failed",
        "invalid_request", "query_too_large", "not_found", "upstream_unavailable",
    }
    assert needed <= set(ERROR_CODES), needed - set(ERROR_CODES)


def test_error_codes_are_unique_snake_case():
    assert len(ERROR_CODES) == len(set(ERROR_CODES))
    assert all(code.replace("_", "").isalnum() and code.islower() for code in ERROR_CODES)




def test_source_record_carries_every_field():
    record = source(
        id="ssb:07459", name="Befolkning", url="https://data.ssb.no/api/v0/no/table/07459",
        licence="CC BY 4.0", attribution="Statistisk sentralbyrå",
        fetched_at="2026-11-23T20:00:00Z", cached=True,
    )
    assert record == {
        "id": "ssb:07459", "name": "Befolkning",
        "url": "https://data.ssb.no/api/v0/no/table/07459", "licence": "CC BY 4.0",
        "attribution": "Statistisk sentralbyrå", "fetched_at": "2026-11-23T20:00:00Z",
        "cached": True,
    }


def test_source_rejects_a_missing_fetch_time():
    with pytest.raises(ValueError):
        source(id="x", name="x", url="https://x", licence="x", attribution="x",
               fetched_at="", cached=False)


def test_reading_notes_drop_blanks_and_repeats_in_order():
    assert reading_notes("a", "", "b", "a", "  ") == ["a", "b"]


def test_truncation_record_shape():
    assert truncation(reason="max_rows", cut={"rows": 3}, how_to_continue="Narrow it") == {
        "reason": "max_rows", "cut": {"rows": 3}, "how_to_continue": "Narrow it",
    }




def test_answer_under_the_cap_passes_through_unchanged():
    answer = {"truncated": False, "rows": rows(10)}
    before = repr(answer)
    assert fit(answer, [("rows", cut_last("rows"))], how_to_continue="x") is answer
    assert repr(answer) == before
    assert "truncation" not in answer


def test_a_40k_token_answer_is_cut_under_the_cap():
    answer = {"truncated": False, "rows": rows(2000)}
    assert estimate_tokens(answer) > 39_000
    started = time.monotonic()
    fit(answer, [("rows", cut_last("rows"))],
        how_to_continue="Ask for fewer regions or a shorter period.")
    assert time.monotonic() - started < 2
    assert estimate_tokens(answer) <= MAX_RESPONSE_TOKENS
    assert answer["truncated"] is True
    cut = answer["truncation"]["cut"]["rows"]
    assert cut == 2000 - len(answer["rows"])
    assert answer["truncation"]["reason"] == "size_cap"
    assert answer["truncation"]["how_to_continue"] == "Ask for fewer regions or a shorter period."


def test_fit_does_not_cut_far_past_the_cap():
    answer = {"rows": rows(2000)}
    fit(answer, [("rows", cut_last("rows"))], how_to_continue="x")
    one_row = estimate_tokens(rows(1)) + 1
    assert estimate_tokens(answer) > MAX_RESPONSE_TOKENS - 50 * one_row


def test_cuts_apply_in_order_and_each_is_counted():
    answer = {"notes": ["n" * 300] * 5, "rows": rows(40)}
    fit(answer, [("notes", cut_last("notes")), ("rows", cut_last("rows"))],
        how_to_continue="x", cap=1_000)
    assert answer["notes"] == []
    assert answer["truncation"]["cut"]["notes"] == 5
    assert answer["truncation"]["cut"]["rows"] == 40 - len(answer["rows"])
    assert estimate_tokens(answer) <= 1_000


def test_the_truncation_note_counts_against_the_cap():
    answer = {"rows": rows(30)}
    how = "h" * 600
    fit(answer, [("rows", cut_last("rows"))], how_to_continue=how, cap=1_000)
    assert estimate_tokens(answer) <= 1_000
    assert answer["truncation"]["how_to_continue"] == how


def test_fit_does_not_add_a_truncated_bool_where_none_existed():
    answer = {"rows": rows(2000)}
    fit(answer, [("rows", cut_last("rows"))], how_to_continue="x")
    assert "truncated" not in answer
    assert "truncation" in answer


def test_fit_keeps_earlier_cuts_in_the_record():
    answer = {
        "truncated": True, "rows": rows(2000),
        "truncation": truncation(reason="max_rows", cut={"upstream_rows": 500},
                                 how_to_continue="Use a narrower filter."),
    }
    fit(answer, [("rows", cut_last("rows"))], how_to_continue="Ask for fewer regions.")
    assert answer["truncation"]["cut"]["upstream_rows"] == 500
    assert answer["truncation"]["cut"]["rows"] > 0
    assert answer["truncation"]["reason"] == "size_cap"


def test_a_server_may_lower_the_cap_but_not_raise_it():
    with pytest.raises(ValueError):
        fit({"rows": []}, [], how_to_continue="x", cap=MAX_RESPONSE_TOKENS + 1)


def test_fit_stops_when_no_cut_has_anything_left():
    answer = {"text": "t" * 10_000, "rows": rows(5)}
    fit(answer, [("rows", cut_last("rows"))], how_to_continue="x", cap=1_000)
    assert answer["rows"] == []
    assert answer["truncation"]["cut"]["rows"] == 5
