from __future__ import annotations

import json

import pytest
from fastmcp import Client, FastMCP

from beta_mcp_core.envelope import coded_error, fit, source
from beta_mcp_core.testing import assert_tool_surface, assert_valid_envelope


def good_answer(**extra) -> dict:
    return {
        "table_id": "07459",
        "rows": [{"period": "2024", "value": 5_550_203}],
        "truncated": False,
        "sources": [source(
            id="ssb:07459", name="Befolkning", url="https://data.ssb.no/api/v0/no/table/07459",
            licence="CC BY 4.0", attribution="Statistisk sentralbyrå",
            fetched_at="2026-11-23T20:00:00+00:00", cached=False,
        )],
        "reading_notes": ["Figures are as published."],
        **extra,
    }


def test_a_valid_answer_passes():
    assert_valid_envelope(good_answer())


def test_a_valid_error_passes():
    assert_valid_envelope(coded_error("not_found", "Tabellen finnes ikke"))


def test_a_source_without_fetched_at_fails():
    answer = good_answer()
    del answer["sources"][0]["fetched_at"]
    with pytest.raises(AssertionError, match="fetched_at"):
        assert_valid_envelope(answer)


def test_a_source_with_a_non_bool_cached_fails():
    answer = good_answer()
    answer["sources"][0]["cached"] = "yes"
    with pytest.raises(AssertionError, match="cached"):
        assert_valid_envelope(answer)


def test_an_answer_without_sources_fails():
    answer = good_answer()
    del answer["sources"]
    with pytest.raises(AssertionError, match="sources"):
        assert_valid_envelope(answer)


def test_an_unknown_error_code_fails():
    with pytest.raises(AssertionError, match="error_code"):
        assert_valid_envelope({"error": "boom", "error_code": "boom"})


def test_an_error_code_without_an_error_fails():
    with pytest.raises(AssertionError, match="error"):
        assert_valid_envelope(good_answer(error_code="not_found"))


def test_truncation_with_a_false_truncated_bool_fails():
    answer = good_answer(truncation={"reason": "size_cap", "cut": {"rows": 1},
                                     "how_to_continue": "x"})
    with pytest.raises(AssertionError, match="truncated"):
        assert_valid_envelope(answer)


def test_truncation_needs_its_three_fields():
    answer = good_answer(truncated=True, truncation={"reason": "size_cap"})
    with pytest.raises(AssertionError, match="truncation"):
        assert_valid_envelope(answer)


def test_truncation_without_a_legacy_bool_passes():
    answer = good_answer(truncation={"reason": "size_cap", "cut": {"rows": 1},
                                     "how_to_continue": "x"})
    del answer["truncated"]
    assert_valid_envelope(answer)


def test_an_answer_over_the_cap_fails():
    answer = good_answer(rows=[{"value": "x" * 100}] * 1000)
    with pytest.raises(AssertionError, match="cap"):
        assert_valid_envelope(answer)


def test_a_fitted_answer_passes():
    answer = good_answer(rows=[{"value": "x" * 100}] * 1000)
    fit(answer, [("rows", lambda r: bool(r["rows"]) and r["rows"].pop() is not None)],
        how_to_continue="Ask for less.")
    assert_valid_envelope(answer)




def test_the_baseline_passes_when_only_keys_were_added():
    baseline = {"table_id": "07459", "rows": [{"period": "2023", "value": 1}],
                "truncated": False}
    assert_valid_envelope(good_answer(), baseline=baseline)


def test_the_baseline_fails_when_a_key_is_gone():
    baseline = {"table_id": "07459", "title": "Befolkning"}
    with pytest.raises(AssertionError, match="title"):
        assert_valid_envelope(good_answer(), baseline=baseline)


def test_the_baseline_fails_when_error_turns_from_a_string_into_an_object():
    baseline = {"error": "Tabellen finnes ikke"}
    answer = {"error": {"code": "not_found", "message": "Tabellen finnes ikke"},
              "error_code": "not_found"}
    with pytest.raises(AssertionError, match="error"):
        assert_valid_envelope(answer, baseline=baseline)


def test_the_baseline_checks_nested_keys_and_list_items():
    baseline = {"rows": [{"period": "2023", "value": 1}]}
    answer = good_answer(rows=[{"period": 2024, "value": 1}])
    with pytest.raises(AssertionError, match=r"rows\[0\]\.period"):
        assert_valid_envelope(answer, baseline=baseline)


def test_the_baseline_treats_int_and_float_as_one_number_type():
    baseline = {"rows": [{"period": "2023", "value": 1}]}
    assert_valid_envelope(good_answer(rows=[{"period": "2024", "value": 1.5}]),
                          baseline=baseline)


def test_the_baseline_lets_a_recorded_null_take_any_value_and_the_reverse():
    baseline = {"unit": None, "title": "Befolkning"}
    assert_valid_envelope(good_answer(unit="personer", title=None), baseline=baseline)


def test_a_call_result_object_is_read_through_its_structured_content():
    class Result:
        structured_content = good_answer()

    assert_valid_envelope(Result())




def make_server(description: str = "Search SSB tables. Then call ssb_metadata.") -> FastMCP:
    server = FastMCP("t")

    @server.tool(description=description)
    def ssb_search(query: str) -> dict:
        return {}

    return server


def test_the_surface_is_recorded_once_and_then_matches(tmp_path, monkeypatch):
    snapshot = tmp_path / "tool_surface.json"
    monkeypatch.setenv("UPDATE_TOOL_SNAPSHOT", "1")
    assert_tool_surface(make_server(), snapshot)
    recorded = json.loads(snapshot.read_text())
    assert list(recorded) == ["ssb_search"]
    monkeypatch.delenv("UPDATE_TOOL_SNAPSHOT")
    assert_tool_surface(make_server(), snapshot)


def test_a_changed_surface_fails(tmp_path, monkeypatch):
    snapshot = tmp_path / "tool_surface.json"
    monkeypatch.setenv("UPDATE_TOOL_SNAPSHOT", "1")
    assert_tool_surface(make_server(), snapshot)
    monkeypatch.delenv("UPDATE_TOOL_SNAPSHOT")
    with pytest.raises(AssertionError, match="changed"):
        assert_tool_surface(make_server("Something else."), snapshot)


def test_a_missing_snapshot_fails_with_how_to_record_it(tmp_path, monkeypatch):
    monkeypatch.delenv("UPDATE_TOOL_SNAPSHOT", raising=False)
    with pytest.raises(AssertionError, match="UPDATE_TOOL_SNAPSHOT=1"):
        assert_tool_surface(make_server(), tmp_path / "missing.json")


def test_a_surface_larger_than_the_limit_fails(tmp_path, monkeypatch):
    snapshot = tmp_path / "tool_surface.json"
    monkeypatch.setenv("UPDATE_TOOL_SNAPSHOT", "1")
    with pytest.raises(AssertionError, match="characters"):
        assert_tool_surface(make_server("x" * 500), snapshot, max_chars=100)


async def test_the_surface_check_works_inside_a_running_event_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("UPDATE_TOOL_SNAPSHOT", "1")
    assert_tool_surface(make_server(), tmp_path / "s.json")
    async with Client(make_server()) as client:
        assert [t.name for t in await client.list_tools()] == ["ssb_search"]
