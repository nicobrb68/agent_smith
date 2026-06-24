import json
import os
import sys
from json import JSONDecodeError
from typing import Any, Dict, List, Tuple

from pydantic import ValidationError
from student.agent_config import AgentConfig


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

        api_keys: List[str] = [os.getenv("OPENAI_API_KEY", "dummy")]
        rotator: TokenRotator = TokenRotator(api_keys)
        llm: LLMClient = LLMClient(
            rotator, self.config.provider_url, self.config.model_name
        )

        sandbox: Sandbox = Sandbox(sandbox_config)

        return llm, sandbox

    def _save_report(
        self,
        success: bool,
        raw_code: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        """Save the final execution report to a JSON file."""
        output_data: Dict[str, Any] = {
            "task_id": self.task_id,
            "completed": bool(success),
            "code": str(raw_code) if raw_code is not None else "",
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
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
                f"OS Error: Cannot write file to {self.config.output}: {e}"
            )
        except TypeError as e:
            print(
                "JSON Serialization Error: Invalid data types provided:"
                f" {e}"
            )

    def _run_evaluation_loop(
        self, llm: Any, sandbox: Any
    ) -> Tuple[bool, str, int, int]:
        """Run the main evaluation loop (Thought -> Code -> Observation)."""
        messages_context: List[Dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "You are a Python expert. Write only the requested"
                    " function. Do not provide any explanations, no markdown"
                    " code blocks, no text. Just pure, valid Python code."
                ),
            },
            {"role": "user", "content": f"Problem statement:\n{self.prompt}"},
        ]
        total_prompt_tokens: int = 0
        total_completion_tokens: int = 0
        success: bool = False
        raw_code: str = ""

        for attempt in range(1, 11):
            print(f"--- Attempt {attempt} / 10 ---")

            api_answer: Dict[str, Any] = llm.call_api(messages_context)
            total_prompt_tokens += api_answer.get("prompt_tokens", 0)
            total_completion_tokens += api_answer.get("completion_tokens", 0)

            if total_prompt_tokens > 6000 or total_completion_tokens > 1500:
                print("Hard limits exceeded (Tokens)! Stopping agent.")
                success = False
                break

            raw_code = api_answer.get("text", "")
            test_code: str = "\n".join(self.tests)
            final_code: str = raw_code + "\n" + test_code

            sandbox_result_raw: Dict[str, Any] = sandbox.execute_code(
                final_code
            )
            success = sandbox_result_raw.get("success", False)
            log: str = str(sandbox_result_raw.get("output", ""))
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
        return success, raw_code, total_prompt_tokens, total_completion_tokens

    def solve(self) -> None:
        """Orchestrate the loading, loop evaluation, and saving process."""
        llm, sandbox = self._init_tools()
        (
            success,
            raw_code,
            prompt_tokens,
            completion_tokens,
        ) = self._run_evaluation_loop(llm, sandbox)
        self._save_report(success, raw_code, prompt_tokens, completion_tokens)


def main() -> None:
    """Main entry point to execute the agent."""
    agent: AgentMbpp = AgentMbpp()
    agent.solve()


if __name__ == "__main__":
    main()