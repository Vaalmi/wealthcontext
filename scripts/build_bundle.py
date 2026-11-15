"""Canonical policy-source digest and an actual `opa build` bundle."""

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def build(source: Path, output: Path, opa: str) -> str:
    files = sorted((
        p for p in source.rglob("*")
        if p.is_file() and (p.suffix == ".rego" or p.name == "data.json")
        and not p.name.endswith("_test.rego")
    ), key=lambda p: p.relative_to(source).as_posix())
    if not files:
        raise ValueError("policy source is empty")
    canonical = [
        {"path": p.relative_to(source).as_posix(),
         "text": p.read_text(encoding="utf-8").replace("\r\n", "\n")}
        for p in files
    ]
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    revision = f"sha256:{digest}"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        staging = Path(directory)
        for entry in canonical:
            path = staging / entry["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(entry["text"], encoding="utf-8", newline="\n")
        (staging / ".manifest").write_text(
            json.dumps({"revision": revision, "roots": ["wealthcontext"], "rego_version": 1}),
            encoding="utf-8", newline="\n",
        )
        subprocess.run(
            [opa, "build", "--bundle", str(staging), "--output", str(output / "bundle.tar.gz")],
            check=True,
        )
    (output / "revision.txt").write_text(revision + "\n", encoding="utf-8", newline="\n")
    return revision


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("policies"))
    parser.add_argument("--output", type=Path, default=Path("build/policy"))
    parser.add_argument("--opa", default=shutil.which("opa") or "/usr/local/bin/opa")
    options = parser.parse_args()
    print(build(options.source, options.output, options.opa))
