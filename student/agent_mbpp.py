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

    def _init_tools(self):
        """Initialize the LLM client, Token rotator, and Sandbox."""
        import os
        from student.llm import LLMClient, TokenRotator
        from student.sandbox import Sandbox
        from student.sandbox_config import SandboxConfig

        api_keys = [os.getenv("OPENAI_API_KEY", "dummy")]
        rotator = TokenRotator(api_keys)
        llm = LLMClient(rotator, self.config.provider_url, self.config.model_name)

        sandbox_config = SandboxConfig()
        sandbox = Sandbox(sandbox_config)

        return llm, sandbox

    def _save_report(self, success: bool, raw_code: str, 
                     prompt_tokens: int, completion_tokens: int) -> None:
        """Save the final execution report to a JSON file."""
        
        output_data = {
            "task_id": self.task_id,
            "completed": success,
            "code": raw_code,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens
        }
        try:
            with open(self.config.output, "w", encoding="utf-8") as f:
                json.dump(output_data, f, indent=4)
        except (OSError) as e:
            print(f"Error: Cannot save result, a problem as occured: {e}")
            
        print(f"🎉 Process finished. Output saved to {self.config.output}")

    def _run_evaluation_loop(self, llm: LLMClienrt, sandbox: Sandbox):

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
        total_prompt_tokens = 0
        total_completion_tokens = 0

        for attempt in range(1, 11):
            print(f"--- Attempt {attempt} / 10 ---")

            api_answer = llm.call_api(messages_context)
            total_prompt_tokens += api_answer.get("prompt_tokens", 0)
            total_completion_tokens += api_answer.get("completion_tokens", 0)

            raw_code = api_answer.get("text", "")
            test_code = "\n".join(self.tests)
            final_code = raw_code + "\n" + test_code

            sandbox_result_raw = sandbox.execute_code(final_code)
            success = sandbox_result_raw.get("success", False)
            log = sandbox_result_raw.get("output", "")
            if success is True:
                print("✅ Code validated successfully by the sandbox!")
                break
            else:
                print(f"❌ Attempt {attempt} failed.")
                messages_context.append({
                    "role": "user",
                    "content": f"Your code failed during execution. Here are the logs:\n{log}\nPlease fix the errors."
                })

    def solve(self):

        llm, sandbox = self._init_tools()
        (success, raw_code, prompt_tokens,
         completion_tokens) = self._run_evaluation_loop(llm, sandbox)
        self._save_report(success, raw_code, prompt_tokens, completion_tokens)
       

        

def main() -> None:
    agent = AgentMbpp()
    agent.solve()


if __name__ == "__main__":
    main()