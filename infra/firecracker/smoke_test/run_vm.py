#!/usr/bin/env python3
# ruff: noqa: DOC201,DOC501,S108,S404,S603,T201
"""Boot the minimal Firecracker smoke-test VM."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import LiteralString

FIRECRACKER = Path("/usr/local/bin/firecracker")
KERNEL = Path("/srv/tussilago/kernels/vmlinux-6.1.155")
ROOTFS = Path("/srv/tussilago/images/smoke-rootfs.ext4")
RUNTIME_DIR = Path("/tmp/tussilago-firecracker-smoke")
PID_FILE = "firecracker.pid"
TAP_NAME = "tap-tuss0"
GUEST_MAC = "06:00:AC:10:00:02"
GUEST_IP = "172.16.0.2"
HOST_IP = "172.16.0.1"
NETMASK = "255.255.255.252"

BOOT_ARGS: LiteralString = (
    "console=ttyS0 reboot=k panic=1 pci=off "
    "root=/dev/vda rw rootfstype=ext4 rootwait init=/sbin/smoke-init "
    f"ip={GUEST_IP}::{HOST_IP}:{NETMASK}:smokevm:eth0:off"
)


def command(name: str) -> str:
    """Return an executable path or exit with a clear error."""
    path: str | None = shutil.which(name)
    if path is None:
        msg: str = f"Missing required command: {name}"
        raise SystemExit(msg)
    return path


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--firecracker", type=Path, default=FIRECRACKER, help=f"Firecracker binary (default: {FIRECRACKER})"
    )
    parser.add_argument("--kernel", type=Path, default=KERNEL, help=f"kernel image (default: {KERNEL})")
    parser.add_argument("--rootfs", type=Path, default=ROOTFS, help=f"rootfs image (default: {ROOTFS})")
    parser.add_argument("--tap", default=TAP_NAME, help=f"TAP interface (default: {TAP_NAME})")
    parser.add_argument("--runtime-dir", type=Path, default=RUNTIME_DIR, help=f"runtime dir (default: {RUNTIME_DIR})")
    parser.add_argument("--mem-size-mib", type=int, default=256, help="guest memory size")
    parser.add_argument("--vcpu-count", type=int, default=1, help="guest vCPU count")
    return parser.parse_args()


def require_file(path: Path, description: str) -> None:
    """Require an existing file."""
    if not path.is_file():
        msg: str = f"Missing {description}: {path}"
        raise SystemExit(msg)


def require_path(path: Path, description: str) -> None:
    """Require an existing path."""
    if not path.exists():
        msg: str = f"Missing {description}: {path}"
        raise SystemExit(msg)


def require_executable(path: Path, description: str) -> None:
    """Require an executable file."""
    require_file(path, description)
    if not os.access(path, os.X_OK):
        msg: str = f"{description} is not executable: {path}"
        raise SystemExit(msg)


def require_read_write(path: Path, description: str) -> None:
    """Require read-write access for the current user."""
    if not os.access(path, os.R_OK | os.W_OK):
        msg: str = (
            f"{description} is not readable and writable by this user: {path}\n"
            f"Fix existing image with: sudo chown $USER:$USER {path}\n"
            "Or rebuild it with: sudo python3 infra/firecracker/smoke_test/create_rootfs.py"
        )
        raise SystemExit(msg)


def require_tap(tap: str) -> None:
    """Require the TAP interface configured by setup_tap.py."""
    ip: str = command("ip")
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [ip, "link", "show", "dev", tap], check=False, capture_output=True, text=True
    )
    if result.returncode != 0:
        msg: str = f"Missing TAP interface {tap}. Run sudo python3 infra/firecracker/smoke_test/setup_tap.py"
        raise SystemExit(msg)


def firecracker_config(args: argparse.Namespace, log_path: Path, metrics_path: Path) -> dict[str, object]:
    """Return the minimal Firecracker config."""
    return {
        "boot-source": {
            "kernel_image_path": str(args.kernel),
            "boot_args": BOOT_ARGS,
        },
        "drives": [
            {
                "drive_id": "rootfs",
                "path_on_host": str(args.rootfs),
                "is_root_device": True,
                "is_read_only": False,
                "cache_type": "Unsafe",
            },
        ],
        "machine-config": {
            "vcpu_count": args.vcpu_count,
            "mem_size_mib": args.mem_size_mib,
            "smt": False,
            "track_dirty_pages": False,
        },
        "network-interfaces": [
            {
                "iface_id": "eth0",
                "host_dev_name": args.tap,
                "guest_mac": GUEST_MAC,
            },
        ],
        "logger": {
            "log_path": str(log_path),
            "level": "Info",
            "show_level": True,
            "show_log_origin": False,
        },
        "metrics": {
            "metrics_path": str(metrics_path),
        },
    }


def write_config(args: argparse.Namespace) -> Path:
    """Write the runtime Firecracker config and return its path."""
    args.runtime_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.runtime_dir / "firecracker.log"
    metrics_path = args.runtime_dir / "metrics.json"
    config_path = args.runtime_dir / "config.json"

    log_path.touch()
    metrics_path.touch()
    config_path.write_text(
        json.dumps(firecracker_config(args, log_path, metrics_path), indent=2) + "\n", encoding="utf-8"
    )
    return config_path


def run_firecracker(args: argparse.Namespace, config_path: Path) -> int:
    """Run Firecracker and keep a PID file for stop_vm.py."""
    firecracker = [
        str(args.firecracker),
        "--no-api",
        "--config-file",
        str(config_path),
    ]
    pid_path = args.runtime_dir / PID_FILE
    process = subprocess.Popen(firecracker)
    pid_path.write_text(f"{process.pid}\n", encoding="utf-8")

    try:
        return process.wait()
    except KeyboardInterrupt:
        process.send_signal(signal.SIGTERM)
        return 130
    finally:
        pid_path.unlink(missing_ok=True)


def main() -> int:
    """Write config and run Firecracker."""
    args = parse_args()
    require_executable(args.firecracker, "Firecracker binary")
    require_file(args.kernel, "kernel")
    require_file(args.rootfs, "rootfs")
    require_read_write(args.rootfs, "rootfs")
    require_path(Path("/dev/kvm"), "KVM device")
    require_tap(args.tap)

    config_path = write_config(args)
    print(f"Wrote {config_path}")
    print("Booting Firecracker. From another host terminal, stop it with:")
    print("  python3 infra/firecracker/smoke_test/stop_vm.py")

    return run_firecracker(args, config_path)


if __name__ == "__main__":
    sys.exit(main())
