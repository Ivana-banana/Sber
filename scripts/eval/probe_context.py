"""Ask the endpoint directly whether it truncates, and from which end.

Development tool. `agent/` never imports it and it never enters the archive.

Why it exists. E055 changes the serving context window and reads the answer off a
suite: solved went up, or it did not. That is an indirect answer to a direct question,
it costs an hour, and a negative result would have two incompatible explanations --
the server was never truncating, or our own code never sent the history in the first
place. This asks the endpoint instead, in about a dozen calls.

Two probes, deliberately independent of the agent:

`needle`  puts a marker at the very start of the system block, pads the conversation to
          a target size, and asks the model to repeat the marker. A model that cannot
          see a marker it was just given has not been given it. Sweeping the target
          size finds the cliff, and putting a second marker at the end of the last user
          message says which end was cut -- which matters more than the fact of cutting:
          losing the head means losing the tool contract, and that is a different
          defect from losing old observations.

`replay`  resends a conversation shaped like the one that wedged in E048 -- a file read
          whose result is already in the history -- and reports whether the model asks
          for the same read again. This is the observed failure, reproduced without
          docker, without the agent and without a task package.

Neither probe proves anything about the evaluation endpoint. They characterise this
stand, which is the thing every other number in this project is measured on.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

HEAD_MARK = "ALPHA-7731"
TAIL_MARK = "OMEGA-4412"

# Rough and deliberately so: this is used to hit a target prompt size, not to report
# one. The server's own `prompt_tokens` is the measurement; this only builds the input.
CHARS_PER_TOKEN = 3.6


def ask(base_url: str, model: str, messages: list[dict], max_tokens: int, timeout: float) -> dict:
    body = json.dumps(
        {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.0}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer probe"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return {"error": f"http {exc.code}", "detail": exc.read().decode("utf-8", "replace")[:400]}
    except OSError as exc:
        return {"error": f"unreachable: {exc}"}
    usage = answer.get("usage") or {}
    choice = (answer.get("choices") or [{}])[0]
    return {
        "wall_s": round(time.monotonic() - started, 2),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "content": str((choice.get("message") or {}).get("content") or ""),
    }


def needle(base_url: str, model: str, target_tokens: int, timeout: float) -> dict:
    """One point of the sweep: can the model still see both ends of its own input?"""
    system = (
        f"SESSION-ID: {HEAD_MARK}\n"
        "You are a file-reading assistant. Answer with two tokens only.\n"
    )
    padding_chars = max(0, int(target_tokens * CHARS_PER_TOKEN) - len(system) - 400)
    # Padding that cannot be summarised away or predicted: distinct numbered lines.
    lines = []
    filled = 0
    index = 0
    while filled < padding_chars:
        line = f"log line {index:05d}: routine event, nothing of interest recorded here\n"
        lines.append(line)
        filled += len(line)
        index += 1
    padding = "".join(lines)
    user = (
        f"{padding}\nREQUEST-ID: {TAIL_MARK}\n"
        "Repeat the SESSION-ID from your instructions, then the REQUEST-ID above. "
        "Nothing else."
    )
    result = ask(
        base_url, model,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=32, timeout=timeout,
    )
    if "error" in result:
        return {"target_tokens": target_tokens, **result}
    text = result["content"]
    return {
        "target_tokens": target_tokens,
        "prompt_tokens": result["prompt_tokens"],
        "sent_chars": len(system) + len(user),
        "head_survived": HEAD_MARK in text,
        "tail_survived": TAIL_MARK in text,
        "wall_s": result["wall_s"],
        "reply": text.replace("\n", " ")[:120],
    }


def replay(base_url: str, model: str, repeats: int, timeout: float) -> dict:
    """The E048 shape: the answer is already in the history. Is it asked for again?

    The history is written here rather than lifted from a trace on purpose -- a trace
    carries a task's own text, and a probe that needs a task package is not cheaper
    than the suite it is meant to replace.
    """
    system = (
        "You are a security auditor working in /app. Call exactly one tool per reply, "
        'as JSON: {"tool": "...", ...}. Tools: list_dir(path), read_range(path, '
        "start_line), run_shell(command), write_file(path, content)."
    )
    file_body = (
        "def login(request):\n"
        "    name = request.form['login_name']\n"
        "    query = \"SELECT * FROM users WHERE name = '\" + name + \"'\"\n"
        "    return db.execute(query)\n"
    )
    history: list[dict] = [{"role": "system", "content": system}]
    history.append({"role": "user", "content": "Audit /app for SQL injection and write /app/report.json."})
    history.append({"role": "assistant", "content": '{"tool": "list_dir", "path": "/app"}'})
    history.append({"role": "user", "content": "OBSERVATION: main.py\ntests/\nREADME.md"})
    # The read, and its result, repeated so the answer is unmistakably already present.
    for _ in range(repeats):
        history.append(
            {"role": "assistant", "content": '{"tool": "read_range", "path": "/app/main.py", "start_line": 1}'}
        )
        history.append({"role": "user", "content": f"OBSERVATION:\n{file_body}"})
    result = ask(base_url, model, history, max_tokens=200, timeout=timeout)
    if "error" in result:
        return {"repeats": repeats, **result}
    text = result["content"]
    asked_again = '"read_range"' in text and "main.py" in text
    return {
        "repeats": repeats,
        "history_messages": len(history),
        "history_chars": sum(len(str(m["content"])) for m in history),
        "prompt_tokens": result["prompt_tokens"],
        "asked_for_the_same_read_again": asked_again,
        "mentions_login_name": "login_name" in text,
        "wall_s": result["wall_s"],
        "reply": text.replace("\n", " ")[:200],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument(
        "--targets", type=int, nargs="*", default=[1000, 2000, 3000, 3800, 5000, 7000],
        help="prompt sizes to sweep, in tokens",
    )
    parser.add_argument(
        "--replays", type=int, nargs="*", default=[1, 4, 10],
        help="how many times the same read/observation pair is repeated in the history",
    )
    args = parser.parse_args()

    print("# needle: can the model still see both ends of its own input?", flush=True)
    for target in args.targets:
        print(json.dumps(needle(args.base_url, args.model, target, args.timeout)), flush=True)

    print("# replay: is a read whose answer is already in the history asked for again?", flush=True)
    for repeats in args.replays:
        print(json.dumps(replay(args.base_url, args.model, repeats, args.timeout)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
