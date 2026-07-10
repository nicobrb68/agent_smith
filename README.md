*This project has been created as part of the 42 curriculum by nolhan.*

# Agent Smith

An autonomous agentic framework capable of solving coding challenges through reasoning, code generation, and sandboxed execution. The agent operates through a structured **Thought -> Code -> Observation** loop, interacting with tools via the Model Context Protocol (MCP).

## Description

Agent Smith is a code agent system that can:

- Reason about programming tasks using LLM inference
- Generate executable Python code as tool calls
- Execute that code inside a secure, configurable sandbox
- Observe results and iterate until a solution is found

It targets two benchmarks:

- **MBPP** (Mostly Basic Python Problems): algorithmic Python exercises
- **SWE-bench**: real-world bug fixing in production GitHub repositories inside Docker containers

## Instructions

### Prerequisites

- Python >= 3.10
- [uv](https://docs.astral.sh/uv/) package manager
- Docker (for SWE-bench tasks)
- API keys for at least one LLM provider (Groq, OpenRouter, Gemini, OpenAI)

### Installation

```bash
uv sync
```

### Configuration

Create a `.env` file with your provider keys:

```bash
GROQ_API_URL=https://api.groq.com/openai/v1
GROQ_MODEL_NAME=llama-3.3-70b-versatile
GROQ_KEYS=gsk_key1,gsk_key2

OPENROUTER_API_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL_NAME=qwen/qwen3-235b-a22b-2507
OPENROUTER_KEYS=sk-or-key1,sk-or-key2
```

### Running the MBPP Agent

```bash
uv run python -m agent_mbpp \
  --task-file task.json \
  --output solution.json \
  --model-name "llama-3.3-70b-versatile" \
  --provider-url "https://api.groq.com/openai/v1"
```

### Running the SWE-bench Agent

```bash
uv run python -m agent_swebench \
  --task-file swebench_task.json \
  --output solution.json \
  --model-name "llama-3.3-70b-versatile" \
  --provider-url "https://api.groq.com/openai/v1"
```

### Interactive Sandbox CLI

```bash
# Basic interactive mode
uv run sandbox

# With custom sandbox config
uv run sandbox sandbox_template.json

# With MBPP MCP tools via stdio
uv run sandbox --mcp-stdio "python mcp_tools_mbpp.py" sandbox_template.json

# With SWE-bench MCP tools
uv run sandbox --mcp-stdio "python mcp_tools_swebench.py"

# With HTTP MCP server
uv run sandbox --mcp-server http://localhost:8000
```

## System Architecture

```
+------------------+
|   Agent Code     |
|  (Orchestrator)  |
+--------+---------+
         |
    LLM API call (OpenAI-compatible)
         |
+--------v---------+     Response containing code block
|    LLM Provider   |-----------------------------+
+------------------+                              |
                                                  v
                                    +-------------+----------+
                                    |    Code Extraction     |
                                    | (Python / XML / JSON / |
                                    |  Hermes / ReAct)       |
                                    +-------------+----------+
                                                  |
                                         Extracted code
                                                  |
                                    +-------------v----------+
                                    |       Sandbox          |
                                    | (multiprocessing.Process|
                                    |  isolation)            |
                                    |                        |
                                    |  Python    Tool Call   |
                                    |  Interpreter -------> MCP Client
                                    +------------------------+     |
                                                              STDIO / HTTP
                                                                   |
                                                          +--------v-------+
                                                          |   MCP Server   |
                                                          | (mcp_tools_*   |
                                                          |  .py files)    |
                                                          +----------------+
```

### Key Components

1. **Agent/Orchestrator** (`agent_mbpp.py`, `agent_swebench.py`): Central loop that calls the LLM, extracts code, feeds it to the sandbox, reads observations, and repeats.

2. **Code Extraction**: Transforms LLM responses into executable Python. Supports multiple formats: markdown code blocks (primary), Anthropic XML tool calls, JSON/Hermes tool calls, and ReAct format.

3. **Sandbox** (`student/sandbox.py`): Execution boundary that enforces security restrictions on LLM-generated code. Uses `multiprocessing.Process` for isolation.

4. **`final_answer()`**: A built-in sandbox construct (NOT an MCP tool) that signals task completion by raising `FinalAnswerException`.

5. **MCP Servers** (`mcp_tools_mbpp.py`, `mcp_tools_swebench.py`): Run as separate processes (via stdio or HTTP), exposing tools as callable Python functions within the sandbox namespace.

6. **LLM Client** (`student/llm.py`): OpenAI-compatible client with multi-provider, multi-key rotation via `TokenRotator`.

## Agent Loop

The agent follows a **Thought -> Code -> Observation** loop:

1. **Thought**: The LLM reasons about the task or previous observations
2. **Code**: The LLM generates Python code containing tool calls
3. **Observation**: The sandbox executes the code and returns stdout/stderr
4. The observation is fed back to the LLM as a new user message
5. The loop repeats until `final_answer()` is called or limits are exceeded

For MBPP, the agent generates a solution function and tests it against assertion-based test cases. An anti-repetition mechanism detects when the model regenerates identical failing code and bumps the sampling temperature to force exploration.

For SWE-bench, the agent explores a Docker-containerized repository using file system and code search tools, edits files, runs the evaluation test suite, and submits a git patch via `final_answer(get_patch())`.

## Sandbox Design

The sandbox provides a secure execution environment using `multiprocessing.Process` isolation:

- **Import restrictions**: Only modules from a configurable allowlist (`authorized_imports`) can be imported. Uses a custom `__import__` hook with `fnmatch` pattern matching.
- **Filesystem restrictions**: File access is limited to `allowed_directories` (default: `/testbed`, `/tmp/agent`). A wrapped `open()` checks `os.path.abspath` against the allowlist.
- **Network blocking**: All socket creation is intercepted and raises `ForbiddenNetworkError`.
- **Dangerous builtins removal**: `exec`, `eval`, `compile`, `breakpoint`, `exit`, `quit` are removed from the execution namespace to prevent privilege escalation.
- **Execution timeout**: Configurable per-execution timeout with `process.terminate()`.
- **Memory limits**: `resource.setrlimit(RLIMIT_AS)` caps RAM usage.
- **Process isolation**: Code runs in a child process; tool calls are proxied through `multiprocessing.Queue` pairs (request/response) so the parent executes MCP calls outside the restricted environment.

Configuration is managed via Pydantic models (`SandboxConfig`) and JSON files (`sandbox_template.json`).

## Tool Implementation Details

### MBPP Tools (`mcp_tools_mbpp.py`)

| Tool | Description |
|------|-------------|
| `execute_python_code(code)` | Execute Python code in the sandbox |
| `run_tests(code, tests)` | Run solution code against test assertions |

### SWE-bench Tools (`mcp_tools_swebench.py`)

| Tool | Description |
|------|-------------|
| `read_file(filepath, start_line, end_line)` | Read file lines with line numbers |
| `edit_file(filepath, old_str, new_str)` | Replace exact string in a file |
| `list_files(directory, pattern)` | List files matching a glob pattern |
| `search_code(pattern, file_pattern)` | Grep-like plain substring search |
| `search_function_or_class_definition_in_code(name)` | Find function/class definitions |
| `find_references(name, filepath, line)` | Find all references to a symbol |
| `run_tests()` | Execute the evaluation script |
| `get_patch()` | Retrieve the unified git diff |
| `run_command(command, workdir)` | Execute a shell command |

All SWE-bench tools operate inside Docker containers via `docker exec`. The `run_tests()` tool includes smart log truncation that preserves test verdict lines regardless of where they fall in long output.

## Benchmark Results and Analysis

See [BENCHMARK_REPORT.md](BENCHMARK_REPORT.md) for the full model comparison report covering 5+ models across 3+ SWE-bench tasks with detailed metrics, provider reliability data, intermediary analysis, and ablation studies.

Key findings:
- The Thought -> Code -> Observation prompt structure significantly outperforms code-only prompts
- Anti-repetition temperature escalation prevents the agent from getting stuck in local minima
- Log truncation must preserve test verdicts to avoid wasting iterations on invisible results

## Resources

- [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) - Protocol for exposing tools as callable functions
- [MBPP Benchmark](https://github.com/google-research/google-research/tree/master/mbpp) - Mostly Basic Python Problems dataset
- [SWE-bench](https://www.swebench.com/) - Software Engineering benchmark for real-world bug fixing
- [OpenAI API Reference](https://platform.openai.com/docs/api-reference) - API specification used by all compatible providers
- [uv Documentation](https://docs.astral.sh/uv/) - Python package manager

### AI Usage

AI (Claude, LLMs via Groq/OpenRouter) was used as a coding partner throughout development for:
- Iterating on system prompt design and ablation testing
- Debugging sandbox isolation edge cases (e.g., `exit()` vs flag-based test harness)
- Reviewing and refactoring code for compliance with the subject specification

All architectural decisions, prompt engineering strategies, and benchmark analysis were made and validated by the project author.
