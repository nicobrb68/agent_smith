import json
import os
import sys
import time
from json import JSONDecodeError
from typing import Any, Dict, List, Tuple

from pydantic import ValidationError
from student.agent_config import AgentConfig
from dotenv import load_dotenv
load_dotenv()


class AgentMbpp:
    """Agent for solving MBPP tasks autonomously."""

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

        self.task_id: str = str(task_data.get("task_id", ""))
        self.prompt: str = str(task_data.get("task_definition", ""))
        self.tests: List[str] = list(task_data.get("test_list", []))
        self.function_definition : str = str(task_data.get("function_definition", ""))

        # we keep the full system prompt handy for the final report
        self.system_prompt: str = (
            "You are a Python expert. Write only the requested function. "
            "Do not provide any explanations, no markdown code blocks, "
            "no text. Just pure, valid Python code."
        )

        print(f"Model : {self.config.model_name}")
        print(f"\n--- [Exercise {self.task_id}] ---")
        print(f"Prompt : {self.prompt}")
        print(f"Number of tests to validate: {len(self.tests)}")

    def _init_tools(self) -> Tuple[Any, Any]:
        """Initialize the LLM client, Token rotator, and Sandbox."""
        from student.llm import LLMClient, TokenRotator
        from student.sandbox import Sandbox
        from student.sandbox_config import SandboxConfig

        template_path: str = "sandbox_template.json"

        if os.path.exists(template_path):
            try:
                with open(template_path, "r", encoding="utf-8") as f:
                    config_data: Dict[str, Any] = json.load(f)
                sandbox_config: SandboxConfig = SandboxConfig(**config_data)
                print(
                    "Sandbox loaded with custom config from"
                    f" {template_path}"
                )
            except (OSError, JSONDecodeError, ValidationError) as e:
                print(
                    f"Failed to parse {template_path} ({e})."
                    " Using default sandbox limits."
                )
                sandbox_config = SandboxConfig()
        else:
            print(
                f"{template_path} not found. Using default sandbox limits."
            )
            sandbox_config = SandboxConfig()

        api_keys: List[str] = []
        for i in range(1, 4):
            key = os.getenv(f"AGENT_KEY_{i}")
            if key:
                api_keys.append(key)

        if not api_keys:
            default_key = os.getenv("OPENAI_API_KEY")
            api_keys = [default_key] if default_key else ["dummy"]

        rotator = TokenRotator() # Il s'occupe de tout charger tout seul de manière abstraite
        llm = LLMClient(rotator, self.config.provider_url, self.config.model_name)

        sandbox: Sandbox = Sandbox(sandbox_config)

        return llm, sandbox

    def _save_report(
        self,
        success: bool,
        raw_code: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_time: float,
        steps: List[Dict[str, Any]],
    ) -> None:
        """Save the final execution report to a JSON file."""
        from datetime import datetime

        # Matches the SolutionOutput Pydantic structure exactly
        output_data: Dict[str, Any] = {
            "task_id": str(self.task_id),
            "benchmark": "mbpp",
            "success": bool(success),
            "solution": str(raw_code) if raw_code is not None else "",
            "iterations": len(steps),
            "total_requests": len(steps),
            "total_input_tokens": int(prompt_tokens),
            "total_output_tokens": int(completion_tokens),
            "total_time_seconds": float(total_time),
            "steps": steps,
            "system_prompt": self.system_prompt,
            "error": None if success else "Agent failed to validate code",
            "timestamp": datetime.now().isoformat(),
        }

        try:
            if os.path.dirname(self.config.output):
                os.makedirs(
                    os.path.dirname(self.config.output), exist_ok=True
                )
            with open(self.config.output, "w", encoding="utf-8") as f:
                json.dump(output_data, f, indent=4)
            print(f"🎉 Process finished. Output saved to {self.config.output}")

        except OSError as e:
            print(
                f"❌ OS Error: Cannot write file to {self.config.output}: {e}"
            )
        except TypeError as e:
            print(
                "❌ JSON Serialization Error: Invalid data types provided:"
                f" {e}"
            )

    def _run_evaluation_loop(
        self, llm: Any, sandbox: Any
    ) -> Tuple[bool, str, int, int, List[Dict[str, Any]]]:
        """Run the main evaluation loop (Thought -> Code -> Observation)."""
        from datetime import datetime

        messages_context: List[Dict[str, str]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": (f"Problem statement:\n{self.prompt}\n\n"
             f"You MUST use this exact function signature:\n"
             f"{self.function_definition}")
             },
        ]
        total_prompt_tokens: int = 0
        total_completion_tokens: int = 0
        success: bool = False
        raw_code: str = ""
        steps: List[Dict[str, Any]] = []

        for attempt in range(1, 11):
            print(f"--- Attempt {attempt} / 10 ---")

            # Measure specific API request latency
            start_api: float = time.time()
            api_answer: Dict[str, Any] = llm.call_api(messages_context)
            end_api: float = time.time()
            request_time_ms: float = (end_api - start_api) * 1000

            step_input_tokens: int = api_answer.get("prompt_tokens", 0)
            step_output_tokens: int = api_answer.get("completion_tokens", 0)

            total_prompt_tokens += step_input_tokens
            total_completion_tokens += step_output_tokens

            if total_prompt_tokens > 6000 or total_completion_tokens > 1500:
                print("Hard limits exceeded (Tokens)! Stopping agent.")
                success = False
                break

            raw_code = api_answer.get("text", "")
            raw_code = api_answer.get("text", "")


            if "```python" in raw_code:
                raw_code = raw_code.split("```python")[1].split("```")[0]
            elif "```" in raw_code:
                raw_code = raw_code.split("```")[1].split("```")[0]
            
            raw_code = raw_code.strip()

            test_code = ""
            for test_assert in self.tests:
                test_code += f"\ntry:\n"
                test_code += f"    {test_assert}\n"
                test_code += f"except AssertionError:\n"
                test_code += f"    print('Assertion failed: {test_assert}')\n"
                test_code += f"    exit(1)\n"
                test_code += f"except Exception as e:\n"
                test_code += f"    print(f'Runtime Error during test: {{type(e).__name__}}: {{e}}')\n"
                test_code += f"    exit(1)\n"
            
            test_code += "\nprint('ALL_TESTS_PASSED')\n"

            final_code: str = raw_code + "\n" + test_code

            sandbox_result_raw: Dict[str, Any] = sandbox.execute_code(
                final_code
            )
            # success = sandbox_result_raw.get("success", False)
            log: str = str(sandbox_result_raw.get("output", ""))
            success = "ALL_TESTS_PASSED" in log

            # Append the structured dictionary corresponding to StepMetrics
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
                    "sandbox_input": final_code,
                    "sandbox_output": log,
                    "retries": 0,
                }
            )

            if success is True:
                print("✅ Code validated successfully by the sandbox!")
                break
            else:
                print(f"❌ Attempt {attempt} failed.")
                messages_context.append(
                    {
                        "role": "user",
                        "content": (
                            "Your code failed during execution. Here are the"
                            f" logs:\n{log}\nPlease fix the errors."
                        ),
                    }
                )

        return (
            success,
            raw_code,
            total_prompt_tokens,
            total_completion_tokens,
            steps,
        )

    def solve(self) -> None:
        """Orchestrate the loading, loop evaluation, and saving process."""
        llm, sandbox = self._init_tools()

        # Track full agent runtime execution
        start_agent: float = time.time()
        (
            success,
            raw_code,
            prompt_tokens,
            completion_tokens,
            steps,
        ) = self._run_evaluation_loop(llm, sandbox)
        end_agent: float = time.time()
        total_time: float = end_agent - start_agent

        self._save_report(
            success, raw_code, prompt_tokens, completion_tokens,
            total_time, steps
        )


def main() -> None:
    """Main entry point to execute the agent."""
    agent: AgentMbpp = AgentMbpp()
    agent.solve()


if __name__ == "__main__":
    main()