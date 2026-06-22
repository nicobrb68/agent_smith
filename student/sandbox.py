import sys
import os
from student.config import SandboxConfig
import resource
import builtins
import multiprocessing
import fnmatch
import socket
import io

class SandboxError(Exception):
    pass

class UnauthorizedImportError(SandboxError):
    pass

class ForbiddenNetworkError(SandboxError):
    pass

def agent_routine(generated_code: str, queue: multiprocessing.Queue):
    self._apply_restriction()
    get_print = io.StringIO()
    sys.stdout = get_print
    sys.stderr = get_print
    try:
        exec(generated_code, {"__builtins__": sys.modules['builtins'].__dict__})
        queue.put({"success": True, "output": get_print.getvalue()})
    except Exception as e:
        queue.put({"success": False, "output": f"{get_print.getvalue()}\n{type(e).__name__}: {e}"})
        
class Sandbox:
    def __init__(self, config: SandboxConfig):
        self.config = config

    def _apply_restrictions(self):
        """Méthode interne pour activer la sécurité JUSTE AVANT l'exécution."""
        # Limiter la mémoire (RAM)
        limit_ram: int = self.config.max_memory_mb * 1024 * 1024  # Convertir Mo en octets
        resource.setrlimit(resource.RLIMIT_AS, (limit_ram, 
                                                limit_ram))
        real_import = builtins.__import__
        # Bloquer les imports non autorisés
        def secure_import(name: str, globals=None, locals=None, fromlist=(), level=0):
            allowed: bool = False
            for module in self.config.authorized_imports:
                if fnmatch.fnmatch(name, module):
                    allowed = True
                    break
            if allowed is False:
                raise UnauthorizedImportError("Unauthorized import detected")

            return real_import(name, globals, locals, fromlist, level)

        builtins.__import__ = secure_import
        # interdire le réseau
        def forbidden_socket(*args, **kwargs):
            raise ForbiddenNetworkError("Network access is disabled inside this sandbox.")

        socket.socket = forbidden_socket



    def execute_code(self, code_str: str) -> str:
        """Exécute le code de l'IA et retourne ce qu'il a écrit (stdout/stderr)."""
        queue = multiprocessing.Queue()
        # cree un process enfant avec fonction a executer et args
        process = multiprocessing.Process(
            target=agent_routine,
            args=(code_str, queue)
        )
        # lancement du process enfant
        process.start()
        process.join(timeout=self.config.max_execution_time_seconds)

        # nettoyage boucle infinie
        if process.is_alive():
            process.terminate()
            process.join()
            return ("Timeout Error: Code execution exceeded the time limit"
                    " or a infinity loops as been created")

        # pas de timeout , recupere output
        if not queue.empty():
            resultat = queue.get()
            return resultat["output"]
        return "Unknow Error: No output recorded"
        
