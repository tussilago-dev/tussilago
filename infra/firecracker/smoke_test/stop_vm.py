#!/usr/bin/env python3
# ruff: noqa: DOC201, S108, T201
"""Stop the Firecracker smoke-test VM."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

RUNTIME_DIR = Path("/tmp/tussilago-firecracker-smoke")
PID_FILE = "firecracker.pid"
STOP_TIMEOUT_SECONDS = 5.0
POLL_SECONDS = 0.1


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=RUNTIME_DIR, help=f"runtime dir (default: {RUNTIME_DIR})")
    return parser.parse_args()


def pid_exists(pid: int) -> bool:
    """Return whether a process exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def proc_cmdline(pid: int) -> str:
    """Read a Linux /proc command line."""
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def pid_file_pid(runtime_dir: Path) -> int | None:
    """Read the PID written by run_vm.py."""
    try:
        return int((runtime_dir / PID_FILE).read_text(encoding="utf-8").strip())
    except OSError, ValueError:
        return None


def matching_pids(runtime_dir: Path) -> list[int]:
    """Find smoke-test Firecracker processes."""
    config_path = str(runtime_dir / "config.json")
    pids: set[int] = set()

    pid: int | None = pid_file_pid(runtime_dir)
    if pid is not None:
        pids.add(pid)

    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdecimal():
                continue
            candidate = int(entry.name)
            cmdline: str = proc_cmdline(candidate)
            if "firecracker" in cmdline and config_path in cmdline:
                pids.add(candidate)

    return sorted(pids)


def wait_for_exit(pid: int, timeout: float) -> bool:
    """Wait until a PID exits."""
    deadline: float = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_exists(pid):
            return True
        time.sleep(POLL_SECONDS)
    return not pid_exists(pid)


def stop_pid(pid: int) -> None:
    """Terminate one process, escalating if needed."""
    if not pid_exists(pid):
        return

    print(f"Stopping Firecracker PID {pid}")
    os.kill(pid, signal.SIGTERM)
    if wait_for_exit(pid, STOP_TIMEOUT_SECONDS):
        return

    print(f"PID {pid} did not exit after SIGTERM; sending SIGKILL")
    os.kill(pid, signal.SIGKILL)


def main() -> int:
    """Stop all matching smoke-test Firecracker processes."""
    args: argparse.Namespace = parse_args()
    pids: list[int] = matching_pids(args.runtime_dir)
    if not pids:
        print("No Firecracker smoke-test VM is running.")
        return 0

    for pid in pids:
        stop_pid(pid)

    (args.runtime_dir / PID_FILE).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
