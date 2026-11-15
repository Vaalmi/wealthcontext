import shutil
import socket
import subprocess
from pathlib import Path

import anyio
import pytest

from scripts.build_bundle import build
from wealthcontext.policy import PolicyClient, PolicyUnavailable


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def opa_server(tmp_path: Path):
    opa = shutil.which("opa")
    if not opa:
        pytest.skip("real OPA service integration runs in make test container")
    revision = build(Path("policies"), tmp_path / "bundle", opa)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    process = subprocess.Popen(
        [opa, "run", "--server", f"--addr=127.0.0.1:{port}", "--bundle",
         str(tmp_path / "bundle/bundle.tar.gz")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    policy = PolicyClient(f"http://127.0.0.1:{port}", revision)
    try:
        with anyio.fail_after(10):
            while True:
                assert process.poll() is None, "OPA exited before readiness"
                try:
                    await policy.verify_revision()
                    break
                except PolicyUnavailable:
                    await anyio.sleep(0.05)
        yield policy
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
