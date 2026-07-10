"""MCP tools for the SWE-bench agent.

Exposes filesystem, code-search, and execution tools that operate
inside a persistent Docker container (see get_container()) through
``docker exec``. These are plain, format-agnostic MCP tools that
the sandboxed agent code calls as regular Python functions.
"""
import os
import re
import subprocess
from typing import List

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("SWE-Bench Tools Server")

DEFAULT_CONTAINER_NAME: str = "swe_sandbox"
MAX_LOG_LINES: int = 150
HEADER_LINES: int = 20
FOOTER_LINES: int = 100

# Lines that carry an actual test verdict (pass/fail counts,
# tracebacks, assertion errors, the SWE-bench harness markers, ...)
# are always preserved when a log gets truncated, regardless of
# where they land, so truncation can never hide whether the tests
# actually passed.
_RESULT_LINE_RE = re.compile(
    r"(\bpassed\b|\bfailed\b|\berror\b|\bok\b|"
    r">>>>> (?:start|end) test output|"
    r"^ran \d+ tests?|"
    r"traceback \(most recent call last\)|"
    r"assertionerror)",
    re.IGNORECASE,
)


def get_container() -> str:
    """Return the name of the persistent target container.

    Returns:
        str: The container name stored in ``.container_id``, or
        the default sandbox name if that file is missing, empty,
        or unreadable.
    """
    try:
        if os.path.exists(".container_id"):
            with open(".container_id", "r", encoding="utf-8") as f:
                name = f.read().strip()
                if name:
                    return name
    except OSError:
        pass
    return DEFAULT_CONTAINER_NAME


def normalize_container_path(path: str) -> str:
    """Normalize an agent-provided path to live under /testbed.

    Args:
        path: A path as given by the agent, absolute or relative.

    Returns:
        str: The path rewritten so it is guaranteed to live under
        ``/testbed`` inside the container.
    """
    if path.startswith("/testbed"):
        return path
    return os.path.join("/testbed", path.lstrip("/"))


@mcp.tool()
def read_file(filepath: str, start_line: int, end_line: int) -> str:
    """Read a slice of a container file with line numbers.

    Args:
        filepath: Path to the file, relative to /testbed or
            absolute.
        start_line: First line to read (1-indexed, inclusive).
        end_line: Last line to read (1-indexed, inclusive).

    Returns:
        str: The requested lines formatted like ``cat -n``, or an
        error message on failure.
    """
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
    """Replace an exact string in a container file.

    Args:
        filepath: Path to the file, relative to /testbed or
            absolute.
        old_str: The exact substring to replace. Must be
            non-empty and non-whitespace-only.
        new_str: The replacement string.

    Returns:
        str: A success message, or an error message on failure
        (file not found, ``old_str`` not present, ...).
    """
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
        print(
            "Error: Could not find the exact 'old_str' in file.",
            file=sys.stderr,
        )
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
    """List container files matching a glob pattern.

    Args:
        directory: Directory to search, relative to /testbed or
            absolute.
        pattern: A ``fnmatch``-style glob pattern (e.g. ``*.py``).

    Returns:
        str: One matching path per line, or a "no files found"
        message.
    """
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


@mcp.tool()
def search_code(pattern: str, file_pattern: str = "*.py") -> str:
    """Search the codebase for a literal substring (grep -F style).

    NOTE: ``pattern`` is matched as a plain, case-sensitive
    substring, NOT as a regular expression. Characters such as
    ``( ) . * + ?`` are matched literally and must NOT be escaped
    (e.g. search for ``area_polygon(s, l)`` as-is, not
    ``area_polygon\\(s, l\\)``).

    Args:
        pattern: The literal substring to search for.
        file_pattern: A ``fnmatch``-style glob restricting which
            files are searched (e.g. ``*.py``).

    Returns:
        str: One ``path:line: content`` entry per match, or a
        "no matches found" message.
    """
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

        matches_file = fnmatch.fnmatch(file, file_pattern)
        matches_rel = fnmatch.fnmatch(rel_path, file_pattern)
        if matches_file or matches_rel:
            try:
                with open(full_path, 'r', encoding='utf-8') as f:
                    for num, line in enumerate(f, 1):
                        if pattern in line:
                            loc = f"{rel_path}:{num}"
                            print(f"{loc}: {line.strip()}")
            except Exception:
                continue
"""
    res = subprocess.run(
        [
            "docker", "exec", "-i", container, "python3", "-",
            file_pattern, pattern,
        ],
        input=code,
        text=True,
        capture_output=True,
    )
    return res.stdout if res.stdout.strip() else "No matches found."


def _preserve_result_lines(
    cleaned_lines: List[str], header: List[str], footer: List[str]
) -> List[str]:
    """Collect verdict lines dropped by header/footer truncation.

    Args:
        cleaned_lines: The full log, one entry per line.
        header: The lines already kept at the start of the log.
        footer: The lines already kept at the end of the log.

    Returns:
        List[str]: Formatted ``L<index>: <line>`` entries for
        every line matching ``_RESULT_LINE_RE`` that falls outside
        the kept header/footer window.
    """
    kept_start = len(header)
    kept_end = len(cleaned_lines) - len(footer)

    preserved: List[str] = []
    for index, line in enumerate(cleaned_lines):
        in_kept_window = kept_start <= index < kept_end
        if in_kept_window and _RESULT_LINE_RE.search(line):
            preserved.append(f"  L{index}: {line}")
    return preserved


@mcp.tool()
def run_tests() -> str:
    """Execute the evaluation script inside the container.

    Returns:
        str: The evaluation script's stdout/stderr. Long logs are
        truncated to a header and a footer to save tokens, but any
        line that looks like an actual test verdict (pass/fail
        counts, tracebacks, assertion errors, the SWE-bench harness
        markers, ...) is always preserved regardless of where it
        falls in the log, so truncation can never hide whether the
        tests actually passed.
    """
    container = get_container()
    try:
        res = subprocess.run(
            [
                "docker", "exec", container, "bash",
                "/testbed/eval_script.sh",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return "Error: Test execution timed out after 300 seconds."

    output = f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    lines = output.split("\n")

    # Drop conda/bash "set -x" activation noise (lines starting
    # with "++"): pure clutter, never carries a verdict.
    cleaned_lines = [
        line for line in lines if not line.strip().startswith("++")
    ]

    if len(cleaned_lines) <= MAX_LOG_LINES:
        return "\n".join(cleaned_lines)

    header = cleaned_lines[:HEADER_LINES]
    footer = cleaned_lines[-FOOTER_LINES:]
    preserved = _preserve_result_lines(cleaned_lines, header, footer)

    truncated_output: List[str] = list(header)
    truncated_output.append("")
    truncated_output.append("... [TRUNCATED LOGS: TOKENS SAVED] ...")
    if preserved:
        truncated_output.append("")
        truncated_output.append(
            "PRESERVED TEST RESULT LINES (kept despite "
            "truncation, line numbers refer to the original log):"
        )
        truncated_output.extend(preserved)
    truncated_output.append("")
    truncated_output.extend(footer)

    return "\n".join(truncated_output)


@mcp.tool()
def get_patch() -> str:
    """Retrieve the unified git diff of repository changes.

    Returns:
        str: The output of ``git diff`` for the container's
        repository, or a message indicating no changes were made.
    """
    container = get_container()
    res = subprocess.run(
        [
            "docker", "exec", "-w", "/testbed", container, "git",
            "-c", "core.fileMode=false", "diff",
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
    """Execute a shell command inside the container.

    Args:
        command: The shell command to run.
        workdir: Working directory, relative to /testbed or
            absolute.

    Returns:
        str: The command's exit code, stdout, and stderr.
    """
    container = get_container()
    target_dir = (
        workdir
        if workdir.startswith("/")
        else os.path.join("/testbed", workdir.lstrip("."))
    )

    res = subprocess.run(
        ["docker", "exec", "-w", target_dir, container, "bash",
         "-c", command],
        capture_output=True,
        text=True,
    )
    return (
        f"EXIT CODE: {res.returncode}\n"
        f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    )


@mcp.tool()
def search_function_or_class_definition_in_code(name: str) -> str:
    """Find where a function or class is defined in the codebase.

    Args:
        name: The exact function or class name to search for.

    Returns:
        str: Matching definition lines with file path and line
        number, or a "no definitions found" message.
    """
    container = get_container()

    code = f"""
import os, re
pattern = re.compile(r'^(\\s*)(def|class)\\s+{re.escape(name)}\\b')
for root, dirs, files in os.walk('/testbed'):
    for f in files:
        if not f.endswith('.py'):
            continue
        full = os.path.join(root, f)
        rel = os.path.relpath(full, '/testbed')
        try:
            with open(full, 'r', encoding='utf-8') as fh:
                for num, line in enumerate(fh, 1):
                    if pattern.match(line):
                        print(f"{{rel}}:{{num}}: {{line.rstrip()}}")
        except Exception:
            continue
"""
    res = subprocess.run(
        ["docker", "exec", "-i", container, "python3"],
        input=code,
        text=True,
        capture_output=True,
    )
    return res.stdout if res.stdout.strip() else "No definitions found."


@mcp.tool()
def find_references(name: str, filepath: str = "", line: int = 0) -> str:
    """Find all references to a symbol in the codebase.

    Args:
        name: The symbol name to search for.
        filepath: Optional file path hint (unused for now, reserved
            for future scope narrowing).
        line: Optional line number hint (unused for now).

    Returns:
        str: Matching reference lines with file path and line
        number, or a "no references found" message.
    """
    container = get_container()

    code = f"""
import os
for root, dirs, files in os.walk('/testbed'):
    for f in files:
        if not f.endswith('.py'):
            continue
        full = os.path.join(root, f)
        rel = os.path.relpath(full, '/testbed')
        try:
            with open(full, 'r', encoding='utf-8') as fh:
                for num, line in enumerate(fh, 1):
                    if {repr(name)} in line:
                        print(f"{{rel}}:{{num}}: {{line.rstrip()}}")
        except Exception:
            continue
"""
    res = subprocess.run(
        ["docker", "exec", "-i", container, "python3"],
        input=code,
        text=True,
        capture_output=True,
    )
    return res.stdout if res.stdout.strip() else "No references found."


if __name__ == "__main__":
    mcp.run()