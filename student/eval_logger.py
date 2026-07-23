"""Mirrors agent runs into the subject-mandated evaluation layout.

Subject requirement (p.36): evaluation results must be stored in
``./evaluations/EVAL_TYPE/YYYY-MM-DD_HH-MM-SS/task_id/task.json,
solution.json, stdout.log, stderr.log``. Agents already write their
report to ``--output`` (required by the CLI spec); this module adds
a best-effort mirror of that same, real data into the extra layout
without changing the existing ``--output`` behavior.

The subject (Ch. VIII) also forbids committing generated outputs,
so ``evaluations/`` must stay in ``.gitignore``.
"""
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Iterator, List, TextIO, Tuple, cast


class Tee:
    """Writes to the original stream while buffering a copy."""

    def __init__(self, original: TextIO) -> None:
        self._original = original
        self._chunks: List[str] = []

    def write(self, text: str) -> int:
        self._chunks.append(text)
        return self._original.write(text)

    def flush(self) -> None:
        self._original.flush()

    def getvalue(self) -> str:
        return "".join(self._chunks)

    def fileno(self) -> int:
        # Code that hands this stream to a subprocess (e.g. the MCP
        # stdio client's ``stderr=sys.stderr``) needs a real OS file
        # descriptor. Delegate to the original stream's fd so that
        # still works; that subprocess's raw output bypasses the
        # in-memory copy, same as it would for any piped fd.
        return self._original.fileno()

    def isatty(self) -> bool:
        return self._original.isatty()


@contextmanager
def capture_stdio() -> Iterator[Tuple[Tee, Tee]]:
    """Tee stdout/stderr so a copy can be saved to *.log files.

    Output still reaches the real terminal exactly as before; this
    only adds an in-memory copy alongside it.
    """
    out_tee = Tee(sys.stdout)
    err_tee = Tee(sys.stderr)
    prev_out: TextIO = sys.stdout
    prev_err: TextIO = sys.stderr
    sys.stdout = cast(TextIO, out_tee)
    sys.stderr = cast(TextIO, err_tee)
    try:
        yield out_tee, err_tee
    finally:
        sys.stdout = prev_out
        sys.stderr = prev_err


def write_evaluation(
    eval_type: str,
    task_id: str,
    task_file: str,
    solution_data: Dict[str, Any],
    stdout_text: str,
    stderr_text: str,
) -> None:
    """Persist task.json/solution.json/stdout.log/stderr.log.

    Best-effort: any failure here is logged as a warning and never
    raised, so it can never take down an otherwise successful run.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    safe_task_id = task_id.replace("/", "_") or "unknown_task"
    eval_dir = os.path.join(
        "evaluations", eval_type, timestamp, safe_task_id,
    )
    try:
        os.makedirs(eval_dir, exist_ok=True)

        task_content = "{}"
        if os.path.exists(task_file):
            with open(task_file, "r", encoding="utf-8") as src:
                task_content = src.read()
        with open(
            os.path.join(eval_dir, "task.json"),
            "w", encoding="utf-8",
        ) as f:
            f.write(task_content)

        with open(
            os.path.join(eval_dir, "solution.json"),
            "w", encoding="utf-8",
        ) as f:
            json.dump(solution_data, f, indent=4)

        with open(
            os.path.join(eval_dir, "stdout.log"),
            "w", encoding="utf-8",
        ) as f:
            f.write(stdout_text)

        with open(
            os.path.join(eval_dir, "stderr.log"),
            "w", encoding="utf-8",
        ) as f:
            f.write(stderr_text)

        print(f"Evaluation log written to {eval_dir}")
    except OSError as e:
        print(
            f"Warning: could not write evaluation log: {e}",
            file=sys.stderr,
        )
