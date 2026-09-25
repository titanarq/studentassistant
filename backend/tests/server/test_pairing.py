"""One-time codes and the two pairing endpoints."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi.testclient import TestClient

from studentassistant.config import ServerSettings
from studentassistant.protocol import PROTOCOL_VERSION
from studentassistant.protocol.rest import PairResponse
from studentassistant.server.pairing import CODE_TTL_SECONDS, PairingCodes, base_url

PUBLIC_URL = "http://192.168.1.20:8765"  # the `server` fixture's `public_url`
PairBody = Callable[[str], dict[str, Any]]


def test_a_minted_code_redeems_once(codes: PairingCodes) -> None:
    code, _ = codes.mint()

    assert codes.redeem(code) is True
    assert codes.redeem(code) is False


def test_a_code_redeems_until_it_expires(codes: PairingCodes, clock: Any) -> None:
    code, expires_at = codes.mint()
    assert expires_at == clock.now + CODE_TTL_SECONDS == clock.now + 300

    clock.advance(CODE_TTL_SECONDS - 1)
    assert codes.redeem(code) is True

    late, _ = codes.mint()
    clock.advance(CODE_TTL_SECONDS)
    assert codes.redeem(late) is False


def test_a_code_is_matched_ignoring_case_and_hyphen(codes: PairingCodes) -> None:
    code, _ = codes.mint()

    assert codes.redeem(code.replace("-", "").lower()) is True


def test_an_unknown_code_is_refused(codes: PairingCodes) -> None:
    codes.mint()

    assert codes.redeem("AAAA-AAAA") is False
    assert codes.redeem("") is False


def test_minting_answers_url_code_and_expiry(local: TestClient, clock: Any) -> None:
    response = local.post("/api/pair/codes")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"url", "code", "expires_at"}
    assert body["url"] == PUBLIC_URL
    assert datetime.fromisoformat(body["expires_at"]).timestamp() == clock.now + 300


def test_pairing_redeems_the_code_and_returns_a_token(
    local: TestClient, lan: TestClient, pair_body: PairBody
) -> None:
    code = local.post("/api/pair/codes").json()["code"]

    response = lan.post("/api/pair", json=pair_body(code))

    assert response.status_code == 200
    paired = PairResponse.model_validate(response.json())
    assert paired.protocol_version == PROTOCOL_VERSION
    assert len(paired.token) >= 43
    assert local.app.state.devices.verify_token(paired.token).id == paired.device_id  # type: ignore[attr-defined]


def test_a_second_redemption_is_refused(
    local: TestClient, lan: TestClient, pair_body: PairBody
) -> None:
    code = local.post("/api/pair/codes").json()["code"]
    assert lan.post("/api/pair", json=pair_body(code)).status_code == 200

    again = lan.post("/api/pair", json=pair_body(code))

    assert again.status_code == 401


def test_an_expired_code_is_refused(
    local: TestClient, lan: TestClient, clock: Any, pair_body: PairBody
) -> None:
    code = local.post("/api/pair/codes").json()["code"]
    clock.advance(CODE_TTL_SECONDS + 1)

    assert lan.post("/api/pair", json=pair_body(code)).status_code == 401


def test_the_three_refusals_look_the_same(
    local: TestClient, lan: TestClient, clock: Any, pair_body: PairBody
) -> None:
    used = local.post("/api/pair/codes").json()["code"]
    lan.post("/api/pair", json=pair_body(used))
    expired = local.post("/api/pair/codes").json()["code"]
    clock.advance(CODE_TTL_SECONDS + 1)

    answers = [lan.post("/api/pair", json=pair_body(code)) for code in ("ZZZZ-ZZZZ", used, expired)]

    assert {answer.status_code for answer in answers} == {401}
    assert len({answer.text for answer in answers}) == 1


def test_minting_from_the_lan_is_refused(lan: TestClient) -> None:
    assert lan.post("/api/pair/codes").status_code == 403


def test_pairing_from_a_public_address_is_refused(
    local: TestClient, public: TestClient, pair_body: PairBody
) -> None:
    code = local.post("/api/pair/codes").json()["code"]

    assert public.post("/api/pair", json=pair_body(code)).status_code == 403


def test_an_invalid_body_is_not_echoed_back(lan: TestClient, pair_body: PairBody) -> None:
    body = pair_body("K7Q2-9XMA")
    body["client_kind"] = "toaster"

    response = lan.post("/api/pair", json=body)

    assert response.status_code == 422
    assert "K7Q2-9XMA" not in response.text


def test_the_base_url_defaults_to_the_lan_address_and_port() -> None:
    server = ServerSettings(port=9100)

    assert base_url(server, address=lambda: "192.168.1.20") == "http://192.168.1.20:9100"
    assert base_url(ServerSettings(public_url="http://pc.lan:1/")) == "http://pc.lan:1"
