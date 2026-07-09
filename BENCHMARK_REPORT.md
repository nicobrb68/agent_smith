# LLM Benchmark Report: Agent Smith Performance Analysis

> **Status note (remove before submission):** This report is built
> exclusively from runs that actually happened and are backed by a
> real `solution.json` in this repository. Sections marked
> `TODO` are hard requirements of the subject (>= 5 models on the
> same >= 3 SWE-bench tasks) that still need real runs before this
> report is submission-ready — see the checklist in the Conclusions
> section.

---

## 1. Setup

**Models/providers actually tested so far:**

| Model | Provider | API | Notes |
| :--- | :--- | :--- | :--- |
| `llama-3.3-70b-versatile` | Groq (free tier) | `https://api.groq.com/openai/v1` | Only model tested to date |
| *(TODO)* | *(e.g. Gemini via Google AI Studio, a Mistral model, an OpenRouter-hosted model, a second Groq model)* | | Need >= 4 more models |

**Tasks used:**

- **SWE-bench:** `sympy__sympy-13480` (chosen because it is one of
  the starter tasks suggested by the subject itself, and is a
  single-line, single-file fix — good for isolating agent-loop
  behavior from repository-navigation complexity).
  `TODO`: at least 2 more SWE-bench tasks are required by the
  subject (it suggests `sympy__sympy-14711` and
  `pydata__xarray-4629` as other easy starters), run on the *same*
  model set as the table above.
- **MBPP:** task `163` (`area_polygon`) and task `108`
  (`merge_sorted_list`) — used here for the ablation study (section
  5), since they are what we actually iterated on together while
  debugging the agent.

All `solution.json` files referenced below must be copied into this
repository (e.g. under `benchmarks/mbpp/` and `benchmarks/swebench/`)
before submission, per the subject's requirement that "the backing
solution.json files must be present in your repository."

---

## 2. Results Table (SWE-bench, per subject section V.7.2)

| Task | Model | Success | Iterations | Input Tokens | Output Tokens | Wall-clock Time |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: |
| `sympy__sympy-13480` | Llama-3.3-70b (Groq) | **Pass** | 7 / 30 | 9,974 | 156 | 50.5s |
| `sympy__sympy-14711` | Llama-3.3-70b (Groq) | `TODO` | | | | |
| `pydata__xarray-4629` | Llama-3.3-70b (Groq) | `TODO` | | | | |
| `sympy__sympy-13480` | *(model 2)* | `TODO` | | | | |
| `sympy__sympy-13480` | *(model 3)* | `TODO` | | | | |
| `sympy__sympy-13480` | *(model 4)* | `TODO` | | | | |
| `sympy__sympy-13480` | *(model 5)* | `TODO` | | | | |
| *(...)* | *(...)* | | | | | |

**The one completed run in detail** (`sympy__sympy-13480`,
Llama-3.3-70b): the agent inspected `hyperbolic.py` around the
reported line, located a one-character typo (`cotm` vs `cothm`)
purely from reading the code — the `hints_text` field was not yet
wired into the prompt at the time of this run (see section 3 of our
fix log / section 5 ablation candidate below), so this was 100%
independent exploration. It called `search_code` once with an
(incorrectly) escaped regex pattern that matched nothing (our
`search_code` tool does plain substring matching, not regex — since
fixed in the tool's docstring), recovered by using `read_file`
directly, applied a single `edit_file`, ran the test suite once, and
submitted via `final_answer(get_patch())`.

---

## 3. Provider Reliability

| Model / Provider | Avg. response time / request | Retries observed | Availability |
| :--- | :---: | :---: | :--- |
| Llama-3.3-70b (Groq, free tier) | 5.22s (SWE-bench run, n=7 requests, min 309ms / max 16.7s) | 0 across every run we logged (MBPP and SWE-bench) | No rate-limit or provider errors observed in any of our ~10 logged runs |
| *(TODO: other providers)* | | | |

Latency on Groq is highly bimodal: most calls return in under 1
second, but a handful of calls in every run we captured spike to
9-17 seconds with no visible cause in the response (no `retries`
recorded) — worth investigating further (possibly Groq-side queueing
under load) before drawing conclusions about "speed" as a selection
criterion.

---

## 4. Intermediary Metrics (SWE-bench, `sympy__sympy-13480`)

- **Step at which the agent first reads/edits the file that appears
  in the final patch (exploration efficiency):** first `read_file`
  on `hyperbolic.py` at **step 2**, first (and only) `edit_file` at
  **step 4**. Two steps of pure investigation before any write —
  consistent with the system prompt's "NO GUESSING" rule.
- **Iterations between "tests first pass" and `final_answer`:** could
  **not** be reliably measured on this run. The `run_tests()` output
  at step 5 was long enough to be truncated by our token-saving log
  filter, and the truncation logic at the time cut out the actual
  pass/fail summary line, leaving the agent (and us) unable to see
  whether the suite had actually passed before it called
  `get_patch()`/`final_answer()`. This is a real bug we found and
  fixed mid-project (see Ablation 4 below) — this run predates the
  fix, which is itself a useful data point about why the fix
  mattered.
- **Step at which test failures first decrease vs baseline:** not
  applicable here — the fix was a single-character typo, so there
  was only ever one `run_tests()` call and no incremental
  pass/fail signal to track.

---

## 5. Ablation Study

All four ablations below are real before/after comparisons captured
during development, each backed by an actual `solution.json` (or, for
the two sandbox/tooling fixes, by a targeted reproduction test since
they are infrastructure bugs rather than prompt/parameter changes we
could A/B on a live task twice under identical conditions).

### Ablation 1 — System prompt: code-only vs. Thought→Code→Observation

*Task: MBPP 163 (`area_polygon`), model: Llama-3.3-70b (Groq).*

| Variant | Success | Iterations | Output Tokens | Time |
| :--- | :---: | :---: | :---: | :---: |
| Code-only prompt, `temperature=0.0` | Fail | 10 / 10 | 803 | 23.2s |
| Code-only prompt, `temperature=0.7` | Fail | 10 / 10 | 731 | 58.5s |
| **Thought+Observation prompt, `temperature=0.7`** | **Pass** | **5 / 10** | **308** | **3.0s** |

The task's parameter names (`s`, `l`) are ambiguous relative to the
formula. Forbidding the model any reasoning text ("no explanations,
just code") left it guessing blindly across 10 identical-budget
attempts in both baseline variants. Adding one short mandatory
`Thought:` line before each code block — explicitly instructing the
model to use the exact expected values shown in a failed
`Observation` to reconsider what each parameter means — let it
correctly re-derive the parameter mapping by attempt 5, using less
than half the output tokens of either failing baseline.

### Ablation 2 — Import allowlist + anti-repetition temperature escalation

*Task: MBPP 108 (`merge_sorted_list`), model: Llama-3.3-70b (Groq),
all three runs already use the Thought+Observation prompt from
Ablation 1.*

| Variant | Success | Iterations | Input Tokens | Time |
| :--- | :---: | :---: | :---: | :---: |
| `heapq` blocked by an over-restrictive `sandbox_template.json` | Pass* | 4 / 10 | 1,617 | 12.6s |
| `heapq` allowed, fixed `temperature=0.7` (no anti-repetition) | **Fail** | 6 / 10 | 6,083 | 4.3s |
| `heapq` allowed, temperature escalates on repeat (0.7→0.9→1.1→1.3) | **Pass** | 5 / 10 | 3,505 | 13.7s |

\* Row 1 only "passes" because `heapq` being unavailable forced the
model away from `heapq.merge` and onto `sorted(num1+num2+num3)`,
which happens to satisfy this task's tests — a lucky accident of a
misconfigured sandbox, not a real fix. See row 2.

Row 2 is the interesting failure: with `heapq` correctly available,
the model latched onto `heapq.merge(...)` (primed by the task's own
wording, "using heap queue algorithm") and regenerated functionally
identical code 6 times in a row at a fixed temperature, burning
6,083 input tokens until it hit the hard 6,000-token limit and
failed outright. (`heapq.merge` silently produces wrong output here
because two of the three input lists in this task's second test case
are not individually pre-sorted, which violates `heapq.merge`'s
precondition — a genuine trap in the MBPP task itself.)

Row 3 adds: (a) whitespace-insensitive exact-repeat detection across
consecutive failed attempts, and (b) a temperature bump
(`+0.2` per consecutive repeat, capped at `1.3`) applied only to the
next call when a repeat is detected. This was enough to push the
model off `heapq.merge` and onto the robust `sorted(...)` fallback
by attempt 5, using 43% fewer input tokens than the failing fixed-
temperature run.

### Ablation 3 — Sandboxed test-harness control flow (`exit()` vs. a flag)

*Not a live A/B on identical conditions (the bug prevents the "before"
state from producing a comparable log at all) — verified via a direct
reproduction instead.*

The original MBPP test harness called `exit(1)` inside the sandboxed
child process on every failing test. Because the sandbox's
multiprocessing worker only calls `queue.put(...)` on normal
completion or on our own `FinalAnswerException`, and deliberately
re-raises `SystemExit`/`KeyboardInterrupt` uncaught (as the subject
requires), every failing test caused the child process to terminate
*before* its captured stdout could be reported back — the parent
then fell through to a generic `"Fatal Sandbox Error: Child process
terminated without returning metrics."`, discarding the actual
assertion message. Reproduced directly:

```
exec() with the old exit(1)-based harness on a failing test:
  -> SystemExit propagates out of the child process
  -> queue.put() is never reached
  -> parent reports a generic Fatal Sandbox Error

exec() with the flag-based harness (__all_tests_passed = False):
  -> script runs to completion
  -> "Assertion failed: assert add(2, 3) == 6" is correctly
     captured and returned
```

Every MBPP `solution.json` in this repository produced after the fix
(all of Ablations 1 and 2 above) shows real, specific
`Assertion failed: ...` / `Runtime Error during test: ...` messages
in `sandbox_output` instead of the generic fatal error — indirect but
consistent evidence the fix holds across many independent runs.

### Ablation 4 — `run_tests()` truncation dropping the pass/fail verdict

*Motivated by the real SWE-bench run in section 2/4 above, where the
truncated log gave no visible verdict; verified via a targeted
reproduction rather than a second live SWE-bench run (not yet
re-run post-fix, see Conclusions checklist).*

| Variant | 363-line synthetic log (60 noise / 3 verdict lines / 300 noise) |
| :--- | :--- |
| Before: header (20) + footer (100) only | 0 / 3 verdict lines visible to the agent |
| After: header + footer + rescued verdict lines | **3 / 3 verdict lines visible**, with original line numbers preserved |

`run_tests()`'s log truncation kept a fixed 20-line header and
100-line footer to control token usage, with no guarantee the actual
pass/fail summary (which, depending on the repo's test runner,
prints anywhere in the log) survives. We added a regex pass
(`passed`/`failed`/`error`/`ok`/`AssertionError`/`Traceback`/the
`>>>>> Start/End Test Output` harness markers) that rescues any
matching line dropped by truncation and re-inserts it under a
`PRESERVED TEST RESULT LINES` header, regardless of where in the log
it originally fell.

---

## 6. Conclusions

**Based on the data actually collected so far:**

- Llama-3.3-70b-versatile via Groq (free tier) is workable for both
  benchmarks, but is sensitive to prompt design in a way that shows
  up clearly in the numbers above: the same model went from 0/2
  MBPP tasks solved to 2/2 solved purely from (a) allowing one short
  reasoning line before code, and (b) escalating temperature on
  detected repetition — no model swap needed.
- On the single completed SWE-bench task, Llama-3.3-70b followed the
  "read before edit" protocol correctly and produced a minimal,
  correct patch without needing the issue's hint text (which wasn't
  even wired in yet at the time of this run).
- Latency on Groq's free tier is unpredictable (309ms-16.7s per
  call in the same 7-request run) — anything time-sensitive in the
  final pipeline should not assume consistent per-call latency.

**What is still required before this report (and the project) is
submission-ready — checklist:**

1. [ ] Add >= 4 more models/providers (subject requires >= 5 total)
       — e.g. Gemini via Google AI Studio, a Mistral model, and 1-2
       OpenRouter-hosted models, all with multi-key rotation
       already supported by `TokenRotator`.
2. [ ] Run all >= 5 models on the *same* >= 3 SWE-bench tasks
       (currently only `sympy__sympy-13480` is done; add
       `sympy__sympy-14711` and `pydata__xarray-4629` at minimum).
3. [ ] Re-run `sympy__sympy-13480` (or a similarly truncation-prone
       task) after the Ablation 4 fix to get a live "after" data
       point for the pass/fail-verdict-visibility metric in section 4.
4. [ ] Copy every referenced `solution.json` into this repository.
5. [ ] Fill in the Provider Reliability table for each new
       model/provider once run.

Until items 1-2 are done, this report does not yet satisfy the
subject's "compare at least 5 models on the same set of at least 3
SWE-bench tasks" requirement — everything above it is real, but it
is currently a single-model case study, not the full comparison.