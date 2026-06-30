import json
from json import JSONDecodeError
import os
import sys
import time
from typing import Any, Dict, List, Tuple
from datetime import datetime
import re
import asyncio

from dotenv import load_dotenv
from pydantic import ValidationError
from student.agent_config import AgentConfig
from student.sandbox import Sandbox
from student.sandbox_config import SandboxConfig
from mcp import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession

load_dotenv()


class AgentSWEBench:
    """Agent designed to solve complex SWE-bench issues autonomously."""

    def __init__(self) -> None:
        """Initialize the agent, configuration, and load task data."""
        self.config: AgentConfig = AgentConfig()
        try:
            with open(self.config.task_file, "r", encoding="utf8") as f:
                task_data: Dict[str, Any] = json.load(f)
        except (OSError, JSONDecodeError, TypeError) as e:
            print(f"Error with the task file provided : {e}\nEnd of program.")
            sys.exit(1)

        self.task_id: str = str(task_data.get("instance_id", ""))
        self.problem_statement: str = str(task_data.get("problem_statement", ""))
        self.eval_script: str = str(task_data.get("eval_script", ""))
        self.repo: str = str(task_data.get("repo", ""))

        # Extraction automatique du commit cible
        commit_match = re.search(r"git checkout ([0-9a-f]{40})", self.eval_script)
        self.base_commit: str = commit_match.group(1) if commit_match else "master"

        # Extraction de la vraie commande de test depuis l'eval_script
        self.test_command: str = "pytest"
        for line in self.eval_script.split("\n"):
            if any(cmd in line for cmd in ["bin/test", "pytest", "python -m unittest"]):
                self.test_command = line.split(" #")[0].strip()
                break

        # Limite stricte du sujet pour SWE-bench 
        self.max_iterations: int = 30

        # System prompt mis à jour pour le modèle Code-Based Tool Calling du sujet [cite: 231, 232]
        self.system_prompt: str = (
            "You are an expert Senior Software Engineer. Your goal is to solve the provided GitHub issue.\n"
            "You interact with the repository by writing standard Python code blocks calling your tools.\n\n"
            "Available functions inside your Python execution namespace:\n"
            "- read_file(filepath: str, start_line: int, end_line: int) -> str (Reads file lines with cat -n style)\n"
            "- edit_file(filepath: str, old_str: str, new_str: str) -> str (Replaces an exact text block)\n"
            "- list_files(directory: str, pattern: str) -> str (List files matching pattern)\n"
            "- search_code(pattern: str, file_pattern: str = '*.py') -> str (Grep for code across files)\n"
            "- run_tests() -> str (Executes the target test suite command)\n"
            "- get_patch() -> str (Returns the unified git diff patch of your edits)\n"
            "- run_command(command: str, workdir: str = '.') -> str (Executes custom shell command)\n"
            "- final_answer(answer_string: str) -> None (Submits your final answer patch)\n\n"
            "CRITICAL PROTOCOL:\n"
            "1. You MUST use 'search_code' or 'read_file' with tight line ranges to locate the bug first.\n"
            "2. NEVER guess a patch or use your memory without displaying the file contents in an observation first.\n"
            "3. The evaluation system checks your exploration traces. Faking steps leads to a score of 0.\n"
            "4. To execute a tool, write a valid python code block enclosed in ```python.\n"
            "5. When you are sure the bug is fixed and tests pass, call final_answer(get_patch()) inside a python code block."
        )

        print(f"Model : {self.config.model_name}")
        print(f"\n--- [SWE-bench Task {self.task_id}] ---")
        print(f"Repository : {self.repo}")

    def _init_tools(self) -> Tuple[Any, Sandbox]:
        """Initialize the LLM client and Sandbox."""
        from student.llm import LLMClient, TokenRotator

        api_keys: List[str] = []
        for i in range(1, 4):
            key = os.getenv(f"AGENT_KEY_{i}")
            if key:
                api_keys.append(key)

        if not api_keys:
            default_key = os.getenv("OPENAI_API_KEY")
            api_keys = [default_key] if default_key else ["dummy"]

        rotator: TokenRotator = TokenRotator(api_keys)
        llm: LLMClient = LLMClient(rotator, self.config.provider_url, self.config.model_name)
        
        sandbox_config = SandboxConfig()
        sandbox: Sandbox = Sandbox(sandbox_config)

        return llm, sandbox

    def _save_report(
        self,
        success: bool,
        final_patch: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_time: float,
        steps: List[Dict[str, Any]],
    ) -> None:
        """Save the final execution report matching SolutionOutput format."""
        output_data: Dict[str, Any] = {
            "task_id": str(self.task_id),
            "benchmark": "swebench",
            "success": bool(success),
            "solution": str(final_patch) if final_patch else "",
            "iterations": len(steps),
            "total_requests": len(steps),
            "total_input_tokens": int(prompt_tokens),
            "total_output_tokens": int(completion_tokens),
            "total_time_seconds": float(total_time),
            "steps": steps,
            "system_prompt": self.system_prompt,
            "error": None if success else "Agent failed to patch repository",
            "timestamp": datetime.now().isoformat(),
        }

        try:
            if os.path.dirname(self.config.output):
                os.makedirs(os.path.dirname(self.config.output), exist_ok=True)
            with open(self.config.output, "w", encoding="utf-8") as f:
                json.dump(output_data, f, indent=4)
            print(f"Process finished. Output saved to {self.config.output}")
        except OSError as e:
            print(f"OS Error: Cannot write file to {self.config.output}: {e}")

    async def _run_evaluation_loop(
        self, llm: Any, sandbox: Sandbox, mcp_client: Any, loop: Any
    ) -> Tuple[bool, str, int, int, List[Dict[str, Any]]]:
        """Run the main software engineering loop using Sandbox execution."""
        messages_context: List[Dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": f"Repository: {self.repo}\n\nIssue Description:\n{self.problem_statement}",
            },
        ]
        total_prompt_tokens: int = 0
        total_completion_tokens: int = 0
        success: bool = False
        final_patch: str = ""
        steps: List[Dict[str, Any]] = []

        # --- BRIDGE ASYNC ENTRE LE EXEC() SYNCHRONE DE LA SANDBOX ET LE CLIENT MCP ---
        def run_mcp_sync(name: str, args: dict) -> str:
            future = asyncio.run_coroutine_threadsafe(mcp_client.call_tool(name, arguments=args), loop)
            return future.result().content[0].text

        # Fonction intermédiaire pour forcer l'affichage dans stdout (capturé par la sandbox)
        def wrap_and_print(name: str, args: dict) -> str:
            res = run_mcp_sync(name, args)
            print(res)  # S'imprime DIRECTEMENT dans le flux stdout pour alimenter l'observation
            return res

        # Injection des outils conformes au sujet avec impression automatique des résultats
        injected_tools = {
            "read_file": lambda filepath, start_line, end_line: wrap_and_print(
                "read_file", {"filepath": filepath, "start_line": int(start_line), "end_line": int(end_line)}
            ),
            "edit_file": lambda filepath, old_str, new_str: wrap_and_print(
                "edit_file", {"filepath": filepath, "old_str": old_str, "new_str": new_str}
            ),
            "list_files": lambda directory=".", pattern="*": wrap_and_print(
                "list_files", {"directory": directory, "pattern": pattern}
            ),
            "search_code": lambda pattern, file_pattern="*.py": wrap_and_print(
                "search_code", {"pattern": pattern, "file_pattern": file_pattern}
            ),
            "run_tests": lambda: wrap_and_print(
                "run_command", {"command": self.test_command}
            ),
            "get_patch": lambda: wrap_and_print(
                "get_patch", {}
            ),
            "run_command": lambda command, workdir=".": wrap_and_print(
                "run_command", {"command": command, "workdir": workdir}
            ),
        }

        for attempt in range(1, self.max_iterations + 1):
            print(f"--- SWE-bench Attempt {attempt} / {self.max_iterations} ---")

            start_api: float = time.time()
            # Pas d'openai_tools passés ! Le modèle doit écrire du code brut [cite: 68]
            try:
                api_answer = llm.call_api(messages_context, tools=None)
            except RuntimeError as e:
                if "LLM_API_EXHAUSTED" in str(e):
                    print(e)
                    break  # <--- MAGIQUE : Sort gracieusement de la boucle for
                else:
                    print(e)
                    break
            end_api: float = time.time()
            request_time_ms: float = (end_api - start_api) * 1000

            step_input_tokens: int = api_answer.get("prompt_tokens", 0)
            step_output_tokens: int = api_answer.get("completion_tokens", 0)
            total_prompt_tokens += step_input_tokens
            total_completion_tokens += step_output_tokens

            # Respect des limites dures [cite: 735]
            if total_prompt_tokens > 300000 or total_completion_tokens > 10000:
                print("Hard limits exceeded (Tokens)! Stopping agent.")
                break

            llm_text = api_answer.get("text", "")
            
            # Extraction du code Python généré par l'IA [cite: 172, 206]
            code_to_run = ""
            if "```python" in llm_text:
                code_to_run = llm_text.split("```python")[1].split("```")[0]
            elif "```" in llm_text:
                code_to_run = llm_text.split("```")[1].split("```")[0]
            else:
                code_to_run = llm_text

            print("🏃 Envoi du code généré au Sandbox...")
            sandbox_res = sandbox.execute_code(code_to_run.strip(), injected_tools=injected_tools)
            
            observation = sandbox_res.get("output", "")
            is_final = sandbox_res.get("is_final", False)

            # Enregistrement du dialogue [cite: 229]
            messages_context.append({"role": "assistant", "content": llm_text})
            messages_context.append({"role": "user", "content": f"Observation from sandbox execution:\n{observation}"})

            # Format de tracking obligatoire des StepMetrics [cite: 511, 555]
            steps.append({
                "step": attempt,
                "input_tokens": step_input_tokens,
                "output_tokens": step_output_tokens,
                "request_time_ms": request_time_ms,
                "timestamp": datetime.now().isoformat(),
                "api_url": self.config.provider_url,
                "model_name": self.config.model_name,
                "llm_output": llm_text,
                "sandbox_input": code_to_run,
                "sandbox_output": observation,
                "retries": 0,
            })

            if is_final:
                final_patch = sandbox_res.get("solution", "").strip()
                success = "diff --git" in final_patch
                if success:
                    print("✅ L'IA a soumis un patch validé via final_answer() !")
                    break

        return (success, final_patch, total_prompt_tokens, total_completion_tokens, steps)

    async def solve(self) -> None:
        """Orchestrate loading, repository setup, and execution."""
        import subprocess
        llm, sandbox = self._init_tools()
        
        # --- CLONAGE ET SETUP AUTOMATIQUE DU REPO (100% Autonome) ---
        repo_dir = self.repo.split("/")[-1]
        if not os.path.exists(repo_dir):
            print(f"📥 Dépôt introuvable. Clonage automatique de [https://github.com/](https://github.com/){self.repo}.git...")
            subprocess.run(f"git clone [https://github.com/](https://github.com/){self.repo}.git", shell=True)
            
        print(f"🔄 Alignement du dépôt sur le commit : {self.base_commit}")
        subprocess.run(f"git checkout {self.base_commit}", shell=True, cwd=repo_dir)

        # Lancement du serveur MCP configuré sur le bon dossier courant (cwd) [cite: 176]
        server_params = StdioServerParameters(
            command=sys.executable,
            args=[os.path.abspath("mcp_tools_swebench.py")],
            cwd=os.path.abspath(repo_dir),
            stderr=sys.stderr
        )
        
        current_loop = asyncio.get_running_loop()
        
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                start_agent = time.time()
                (
                    success,
                    final_patch,
                    prompt_tokens,
                    completion_tokens,
                    steps,
                ) = await self._run_evaluation_loop(llm, sandbox, session, current_loop)
                end_agent = time.time()
                total_time = end_agent - start_agent

        self._save_report(success, final_patch, prompt_tokens, completion_tokens, total_time, steps)


def main() -> None:
    """Main entry point to execute the SWE-bench agent."""
    agent: AgentSWEBench = AgentSWEBench()
    asyncio.run(agent.solve())


if __name__ == "__main__":
    main()