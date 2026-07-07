import os
import subprocess
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("SWE-Bench Tools Server")


def get_container() -> str:
    """get the name of the container"""
    try:
        if os.path.exists(".container_id"):
            with open(".container_id", "r", encoding="utf-8") as f:
                return f.read().strip()
    except OSError:
        pass
    return "swe_sandbox"


def normalize_container_path(path: str) -> str:
    """Normalize path /testbed."""
    if path.startswith("/testbed"):
        return path
    return os.path.join("/testbed", path.lstrip("/"))


@mcp.tool()
def read_file(filepath: str, start_line: int, end_line: int) -> str:
    """Read file content with line numbers inside the container."""
    container = get_container()
    target_path = normalize_container_path(filepath)

    code = f"""
import sys
try:
    with open({repr(target_path)}, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    start = max(0, int({start_line}) - 1)
    end = min(len(lines), int({end_line}))
    for idx, line in enumerate(lines[start:end], start=start + 1):
        print(f"{{idx}}: {{line.rstrip()}}")
except Exception as e:
    print(f"Error reading file: {{e}}", file=sys.stderr)
"""
    res = subprocess.run(
        ["docker", "exec", "-i", container, "python3"],
        input=code,
        text=True,
        capture_output=True,
    )
    return res.stdout if res.returncode == 0 else res.stderr


@mcp.tool()
def edit_file(filepath: str, old_str: str, new_str: str) -> str:
    """Replace an exact string in a container file with a new string."""
    if not old_str or not old_str.strip():
        return (
            "Error: 'old_str' cannot be empty or whitespace. "
            "Modification aborted."
        )

    container = get_container()
    target_path = normalize_container_path(filepath)

    code = f"""
import sys
try:
    with open({repr(target_path)}, 'r', encoding='utf-8') as f:
        content = f.read()
    if {repr(old_str)} not in content:
        print("Error: Could not find exact 'old_str' in file", file=sys.stderr)
        sys.exit(1)
    new_content = content.replace({repr(old_str)}, {repr(new_str)})
    with open({repr(target_path)}, 'w', encoding='utf-8') as f:
        f.write(new_content)
    print("File edited successfully.")
except Exception as e:
    print(f"Error: {{e}}", file=sys.stderr)
    sys.exit(1)
"""
    res = subprocess.run(
        ["docker", "exec", "-i", container, "python3"],
        input=code,
        text=True,
        capture_output=True,
    )
    return res.stdout if res.returncode == 0 else res.stderr


@mcp.tool()
def list_files(directory: str = ".", pattern: str = "*") -> str:
    """List container files matching a given pattern."""
    container = get_container()
    target_dir = normalize_container_path(directory)

    code = f"""
import os, fnmatch
for root, dirs, files in os.walk({repr(target_dir)}):
    for file in files:
        if fnmatch.fnmatch(file, {repr(pattern)}):
            print(os.path.join(root, file))
"""
    res = subprocess.run(
        ["docker", "exec", "-i", container, "python3"],
        input=code,
        text=True,
        capture_output=True,
    )
    return (
        res.stdout
        if res.stdout.strip()
        else "No files found matching pattern."
    )


# === V.5.2 CODE SEARCH TOOLS ===

@mcp.tool()
def search_code(pattern: str, file_pattern: str = "*.py") -> str:
    """Perform a grep-like search inside the container codebase."""
    container = get_container()

    code = """
import sys
import os
import fnmatch

file_pattern = sys.argv[1]
pattern = sys.argv[2]

for root, dirs, files in os.walk('/testbed'):
    for file in files:
        full_path = os.path.join(root, file)
        rel_path = os.path.relpath(full_path, '/testbed')

        match_f = fnmatch.fnmatch(file, file_pattern)
        match_r = fnmatch.fnmatch(rel_path, file_pattern)
        if match_f or match_r:
            try:
                with open(full_path, 'r', encoding='utf-8') as f:
                    for num, line in enumerate(f, 1):
                        if pattern in line:
                            print(f"{rel_path}:{num}: {line.strip()}")
            except:
                continue
"""
    res = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            container,
            "python3",
            "-",
            file_pattern,
            pattern,
        ],
        input=code,
        text=True,
        capture_output=True,
    )
    return res.stdout if res.stdout.strip() else "No matches found."


# === V.5.3 EXECUTION TOOLS ===

@mcp.tool()
def run_tests() -> str:
    """Execute the evaluation script directly inside the container."""
    container = get_container()
    try:
        res = subprocess.run(
            ["docker", "exec", container, "bash", "/testbed/eval_script.sh"],
            capture_output=True,
            text=True,
            timeout=300,
        )

        output = f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        lines = output.split("\n")

        cleaned_lines = [
            line_str
            for line_str in lines
            if not line_str.strip().startswith("++")
        ]

        if len(cleaned_lines) > 150:
            header = cleaned_lines[:20]
            footer = cleaned_lines[-100:]
            truncated_output = (
                header
                + ["\n... [TRUNCATED LOGS: TOKENS SAVED] ...\n"]
                + footer
            )
            return "\n".join(truncated_output)

        return "\n".join(cleaned_lines)

    except subprocess.TimeoutExpired:
        return "Error: Test execution timed out after 300 seconds."


@mcp.tool()
def get_patch() -> str:
    """Retrieve the unified git diff of all changes made to the repo."""
    container = get_container()
    res = subprocess.run(
        [
            "docker",
            "exec",
            "-w",
            "/testbed",
            container,
            "git",
            "-c",
            "core.fileMode=false",
            "diff",
        ],
        capture_output=True,
        text=True,
    )
    return (
        res.stdout
        if res.stdout.strip()
        else "No changes made yet (empty diff)."
    )


@mcp.tool()
def run_command(command: str, workdir: str = ".") -> str:
    """Execute a shell command in the specified working directory."""
    container = get_container()
    target_dir = (
        workdir
        if workdir.startswith("/")
        else os.path.join("/testbed", workdir.lstrip("."))
    )

    res = subprocess.run(
        [
            "docker",
            "exec",
            "-w",
            target_dir,
            container,
            "bash",
            "-c",
            command,
        ],
        capture_output=True,
        text=True,
    )
    return (
        f"EXIT CODE: {res.returncode}\n"
        f"STDOUT:\n{res.stdout}\n"
        f"STDERR:\n{res.stderr}"
    )


if __name__ == "__main__":
    mcp.run()
