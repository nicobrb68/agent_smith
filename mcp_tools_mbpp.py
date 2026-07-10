import json
from json import JSONDecodeError
import os
import sys
from typing import Any, Dict

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError
from student.sandbox import Sandbox
from student.sandbox_config import SandboxConfig


mcp = FastMCP("MBPP Sandbox Server")


def _init_sandbox() -> Sandbox:
    """
    Initialize the Sandbox by reading the configuration file.

    Returns:
        Sandbox: A configured instance of the secure environment.
    """
    template_path: str = "sandbox_template.json"

    if os.path.exists(template_path):
        try:
            with open(template_path, "r", encoding="utf-8") as f:
                config_data: Dict[str, Any] = json.load(f)
            sandbox_config: SandboxConfig = SandboxConfig(**config_data)
            # Redirect to stderr to avoid polluting the MCP stdio channel
            print(
                f"Sandbox loaded with custom config from {template_path}",
                file=sys.stderr,
            )
        except (OSError, JSONDecodeError, ValidationError) as e:
            print(
                f"Failed to parse {template_path} ({e}). "
                "Using default sandbox limits.",
                file=sys.stderr,
            )
            sandbox_config = SandboxConfig()
    else:
        print(
            f"{template_path} not found. Using default sandbox limits.",
            file=sys.stderr,
        )
        sandbox_config = SandboxConfig()

    return Sandbox(sandbox_config)


@mcp.tool()
def execute_python_code(code: str) -> str:
    """
    Execute pure Python code safely inside the sandbox and get output back.

    Use this tool to run tests, validate algorithms, or benchmark solutions.

    Args:
        code (str): The complete and valid Python script to execute.

    Returns:
        str: The full stdout and stderr captured during execution.
    """
    sandbox = _init_sandbox()
    result: Dict[str, Any] = sandbox.execute_code(code)
    return str(result.get("output", ""))


@mcp.tool()
def run_tests(code: str, tests: str) -> str:
    """
    Run a Python solution against test assertions inside the sandbox.

    Args:
        code (str): The complete Python solution to test.
        tests (str): Newline-separated assert statements to execute
            after the solution code.

    Returns:
        str: The full stdout and stderr captured during execution,
            including 'ALL_TESTS_PASSED' on success.
    """
    test_lines = [t.strip() for t in tests.strip().splitlines() if t.strip()]
    harness_parts = [code, "", "__all_tests_passed = True"]
    for t in test_lines:
        harness_parts.append("try:")
        harness_parts.append(f"    {t}")
        harness_parts.append("except AssertionError:")
        harness_parts.append(f"    print('Assertion failed: {t}')")
        harness_parts.append("    __all_tests_passed = False")
        harness_parts.append("except Exception as e:")
        harness_parts.append(
            f"    print(f'Runtime Error during [ {t} ]: "
            "{type(e).__name__}: {e}')"
        )
        harness_parts.append("    __all_tests_passed = False")
    harness_parts.append("")
    harness_parts.append("if __all_tests_passed:")
    harness_parts.append("    print('ALL_TESTS_PASSED')")

    sandbox = _init_sandbox()
    result: Dict[str, Any] = sandbox.execute_code("\n".join(harness_parts))
    return str(result.get("output", ""))


if __name__ == "__main__":
    mcp.run()
