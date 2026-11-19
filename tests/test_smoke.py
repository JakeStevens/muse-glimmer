"""Ensure the live smoke script fails on broken responses instead of reporting success."""

import importlib.util
import io
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "smoke_test", Path(__file__).resolve().parents[1] / "scripts/smoke_test.py"
)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def sse(*events, done=True):
    lines = ["data: " + json.dumps(event) + "\n\n" for event in events]
    if done:
        lines.append("data: [DONE]\n\n")
    return io.BytesIO("".join(lines).encode())


def test_reassembles_streamed_parallel_tool_calls():
    response = sse(
        {"choices": [{"index": 0, "delta": {"reasoning_content": "checking"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_a", "function": {"name": "delegate", "arguments": '{"job":'}},
            {"index": 1, "id": "call_b", "function": {"name": "delegate", "arguments": '{"job":'}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 1, "function": {"arguments": '"beta"}'}},
            {"index": 0, "function": {"arguments": '"alpha"}'}},
        ]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
    )
    result, first_delta = smoke.stream_response(response)
    assert first_delta is not None
    message = result["choices"][0]["message"]
    assert message["reasoning_content"] == "checking"
    assert smoke.calls_for(message, "delegate", "job", ["alpha", "beta"]) == {
        "alpha": "call_a", "beta": "call_b",
    }
    assert result["usage"]["completion_tokens"] == 20


@pytest.mark.parametrize(("failure", "diagnostic"), [
    ("no_done", "without \\[DONE\\]"), ("error", "Stream error"),
    ("length", "expected finish_reason=stop"), ("no_finish", "expected finish_reason=stop"),
    ("no_usage", "invalid token usage"), ("no_cache_stats", "invalid cached_tokens"),
    ("bad_total", "inconsistent total_tokens"), ("over_limit", "invalid token usage"),
    ("tool_finish_mismatch", "expected finish_reason=tool_calls"),
])
def test_broken_stream_cannot_pass(monkeypatch, failure, diagnostic):
    usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
             "prompt_tokens_details": {"cached_tokens": 90}}
    if failure == "no_cache_stats":
        usage.pop("prompt_tokens_details")
    if failure == "bad_total":
        usage["total_tokens"] = 1
    if failure == "over_limit":
        usage.update(completion_tokens=1000, total_tokens=1100)
    events = [{"choices": [{"index": 0, "delta": {"content": "READY"},
                           "finish_reason": "length" if failure == "length" else "stop"}]}]
    if failure == "no_finish":
        events[0]["choices"][0]["finish_reason"] = None
    if failure == "tool_finish_mismatch":
        events[0]["choices"][0]["delta"]["tool_calls"] = [
            {"index": 0, "id": "call_a", "function": {"name": "lookup", "arguments": "{}"}},
        ]
    if failure != "no_usage":
        events.append({"choices": [], "usage": usage})
    if failure == "error":
        events.append({"error": {"message": "worker died"}})
    server = smoke.Server("http://localhost:8000", "fixture", 10)
    monkeypatch.setattr(server, "request", lambda *args: sse(*events, done=failure != "no_done"))
    with pytest.raises(RuntimeError, match=diagnostic):
        server.chat("broken", "test-session", [{"role": "user", "content": "hi"}], stream=True)
    assert server.active == 0
    assert server.records == []


def test_unicode_and_escaped_arguments_survive_fragmentation():
    value = 'café/"notes"\\draft.txt\nnext'
    arguments = json.dumps({"path": value}, ensure_ascii=False)
    events = [{"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "call", "function": {"name": "read", "arguments": ""}},
    ]}}]}]
    events.extend({"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": character}},
    ]}}]} for character in arguments)
    events.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
    result, _ = smoke.stream_response(sse(*events))
    message = result["choices"][0]["message"]
    assert smoke.calls_for(message, "read", "path", [value]) == {value: "call"}
    message["tool_calls"][0]["function"]["arguments"] = arguments[:-1]
    with pytest.raises(ValueError):
        smoke.calls_for(message, "read", "path", [value])


def test_wrong_tool_results_and_small_cache_hits_fail():
    with pytest.raises(RuntimeError, match="Wrong final answer"):
        smoke.text_has({"content": "ALPHA-wrong"}, "ALPHA-expected")
    with pytest.raises(RuntimeError, match="prefix reuse"):
        smoke.cached({"label": "warm", "prompt_tokens": 1000, "cached_tokens": 3})
    message = {"tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "delegate", "arguments": '{"job":"alpha"}'}},
        {"id": "b", "type": "function", "function": {"name": "delegate", "arguments": '{"job":"alpha"}'}},
    ]}
    with pytest.raises(RuntimeError, match="Duplicate"):
        smoke.calls_for(message, "delegate", "job", ["alpha", "beta"])


def test_parallel_failure_stops_other_workers_promptly():
    server = smoke.Server("http://localhost:8000", "fixture", 10)
    stopped = []

    def worker(index):
        if index == 1:
            raise RuntimeError("worker failed")
        stopped.append(server.stopping.wait(timeout=5))

    with pytest.raises(RuntimeError, match="worker failed"):
        smoke.parallel(server, 2, worker, [0, 1])
    assert stopped == [True]
