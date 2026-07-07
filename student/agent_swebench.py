import json
from json import JSONDecodeError
import os
import sys
import time
from typing import Any, Dict, List, Tuple, Callable
from datetime import datetime
import re
import asyncio
import subprocess

from dotenv import load_dotenv
from student.agent_config import AgentConfig
from student.sandbox import Sandbox
from student.sandbox_config import SandboxConfig
from mcp import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession

load_dotenv()


class AgentSWEBench:
    """Agent designed to solve complex SWE-bench issues autonomously."""

    def __init__(self) -> None:
        """Initialize the agent, configuration, and load task data."""
        self.config: AgentConfig = AgentConfig()
        try:
            with open(self.config.task_file, "r", encoding="utf8") as f:
                task_data: Dict[str, Any] = json.load(f)
        except (OSError, JSONDecodeError, TypeError) as e:
            print(f"Error with the task file provided : {e}\nEnd of program.")
            sys.exit(1)

        self.task_id: str = str(task_data.get("instance_id", ""))
        self.problem_statement: str = str(
            task_data.get("problem_statement", "")
        )
        self.eval_script: str = str(task_data.get("eval_script", ""))
        self.repo: str = str(task_data.get("repo", ""))
        self.docker_image: str = str(task_data.get("docker_image", ""))

        self.max_iterations: int = 30

        self.system_prompt: str = (
            "You are a Senior Software Engineer. Fix the provided GitHub "
            "issue using Python code blocks.\n\n"
            "Available tools (MUST be wrapped in print()):\n"
            "- read_file(filepath, start_line, end_line) -> str\n"
            "- edit_file(filepath, old_str, new_str) -> str\n"
            "- list_files(directory, pattern) -> str\n"
            "- search_code(pattern, file_pattern) -> str\n"
            "- run_tests() -> str\n"
            "- get_patch() -> str\n"
            "- run_command(command, workdir) -> str\n"
            "- final_answer(patch_str) -> None\n\n"
            "STRICT PROTOCOL:\n"
            "1. NO GUESSING. You must call search_code or read_file to "
            "inspect code BEFORE editing.\n"
            "2. Execute exactly ONE tool call per turn inside a single "
            "```python block, then wait.\n"
            "3. NEVER pass empty strings or whitespace as old_str to "
            "edit_file.\n"
            "4. IF A TEST FAILS: Analyze the error stack trace, read the "
            "breaking file, fix it, and re-run tests.\n"
            "5. Submit ONLY when run_tests() passes 100% cleanly by "
            "calling final_answer(get_patch())."
        )

        print(f"Model : {self.config.model_name}")
        print(f"\n--- [SWE-bench Task {self.task_id}] ---")
        print(f"Docker Image : {self.docker_image}")

    def _init_tools(self) -> Tuple[Any, Sandbox]:
        """Initialize the LLM client and Sandbox."""
        from student.llm import LLMClient, TokenRotator

        rotator = TokenRotator()
        llm = LLMClient(
            rotator,
            self.config.provider_url,
            self.config.model_name,
            temperature=0.0,
        )

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
        """Save the final execution report matching SolutionOutput format."""
        output_data: Dict[str, Any] = {
            "task_id": str(self.task_id),
            "benchmark": "swebench",
            "success": bool(success),
            "solution": str(final_patch) if final_patch else "",
            "iterations": len(steps),
            "total_requests": len(steps),
            "total_input_tokens": int(prompt_tokens),
            "total_output_tokens": int(completion_tokens),
            "total_time_seconds": float(total_time),
            "steps": steps,
            "system_prompt": self.system_prompt,
            "error": None if success else "Agent failed to patch repository",
            "timestamp": datetime.now().isoformat(),
        }

        try:
            if os.path.dirname(self.config.output):
                os.makedirs(os.path.dirname(self.config.output), exist_ok=True)
            with open(self.config.output, "w", encoding="utf-8") as f:
                json.dump(output_data, f, indent=4)
            print(f"Process finished. Output saved to {self.config.output}")
        except OSError as e:
            print(f"OS Error: Cannot write file to {self.config.output}: {e}")

    async def _run_evaluation_loop(
        self, llm: Any, sandbox: Sandbox, mcp_client: Any, loop: Any
    ) -> Tuple[bool, str, int, int, List[Dict[str, Any]]]:
        """Run the main software engineering loop using Sandbox execution."""
        messages_context: List[Dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": f"Repository: {self.repo}\n"
                           f"Note: You are already placed at the root of "
                           f"the repository directory (/testbed).\n"
                           f"All paths should be relative to the current "
                           f"directory (e.g., use 'widgets.py').\n\n"
                           f"Issue Description:\n{self.problem_statement}",
            },
        ]
        total_prompt_tokens: int = 0
        total_completion_tokens: int = 0
        success: bool = False
        final_patch: str = ""
        steps: List[Dict[str, Any]] = []

        import mcp_tools_swebench

        def call_and_print(func: Callable[..., Any],
                           *args: Any, **kwargs: Any) -> Any:
            res = func(*args, **kwargs)
            print(res)
            return res

        injected_tools: Dict[str, Callable[..., Any]] = {
            "read_file": lambda filepath, start_line, end_line: (
                call_and_print(
                    mcp_tools_swebench.read_file,
                    filepath,
                    start_line,
                    end_line,
                )
            ),
            "edit_file": lambda filepath, old_str, new_str: call_and_print(
                mcp_tools_swebench.edit_file, filepath, old_str, new_str
            ),
            "list_files": lambda directory=".", pattern="*": call_and_print(
                mcp_tools_swebench.list_files, directory, pattern
            ),
            "search_code": lambda pattern, file_pattern="*.py": (
                call_and_print(
                    mcp_tools_swebench.search_code, pattern, file_pattern
                )
            ),
            "run_tests": lambda: call_and_print(
                mcp_tools_swebench.run_tests
            ),
            "get_patch": lambda: call_and_print(
                mcp_tools_swebench.get_patch
            ),
            "run_command": lambda command, workdir=".": call_and_print(
                mcp_tools_swebench.run_command, command, workdir
            ),
        }

        for attempt in range(1, self.max_iterations + 1):
            print(
                f"--- SWE-bench Attempt {attempt} / "
                f"{self.max_iterations} ---"
            )

            start_api: float = time.time()
            try:
                api_answer = llm.call_api(messages_context, tools=None)
            except RuntimeError as e:
                print(e)
                break

            end_api: float = time.time()
            request_time_ms: float = (end_api - start_api) * 1000

            step_input_tokens: int = api_answer.get("prompt_tokens", 0)
            step_output_tokens: int = api_answer.get("completion_tokens", 0)
            total_prompt_tokens += step_input_tokens
            total_completion_tokens += step_output_tokens

            if (
                total_prompt_tokens > 300000
                or total_completion_tokens > 10000
            ):
                print("Hard limits exceeded (Tokens)! Stopping agent.")
                break

            llm_text = api_answer.get("text", "")

            python_blocks = re.findall(
                r"```python\s*(.*?)\s*```", llm_text, re.DOTALL
            )
            if python_blocks:
                code_to_run = "\n".join(python_blocks)
            else:
                code_to_run = llm_text

            print("Executing code in Sandbox...")
            sandbox_res = sandbox.execute_code(
                code_to_run.strip(), injected_tools=injected_tools
            )

            observation = sandbox_res.get("output", "")
            is_final = sandbox_res.get("is_final", False)

            messages_context.append(
                {"role": "assistant", "content": llm_text}
            )
            messages_context.append(
                {
                    "role": "user",
                    "content": (
                        f"Observation from sandbox execution:\n"
                        f"{observation}"
                    ),
                }
            )

            steps.append({
                "step": attempt,
                "input_tokens": step_input_tokens,
                "output_tokens": step_output_tokens,
                "request_time_ms": request_time_ms,
                "timestamp": datetime.now().isoformat(),
                "api_url": llm.rotator.get_current_config()["url"],
                "model_name": llm.rotator.get_current_config()["model"],
                "llm_output": llm_text,
                "sandbox_input": code_to_run,
                "sandbox_output": observation,
                "retries": 0,
            })

            if is_final:
                final_patch = sandbox_res.get("solution", "").strip()
                success = "diff --git" in final_patch
                if success:
                    print(
                        "✅ L'IA a soumis un patch validé via final_answer() !"
                    )
                    break

        return (
            success,
            final_patch,
            total_prompt_tokens,
            total_completion_tokens,
            steps,
        )

    async def solve(self) -> None:
        """Orchestrate container generation and systematic teardown."""
        llm, sandbox = self._init_tools()

        container_name = f"agent_smith_{self.task_id}"

        try:
            with open(".container_id", "w", encoding="utf-8") as f:
                f.write(container_name)
        except OSError as e:
            print(f"Error: {e}")
            sys.exit(1)

        subprocess.run(
            f"docker rm -f {container_name}", shell=True, capture_output=True
        )

        print(f"📥 Instanciation du conteneur isolé : {container_name}...")
        start_res = subprocess.run([
            "docker", "run", "-d",
            "--name", container_name,
            self.docker_image,
            "tail", "-f", "/dev/null"
        ], capture_output=True, text=True)

        if start_res.returncode != 0:
            print(f"Erreur fatale au lancement de Docker : {start_res.stderr}")
            sys.exit(1)

        print("📝 Injection du script de test officiel dans /testbed...")
        subprocess.run(
            [
                "docker", "exec", "-i", container_name,
                "bash", "-c", "cat > /testbed/eval_script.sh"
            ],
            input=self.eval_script,
            text=True,
            capture_output=True
        )
        subprocess.run(
            [
                "docker", "exec", container_name,
                "chmod", "+x", "/testbed/eval_script.sh"
            ],
            capture_output=True
        )

        server_params = StdioServerParameters(
            command=sys.executable,
            args=[os.path.abspath("mcp_tools_swebench.py")],
            cwd=os.path.abspath("."),
            stderr=sys.stderr
        )

        current_loop = asyncio.get_running_loop()

        try:
            async with stdio_client(server_params) as (
                read_stream, write_stream
            ):
                async with ClientSession(
                    read_stream, write_stream
                ) as session:
                    await session.initialize()

                    start_agent = time.time()
                    (
                        success,
                        final_patch,
                        prompt_tokens,
                        completion_tokens,
                        steps,
                    ) = await self._run_evaluation_loop(
                        llm, sandbox, session, current_loop
                    )
                    end_agent = time.time()
                    total_time = end_agent - start_agent
        finally:
            print(
                f"🧹 Fermeture et suppression du conteneur : "
                f"{container_name}..."
            )
            subprocess.run(
                f"docker rm -f {container_name}",
                shell=True,
                capture_output=True
            )
            if os.path.exists(".container_id"):
                os.remove(".container_id")

        self._save_report(
            success,
            final_patch,
            prompt_tokens,
            completion_tokens,
            total_time,
            steps,
        )


def main() -> None:
    """Main entry point to execute the SWE-bench agent."""
    agent: AgentSWEBench = AgentSWEBench()
    asyncio.run(agent.solve())


if __name__ == "__main__":
    main()
