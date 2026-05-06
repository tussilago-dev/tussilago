from __future__ import annotations

import argparse
import hashlib
import logging
import shutil
import tarfile
from _hashlib import HASH
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from typing import LiteralString
from typing import TypedDict

import niquests
import xmltodict

if TYPE_CHECKING:
    from _hashlib import HASH

REPO = "firecracker-microvm/firecracker"
S3_BUCKET_URL = "https://s3.amazonaws.com/spec.ccfc.min"
# TODO(TheLovinator): We should mirror this bucket to our own servers.  # noqa: TD003

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


def main() -> None:  # noqa: PLR0914, PLR0915
    """Download Firecracker artifacts and write their lock metadata.

    Raises:
        SystemExit: If no kernel artifact is found.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="latest")
    parser.add_argument("--arch", default="x86_64")
    parser.add_argument("--out", default="infra/firecracker/artifacts")
    parser.add_argument("--lock", default="infra/firecracker/firecracker.lock")
    parser.add_argument("--log-level", choices=LOG_LEVELS, default="INFO")
    args: argparse.Namespace = parser.parse_args()

    configure_logging(args.log_level)
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

    out = Path(args.out)
    lock = Path(args.lock)
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

    firecracker_bin: Path = next(extract_dir.glob("firecracker-*"))
    jailer_bin: Path = next(extract_dir.glob("jailer-*"))
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
