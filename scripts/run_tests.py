"""Deterministic test runner.

Runs ``python -m unittest`` as a subprocess with output capture, so
the result always lands in ``test.log`` regardless of how the shell
that's invoking us handles its own redirection. The previous
PowerShell-driven invocations of unittest would sometimes drop the
final ``OK (skipped=N)`` summary line, which made it impossible to
confirm the test outcome. This script captures the output, writes it
to disk with an explicit ``fsync``, and exits with a non-zero status
if the test run failed.

Usage from a shell::

    python scripts/run_tests.py                # full suite
    python scripts/run_tests.py tests.test_tasks  # one module
    python scripts/run_tests.py --list          # just list the test files
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = ROOT / "test.log"
DEFAULT_TIMEOUT = 300  # 5 minutes; the full suite is currently ~10s


def _python_executable() -> str:
    """Pick the venv python if it exists, else the current interpreter."""
    if sys.platform == "win32":
        venv_py = ROOT / ".venv" / "Scripts" / "python.exe"
    else:
        venv_py = ROOT / ".venv" / "bin" / "python"
    if venv_py.exists():
        return str(venv_py)
    return sys.executable


def list_test_files() -> int:
    """Print the discoverable test file names and exit 0."""
    tests_dir = ROOT / "tests"
    if not tests_dir.exists():
        print(f"no tests/ directory at {tests_dir}", file=sys.stderr)
        return 1
    files = sorted(p.name for p in tests_dir.glob("test_*.py"))
    for name in files:
        print(f"  {name[:-3]}")  # strip ".py"
    return 0


def run(args: list[str], log_path: Path, timeout: int) -> int:
    """Run unittest with capture_output, write the result to log_path."""
    cmd = [_python_executable(), "-u", "-m", "unittest", *args]
    print(f"$ {' '.join(cmd)}")
    print(f"  cwd: {ROOT}")
    print(f"  log: {log_path}")
    started = time.time()
    # Make sure the log file exists immediately, even if the run hangs.
    # This gives the user a signal that the runner started; the actual
    # output overwrites it at the end.
    try:
        log_path.write_text(
            f"$ {' '.join(cmd)}\nrunning... (timeout {timeout}s)\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"ERROR: cannot create {log_path}: {exc}", file=sys.stderr)
        return 2
    # On Windows, ``CREATE_NEW_PROCESS_GROUP`` puts the child in its
    # own process group so a Ctrl+C / Ctrl+Break sent to the parent
    # terminal doesn't kill the test process. The ``start /B`` and
    # ``Start-Process -NoNewWindow`` invocations from a parent
    # PowerShell session have been observed to send a kill signal to
    # the entire group when the parent shell detects the test
    # process's output is buffering; this flag prevents that.
    popen_kwargs: dict = {
        "cwd": str(ROOT),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        result = subprocess.run(cmd, timeout=timeout, **popen_kwargs)
        print(f"  subprocess returned: exit={result.returncode} "
              f"stdout_len={len(result.stdout)} stderr_len={len(result.stderr)}")
    except subprocess.TimeoutExpired as exc:
        print(f"  subprocess TIMED OUT after {timeout}s")
        # Even on timeout, write what we have so the user can see how
        # far the suite got. ``exc.stdout`` / ``exc.stderr`` are bytes
        # here because capture_output returns the raw streams.
        with open(log_path, "wb") as fh:
            fh.write(b"ERROR: test run timed out after "
                    + str(timeout).encode("ascii") + b" seconds\n")
            fh.write(b"--- stdout (truncated) ---\n")
            fh.write(exc.stdout or b"")
            fh.write(b"\n--- stderr (truncated) ---\n")
            fh.write(exc.stderr or b"")
            fh.flush()
            os.fsync(fh.fileno())
        print(f"ERROR: test run timed out after {timeout}s")
        return 124  # standard timeout exit code
    except Exception as exc:
        print(f"  subprocess FAILED: {type(exc).__name__}: {exc}")
        log_path.write_text(
            f"ERROR: subprocess.run raised {type(exc).__name__}: {exc}\n",
            encoding="utf-8",
        )
        return 3
    elapsed = time.time() - started
    combined = (
        f"$ {' '.join(cmd)}\n"
        f"exit: {result.returncode}\n"
        f"elapsed: {elapsed:.2f}s\n\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}\n"
    )
    try:
        with open(log_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(combined)
            fh.flush()
            os.fsync(fh.fileno())
        print(f"  wrote {len(combined)} bytes to {log_path}")
    except OSError as exc:
        print(f"  ERROR writing log: {type(exc).__name__}: {exc}")
        return 4
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the unittest suite with deterministic output capture."
    )
    parser.add_argument(
        "args",
        nargs="*",
        help="Arguments to pass to ``python -m unittest``. "
             "Defaults to ``discover -s tests --buffer``.",
    )
    parser.add_argument(
        "--log", type=Path, default=DEFAULT_LOG,
        help="Path to write the captured output. Default: %(default)s",
    )
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT,
        help="Maximum seconds to wait. Default: %(default)s",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List discoverable test modules and exit.",
    )
    parsed = parser.parse_args()
    if parsed.list:
        return list_test_files()
    args = parsed.args or ["discover", "-s", "tests", "--buffer"]
    return run(args, parsed.log, parsed.timeout)


if __name__ == "__main__":
    sys.exit(main())