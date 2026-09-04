# Reducing End-to-End Latency in a Deadline-Constrained Local LLM Cybersecurity Agent

## Bottom line

Your failure profile—**13 of 14 unfinished tasks dying on wall-clock timeout rather than on reasoning or verification errors**—strongly suggests that the first optimization target should be *trajectory latency*, not better reasoning. For a single-agent `inspect → plan → act → verify` loop on a local ~7B model, the highest-leverage changes are likely to be:

**collapse planning and action into one generation; allow multiple independent read-only actions from that generation; keep the conversation prefix byte-for-byte stable so vLLM/llama.cpp can reuse KV state; compress each tool observation before it enters history rather than repeatedly rewriting old history; use speculative decoding only after benchmarking a small same-family draft; and replace the fixed 30-step/fixed-percentage budget with a live “how many rounds can still fit?” estimator.**

The empirical literature supports unusually large gains in exactly these areas. LLMCompiler reports up to **3.7× latency speedup** by parallelizing dependent/independent function calls; speculative tool execution systems report **20–48.5% task-time reductions**; terminal-specific observation compression reports **12–27% fewer tokens with equal or better success**; a llama.cpp experiment with a **Qwen2.5-Coder-7B target** reports **1.75× token-generation speedup** from a 0.5B draft; and time-aware 8B agents improve on-time completion by **8–23 percentage points** under deliberately tight budgets. citeturn4view5turn0academia41turn15view5turn19view5turn22view0

There is one particularly important interaction for your architecture: **prompt compression and prefix caching can work against each other**. A 2026 coding-agent study found that sliding-window trajectory compression removed **40.3% of tokens but reduced cost by only 8.6%**, while another trajectory compressor removed **43.7% of tokens yet increased cost by 10.6%**, because rewriting prior context destroyed KV/prefix-cache reuse. For your repeated local-agent turns, that argues strongly for *compress-on-ingress*: shorten a terminal observation once, append it, and never rewrite it unless you deliberately start a new context epoch. citeturn15view1

The numbers below come mainly from primary papers, official vLLM/Qwen documentation, and experiments in the official llama.cpp repository. Several of the most relevant 2026 systems are recent arXiv preprints, so their absolute benchmark numbers should not be assumed to transfer directly to a 7B CTF agent; I distinguish direct 7B/local evidence from larger-model or serving-system evidence where this matters. citeturn20view0turn20view1turn19view5


## Fewer model calls per task

### Finding — Compile several actions from one LLM turn instead of one turn per action

**LLMCompiler** is probably the clearest measured precedent for your “batch tool calls / combine planning+action” requirement. Instead of the classical ReAct pattern of serially deciding and executing one tool at a time, its planner emits an execution plan whose independent tool calls can run in parallel and whose dependencies are scheduled as a DAG. Across its evaluated tool-use workloads, the paper reports **up to 3.7× lower latency**, **up to 6.7× cost reduction**, and **up to roughly 9% higher accuracy** than ReAct. citeturn4view5

For your loop, I would not have separate `plan()` and `act()` LLM requests. Make one request produce something structurally equivalent to:

```text
decision:
  status: continue | finish | recover
  rationale: <= 40 tokens
  actions:
    - command: ...
      dependency: []
      read_only: true
    - command: ...
      dependency: []
      read_only: true
  success_check: ...
```

If `plan` and `act` are currently two serial model calls on every iteration, merging them reduces those phase calls **from two to one—exactly 50% fewer LLM invocations for that portion of the loop**, independent of model speed. The LLMCompiler result indicates that the next step—issuing several independent actions from that same decision—can produce additional wall-clock gains when tools themselves have non-trivial latency. citeturn4view5

This maps unusually well to CTF/forensics. A single inspection decision can often request, for example, `file`, `stat`, `strings`, selected `readelf` views, targeted `grep`, and metadata collection at once rather than spending six LLM rounds discovering them serially. For mutating operations, keep dependency ordering explicit rather than blindly parallelizing them; LLMCompiler's gains come from exploiting actual independence rather than merely running everything concurrently. citeturn4view5

### Finding — Execute likely tools while generation is still happening

Two recent systems give quantitative evidence for **speculative tool execution**, which is different from speculative *token decoding*.

**PASTE / “Act While Thinking”** identifies recurrent action patterns and begins executing predictable tools while the LLM is still producing its decision. Its reported experiments reduce average task-completion time by **48.5%** and improve tool-execution throughput by **1.8×** relative to its agent baselines. citeturn0academia41

**Speculative Actions** instead uses a faster predictor to guess forthcoming actions, starts them in parallel, then commits their results only if the main agent actually chooses the predicted action. Across gaming, e-commerce, and web-search workloads, the paper reports **up to 55% next-action prediction accuracy** and **up to 20% latency reduction**. citeturn13view4

For an isolated CTF microVM, the practical low-risk subset is compelling: speculate **read-only/idempotent** operations, not arbitrary fixes. If the latest observation is an ELF binary, for example, you can start predictable metadata/disassembly searches while the model decides what it wants next. Filesystem-changing actions should either remain serialized or be run against a snapshot/copy, because speculative execution becomes much harder once an incorrect branch has externally visible side effects; recent transactional-agent work explicitly identifies this commit/rollback problem. citeturn14search15

### Finding — Stop on convergence rather than consuming the entire step allowance

A 2026 study of semantic early stopping found that a judge-free semantic convergence criterion reduced **operational token use by 38%** relative to a fixed `max_iterations` policy while maintaining essentially unchanged quality on its evaluation split; adding a separate judge every round was counterproductive because the judge itself consumed enough inference to erase the benefit. citeturn12view0

The exact benchmark is not a terminal/CTF agent, so I would transfer the *mechanism*, not the reported 38% literally. In your environment, useful cheap convergence signals can be computed without another LLM call:

```text
stop exploration when any is true:
    verifier already passes
    same command class failed twice with materially identical stderr
    last N observations introduce no new files/symbols/errors/evidence
    proposed action is equivalent to a recently executed action
    remaining time cannot fit another normal round plus verification
```

The key empirical lesson is that **an LLM judge is the wrong way to save LLM latency** when the model itself is your bottleneck. A deterministic state/observation signature is more compatible with your stdlib-only architecture. citeturn12view0


## Faster local inference with vLLM or llama.cpp

### Finding — Four-bit weight quantization can be a major win, but benchmark your actual backend

Qwen's official quantization documentation states that **AutoAWQ 4-bit inference is about 3× faster and uses about 3× less memory than FP16**, and specifically demonstrates `Qwen2.5-7B-Instruct-AWQ`. The same documentation supports serving those AWQ models through vLLM's OpenAI-compatible server. citeturn20view0

That makes **AWQ the first format I would benchmark for a CUDA/vLLM deployment** if the current copy of your 7B is FP16/BF16 or an otherwise poorly optimized quant. This is not a promise of 3× end-to-end agent acceleration: Qwen's quoted figure is a model-level quantization result and your agent also spends time on prefill, tools and verification. Nevertheless, for a timeout-dominated single-user workload, reducing weight-bandwidth pressure is exactly the right direction. citeturn20view0

For llama.cpp/GGUF, do **not** select the smallest bit count purely by model size. Backend kernels, CPU/GPU split, memory bandwidth and the particular quantization format can alter the speed hierarchy. The official llama.cpp speculative-decoding discussion, for example, reports different practical behavior for `IQ4_XS`, `Q6_K` and other formats depending on backend, while its reported perplexity measurements also show a quality penalty as quantization becomes more aggressive. citeturn19view5

For security/tool calling, where one malformed command can cost an extra entire inference round, I would generally benchmark **Q4/Q5/Q6-class GGUF rather than optimizing solely for minimum bytes**. The right metric is not just tok/s; use:

\[
\text{successful tasks per wall-clock hour}
\]

because a quant that is 15% faster but adds enough command/reasoning errors to cause another retry can lose end-to-end. This is an inference from the reported quantization-quality and speculative-agent behavior rather than a universal llama.cpp rule. citeturn19view5

### Finding — Cross-turn KV/prefix reuse is exceptionally well matched to your loop

vLLM's **Automatic Prefix Caching** caches a request's KV blocks and lets a later request sharing the same prefix skip computation of that entire shared prefix. The official documentation explicitly lists **multi-round conversations** as a target workload: instead of prefilling the whole conversation again on every turn, vLLM reuses previously computed history. APC affects **prefill**, not generation, so it helps most when your prompts/history are large relative to the few tokens needed to choose the next tool. citeturn20view1turn20view2

This is almost exactly your agent traffic pattern:

```text
turn 1: system + task + O1
turn 2: system + task + O1 + A1 + O2
turn 3: system + task + O1 + A1 + O2 + A2 + O3
...
```

As long as the prefix stays exactly stable, each new turn should need prefill primarily for the newly appended suffix rather than for the entire accumulated history. vLLM enables this with `enable_prefix_caching=True`. citeturn20view1

The magnitude can be very large in genuinely agentic serving workloads. **Continuum**, a 2026 KV-cache retention system designed around the fact that agents pause for tool calls between LLM invocations, reports **more than 8× improvement in average job completion time** while improving throughput on SWE-Bench, BFCL and OpenHands workloads, including **Llama-3.1 8B**. Its mechanism is to retain/pin useful cross-turn KV state through tool pauses instead of immediately evicting it. citeturn20view5

The `>8×` number should **not** be projected onto your single-agent microVM: much of Continuum's gain concerns scheduling and cache eviction under shared-serving contention. But it is strong systems evidence that treating a tool pause as the end of an unrelated request—as traditional LLM serving often does—is particularly damaging to agentic workloads. citeturn20view5

A practical implication follows: keep your stable text at the **front**, append new state at the **end**, and avoid putting volatile timestamps, current-budget strings, random IDs, or reconstructed summaries near the beginning of the prompt. Those seemingly innocuous changes destroy the common prefix and therefore the cache hit. This recommendation follows directly from vLLM's exact-prefix reuse semantics. citeturn20view1

### Finding — A 0.5–1B draft can accelerate a 7–8B target, but only when the pair matches well

The most directly relevant local result I found is in the official llama.cpp repository. On an RTX 4070, with **Qwen2.5-Coder-7B-Instruct Q6_K** as target and **Qwen2.5-Coder-0.5B** as draft on a HumanEval coding task, the reported token-generation rate rises from roughly **65 tok/s to 115 tok/s**, or **1.75×**, using an eight-token draft. For a general Qwen2.5-7B task, the same 0.5B draft raises about **65.7 to 92.3 tok/s**, or **1.41×**. A Llama-3.1-8B / Llama-3.2-1B pair reaches approximately **1.83×**, while a Gemma-2 9B / 2B pair in the same experiments provides **no speedup at all**. citeturn19view5

The broader EAGLE research reports much larger **2.7–3.5×** latency speedups for Llama-2-Chat 70B and EAGLE-2 reports **3.05–4.26×** on its evaluated larger models, but those numbers are not representative targets for a plain 7B + external draft in llama.cpp. citeturn17search0turn17search8

So for your size regime, I would test:

```text
target: existing ~7B quant
draft: 0.5B first, 1B second
draft tokens: sweep 2, 4, 6, 8
workload: actual agent decision outputs, not generic prose
metric: median and p90 end-to-end request latency
```

The llama.cpp results are particularly encouraging for you because **coding had a higher speculative acceptance/speedup than the general-text task**, but they also demonstrate why speculative decoding must be benchmarked per model pair instead of enabled unconditionally. citeturn19view5

On sampling, I would use **`temperature=0` and `top_p=1` for action-selection calls**. SWE-agent itself uses those defaults for its generic API model. This should primarily be regarded as a determinism/retry optimization rather than a large raw tok/s optimization: it makes repeated tool decisions stable and avoids paying for diversity you do not want in a deadline-constrained command loop. citeturn21view2

Also cap `max_tokens` aggressively. A tool-selection turn normally does not need hundreds of tokens of reasoning. Speculative decoding improves decode cost per token; producing fewer output tokens removes the work altogether.


## Shorter prompts without destroying cache reuse

### Finding — Compress tool observations before they enter the trajectory

**CoACT**, explicitly developed for coding-agent observations, reports that observations account for **45.7% of total tokens on SWE-bench Verified and as much as 67.8% on Terminal-Bench**. Its action-preserving observation compressor reduces average total token consumption by **33.0%** while maintaining solving performance close to the uncompressed agent. On Qwen3.5-35B-A3B, tokens fall from **3.795M to 2.428M** while pass@1 actually rises from **57.0% to 60.5%** in the reported experiment. citeturn15view1

That result argues that your **“last eight raw observations” is probably the wrong unit of memory**. Eight `grep` outputs, recursive directory listings, linker dumps or test logs can dwarf the actual reasoning tokens.

For your no-extra-model architecture, I would approximate action-preserving compression deterministically at the tool layer:

```text
Always preserve:
- exit status
- stderr
- matched lines and ±N lines context
- filenames / paths
- changed values
- assertion/test failures
- hashes / offsets / symbols / addresses
- first occurrence of each distinct error

Compress:
- repeated warnings
- duplicated lines
- large directory listings
- successful test spam
- progress bars
- unchanged records
- huge hexdumps unless explicitly requested

For long output:
- head + tail + selected matches
- "[... 18,431 bytes omitted ...]"
- persist full output to /tmp/agent-artifacts/<id>
```

The full raw result remains locally available to a later `cat_range`/`grep_artifact` action, but it never contaminates every subsequent LLM prefill.

### Finding — Terminal-specific compression has already produced double-digit savings

**TACO**, a 2026 terminal-agent observation-compression framework, reports **12–27% lower total token consumption** across SWE-Bench Lite, CompileBench, DevEval and CRUST-Bench while maintaining or improving success. On TerminalBench 1.0/2.0, integrating it into terminal agents yields **1–4 percentage-point absolute accuracy gains**; under matched token budgets, accuracy improves roughly **2–3 points**. citeturn15view5

**SWE-Pruner** independently reports **23–54% token reduction** on agent tasks and, on SWE-Bench Verified with Claude Sonnet 4.5, a **39% reduction** accompanied by **up to 26% fewer interaction rounds**. Its analysis found read operations consuming **76.1%** of the examined agent's tokens. citeturn15view4

Those systems use mechanisms more elaborate than you want—a learned 0.6B pruner in SWE-Pruner, evolving compression logic in TACO—but the empirical distribution is directly useful: **read/observation output, not merely the system prompt, is often the dominant context cost in terminal agents**. citeturn15view4turn15view5

For your architecture, I would therefore spend engineering effort on per-tool formatters before adding an LLM summarizer. A deterministic `summarize_ls`, `summarize_tests`, `summarize_grep`, `summarize_readelf`, `summarize_diff`, etc. has essentially zero inference latency and is far easier to reason about under a 120-second deadline.

### Finding — Do not repeatedly summarize/rewrite the old trajectory if you want prefix caching

This is perhaps the most actionable result of the research.

CoACT's comparison shows that trajectory-level compression can lower token counts while barely lowering—or even increasing—actual inference cost. A sliding-window strategy reduces token count **40.3%** but estimated cost only **8.6%**; AgentDiet reduces tokens **43.7%** but increases estimated cost **10.6%**. The paper attributes the discrepancy to lost prefix/KV-cache reuse when earlier trajectory content is rewritten. citeturn15view1

That finding combines directly with vLLM APC, which only reuses previously computed KV blocks when a new request shares the previous request's prefix. citeturn20view1

Therefore I would replace:

```text
last 8 raw observations
→ periodically regenerate summary of previous observations
→ send rewritten prompt
```

with:

```text
stable system/task prefix
+ immutable compact observation_1
+ immutable action_1
+ immutable compact observation_2
+ immutable action_2
...
```

and only perform a **context epoch reset** when the context has genuinely become too large. At an epoch reset, create one compact state capsule such as:

```text
GOAL
KNOWN FACTS
FILES MODIFIED
TEST STATUS
FAILED APPROACHES
OPEN HYPOTHESIS
NEXT ACTION
ARTIFACT REFERENCES
```

then accept that you intentionally paid one cache miss. Between resets, never rewrite the existing prefix. This is an inference from CoACT's cache-cost ablation plus vLLM's APC semantics. citeturn15view1turn20view1


## Deadline-aware budgeting rather than a fixed iteration ceiling

### Finding — An 8B model can learn to change behavior when remaining wall time is explicit

**Timely Machine / TimelyLM** is the closest quantitative paper I found to exactly your problem. It argues that token or iteration budgets are insufficient because each agent round has both an unpredictable generation latency and an unpredictable tool latency. It explicitly measures average per-step wall-clock latency—including tools—and expresses task budgets as multiples of that latency. citeturn22view2

Under a tight **0.75× time budget**, baseline Qwen3-8B completes on time on only:

| Benchmark | Qwen3-8B | TimelyLM-8B | Absolute gain |
|---|---:|---:|---:|
| AIME | 30.0% | 53.3% | **+23.3 pp** |
| GPQA-Diamond | 36.0% | 52.0% | **+16.0 pp** |
| MATH | 48.0% | 56.0% | **+8.0 pp** |

These gains include time-aware training (`SFT + Timely-RL`), so simply adding `remaining_seconds=...` to your unmodified 7B prompt should **not** be expected to reproduce them. The transferable runtime idea is nevertheless clear: remaining time must become an explicit state variable, and the controller should reason in estimated wall-clock rounds rather than in a fixed count of 30 actions. citeturn22view0turn22view1

### Finding — Production-style terminal harnesses are moving toward explicit phase changes near the deadline

**LemonHarness** feeds elapsed and remaining time to the model on every turn and changes behavior from exploration toward implementation/result preservation and finally targeted validation as the deadline approaches. On Terminal-Bench 2.0 it reports **84.5% accuracy over 445 trials** with one configuration and **86.52% average accuracy** in additional GPT-5.5 validation. There is no clean ablation isolating the contribution of its time controller, so those percentages should be treated as evidence that the complete harness works, not as an 84.5%-caused-by-timing claim. citeturn22view3turn22view4

The paper explicitly identifies open-ended exploration, long-running commands and redundant late verification as mechanisms by which an otherwise solvable terminal task becomes a timeout. Its controller treats wall time as part of agent state instead of leaving it solely to an external kill signal. citeturn22view3

For your controller, the important adaptation is to make this **deterministic outside the LLM**. You do not need the 7B model to be excellent at arithmetic or time management.

Track online estimates:

\[
L_\mathrm{LLM}^{p90},\quad
L_\mathrm{tool}^{p90},\quad
L_\mathrm{verify}^{p90}
\]

and before *every* iteration compute:

\[
R = deadline - now
\]

\[
S = R - L_\mathrm{verify}^{p90} - safety
\]

\[
N_\mathrm{fit}
=
\left\lfloor
\frac{S}
{L_\mathrm{LLM}^{p90}+L_\mathrm{tool}^{p90}}
\right\rfloor
\]

Then tell the model, for example:

```text
TIME_LEFT=74s
ESTIMATED_ROUNDS_LEFT=2
MODE=IMPLEMENT
VERIFICATION_RESERVED=24s
```

This operationalizes Timely Machine's per-round latency idea without retraining your local model. citeturn22view2

### Finding — SWE-agent and OpenHands mostly demonstrate why a hard cap alone is insufficient

The systems you named are actually useful as a **negative result** here.

Current SWE-agent configuration exposes per-instance **cost and LLM-call ceilings**; its generic API-model defaults include a `$3` per-instance cost ceiling, optional per-instance call limit, and deterministic `temperature=0`. These are discrete safety/resource limits, not a continuous “can another complete round fit before deadline?” scheduler. citeturn21view2

OpenHands has likewise used hard maximum-iteration termination; a documented issue shows a CLI session terminating at **iteration 500/500** with `Agent reached maximum iteration`, rather than transitioning gracefully into a completion mode. citeturn21view3

**InterCode** defines interactive code actions and execution-feedback observations and evaluates ReAct/Plan-and-Solve strategies, but its core contribution is the interactive environment rather than a wall-clock scheduling policy. **Cybench** provides 40 realistic cybersecurity tasks and human timing information; its strongest unguided agents solved complete challenges that human teams solved in up to **11 minutes**, while its hardest challenge required humans **24 h 54 min**, but again Cybench is primarily an evaluation framework rather than a deadline-aware controller. citeturn21view5turn22view5

So I would **not copy the resource controller from SWE-agent/Cybench/InterCode** for your timeout problem. The more directly applicable designs are Timely Machine and LemonHarness: monitor actual elapsed time and predicted remaining-round cost continuously. citeturn22view2turn22view3


## Timeout recovery and preserving partial progress

### Finding — Rewind both agent context and environment, not merely the textual reasoning

The very recent **AgentRewind** work is the strongest quantitative recovery result I found. It checkpoints **both agent context and environment state**, and when the current trajectory stops making progress it can revert to an earlier synchronized checkpoint while carrying forward a summary of what failed. citeturn15view2

Across three agent harnesses with the same base model, adding AgentRewind raised task success:

| Harness | Continue | With rewind | Gain |
|---|---:|---:|---:|
| mini-SWE-agent | 62.2% | 87.8% | **+25.6 pp** |
| FnCallAgent | 58.5% | 81.7% | **+23.2 pp** |
| CodeAgent | 67.1% | 82.9% | **+15.8 pp** |

In a paired experiment starting from **50 identical failed endpoints**, ordinary “continue trying” recovered **8%**, whereas rewind recovered **30%**. Removing environment rewind from the full system dropped success from **87.8% to 43.9%**, showing that preserving only text memory is not equivalent to recovering a corrupted working state. citeturn16view0turn16view2

The paper does **not** impose your 120–600 second wall-clock constraint, so this is evidence for the recovery primitive rather than evidence that rewind itself saves deadline time. citeturn16view2

For your microVM, the inexpensive analogue is:

```text
checkpoint after every verified milestone, not every command:

C0: original workspace
C1: diagnosis established
C2: minimal patch/config change passes local sanity check
C3: partial verifier progress improved
C4: final verified solution
```

A checkpoint can often be much cheaper than a full microVM snapshot: preserve original copies of touched files, current diff, verifier score/status, and a compact state record. The crucial property suggested by AgentRewind is **alignment**: the filesystem checkpoint and the textual state describing it must correspond to the same moment. citeturn15view2turn16view2

### Finding — Repair only the invalid suffix instead of regenerating known-good work

**Hydra** applies checkpoint-and-rollback to generated code: checking runs incrementally, valid prefixes are checkpointed, and static errors cause targeted rollback/repair rather than complete post-hoc regeneration. On C/C++ generations that encountered static errors, it reports **71% lower latency and 70% fewer generated tokens** for Qwen2.5-Coder-32B versus post-hoc repair; with another evaluated model the gains were smaller but still substantial. citeturn16view4

Although Hydra operates during code generation rather than a terminal CTF trajectory, the principle maps directly to your `recover` state:

> **Never ask the LLM to rediscover or regenerate a region already proven valid.**

If a forensic hypothesis led to five useful artifacts and the sixth command failed, retain the five. If three of four tests pass after a patch, preserve the working patch and repair the failing suffix. If a configuration file was validated except for one directive, restore only the affected artifact rather than resetting the whole task. Hydra's numbers quantify how expensive full post-hoc regeneration can be relative to prefix-preserving repair. citeturn15view3turn16view4

### Finding — Near the deadline, switch from “maximize probability of full solution” to “preserve best-known state”

AgentRewind's benchmark explicitly measures **partial checklist progress**, and its results show that recovery improves not just binary task success but the amount of completed work. For mini-SWE-agent, average checklist progress moves from **81.4% to 94.3%**; on the paired failed-endpoint recovery experiment, rewind produces a **12.2 percentage-point progress increase** versus **5.1 points** from simply continuing. citeturn16view0turn16view2

That suggests a concrete timeout policy for CTF-style `fix/forensic/audit` tasks:

```text
normal:
    explore / modify / test

deadline approaching:
    freeze destructive exploration
    restore best-known checkpoint if current state is worse
    run cheapest decisive verifier
    persist answer/evidence/patch
    finish

emergency:
    no new LLM call unless it fits in predicted p90
    emit best-known valid result
```

For llama.cpp specifically, the server also supports saving/restoring slot prompt-cache state; the repository documentation includes an example restoring a **1,745-token** prompt-cache state in about **42.9 ms**. That is not a filesystem checkpoint and does not replace one, but it illustrates that the expensive **model-side context state** can also be retained rather than rebuilt from text after a recovery transition. citeturn6search24


## Recommended configuration for your agent

Given your timeout distribution, I would implement the following in this order.

**First, change the loop shape before touching sophisticated inference optimizations.** Replace separate planning and action generations with one short structured `DECIDE` call. Permit that call to issue a batch of independent read-only tools. Verification should be deterministic whenever possible, without another “judge” LLM invocation. LLMCompiler's **up-to-3.7×** latency result and the **38% token reduction** from judge-free early stopping indicate that call count is large enough to dominate architectural micro-optimizations. citeturn4view5turn12view0

A practical loop becomes:

```python
while True:
    update_deadline_stats()

    if current_state_verified():
        return finish()

    if cannot_fit_one_more_round():
        return finish_best_checkpoint()

    mode = choose_mode_from_time()       # explore / implement / verify / emergency

    decision = llm_decide(                # ONE model call
        compact_state,
        time_left,
        estimated_rounds_left,
        output_token_cap,
    )

    results = execute_dependency_batch(decision.actions)
    observations = deterministic_compress(results)
    append_immutable_history(decision, observations)

    update_best_checkpoint()

    if should_early_stop():
        return verify_or_finish()
```

This keeps the architecture single-agent, no RAG, no multiagent framework, and is implementable entirely with Python's stdlib.

**Second, make KV reuse an invariant.** Enable vLLM APC and arrange messages as a monotonically extending prefix. Do not regenerate the system prompt dynamically; do not insert `TIME_LEFT` near the start of the context; append volatile runtime state at the end. vLLM explicitly reuses shared-prefix KV state across multi-round conversations, while coding-agent experiments show that rewriting history can erase much of the apparent benefit of token compression. citeturn20view1turn15view1

In other words:

```text
GOOD FOR CACHE:
[SYSTEM fixed]
[TASK fixed]
[O1 compact fixed]
[A1 fixed]
[O2 compact fixed]
...
[RUNTIME time_left=...]
[next generation]

BAD FOR CACHE:
[SYSTEM including current timestamp/time_left]
[newly regenerated summary of O1..O7]
[O8]
```

**Third, replace the “last eight observations” policy with a byte/token budget plus per-tool compression.** Terminal-agent data shows observations can represent **up to 67.8%** of tokens, while terminal-specific compression already achieves **12–27% lower total tokens** without sacrificing success. Keep raw tool output on disk and make history contain a compact pointer/evidence representation. citeturn15view1turn15view5

I would initially avoid an LLM-based summarization call entirely. For a local 7B where inference is your diagnosed bottleneck, spending another 7B invocation to save tokens from the next 7B invocation is difficult to justify unless the saved prefix is enormous. TACO/SWE-Pruner/CoACT demonstrate that observation selection is valuable, but your fastest first approximation is deterministic compression. citeturn15view1turn15view4turn15view5

**Fourth, make wall-clock latency—not `step < 30`—the primary loop budget.** Keep 30 only as a circuit breaker. Estimate p50/p90 round duration online. Timely Machine specifically finds that tool/generation latency varies by task and therefore token-only budgeting is inadequate. citeturn22view2

For example:

```python
round_p90 = llm_p90 + tool_p90
verify_budget = max(verify_p90 * 1.25, minimum_verify_seconds)
usable = deadline - now - verify_budget

rounds_left = max(0, int(usable // round_p90))
```

Then transition approximately as follows:

| Estimated rounds remaining | Policy |
|---:|---|
| 4+ | normal exploration allowed |
| 2–3 | exploit strongest hypothesis; batch inspections |
| 1 | no broad exploration; implement or verify |
| 0 | restore best checkpoint; cheapest verifier; submit |

The exact thresholds should be measured on your tasks, but the underlying policy follows the empirical finding that time-aware agents need to alter behavior before the kill signal rather than at it. citeturn22view0turn22view3

**Fifth, benchmark speculative decoding only after the above changes.** For a 7B coding target, the most directly relevant llama.cpp evidence suggests a **0.5B same-family coder draft** with roughly **4–8 speculative tokens** as a sensible starting sweep: Qwen2.5-Coder-7B improved **1.75×** in the reported experiment. Do not permanently enable it until your actual CTF action prompts show a positive p90 latency change; the same llama.cpp experiments include model pairs where speculation slows inference. citeturn19view5

**Sixth, benchmark the weight format on the exact hardware.** On vLLM/CUDA, AWQ 4-bit is the obvious candidate because Qwen officially reports **3× speed and 3× memory reduction versus FP16** and supports AWQ directly through vLLM. On llama.cpp, compare the existing quant against at least two nearby GGUF formats with your *real agent prompts* rather than relying on generic tok/s measurements. citeturn20view0turn19view5

Finally, I would change your timeout semantics from:

```text
deadline expires → process killed → task lost
```

to:

```text
deadline - emergency_margin reached
→ stop exploratory actions
→ restore highest-scoring valid checkpoint if necessary
→ deterministic final verification
→ persist patch / forensic evidence / audit answer
→ exit normally
```

That is the most direct response to your observed 13/14 timeout failure mode. AgentRewind shows **+15.8 to +25.6 percentage points in success across three harnesses** from aligned state/context recovery, while Hydra shows that preserving validated prefixes can cut repair latency by as much as **71%**. Your deadline controller should therefore regard a partially verified state as an asset to protect, not merely an intermediate state on the way to an all-or-nothing final answer. citeturn16view0turn16view4

The resulting priority order, based on fit to your particular bottleneck, is therefore: **one LLM decision per interaction → batched read-only actions → immutable compressed observations + prefix caching → live p90 time-to-finish gate → aggressive output-token caps/greedy tool calling → checkpoint-and-submit behavior → 7B+0.5B speculative decoding → quantization/backend tuning**. The first four alter the amount of inference work the agent requests; the latter optimizations merely make that remaining work faster, which is why I would attack them in that order given a workload where almost every unfinished task is currently dying on elapsed time. citeturn4view5turn15view1turn20view1turn22view2turn19view5turn20view0