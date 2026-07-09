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

from student.agent_config import AgentConfig

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
        self.test_imports: List[str] = list(
            task_data.get("test_imports", [])
        )
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

        print(f"Model : {self.config.model_name}")
        print(f"\n--- [Exercise {self.task_id}] ---")
        print(f"Prompt : {self.prompt}")
        print(f"Number of tests to validate: {len(self.tests)}")

    def _init_tools(self) -> Tuple[Any, Any]:
        """Initialize the LLM client and connect to the MBPP MCP server.

        Returns:
            Tuple[Any, Any]: The LLM client and a connected
            ``MCPBridge`` exposing the MBPP tools (``run_tests``,
            ``execute_python_code``) from mcp_tools_mbpp.py. That
            server owns and configures its own ``Sandbox`` from
            ``sandbox_template.json`` (see its ``_init_sandbox``) --
            the sandbox boundary is enforced there.
        """
        from student.llm import LLMClient, TokenRotator
        from student.mcp_bridge import MCPBridge

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

        bridge = MCPBridge()
        bridge.connect_stdio(f"{sys.executable} mcp_tools_mbpp.py")

        return llm, bridge

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
        lines: List[str] = list(self.test_imports) + [
            "", "__all_tests_passed = True"
        ]

        for test_assert in self.tests:
            lines.append("try:")
            lines.append(f"    {test_assert}")
            lines.append("except AssertionError:")
            lines.append(
                f"    print('Assertion failed: {test_assert}')"
            )
            lines.append("    __all_tests_passed = False")
            lines.append("except Exception as e:")
            lines.append(
                f"    print(f'Runtime Error during [ {test_assert} ]: "
                "{type(e).__name__}: {e}')"
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
        """Extract the candidate Python source from raw LLM output.

        Delegates to ``student.code_extraction`` so the agent also
        accepts XML ``<invoke>``, JSON ``<tool_call>``, and ReAct
        tool-call formats besides fenced ```python blocks (subject
        section V.1.2), instead of only stripping a markdown fence.

        Args:
            raw_text: The raw text returned by the LLM.

        Returns:
            str: The candidate Python source, or ``""`` if nothing
            usable was found in ``raw_text``.
        """
        from student.code_extraction import extract_code

        return extract_code(raw_text).code

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
        self, llm: Any, bridge: Any
    ) -> Tuple[bool, str, int, int, List[Dict[str, Any]]]:
        """Run the Thought -> Code -> Observation loop.

        Args:
            llm: The LLM client used to request candidate code.
            bridge: The connected ``MCPBridge`` used to validate
                each candidate through the MBPP server's
                ``run_tests`` tool.

        Returns:
            Tuple containing: whether the task was solved, the
            last candidate solution, the total prompt tokens used,
            the total completion tokens used, and the per-step
            metrics.
        """
        test_imports_note = (
            f"\n\nThe test assertions run in the SAME namespace as "
            f"your code and rely on these imports:\n"
            f"{chr(10).join(self.test_imports)}\n"
            "(already guaranteed to be present, no need to "
            "re-import them, but don't shadow these names)."
            if self.test_imports
            else (
                "\n\nNo test_imports were provided for this task: "
                "if a test assertion uses a module (e.g. "
                "math.isclose(...), cmath.phase(...)), YOUR code "
                "must import it yourself, since the assertions run "
                "in the same namespace as your code."
            )
        )
        messages_context: List[Dict[str, str]] = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": (
                    f"Problem statement:\n{self.prompt}\n\n"
                    "You MUST use this exact function "
                    f"signature:\n{self.function_definition}"
                    f"{test_imports_note}\n\n"
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

        for attempt in range(1, MAX_ATTEMPTS + 1):
            print(f"--- Attempt {attempt} / {MAX_ATTEMPTS} ---")

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
            api_answer: Dict[str, Any] = llm.call_api(
                messages_context, temperature=call_temperature
            )
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

            try:
                log = str(
                    bridge.call_tool("run_tests", {"code": final_code})
                )
            except Exception as e:
                log = (
                    "MCP Error: failed to call the run_tests tool "
                    f"({type(e).__name__}: {e})."
                )
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
        llm, bridge = self._init_tools()

        try:
            start_agent: float = time.time()
            (
                success,
                raw_code,
                prompt_tokens,
                completion_tokens,
                steps,
            ) = self._run_evaluation_loop(llm, bridge)
            end_agent: float = time.time()
            total_time: float = end_agent - start_agent
        finally:
            bridge.close()

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
    agent: AgentMbpp = AgentMbpp()
    agent.solve()


if __name__ == "__main__":
    main()