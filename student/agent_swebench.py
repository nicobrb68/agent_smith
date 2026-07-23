import json
from json import JSONDecodeError
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from datetime import datetime
import re
import asyncio
import subprocess
import threading

from dotenv import load_dotenv
from pydantic import ValidationError
from student.agent_config import AgentConfig
from student.eval_logger import capture_stdio, write_evaluation
from student.sandbox import Sandbox
from student.sandbox_config import SandboxConfig

load_dotenv()


class _McpConnection:
    """MCP session on a dedicated background event loop."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True,
        )
        self._thread.start()
        self._session: Any = None
        self._transport_cm: Any = None
        self._session_cm: Any = None
        self.tool_list: List[Any] = []

    def connect_stdio(self, command: str, args: List[str],
                      cwd: str) -> None:
        future = asyncio.run_coroutine_threadsafe(
            self._do_connect(command, args, cwd),
            self._loop,
        )
        future.result(timeout=30)

    async def _do_connect(self, command: str,
                          args: List[str],
                          cwd: str) -> None:
        from mcp import StdioServerParameters, stdio_client
        from mcp.client.session import ClientSession

        params = StdioServerParameters(
            command=command, args=args,
            cwd=cwd, stderr=sys.stderr,
        )
        self._transport_cm = stdio_client(params)
        r, w = await self._transport_cm.__aenter__()
        self._session_cm = ClientSession(r, w)
        self._session = (
            await self._session_cm.__aenter__()
        )
        await self._session.initialize()
        result = await self._session.list_tools()
        self.tool_list = result.tools

    def call_tool(
        self, name: str, arguments: Dict[str, Any],
    ) -> str:
        future = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(
                name, arguments=arguments,
            ),
            self._loop,
        )
        result = future.result(timeout=120)
        parts: List[str] = []
        for block in result.content:
            if hasattr(block, "text"):
                parts.append(block.text)
        return "\n".join(parts) if parts else str(result)

    def close(self) -> None:
        async def _cleanup() -> None:
            try:
                if self._session_cm:
                    await self._session_cm.__aexit__(
                        None, None, None,
                    )
            except Exception:
                pass
            try:
                if self._transport_cm:
                    await self._transport_cm.__aexit__(
                        None, None, None,
                    )
            except Exception:
                pass

        future = asyncio.run_coroutine_threadsafe(
            _cleanup(), self._loop,
        )
        try:
            future.result(timeout=5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)


class AgentSWEBench:
    """Agent for solving SWE-bench issues autonomously."""

    def __init__(self) -> None:
        """Initialize the agent and load task data."""
        self.config: AgentConfig = AgentConfig()
        try:
            with open(
                self.config.task_file, "r", encoding="utf8"
            ) as f:
                task_data: Dict[str, Any] = json.load(f)
        except (OSError, JSONDecodeError, TypeError) as e:
            print(
                f"Error with the task file provided : {e}\n"
                "End of program."
            )
            sys.exit(1)

        self.task_id: str = str(
            task_data.get("instance_id", "")
        )
        self.problem_statement: str = str(
            task_data.get("problem_statement", "")
        )
        self.eval_script: str = str(
            task_data.get("eval_script", "")
        )
        self.repo: str = str(task_data.get("repo", ""))
        self.docker_image: str = str(
            task_data.get("docker_image", "")
        )
        self.hints_text: str = str(
            task_data.get("hints_text", "")
        )

        if self.config.max_iterations is not None:
            self.max_iterations: int = (
                self.config.max_iterations
            )
        else:
            self.max_iterations = 30

        self.system_prompt: str = ""
        self.last_output_data: Optional[Dict[str, Any]] = None

        print(f"Model : {self.config.model_name}")
        print(f"\n--- [SWE-bench Task {self.task_id}] ---")
        print(f"Docker Image : {self.docker_image}")

    @staticmethod
    def _build_tool_table(
        tool_list: List[Any],
    ) -> str:
        """Generate a markdown tool table from MCP schemas.

        Args:
            tool_list: MCP tool objects from list_tools().

        Returns:
            str: A markdown table documenting every tool.
        """
        rows: List[str] = [
            "| Tool | Signature | Description |",
            "| --- | --- | --- |",
        ]
        for tool in tool_list:
            name = tool.name
            schema = tool.inputSchema or {}
            props = schema.get("properties", {})
            required = set(schema.get("required", []))
            params: List[str] = []
            for pname, pinfo in props.items():
                if pname in required:
                    params.append(pname)
                else:
                    default = pinfo.get("default", "")
                    params.append(
                        f"{pname}={repr(default)}"
                    )
            sig = f"{name}({', '.join(params)})"
            desc = (tool.description or "").split("\n")[0]
            rows.append(f"| {name} | {sig} | {desc} |")
        rows.append(
            "| final_answer | final_answer(patch_str)"
            " | Submit your fix. Argument must be the "
            "output of get_patch() |"
        )
        return "\n".join(rows)

    @staticmethod
    def _build_prompt(tool_table: str) -> str:
        """Build the system prompt for the SWE-bench agent.

        Args:
            tool_table: Markdown table of available tools.

        Returns:
            str: The full system prompt string.
        """
        lines = [
            "You are a Senior Software Engineer. Your "
            "job is to fix the GitHub issue described "
            "below inside a repository mounted at "
            "/testbed.",
            "",
            "## Available Tools",
            "Call tools inside a single ```python block."
            " Each tool returns a string. Wrap every "
            "call in print() so you can see the result.",
            "",
            tool_table,
            "",
            "## Workflow",
            "Follow this step-by-step debugging "
            "methodology:",
            "1. UNDERSTAND: Read the issue carefully. "
            "Identify the symptom and likely area.",
            "2. LOCATE: Use search_code or "
            "search_function_or_class_definition_in_"
            "code to find relevant code. Do NOT guess "
            "file paths.",
            "3. READ: Use read_file to examine the code"
            " around the relevant area. Read enough "
            "context (50-100 lines).",
            "4. DIAGNOSE: Identify the root cause from "
            "the code you read.",
            "5. FIX: Use edit_file with the exact "
            "old_str copied from read_file output. "
            "Make minimal, targeted changes.",
            "6. VERIFY: Run run_tests() to check if "
            "your fix works.",
            "7. SUBMIT: If tests pass, call "
            "final_answer(get_patch()). If tests fail, "
            "read the error, go back to step 3.",
            "",
            "## Rules",
            "- ONE tool call per turn in a single "
            "```python block, then STOP and wait for "
            "the Observation.",
            "- NEVER fabricate file paths or code "
            "- always read first.",
            "- NEVER pass empty or whitespace-only "
            "old_str to edit_file.",
            "- search_code uses PLAIN SUBSTRING "
            "matching, not regex. Do not escape "
            "characters.",
            "- If you get stuck, try a different search"
            " term or read a broader range of lines.",
            "- Keep edits minimal - fix only what the "
            "issue requires.",
            "",
            "## Example Turn",
            "Thought: I need to find where "
            "`validate_email` is defined.",
            "```python",
            "print(search_function_or_class_definition"
            "_in_code('validate_email'))",
            "```",
        ]
        return "\n".join(lines)

    def _init_tools(self) -> Tuple[Any, Sandbox]:
        """Initialize the LLM client and Sandbox.

        Returns:
            Tuple[Any, Sandbox]: The LLM client and sandbox.
        """
        from student.llm import LLMClient, TokenRotator

        rotator = TokenRotator()
        llm = LLMClient(
            rotator,
            self.config.provider_url,
            self.config.model_name,
            temperature=0.0,
        )

        template_path: str = "sandbox_template.json"
        if os.path.exists(template_path):
            try:
                with open(
                    template_path, "r", encoding="utf-8"
                ) as f:
                    config_data: Dict[str, Any] = json.load(f)
                sandbox_config = SandboxConfig(**config_data)
                print(
                    "Sandbox loaded with custom config "
                    f"from {template_path}"
                )
            except (
                OSError, JSONDecodeError, ValidationError
            ) as e:
                print(
                    f"Failed to parse {template_path} "
                    f"({e}). Using default sandbox limits."
                )
                sandbox_config = SandboxConfig()
        else:
            sandbox_config = SandboxConfig()

        sandbox: Sandbox = Sandbox(sandbox_config)
        return llm, sandbox

    def _save_report(
        self,
        success: bool,
        final_patch: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_time: float,
        steps: List[Dict[str, Any]],
    ) -> None:
        """Save the report matching SolutionOutput format."""
        output_data: Dict[str, Any] = {
            "task_id": str(self.task_id),
            "benchmark": "swebench",
            "success": bool(success),
            "solution": (
                str(final_patch) if final_patch else ""
            ),
            "iterations": len(steps),
            "total_requests": len(steps),
            "total_input_tokens": int(prompt_tokens),
            "total_output_tokens": int(completion_tokens),
            "total_time_seconds": float(total_time),
            "steps": steps,
            "system_prompt": self.system_prompt,
            "error": (
                None if success
                else "Agent failed to patch repository"
            ),
            "timestamp": datetime.now().isoformat(),
        }
        self.last_output_data = output_data

        try:
            if os.path.dirname(self.config.output):
                os.makedirs(
                    os.path.dirname(self.config.output),
                    exist_ok=True,
                )
            with open(
                self.config.output, "w", encoding="utf-8"
            ) as f:
                json.dump(output_data, f, indent=4)
            print(
                "Process finished. Output saved to "
                f"{self.config.output}"
            )
        except OSError as e:
            print(
                "OS Error: Cannot write file to "
                f"{self.config.output}: {e}"
            )

    @staticmethod
    def _extract_code(raw_text: str) -> str:
        """Extract executable Python from LLM output.

        Supports: markdown fenced blocks, Anthropic XML
        tool_use, JSON/Hermes tool_call, and ReAct format.

        Args:
            raw_text: Raw LLM output text.

        Returns:
            str: Extracted Python code.
        """
        python_blocks = re.findall(
            r"```python\s*(.*?)\s*```",
            raw_text, re.DOTALL,
        )
        if python_blocks:
            return "\n".join(python_blocks)

        xml_pat = (
            r"<tool_use>\s*<tool_name>\s*(\w+)\s*"
            r"</tool_name>\s*<parameters>(.*?)"
            r"</parameters>\s*</tool_use>"
        )
        xml_match = re.search(
            xml_pat, raw_text, re.DOTALL,
        )
        if xml_match:
            tool_name = xml_match.group(1)
            params_block = xml_match.group(2).strip()
            params: Dict[str, str] = {}
            for pm in re.finditer(
                r"<(\w+)>(.*?)</\1>",
                params_block, re.DOTALL,
            ):
                params[pm.group(1)] = pm.group(2).strip()
            args = ", ".join(
                f"{k}={repr(v)}"
                for k, v in params.items()
            )
            return f"print({tool_name}({args}))"

        json_pat = (
            r'"name"\s*:\s*"(\w+)".*?'
            r'"arguments"\s*:\s*\{([^}]*)\}'
        )
        json_match = re.search(
            json_pat, raw_text, re.DOTALL,
        )
        if json_match:
            tool_name = json_match.group(1)
            try:
                args_obj = json.loads(
                    "{" + json_match.group(2) + "}"
                )
                args = ", ".join(
                    f"{k}={repr(v)}"
                    for k, v in args_obj.items()
                )
                return f"print({tool_name}({args}))"
            except (json.JSONDecodeError, ValueError):
                pass

        react_pat = (
            r"Action\s*:\s*(\w+)\s*\n"
            r"Action\s*Input\s*:\s*(.*?)(?:\n|$)"
        )
        react_match = re.search(
            react_pat, raw_text, re.DOTALL,
        )
        if react_match:
            tool_name = react_match.group(1)
            action_input = react_match.group(2).strip()
            try:
                args_obj = json.loads(action_input)
                if isinstance(args_obj, dict):
                    args = ", ".join(
                        f"{k}={repr(v)}"
                        for k, v in args_obj.items()
                    )
                    return (
                        f"print({tool_name}({args}))"
                    )
            except (json.JSONDecodeError, ValueError):
                return (
                    f"print({tool_name}"
                    f"({repr(action_input)}))"
                )

        return raw_text

    @staticmethod
    def _build_mcp_tools(
        conn: _McpConnection,
    ) -> Dict[str, Callable[..., Any]]:
        """Build sandbox tools that route through MCP.

        Args:
            conn: Active MCP connection on its own loop.

        Returns:
            Dict mapping tool names to callables.
        """
        tools: Dict[str, Callable[..., Any]] = {}

        for tool in conn.tool_list:
            schema = tool.inputSchema or {}
            props = schema.get("properties", {})
            param_names = list(props.keys())
            tname = tool.name

            def _make_wrapper(
                name: str, pnames: List[str],
            ) -> Callable[..., Any]:
                def wrapper(
                    *args: Any, **kwargs: Any,
                ) -> str:
                    call_kwargs: Dict[str, Any] = dict(
                        zip(pnames, args),
                    )
                    call_kwargs.update(kwargs)
                    return conn.call_tool(
                        name, call_kwargs,
                    )
                return wrapper

            tools[tname] = _make_wrapper(
                tname, param_names,
            )
        return tools

    def _run_evaluation_loop(
        self,
        llm: Any,
        sandbox: Sandbox,
        conn: _McpConnection,
    ) -> Tuple[
        bool, str, int, int, List[Dict[str, Any]]
    ]:
        """Run the main SWE-bench agent loop.

        Args:
            llm: The LLM client.
            sandbox: The sandbox instance.
            conn: Active MCP connection.

        Returns:
            Tuple of success, patch, prompt tokens,
            completion tokens, and step metrics.
        """
        tool_table = self._build_tool_table(
            conn.tool_list,
        )
        self.system_prompt = self._build_prompt(
            tool_table,
        )

        hint_block = ""
        if self.hints_text.strip():
            hint_block = (
                "\n\nHint (optional, provided with "
                "the task - it may point you in the "
                "right direction, but you must still "
                "follow the STRICT PROTOCOL: read/"
                "search the actual code before "
                "editing, do not assume the hint "
                "alone is sufficient):\n"
                f"{self.hints_text}"
            )

        user_msg = (
            f"Repository: {self.repo}\n"
            "Note: You are already placed at the root "
            "of the repository directory (/testbed).\n"
            "All paths should be relative to the "
            "current directory (e.g., use "
            "'django/forms/widgets.py').\n\n"
            "Issue Description:\n"
            f"{self.problem_statement}"
            f"{hint_block}"
        )

        messages_context: List[Dict[str, Any]] = [
            {
                "role": "system",
                "content": self.system_prompt,
            },
            {"role": "user", "content": user_msg},
        ]
        total_prompt_tokens: int = 0
        total_completion_tokens: int = 0
        success: bool = False
        final_patch: str = ""
        steps: List[Dict[str, Any]] = []

        injected_tools = self._build_mcp_tools(conn)
        max_obs_chars: int = 15000
        loop_start: float = time.time()

        for attempt in range(
            1, self.max_iterations + 1
        ):
            elapsed = time.time() - loop_start
            if elapsed > 850:
                print(
                    "Approaching 900s time limit. "
                    "Stopping agent."
                )
                break

            print(
                f"--- SWE-bench Attempt {attempt}"
                f" / {self.max_iterations} ---"
            )

            start_api: float = time.time()
            try:
                api_answer = llm.call_api(
                    messages_context, tools=None,
                )
            except RuntimeError as e:
                print(e)
                break

            end_api: float = time.time()
            request_time_ms: float = (
                (end_api - start_api) * 1000
            )

            step_in: int = api_answer.get(
                "prompt_tokens", 0,
            )
            step_out: int = api_answer.get(
                "completion_tokens", 0,
            )
            total_prompt_tokens += step_in
            total_completion_tokens += step_out

            if (
                total_prompt_tokens > 300000
                or total_completion_tokens > 10000
            ):
                print(
                    "Hard limits exceeded (Tokens)! "
                    "Stopping agent."
                )
                break

            llm_text = api_answer.get("text", "")
            code_to_run = self._extract_code(llm_text)
            cfg = llm.rotator.get_current_config()

            has_code = (
                "```python" in llm_text
                or "<tool_use>" in llm_text
                or '"name"' in llm_text
                or "Action:" in llm_text
            )

            if not has_code:
                observation = (
                    "No valid code block found in "
                    "your response. You MUST respond "
                    "with a ```python block containing"
                    " exactly one tool call wrapped in"
                    " print(). Example:\n"
                    "```python\n"
                    "print(search_code("
                    "'some_function'))\n"
                    "```"
                )
                messages_context.append(
                    {
                        "role": "assistant",
                        "content": llm_text,
                    }
                )
                messages_context.append(
                    {
                        "role": "user",
                        "content": (
                            "Observation:\n"
                            f"{observation}"
                        ),
                    }
                )
                steps.append({
                    "step": attempt,
                    "input_tokens": step_in,
                    "output_tokens": step_out,
                    "request_time_ms": request_time_ms,
                    "timestamp": (
                        datetime.now().isoformat()
                    ),
                    "api_url": cfg["url"],
                    "model_name": cfg["model"],
                    "llm_output": llm_text,
                    "sandbox_input": "",
                    "sandbox_output": observation,
                    "retries": 0,
                })
                continue

            print("Executing code in Sandbox...")
            sandbox_res = sandbox.execute_code(
                code_to_run.strip(),
                injected_tools=injected_tools,
            )

            observation = sandbox_res.get("output", "")
            is_final = sandbox_res.get(
                "is_final", False,
            )

            if len(observation) > max_obs_chars:
                half = max_obs_chars // 2
                trunc_msg = (
                    "\n\n... [TRUNCATED - output too "
                    f"long, {len(observation)} chars "
                    "total] ...\n\n"
                )
                observation = (
                    observation[:half]
                    + trunc_msg
                    + observation[-half:]
                )

            if (
                not sandbox_res.get("success")
                and not is_final
            ):
                observation = (
                    f"[SANDBOX ERROR]\n{observation}"
                )

            messages_context.append(
                {
                    "role": "assistant",
                    "content": llm_text,
                }
            )
            messages_context.append(
                {
                    "role": "user",
                    "content": (
                        f"Observation:\n{observation}"
                    ),
                }
            )

            steps.append({
                "step": attempt,
                "input_tokens": step_in,
                "output_tokens": step_out,
                "request_time_ms": request_time_ms,
                "timestamp": (
                    datetime.now().isoformat()
                ),
                "api_url": cfg["url"],
                "model_name": cfg["model"],
                "llm_output": llm_text,
                "sandbox_input": code_to_run,
                "sandbox_output": observation,
                "retries": 0,
            })

            if is_final:
                final_patch = sandbox_res.get(
                    "solution", "",
                ).strip()
                success = "diff --git" in final_patch
                if success:
                    print(
                        "Patch submitted via "
                        "final_answer()."
                    )
                    break

        return (
            success,
            final_patch,
            total_prompt_tokens,
            total_completion_tokens,
            steps,
        )

    def solve(self) -> None:
        """Run the full SWE-bench agent pipeline."""
        llm, sandbox = self._init_tools()

        container_name = f"agent_smith_{self.task_id}"

        try:
            with open(
                ".container_id", "w", encoding="utf-8",
            ) as f:
                f.write(container_name)
        except OSError as e:
            print(f"Error: {e}")
            sys.exit(1)

        subprocess.run(
            f"docker rm -f {container_name}",
            shell=True, capture_output=True,
        )

        print(
            "Starting container: "
            f"{container_name}..."
        )
        start_res = subprocess.run(
            [
                "docker", "run", "-d",
                "--name", container_name,
                self.docker_image,
                "tail", "-f", "/dev/null",
            ],
            capture_output=True, text=True,
        )

        if start_res.returncode != 0:
            print(
                "Fatal Docker error: "
                f"{start_res.stderr}"
            )
            sys.exit(1)

        print(
            "Injecting test script into "
            "/testbed..."
        )
        subprocess.run(
            [
                "docker", "exec", "-i",
                container_name, "bash", "-c",
                "cat > /testbed/eval_script.sh",
            ],
            input=self.eval_script,
            text=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "docker", "exec", container_name,
                "chmod", "+x",
                "/testbed/eval_script.sh",
            ],
            capture_output=True,
        )

        conn = _McpConnection()
        try:
            conn.connect_stdio(
                sys.executable,
                [os.path.abspath(
                    "mcp_tools_swebench.py"
                )],
                os.path.abspath("."),
            )
            print(
                "MCP tools discovered: "
                f"{[t.name for t in conn.tool_list]}"
            )

            start_agent = time.time()
            (
                success,
                final_patch,
                prompt_tokens,
                completion_tokens,
                steps,
            ) = self._run_evaluation_loop(
                llm, sandbox, conn,
            )
            end_agent = time.time()
            total_time = end_agent - start_agent
        finally:
            conn.close()
            print(
                "Stopping container: "
                f"{container_name}..."
            )
            subprocess.run(
                f"docker rm -f {container_name}",
                shell=True, capture_output=True,
            )
            if os.path.exists(".container_id"):
                os.remove(".container_id")

        self._save_report(
            success, final_patch,
            prompt_tokens, completion_tokens,
            total_time, steps,
        )


def main() -> None:
    """Entry point for the SWE-bench agent."""
    with capture_stdio() as (out_tee, err_tee):
        agent: Optional[AgentSWEBench] = None
        try:
            agent = AgentSWEBench()
            agent.solve()
        finally:
            if agent is not None and agent.last_output_data:
                write_evaluation(
                    "swebench",
                    agent.task_id,
                    agent.config.task_file,
                    agent.last_output_data,
                    out_tee.getvalue(),
                    err_tee.getvalue(),
                )


if __name__ == "__main__":
    main()
