from __future__ import annotations

import argparse
import hashlib
import logging
import os
import shutil
import subprocess  # noqa: S404
import sys
import tarfile
import tomllib
from _hashlib import HASH
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from typing import LiteralString
from typing import TypedDict

import niquests
import xmltodict  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from _hashlib import HASH

REPO = "firecracker-microvm/firecracker"
S3_BUCKET_URL = "https://s3.amazonaws.com/spec.ccfc.min"
# TODO(TheLovinator): We should mirror this bucket to our own servers.  # noqa: TD003

INSTALL_FIRECRACKER = Path("/usr/local/bin/firecracker")
INSTALL_JAILER = Path("/usr/local/bin/jailer")
INSTALL_KERNELS_DIR = Path("/srv/tussilago/kernels")
EXECUTABLE_MODE = 0o755
KERNEL_MODE = 0o644

logger: logging.Logger = logging.getLogger(__name__)
LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
SHA256_HEX_LENGTH = 64


class GitHubRelease(TypedDict):
    """Subset of the GitHub release response used by this script."""

    tag_name: str


type XmlValue = str | list[XmlValue] | dict[str, XmlValue]
type XmlDict = dict[str, XmlValue]


@dataclass(frozen=True)
class S3Artifact:
    """S3 artifact metadata used for download verification."""

    key: str
    etag: str
    size: int


@dataclass(frozen=True)
class CachedArtifacts:
    """Downloaded artifacts in the local repo cache."""

    firecracker: Path
    jailer: Path
    kernel: Path


def configure_logging(level: str) -> None:
    """Configure script logging."""
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest for a file."""
    logger.debug("Hashing %s", path)
    h: HASH = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _md5_file(path: Path) -> str:
    logger.debug("Hashing %s with MD5 for S3 ETag verification", path)
    h: HASH = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_text(url: str) -> str:
    logger.info("Downloading %s", url)
    response: niquests.Response = niquests.get(url, timeout=30)
    response.raise_for_status()
    return response.text or ""


def _parse_sha256_checksum(text: str, filename: str) -> str:
    for line in text.splitlines():
        checksum, _, name = line.strip().partition("  ")
        if name == filename and len(checksum) == SHA256_HEX_LENGTH:
            return checksum

    msg: str = f"No SHA-256 checksum for {filename}"
    raise SystemExit(msg)


def _github_release_sha256(version: str, filename: str) -> str:
    checksum_url: str = f"https://github.com/{REPO}/releases/download/{version}/{filename}.sha256.txt"
    checksum: str = _parse_sha256_checksum(_download_text(checksum_url), filename)
    logger.info("Fetched GitHub SHA-256 for %s", filename)
    return checksum


def _read_sha256_sums(path: Path) -> dict[str, str]:
    sums: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        checksum, _, name = line.strip().partition("  ")
        if checksum and name:
            sums[name.removeprefix("./")] = checksum
    return sums


def _verify_sha256(path: Path, expected: str) -> str:
    actual: str = sha256_file(path)
    if actual != expected:
        msg: str = f"SHA-256 mismatch for {path}: expected {expected}, got {actual}"
        raise SystemExit(msg)
    logger.info("Verified SHA-256 for %s", path)
    return actual


def _verify_s3_artifact(path: Path, artifact: S3Artifact) -> None:
    actual_size: int = path.stat().st_size
    if actual_size != artifact.size:
        msg: str = f"S3 size mismatch for {path}: expected {artifact.size}, got {actual_size}"
        raise SystemExit(msg)

    if "-" in artifact.etag:
        logger.warning("Skipping multipart S3 ETag verification for %s", path)
        return

    actual_etag: str = _md5_file(path)
    if actual_etag != artifact.etag:
        msg = f"S3 ETag mismatch for {path}: expected {artifact.etag}, got {actual_etag}"
        raise SystemExit(msg)
    logger.info("Verified S3 size and ETag for %s", path)


def download(url: str, dest: Path) -> None:
    """Download a URL to a destination path."""
    logger.info("Downloading %s to %s", url, dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    response: niquests.Response = niquests.get(url, timeout=30, stream=True)
    try:
        response.raise_for_status()
        with dest.open("wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
    finally:
        response.close()
    logger.info("Downloaded %s (%d bytes)", dest, dest.stat().st_size)


def github_latest_version() -> str:
    """Return the latest Firecracker GitHub release tag."""
    url: LiteralString = f"https://api.github.com/repos/{REPO}/releases/latest"
    logger.info("Fetching latest Firecracker release from GitHub")
    response: niquests.Response = niquests.get(url, timeout=30)
    response.raise_for_status()
    data: GitHubRelease = response.json()
    logger.info("Latest Firecracker release is %s", data["tag_name"])
    return data["tag_name"]


def s3_contents_list(contents: XmlValue | None) -> list[XmlValue]:
    """Return xmltodict's S3 Contents shape as a list."""
    if contents is None:
        return []
    if isinstance(contents, list):
        return contents
    return [contents]


def s3_artifacts(prefix: str) -> list[S3Artifact]:
    """Return S3 artifact metadata under a prefix."""
    logger.info("Listing S3 artifacts under %s", prefix)
    response: niquests.Response = niquests.get(
        S3_BUCKET_URL,
        params={"prefix": prefix, "list-type": "2"},
        timeout=30,
    )
    response.raise_for_status()

    data: XmlDict = xmltodict.parse(response.text or "")
    bucket: str | list[XmlValue] | dict[str, XmlValue] | None = data.get("ListBucketResult")

    contents: XmlValue | None = bucket.get("Contents") if isinstance(bucket, dict) else None
    artifacts: list[S3Artifact] = []
    for item in s3_contents_list(contents):
        if not isinstance(item, dict):
            continue
        key: str | list[XmlValue] | dict[str, XmlValue] | None = item.get("Key")
        etag: str | list[XmlValue] | dict[str, XmlValue] | None = item.get("ETag")
        size: str | list[XmlValue] | dict[str, XmlValue] | None = item.get("Size")
        if isinstance(key, str) and isinstance(etag, str) and isinstance(size, str):
            artifacts.append(
                S3Artifact(
                    key=key,
                    etag=etag.strip('"'),
                    size=int(size),
                )
            )
    logger.info("Found %d S3 artifacts under %s", len(artifacts), prefix)
    return artifacts


def s3_list(prefix: str) -> list[str]:
    """Return S3 keys under a prefix."""
    return [artifact.key for artifact in s3_artifacts(prefix)]


def is_kernel_key(key: str) -> bool:
    """Return whether an S3 key is a bootable vmlinux kernel artifact."""
    name: str = Path(key).name
    version: str = name.removeprefix("vmlinux-")
    return (
        name.startswith("vmlinux-")
        and not name.endswith(".config")
        and "no-acpi" not in name
        and all(part.isdecimal() for part in version.split("."))
    )


def version_tuple_from_vmlinux(key: str) -> tuple[int, ...]:
    """Return a comparable kernel version tuple from a vmlinux key."""
    # firecracker-ci/v1.15/x86_64/vmlinux-6.1.155
    name: str = Path(key).name
    version: str = name.removeprefix("vmlinux-")
    return tuple(int(part) for part in version.split("."))


def write_lock(path: Path, values: dict[str, str]) -> None:
    """Write Firecracker artifact metadata to the lock file."""
    logger.info("Writing lock file to %s", path)
    lines: list[str] = [f'{key} = "{value}"' for key, value in values.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Wrote %d lock entries", len(values))


def _read_lock(path: Path) -> dict[str, str]:
    if not path.is_file():
        msg: str = f"Missing lock file {path}. Run the download step before installing."
        raise SystemExit(msg)

    try:
        data: dict[str, object] = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        msg = f"Could not parse lock file {path}: {exc}"
        raise SystemExit(msg) from exc

    values: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(value, str):
            msg = f"Lock value {key} in {path} must be a string"
            raise SystemExit(msg)
        values[key] = value
    return values


def _lock_value(values: dict[str, str], key: str, lock: Path) -> str:
    try:
        return values[key]
    except KeyError as exc:
        msg = f"Lock file {lock} is missing required key {key}"
        raise SystemExit(msg) from exc


def _cached_artifacts(out: Path, lock: Path, requested_version: str, requested_arch: str) -> CachedArtifacts:
    values: dict[str, str] = _read_lock(lock)
    version: str = _lock_value(values, "firecracker_version", lock)
    arch: str = _lock_value(values, "arch", lock)

    if requested_version not in {"latest", version}:
        msg = (
            f"Lock file {lock} is for Firecracker {version}, but --version requested {requested_version}. "
            "Run the download step for the requested version first."
        )
        raise SystemExit(msg)
    if arch != requested_arch:
        msg = (
            f"Lock file {lock} is for architecture {arch}, but --arch requested {requested_arch}. "
            "Run the download step for the requested architecture first."
        )
        raise SystemExit(msg)

    release_dir: Path = out / f"release-{version}-{arch}"
    return CachedArtifacts(
        firecracker=release_dir / _lock_value(values, "firecracker_binary", lock),
        jailer=release_dir / _lock_value(values, "jailer_binary", lock),
        kernel=out / _lock_value(values, "kernel", lock),
    )


def _require_file(path: Path, description: str) -> None:
    if not path.is_file():
        msg = f"Missing {description}: {path}. Run the download step before installing."
        raise SystemExit(msg)


def _require_release_binary(path: Path, description: str) -> None:
    _require_file(path, description)
    if path.name.endswith(".debug"):
        msg = f"Refusing to install debug companion file as {description}: {path}"
        raise SystemExit(msg)


def _require_root() -> None:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        msg = (
            "Installing Firecracker artifacts requires root permissions. "
            "Re-run with sudo, for example: "
            "sudo .venv/bin/python infra/download_firecracker.py --install"
        )
        raise SystemExit(msg)


def _install_file(source: Path, dest: Path, mode: int, *, create_parent: bool = False) -> None:
    if create_parent:
        dest.parent.mkdir(parents=True, exist_ok=True)
    elif not dest.parent.is_dir():
        msg = f"Install directory {dest.parent} does not exist"
        raise SystemExit(msg)

    try:
        shutil.copyfile(source, dest)
        dest.chmod(mode)
    except PermissionError as exc:
        msg = f"Permission denied while installing {dest}. Re-run with sudo."
        raise SystemExit(msg) from exc


def _kernel_install_path(kernel: Path) -> Path:
    return INSTALL_KERNELS_DIR / kernel.name


def _install_artifacts(artifacts: CachedArtifacts) -> None:
    _require_release_binary(artifacts.firecracker, "downloaded firecracker binary")
    _require_release_binary(artifacts.jailer, "downloaded jailer binary")
    _require_file(artifacts.kernel, "downloaded kernel")

    kernel_dest: Path = _kernel_install_path(artifacts.kernel)
    _require_root()
    _install_file(artifacts.firecracker, INSTALL_FIRECRACKER, EXECUTABLE_MODE)
    _install_file(artifacts.jailer, INSTALL_JAILER, EXECUTABLE_MODE)
    _install_file(artifacts.kernel, kernel_dest, KERNEL_MODE, create_parent=True)
    logger.info("Installed Firecracker to %s", INSTALL_FIRECRACKER)
    logger.info("Installed jailer to %s", INSTALL_JAILER)
    logger.info("Installed kernel to %s", kernel_dest)


def _command_version(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, f"{path} is missing"

    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(  # noqa: S603
            [str(path), "--version"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
    except OSError as exc:
        return False, str(exc)
    except subprocess.TimeoutExpired:
        return False, "timed out"

    output: str = result.stdout.strip() or "no version output"
    if result.returncode != 0:
        return False, f"exit {result.returncode}: {output}"
    return True, output.splitlines()[0]


def _verify_installation(kernel_path: Path) -> bool:
    firecracker_ok, firecracker_detail = _command_version(INSTALL_FIRECRACKER)
    jailer_ok, jailer_detail = _command_version(INSTALL_JAILER)

    kernel_ok: bool = kernel_path.is_file()
    kernel_detail: str = f"{kernel_path} exists" if kernel_ok else f"{kernel_path} is missing"

    checks: tuple[tuple[str, bool, str], ...] = (
        ("firecracker", firecracker_ok, firecracker_detail),
        ("jailer", jailer_ok, jailer_detail),
        ("kernel", kernel_ok, kernel_detail),
    )
    success: bool = all(ok for _, ok, _ in checks)

    lines: list[str] = ["Firecracker installation verification:"]
    for name, ok, detail in checks:
        status: str = "OK" if ok else "FAIL"
        lines.append(f"{status} {name}: {detail}")
    lines.append(f"Result: {'success' if success else 'failure'}")
    sys.stdout.write("\n".join(lines) + "\n")
    return success


def main() -> None:  # noqa: PLR0914, PLR0915
    """Download, install, or verify Firecracker artifacts.

    Raises:
        SystemExit: If no kernel artifact is found.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="latest")
    parser.add_argument("--arch", default="x86_64")
    parser.add_argument("--out", default="infra/firecracker/artifacts")
    parser.add_argument("--lock", default="infra/firecracker/firecracker.lock")
    parser.add_argument("--log-level", choices=LOG_LEVELS, default="INFO")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args: argparse.Namespace = parser.parse_args()

    configure_logging(args.log_level)

    out = Path(args.out)
    lock = Path(args.lock)

    if args.install:
        logger.info("Installing Firecracker artifacts from %s", out)
        cached: CachedArtifacts = _cached_artifacts(out, lock, args.version, args.arch)
        _install_artifacts(cached)
        if args.verify:
            raise SystemExit(0 if _verify_installation(_kernel_install_path(cached.kernel)) else 1)
        return

    if args.verify:
        cached = _cached_artifacts(out, lock, args.version, args.arch)
        raise SystemExit(0 if _verify_installation(_kernel_install_path(cached.kernel)) else 1)

    logger.info("Starting Firecracker artifact download")

    version: str = github_latest_version() if args.version == "latest" else args.version
    ci_version: str = version.rsplit(".", 1)[0]
    arch: str = args.arch
    logger.info(
        "Using Firecracker %s with CI artifacts %s for %s",
        version,
        ci_version,
        arch,
    )

    logger.info("Artifact output directory is %s", out)

    # Official Firecracker release tarball.
    tgz_name: str = f"firecracker-{version}-{arch}.tgz"
    tgz_url: str = f"https://github.com/{REPO}/releases/download/{version}/{tgz_name}"
    tgz_path: Path = out / tgz_name
    expected_tgz_sha256: str = _github_release_sha256(version, tgz_name)
    download(tgz_url, tgz_path)
    tgz_sha256: str = _verify_sha256(tgz_path, expected_tgz_sha256)

    extract_dir: Path = out / f"release-{version}-{arch}"
    if extract_dir.exists():
        logger.info("Removing existing extraction directory %s", extract_dir)
        shutil.rmtree(extract_dir)

    logger.info("Extracting %s into %s", tgz_path, out)
    with tarfile.open(tgz_path, "r:gz") as tf:
        tf.extractall(out, filter="data")
    logger.info("Extracted release into %s", extract_dir)

    firecracker_bin: Path = extract_dir / f"firecracker-{version}-{arch}"
    jailer_bin: Path = extract_dir / f"jailer-{version}-{arch}"
    _require_file(firecracker_bin, "extracted firecracker binary")
    _require_file(jailer_bin, "extracted jailer binary")
    logger.info("Selected Firecracker binary %s", firecracker_bin)
    logger.info("Selected jailer binary %s", jailer_bin)
    release_sums: dict[str, str] = _read_sha256_sums(extract_dir / "SHA256SUMS")
    firecracker_bin_sha256: str = _verify_sha256(
        firecracker_bin,
        release_sums[firecracker_bin.name],
    )
    jailer_bin_sha256: str = _verify_sha256(
        jailer_bin,
        release_sums[jailer_bin.name],
    )

    # Firecracker CI artifacts.
    prefix: str = f"firecracker-ci/{ci_version}/{arch}/"
    artifacts: list[S3Artifact] = s3_artifacts(prefix)

    kernel_artifacts: list[S3Artifact] = [artifact for artifact in artifacts if is_kernel_key(artifact.key)]
    if not kernel_artifacts:
        msg: str = f"No vmlinux kernel found under s3://spec.ccfc.min/{prefix}"
        raise SystemExit(msg)

    kernel_artifact: S3Artifact = max(
        kernel_artifacts,
        key=lambda artifact: version_tuple_from_vmlinux(artifact.key),
    )
    kernel_key: str = kernel_artifact.key
    kernel_name: str = Path(kernel_key).name
    kernel_path: Path = out / kernel_name
    logger.info("Selected kernel artifact %s", kernel_key)
    download(f"{S3_BUCKET_URL}/{kernel_key}", kernel_path)
    _verify_s3_artifact(kernel_path, kernel_artifact)
    kernel_sha256: str = sha256_file(kernel_path)

    write_lock(
        path=lock,
        values={
            "firecracker_version": version,
            "ci_version": ci_version,
            "arch": arch,
            "firecracker_tgz": tgz_name,
            "firecracker_tgz_sha256": tgz_sha256,
            "firecracker_binary": firecracker_bin.name,
            "firecracker_binary_sha256": firecracker_bin_sha256,
            "jailer_binary": jailer_bin.name,
            "jailer_binary_sha256": jailer_bin_sha256,
            "kernel": kernel_name,
            "kernel_sha256": kernel_sha256,
        },
    )
    logger.info("Finished Firecracker artifact download")


if __name__ == "__main__":
    main()
