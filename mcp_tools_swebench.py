from mcp.server.fastmcp import FastMCP





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
