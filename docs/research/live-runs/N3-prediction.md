# N3 — assisted source diagnostic

Registered before new model runs, 2026-09-07. Agent code: `52455eb`, identical
submission to N1 B (`21a43317d6ed9e78473e7334b6a9ed04e21d0dee1cf80af6ed3e1a782d91bf99`).

Question: can the current model/loop produce an officially accepted audit when
one relevant source is already supplied? This is an assisted diagnostic, not an
autonomous public-score improvement or acceptance of a runtime change.

Protocol: fresh B then assisted A, same archive, model, native_tools default,
4096 context, 600-second task limit and unchanged official task environment/tests.
Only A's instruction receives a labelled verbatim copy of
`environment/app/routers/auth.py`. No solution, checker, expected finding, payload
or added vulnerability hint enters the input. The original source itself contains
`# VULNERABLE: raw f-string concatenation`; this limits any claim of unaided
understanding. Source selection is manual and task-specific only in this diagnostic.

Prediction: A writes a substantive finding accepted by the official verifier;
B remains zero. A zero with a confirmed supplied source means navigation alone
is insufficient. Inspect whether the loss is reasoning, report/schema, controller
or timeout. A zero caused by truncation/transport is inconclusive about reasoning.
If A passes, repeat A then B to control order. If A fails, inspect the evidence
before another run or any runtime change.

Runtime observation before runs: `ollama ps` reports 100% CPU, context 4096,
model `qwen2.5-coder:7b-instruct-q4_K_M` ID `dae161e27b0e`. This differs from N1's
claimed partial GPU offload, so use a fresh B and do not compare latency to N1.
No competing suite process or task container was present. Preserve request bodies,
traces, final candidate even on success, raw official pytest output, token splits,
wall time, checksums and each zero's cause.

Research harness: `docs/research/n3-source-diagnostic.py` delegates to the existing
runner, asserts that only the instruction changes, and adds read-only post-run
capture. Neither `agent/` nor official fixtures are edited.
