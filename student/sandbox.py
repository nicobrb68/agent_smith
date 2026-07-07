import builtins
import fnmatch
import io
import multiprocessing
import resource
import socket
import sys
import time
from queue import Empty
from typing import Any, Dict, Callable

from student.sandbox_config import SandboxConfig
from student.errors import ForbiddenNetworkError, UnauthorizedImportError, FinalAnswerException


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

        socket.socket = forbidden_socket

    def execute_code(self, code_str: str, injected_tools: Dict[str, Callable] | None = None) -> Dict[str, Any]:
        """
        Execute the AI code and return its output along with execution flags.
        """
        queue: multiprocessing.Queue[Dict[str, Any]] = multiprocessing.Queue()
        request_queue = multiprocessing.Queue()
        response_queue = multiprocessing.Queue()
        
        tools_to_inject = injected_tools or {}

        # 1. Génération dynamique de proxies légers
        proxy_tools = {}
        for tool_name in tools_to_inject.keys():
            def make_proxy(name):
                def proxy_func(*args, **kwargs):
                    request_queue.put((name, args, kwargs))
                    return response_queue.get()
                return proxy_func
            proxy_tools[tool_name] = make_proxy(tool_name)

        def agent_routine(tools_dict) -> None:
            # Capturer TOUT le flux d'impression dès le départ
            get_print = io.StringIO()
            sys.stdout = get_print
            sys.stderr = get_print
            
            try:
                # Placer les restrictions à l'intérieur du try pour intercepter les MemoryErrors d'initialisation
                self._apply_restrictions()
                
                builtins_dict = sys.modules["builtins"].__dict__
                execution_globals = {"__builtins__": builtins_dict}
                
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
                    "solution": ""
                })
            except FinalAnswerException as fae:
                queue.put({
                    "success": True, 
                    "is_final": True, 
                    "output": get_print.getvalue(),
                    "solution": fae.answer
                })
            except BaseException as e:
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise e
                # Remonter l'erreur système exacte (ex: MemoryError) au parent
                queue.put({
                    "success": False,
                    "is_final": False,
                    "output": f"{get_print.getvalue()}\n{type(e).__name__}: {e}",
                    "solution": ""
                })

        # 2. Lancement de la Sandbox isolée
        process = multiprocessing.Process(target=agent_routine, args=(proxy_tools,))
        process.start()

        start_time = time.time()
        timeout = float(self.config.max_execution_time_seconds)
        final_result = None
        
        # 3. Boucle d'écoute active du Parent (Orchestrator)
        while process.is_alive():
            elapsed = time.time() - start_time
            if elapsed > timeout:
                process.terminate()
                process.join()
                return {
                    "success": False,
                    "is_final": False,
                    "output": "Timeout Error: Execution exceeded limit.",
                    "solution": ""
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

        # 4. Lecture sécurisée des données de la Queue avec timeout explicite (sans passer par .empty())
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
            "output": "Fatal Sandbox Error: Child process terminated without returning metrics.",
            "solution": ""
        }