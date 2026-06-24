import json
import sys
from json import JSONDecodeError
from student.agent_config import AgentConfig


class AgentMbpp:

    def __init__(self) -> None:
        self.config = AgentConfig()
        try:
            with open(self.config.task_file, "r", encoding="utf8") as f:
                task_data = json.load(f)
        except (OSError, JSONDecodeError, TypeError) as e:
            print(
                f"Error with the task file provided : {e}\nEnd of program."
            )
            sys.exit(1)

        self.task_id = task_data.get("task_id", "")
        self.prompt = task_data.get("task_definition", "")
        self.tests = task_data.get("test_list", [])

        print(f"Model : {self.config.model_name}")
        print(f"\n--- [Exercise {self.task_id}] ---")
        print(f"Prompt : {self.prompt}")
        print(f"Number of tests to validate: {len(self.tests)}")

    def solve(self):
        import os
        from student.llm import LLMClient, TokenRotator
        from student.sandbox import Sandbox
        from student.sandbox_config import SandboxConfig

        api_keys = [os.getenv("OPENAI_API_KEY", "dummy")]
        rotator = TokenRotator(api_keys)
        llm = LLMClient(rotator, self.config.provider_url, self.config.model_name)
        sandbox_config = SandboxConfig()
        sandbox = Sandbox(sandbox_config)
        # initialize the conversation history
        messages_context = [
            {
                "role": "system",
                "content": (
                    "You are a Python expert. Write only the requested function. "
                    "Do not provide any explanations, no markdown code blocks, no text. "
                    "Just pure, valid Python code."
                )
            },
            {
                "role": "user",
                "content": f"Problem statement:\n{self.prompt}"
            }
        ]
        llm.call_api(messages_context)
        

def main() -> None:
    agent = AgentMbpp()
    agent.solve()


if __name__ == "__main__":
    main()