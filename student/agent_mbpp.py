"""Autonomous agent that solves MBPP tasks.

The agent runs a Thought -> Code -> Observation loop: it asks the
LLM for a Python function, executes it against the task's test
list inside the sandbox, and feeds the observation back to the LLM
until the tests pass or a hard limit (iterations / tokens) is hit.
"""
import json
import os
import sys
import time
from datetime import datetime
from json import JSONDecodeError
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from pydantic import ValidationError

from student.agent_config import AgentConfig
from student.eval_logger import capture_stdio, write_evaluation

load_dotenv()

MAX_ATTEMPTS: int = 10
MAX_PROMPT_TOKENS: int = 6000
MAX_COMPLETION_TOKENS: int = 1500

# MBPP is a self-correcting, single-file loop with no external
# tools: the model only ever sees its own code and the sandbox
# feedback. At temperature 0.0 a model that picked a wrong
# assumption tends to regenerate the exact same wrong code on every
# retry, since nothing forces it to explore an alternative. A
# moderate temperature lets it actually diversify between attempts.
# (SWE-bench keeps 0.0: it relies on tool calls to explore the
# repository, where determinism matters more than diversity.)
MBPP_TEMPERATURE: float = 0.7

# When the model regenerates the exact same (failing) code as its
# previous attempt, nudging the temperature up for the next call
# forces it out of that local minimum instead of retrying the same
# thing forever. The bump grows with consecutive repeats and is
# capped so it never turns into pure noise.
REPEAT_TEMPERATURE_STEP: float = 0.2
REPEAT_TEMPERATURE_MAX: float = 1.3


class AgentMbpp:
    """Agent dedicated to solving MBPP tasks autonomously."""

    def __init__(self) -> None:
        """Load the configuration and the task data from disk."""
        self.config: AgentConfig = AgentConfig()
        try:
            with open(
                self.config.task_file, "r", encoding="utf8"
            ) as f:
                task_data: Dict[str, Any] = json.load(f)
        except (OSError, JSONDecodeError, TypeError) as e:
            print(
                f"Error with the task file provided: {e}\n"
                "End of program."
            )
            sys.exit(1)

        self.task_id: str = str(task_data.get("task_id", ""))
        self.prompt: str = str(task_data.get("task_definition", ""))
        self.tests: List[str] = list(task_data.get("test_list", []))
        self.function_definition: str = str(
            task_data.get("function_definition", "")
        )

        # Structured Thought -> Code response format, as required
        # by the subject (a system prompt with no reasoning slot
        # forces the model to guess blindly on math-heavy tasks
        # instead of using the Observation from a failed attempt).
        self.system_prompt: str = (
            "You are a Python expert solving MBPP coding "
            "exercises through a Thought -> Code -> Observation "
            "loop.\n\n"
            "Answer using EXACTLY this format, every turn:\n"
            "Thought: one short sentence (max ~20 words) stating "
            "your plan, or, after a failed attempt, what the "
            "Observation taught you and how you will fix it.\n"
            "```python\n"
            "<the full solution, nothing else>\n"
            "```\n\n"
            "Rules:\n"
            "- Exactly one function, matching the required "
            "signature.\n"
            "- No text after the closing fence.\n"
            "- If an attempt fails, the Observation gives the "
            "exact expected value for each failing test case. "
            "Use it: if your formula is structurally reasonable "
            "but numerically wrong, first reconsider what each "
            "parameter actually represents (its name may be "
            "ambiguous) before adding complexity to the formula.\n"
            "- Prefer the simplest hypothesis consistent with the "
            "signature before trying more complex derivations.\n\n"
            "Example:\n"
            "Thought: I will sum the list and divide by its "
            "length.\n"
            "```python\n"
            "def average(nums):\n"
            "    return sum(nums) / len(nums)\n"
            "```"
        )

        if self.config.max_iterations is not None:
            self.max_attempts: int = self.config.max_iterations
        else:
            self.max_attempts = MAX_ATTEMPTS

        self.last_output_data: Optional[Dict[str, Any]] = None

        print(f"Model : {self.config.model_name}")
        print(f"\n--- [Exercise {self.task_id}] ---")
        print(f"Prompt : {self.prompt}")
        print(f"Number of tests to validate: {len(self.tests)}")

    def _init_tools(self) -> Tuple[Any, Any]:
        """Initialize the LLM client and the sandbox.

        Returns:
            Tuple[Any, Any]: The LLM client and the sandbox
            instance used to run the agent loop.
        """
        from student.llm import LLMClient, TokenRotator
        from student.sandbox import Sandbox
        from student.sandbox_config import SandboxConfig

        template_path: str = "sandbox_template.json"

        if os.path.exists(template_path):
            try:
                with open(
                    template_path, "r", encoding="utf-8"
                ) as f:
                    config_data: Dict[str, Any] = json.load(f)
                sandbox_config: SandboxConfig = SandboxConfig(
                    **config_data
                )
                print(
                    "Sandbox loaded with custom config from "
                    f"{template_path}"
                )
            except (OSError, JSONDecodeError, ValidationError) as e:
                print(
                    f"Failed to parse {template_path} ({e}). "
                    "Using default sandbox limits."
                )
                sandbox_config = SandboxConfig()
        else:
            print(
                f"{template_path} not found. Using default "
                "sandbox limits."
            )
            sandbox_config = SandboxConfig()

        # TokenRotator reads its own provider keys directly from
        # the environment (GROQ_KEYS, OPENROUTER_KEYS, ...). There
        # is nothing to forward to it here: earlier code used to
        # read AGENT_KEY_1/2/3 / OPENAI_API_KEY into a local list
        # that was never actually passed anywhere, which was
        # misleading during debugging.
        rotator = TokenRotator()
        llm = LLMClient(
            rotator,
            self.config.provider_url,
            self.config.model_name,
            temperature=MBPP_TEMPERATURE,
        )

        sandbox: Any = Sandbox(sandbox_config)

        return llm, sandbox

    def _build_test_code(self) -> str:
        """Build the test harness appended to the LLM's solution.

        The harness never uses ``exit()``/``sys.exit()``: raising
        SystemExit inside the sandboxed child process causes it to
        terminate before it can report its captured output back to
        the parent process (see Sandbox.execute_code), which would
        silently turn every failing test into a generic "Fatal
        Sandbox Error" instead of the real assertion/traceback. All
        tests are therefore executed to completion and failures are
        tracked with a plain flag instead.

        Returns:
            str: The Python source to append to the candidate
            solution before sandboxed execution.
        """
        lines: List[str] = ["", "__all_tests_passed = True"]

        for test_assert in self.tests:
            safe = test_assert.replace("\\", "\\\\").replace(
                '"', '\\"'
            )
            f_safe = safe.replace("{", "{{").replace("}", "}}")
            lines.append("try:")
            lines.append(f"    {test_assert}")
            lines.append("except AssertionError:")
            lines.append(
                f'    print("Assertion failed: {safe}")'
            )
            lines.append("    __all_tests_passed = False")
            lines.append("except Exception as e:")
            lines.append(
                f'    print(f"Runtime Error during '
                f'[ {f_safe} ]: '
                '{type(e).__name__}: {e}")'
            )
            lines.append("    __all_tests_passed = False")

        lines.append("")
        lines.append("if __all_tests_passed:")
        lines.append("    print('ALL_TESTS_PASSED')")
        lines.append("")

        return "\n".join(lines)

    def _save_report(
        self,
        success: bool,
        raw_code: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_time: float,
        steps: List[Dict[str, Any]],
    ) -> None:
        """Write the final SolutionOutput-shaped report to disk."""
        output_data: Dict[str, Any] = {
            "task_id": str(self.task_id),
            "benchmark": "mbpp",
            "success": bool(success),
            "solution": str(raw_code) if raw_code else "",
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
                f"Process finished. Output saved to "
                f"{self.config.output}"
            )
        except OSError as e:
            print(
                "OS Error: Cannot write file to "
                f"{self.config.output}: {e}"
            )
        except TypeError as e:
            print(
                "JSON Serialization Error: Invalid data types "
                f"provided: {e}"
            )

    @staticmethod
    def _extract_code(raw_text: str) -> str:
        """Strip an optional markdown code fence from LLM output.

        Args:
            raw_text: The raw text returned by the LLM.

        Returns:
            str: The candidate Python source, stripped of any
            surrounding markdown fence and whitespace.
        """
        code = raw_text
        if "```python" in code:
            code = code.split("```python")[1].split("```")[0]
        elif "```" in code:
            code = code.split("```")[1].split("```")[0]
        return code.strip()

    @staticmethod
    def _is_same_code(code_a: str, code_b: str) -> bool:
        """Compare two candidate solutions ignoring blank lines.

        Used to detect when the model regenerated the exact same
        (failing) solution again, so the agent can react instead
        of burning iterations on an identical retry.

        Args:
            code_a: The first candidate solution.
            code_b: The second candidate solution.

        Returns:
            bool: True if both are non-empty and identical once
            blank lines and surrounding whitespace are ignored.
        """
        if not code_a or not code_b:
            return False

        def _normalize(code: str) -> str:
            return "\n".join(
                line.strip()
                for line in code.strip().splitlines()
                if line.strip()
            )

        return _normalize(code_a) == _normalize(code_b)

    def _run_evaluation_loop(
        self, llm: Any, sandbox: Any
    ) -> Tuple[bool, str, int, int, List[Dict[str, Any]]]:
        """Run the Thought -> Code -> Observation loop.

        Args:
            llm: The LLM client used to request candidate code.
            sandbox: The sandbox used to execute candidate code.

        Returns:
            Tuple containing: whether the task was solved, the
            last candidate solution, the total prompt tokens used,
            the total completion tokens used, and the per-step
            metrics.
        """
        messages_context: List[Dict[str, str]] = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": (
                    f"Problem statement:\n{self.prompt}\n\n"
                    "You MUST use this exact function "
                    f"signature:\n{self.function_definition}\n\n"
                    "Note: parameter names in the signature may "
                    "not be self-explanatory (e.g. a short name "
                    "could mean a count, a length, or a ratio) - "
                    "if your first attempt fails, use the "
                    "expected values shown in the Observation to "
                    "figure out what each parameter really means."
                ),
            },
        ]
        total_prompt_tokens: int = 0
        total_completion_tokens: int = 0
        success: bool = False
        raw_code: str = ""
        steps: List[Dict[str, Any]] = []
        # Anti-repetition tracking: if the model regenerates the
        # exact same failing code again, the next call's
        # temperature is bumped up so it is pushed out of that
        # local minimum instead of retrying forever.
        last_failed_code: str = ""
        repeat_streak: int = 0

        for attempt in range(1, self.max_attempts + 1):
            print(f"--- Attempt {attempt} / {self.max_attempts} ---")

            call_temperature: Optional[float] = None
            if repeat_streak > 0:
                call_temperature = min(
                    MBPP_TEMPERATURE
                    + REPEAT_TEMPERATURE_STEP * repeat_streak,
                    REPEAT_TEMPERATURE_MAX,
                )
                print(
                    f"Repeated identical code {repeat_streak}x - "
                    f"bumping temperature to {call_temperature:.2f}"
                )

            start_api: float = time.time()
            try:
                api_answer: Dict[str, Any] = llm.call_api(
                    messages_context,
                    temperature=call_temperature,
                )
            except RuntimeError as e:
                print(f"LLM API error: {e}")
                break
            end_api: float = time.time()
            request_time_ms: float = (end_api - start_api) * 1000

            step_input_tokens: int = api_answer.get(
                "prompt_tokens", 0
            )
            step_output_tokens: int = api_answer.get(
                "completion_tokens", 0
            )

            total_prompt_tokens += step_input_tokens
            total_completion_tokens += step_output_tokens

            if (
                total_prompt_tokens > MAX_PROMPT_TOKENS
                or total_completion_tokens > MAX_COMPLETION_TOKENS
            ):
                print("Hard limits exceeded (Tokens)! Stopping.")
                success = False
                break

            llm_output: str = api_answer.get("text", "")
            raw_code = self._extract_code(llm_output)

            test_code = self._build_test_code()
            final_code: str = raw_code + "\n" + test_code

            sandbox_result: Dict[str, Any] = sandbox.execute_code(
                final_code
            )
            log: str = str(sandbox_result.get("output", ""))
            success = "ALL_TESTS_PASSED" in log

            steps.append(
                {
                    "step": attempt,
                    "input_tokens": step_input_tokens,
                    "output_tokens": step_output_tokens,
                    "request_time_ms": request_time_ms,
                    "timestamp": datetime.now().isoformat(),
                    "api_url": self.config.provider_url,
                    "model_name": self.config.model_name,
                    "llm_output": llm_output,
                    "sandbox_input": final_code,
                    "sandbox_output": log,
                    "retries": 0,
                }
            )

            if success:
                print("Code validated successfully!")
                break

            print(f"Attempt {attempt} failed.")

            is_repeat = self._is_same_code(raw_code, last_failed_code)
            repeat_streak = repeat_streak + 1 if is_repeat else 0
            last_failed_code = raw_code

            if is_repeat:
                feedback = (
                    "Your code is IDENTICAL to your previous "
                    "failed attempt - repeating it will fail the "
                    "same way again. You MUST try a fundamentally "
                    "different approach or algorithm, not a minor "
                    f"tweak.\n\nObservation:\n{log}\n"
                    "Please fix the errors."
                )
            else:
                feedback = (
                    f"Observation:\n{log}\nPlease fix the errors."
                )

            messages_context.append(
                {"role": "assistant", "content": llm_output}
            )
            messages_context.append(
                {"role": "user", "content": feedback}
            )

        return (
            success,
            raw_code,
            total_prompt_tokens,
            total_completion_tokens,
            steps,
        )

    def solve(self) -> None:
        """Run the agent end to end and persist the report."""
        llm, sandbox = self._init_tools()

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
            success,
            raw_code,
            prompt_tokens,
            completion_tokens,
            total_time,
            steps,
        )


def main() -> None:
    """Entry point used by ``python -m agent_mbpp``."""
    with capture_stdio() as (out_tee, err_tee):
        agent: Optional[AgentMbpp] = None
        try:
            agent = AgentMbpp()
            agent.solve()
        finally:
            if agent is not None and agent.last_output_data:
                write_evaluation(
                    "mbpp",
                    agent.task_id,
                    agent.config.task_file,
                    agent.last_output_data,
                    out_tee.getvalue(),
                    err_tee.getvalue(),
                )


if __name__ == "__main__":
    main()
