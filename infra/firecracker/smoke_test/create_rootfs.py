#!/usr/bin/env python3
# ruff: noqa: DOC201,DOC501,S404,S603,T201
"""Create the tiny ext4 rootfs used by the Firecracker smoke test."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

OUTPUT = Path("/srv/tussilago/images/smoke-rootfs.ext4")
DEFAULT_IMAGE = "docker.io/library/debian:bookworm-slim"
DEFAULT_SIZE_MIB = 512
MIN_SIZE_MIB = 128
GUEST_CIDR = "172.16.0.2/30"
HOST_IP = "172.16.0.1"

INIT_SCRIPT = f"""#!/bin/sh
set -eu

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
mount -t proc proc /proc 2>/dev/null || true
mount -t sysfs sysfs /sys 2>/dev/null || true
mount -t tmpfs tmpfs /run 2>/dev/null || true
mount -t tmpfs tmpfs /tmp 2>/dev/null || true

ip link set lo up 2>/dev/null || true
ip addr replace {GUEST_CIDR} dev eth0 2>/dev/null || true
ip link set eth0 up 2>/dev/null || true
ip route replace default via {HOST_IP} dev eth0 2>/dev/null || true

echo "smoke-init: Firecracker guest booted"
echo "smoke-init: eth0={GUEST_CIDR}, host={HOST_IP}"

while true; do
    if [ -c /dev/ttyS0 ]; then
        echo "smoke-init: opening /bin/sh on ttyS0"
        echo "smoke-init: exit respawns the shell; reboot or poweroff stops the VM"
        setsid /bin/sh -c 'exec /bin/sh -i </dev/ttyS0 >/dev/ttyS0 2>&1' || true
        echo "smoke-init: shell exited; respawning"
        sleep 1
    else
        sleep 3600
    fi
done
"""

REBOOT_SCRIPT = """#!/bin/sh
sync
echo b > /proc/sysrq-trigger
"""

POWEROFF_SCRIPT = """#!/bin/sh
sync
echo b > /proc/sysrq-trigger
"""


def command(name: str) -> str:
    """Return an executable path or exit with a clear error."""
    path: str | None = shutil.which(name)
    if path is None:
        msg: str = f"Missing required command: {name}"
        raise SystemExit(msg)
    return path


def run(args: list[str]) -> None:
    """Run a command, echoing it for smoke-test debuggability."""
    print("+", " ".join(args))
    subprocess.run(args, check=True)


def require_root() -> None:
    """Require root because loop mounting and root-owned files are involved."""
    if os.geteuid() != 0:
        msg = "Run with sudo: this script creates and mounts an ext4 image."
        raise SystemExit(msg)


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT, help=f"ext4 image path (default: {OUTPUT})")
    parser.add_argument(
        "--size-mib", type=int, default=DEFAULT_SIZE_MIB, help=f"image size (default: {DEFAULT_SIZE_MIB})"
    )
    parser.add_argument("--image", default=DEFAULT_IMAGE, help=f"container image to export (default: {DEFAULT_IMAGE})")
    parser.add_argument(
        "--source-dir",
        type=Path,
        help="existing rootfs directory to pack instead of creating one from the Debian container",
    )
    return parser.parse_args()


def export_container_rootfs(image: str, workdir: Path) -> Path:
    """Create a Debian container, install tiny boot helpers, and export it as tar."""
    podman: str = command("podman")
    container: str = f"tussilago-smoke-rootfs-{uuid.uuid4().hex}"
    tar_path: Path = workdir / "rootfs.tar"

    try:
        run([podman, "pull", image])
        run(
            [
                podman,
                "create",
                "--name",
                container,
                image,
                "sh",
                "-euxc",
                (
                    "export DEBIAN_FRONTEND=noninteractive; "
                    "apt-get update; "
                    "apt-get install -y --no-install-recommends iproute2 iputils-ping; "
                    "apt-get clean; "
                    "rm -rf /var/lib/apt/lists/*"
                ),
            ],
        )
        run([podman, "start", "--attach", container])
        run([podman, "export", "--output", str(tar_path), container])
    finally:
        subprocess.run([podman, "rm", "-f", container], check=False)

    return tar_path


def tar_source_dir(source_dir: Path, workdir: Path) -> Path:
    """Pack an existing rootfs directory into a tar file."""
    if not source_dir.is_dir():
        msg: str = f"Source rootfs directory does not exist: {source_dir}"
        raise SystemExit(msg)

    tar_path: Path = workdir / "rootfs.tar"
    run([command("tar"), "--create", "--file", str(tar_path), "--directory", str(source_dir), "."])
    return tar_path


def install_smoke_files(mountpoint: Path) -> None:
    """Write the simple PID 1 init and a couple of useful identity files."""
    for dirname in ("dev", "proc", "sys", "run", "tmp", "sbin", "etc", "root"):
        (mountpoint / dirname).mkdir(parents=True, exist_ok=True)

    init_path: Path = mountpoint / "sbin/smoke-init"
    init_path.write_text(INIT_SCRIPT, encoding="utf-8")
    init_path.chmod(0o755)

    reboot_path: Path = mountpoint / "sbin/reboot"
    reboot_path.write_text(REBOOT_SCRIPT, encoding="utf-8")
    reboot_path.chmod(0o755)

    poweroff_path: Path = mountpoint / "sbin/poweroff"
    poweroff_path.write_text(POWEROFF_SCRIPT, encoding="utf-8")
    poweroff_path.chmod(0o755)

    (mountpoint / "etc/hostname").write_text("smokevm\n", encoding="utf-8")
    (mountpoint / "etc/hosts").write_text(
        f"127.0.0.1 localhost\n127.0.1.1 smokevm\n{HOST_IP} host\n",
        encoding="utf-8",
    )


def sudo_owner() -> tuple[int, int] | None:
    """Return the user and group that invoked sudo, if available."""
    uid: str | None = os.environ.get("SUDO_UID")
    gid: str | None = os.environ.get("SUDO_GID")
    if uid is None or gid is None:
        return None

    return int(uid), int(gid)


def make_output_accessible(output: Path) -> None:
    """Make the rootfs writable by the user expected to run Firecracker."""
    output.chmod(0o644)

    owner: tuple[int, int] | None = sudo_owner()
    if owner is None:
        print(f"Kept {output} owned by root; run Firecracker as root or chown the image.")
        return

    os.chown(output, owner[0], owner[1])
    print(f"Set {output} owner to {owner[0]}:{owner[1]}")


def build_image(rootfs_tar: Path, output: Path, size_mib: int, workdir: Path) -> None:
    """Build the ext4 image, unpack the rootfs tar, and install smoke files."""
    if size_mib < MIN_SIZE_MIB:
        msg: str = f"--size-mib must be at least {MIN_SIZE_MIB}"
        raise SystemExit(msg)

    output.parent.mkdir(parents=True, exist_ok=True)
    image_path: Path = workdir / "smoke-rootfs.ext4"
    mountpoint: Path = workdir / "mnt"
    mountpoint.mkdir()

    mounted = False
    try:
        run([command("truncate"), "--size", f"{size_mib}M", str(image_path)])
        run([command("mkfs.ext4"), "-F", "-L", "smoke-rootfs", str(image_path)])
        run([command("mount"), "-o", "loop", str(image_path), str(mountpoint)])
        mounted = True
        run([command("tar"), "--extract", "--numeric-owner", "--file", str(rootfs_tar), "--directory", str(mountpoint)])
        install_smoke_files(mountpoint)
    finally:
        if mounted:
            run([command("umount"), str(mountpoint)])

    shutil.move(str(image_path), output)
    make_output_accessible(output)
    print(f"Wrote {output}")


def main() -> int:
    """Run the rootfs build."""
    args: argparse.Namespace = parse_args()
    require_root()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory(prefix=".smoke-rootfs-", dir=args.output.parent) as tmp:
            workdir = Path(tmp)
            rootfs_tar: Path = (
                tar_source_dir(args.source_dir, workdir)
                if args.source_dir
                else export_container_rootfs(args.image, workdir)
            )
            build_image(rootfs_tar, args.output, args.size_mib, workdir)
    except KeyboardInterrupt:
        print("\nInterrupted; existing rootfs image was left untouched.", file=sys.stderr)
        return 130
    except subprocess.CalledProcessError as exc:
        print(f"Command failed with exit code {exc.returncode}: {' '.join(exc.cmd)}", file=sys.stderr)
        print("Existing rootfs image was left untouched.", file=sys.stderr)
        return exc.returncode

    return 0


if __name__ == "__main__":
    sys.exit(main())
