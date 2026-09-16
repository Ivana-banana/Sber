# Oracle replay

A scripted OpenAI-compatible endpoint that answers each call with the next reference
action for the task, so the real archive runs its real tools, policies and verifier
with a model that never errs. A zero here is the agent's fault, not the model's.

```
python3 scripts/eval/replay/extra_script.py            # writes extra.json from solution/solve.sh
python3 scripts/eval/replay/public_fix_script.py login # writes public-login.json
docker run -d --name sber-replay --network sber-eval -v "$PWD/scripts/eval/replay":/r:ro \
  -v /tmp/calls.jsonl:/log.jsonl python:3.12-slim python3 /r/endpoint.py /r/extra.json /log.jsonl
python3 scripts/eval/run_suite.py --tasks-root local_task_extra/tasks --model replay \
  --base-url http://sber-replay:8000/v1 --api-key x --archive build/<cand>.zip \
  --label REPLAY-<cand> --out build/eval-REPLAY-<cand>
```

`/log.jsonl` holds every action sent and the last observation the agent returned, which
is where a refusal shows. `$KILL` and `$READY` in a script are filled from the latest
stale-service hint. Scripts are keyed by a phrase of the instruction.

The endpoint keeps a per-task cursor for the whole life of the container: a second
suite against the same `sber-replay` starts every task past the end of its script and
scores 0/18 with `stop=steps` (REPLAY-fc, 2026-09-17). `docker rm -f sber-replay` and
start it again before every run.
