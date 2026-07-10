# Agent Smith - TODO

## Critical (exam blockers)

- [x] Fix SyntaxError in test harness (`agent_mbpp.py` + `mcp_tools_mbpp.py`) — single quotes in assertions broke print strings
- [x] Fix `sandbox_cli.py` MCP protocol — `_McpConnection` class keeps session alive in background thread, wrappers make real `session.call_tool()` calls
- [x] Populate `benchmarks/mbpp/` — tasks 163 and 108 with real validated solutions (Gemini + Llama)
- [ ] Populate `benchmarks/swebench/` — needs real Docker runs on sympy-13480, sympy-14711, xarray-4629 (long runs, do manually)
- [ ] Pull `python:3.11-slim` Docker image on exam machine before running moulinette (`docker pull python:3.11-slim`)

## Subject compliance

- [ ] Evaluation logging structure — subject (p.36) requires `./evaluations/EVAL_TYPE/YYYY-MM-DD_HH-MM-SS/task_id/task.json, solution.json, stdout.log, stderr.log`. Not currently implemented (agents write to `--output` path only).
- [ ] Dynamic sandbox tool manual — subject (p.17): "When a different MCP server is connected, the sandbox should dynamically discover and expose that server's tools." Discovery works but tool docs are not injected into the LLM system prompt dynamically.
- [ ] No hardcoded API keys in source code — `.env` must be in `.gitignore`. Verify before submission.

## Improvements (nice to have)

- [ ] SWE-bench end-to-end test — never tested a full dump->run->validate cycle
- [ ] Test MBPP on harder tasks (math-heavy, edge cases with string quoting)
- [ ] Consider lowering `MBPP_TEMPERATURE` for easier tasks to save iterations
- [ ] `sandbox_template.json` — verify `authorized_imports` covers all common MBPP needs

## Exam day checklist

1. `docker pull python:3.11-slim`
2. Verify `.env` has all provider keys uncommented
3. `make lint` passes
4. Run full MBPP cycle: dump -> agent -> validate
5. Run full SWE-bench cycle: dump -> agent -> validate
6. `benchmarks/` directories contain solution.json files
