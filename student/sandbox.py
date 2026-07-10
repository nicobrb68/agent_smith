import builtins
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
    PathAccessError,
    UnauthorizedImportError,
    FinalAnswerException,
)


class Sandbox:
    """A secure sandbox environment to execute untrusted code."""

    def __init__(self, config: SandboxConfig) -> None:
        """Initialize the sandbox with a specific configuration."""
        self.config = config

    def _apply_restrictions(self) -> None:
        """Internal method to activate security JUST BEFORE execution."""
        if self.config.max_memory_mb > 0:
            limit_ram: int = (
                self.config.max_memory_mb * 1024 * 1024
            )
            try:
                resource.setrlimit(
                    resource.RLIMIT_AS,
                    (limit_ram, limit_ram),
                )
            except ValueError:
                try:
                    resource.setrlimit(
                        resource.RLIMIT_AS,
                        (limit_ram, resource.RLIM_INFINITY),
                    )
                except ValueError:
                    pass

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

        proxy_tools = {}
        for tool_name in tools_to_inject.keys():
            def make_proxy(name: str) -> Callable[..., Any]:
                def proxy_func(*args: Any, **kwargs: Any) -> Any:
                    request_queue.put((name, args, kwargs))
                    return response_queue.get()
                return proxy_func
            proxy_tools[tool_name] = make_proxy(tool_name)

        def agent_routine(tools_dict: Dict[str, Callable[..., Any]]) -> None:
            get_print = io.StringIO()
            sys.stdout = get_print
            sys.stderr = get_print

            try:
                self._apply_restrictions()

                safe_builtins = dict(sys.modules["builtins"].__dict__)

                for dangerous in ("exec", "eval", "compile",
                                  "breakpoint", "exit", "quit"):
                    safe_builtins.pop(dangerous, None)

                allowed_dirs: List[str] = list(
                    self.config.allowed_directories or []
                )
                if allowed_dirs:
                    _real_open = open

                    def safe_open(
                        file: Any, *args: Any, **kwargs: Any
                    ) -> Any:
                        path = os.path.abspath(str(file))
                        for d in allowed_dirs:
                            if path.startswith(os.path.abspath(d)):
                                break
                        else:
                            raise PathAccessError(
                                f"Access denied: {path}"
                            )
                        return _real_open(file, *args, **kwargs)

                    safe_builtins["open"] = safe_open

                execution_globals: Dict[str, Any] = (
                    {"__builtins__": safe_builtins})

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
            except KeyboardInterrupt:
                queue.put({
                    "success": False,
                    "is_final": False,
                    "output": (
                        f"{get_print.getvalue()}\n"
                        "KeyboardInterrupt: "
                        "Execution interrupted by user."
                    ),
                    "solution": "",
                })
                raise
            except SystemExit as se:
                queue.put({
                    "success": False,
                    "is_final": False,
                    "output": (
                        f"{get_print.getvalue()}\n"
                        "SystemExit: Sandboxed code "
                        f"called exit (code={se.code})."
                    ),
                    "solution": "",
                })
                raise
            except BaseException as e:
                queue.put({
                    "success": False,
                    "is_final": False,
                    "output": f"{get_print.getvalue()}\n"
                    f"{type(e).__name__}: {e}",
                    "solution": "",
                })

        ctx = multiprocessing.get_context("fork")
        process = ctx.Process(
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
                return {
                    "success": False,
                    "is_final": False,
                    "output": "Timeout Error: Execution exceeded limit.",
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
