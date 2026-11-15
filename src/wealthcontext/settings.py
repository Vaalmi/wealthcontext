"""Small explicit runtime configuration; no credentials are stored in source."""

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    bundle_dir: Path
    opa_url: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.environ.get("WEALTHCONTEXT_DATA_DIR", "runtime")),
            bundle_dir=Path(os.environ.get("WEALTHCONTEXT_BUNDLE_DIR", "build/policy")),
            opa_url=os.environ.get("WEALTHCONTEXT_OPA_URL", "http://opa:8181"),
        )

    @property
    def expected_revision(self) -> str:
        value = (self.bundle_dir / "revision.txt").read_text(encoding="utf-8").strip()
        if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
            raise ValueError("invalid policy revision")
        return value
