#!/usr/bin/env python3
# ruff: noqa: DOC201,DOC501,S404,S603,T201
"""Create the TAP device used by the Firecracker smoke test."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

TAP_NAME = "tap-tuss0"
HOST_CIDR = "172.16.0.1/30"
GUEST_CIDR = "172.16.0.2/30"


def command(name: str) -> str:
    """Return an executable path or exit with a clear error."""
    path: str | None = shutil.which(name)
    if path is None:
        msg: str = f"Missing required command: {name}"
        raise SystemExit(msg)
    return path


def run(args: list[str], *, check: bool = True) -> None:
    """Run a command, echoing it for smoke-test debuggability."""
    print("+", " ".join(args))
    subprocess.run(args, check=check)


def require_root() -> None:
    """Require root because TAP setup needs CAP_NET_ADMIN."""
    if os.geteuid() != 0:
        msg = "Run with sudo: this script creates and configures a TAP device."
        raise SystemExit(msg)


def default_owner() -> str | None:
    """Return the invoking non-root user ID when run through sudo."""
    return os.environ.get("SUDO_UID")


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tap", default=TAP_NAME, help=f"TAP interface name (default: {TAP_NAME})")
    parser.add_argument("--host-cidr", default=HOST_CIDR, help=f"host address (default: {HOST_CIDR})")
    parser.add_argument(
        "--guest-cidr", default=GUEST_CIDR, help=f"guest address printed for reference (default: {GUEST_CIDR})"
    )
    parser.add_argument("--owner", default=default_owner(), help="user or uid allowed to open the TAP device")
    return parser.parse_args()


def link_exists(ip: str, tap: str) -> bool:
    """Return whether the TAP link already exists."""
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [ip, "link", "show", "dev", tap], check=False, capture_output=True, text=True
    )
    return result.returncode == 0


def create_tap(ip: str, tap: str, owner: str | None) -> None:
    """Create the TAP interface if it is missing."""
    if link_exists(ip, tap):
        print(f"{tap} already exists")
        return

    args: list[str] = [ip, "tuntap", "add", "dev", tap, "mode", "tap"]
    if owner:
        args.extend(["user", owner])
    run(args)


def configure_tap(ip: str, tap: str, host_cidr: str) -> None:
    """Assign the host address and bring the TAP interface up."""
    run([ip, "-4", "addr", "flush", "dev", tap])
    run([ip, "addr", "add", host_cidr, "dev", tap])
    run([ip, "link", "set", "dev", tap, "up"])


def main() -> int:
    """Run TAP setup."""
    args: argparse.Namespace = parse_args()
    require_root()

    ip: str = command("ip")
    create_tap(ip, args.tap, args.owner)
    configure_tap(ip, args.tap, args.host_cidr)

    print(f"Ready: host={args.host_cidr}, guest={args.guest_cidr}, tap={args.tap}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
