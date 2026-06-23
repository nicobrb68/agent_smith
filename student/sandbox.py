import builtins
import fnmatch
import io
import multiprocessing
import resource
import socket
import sys
from typing import Any, Dict

from student.config import SandboxConfig
from student.errors import ForbiddenNetworkError, UnauthorizedImportError


class Sandbox:
    """A secure sandbox environment to execute untrusted code."""

    def __init__(self, config: SandboxConfig) -> None:
        """Initialize the sandbox with a specific configuration."""
        self.config = config

    def _apply_restrictions(self) -> None:
        """Internal method to activate security JUST BEFORE execution."""
        # Convert Mo to bytes
        limit_ram: int = self.config.max_memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit_ram, limit_ram))

        real_import = builtins.__import__

        # Block unauthorized imports
        def secure_import(
            name: str,
            globals: Any = None,
            locals: Any = None,
            fromlist: Any = (),
            level: int = 0,
        ) -> Any:
            allowed: bool = False
            for module in self.config.authorized_imports:
                if fnmatch.fnmatch(name, module):
                    allowed = True
                    break
            if allowed is False:
                raise UnauthorizedImportError(
                    f"Unauthorized import detected: {name}"
                )

            return real_import(name, globals, locals, fromlist, level)

        builtins.__import__ = secure_import

        # Disable network access
        def forbidden_socket(*args: Any, **kwargs: Any) -> Any:
            raise ForbiddenNetworkError(
                "Network access is disabled inside this sandbox."
            )

        socket.socket = forbidden_socket

    def execute_code(self, code_str: str) -> str:
        """Execute the AI code and return its output (stdout/stderr)."""
        queue: multiprocessing.Queue[Dict[str, Any]] = multiprocessing.Queue()

        def agent_routine() -> None:
            self._apply_restrictions()
            get_print = io.StringIO()
            sys.stdout = get_print
            sys.stderr = get_print
            try:
                builtins_dict = sys.modules["builtins"].__dict__
                exec(code_str, {"__builtins__": builtins_dict})
                queue.put({"success": True, "output": get_print.getvalue()})
            except Exception as e:
                queue.put(
                    {
                        "success": False,
                        "output": (
                            f"{get_print.getvalue()}\n"
                            f"{type(e).__name__}: {e}"
                        ),
                    }
                )

        # Create child process with target function
        process = multiprocessing.Process(target=agent_routine)

        # Launch child process
        process.start()
        process.join(timeout=float(self.config.max_execution_time_seconds))

        # Clean infinite loops
        if process.is_alive():
            process.terminate()
            process.join()
            return (
                "Timeout Error: Code execution exceeded the time limit"
                " or an infinite loop has been created"
            )

        # No timeout, retrieve output
        if not queue.empty():
            resultat = queue.get()
            return str(resultat["output"])
        return "Unknown Error: No output recorded"
