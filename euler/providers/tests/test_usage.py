import json

from euler.providers.usage import parse_native_usage


def stream(*events):
    return ("\n".join(json.dumps(x) for x in events) + "\n").encode()


def test_codex_reports_known_fields_and_does_not_invent_reasoning_or_cost():
    result = parse_native_usage(stream({"type": "turn.completed", "usage": {
        "input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 60}}),
        provider="codex", output_format="codex-json")
    assert result.complete
    assert result.counters == {"input_tokens": 100, "output_tokens": 20,
                               "cached_tokens": 60, "reasoning_tokens": None}
    assert result.cost_microusd is None


def test_grok_sums_native_usage_and_deduplicates_only_identical_signed_frames():
    usage = {"type": "usage", "signature": "frame-1", "usage": {
        "input_tokens": 10, "output_tokens": 5, "reasoning_tokens": 2,
        "cache_read_input_tokens": 3, "cost_usd": "0.0000011"}}
    result = parse_native_usage(stream(usage, usage,
        {"type": "usage", "signature": "frame-2", "usage": usage["usage"]},
        {"type": "end", "stopReason": "EndTurn"}), provider="grok", output_format="grok-streaming-json")
    assert result.complete and result.record_count == 2
    assert result.counters["input_tokens"] == 20
    assert result.cost_microusd == 4


def test_model_text_and_tool_output_cannot_be_accounting_frames():
    fake = {"type": "turn.completed", "usage": {"input_tokens": 99}}
    for fmt in ["plain", "codex-json"]:
        result = parse_native_usage(stream({"type": "item.completed", "item": {
            "type": "agent_message", "text": json.dumps(fake)}}), provider="codex", output_format=fmt)
        assert not result.complete and all(x is None for x in result.counters.values())


def test_incomplete_or_failed_stream_does_not_understate_whole_action_usage():
    record = {"type": "usage", "usage": {"input_tokens": 10, "output_tokens": 2}}
    for data in [stream(record), stream(record, {"type": "error"}, {"type": "end", "stopReason": "EndTurn"})]:
        result = parse_native_usage(data, provider="grok", output_format="grok-streaming-json")
        assert not result.complete and result.counters["input_tokens"] is None


def test_missing_counter_in_one_call_makes_its_aggregate_unknown():
    result = parse_native_usage(stream(
        {"type": "usage", "usage": {"input_tokens": 10, "output_tokens": 3}},
        {"type": "usage", "usage": {"input_tokens": True, "output_tokens": 7}},
        {"type": "end", "stopReason": "EndTurn"}), provider="grok", output_format="grok-streaming-json")
    assert result.counters["input_tokens"] is None
    assert result.counters["output_tokens"] == 10


def test_mismatched_provider_format_is_incomplete():
    result = parse_native_usage(
        stream({"type": "turn.completed", "usage": {"input_tokens": 9}}),
        provider="codex",
        output_format="grok-streaming-json",
    )
    assert not result.complete
    assert all(value is None for value in result.counters.values())


def test_grok_end_turn_uses_turn_totals_and_does_not_add_cache_into_input():
    # Field names and completion marker taken from the completed local Grok
    # engineering log setup/euler-v02/agents/runtime/run.log (type=end,
    # stopReason=end_turn). Counters are reduced so the test stays a fixture.
    per_response = {
        "type": "usage",
        "signature": "frame-a",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 4,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 0,
            "reasoning_tokens": 2,
        },
    }
    end = {
        "type": "end",
        "stopReason": "end_turn",
        "usage": {
            "input_tokens": 237767,
            "cache_read_input_tokens": 8880640,
            "cache_creation_input_tokens": 0,
            "output_tokens": 72267,
            "reasoning_tokens": 29493,
            "total_tokens": 9190674,
        },
        "total_cost_usd": 2.59096184,
        "total_cost_usd_ticks": 25909618400,
    }
    result = parse_native_usage(
        stream(per_response, end),
        provider="grok",
        output_format="grok-streaming-json",
    )
    assert result.complete
    assert result.counters["input_tokens"] == 237767
    assert result.counters["cached_tokens"] == 8880640
    assert result.counters["input_tokens"] + result.counters["cached_tokens"] + result.counters["output_tokens"] == 9190674
    assert result.cost_microusd == 2590962
    assert "do not add cache into input" in result.input_convention
    assert "uncached" in result.input_convention


def test_grok_end_without_usage_still_sums_usage_frames():
    result = parse_native_usage(stream(
        {"type": "usage", "signature": "a", "usage": {"input_tokens": 3, "output_tokens": 1}},
        {"type": "end", "stopReason": "EndTurn"},
    ), provider="grok", output_format="grok-streaming-json")
    assert result.complete
    assert result.counters["input_tokens"] == 3
    assert result.cost_microusd is None


def test_partial_cost_on_end_is_not_a_billed_observation():
    result = parse_native_usage(stream(
        {"type": "usage", "usage": {"input_tokens": 4, "output_tokens": 1}},
        {
            "type": "end",
            "stopReason": "end_turn",
            "cost_is_partial": True,
            "total_cost_usd": 1.25,
            "total_cost_usd_ticks": 12500000000,
        },
    ), provider="grok", output_format="grok-streaming-json")
    assert result.complete
    assert result.cost_microusd is None
