import builtins
import ctypes
import fnmatch
import io
import multiprocessing
import os
import resource
import socket
import sys
import time
from queue import Empty
from typing import Any, Dict, Callable, List

from student.sandbox_config import SandboxConfig
from student.errors import (
    ForbiddenNetworkError,
    ForbiddenFileAccessError,
    UnauthorizedImportError,
    FinalAnswerException,
)

# Builtins removed from the executed namespace only (the sandbox's
# own `exec(code_str, ...)` call further down keeps using the real,
# unrestricted builtins) so LLM-generated code cannot re-implement
# `exec`/`import` from scratch and bypass the import allowlist.
_DANGEROUS_BUILTINS = ("eval", "exec", "compile", "execfile", "breakpoint")

# A single tool call is truncated past this size before being sent
# back to the LLM, to avoid blowing the token budget on one
# Observation (e.g. a runaway `list_files` on a huge repo).
_MAX_TOOL_OUTPUT_CHARS: int = 20_000

# Size of the shared buffer used to recover partial stdout/stderr
# when a run is killed on timeout. This is a POSIX shared-memory
# ctypes array (multiprocessing.Array), NOT a multiprocessing.
# Manager: a Manager's proxy talks to its server process over a
# socket, which would itself get blocked by this sandbox's own
# network restriction as soon as it tries to reconnect after fork.
_PARTIAL_OUTPUT_BUFFER_SIZE: int = 20_000


class Sandbox:
    """A secure sandbox environment to execute untrusted code."""

    def __init__(self, config: SandboxConfig) -> None:
        """Initialize the sandbox with a specific configuration."""
        self.config = config

    def _apply_restrictions(self) -> None:
        """Internal method to activate security JUST BEFORE execution."""
        limit_ram: int = self.config.max_memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit_ram, limit_ram))

        real_import = builtins.__import__

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

        def forbidden_socket(*args: Any, **kwargs: Any) -> Any:
            raise ForbiddenNetworkError(
                "Network access is disabled inside this sandbox."
            )

        socket.socket = forbidden_socket  # type: ignore[misc, assignment]

    def _restricted_builtins(self) -> Dict[str, Any]:
        """Build the ``__builtins__`` dict used by the EXECUTED code.

        Starts from a copy of the real builtins (the sandbox's own
        ``exec(code_str, ...)`` call right after this still uses the
        real, unrestricted builtins -- this is only a copy), strips
        the dangerous entries in ``_DANGEROUS_BUILTINS``, and wraps
        ``open`` so filesystem access is limited to
        ``self.config.allowed_directories``.
        """
        restricted: Dict[str, Any] = dict(sys.modules["builtins"].__dict__)
        for name in _DANGEROUS_BUILTINS:
            restricted.pop(name, None)

        real_open = restricted.get("open", open)
        allowed_dirs: List[str] = [
            os.path.abspath(d) for d in self.config.allowed_directories
        ]

        def restricted_open(file: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(file, (str, bytes, os.PathLike)):
                abs_path = os.path.abspath(os.fspath(file))
                is_allowed = any(
                    abs_path == d or abs_path.startswith(d + os.sep)
                    for d in allowed_dirs
                )
                if not is_allowed:
                    raise ForbiddenFileAccessError(
                        f"Access denied: '{file}' is outside the "
                        f"allowed directories {allowed_dirs}."
                    )
            return real_open(file, *args, **kwargs)

        restricted["open"] = restricted_open
        return restricted

    def execute_code(
        self,
        code_str: str,
        injected_tools: Dict[str, Callable] | None = None,
    ) -> Dict[str, Any]:
        """Execute the AI code and return its output along with execution flags
        """
        queue: multiprocessing.Queue[Dict[str, Any]] = multiprocessing.Queue()
        request_queue: multiprocessing.Queue[Any] = multiprocessing.Queue()
        response_queue: multiprocessing.Queue[Any] = multiprocessing.Queue()

        tools_to_inject = injected_tools or {}

        # Shared-memory buffer (see module docstring for why not a
        # Manager) so a killed-on-timeout run can still report
        # whatever it had printed so far, instead of nothing.
        partial_buffer = multiprocessing.Array(
            ctypes.c_wchar, _PARTIAL_OUTPUT_BUFFER_SIZE, lock=True
        )
        partial_buffer[:] = ["\x00"] * _PARTIAL_OUTPUT_BUFFER_SIZE

        proxy_tools = {}
        for tool_name in tools_to_inject.keys():
            def make_proxy(name: str) -> Callable[..., Any]:
                def proxy_func(*args: Any, **kwargs: Any) -> Any:
                    request_queue.put((name, args, kwargs))
                    return response_queue.get()
                return proxy_func
            proxy_tools[tool_name] = make_proxy(tool_name)

        def agent_routine(tools_dict: Dict[str, Callable[..., Any]]) -> None:
            class _SharedWriter(io.StringIO):
                """Mirrors every write to ``partial_buffer`` so the
                parent can recover partial output if this process
                gets killed on timeout."""

                def write(self, s: str) -> int:
                    n = super().write(s)
                    try:
                        tail = self.getvalue()[-_PARTIAL_OUTPUT_BUFFER_SIZE:]
                        with partial_buffer.get_lock():
                            partial_buffer[:len(tail)] = list(tail)
                            remaining = (
                                _PARTIAL_OUTPUT_BUFFER_SIZE - len(tail)
                            )
                            if remaining > 0:
                                partial_buffer[len(tail):] = (
                                    ["\x00"] * remaining
                                )
                    except Exception:
                        pass
                    return n

            get_print = _SharedWriter()
            sys.stdout = get_print
            sys.stderr = get_print

            try:
                self._apply_restrictions()

                execution_globals: Dict[str, Any] = (
                    {"__builtins__": self._restricted_builtins()})

                for tool_name, tool_proxy in tools_dict.items():
                    execution_globals[tool_name] = tool_proxy

                def final_answer(answer_string: str) -> None:
                    raise FinalAnswerException(str(answer_string))

                execution_globals["final_answer"] = final_answer

                exec(code_str, execution_globals)
                queue.put({
                    "success": True,
                    "is_final": False,
                    "output": get_print.getvalue(),
                    "solution": "",
                })
            except FinalAnswerException as fae:
                queue.put({
                    "success": True,
                    "is_final": True,
                    "output": get_print.getvalue(),
                    "solution": fae.answer,
                })
            except SyntaxError as e:
                queue.put({
                    "success": False,
                    "is_final": False,
                    "output": f"{get_print.getvalue()}\n"
                    f"SyntaxError: the submitted code block is not "
                    f"valid Python and could not be executed: {e}",
                    "solution": "",
                })
            except BaseException as e:
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise e
                queue.put({
                    "success": False,
                    "is_final": False,
                    "output": f"{get_print.getvalue()}\n"
                    f"{type(e).__name__}: {e}",
                    "solution": "",
                })

        process = multiprocessing.Process(
            target=agent_routine, args=(proxy_tools,)
        )
        process.start()

        start_time = time.time()
        timeout = float(self.config.max_execution_time_seconds)
        final_result = None

        while process.is_alive():
            elapsed = time.time() - start_time
            if elapsed > timeout:
                process.terminate()
                process.join()
                with partial_buffer.get_lock():
                    partial_output = "".join(
                        partial_buffer[:]
                    ).split("\x00", 1)[0]
                return {
                    "success": False,
                    "is_final": False,
                    "output": (
                        "Timeout Error: execution exceeded the "
                        f"{timeout:.0f}s limit and was killed. The "
                        "output below is PARTIAL (whatever was "
                        f"printed before the kill):\n{partial_output}"
                    ),
                    "solution": "",
                }

            try:
                req = request_queue.get(timeout=0.1)
                t_name, t_args, t_kwargs = req

                if t_name in tools_to_inject:
                    try:
                        res = tools_to_inject[t_name](*t_args, **t_kwargs)
                    except Exception as err:
                        res = f"Error executing tool {t_name}: {str(err)}"
                else:
                    res = f"Error: Tool {t_name} not found."

                if isinstance(res, str) and len(res) > _MAX_TOOL_OUTPUT_CHARS:
                    res = (
                        res[:_MAX_TOOL_OUTPUT_CHARS]
                        + "\n... [TRUNCATED: tool output exceeded "
                        f"{_MAX_TOOL_OUTPUT_CHARS} characters and was "
                        "cut here to save tokens]"
                    )

                response_queue.put(res)
            except Empty:
                continue

        try:
            final_result = queue.get(timeout=1.0)
        except Empty:
            final_result = None

        process.join()

        if final_result:
            return final_result

        return {
            "success": False,
            "is_final": False,
            "output": (
                "Fatal Sandbox Error: Child process terminated without "
                "returning metrics."
            ),
            "solution": "",
        }
