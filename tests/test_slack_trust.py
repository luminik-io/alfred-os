from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from slack_trust import (  # noqa: E402
    SlackTrustStore,
    normalize_slack_user_id,
    parse_trusted_user_ids,
    trusted_user_ids,
)


def test_normalize_slack_user_id_accepts_ids_and_mentions() -> None:
    assert normalize_slack_user_id("U0123ABCDEF") == "U0123ABCDEF"
    assert normalize_slack_user_id("<@U0123ABCDEF>") == "U0123ABCDEF"
    assert normalize_slack_user_id("<@U0123ABCDEF|teammate>") == "U0123ABCDEF"
    assert normalize_slack_user_id("not a user") is None


def test_parse_trusted_user_ids_dedupes_and_drops_invalid_tokens() -> None:
    assert parse_trusted_user_ids("U1ABC, <@U2DEF>; nope U1ABC") == ("U1ABC", "U2DEF")


def test_store_add_remove_and_snapshot(tmp_path: Path) -> None:
    store = SlackTrustStore.from_state_root(tmp_path)

    added, user = store.add("<@U2DEF>", added_by="UOPERATOR")
    assert added is True
    assert user.user_id == "U2DEF"

    added_again, same_user = store.add("U2DEF", added_by="UOPERATOR")
    assert added_again is False
    assert same_user.user_id == "U2DEF"

    payload = json.loads((tmp_path / "slack-trust" / "trusted-users.json").read_text())
    assert payload["version"] == 1
    assert payload["users"][0]["user_id"] == "U2DEF"

    snapshot = store.snapshot(
        operator_user_id="UOPERATOR",
        env_trusted_user_ids=("UENV1",),
    )
    rows = {row.user_id: row for row in snapshot.users}
    assert rows["UOPERATOR"].sources == ("operator",)
    assert rows["UENV1"].sources == ("env",)
    assert rows["U2DEF"].sources == ("local",)
    assert rows["U2DEF"].can_remove is True

    assert store.remove("U2DEF") is True
    assert store.list_local() == ()


def test_store_mutations_refuse_malformed_payload(tmp_path: Path) -> None:
    store = SlackTrustStore.from_state_root(tmp_path)
    path = tmp_path / "slack-trust" / "trusted-users.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="not valid JSON"):
        store.add("U2DEF", added_by="UOPERATOR")
    with pytest.raises(ValueError, match="not valid JSON"):
        store.remove("U2DEF")

    assert path.read_text(encoding="utf-8") == "{broken"


def test_trusted_user_ids_combines_operator_env_and_local(tmp_path: Path) -> None:
    store = SlackTrustStore.from_state_root(tmp_path)
    store.add("U2DEF", added_by="UOPERATOR")

    assert trusted_user_ids(
        operator_user_id="UOPERATOR",
        state_root=tmp_path,
        static_user_ids=("UENV1", "U2DEF"),
    ) == ("UOPERATOR", "UENV1", "U2DEF")
