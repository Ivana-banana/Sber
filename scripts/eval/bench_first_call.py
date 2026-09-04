"""What the failed `native_tools` rung costs on the first call of a task.

Development tool. Runs inside the proxy container, the only place with a route to the
model:

    docker exec sber-proxy python3 /opt/eval/bench_first_call.py \
        --model qwen2.5-coder:7b-instruct-q4_K_M

In every one of the 36 task runs of E033 and E034 the model answered the first request
with `{"name": "list_dir", "arguments": {...}}` written as text -- the OpenAI function
shape, but naming the tool it wants rather than calling the one function the request
offered. `agent.llm` salvages the JSON out of the prose and then rejects it, because
`ACTION_SCHEMA` requires a `tool` key. The ladder demotes to `json_schema` and asks
again. So every task pays two requests for its first action, inside the step that
already costs 39-49 seconds.

Two questions, and both need a number before anything is changed:

1. How much of the first call is the wasted rung? `native` against `json_schema`, each
   on a system prompt the server has not seen.
2. Does the second request get any of the first one's prefill back? `ladder` is the two
   in sequence, as the agent issues them today. If `ladder` is about `native` plus
   `json_schema`, the `tools` payload changes the prompt early enough that nothing is
   reused, and removing the rung saves nearly its whole cost.

The schema is copied from `agent/prompt.py` rather than imported: the proxy container
mounts `scripts/eval` alone. If `ACTION_SCHEMA` there changes, this copy is stale and
the comparison drifts -- it is a bench, not a contract, and the tests do not cover it.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.error
import urllib.request

TOOL_NAME = "emit_action"

ACTION_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "tool": {
            "type": "string",
            "enum": ["run_shell", "read_range", "list_dir", "search", "write_file", "finish"],
        },
        "command": {"type": "string", "description": "run_shell: shell command line"},
        "path": {"type": "string", "description": "file or directory the action targets"},
        "start_line": {"type": "integer", "description": "read_range: first line, 1-based"},
        "pattern": {"type": "string", "description": "search: regular expression"},
        "content": {"type": "string", "description": "write_file: exact bytes to write"},
        "verify": {"type": "string", "description": "finish: command proving the result"},
        "summary": {"type": "string", "description": "finish: what was done, one line"},
    },
    "required": ["tool"],
}

SYSTEM = """\
РОЛЬ
Ты — автономный локальный исполнитель. Цель — проверяемое решение задачи.

ИНСТРУМЕНТЫ
Ровно один вызов за шаг, в виде JSON по схеме. Рассуждения не пиши.
run_shell(command) — команда через /bin/sh в рабочем каталоге.
read_range(path, start_line) — окно текстового файла.
list_dir(path) — содержимое каталога.
search(pattern, path) — регулярное выражение по дереву.
write_file(path, content) — записать файл целиком.
finish(path, verify, summary) — закончить работу и доказать результат.

ПРОЦЕДУРА (audit-{seed})
Найди дефект, докажи его и почини причину, а не симптом.
Проверка обязана падать на неверном ответе.

СРЕДА
Рабочий каталог /app. Python 3.12. Сети нет.

ЗАДАЧА
Осмотри проект и начни с того, что установи, какие файлы в нём есть.
"""

OBSERVATION = "НАБЛЮДЕНИЕ шаг 0\nexit=0\nработа начата, действий ещё не было\n"


def request(url: str, body: dict[str, object], timeout: float) -> tuple[float, dict]:
    payload = json.dumps(body).encode()
    call = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    started = time.monotonic()
    with urllib.request.urlopen(call, timeout=timeout) as reply:
        answer = json.loads(reply.read().decode())
    return time.monotonic() - started, answer


def base(model: str, seed: int) -> dict[str, object]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM.format(seed=seed)},
            {"role": "user", "content": OBSERVATION},
        ],
        "temperature": 0,
        "max_tokens": 64,
        "stream": False,
    }


def native(model: str, seed: int) -> dict[str, object]:
    body = base(model, seed)
    body["tools"] = [
        {"type": "function", "function": {"name": TOOL_NAME, "parameters": ACTION_SCHEMA}}
    ]
    return body


def json_schema(model: str, seed: int) -> dict[str, object]:
    body = base(model, seed)
    body["response_format"] = {
        "type": "json_schema",
        "json_schema": {"name": TOOL_NAME, "schema": ACTION_SCHEMA, "strict": True},
    }
    return body


def verdict(answer: dict) -> str:
    """What `agent.llm._read_reply` would decide about this reply, in one word."""
    message = ((answer.get("choices") or [{}])[0]).get("message") or {}
    if message.get("tool_calls"):
        return "tool_calls"
    content = message.get("content") or ""
    text = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        parsed = json.loads(text.strip())
    except ValueError:
        return "not-json"
    if isinstance(parsed, dict) and "tool" in parsed:
        return "accepted"
    if isinstance(parsed, dict) and "name" in parsed:
        return "name/arguments"
    return "other-json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://sber-model:11434/v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args()
    url = f"{args.base_url.rstrip('/')}/chat/completions"

    times: dict[str, list[float]] = {"native": [], "json_schema": [], "ladder": []}
    seen: dict[str, str] = {}

    for run in range(args.repeats):
        seed = run * 3
        # Each condition gets a system prompt the server has not seen, so that what is
        # measured is a first call and not a cache hit from the condition before it.
        seconds, answer = request(url, native(args.model, seed), args.timeout)
        times["native"].append(seconds)
        seen["native"] = verdict(answer)
        print(f"  native      {seconds:7.1f} s  reply: {seen['native']}", flush=True)

        seconds, answer = request(url, json_schema(args.model, seed + 1), args.timeout)
        times["json_schema"].append(seconds)
        seen["json_schema"] = verdict(answer)
        print(f"  json_schema {seconds:7.1f} s  reply: {seen['json_schema']}", flush=True)

        # The ladder as the agent runs it today: the native attempt, then the demotion,
        # both against the same prompt.
        first, _ = request(url, native(args.model, seed + 2), args.timeout)
        second, answer = request(url, json_schema(args.model, seed + 2), args.timeout)
        times["ladder"].append(first + second)
        seen["ladder"] = verdict(answer)
        print(
            f"  ladder      {first + second:7.1f} s  ({first:.1f} + {second:.1f})"
            f"  reply: {seen['ladder']}",
            flush=True,
        )

    print("\nmedian seconds")
    for name in ("native", "json_schema", "ladder"):
        print(f"  {name:<12}{statistics.median(times[name]):7.1f}   reply {seen[name]}")
    saved = statistics.median(times["ladder"]) - statistics.median(times["json_schema"])
    print(f"\nwhat dropping the rung would save on one first call: {saved:.1f} s")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.URLError as error:
        raise SystemExit(f"endpoint unreachable: {error}") from error
