"""Interactive sandbox CLI: ``uv run sandbox``.

Implements subject section V.2.1: a REPL-style command-line mode that
reads user-typed code in a loop and executes each entry inside the
sandbox namespace, subject to the same import/filesystem/timeout/
memory restrictions as everywhere else, with any connected MCP tool
wrappers and ``final_answer`` available. Exits cleanly on ``exit`` or
EOF (Ctrl+D).

Usage (see the subject for the exact invocations)::

    uv run sandbox
    uv run sandbox sandbox_template.json
    uv run sandbox --mcp-stdio "python mcp_tools_mbpp.py" sandbox_template.json
    uv run sandbox --mcp-server http://localhost:8000
    uv run sandbox --mcp-stdio "python mcp_tools_swebench.py"
"""
import argparse
import json
import sys
from typing import Any, Dict, Optional

from pydantic import ValidationError

from student.mcp_bridge import MCPBridge
from student.sandbox import Sandbox
from student.sandbox_config import SandboxConfig

PROMPT: str = ">>> "
BANNER: str = (
    "Agent Smith interactive sandbox. Type Python code, one entry per "
    "prompt. Type 'exit' or press Ctrl+D to quit."
)


def _parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    """Parse the sandbox CLI's command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="sandbox",
        description="Interactive REPL for the Agent Smith sandbox.",
    )
    parser.add_argument(
        "config_file",
        nargs="?",
        default="sandbox_template.json",
        help="Path to a SandboxConfig JSON file (default: "
        "sandbox_template.json, falls back to built-in defaults "
        "if missing).",
    )
    parser.add_argument(
        "--mcp-stdio",
        metavar="COMMAND",
        help='MCP server command to launch over stdio, e.g. '
        '"python mcp_tools_mbpp.py".',
    )
    parser.add_argument(
        "--mcp-server",
        metavar="URL",
        help="URL of an MCP server to connect to over streamable HTTP.",
    )
    return parser.parse_args(argv)


def _load_config(config_file: str) -> SandboxConfig:
    """Load a SandboxConfig from disk, or fall back to defaults."""
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            data: Dict[str, Any] = json.load(f)
        config = SandboxConfig(**data)
        print(f"Sandbox loaded with custom config from {config_file}")
        return config
    except FileNotFoundError:
        print(f"{config_file} not found. Using default sandbox limits.")
    except (json.JSONDecodeError, ValidationError) as e:
        print(f"Failed to parse {config_file} ({e}). Using defaults.")
    return SandboxConfig()


def _connect_mcp(args: argparse.Namespace) -> Optional[MCPBridge]:
    """Connect to an MCP server per the CLI flags, if any were given."""
    if args.mcp_stdio and args.mcp_server:
        print(
            "Error: --mcp-stdio and --mcp-server are mutually "
            "exclusive, pick one transport.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not args.mcp_stdio and not args.mcp_server:
        return None

    bridge = MCPBridge()
    try:
        if args.mcp_stdio:
            print(f"Connecting to MCP server (stdio): {args.mcp_stdio}")
            bridge.connect_stdio(args.mcp_stdio)
        else:
            print(f"Connecting to MCP server (http): {args.mcp_server}")
            bridge.connect_http(args.mcp_server)
    except Exception as e:
        print(f"Failed to connect to the MCP server: {e}", file=sys.stderr)
        sys.exit(1)

    tool_names = ", ".join(t["name"] for t in bridge.tools) or "(none)"
    print(f"Connected. Tools available: {tool_names}")
    return bridge


def run_repl(sandbox: Sandbox, bridge: Optional[MCPBridge]) -> None:
    """Run the interactive read-eval-print loop.

    Args:
        sandbox: The configured sandbox entries are executed in.
        bridge: The connected MCP bridge, if any (its tools are made
            available inside the sandbox namespace).
    """
    injected_tools = bridge.build_tool_proxies() if bridge else {}

    print(BANNER)
    if bridge:
        print("\n" + bridge.build_manual() + "\n")

    while True:
        try:
            entry = input(PROMPT)
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            continue

        stripped = entry.strip()
        if not stripped:
            continue
        if stripped in ("exit", "exit()", "quit", "quit()"):
            break

        result = sandbox.execute_code(entry, injected_tools=injected_tools)

        if result.get("output"):
            print(result["output"], end="" if result["output"].endswith(
                "\n") else "\n")
        if result.get("is_final"):
            print(f"[final_answer received]: {result.get('solution', '')}")
        if not result.get("success") and not result.get("output"):
            print("(no output)")


def main() -> None:
    """Entry point registered as the ``sandbox`` console script."""
    args = _parse_args()
    config = _load_config(args.config_file)
    sandbox = Sandbox(config)
    bridge = _connect_mcp(args)

    try:
        run_repl(sandbox, bridge)
    finally:
        if bridge:
            bridge.close()


if __name__ == "__main__":
    main()
