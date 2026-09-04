"""Does a shared prompt prefix still pay off after the calls that come between?

Development tool. Runs inside the proxy container, which is the only place with a
route to the model:

    docker exec sber-proxy python3 /opt/eval/bench_prefix.py \
        --model qwen2.5-coder:7b-instruct-q4_K_M

E034 moved the shared blocks of the system prompt to the front so consecutive tasks
would share a prefix the server could keep. Back to back on this stand that was worth
8 seconds on the first call, 22.1 s down to 14.1 s. In the suite it was worth nothing.
The difference between the two settings is what happens in between: the bench asked
for two first calls in a row, the suite runs about eight long calls of the first task
before the second task's first call arrives, each one longer than the last as
observations accumulate.

So this measures one first call under four histories:

    cold        a prefix the server has never seen
    repeat      the identical request again
    adjacent    same prefix, new tail, nothing in between   -- the E034 bench
    worked      same prefix, new tail, after N calls of a task -- what the suite does

`worked` is the number that matters. If it lands on `cold`, the prefix was evicted and
no ordering of blocks can help across tasks on this server; the saving is real only
inside one task, where it is already collected. If it lands on `adjacent`, eviction is
not the explanation and E034's null result needs another one.

Times are wall clock around one request with a 16-token cap, because prefill is what
is being compared and decode would add noise proportional to how chatty the model
feels. `prompt_tokens` from the reply is printed next to each number so a comparison
between two differently sized prompts is visible rather than assumed.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.error
import urllib.request

SHARED = """\
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

РАБОТА
Сначала осмотрись, потом меняй. Каждый шаг приближает к проверяемому результату.
Не выдумывай пути и имена файлов: сначала посмотри, что есть на диске.

ПРОВЕРКА
Результат считается готовым только после команды, которая падает на неверном ответе.
Команда, которая печатает файл, ничего не проверяет.

ОТВЕТ
Ровно один JSON-объект по схеме, без пояснений вокруг него.
"""
"""A stand-in for the resident part of the real prompt: same size band, same language."""

TAILS = (
    "ЗАДАЧА\nНайди захардкоженный токен в /app/service.py и вынеси его в переменную "
    "окружения, не меняя поведения остального кода.",
    "ЗАДАЧА\nВ /app/extract.py архив распаковывается без проверки путей. Закрой обход "
    "каталога, сохранив распаковку обычных файлов.",
    "ЗАДАЧА\nВ /srv/logs/access.log найди источник перебора SSH и запиши его адрес в "
    "/srv/report/answer.txt одной строкой.",
)
"""Three per-task tails: what changes between two tasks that share the resident block."""

OBSERVATION = (
    "НАБЛЮДЕНИЕ шаг {step}\nexit=0\n"
    + "\n".join(f"{n:4d}\tdef handler_{n}(request, context):  # branch {n}" for n in range(40))
    + "\n"
)
"""One step's observation, sized like a `read_range` window in the real loop."""


def ask(url: str, model: str, system: str, history: list[str], timeout: float) -> tuple[float, int]:
    """One completion. Returns wall seconds and the prompt tokens the server counted."""
    messages = [{"role": "system", "content": system}]
    for index, text in enumerate(history):
        messages.append({"role": "assistant" if index % 2 else "user", "content": text})
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": 16,
            "stream": False,
        }
    ).encode()
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        body = json.loads(reply.read().decode())
    elapsed = time.monotonic() - started
    return elapsed, int((body.get("usage") or {}).get("prompt_tokens") or 0)


def filler(seed: int) -> str:
    """A resident block the server has certainly not seen, at the same size as SHARED."""
    return SHARED.replace("шаг", f"шаг-{seed}").replace("файл", f"файл-{seed}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://sber-model:11434/v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--steps", type=int, default=8, help="calls between the two firsts")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args()
    url = f"{args.base_url.rstrip('/')}/chat/completions"

    results: dict[str, list[float]] = {}
    tokens: dict[str, int] = {}

    def record(name: str, seconds: float, prompt_tokens: int) -> None:
        results.setdefault(name, []).append(seconds)
        tokens[name] = prompt_tokens
        print(f"  {name:<10} {seconds:7.1f} s  {prompt_tokens:5d} prompt tokens", flush=True)

    for run in range(args.repeats):
        print(f"round {run + 1}/{args.repeats}", flush=True)

        # cold: a resident block never sent before, so nothing of it is cached.
        record("cold", *ask(url, args.model, filler(run * 7 + 1), [TAILS[0]], args.timeout))

        # repeat: the identical request, the easiest possible cache hit.
        record("repeat", *ask(url, args.model, filler(run * 7 + 1), [TAILS[0]], args.timeout))

        # adjacent: same resident block, different task, nothing in between. The
        # priming call is what makes the block shared at all -- without it the first
        # round measures a cold prompt and calls it adjacent.
        ask(url, args.model, SHARED, [TAILS[0]], args.timeout)
        seconds, count = ask(url, args.model, SHARED, [TAILS[1]], args.timeout)
        record("adjacent", seconds, count)

        # worked: same resident block, but a task's worth of calls happened in between,
        # each one longer than the last -- the shape the suite actually produces.
        history = [TAILS[1]]
        for step in range(args.steps):
            history = [*history, OBSERVATION.format(step=step)]
            ask(url, args.model, SHARED, history, args.timeout)
        record("worked", *ask(url, args.model, SHARED, [TAILS[2]], args.timeout))

    print("\nmedian seconds")
    for name in ("cold", "repeat", "adjacent", "worked"):
        if results.get(name):
            print(f"  {name:<10} {statistics.median(results[name]):7.1f}  ({tokens[name]} tokens)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.URLError as error:  # the endpoint is the usual failure here
        raise SystemExit(f"endpoint unreachable: {error}") from error
