"""`get_client(role)`: per-role config, explicit effort, caching, retries, typed errors."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import anthropic
import httpx2
import pytest

from studentassistant.config import Settings
from studentassistant.llm import (
    ROLES,
    AnthropicTransport,
    FakeClaude,
    LLMAPIError,
    LLMConnectionError,
    LLMRateLimitError,
    LLMRetriesExhaustedError,
    LLMServerError,
    UnknownRoleError,
    cached_block,
    get_client,
)

USER = [{"role": "user", "content": "Hola"}]


# -- roles -------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("role", "model", "effort", "max_tokens"),
    [
        ("observer", "claude-sonnet-5-5", "medium", 16_000),
        ("transcriber", "claude-sonnet-5-5", "medium", 16_000),
        ("editor", "claude-sonnet-5-5", "high", 64_000),
        ("generator", "claude-sonnet-5-5", "high", 64_000),
    ],
)
def test_each_role_gets_its_configured_model_effort_and_max_tokens(
    settings: Settings, role: str, model: str, effort: str, max_tokens: int
) -> None:
    fake = FakeClaude().reply_text("ok")
    client = get_client(role, settings=settings, transport=fake)

    asyncio.run(client.create(USER))

    (request,) = fake.requests
    assert (request.model, request.effort, request.max_tokens) == (model, effort, max_tokens)
    params = request.api_params()
    # Effort is always sent explicitly; thinking is never sent (so never disabled).
    assert params["output_config"] == {"effort": effort}
    assert "thinking" not in params


def test_roles_follow_the_config(settings: Settings) -> None:
    settings.llm.roles.editor.model = "claude-sonnet-de-prueba"
    settings.llm.roles.editor.effort = "max"
    fake = FakeClaude().reply_text("ok")

    asyncio.run(get_client("editor", settings=settings, transport=fake).create(USER))

    assert fake.requests[0].model == "claude-sonnet-de-prueba"
    assert fake.requests[0].api_params()["output_config"] == {"effort": "max"}


def test_every_documented_role_is_known() -> None:
    assert ROLES == ("observer", "transcriber", "editor", "generator")


def test_an_unknown_role_raises_a_typed_error(settings: Settings) -> None:
    with pytest.raises(UnknownRoleError) as info:
        get_client("tutor", settings=settings)

    assert info.value.role == "tutor"


def test_max_tokens_can_be_lowered_per_call(settings: Settings) -> None:
    fake = FakeClaude().reply_text("ok")

    asyncio.run(fake.client("editor", settings=settings).create(USER, max_tokens=256))

    assert fake.requests[0].max_tokens == 256


@pytest.mark.parametrize("kind", ["any", "tool"])
def test_forced_tool_choice_is_refused(settings: Settings, kind: str) -> None:
    client = FakeClaude().client("editor", settings=settings)

    with pytest.raises(ValueError, match="forced tool use"):
        client.build_request(USER, tool_choice={"type": kind, "name": "t"})


# -- prompt caching ----------------------------------------------------------------------------
def test_the_breakpoint_sits_on_the_last_system_block(settings: Settings) -> None:
    client = FakeClaude().client("observer", settings=settings)
    tools = [{"name": "a", "input_schema": {}}, {"name": "b", "input_schema": {}}]

    request = client.build_request(USER, system=["Instrucciones", "Fuentes del tema"], tools=tools)

    assert "cache_control" not in request.system[0]
    assert request.system[-1]["cache_control"] == {"type": "ephemeral"}
    assert all("cache_control" not in tool for tool in request.tools)
    # Volatile content (the messages) is never marked, and the caller's tools are not mutated.
    assert request.messages == USER
    assert all("cache_control" not in tool for tool in tools)


def test_without_system_the_breakpoint_sits_on_the_last_tool(settings: Settings) -> None:
    client = FakeClaude().client("observer", settings=settings)
    tools = [{"name": "a", "input_schema": {}}, {"name": "b", "input_schema": {}}]

    request = client.build_request(USER, tools=tools)

    assert "cache_control" not in request.tools[0]
    assert request.tools[-1]["cache_control"] == {"type": "ephemeral"}


def test_caching_can_be_turned_off_and_explicit_breakpoints_are_kept(settings: Settings) -> None:
    client = FakeClaude().client("observer", settings=settings)

    plain = client.build_request(USER, system="Instrucciones", cache=False)
    explicit = client.build_request(USER, system=[cached_block("Fuentes"), "Resumen"])

    assert "cache_control" not in plain.system[0]
    assert explicit.system[0]["cache_control"] == {"type": "ephemeral"}
    assert explicit.system[1]["cache_control"] == {"type": "ephemeral"}


# -- retries -----------------------------------------------------------------------------------
def test_transient_errors_are_retried_with_backoff(settings: Settings) -> None:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    fake = (
        FakeClaude()
        .fail(LLMRateLimitError("429"))
        .fail(LLMServerError("503", status_code=503))
        .fail(LLMConnectionError("reset"))
        .reply_text("por fin")
    )
    client = get_client("editor", settings=settings, transport=fake, sleep=sleep)

    response = asyncio.run(client.create(USER))

    assert response.text == "por fin"
    assert len(fake.requests) == 4
    assert slept == [1.0, 2.0, 4.0]


def test_retry_after_from_the_server_wins(settings: Settings) -> None:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    fake = FakeClaude().fail(LLMRateLimitError("429", retry_after=7.5)).reply_text("ok")

    asyncio.run(get_client("observer", settings=settings, transport=fake, sleep=sleep).create(USER))

    assert slept == [7.5]


def test_attempts_are_bounded(settings: Settings) -> None:
    settings.llm.max_attempts = 2
    fake = FakeClaude().fail(LLMRateLimitError("a")).fail(LLMRateLimitError("b")).reply_text("x")

    with pytest.raises(LLMRetriesExhaustedError) as info:
        asyncio.run(fake.client("editor", settings=settings).create(USER))

    assert info.value.attempts == 2
    assert isinstance(info.value.last_error, LLMRateLimitError)
    assert len(fake.requests) == 2
    assert fake.pending == 1


def fail_every_attempt(count: int) -> FakeClaude:
    fake = FakeClaude()
    for n in range(count):
        fake.fail(LLMConnectionError(f"timed out {n}"))
    return fake.reply_text("never")


def test_the_observer_role_makes_two_attempts_by_default(settings: Settings) -> None:
    fake = fail_every_attempt(4)

    with pytest.raises(LLMRetriesExhaustedError) as info:
        asyncio.run(fake.client("observer", settings=settings).create(USER))

    assert info.value.attempts == 2 and len(fake.requests) == 2


@pytest.mark.parametrize("role", ["transcriber", "editor", "generator"])
def test_other_roles_keep_the_global_attempts(settings: Settings, role: str) -> None:
    settings.llm.max_attempts = 3
    fake = fail_every_attempt(4)

    with pytest.raises(LLMRetriesExhaustedError) as info:
        asyncio.run(fake.client(role, settings=settings).create(USER))

    assert info.value.attempts == 3 and len(fake.requests) == 3


def test_a_role_override_of_the_attempts_wins_over_the_global_one(settings: Settings) -> None:
    settings.llm.max_attempts = 4
    settings.llm.roles.editor.max_attempts = 1
    fake = fail_every_attempt(4)

    with pytest.raises(LLMRetriesExhaustedError):
        asyncio.run(fake.client("editor", settings=settings).create(USER))

    assert len(fake.requests) == 1


def test_the_request_carries_the_roles_turn_timeout(settings: Settings) -> None:
    settings.llm.roles.generator.turn_timeout_seconds = 45.0
    fake = FakeClaude()

    def timeout(role: str) -> float | None:
        return fake.client(role, settings=settings).build_request(USER).turn_timeout_seconds

    assert timeout("observer") == 90.0
    assert timeout("generator") == 45.0
    assert timeout("editor") is None and timeout("transcriber") is None
    assert "turn_timeout_seconds" not in fake.client("observer").build_request(USER).api_params()


def test_non_transient_errors_are_not_retried(settings: Settings) -> None:
    fake = FakeClaude().fail(LLMAPIError("bad", status_code=400)).reply_text("x")

    with pytest.raises(LLMAPIError):
        asyncio.run(fake.client("observer", settings=settings).create(USER))

    assert len(fake.requests) == 1


# -- the real transport, over a mocked HTTP layer (no network) ---------------------------------
def sse(*events: tuple[str, dict[str, Any]]) -> bytes:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


def message_stream(text: str, model: str) -> bytes:
    return sse(
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 1,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 8,
                    },
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 5},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    )


def error_body(kind: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": kind}}


class Recorder:
    """An httpx2 handler replaying canned responses and keeping the request bodies."""

    def __init__(self, *responses: httpx2.Response | Exception) -> None:
        self.responses = list(responses)
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(request.content))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def sdk_transport(recorder: Recorder) -> AnthropicTransport:
    sdk = anthropic.AsyncAnthropic(
        api_key="sk-ant-test",
        base_url="http://claude.test",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(recorder)),
    )
    return AnthropicTransport(sdk)


def stream_ok(text: str, model: str) -> httpx2.Response:
    return httpx2.Response(
        200, content=message_stream(text, model), headers={"content-type": "text/event-stream"}
    )


def test_the_real_transport_streams_and_maps_usage(settings: Settings) -> None:
    recorder = Recorder(stream_ok("Hola, Ana", "claude-sonnet-5-5"))
    client = get_client("editor", settings=settings, transport=sdk_transport(recorder))

    response = asyncio.run(client.create(USER, system="Eres el editor."))

    assert response.text == "Hola, Ana"
    assert response.stop_reason == "end_turn"
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 5
    assert response.usage.cache_read_input_tokens == 8
    (body,) = recorder.bodies
    assert body["stream"] is True
    assert body["model"] == "claude-sonnet-5-5"
    assert body["output_config"] == {"effort": "high"}
    assert "thinking" not in body
    assert body["system"][-1]["cache_control"] == {"type": "ephemeral"}


def test_the_real_transport_streams_the_text_deltas(settings: Settings) -> None:
    recorder = Recorder(stream_ok("Hola, Ana", "claude-sonnet-5-5"))
    client = get_client("editor", settings=settings, transport=sdk_transport(recorder))
    deltas: list[str] = []

    async def on_text(text: str) -> None:
        deltas.append(text)

    response = asyncio.run(client.create(USER, on_text=on_text))

    assert deltas == ["Hola, Ana"] and response.text == "Hola, Ana"


@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (httpx2.Response(429, json=error_body("rate_limit_error")), LLMRateLimitError),
        (httpx2.Response(500, json=error_body("api_error")), LLMServerError),
        (httpx2.Response(529, json=error_body("overloaded_error")), LLMServerError),
        (httpx2.Response(400, json=error_body("invalid_request_error")), LLMAPIError),
        (httpx2.Response(401, json=error_body("authentication_error")), LLMAPIError),
        (httpx2.ConnectError("refused"), LLMConnectionError),
    ],
)
def test_sdk_errors_become_typed_errors(
    settings: Settings, response: httpx2.Response | Exception, error_type: type[Exception]
) -> None:
    transport = sdk_transport(Recorder(response))
    request = FakeClaude().client("observer", settings=settings).build_request(USER)

    with pytest.raises(error_type) as info:
        asyncio.run(transport.send(request))

    assert not isinstance(info.value, anthropic.AnthropicError)
    assert isinstance(info.value.__cause__, anthropic.AnthropicError)


def test_a_mid_stream_overload_is_retryable(settings: Settings) -> None:
    body = sse(("error", error_body("overloaded_error")))
    response = httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})
    transport = sdk_transport(Recorder(response))
    request = FakeClaude().client("observer", settings=settings).build_request(USER)

    with pytest.raises(LLMServerError):
        asyncio.run(transport.send(request))


def test_the_real_transport_is_retried_by_the_client(settings: Settings) -> None:
    recorder = Recorder(
        httpx2.Response(429, json=error_body("rate_limit_error"), headers={"retry-after": "3"}),
        stream_ok("ok", "claude-sonnet-5-5"),
    )
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    client = get_client(
        "observer", settings=settings, transport=sdk_transport(recorder), sleep=sleep
    )

    assert asyncio.run(client.create(USER)).text == "ok"
    assert slept == [3.0]
    assert len(recorder.bodies) == 2


class HangingStream(httpx2.AsyncByteStream):
    """A streamed body that never sends its first event."""

    async def __aiter__(self):  # type: ignore[override]
        await asyncio.sleep(3600)
        yield b""


def test_the_real_transport_gives_up_after_the_roles_turn_timeout(settings: Settings) -> None:
    settings.llm.roles.observer.turn_timeout_seconds = 0.2
    recorder = Recorder(
        *[
            httpx2.Response(
                200, stream=HangingStream(), headers={"content-type": "text/event-stream"}
            )
            for _ in range(2)
        ]
    )
    client = get_client(
        "observer", settings=settings, transport=sdk_transport(recorder), sleep=no_wait
    )

    async def go() -> None:
        await asyncio.wait_for(client.create(USER), 10.0)

    with pytest.raises(LLMRetriesExhaustedError) as info:
        asyncio.run(go())

    assert isinstance(info.value.last_error, LLMConnectionError)
    assert "within 0.2 s" in str(info.value.last_error)
    assert len(recorder.bodies) == 2


async def no_wait(seconds: float) -> None:
    return None


def test_building_a_client_needs_no_api_key(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings.llm.backend = "api"

    client = get_client("editor", settings=settings)

    assert isinstance(client.transport, AnthropicTransport)
