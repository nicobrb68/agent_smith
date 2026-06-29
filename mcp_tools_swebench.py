from mcp.server.fastmcp import FastMCP
import os
import subprocess




mcp = FastMCP("SWE-Bench Tools Server")

@mcp.tool()
def view_file(file_path : str) -> str:
    """
    Read and return the entire content of a file.

    Args:
        file_path (str): The relative path to the file to read.

    Returns:
        str: The content of the file or an error message.
    """
    try:
        with open(file_path, encoding="utf-8") as f:
            file_str: str = f.read()
    except OSError as e:
        return str(e)
    return file_str


@mcp.tool()
def save_file(file_path: str, edit_content: str) -> str:
    """
    Write or overwrite content into a specified file.

    Args:
        file_path (str): The relative path to the file to write.
        edit_content (str): The full text content to insert into the file.

    Returns:
        str: A success message or an error message if the operation fails.
    """
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(edit_content)
    except OSError as e:
        return str(e)
    return "File written successfully"


@mcp.tool()
def search_grep(pattern: str) -> str:
    """
    Search for a text pattern recursively in all files within the project.

    Args:
        pattern (str): The string or code snippet to search for.

    Returns:
        str: A formatted list of matches with file paths and line numbers.
    """
    results: list[str] = []
    for root, dirs, files in os.walk("."):
        for file in files:
            if file.endswith(".py"):
                full_path = os.path.join(root, file)
                try:
                    with open(full_path, "r", encoding="utf-8") as f:
                        for line_num, line in enumerate(f, 1):
                            if pattern in line:
                                results.append(f"{full_path}:{line_num}:{line.strip()}")
                except OSError:
                    continue
    return "\n".join(results) if results else "No matches found."


@mcp.tool()
def run_test(command: str) -> str:
    """
    Execute a test command in the project environment and return the output.

    Args:
        command (str): The full shell command to run the tests.

    Returns:
        str: The stdout and stderr output from the test execution.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60
        )
        return f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    except subprocess.TimeoutExpired:
        return "Error: Test execution timed out after 60 seconds."
    except Exception as e:
        return f"Error executing tests: {str(e)}"
