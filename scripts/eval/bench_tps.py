#!/usr/bin/env python3
"""Measure prompt-eval and generation throughput of the model endpoint.

Development tool for choosing a stand. Runs inside the proxy container, the only place
with a route to the model:

    docker exec sber-proxy python3 /opt/eval/bench_tps.py --model qwen3:30b-a3b

It talks to ollama's native /api/chat because that response carries exact token counts
and durations (prompt_eval_count/duration, eval_count/duration); the OpenAI shim only
reports usage. The prompt is a fixed block of about --prompt-tokens tokens so prefill is
comparable across models; --max-tokens bounds generation. A thinking model spends its
tokens on reasoning -- fine, the number measured is raw decode speed either way.

Prints one TSV row per run and a summary line; no stand state is changed.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request


def _prompt(target_tokens: int) -> str:
    # ~1.3 tokens per word on Qwen tokenizers; a plain, non-repetitive text so the
    # server cannot skip work via an exact prefix cache between runs.
    words = ("audit log review: request id %d from host h%d returned status %d after %d ms; ")
    out = []
    n = 0
    i = 0
    while n < target_tokens:
        chunk = words % (1000 + i, i % 17, 200 + (i % 5) * 100, 3 + i % 97)
        out.append(chunk)
        n += len(chunk.split()) * 1.3
        i += 1
    return "".join(out) + "\nSummarise the anomalies you see in three sentences."


def _run(upstream: str, model: str, prompt: str, max_tokens: int, timeout: float) -> dict:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"num_predict": max_tokens, "temperature": 0},
    }).encode()
    req = urllib.request.Request(f"{upstream}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read())
    wall = time.monotonic() - t0
    pe_n = payload.get("prompt_eval_count", 0)
    pe_d = payload.get("prompt_eval_duration", 0) / 1e9
    ev_n = payload.get("eval_count", 0)
    ev_d = payload.get("eval_duration", 0) / 1e9
    return {
        "wall_s": round(wall, 1),
        "prompt_tokens": pe_n,
        "prompt_tps": round(pe_n / pe_d, 1) if pe_d else None,
        "gen_tokens": ev_n,
        "gen_tps": round(ev_n / ev_d, 1) if ev_d else None,
        "load_s": round(payload.get("load_duration", 0) / 1e9, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--upstream", default="http://sber-model:11434")
    ap.add_argument("--model", required=True)
    ap.add_argument("--prompt-tokens", type=int, default=3000)
    ap.add_argument("--max-tokens", type=int, default=512)
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=600.0)
    args = ap.parse_args()

    prompt = _prompt(args.prompt_tokens)
    rows = []
    print("run\twall_s\tload_s\tprompt_tokens\tprompt_tps\tgen_tokens\tgen_tps")
    for i in range(1, args.runs + 1):
        r = _run(args.upstream, args.model, prompt, args.max_tokens, args.timeout)
        rows.append(r)
        print(f"{i}\t{r['wall_s']}\t{r['load_s']}\t{r['prompt_tokens']}\t{r['prompt_tps']}\t{r['gen_tokens']}\t{r['gen_tps']}")
        sys.stdout.flush()
    warm = [r for r in rows[1:] if r["gen_tps"]] or rows
    gen = sum(r["gen_tps"] or 0 for r in warm) / len(warm)
    pe = sum(r["prompt_tps"] or 0 for r in warm) / len(warm)
    print(f"summary\tmodel={args.model}\tgen_tps={gen:.1f}\tprompt_tps={pe:.1f}\truns={len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
