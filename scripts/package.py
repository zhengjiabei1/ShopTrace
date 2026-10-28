#!/usr/bin/env python3
"""Package project source, excluding runtime data and third-party checkouts."""
import argparse
import hashlib
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = {"lab", "webagent", "frontend", "experience", "configs", "docs", "scripts", "tests", "deploy", "portal", "experiments", "benchmarks", ".github"}
FILES = {"README.md", "README.zh-CN.md", "CHANGELOG.md", "LICENSE", ".gitignore", ".env.example", "pyproject.toml", "requirements.txt", "requirements-webagent.txt", "requirements-dev.txt", "requirements.lock.txt", "upstream.lock.json", "Launch ShopTrace.command", "THIRD_PARTY_NOTICES.md"}
DEPLOY_FILES = {"Dockerfile", "Dockerfile.dockerignore", "compose.yaml", "compose.proxy-ca.yaml", "Caddyfile.example", "runtime.env.example"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    dest = args.output.resolve()
    if dest.exists():
        parser.error("Refusing to overwrite an existing deliverable")
    selected = []
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if relative.parts[0] not in DIRECTORIES and str(relative) not in FILES:
            continue
        if any(part in {"work", "node_modules", ".git"} for part in relative.parts[1:]):
            continue
        if relative.parts[0] == "deploy" and (len(relative.parts) != 2 or relative.name not in DEPLOY_FILES):
            continue
        if path.is_symlink() or not path.is_file() or "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        selected.append((relative.as_posix(), path))
    selected.sort()
    dest.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    with zipfile.ZipFile(dest, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, path in selected:
            content = path.read_bytes()
            entries.append(f"{hashlib.sha256(content).hexdigest()}  {name}")
            info = zipfile.ZipInfo(f"shopping-agent-lab/{name}")
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if name.endswith((".sh", ".command")) else 0o100644) << 16
            archive.writestr(info, content)
        archive.writestr("shopping-agent-lab/MANIFEST.sha256", "\n".join(entries) + "\n")
    print(f"{dest.name}: {len(selected)} source/document files, {dest.stat().st_size} bytes")
    print(f"SHA256 {hashlib.sha256(dest.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
