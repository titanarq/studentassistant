"""Shared fixtures of the server tests: an isolated config, a fake clock and clients by address.

Starlette's TestClient reports its peer as `testclient`, which is no address at all; every client
here names a real one, so the LAN guard and the loopback trust see what a socket would give them.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ws_harness import MsClock, RecordingSink, WsHarness

from studentassistant.config import ServerSettings, SttSettings
from studentassistant.server.app import create_app
from studentassistant.server.pairing import PairingCodes
from studentassistant.stt import SpeechToTextProvider, provider_from_settings
from studentassistant.vault import Vault

LOOPBACK_HOST = "127.0.0.1"
LAN_HOST = "192.168.1.30"
PUBLIC_HOST = "8.8.8.8"
PUBLIC_URL = "http://192.168.1.20:8765"
LOCAL_BASE_URL = "http://localhost:8765"


class HostedTestClient(TestClient):
    """A TestClient whose WebSockets carry its `base_url`'s Host, not Starlette's `testserver`."""

    def websocket_connect(self, url: str, *args: Any, **kwargs: Any) -> Any:
        if "://" not in url:
            url = str(self.base_url.copy_with(scheme="ws")).rstrip("/") + url
        return super().websocket_connect(url, *args, **kwargs)


class FakeClock:
    """Epoch seconds that only move when a test says so."""

    def __init__(self, now: float = 1_790_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No server test reads `~/.config/studentassistant` or an `SA_*` of the machine (AGENTS.md)."""
    for name in list(os.environ):
        if name.startswith("SA_"):
            monkeypatch.delenv(name)
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SA_CONFIG", str(path))
    return path


@pytest.fixture
def devices_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "devices.json"


@pytest.fixture
def server(devices_path: Path) -> ServerSettings:
    return ServerSettings(devices_path=devices_path, public_url=PUBLIC_URL)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def codes(clock: FakeClock) -> PairingCodes:
    return PairingCodes(clock=clock)


@pytest.fixture
def app(server: ServerSettings, codes: PairingCodes, tmp_path: Path) -> FastAPI:
    return create_app(static_dir=tmp_path / "no-web-build", server=server, codes=codes)


@pytest.fixture
def client_at(app: FastAPI) -> Callable[[str], TestClient]:
    """A TestClient over `app` whose requests come from `host`."""

    def make(host: str) -> TestClient:
        # A browser on the PC sends `localhost`; the LAN uses the address `public_url` names.
        base_url = LOCAL_BASE_URL if host in (LOOPBACK_HOST, "::1") else PUBLIC_URL
        return HostedTestClient(app, base_url=base_url, client=(host, 50000))

    return make


@pytest.fixture
def client_with_host() -> Callable[[FastAPI, str], TestClient]:
    """A loopback TestClient over `app` whose requests carry `Host: host_header`."""

    def make(app: FastAPI, host_header: str) -> TestClient:
        return HostedTestClient(
            app, base_url=f"http://{host_header}", client=(LOOPBACK_HOST, 50000)
        )

    return make


@pytest.fixture
def local(client_at: Callable[[str], TestClient]) -> TestClient:
    return client_at(LOOPBACK_HOST)


@pytest.fixture
def lan(client_at: Callable[[str], TestClient]) -> TestClient:
    return client_at(LAN_HOST)


@pytest.fixture
def public(client_at: Callable[[str], TestClient]) -> TestClient:
    return client_at(PUBLIC_HOST)


PairBody = Callable[[str], dict[str, Any]]


@pytest.fixture
def pair_body() -> PairBody:
    """The `rest.pair.request` body redeeming `code`."""

    def body(code: str) -> dict[str, Any]:
        return {
            "pairing_code": code,
            "device_name": "Móvil de Lucía",
            "client_kind": "android",
            "protocol_version": "1.0",
        }

    return body


@pytest.fixture
def pair_device(
    local: TestClient, lan: TestClient, pair_body: PairBody
) -> Callable[[], dict[str, Any]]:
    """Mint a code on the PC and redeem it from the LAN: the `rest.pair.response` body."""

    def pair() -> dict[str, Any]:
        code = local.post("/api/pair/codes").json()["code"]
        response = lan.post("/api/pair", json=pair_body(code))
        assert response.status_code == 200
        return response.json()

    return pair


# -- the session WebSocket (`server/ws.py`) ----------------------------------------------------


@pytest.fixture
def stt_settings() -> SttSettings:
    """The `[stt]` section of the WebSocket tests; server-mode tests override it."""
    return SttSettings(mode="client", provider="web-speech", language="es")


@pytest.fixture
def ws(
    server: ServerSettings,
    codes: PairingCodes,
    tmp_path: Path,
    tmp_vault: Vault,
    stt_settings: SttSettings,
) -> WsHarness:
    """An app over `tmp_vault` with an active session, a recording sink and `provider_from_settings`
    (the `fake` provider in server mode), and a gateway clock 10 s after the session started."""
    app = create_app(
        static_dir=tmp_path / "no-web-build",
        server=server,
        codes=codes,
        vault=tmp_vault,
        stt=stt_settings,
    )
    sinks: list[RecordingSink] = []
    providers: list[SpeechToTextProvider] = []

    def sink_factory(settings: SttSettings, clock_offset: float) -> RecordingSink:
        sink = RecordingSink(settings.provider, clock_offset=clock_offset)
        sinks.append(sink)
        return sink

    def provider_factory(settings: SttSettings) -> SpeechToTextProvider:
        provider = provider_from_settings(settings)
        providers.append(provider)
        return provider

    clock = MsClock()
    gateway = app.state.gateway
    gateway.sink_factory = sink_factory
    gateway.provider_factory = provider_factory
    gateway.clock = clock
    client = HostedTestClient(app, base_url=LOCAL_BASE_URL, client=(LOOPBACK_HOST, 50000))
    lan = HostedTestClient(app, base_url=PUBLIC_URL, client=(LAN_HOST, 50000))
    harness = WsHarness(app, client, lan, tmp_vault, clock, sinks, providers)
    assert client.post("/api/subjects", json={"name": "Física"}).status_code == 201
    assert (
        client.post("/api/subjects/fisica/topics", json={"name": "Cinemática"}).status_code == 201
    )
    started = client.post(
        "/api/sessions",
        json={"subject_id": "fisica", "topic_id": "cinematica", "client_time_ms": 1_000},
    )
    assert started.status_code == 201
    harness.session = started.json()
    clock.now = harness.started_at_ms + 10_000
    return harness
