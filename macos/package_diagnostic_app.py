#!/usr/bin/env python3
"""Build a relocatable, ad-hoc signed macOS diagnostic package from an existing SDR++ .app.

The script copies non-system Mach-O dependencies into Contents/Frameworks,
rewrites load commands, signs the application, writes a small diagnostic
launcher and creates a ZIP plus SHA-256 checksum.

The SDRplay API service must still be installed on every destination Mac.
Use --bundle-sdrplay only for private copies when you also want the local
SDRplay client library embedded in the application.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
from pathlib import Path

SYSTEM_PREFIXES = ("/System/Library/", "/usr/lib/")
SYSTEM_FRAMEWORKS = {
    "Accelerate": "/System/Library/Frameworks/Accelerate.framework/Versions/A/Accelerate",
    "AppKit": "/System/Library/Frameworks/AppKit.framework/Versions/C/AppKit",
    "AudioToolbox": "/System/Library/Frameworks/AudioToolbox.framework/Versions/A/AudioToolbox",
    "AudioUnit": "/System/Library/Frameworks/AudioUnit.framework/Versions/A/AudioUnit",
    "CFNetwork": "/System/Library/Frameworks/CFNetwork.framework/Versions/A/CFNetwork",
    "Cocoa": "/System/Library/Frameworks/Cocoa.framework/Versions/A/Cocoa",
    "CoreAudio": "/System/Library/Frameworks/CoreAudio.framework/Versions/A/CoreAudio",
    "CoreFoundation": "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation",
    "CoreGraphics": "/System/Library/Frameworks/CoreGraphics.framework/Versions/A/CoreGraphics",
    "CoreServices": "/System/Library/Frameworks/CoreServices.framework/Versions/A/CoreServices",
    "CoreVideo": "/System/Library/Frameworks/CoreVideo.framework/Versions/A/CoreVideo",
    "Foundation": "/System/Library/Frameworks/Foundation.framework/Versions/C/Foundation",
    "IOKit": "/System/Library/Frameworks/IOKit.framework/Versions/A/IOKit",
    "Metal": "/System/Library/Frameworks/Metal.framework/Versions/A/Metal",
    "OpenGL": "/System/Library/Frameworks/OpenGL.framework/Versions/A/OpenGL",
    "QuartzCore": "/System/Library/Frameworks/QuartzCore.framework/Versions/A/QuartzCore",
    "Security": "/System/Library/Frameworks/Security.framework/Versions/A/Security",
    "SystemConfiguration": "/System/Library/Frameworks/SystemConfiguration.framework/Versions/A/SystemConfiguration",
}
SEARCH_ROOTS = (
    Path("/opt/homebrew/lib"),
    Path("/usr/local/lib"),
    Path("/Library/Frameworks"),
    Path("/Library/SDRplayAPI/3.15.1/lib"),
)
SDRPLAY_EXTERNAL_RPATHS = (
    "/usr/local/lib",
    "/Library/SDRplayAPI/3.15.1/lib",
)


def run(*args: str | os.PathLike[str], capture: bool = False, check: bool = True) -> str:
    cmd = [os.fspath(arg) for arg in args]
    proc = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"{proc.stdout or ''}{proc.stderr or ''}"
        )
    return proc.stdout or ""


def require_tools() -> None:
    for name in ("codesign", "ditto", "file", "install_name_tool", "lipo", "otool", "xattr"):
        if shutil.which(name) is None:
            raise RuntimeError(f"Required tool not found: {name}")


def is_macho(path: Path) -> bool:
    if not path.is_file():
        return False
    return "Mach-O" in run("file", path, capture=True, check=False)


def macho_files(app: Path) -> list[Path]:
    result: list[Path] = []
    for relative in ("Contents/MacOS", "Contents/Frameworks", "Contents/Plugins"):
        root = app / relative
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if is_macho(path):
                result.append(path)
    return result


def dependencies(binary: Path) -> list[str]:
    output = run("otool", "-L", binary, capture=True)
    result: list[str] = []
    for line in output.splitlines()[1:]:
        stripped = line.strip()
        if stripped:
            result.append(stripped.split(" (compatibility version", 1)[0])
    return result


def rpaths(binary: Path) -> list[str]:
    output = run("otool", "-l", binary, capture=True)
    lines = output.splitlines()
    result: list[str] = []
    for index, line in enumerate(lines):
        if line.strip() != "cmd LC_RPATH":
            continue
        for candidate in lines[index + 1 : index + 7]:
            candidate = candidate.strip()
            if candidate.startswith("path "):
                result.append(candidate[5:].split(" (offset", 1)[0])
                break
    return result


def expand_special(value: str, binary: Path, app: Path) -> Path:
    executable_dir = app / "Contents/MacOS"
    if value.startswith("@loader_path"):
        suffix = value[len("@loader_path") :].lstrip("/")
        return (binary.parent / suffix).resolve()
    if value.startswith("@executable_path"):
        suffix = value[len("@executable_path") :].lstrip("/")
        return (executable_dir / suffix).resolve()
    return Path(value).expanduser().resolve()


def resolve_dependency(dep: str, binary: Path, app: Path) -> Path | None:
    if dep.startswith("@loader_path") or dep.startswith("@executable_path"):
        candidate = expand_special(dep, binary, app)
        return candidate if candidate.is_file() else None

    if dep.startswith("@rpath/"):
        suffix = dep[len("@rpath/") :]
        for value in rpaths(binary):
            candidate = expand_special(value, binary, app) / suffix
            if candidate.is_file():
                return candidate.resolve()
        for root in (app / "Contents/Frameworks", *SEARCH_ROOTS):
            candidate = root / suffix
            if candidate.is_file():
                return candidate.resolve()
        return None

    candidate = Path(dep)
    return candidate.resolve() if candidate.is_file() else None


def is_system_dependency(dep: str) -> bool:
    return dep.startswith(SYSTEM_PREFIXES)


def is_sdrplay_dependency(dep: str) -> bool:
    return Path(dep).name.startswith("libsdrplay_api")


def ensure_writable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IWUSR)


def remove_signature(path: Path) -> None:
    run("codesign", "--remove-signature", path, check=False)


def add_rpath(binary: Path, value: str) -> None:
    if value not in rpaths(binary):
        run("install_name_tool", "-add_rpath", value, binary)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_framework(dep: str, binary: Path) -> bool:
    system_path = SYSTEM_FRAMEWORKS.get(Path(dep).name)
    if system_path is None:
        return False
    if dep != system_path:
        run("install_name_tool", "-change", dep, system_path, binary)
    return True


def bundle_dependencies(app: Path, bundle_sdrplay: bool) -> None:
    frameworks = app / "Contents/Frameworks"
    frameworks.mkdir(parents=True, exist_ok=True)

    queue = macho_files(app)
    processed: set[Path] = set()
    source_for_name: dict[str, Path] = {}

    while queue:
        binary = queue.pop(0).resolve()
        if binary in processed or not binary.exists():
            continue
        processed.add(binary)
        ensure_writable(binary)
        remove_signature(binary)

        for dep in dependencies(binary):
            dep_name = Path(dep).name

            # The first otool entry for a dylib is normally its own install ID.
            if dep_name == binary.name:
                continue
            if is_system_dependency(dep):
                continue
            if normalize_framework(dep, binary):
                continue

            if is_sdrplay_dependency(dep) and not bundle_sdrplay:
                desired = f"@rpath/{dep_name}"
                if dep != desired:
                    run("install_name_tool", "-change", dep, desired, binary)
                add_rpath(binary, "@loader_path/../Frameworks")
                for value in SDRPLAY_EXTERNAL_RPATHS:
                    add_rpath(binary, value)
                continue

            resolved = resolve_dependency(dep, binary, app)
            if resolved is None:
                raise RuntimeError(f"Cannot resolve {dep!r}, required by {binary}")

            try:
                resolved.relative_to(app)
                inside_app = True
            except ValueError:
                inside_app = False

            if inside_app:
                destination = resolved
            else:
                destination = frameworks / dep_name
                previous = source_for_name.get(dep_name)
                if previous is not None and previous != resolved:
                    if sha256(previous) != sha256(resolved):
                        raise RuntimeError(
                            f"Conflicting dependencies named {dep_name}: {previous} and {resolved}"
                        )
                source_for_name[dep_name] = resolved
                if not destination.exists():
                    print(f"Bundling {resolved} -> {destination}")
                    shutil.copy2(resolved, destination, follow_symlinks=True)
                    ensure_writable(destination)
                    remove_signature(destination)
                    if is_macho(destination):
                        run("install_name_tool", "-id", f"@rpath/{dep_name}", destination)
                        queue.append(destination)

            desired = f"@rpath/{dep_name}"
            if dep != desired:
                run("install_name_tool", "-change", dep, desired, binary)

        add_rpath(binary, "@loader_path/../Frameworks")


def validate_bundle(app: Path, bundle_sdrplay: bool) -> None:
    problems: list[str] = []
    frameworks = app / "Contents/Frameworks"

    for binary in macho_files(app):
        archs = run("lipo", "-archs", binary, capture=True, check=False).strip()
        if archs and "arm64" not in archs.split():
            problems.append(f"Non-arm64 Mach-O: {binary} ({archs})")

        for dep in dependencies(binary):
            dep_name = Path(dep).name
            if dep_name == binary.name or is_system_dependency(dep):
                continue
            if dep_name in SYSTEM_FRAMEWORKS:
                continue
            if is_sdrplay_dependency(dep) and not bundle_sdrplay:
                continue
            if dep.startswith("@rpath/"):
                if not (frameworks / dep_name).is_file():
                    problems.append(f"Missing bundled dependency {dep} for {binary}")
                continue
            if dep.startswith("@loader_path") or dep.startswith("@executable_path"):
                if resolve_dependency(dep, binary, app) is None:
                    problems.append(f"Unresolved relative dependency {dep} for {binary}")
                continue
            if dep.startswith("/opt/homebrew/") or dep.startswith("/usr/local/"):
                problems.append(f"Non-portable dependency {dep} for {binary}")
                continue
            try:
                Path(dep).resolve().relative_to(app)
            except (ValueError, FileNotFoundError):
                problems.append(f"Unexpected external dependency {dep} for {binary}")

    if problems:
        raise RuntimeError("Bundle validation failed:\n- " + "\n- ".join(problems))


def sign_bundle(app: Path) -> None:
    run("xattr", "-cr", app, check=False)
    for binary in sorted(macho_files(app), key=lambda path: len(path.parts), reverse=True):
        run("codesign", "--force", "--sign", "-", "--timestamp=none", binary)
    run("codesign", "--force", "--deep", "--sign", "-", "--timestamp=none", app)
    run("codesign", "--verify", "--deep", "--strict", "--verbose=2", app)


def git_commit() -> str:
    return run("git", "rev-parse", "HEAD", capture=True, check=False).strip() or "unknown"


def write_support_files(package_dir: Path, app_name: str, commit: str, bundle_sdrplay: bool) -> None:
    readme = package_dir / "README.txt"
    readme.write_text(
        "SDR++ RSP1B diagnostic build\n"
        "==============================\n\n"
        f"Git commit: {commit}\n"
        "Architecture: Apple Silicon (arm64)\n"
        "Signing: ad-hoc (not notarized)\n\n"
        "This is a diagnostic snapshot, not a stable release. The current build\n"
        "contains SDRplay lifecycle recovery, DSP/audio diagnostics, and deferred\n"
        "center-frequency tuning while dragging the frequency scale.\n\n"
        "Requirements on every Mac:\n"
        "1. Install SDRplay API 3.15.1, including its background service.\n"
        + (
            "2. The SDRplay client library is embedded for this private package, but\n"
            "   the official background service is still required.\n"
            if bundle_sdrplay
            else
            "2. The SDRplay client library is not embedded. The official installer\n"
            "   must make libsdrplay_api available under /usr/local/lib or\n"
            "   /Library/SDRplayAPI/3.15.1/lib.\n"
        )
        + "\nFirst launch:\n"
        "- Try right-clicking the app and choosing Open.\n"
        "- If macOS still blocks it, run:\n"
        f"    xattr -dr com.apple.quarantine \"{app_name}.app\"\n\n"
        "Recommended test configuration:\n"
        "- Radio sink: Audio\n"
        "- Radio2 sink: None until multi-radio audio is tested further\n"
        "- Use the supplied Run-with-log.command to keep a separate configuration\n"
        "  and write a diagnostic log under ~/Library/Logs.\n",
        encoding="utf-8",
    )

    launcher = package_dir / "Run-with-log.command"
    launcher.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "HERE=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)\n"
        f"APP=\"$HERE/{app_name}.app\"\n"
        "ROOT=\"$HOME/Library/Application Support/SDRPP-RSP1B-Diagnostic\"\n"
        "LOG=\"$HOME/Library/Logs/SDRPP-RSP1B-Diagnostic.log\"\n"
        "mkdir -p \"$ROOT\" \"$(dirname \"$LOG\")\"\n"
        "EXEC=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' \"$APP/Contents/Info.plist\")\n"
        "echo \"Starting diagnostic build. Log: $LOG\"\n"
        "\"$APP/Contents/MacOS/$EXEC\" --root \"$ROOT\" 2>&1 | tee -a \"$LOG\"\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)


def update_info_plist(app: Path, display_name: str, commit: str) -> None:
    plist_path = app / "Contents/Info.plist"
    if not plist_path.is_file():
        return
    with plist_path.open("rb") as stream:
        data = plistlib.load(stream)
    data["CFBundleName"] = display_name
    data["CFBundleDisplayName"] = display_name
    data["CFBundleIdentifier"] = "org.sdrpp.rsp1b-diagnostic"
    data["CFBundleVersion"] = commit[:12] if commit != "unknown" else "diagnostic"
    data["CFBundleShortVersionString"] = "1.2.1-rsp1b-diagnostic"
    with plist_path.open("wb") as stream:
        plistlib.dump(data, stream, sort_keys=True)


def create_manifest(package_dir: Path, app: Path, commit: str) -> None:
    manifest = {
        "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "git_commit": commit,
        "app": app.name,
        "architecture": "arm64",
        "macho_files": [str(path.relative_to(app)) for path in sorted(macho_files(app))],
    }
    (package_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", default="SDRPP-Diagnostic.app", help="Existing working .app bundle")
    parser.add_argument("--output-dir", default="dist", help="Output directory")
    parser.add_argument("--name", default="SDRPlusPlus-RSP1B-Diagnostic-arm64", help="Package/app name")
    parser.add_argument(
        "--bundle-sdrplay",
        action="store_true",
        help="Embed the locally installed SDRplay client library (private copies only)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    require_tools()

    source_app = Path(args.app).expanduser().resolve()
    if not source_app.is_dir() or source_app.suffix != ".app":
        raise RuntimeError(f"Application bundle not found: {source_app}")

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    package_dir = output_dir / args.name
    app = package_dir / f"{args.name}.app"
    zip_path = output_dir / f"{args.name}.zip"
    checksum_path = output_dir / f"{args.name}.zip.sha256"

    if package_dir.exists():
        shutil.rmtree(package_dir)
    if zip_path.exists():
        zip_path.unlink()
    if checksum_path.exists():
        checksum_path.unlink()

    package_dir.mkdir(parents=True)
    print(f"Copying {source_app} -> {app}")
    shutil.copytree(source_app, app, symlinks=True)

    commit = git_commit()
    update_info_plist(app, args.name, commit)
    bundle_dependencies(app, args.bundle_sdrplay)
    validate_bundle(app, args.bundle_sdrplay)
    sign_bundle(app)
    write_support_files(package_dir, args.name, commit, args.bundle_sdrplay)
    create_manifest(package_dir, app, commit)

    run("ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", package_dir, zip_path)
    checksum_path.write_text(f"{sha256(zip_path)}  {zip_path.name}\n", encoding="ascii")

    print("Package created:")
    print(f"  {zip_path}")
    print(f"  {checksum_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
