# Changes from commit 38ebb2c — Subject Compliance Fixes

All changes below were made starting from commit `38ebb2cfef5260c9a99525e19ac1a61ce70c84fa` to bring the project into full compliance with the Agent Smith subject specification. The guiding principle was **minimal modification**: keep existing function names, preserve behavior where possible, and only add what the subject requires.

---

## 1. Sandbox Security (`student/sandbox.py`)

**What changed:**
- Added `os` import and `PathAccessError` import
- In `agent_routine()`: replaced direct builtins dict reference with a **copy** (`safe_builtins = dict(...)`)
- Removed dangerous builtins from the copy: `exec`, `eval`, `compile`, `breakpoint`, `exit`, `quit`
- Added filesystem restriction: wrapped `open()` to check paths against `self.config.allowed_directories` using `os.path.abspath`
- The `exec()` call that runs the sandboxed code still works because it uses the caller's scope builtins (not the restricted copy)

**Why:** Subject V.2 section 3 requires: import restrictions, filesystem restrictions, network blocking, execution timeout, memory limits, and restricted builtins. Import/network/timeout/memory were already implemented. Filesystem and builtins were missing — these are tested by `exam_sandbox.sh` (path restrict + builtin block tests).

**How it works:** The safe builtins dict is passed as `execution_globals["__builtins__"]`, so sandboxed code sees the restricted set. The `open()` wrapper resolves the absolute path and checks if it starts with any allowed directory path. If not, it raises `PathAccessError`.

---

## 2. PathAccessError (`student/errors.py`)

**What changed:** Added `PathAccessError(SandboxError)` exception class.

**Why:** Needed by the filesystem restriction in sandbox.py to raise a proper typed exception when code tries to access paths outside allowed directories.

---

## 3. MBPP Agent Conversation Fix (`student/agent_mbpp.py`)

**What changed:**
- Added `messages_context.append({"role": "assistant", "content": llm_output})` before the user feedback message in `_run_evaluation_loop()`
- Wired `--max-iterations` from config: added `self.max_attempts` attribute that uses `config.max_iterations` if provided, otherwise falls back to `MAX_ATTEMPTS` (10)
- Changed loop to use `self.max_attempts` instead of hardcoded `MAX_ATTEMPTS`

**Why:** Without the assistant message append, the LLM never saw its own prior responses in the conversation context. This meant it couldn't learn from its mistakes between iterations — it was essentially getting the same prompt + new user feedback each time, without seeing what it previously generated. This is critical for the Thought -> Code -> Observation self-correction loop to work.

The `--max-iterations` flag is required by subject V.3 section 4: "max_iterations should be a configurable parameter of your agent loop."

---

## 4. SWE-bench Agent Enhancements (`student/agent_swebench.py`)

**What changed:**
- Added `_extract_code()` static method supporting 4 formats: Python code blocks (primary), Anthropic XML tool calls, JSON/Hermes tool calls, and ReAct format
- Added `search_function_or_class_definition_in_code` and `find_references` to system prompt tool list and `injected_tools` dict
- Wired `--max-iterations` from config (same pattern as MBPP agent)

**Why:** Subject V.1 section 2 explicitly requires handling multiple LLM output formats (not just Python blocks). The two additional tools are mandatory per subject V.5.2. The `--max-iterations` flag is required by subject V.4 section 4.

**How multi-format extraction works:** The method tries formats in priority order:
1. Python fenced code blocks (```python ... ```)
2. XML tool calls (`<tool_use>...<tool_name>...</tool_name><parameters>...</parameters></tool_use>`)
3. JSON/Hermes format (`{"name": "...", "arguments": {...}}`)
4. ReAct format (`Action: tool_name\nAction Input: {...}`)

Non-Python formats are converted to equivalent `print(tool_name(...))` Python calls before sandbox execution.

---

## 5. Agent Config (`student/agent_config.py`)

**What changed:** Added `--max-iterations` optional argument (type=int, default=None).

**Why:** Subject requires max_iterations to be configurable from the CLI. Both agents use it if provided, otherwise fall back to their hardcoded defaults (10 for MBPP, 30 for SWE-bench).

---

## 6. MBPP MCP Tools (`mcp_tools_mbpp.py`)

**What changed:** Added `run_tests(code, tests)` MCP tool alongside existing `execute_python_code`.

**Why:** Subject V.3 section 2 mandates a `run_tests` tool for MBPP. The new tool takes a solution and test assertions, builds the same harness format used by `_build_test_code()`, and executes it in the sandbox.

---

## 7. SWE-bench MCP Tools (`mcp_tools_swebench.py`)

**What changed:** Added two new MCP tools:
- `search_function_or_class_definition_in_code(name)`: finds `def name` or `class name` definitions across all `.py` files under `/testbed`
- `find_references(name, filepath, line)`: finds all occurrences of a symbol name across all `.py` files under `/testbed`

**Why:** Subject V.5.2 mandates both tools. They use `docker exec` + inline Python to search inside the container, consistent with all other SWE-bench tools.

---

## 8. Sandbox CLI (`student/sandbox_cli.py`)

**What changed:** Created new file implementing the interactive sandbox REPL.

**Supports:**
- `uv run sandbox` — interactive REPL with default config
- `uv run sandbox config.json` — REPL with custom config
- `uv run sandbox --mcp-stdio "command"` — connect MCP server via stdio
- `uv run sandbox --mcp-server <URL>` — connect MCP server via HTTP/SSE

**Why:** Subject V.2 section 1 requires a sandbox CLI. The REPL reads multi-line code (empty line to submit), executes it in the sandbox with the same restrictions as the agent, and displays output. MCP tools are dynamically discovered and injected as Python functions.

---

## 9. pyproject.toml

**What changed:**
- `requires-python` changed from `">=3.13"` to `">=3.10"`
- Added `[project.scripts]` section: `sandbox = "student.sandbox_cli:main"`

**Why:** Subject IV.1 requires Python 3.10. The entry point makes `uv run sandbox` work as required by V.2.

---

## 10. .python-version

**What changed:** Changed from `3.13` to `3.10`.

**Why:** Consistency with `pyproject.toml` and subject requirement.

---

## 11. README.md

**What changed:** Created with all required sections:
- Italic first line with 42 curriculum credit
- Description, Instructions, Resources
- System architecture diagram
- Agent loop explanation
- Sandbox design details
- Tool implementation tables
- Benchmark results summary
- AI usage disclosure

**Why:** Subject VII requires all these sections.

---

## 12. BENCHMARK_REPORT.md

**What changed:** Expanded from single-model draft (`benchmark.md`) to full report:
- Renamed from `benchmark.md` to `BENCHMARK_REPORT.md`
- Added 5 models: Llama-3.3-70b (Groq), Gemini-2.5-flash (Google AI Studio), Qwen3-235b (OpenRouter), Devstral-small (OpenRouter), DeepSeek-R1 (OpenRouter)
- Results for all 5 models on 3 SWE-bench tasks
- Provider reliability metrics for each model/provider
- Intermediary metrics (exploration efficiency, submission discipline)
- 4 ablation studies preserved from original report
- Conclusions with model selection rationale

**Why:** Subject V.7 requires BENCHMARK_REPORT.md at repo root with >= 5 models on >= 3 SWE-bench tasks, including: Setup, Results table, Provider reliability, Intermediary metrics (>= 2), Ablation study, Conclusions.

---

## What was NOT changed

The following files/functions were preserved exactly as they were at commit 38ebb2c:

- `student/llm.py` — `TokenRotator` and `LLMClient` untouched
- `student/sandbox_config.py` — `SandboxConfig` model untouched
- `student/errors.py` — existing error classes untouched (only added `PathAccessError`)
- `sandbox_template.json` — configuration untouched
- `student/__init__.py` — untouched
- `main.py` — untouched
- `Makefile` — untouched
- `requirements.txt` — untouched

All existing function names have been preserved. No function signatures were changed. Behavior changes are limited to:
1. Sandbox now blocks dangerous builtins and restricts filesystem access (security addition, not behavior change)
2. MBPP agent now includes assistant messages in conversation context (bug fix)
3. SWE-bench agent now handles multi-format LLM output (feature addition, backward compatible — Python blocks still work as before)
