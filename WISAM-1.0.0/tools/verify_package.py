"""Verify release files against SHA256SUMS.json."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    manifest_path = root / "SHA256SUMS.json"
    if not manifest_path.is_file():
        print("SHA256SUMS.json was not found.")
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    for record in manifest["files"]:
        rel = record["path"]
        path = (root / rel).resolve()
        if not path.is_relative_to(root):
            errors.append(f"Unsafe manifest path: {rel}")
            continue
        if not path.is_file():
            errors.append(f"Missing: {rel}")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != record["sha256"] or path.stat().st_size != record["bytes"]:
            errors.append(f"Changed: {rel}")
    if errors:
        print("Integrity verification failed:")
        for error in errors:
            print(" - " + error)
        return 1
    print(f"PASS: {len(manifest['files'])} listed files match their SHA-256 values.")
    print("Generated outputs are outside the source manifest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
