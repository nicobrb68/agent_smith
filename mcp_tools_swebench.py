from mcp.server.fastmcp import FastMCP
import os
import subprocess
import fnmatch  # Déplacé ici pour éviter les imports dynamiques

mcp = FastMCP("SWE-Bench Tools Server")

# === V.5.1 FILE SYSTEM TOOLS ===

@mcp.tool()
def read_file(filepath: str, start_line: int, end_line: int) -> str:
    """
    Read the content of a file with line numbers, similar to cat -n.

    Args:
        filepath (str): The path to the file to read.
        start_line (int): The 1-indexed starting line number.
        end_line (int): The 1-indexed ending line number.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()
        
        start = max(0, int(start_line) - 1)
        end = min(len(lines), int(end_line))
        
        output_lines = []
        for idx, line in enumerate(lines[start:end], start=start + 1):
            output_lines.append(f"{idx}: {line.rstrip()}")
            
        return "\n".join(output_lines) if output_lines else "Empty range or file."
    except OSError as e:
        return f"Error reading file: {str(e)}"


@mcp.tool()
def edit_file(filepath: str, old_str: str, new_str: str) -> str:
    """
    Replace an exact string in a file with a new string.

    Args:
        filepath (str): The path to the file to modify.
        old_str (str): The exact block of text to replace.
        new_str (str): The new text to insert instead.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
            
        if old_str not in content:
            return f"Error: Could not find the exact 'old_str' in {filepath}. Modification aborted."
            
        new_content = content.replace(old_str, new_str)
        
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(new_content)
            
        return "File edited successfully."
    except OSError as e:
        return f"Error editing file: {str(e)}"


@mcp.tool()
def list_files(directory: str = ".", pattern: str = "*") -> str:
    """
    List files in a directory matching a given pattern.
    """
    results = []
    try:
        for root, dirs, files in os.walk(directory):
            for file in files:
                if fnmatch.fnmatch(file, pattern):
                    results.append(os.path.join(root, file))
        return "\n".join(results) if results else "No files found matching pattern."
    except Exception as e:
        return f"Error listing files: {str(e)}"


# === V.5.2 CODE SEARCH TOOLS ===

@mcp.tool()
def search_code(pattern: str, file_pattern: str = "*.py") -> str:
    """
    Perform a grep-like search in the codebase.
    Format: /absolute/path_to_file.py: <line_number> <line_content>
    """
    results = []
    for root, dirs, files in os.walk("."):
        for file in files:
            if fnmatch.fnmatch(file, file_pattern):
                full_path = os.path.join(root, file)
                abs_path = os.path.abspath(full_path)
                try:
                    with open(full_path, "r", encoding="utf-8") as f:
                        for line_num, line in enumerate(f, 1):
                            if pattern in line:
                                results.append(f"{abs_path}: {line_num} {line.strip()}")
                except OSError:
                    continue
    return "\n".join(results) if results else "No matches found."


# === V.5.3 EXECUTION TOOLS ===

@mcp.tool()
def run_tests() -> str:
    """
    Execute the evaluation test suite command for the repository.
    """
    cmd = "bin/test" if os.path.exists("bin/test") else "pytest"
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        return f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    except subprocess.TimeoutExpired:
        return "Error: Test execution timed out after 60 seconds."
    except Exception as e:
        return f"Error executing tests: {str(e)}"


@mcp.tool()
def get_patch() -> str:
    """
    Retrieve the unified git diff of all changes made to the repository.
    Strictly uses the flags demanded by the subject contract.
    """
    try:
        result = subprocess.run(
            "git -c core.fileMode=false diff",
            shell=True,
            capture_output=True,
            text=True
        )
        return result.stdout if result.stdout else "No changes made yet (empty diff)."
    except Exception as e:
        return f"Error getting patch: {str(e)}"


@mcp.tool()
def run_command(command: str, workdir: str = ".") -> str:
    """
    Execute a shell command in the specified working directory.
    Returns the command's stdout, stderr, and exit code.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=60
        )
        return f"EXIT CODE: {result.returncode}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    except subprocess.TimeoutExpired:
        return "Error: Command timed out after 60 seconds."
    except Exception as e:
        return f"Error executing command: {str(e)}"

@mcp.tool()
def run_eval_script(script_content: str, repo_dir: str) -> str:
    """
    Exécute le script d'évaluation officiel fourni par le benchmark
    au sein du dépôt pour garantir la conformité de l'environnement.
    """
    script_path = os.path.join(repo_dir, "run_eval_generated.sh")
    
    # 1. On écrit le script brut dans un fichier shell
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script_content)
        
    # 2. On rend le script exécutable
    os.chmod(script_path, 0o755)
    
    try:
        # 3. On l'exécute à l'intérieur du dossier du dépôt avec /bin/bash
        result = subprocess.run(
            ["/bin/bash", "./run_eval_generated.sh"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=120
        )
        
        # Nettoyage
        if os.path.exists(script_path):
            os.remove(script_path)
            
        return f"EXIT CODE: {result.returncode}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        
    except subprocess.TimeoutExpired:
        if os.path.exists(script_path):
            os.remove(script_path)
        return "Error: Test execution timed out after 120 seconds."
    except Exception as e:
        if os.path.exists(script_path):
            os.remove(script_path)
        return f"Error executing eval script: {str(e)}"


if __name__ == "__main__":
    mcp.run()