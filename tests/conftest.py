"""Shared test isolation for repository-wide pytest runs."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_external_operator_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests that need live operator env values set them explicitly."""

    for name in (
        "ALFREDRC",
        "ALFRED_CODE_MAP_REPOS",
        "ALFRED_CODE_MEMORY_REPOS",
        "SLACK_APPROVER_USER_ID",
    ):
        monkeypatch.delenv(name, raising=False)
