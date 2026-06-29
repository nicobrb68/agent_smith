import json
from json import JSONDecodeError
import os
import sys
import time
from typing import Any, Dict, List, Tuple

from dotenv import load_dotenv
from pydantic import ValidationError
from student.agent_config import AgentConfig

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
            print(
                f"Error with the task file provided : {e}\nEnd of program."
            )
            sys.exit(1)

        self.task_id: str = str(task_data.get("instance_id", ""))
        self.problem_statement: str = str(
            task_data.get("problem_statement", "")
        )
        self.repo: str = str(task_data.get("repo", ""))

        # SWE-bench constraints
        self.max_iterations: int = 30

        # system prompt
        self.system_prompt: str = (
            "You are an expert Senior Software Engineer. Your goal is to "
            "solve the provided GitHub issue by exploring the repository, "
            "modifying files, and validating your changes. "
            "Always produce a valid git patch as your final answer."
        )

        print(f"Model : {self.config.model_name}")
        print(f"\n--- [SWE-bench Task {self.task_id}] ---")
        print(f"Repository : {self.repo}")

    def _init_tools(self) -> Tuple[Any, Any]:
        """Initialize the LLM client and Token rotator (Sandbox via MCP)."""
        from student.llm import LLMClient, TokenRotator

        api_keys: List[str] = []
        for i in range(1, 4):
            key = os.getenv(f"AGENT_KEY_{i}")
            if key:
                api_keys.append(key)

        if not api_keys:
            default_key = os.getenv("OPENAI_API_KEY")
            api_keys = [default_key] if default_key else ["dummy"]

        rotator: TokenRotator = TokenRotator(api_keys)
        llm: LLMClient = LLMClient(
            rotator, self.config.provider_url, self.config.model_name
        )

        mcp_client_placeholder: Any = None

        return llm, mcp_client_placeholder

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
        from datetime import datetime

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
                os.makedirs(
                    os.path.dirname(self.config.output), exist_ok=True
                )
            with open(self.config.output, "w", encoding="utf-8") as f:
                json.dump(output_data, f, indent=4)
            print(f"Process finished. Output saved to {self.config.output}")

        except OSError as e:
            print(
                f"OS Error: Cannot write file to {self.config.output}: {e}"
            )

    def _run_evaluation_loop(
        self, llm: Any, mcp_client: Any
    ) -> Tuple[bool, str, int, int, List[Dict[str, Any]]]:
        """Run the main software engineering loop (30 attempts max)."""
        from datetime import datetime

        messages_context: List[Dict[str, str]] = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": (
                    f"Repository: {self.repo}\n\n"
                    f"Issue Description:\n{self.problem_statement}"
                ),
            },
        ]
        total_prompt_tokens: int = 0
        total_completion_tokens: int = 0
        success: bool = False
        final_patch: str = ""
        steps: List[Dict[str, Any]] = []

        for attempt in range(1, self.max_iterations + 1):
            print(f"--- SWE-bench Attempt {attempt} / 30 ---")

            start_api: float = time.time()
            api_answer: Dict[str, Any] = llm.call_api(messages_context)
            end_api: float = time.time()
            request_time_ms: float = (end_api - start_api) * 1000

            step_input_tokens: int = api_answer.get("prompt_tokens", 0)
            step_output_tokens: int = api_answer.get("completion_tokens", 0)

            total_prompt_tokens += step_input_tokens
            total_completion_tokens += step_output_tokens

            # SWE-bench broad limits: 300k input / 50k output
            if total_prompt_tokens > 300000 or total_completion_tokens > 50000:
                print("Hard limits exceeded (Tokens)! Stopping agent.")
                break

            final_patch = api_answer.get("text", "")

            # Temporary extraction layer for git diff formatting
            if "```diff" in final_patch:
                final_patch = final_patch.split("```diff")[1].split("```")[0]
            elif "```" in final_patch:
                final_patch = final_patch.split("```")[1].split("```")[0]

            final_patch = final_patch.strip()

            # Temporary success criterion before tool integration
            success = "diff --git" in final_patch

            steps.append(
                {
                    "step": attempt,
                    "input_tokens": step_input_tokens,
                    "output_tokens": step_output_tokens,
                    "request_time_ms": request_time_ms,
                    "timestamp": datetime.now().isoformat(),
                    "api_url": self.config.provider_url,
                    "model_name": self.config.model_name,
                    "llm_output": api_answer.get("text", ""),
                    "sandbox_input": "mcp_call_placeholder",
                    "sandbox_output": "mcp_response_placeholder",
                    "retries": 0,
                }
            )

            if success is True:
                print("✅ Patch file generated successfully!")
                break
            else:
                print(f"❌ Attempt {attempt} failed.")
                messages_context.append(
                    {
                        "role": "user",
                        "content": "Your patch format was invalid. Try again.",
                    }
                )

        return (
            success,
            final_patch,
            total_prompt_tokens,
            total_completion_tokens,
            steps,
        )

    def solve(self) -> None:
        """Orchestrate loading, engineering loop, and report saving."""
        llm, mcp_client = self._init_tools()
        server_params = StdioServerParameters(
            command="python",
            args=["-m", "student.mcp_tools_swebench"],
        )
        start_agent : float = 0.0

        with stdio_client(server_params) as (read_stream, write_stream):
            with ClientSession(read_stream, write_stream) as session:
                session.initialize()
                available_tools = session.list_tools()

                start_agent = time.time()
                (
                    success,
                    final_patch,
                    prompt_tokens,
                    completion_tokens,
                    steps,
                ) = self._run_evaluation_loop(llm, session)
                end_agent: float = time.time()
                total_time: float = end_agent - start_agent

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
    agent.solve()


if __name__ == "__main__":
    main()