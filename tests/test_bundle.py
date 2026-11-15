import json
import shutil
import socket
import subprocess
import tarfile
from pathlib import Path

import anyio
import pytest

from scripts.build_bundle import build
from wealthcontext.policy import PolicyClient, PolicyUnavailable


@pytest.mark.skipif(
    shutil.which("opa") is None, reason="real OPA build runs in make test container"
)
def test_bundle_revision_matches_manifest_and_changes_with_policy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    shutil.copytree("policies", source)
    output = tmp_path / "output"
    opa = shutil.which("opa")
    assert opa
    first = build(source, output, opa)
    assert build(source, output, opa) == first
    with tarfile.open(output / "bundle.tar.gz") as archive:
        manifest = archive.extractfile("/.manifest")
        assert manifest
        assert json.load(manifest)["revision"] == first
    (source / "data.json").write_text('{"wealthcontext":{"configuration":{"version":2}}}')
    assert build(source, output, opa) != first


@pytest.mark.anyio
@pytest.mark.skipif(shutil.which("opa") is None, reason="real OPA server runs in make test")
async def test_actual_opa_manifest_revision_is_verified_and_mismatch_denied(tmp_path: Path) -> None:
    opa = shutil.which("opa")
    assert opa
    revision = build(Path("policies"), tmp_path, opa)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    process = subprocess.Popen(
        [opa, "run", "--server", f"--addr=127.0.0.1:{port}",
         "--bundle", str(tmp_path / "bundle.tar.gz")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        policy = PolicyClient(f"http://127.0.0.1:{port}", revision)
        with anyio.fail_after(10):
            while True:
                assert process.poll() is None, "OPA exited before becoming ready"
                try:
                    assert await policy.verify_revision() == revision
                    break
                except PolicyUnavailable:
                    await anyio.sleep(0.05)
        wrong = PolicyClient(f"http://127.0.0.1:{port}", "sha256:" + "0" * 64)
        with pytest.raises(PolicyUnavailable):
            await wrong.verify_revision()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
