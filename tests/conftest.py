from __future__ import annotations

import json
from pathlib import Path

import pytest

from fixture_repo import build

CANNED = Path(__file__).parent / "fixtures" / "canned"


@pytest.fixture(scope="session")
def fixture_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build(tmp_path_factory.mktemp("sfdx") / "repo")


@pytest.fixture
def canned():
    def load(name: str) -> dict:
        return json.loads((CANNED / f"{name}.json").read_text(encoding="utf-8"))

    return load
