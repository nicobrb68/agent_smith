import json
from json import JSONDecodeError
import os
import sys
from typing import Any, Dict

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError
from student.sandbox import Sandbox
from student.sandbox_config import SandboxConfig

# 1. Initialize the FastMCP server at the global module level
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


# 2. Declare the tool at the global level for FastMCP inspection
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


# 3. Main entry point to run the MCP server over standard I/O
if __name__ == "__main__":
    mcp.run()