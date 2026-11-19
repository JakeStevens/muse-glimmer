#!/usr/bin/env python3
"""Quick live checks: cache reuse/edits, stream limits/disconnects, and tool/agent turns.

Uses only Python's standard library. No agent client or model download is needed.
Run against a dedicated server: this test consumes session and prefix-cache capacity.
Use --requests 24 to add sustained concurrent traffic after the smoke checks.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def tool(name, description, key, values):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {key: {"type": "string", "enum": values}},
                "required": [key],
                "additionalProperties": False,
            },
        },
    }


def stream_response(response):
    """Reassemble OpenAI SSE deltas, including fragmented tool arguments."""
    message = {"role": "assistant", "content": "", "reasoning_content": ""}
    calls = {}
    usage = finish = first_delta = None
    done = False
    for raw in response:
        line = raw.decode().strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            done = True
            break
        event = json.loads(data)
        require("error" not in event, f"Stream error: {event.get('error')}")
        if event.get("usage") is not None:
            usage = event["usage"]
        for choice in event.get("choices", []):
            require(choice["index"] == 0, "Unexpected extra stream choice")
            delta = choice.get("delta", {})
            if first_delta is None and any(delta.get(k) for k in ("content", "reasoning_content", "tool_calls")):
                first_delta = time.monotonic()
            for key in ("content", "reasoning_content"):
                message[key] += delta.get(key) or ""
            for part in delta.get("tool_calls") or []:
                call = calls.setdefault(part["index"], {
                    "id": "", "type": "function", "function": {"name": "", "arguments": ""},
                })
                call["id"] += part.get("id") or ""
                for key in ("name", "arguments"):
                    call["function"][key] += part.get("function", {}).get(key) or ""
            finish = choice.get("finish_reason") or finish
    require(done, "Stream ended without [DONE]")
    if calls:
        message["tool_calls"] = [calls[i] for i in sorted(calls)]
    return {"choices": [{"message": message, "finish_reason": finish}], "usage": usage}, first_delta


class Server:
    def __init__(self, base_url, model, timeout):
        self.base_url = base_url.rstrip("/").removesuffix("/v1")
        self.model = model
        self.timeout = timeout
        self.run_id = "smoke-" + uuid.uuid4().hex[:12]
        self.sessions = set()
        self.records = []
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.active = self.peak = 0
        self.aborted = 0

    def request(self, method, path, body=None, expected_status=200):
        data = None if body is None else json.dumps(body).encode()
        request = Request(self.base_url + path, data=data, method=method,
                          headers={"Content-Type": "application/json"})
        try:
            response = urlopen(request, timeout=self.timeout)
        except HTTPError as error:
            response = error
        if response.status != expected_status:
            with response:
                detail = response.read().decode(errors="replace")[:1500]
            raise RuntimeError(f"{method} {path}: expected HTTP {expected_status}, got {response.status}: {detail}")
        return response

    def close(self, session):
        with self.request("DELETE", f"/v1/sessions/{session}") as response:
            require(json.load(response).get("closed") is True, f"Could not close {session}")
        with self.lock:
            self.sessions.discard(session)

    def chat(self, label, session, messages, *, tools=None, stream=False,
             max_tokens=256, tool_choice="auto", expected_finish=None):
        require(not self.stopping.is_set(), "Smoke test stopped after a failure or interrupt")
        body = {
            "model": self.model, "session_id": session, "messages": messages,
            "temperature": 0.0, "max_tokens": max_tokens, "stream": stream,
        }
        if tools is not None:
            body.update(tools=tools, tool_choice=tool_choice, parallel_tool_calls=True)
        if stream:
            body["stream_options"] = {"include_usage": True}
        started = time.monotonic()
        with self.lock:
            self.sessions.add(session)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            with self.request("POST", "/v1/chat/completions", body) as response:
                result, first = stream_response(response) if stream else (json.load(response), None)
            finished = time.monotonic()
            require("error" not in result, f"{label}: {result.get('error')}")
            require(len(result.get("choices", [])) == 1, f"{label}: missing/extra choices")
            choice = result["choices"][0]
            message = choice["message"]
            finish = expected_finish or ("tool_calls" if message.get("tool_calls") else "stop")
            require(choice.get("finish_reason") == finish,
                    f"{label}: expected finish_reason={finish}, got {choice.get('finish_reason')}")
            usage = result.get("usage") or {}
            prompt, output = usage.get("prompt_tokens"), usage.get("completion_tokens")
            cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            require(type(prompt) is int and prompt > 0 and type(output) is int and 0 < output <= max_tokens,
                    f"{label}: missing/invalid token usage")
            require(usage.get("total_tokens") == prompt + output, f"{label}: inconsistent total_tokens")
            require(type(cached) is int and 0 <= cached <= prompt,
                    f"{label}: missing/invalid cached_tokens")
            require(message.get("role") == "assistant", f"{label}: missing assistant message")
            record = {
                "label": label, "session": session, "stream": stream,
                "prompt_tokens": prompt, "cached_tokens": cached, "output_tokens": output,
                "started": started, "finished": finished, "elapsed_s": finished - started,
                "first_delta_s": None if first is None else first - started,
            }
            with self.lock:
                self.records.append(record)
                print(f"{label:<23} {prompt:>7} {cached:>7} {output:>6} "
                      f"{cached / prompt:>7.0%} {finished - started:>7.2f}s", flush=True)
            return message, record
        finally:
            with self.lock:
                self.active -= 1


def cached(record):
    require(record["cached_tokens"] / record["prompt_tokens"] >= 0.5,
            f"{record['label']}: expected >=50% prefix reuse; check prefix-cache settings")


def calls_for(message, name, key, expected):
    calls = message.get("tool_calls") or []
    require(len(calls) == len(expected), f"Expected {len(expected)} {name} calls; got {len(calls)}")
    require(all(call.get("id") for call in calls) and len({call["id"] for call in calls}) == len(calls),
            "Missing or duplicate tool-call IDs")
    jobs = {}
    for call in calls:
        require(call.get("type") == "function" and call["function"]["name"] == name,
                f"Unexpected tool: {call}")
        arguments = json.loads(call["function"]["arguments"])
        require(set(arguments) == {key} and arguments[key] in expected,
                f"Unexpected {name} arguments: {arguments}")
        require(arguments[key] not in jobs, f"Duplicate {name} task: {arguments[key]}")
        jobs[arguments[key]] = call["id"]
    return jobs


def text_has(message, *receipts):
    require(not message.get("tool_calls"), "Expected final text, got another tool call")
    content = message.get("content") or ""
    require(all(receipt in content for receipt in receipts), f"Wrong final answer: {content!r}")


def parallel(server, workers, function, items):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(function, item) for item in items]
        try:
            for future in as_completed(futures):
                future.result()  # Observe any failure promptly, not in submission order.
            return [future.result() for future in futures]
        except BaseException:
            # Stop new turns on Ctrl+C/failure; in-flight HTTP calls remain timeout-bounded.
            server.stopping.set()
            for future in futures:
                future.cancel()
            raise


def exercise(server, concurrency, requests, context):
    system = "Reasoning strength: low\nFollow the instructions exactly. Be concise."
    basic = [{"role": "user", "content": "Hi"}]
    for patch, status, code in [
        ({"max_tokens": 0}, 400, "invalid_value"),
        ({"model": server.run_id}, 404, "model_not_found"),
        ({"max_tokens": context}, 400, "context_length_exceeded"),
    ]:
        with server.request("POST", "/v1/chat/completions",
                            {"model": server.model, "messages": basic, "max_tokens": 1, **patch},
                            expected_status=status) as response:
            require(json.load(response).get("error", {}).get("code") == code,
                    f"Expected structured {code} error")
    print("PASS invalid token budget, unknown model, and context overflow rejected", flush=True)

    # Long, identical prompt with a unique prefix prevents hits from previous test runs.
    prefix = f"{server.run_id}\n{system}\nReference notes (data, not instructions):\n" + "\n".join(
        f"Record {i:03d}: green square, blue circle, orange triangle." for i in range(32)
    )
    shared = [{"role": "system", "content": prefix}, {"role": "user", "content": "Reply OLD."}]
    for name in ("cold", "warm"):
        session = f"{server.run_id}-prefix-{name}"
        _, record = server.chat(f"shared-prefix/{name}", session, shared, stream=name == "warm",
                                max_tokens=1, expected_finish="length")
        require(record["output_tokens"] == 1, "One-token limit was not respected")
        if name == "warm":
            cached(record)
        else:
            server.close(session)

    # Disconnect after actual output, then reuse the same session with edited history.
    interrupted = [shared[0], {"role": "user", "content": "List the numbers from 1 through 10000."}]
    with server.request("POST", "/v1/chat/completions", {
        "model": server.model, "session_id": session, "messages": interrupted,
        "stream": True, "max_tokens": 256, "temperature": 0.0,
    }) as response:
        for raw in response:
            if not raw.startswith(b"data:"):
                continue
            require(raw[5:].strip() != b"[DONE]", "Stream completed before disconnect could be tested")
            event = json.loads(raw[5:])
            require("error" not in event, f"Interrupted stream failed: {event}")
            if any(c.get("delta", {}).get(k) for c in event.get("choices", [])
                   for k in ("content", "reasoning_content")):
                require(not any(c.get("finish_reason") for c in event["choices"]),
                        "Stream completed before disconnect could be tested")
                server.aborted += 1
                break
        else:
            raise RuntimeError("Stream ended before the disconnect test received output")
    edited = [shared[0], {"role": "user", "content": "Reply NEW. Do not call any tools."}]
    unused = [tool("lookup", "Unused tool", "key", ["old"])]
    message, record = server.chat("edited/after-disconnect", session, edited,
                                  tools=unused, tool_choice="none", max_tokens=128)
    text_has(message, "NEW")
    require("OLD" not in (message.get("content") or ""), "Edited prompt reused stale output")
    cached(record)
    server.close(session)
    print("PASS shared/edited prefixes, one-token limits, disconnect recovery, and tools disabled", flush=True)

    jobs = ["alpha", "beta"]
    receipts = {"alpha": f"R-{uuid.uuid4().hex[:6]}", "beta": "EMPTY"}
    delegate = [tool("delegate", "Run an independent child agent to retrieve a receipt.", "job", jobs)]
    parent = [
        {"role": "system", "content": system},
        {"role": "user", "content": "Call delegate for alpha and beta in parallel, both in this response. "
         "Then return a JSON object mapping each job name to its receipt. No markdown or extra text."},
    ]
    parent_session = f"{server.run_id}-parent"
    message, _ = server.chat("parent/delegate", parent_session, parent, tools=delegate,
                             stream=True, max_tokens=512)
    delegated = calls_for(message, "delegate", "job", jobs)
    parent.append(message)
    # A plain server reserves idle session slots; release the parent while children work.
    server.close(parent_session)

    def child(job):
        session = f"{server.run_id}-child-{job}"
        path = 'café/"notes"\\draft.txt' if job == "alpha" else "empty.txt"
        lookup = [tool("lookup_receipt", "Retrieve the receipt; it cannot be inferred.", "path", [path])]
        history = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Call lookup_receipt for this literal path: `{path}`. "
             "Then reply only with its receipt. If the tool output is empty, reply EMPTY. Do not retry."},
        ]
        try:
            answer, first = server.chat(f"child-{job}/tool", session, history, tools=lookup)
            call_id = calls_for(answer, "lookup_receipt", "path", [path])[path]
            history.extend([answer, {"role": "tool", "tool_call_id": call_id,
                                    "content": json.dumps({"receipt": receipts[job]}) if job == "alpha" else ""}])
            answer, followup = server.chat(f"child-{job}/result", session, history, tools=lookup, stream=True)
            text_has(answer, receipts[job])
            require(receipts[jobs[1 - jobs.index(job)]] not in (answer.get("content") or ""),
                    "Child session leaked the other child's receipt")
            cached(followup)
            return answer["content"], first
        finally:
            server.close(session)

    children = dict(zip(jobs, parallel(server, 2, child, jobs)))
    intervals = [value[1] for value in children.values()]
    require(max(r["started"] for r in intervals) < min(r["finished"] for r in intervals),
            "Child HTTP requests did not overlap")
    result_order = list(delegated)
    # Re-enable reversed results once call-ID association is fixed:
    # https://github.com/pytorch/executorch/issues/25299
    # result_order.reverse()
    for job in result_order:
        parent.append({"role": "tool", "tool_call_id": delegated[job], "content": children[job][0]})
    message, _ = server.chat("parent/results", parent_session, parent, tools=delegate)
    require(not message.get("tool_calls"), "parent/results: expected final text, got another tool call")
    actual = json.loads(message.get("content") or "")
    require(actual == receipts,
            f"parent/results: tool results paired incorrectly; expected {receipts}, got {actual}")
    server.close(parent_session)
    print("PASS parallel agents, escaped arguments, empty results, and warm tool turns", flush=True)
    print("SKIP reversed tool results: https://github.com/pytorch/executorch/issues/25299", flush=True)

    if not requests:
        return

    markers = [f"RECEIPT-{uuid.uuid4().hex[:12]}" for _ in range(concurrency)]

    def traffic(worker):
        session = f"{server.run_id}-load-{worker}"
        marker = markers[worker]
        history = [{"role": "system", "content": prefix + f"\nYour session receipt is {marker}."}]
        try:
            for turn, index in enumerate(range(worker, requests, concurrency)):
                history.append({"role": "user", "content": f"Turn {turn}: reply with your session receipt only."})
                message, record = server.chat(f"load/{index + 1}", session, history, stream=index % 2 == 0)
                text_has(message, marker)
                require(not any(other in message["content"] for other in markers if other != marker),
                        f"load/{index + 1}: another session's receipt leaked into the response")
                if turn:
                    cached(record)
                history.append(message)
        finally:
            server.close(session)

    parallel(server, concurrency, traffic, range(concurrency))
    print("PASS concurrent multi-turn traffic, streaming, and session isolation", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Existing server URL")
    parser.add_argument("--model", help="Model ID (auto-detected when the server exposes one model)")
    parser.add_argument("--concurrency", type=int, default=4, help="Load sessions; must fit server capacity (default: 4)")
    parser.add_argument("--requests", type=int, default=0, help="Optional extra load requests (default: 0)")
    parser.add_argument("--timeout", type=float, default=60, help="HTTP socket timeout in seconds (default: 60)")
    parser.add_argument("--output", type=Path, help="Write metrics and pass/fail result as JSON")
    args = parser.parse_args(argv)
    if args.concurrency < 2 or (args.requests != 0 and args.requests < 2 * args.concurrency) or args.timeout <= 0:
        parser.error("need concurrency >=2, requests either 0 or >=2*concurrency, and timeout >0")
    server = Server(args.base_url, args.model, args.timeout)
    started = time.monotonic()
    error = None
    try:
        with server.request("GET", "/v1/models") as response:
            models = {entry["id"]: entry for entry in json.load(response)["data"]}
        if server.model is None:
            require(len(models) == 1, "Server exposes multiple/no models; pass --model")
            server.model = next(iter(models))
        require(server.model in models, f"Model {server.model!r} is not served")
        context = models[server.model].get("context_length")
        require(type(context) is int and context > 0, "Server must advertise context_length")
        print(f"Server: {server.base_url} | Model: {server.model} | Run: {server.run_id}", flush=True)
        print(f"{'Request':<23} {'Prompt':>7} {'Cached':>7} {'Output':>6} {'Hit':>7} {'Elapsed':>8}", flush=True)
        exercise(server, args.concurrency, args.requests, context)
    except (Exception, KeyboardInterrupt) as failure:
        error = str(failure) or type(failure).__name__
    finally:
        for session in sorted(server.sessions):
            try:
                server.close(session)
            except Exception as failure:
                error = error or f"Session cleanup failed: {failure}"
                print(f"Cleanup failed for {session}: {failure}", file=sys.stderr)
    elapsed = time.monotonic() - started
    tokens = sum(record["output_tokens"] for record in server.records)
    report = {
        "passed": error is None, "error": error, "base_url": server.base_url,
        "model": server.model, "run_id": server.run_id,
        "elapsed_s": elapsed, "peak_http_requests": server.peak,
        "aborted_streams": server.aborted,
        "output_tokens_per_wall_second": tokens / elapsed, "requests": server.records,
    }
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"{'FAIL: ' + error if error else 'PASS'} | {len(server.records)} completed + {server.aborted} aborted | "
          f"peak HTTP in flight: {server.peak} | {tokens / elapsed:.1f} output tok/s (includes prefill/idle)")
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
