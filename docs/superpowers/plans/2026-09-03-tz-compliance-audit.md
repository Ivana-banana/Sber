# TZ Compliance Audit and Remaining Runs Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove which competition requirements the current repository satisfies, identify every evidence gap, and execute the highest-priority remaining reproducible run without weakening solved count.

**Architecture:** Treat `docs/task.md` as the normalized requirement ledger and verify every normative statement against source, tests, a freshly built archive, and the pinned target image. Keep compliance evidence separate from model-quality evidence. Only after the audit selects a remaining experiment may production behavior change, and that experiment must compare one factor against a fresh control.

**Tech Stack:** Python 3.12 standard library, pytest, POSIX shell, Docker, existing repository evaluation scripts.

**Spec:** `docs/task.md`

## Global Constraints

- The archive must be a `.zip` of at most 10 MiB with executable `run.sh` at its root.
- Runtime is offline in `secureintelligent/acp`; no dependency installation or internet access is permitted.
- The runner calls `./run.sh "<task instruction>"` and supplies `LOCAL_AGENT_MODEL`, `OPENAI_BASE_URL`, and `OPENAI_API_KEY`.
- Each task has a finite time/token budget; the agent must have stop conditions and verification reserve.
- Production code and prompts must not hardcode public task names, paths, flags, or reference answers.
- Ranking is solved count first, total LLM tokens second, wall time third.
- Every experiment records commit, model, image, command, solved count, tokens, time, and a primary reason for every zero.
- No runtime dependency may be added.

---

### Task 1: Requirement-to-evidence ledger

**Files:**
- Create: `docs/tz-compliance-audit.md`
- Modify: `PLAN.md`
- Modify: `HANDOFF.md`
- Modify: `JOURNAL.md`

**Interfaces:**
- Consumes: normative statements in `docs/task.md`, source under `agent/`, packaging scripts, tests, and existing experiment cards.
- Produces: a row-per-requirement table with status `PASS`, `PARTIAL`, `BLOCKED`, or `NOT REQUIRED`, exact evidence, and the next executable check.

- [x] **Step 1: Verify the normalized spec still matches the immutable source**

Run:

```sh
sha256sum "Тз сбер.docx"
```

Expected: `5835a7a79d97a7390b3daf4cbd60439be38df8062be2694765010a98f9bf6d9c`.

- [x] **Step 2: Map every normative requirement to implementation and tests**

Inspect:

```sh
rg -n "MAX_SUBMISSION_BYTES|MODEL_ENV_NAME|BASE_URL_ENV_NAME|API_KEY_ENV_NAME|DEFAULT_TASK_TIMEOUT_S|VERIFY_RESERVE|MAX_STEPS|PUBLIC_TASK_NAMES" agent tests scripts
```

Write `docs/tz-compliance-audit.md` with separate sections for submission contract, runtime/offline behavior, endpoint contract, budget/termination, security/generality, task-capability coverage, and evaluation evidence.

- [x] **Step 3: Record only genuinely human-dependent gaps in `BLOCKED.md`**

The organizer's archive extraction method, hidden model/server, hidden task image Dockerfiles, and missing metadata for the already-submitted KT4 artifact are external gaps. Local failures and missing tests remain actionable work, not blockers.

- [x] **Step 4: Check the ledger for omissions**

Run:

```sh
rg -n '^\| .+ \| (PASS|PARTIAL|BLOCKED|NOT REQUIRED) \|' docs/tz-compliance-audit.md
rg -n '10 MiB|run\.sh|LOCAL_AGENT_MODEL|OPENAI_BASE_URL|OPENAI_API_KEY|offline|budget|reserve|hardcod|15|token|time' docs/tz-compliance-audit.md
```

Expected: every normative category has at least one explicit status row and evidence path.

- [x] **Step 5: Commit the audit ledger**

```sh
git add docs/tz-compliance-audit.md PLAN.md HANDOFF.md JOURNAL.md docs/superpowers/plans/2026-09-03-tz-compliance-audit.md
git commit -m "docs: audit competition requirements against evidence"
git push origin eval/live-baseline
```

### Task 2: Fresh compliance baseline

**Files:**
- Modify: `docs/tz-compliance-audit.md`
- Modify: `docs/experiments.md`
- Modify: `JOURNAL.md`

**Interfaces:**
- Consumes: current committed source and the pinned `secureintelligent/acp` OCI digest.
- Produces: fresh unit, archive, extracted-entrypoint, offline-container, and static-policy evidence tied to one commit and archive hash.

- [x] **Step 1: Run the unit and contract suite**

```sh
python3 -m pytest -q
```

Expected: all collected tests pass; skips, if any, are named and not counted as target-image proof.

- [x] **Step 2: Build and inspect the actual archive**

```sh
python3 scripts/build_submission.py --out build/tz-audit-submission.zip
python3 scripts/check_submission.py build/tz-audit-submission.zip
sha256sum build/tz-audit-submission.zip
```

Expected: all archive checks pass, `run.sh` is at archive root with Unix executable mode, size is at most 10 MiB, and no excluded development files are present.

- [x] **Step 3: Attempt the pinned image and run the available offline substitute**

Inspect `secureintelligent/acp@sha256:1324d7bb140422a9929f5bc538f55ec0480d2731742effee02bf67604130c8a3`. It is absent on this daemon, so record the gate as `PARTIAL` and run the exact archive in the available `python:3.12-slim@sha256:78387bc3…` substitute with `--network none --read-only`. Non-empty must return `0`, empty must return `2`, and the mounted source checksum must remain unchanged. Do not relabel the substitute as ACP evidence.

- [x] **Step 4: Run static anti-overfit and secret checks**

```sh
rg -n -i "bye-file|hello-file|find-sqli-login|fix-sqli-login|fix-sqli-search|incident-log-forensics" agent run.sh
rg -n "OPENAI_API_KEY=|sk-[A-Za-z0-9_-]{12,}|Bearer [A-Za-z0-9._-]{12,}" agent run.sh scripts tests docs
git diff --check
```

Expected: no production hardcode or secret value; documented variable names and test fixtures are classified separately from leaked values.

- [x] **Step 5: Record the baseline as an experiment and commit**

Append a card with commit SHA, commands, archive size/hash, image digest, test counts, duration, and any zero/failure taxonomy to `docs/experiments.md`; update the ledger and journal; then commit and push the documentation only.

### Task 3: Restore the accepted candidate and measure it

**Files:**
- Revert: E014 changes in `agent/loop.py` and `tests/test_loop.py` through a normal Git revert.
- Modify: `docs/experiments.md`, `docs/research/live-runs/`, `JOURNAL.md`, `HANDOFF.md`, and `PLAN.md`.

**Interfaces:**
- Consumes: the recorded rejection of E014, the existing 18-task utility suite, and the local `qwen2.5-coder:7b-instruct-q4_K_M` endpoint.
- Produces: an accepted current branch without the known losing factor and a fresh 18-row utility baseline tied to its exact archive.

- [x] **Step 1: Remove the rejected E014 factor**

Inspect `git show 2c7bdc5`, then create a normal revert commit that removes only its fenced/OpenAI-envelope parsing and its matching tests. Run the targeted loop tests and the full suite. Do not rewrite published history.

- [x] **Step 2: Establish a fresh utility baseline**

Start the existing model and counting proxy, build `build/e017-accepted-control.zip`, preflight that exact file, and run:

```sh
sh scripts/eval/start_model.sh
sh scripts/eval/start_proxy.sh
python3 scripts/build_submission.py --out build/e017-accepted-control.zip
python3 scripts/check_submission.py build/e017-accepted-control.zip
python3 scripts/eval/run_suite.py --tasks-root local_task_extra/tasks --model qwen2.5-coder:7b-instruct-q4_K_M --base-url http://sber-proxy:8000/v1 --api-key ollama --network sber-eval --counter-container sber-proxy --timeout-scale 1.0 --archive build/e017-accepted-control.zip --out build/eval-E017-accepted-control --label E017-accepted-control
```

Expected: 18 complete rows from the explicitly supplied archive, external reward for each task, endpoint calls/tokens, wall time, stop reason, and primary failure mode.

- [x] **Step 3: Record and independently criticise E017**

Store the compact raw TSV, exact command, archive hash/size, model manifest, image digests, solved/tokens/time, and reason for every zero. Give the actual revert diff, test output, raw TSV, and summary to an independent critic; address reproducible objections.

- [x] **Step 4: Close the run without inventing a security design**

Run `pytest -q`, archive preflight, `git diff --check`, and production hardcode/secret scans. Commit and push E017 documentation. Record three concrete security-treatment approaches from E015, but do not implement one until the user approves the architectural trade-off required by the brainstorming gate.

Closed by E017/E018 evidence and final independent critique. The three unselected
security candidates are recorded in `docs/decisions.md`: submission-side Landlock,
host/runtime confinement, and replacement of free shell actions with typed mediated
operations. None is treated as approved production behavior.
