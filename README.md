*This project has been created as part of the 42 curriculum by <your_login> (please replace with your actual 42 login(s), comma-separated if this was a group project).*

# Agent Smith

## Description

Agent Smith is an autonomous **Code Agent** framework: an LLM-driven
loop that reasons about a coding task, writes executable Python code,
runs it inside a secured sandbox, observes the result, and iterates
until the task is solved.

Instead of classic JSON-based tool calling, the agent writes plain
Python code that calls tools directly as functions (e.g.
`result = search_code("validate_email")`). This "code agent" approach
supports persistent variables, loops, and conditional logic across
steps, which a single JSON tool call cannot.

The project applies this framework to two benchmarks:

- **MBPP** (`student/agent_mbpp.py`) -- self-contained algorithmic
  Python problems, validated through the `run_tests` MCP tool.
- **SWE-bench** (`student/agent_swebench.py`) -- real bug-fixing
  tasks in real repositories, solved inside disposable Docker
  containers through a dedicated MCP tool server
  (`mcp_tools_swebench.py`).

## Architecture

- **Agent/Orchestrator** (`AgentMbpp`, `AgentSWEBench`) drives the
  Thought -> Code -> Observation loop: calls the LLM, extracts code,
  sends it to the sandbox, reads the observation, repeats.
- **Code extraction** (`student/code_extraction.py`) turns whatever
  format the LLM produced (fenced Python, Anthropic-style
  `<invoke>`, Hermes-style `<tool_call>`, or ReAct `Action:`) into an
  equivalent Python call, or explains clearly why nothing was found.
  Both agents' `_extract_code`/inline extraction now delegate to it.
- **Sandbox** (`student/sandbox.py`) is the only place LLM-generated
  code actually executes. It enforces, on every run: an import
  allowlist, a filesystem allowlist (`allowed_directories`), a
  restricted builtins set (no `eval`/`exec`/`compile`/...), no
  network access, a wall-clock timeout (with partial output recovery
  on kill), and a memory limit (`RLIMIT_AS`).
- **MCPBridge** (`student/mcp_bridge.py`) owns one real
  `mcp.ClientSession` (stdio or streamable HTTP) on a dedicated
  background thread with its own event loop, and exposes every
  discovered tool as a plain synchronous Python function the sandbox
  can inject. This is necessary, not just stylistic: an
  `asyncio`/MCP session is bound to the event loop that created it,
  and `Sandbox.execute_code`'s tool-dispatch loop is synchronous, so
  a session created on the "main" loop cannot safely be awaited from
  inside it without deadlocking. Both `AgentMbpp` and `AgentSWEBench`
  use `MCPBridge` for this reason (`AgentSWEBench.solve()` used to
  open a `ClientSession` directly and then bypass it entirely via a
  plain Python import of `mcp_tools_swebench` -- a real bug fixed as
  part of this pass, see below).
- **MCP servers** (`mcp_tools_mbpp.py`, `mcp_tools_swebench.py`) are
  separate processes exposing the mandatory tools. `final_answer()`
  is **not** one of them -- it's a sandbox-native construct that ends
  the agent loop.
- **Sandbox CLI** (`student/sandbox_cli.py`, `uv run sandbox`) is a
  REPL for interactively exercising the sandbox, optionally
  connected to an MCP server via `--mcp-stdio`/`--mcp-server`.

## Sandbox design

`SandboxConfig` (Pydantic, loaded from `sandbox_template.json`)
controls, per run:

| Field | Purpose |
|---|---|
| `authorized_imports` | Glob allowlist for `import` statements |
| `allowed_directories` | Paths `open()` is allowed to touch |
| `max_execution_time_seconds` | Wall-clock timeout |
| `max_memory_mb` | RAM limit (`resource.RLIMIT_AS`) |

Every run happens in a forked child process. `_restricted_builtins()`
builds a *copy* of the real builtins for the executed code only (the
sandbox's own `exec(code_str, ...)` call keeps using the real,
unrestricted ones): dangerous entries (`eval`, `exec`, `compile`, ...)
are removed, and `open` is wrapped to enforce `allowed_directories`.

On timeout the child is killed, but stdout/stderr captured so far is
recovered from a shared `multiprocessing.Array` (deliberately **not**
a `multiprocessing.Manager`, whose proxy would itself get blocked by
the sandbox's own network restriction on reconnect after fork -- see
the comment above `_PARTIAL_OUTPUT_BUFFER_SIZE` in `sandbox.py`) so
the LLM sees a genuinely partial Observation instead of nothing.
`KeyboardInterrupt`/`SystemExit` raised by sandboxed code are **not**
swallowed into a fake Observation -- they propagate so the run can
shut down cleanly instead of silently continuing.

## Agent loop

Both agents follow the same shape:

1. Send the system prompt and the task to the LLM.
2. Extract code from the response (`_extract_code`, delegating to
   `student/code_extraction.py`).
3. Execute it in the `Sandbox`, with the MCP tool proxies
   (`bridge.build_tool_proxies()`) injected.
4. Feed the Observation back and repeat, until `final_answer()` is
   called or a hard limit (iterations/tokens/time) is hit.
5. Persist a `SolutionOutput`-shaped `solution.json` with full
   per-step metrics (tokens, timing, raw LLM output, sandbox
   input/output) for traceability.

MBPP additionally bumps the sampling temperature after repeated
identical failing attempts, to push the model out of a local minimum
instead of regenerating the same wrong code forever (see
`REPEAT_TEMPERATURE_STEP`/`REPEAT_TEMPERATURE_MAX`).

**MBPP test_imports:** the task's `test_imports` field (e.g.
`["import math"]` for a test using `math.isclose(...)`) is read in
`AgentMbpp.__init__` and prepended to the generated test harness in
`_build_test_code`, since the test assertions run in the *same*
namespace as the candidate code. The agent's user prompt also warns
the model explicitly when `test_imports` is empty, since a candidate
that only imports what its own logic needs (e.g. `cmath`) can
otherwise fail the tests with a `NameError` on an unrelated name the
tests reference (e.g. `math`) -- this was previously a silent trap
with no clear signal for the model to self-correct from.

## Tool implementation details

- **File System**: `read_file`, `edit_file` (rejects empty/whitespace
  `old_str`; warns if a `.py` edit breaks parsing), `list_files`.
- **Code Search**: `search_code` (literal substring grep),
  `search_function_or_class_definition_in_code` (AST-based, exact
  name match), `find_references` (uses `jedi` inside the container
  when available for scope-aware resolution, falls back to a
  whole-word text search otherwise). The latter two were missing
  from the original tool set despite being mandatory (subject
  section V.5.2) and have been added to `mcp_tools_swebench.py`.
- **Execution**: `run_tests` (SWE-bench: runs the task's eval script
  inside Docker; MBPP: runs a candidate + its generated assertion
  harness inside the local sandbox -- `mcp_tools_mbpp.py` also still
  exposes the original generic `execute_python_code` tool),
  `get_patch` (`git -c core.fileMode=false diff`), `run_command`.

## Instructions

```bash
# Install dependencies
uv sync

# Interactive sandbox REPL
uv run sandbox
uv run sandbox sandbox_template.json
uv run sandbox --mcp-stdio "python mcp_tools_mbpp.py" sandbox_template.json
uv run sandbox --mcp-server http://localhost:8000

# MBPP: dump a task, run the agent, validate
cd moulinette && uv run moulinette_eval dump mbpp --output ../cache/mbpp_task.json && cd ..
uv run python -m student.agent_mbpp --task-file cache/mbpp_task.json \
    --output cache/mbpp_solution.json \
    --model-name "<model>" --provider-url "<provider_base_url>"
cd moulinette && uv run moulinette_eval validate mbpp ../cache/mbpp_task.json ../cache/mbpp_solution.json && cd ..

# SWE-bench: same idea (requires Docker)
cd moulinette && uv run moulinette_eval dump swebench --output ../cache/swebench_task.json && cd ..
uv run python -m student.agent_swebench --task-file cache/swebench_task.json \
    --output cache/swebench_solution.json \
    --model-name "<model>" --provider-url "<provider_base_url>"

# Lint / type-check
make lint
```

API keys are read from environment variables / a `.env` file, e.g.:

```
GROQ_API_URL=https://api.groq.com/openai/v1
GROQ_MODEL_NAME=llama-3.3-70b-versatile
GROQ_KEYS=key1,key2
OPENROUTER_API_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL_NAME=<model>
OPENROUTER_KEYS=key1,key2
```

`TokenRotator` discovers every `<PROVIDER>_API_URL` /
`<PROVIDER>_MODEL_NAME` / `<PROVIDER>_KEYS` triple present in the
environment and rotates across all of them (multiple keys per
provider, and multiple providers) on rate limits or transient errors.

## Benchmark results and analysis

See [`BENCHMARK_REPORT.md`](./BENCHMARK_REPORT.md) at the root of the
repository for the model comparison, provider reliability notes,
intermediary metrics, and the ablation studies (subject section V.7).
It is explicit about what is a real, logged run versus what is still
a `TODO` before the required 5-model x 3-task matrix is complete.

## Resources

- [Model Context Protocol specification](https://modelcontextprotocol.io/)
- [SWE-bench](https://www.swebench.com/) and
  [SWE-bench Verified](https://openai.com/index/introducing-swe-bench-verified/)
- [MBPP dataset](https://github.com/google-research/google-research/tree/master/mbpp)
- [smolagents blog post on code agents](https://huggingface.co/blog/smolagents) --
  background reading on the "code as the action space" idea this
  project implements from scratch (no smolagents/langgraph/etc. code
  was used, per the subject's constraints).
- [Python `resource` module docs](https://docs.python.org/3/library/resource.html)
  (`RLIMIT_AS`, used for the sandbox's memory limit)

### AI usage disclosure

An AI assistant (Claude) was used during this project for:

- **Code review against the subject**: auditing the codebase section
  by section against the project requirements and flagging missing
  or broken mandatory pieces -- the two missing search tools, the
  missing `uv run sandbox` CLI, the unenforced filesystem/builtins
  restrictions in the sandbox, `AgentMbpp` never actually calling
  MCP, `AgentSWEBench` opening an MCP session and then bypassing it
  via a direct Python import, an escaped-brace bug that silently
  hid real exception messages in the MBPP test harness, and
  `test_imports` from the MBPP task never being read or used.
- **Implementing the fixes above** while keeping the existing
  functions and structure wherever the underlying bug didn't require
  changing them: e.g. `_extract_code` and `_build_test_code` in
  `AgentMbpp` keep their original names/call sites and got smaller,
  targeted edits rather than a rewrite; `mcp_tools_mbpp.py`'s
  original `execute_python_code` tool was kept as-is, with `run_tests`
  added alongside it.
- Every change was manually reviewed, and the sandbox changes in
  particular were smoke-tested in isolation (import restriction,
  filesystem restriction, `eval`/`exec` removal, timeout with partial
  output, tool output truncation, positional and keyword tool calls)
  before being kept, since `sandbox.py` is the project's actual
  security boundary.

As emphasized in the subject's AI Instructions: this assistance
sped up implementation, but the underlying design decisions (why
`MCPBridge` needs its own thread/event loop, why partial output is
recovered through shared memory rather than a `multiprocessing.
Manager`, why MBPP and SWE-bench use different default temperatures)
needed to be understood and are explained in code comments and in
this README, not just pasted in.
