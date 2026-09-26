"""The Claude Code backend over a fake `claude` executable: one process per conversation, only
the new turns sent, client tools emulated, usage and cost from the CLI, errors mapped."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from fake_claude_cli import FakeClaudeCli, install_fake_claude
from ledger_helpers import binding, capped_settings, make_topic
from pydantic import BaseModel

from studentassistant.config import ClaudeCodeSettings, Settings
from studentassistant.llm import (
    ClaudeCodeTransport,
    CostConfirmationRequiredError,
    LLMAPIError,
    LLMConnectionError,
    LLMRateLimitError,
    LLMServerError,
    StructuredOutputError,
    get_client,
    no_sleep,
    structured,
    web_search_tool,
)
from studentassistant.llm.claude_code import parse_tool_calls
from studentassistant.vault import Vault, read_ledger

TEST_TIMEOUT_SECONDS = 30.0


def run[T](factory: Callable[[], Awaitable[T]]) -> T:
    """Run a coroutine with a hard bound, so a stuck fake never hangs the suite."""

    async def bounded() -> T:
        return await asyncio.wait_for(factory(), TEST_TIMEOUT_SECONDS)

    return asyncio.run(bounded())


@pytest.fixture
def fake(tmp_path: Path) -> FakeClaudeCli:
    return install_fake_claude(tmp_path / "fake-claude")


def user(text: str, *, cache: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "text", "text": text}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return {"role": "user", "content": [block]}


class Answer(BaseModel):
    topic: str


def test_a_call_starts_one_restricted_process(fake: FakeClaudeCli, settings: Settings) -> None:
    fake.reply("Hola", usage={"input_tokens": 120, "output_tokens": 7}, cost=0.0125)
    transport = ClaudeCodeTransport(fake.settings())
    client = get_client("observer", settings=settings, transport=transport)

    async def go() -> Any:
        try:
            return await client.create([user("Saluda")], system="Eres un observador.")
        finally:
            await transport.aclose()

    response = run(go)

    assert response.text == "Hola"
    assert response.stop_reason == "end_turn"
    assert response.model == settings.llm.roles.observer.model
    assert response.usage.input_tokens == 120 and response.usage.output_tokens == 7
    assert response.billing == "subscription"
    assert response.reported_usd == pytest.approx(0.0125)
    [started] = fake.runs
    argv = started["argv"]
    assert argv[:7] == [
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
    ]
    assert argv[argv.index("--model") + 1] == settings.llm.roles.observer.model
    assert argv[argv.index("--effort") + 1] == settings.llm.roles.observer.effort
    assert argv[argv.index("--tools") + 1] == ""
    assert "--no-session-persistence" in argv and "--strict-mcp-config" in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert started["system"] == "Eres un observador."
    assert started["max_tokens"] == str(settings.llm.roles.observer.max_tokens)
    assert Path(started["cwd"]) == fake.directory / "work"
    assert fake.turn_texts(0) == ["Saluda"]
    # Closed: the system prompt file is gone with its process.
    assert list((fake.directory / "work").glob("system-*")) == []


def test_a_continued_conversation_sends_only_the_new_turn(
    fake: FakeClaudeCli, settings: Settings
) -> None:
    fake.reply("uno", cost=0.01).reply("dos", cost=0.02)
    transport = ClaudeCodeTransport(fake.settings())
    client = get_client("editor", settings=settings, transport=transport)

    async def go() -> tuple[Any, Any]:
        try:
            conversation = [user("Primera", cache=True)]
            first = await client.create(conversation, system="Tutor")
            conversation = [user("Primera"), first.assistant_turn(), user("Segunda", cache=True)]
            second = await client.create(conversation, system="Tutor")
            return first, second
        finally:
            await transport.aclose()

    first, second = run(go)

    assert (first.text, second.text) == ("uno", "dos")
    assert len(fake.runs) == 1
    assert [turn["pid"] for turn in fake.turns] == [fake.runs[0]["pid"]] * 2
    assert fake.turn_texts(1) == ["Segunda"]
    # `total_cost_usd` is the process's running total: each turn gets its own share.
    assert first.reported_usd == pytest.approx(0.01)
    assert second.reported_usd == pytest.approx(0.02)


def test_a_different_conversation_gets_its_own_process_with_its_history(
    fake: FakeClaudeCli, settings: Settings
) -> None:
    fake.reply("A").reply("B").reply("C")
    transport = ClaudeCodeTransport(fake.settings())
    client = get_client("editor", settings=settings, transport=transport)

    async def go() -> None:
        try:
            first = await client.create([user("Hola")], system="Tutor")
            # Another system prompt: another conversation.
            await client.create([user("Hola")], system="Otro")
            # An edited history (not what the first process saw): a fresh process gets it all.
            await client.create(
                [user("Hola editado"), first.assistant_turn(), user("Sigue")], system="Tutor"
            )
        finally:
            await transport.aclose()

    run(go)

    assert len(fake.runs) == 3
    assert len({turn["pid"] for turn in fake.turns}) == 3
    history = fake.turn_texts(2)
    assert "=== user ===" in history and "Hola editado" in history
    assert "=== assistant (your earlier reply) ===" in history and "A" in history
    assert history[-1] == "Sigue"


def test_structured_goes_through_emulated_tools_with_one_repair(
    fake: FakeClaudeCli, settings: Settings
) -> None:
    fake.reply('{"tool_calls": [{"name": "answer", "input": {"topic": 3')  # cut-off JSON
    fake.reply(
        '```json\n{"tool_calls": [{"name": "answer", "input": {"topic": "Derivadas"}}]}\n```'
    )
    transport = ClaudeCodeTransport(fake.settings())
    client = get_client("generator", settings=settings, transport=transport, sleep=no_sleep)

    async def go() -> Any:
        try:
            return await structured(
                client,
                [user("¿De qué va?")],
                Answer,
                tool_name="answer",
                tool_description="The topic",
                system="Clasifica.",
            )
        finally:
            await transport.aclose()

    result = run(go)

    assert result.value == Answer(topic="Derivadas")
    assert [r.stop_reason for r in result.responses] == ["tool_use", "tool_use"]
    [started] = fake.runs
    assert "## answer" in started["system"] and '"topic"' in started["system"]
    assert '{"tool_calls"' in started["system"]
    repair = "\n".join(fake.turn_texts(1))
    assert "[tool_result of `answer`" in repair and "ERROR" in repair
    assert "not valid JSON" in repair


def test_structured_gives_up_after_the_repair(fake: FakeClaudeCli, settings: Settings) -> None:
    fake.reply("No sé.").reply("Sigo sin saber.")
    transport = ClaudeCodeTransport(fake.settings())
    client = get_client("generator", settings=settings, transport=transport)

    async def go() -> None:
        try:
            await structured(
                client, [user("?")], Answer, tool_name="answer", tool_description="The topic"
            )
        finally:
            await transport.aclose()

    with pytest.raises(StructuredOutputError):
        run(go)


def test_text_is_streamed_but_a_tool_call_is_not(fake: FakeClaudeCli, settings: Settings) -> None:
    tool = {"name": "note", "description": "Take a note", "input_schema": {"type": "object"}}
    fake.reply("Hola, estudiante").reply('{"tool_calls": [{"name": "note", "input": {}}]}')
    fake.reply("Sin herramienta")
    transport = ClaudeCodeTransport(fake.settings())
    client = get_client("editor", settings=settings, transport=transport)
    deltas: list[list[str]] = [[], [], []]

    async def go() -> list[Any]:
        try:
            responses = []
            for index in range(3):

                async def sink(delta: str, index: int = index) -> None:
                    deltas[index].append(delta)

                responses.append(
                    await client.create(
                        [user(f"turno {index}")],
                        system=f"s{index}",
                        tools=[tool] if index else None,
                        on_text=sink,
                    )
                )
            return responses
        finally:
            await transport.aclose()

    plain, call, prose = run(go)

    assert "".join(deltas[0]) == "Hola, estudiante" and len(deltas[0]) == 2
    assert call.tool_calls[0].name == "note" and deltas[1] == []
    assert call.tool_calls[0].id.startswith("toolu_cc_")
    assert "".join(deltas[2]) == "Sin herramienta"
    assert plain.stop_reason == "end_turn" and prose.stop_reason == "end_turn"


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(429, LLMRateLimitError), (529, LLMServerError), (400, LLMAPIError), (None, LLMAPIError)],
)
def test_an_error_result_maps_to_an_llm_error_and_drops_the_process(
    fake: FakeClaudeCli, settings: Settings, status: int | None, error_type: type[Exception]
) -> None:
    fake.reply(error={"status": status, "message": "limit"})
    transport = ClaudeCodeTransport(fake.settings())
    client = get_client("editor", settings=settings, transport=transport)

    async def go() -> None:
        try:
            await client.transport.send(client.build_request([user("x")]))
        finally:
            assert transport.process_count == 0
            await transport.aclose()

    with pytest.raises(error_type, match="limit"):
        run(go)


def test_a_dead_process_is_a_connection_error_retried_on_a_fresh_one(
    fake: FakeClaudeCli, settings: Settings
) -> None:
    fake.reply(exit=3).reply("Ya")
    transport = ClaudeCodeTransport(fake.settings())
    client = get_client("editor", settings=settings, transport=transport, sleep=no_sleep)

    async def go() -> Any:
        try:
            with pytest.raises(LLMConnectionError, match="exiting on purpose"):
                await transport.send(client.build_request([user("x")]))
            return await client.create([user("x")])
        finally:
            await transport.aclose()

    assert run(go).text == "Ya"
    assert len(fake.runs) == 2


@pytest.mark.parametrize("configured", ["default", "tilde", "relative"])
def test_the_workdir_and_prompt_file_reach_the_cli_as_absolute_paths(
    fake: FakeClaudeCli,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured: str,
) -> None:
    # Issue #307: the default `~/...` workdir was never expanded, so the CLI ran in `./~/...` and
    # resolved the relative prompt path against it a second time ("System prompt file not found").
    home = tmp_path / "home"
    home.mkdir()
    elsewhere = tmp_path / "service-cwd"
    elsewhere.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(elsewhere)
    workdir = {"tilde": "~/.cache/sa/claude-code", "relative": "rel/claude-code"}
    cc_settings = ClaudeCodeSettings(
        executable=str(fake.executable),
        **({"workdir": workdir[configured]} if configured in workdir else {}),
    )
    expected = {
        "default": home / ".cache/studentassistant/claude-code",
        "tilde": home / ".cache/sa/claude-code",
        "relative": elsewhere / "rel/claude-code",
    }[configured]
    fake.reply("Hola")
    transport = ClaudeCodeTransport(cc_settings)
    client = get_client("observer", settings=settings, transport=transport)

    async def go() -> Any:
        try:
            return await client.create([user("Saluda")], system="Eres un observador.")
        finally:
            await transport.aclose()

    assert run(go).text == "Hola"
    [started] = fake.runs
    prompt_file = Path(started["argv"][started["argv"].index("--system-prompt-file") + 1])
    assert prompt_file.is_absolute() and prompt_file.parent == expected
    assert Path(started["cwd"]) == expected
    assert started["system"] == "Eres un observador."
    assert not (elsewhere / "~").exists()


def test_a_process_that_dies_at_startup_surfaces_its_stderr(
    fake: FakeClaudeCli, settings: Settings, tmp_path: Path
) -> None:
    # A CLI that fails before reading stdin (as the real one does on a bad flag or a missing
    # prompt file) must report why, not a bare "Broken pipe".
    script = tmp_path / "dying-claude"
    script.write_text(
        "#!/bin/sh\necho 'Error: System prompt file not found: /nowhere.md' >&2\nexit 1\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    async def spawn_and_let_it_die(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await asyncio.create_subprocess_exec(*args, **kwargs)
        await asyncio.wait_for(process.wait(), TEST_TIMEOUT_SECONDS)  # gone before the write
        return process

    transport = ClaudeCodeTransport(
        fake.settings(executable=str(script)), spawn=spawn_and_let_it_die
    )
    request = get_client("editor", settings=settings, transport=transport).build_request(
        [user("x")]
    )

    async def go() -> None:
        try:
            await transport.send(request)
        finally:
            await transport.aclose()

    with pytest.raises(LLMConnectionError, match=r"code 1\).*System prompt file not found"):
        run(go)


def test_a_tilde_executable_is_expanded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    assert ClaudeCodeSettings(executable="~/bin/claude").executable == str(tmp_path / "bin/claude")
    assert ClaudeCodeSettings().executable == "claude"


def test_a_turn_over_the_timeout_kills_the_process(fake: FakeClaudeCli, settings: Settings) -> None:
    fake.reply(hang=True)
    transport = ClaudeCodeTransport(fake.settings(turn_timeout_seconds=0.5))
    client = get_client("editor", settings=settings, transport=transport)

    async def go() -> None:
        try:
            await transport.send(client.build_request([user("x")]))
        finally:
            assert transport.process_count == 0
            await transport.aclose()

    with pytest.raises(LLMConnectionError, match="did not answer"):
        run(go)


def test_an_idle_process_is_closed_by_its_timer(fake: FakeClaudeCli, settings: Settings) -> None:
    fake.reply("Hola")
    transport = ClaudeCodeTransport(fake.settings(idle_timeout_seconds=0.2))
    client = get_client("editor", settings=settings, transport=transport)

    async def go() -> tuple[int, int]:
        await client.create([user("x")])
        live = transport.process_count
        for _ in range(100):  # bounded: at most 10 s
            await asyncio.sleep(0.1)
            if transport.process_count == 0:
                break
        count = transport.process_count
        await transport.aclose()
        return live, count

    assert run(go) == (1, 0)


def test_the_least_recently_used_idle_process_makes_room(
    fake: FakeClaudeCli, settings: Settings
) -> None:
    fake.reply("A").reply("B").reply("C")
    transport = ClaudeCodeTransport(fake.settings(max_processes=1))
    client = get_client("editor", settings=settings, transport=transport)

    async def go() -> int:
        try:
            first = await client.create([user("uno")], system="1")
            await client.create([user("dos")], system="2")
            # The first conversation was closed to make room: it starts again from its history.
            await client.create([user("uno"), first.assistant_turn(), user("tres")], system="1")
            return transport.process_count
        finally:
            await transport.aclose()

    assert run(go) == 1
    assert len(fake.runs) == 3


def test_server_tools_are_refused_before_starting_anything(
    fake: FakeClaudeCli, settings: Settings
) -> None:
    transport = ClaudeCodeTransport(fake.settings())
    client = get_client("editor", settings=settings, transport=transport)
    request = client.build_request([user("busca")], tools=[web_search_tool(settings.llm)])

    with pytest.raises(LLMAPIError, match="claude-code backend"):
        run(lambda: transport.send(request))
    assert fake.runs == []


def test_a_missing_executable_is_a_clear_error(tmp_path: Path, settings: Settings) -> None:
    fake = FakeClaudeCli.__new__(FakeClaudeCli)
    fake.directory = tmp_path
    fake.executable = tmp_path / "no-such-claude"
    transport = ClaudeCodeTransport(fake.settings())
    request = get_client("editor", settings=settings, transport=transport).build_request(
        [user("x")]
    )

    with pytest.raises(LLMAPIError, match="cannot run the Claude Code CLI"):
        run(lambda: transport.send(request))


def test_images_and_tool_results_reach_the_cli(fake: FakeClaudeCli, settings: Settings) -> None:
    tool = {"name": "note", "description": "Take a note", "input_schema": {"type": "object"}}
    fake.reply('{"tool_calls": [{"name": "note", "input": {"a": 1}}]}').reply("Hecho")
    transport = ClaudeCodeTransport(fake.settings())
    client = get_client("transcriber", settings=settings, transport=transport)
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}}

    async def go() -> None:
        try:
            first_turn = {"role": "user", "content": [image, {"type": "text", "text": "Lee"}]}
            first = await client.create([first_turn], tools=[tool])
            [call] = first.tool_calls
            result = {"type": "tool_result", "tool_use_id": call.id, "content": "anotado"}
            await client.create(
                [first_turn, first.assistant_turn(), {"role": "user", "content": [result]}],
                tools=[tool],
            )
        finally:
            await transport.aclose()

    run(go)

    assert fake.turns[0]["turn"]["message"]["content"][0] == image
    assert fake.turn_texts(1) == [
        f"[tool_result of `note` ({fake_tool_id(fake)})]\nanotado",
    ]


def fake_tool_id(fake: FakeClaudeCli) -> str:
    text = fake.turn_texts(1)[0]
    return text[text.index("(") + 1 : text.index(")")]


def test_a_bound_call_records_the_reported_cost_as_subscription(
    fake: FakeClaudeCli, tmp_vault: Vault, settings: Settings
) -> None:
    topic = make_topic(tmp_vault)
    fake.reply("Hola", usage={"input_tokens": 1000, "output_tokens": 100}, cost=0.5)
    transport = ClaudeCodeTransport(fake.settings())
    client = get_client(
        "editor",
        settings=capped_settings(settings, per_session=0.4),
        transport=transport,
        ledger=binding(tmp_vault, topic),
    )

    async def go() -> None:
        try:
            await client.create([user("x")])
        finally:
            await transport.aclose()

    run(go)

    [entry] = read_ledger(tmp_vault, *topic)
    assert entry.billing == "subscription"
    assert entry.estimated_usd == pytest.approx(0.5)
    assert (entry.input_tokens, entry.output_tokens) == (1000, 100)
    # The cap still applies to the reported cost.
    with pytest.raises(CostConfirmationRequiredError):
        run(lambda: client.create([user("y")]))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hola", None),
        (
            '{"tool_calls": [{"name": "a", "input": {"x": 1}}]}',
            ("", [{"name": "a", "input": {"x": 1}}]),
        ),
        (
            'Voy.\n```json\n{"tool_calls": [{"name": "a"}]}\n```',
            ("Voy.", [{"name": "a", "input": {}}]),
        ),
        (
            '{"tool_calls": [{"name": "a", "input": {',
            ("", [{"name": "a", "input": '{"tool_calls": [{"name": "a", "input": {'}]),
        ),
        ('{"tool_calls": []}', None),
        ('{"other": 1}', None),
    ],
)
def test_parse_tool_calls(text: str, expected: Any) -> None:
    parsed = parse_tool_calls(text)
    if expected is not None and isinstance(expected[1][0]["input"], str):
        # A malformed call keeps its raw input text (what `structured` reports as bad JSON).
        assert parsed is not None and parsed[1][0]["name"] == "a"
        with pytest.raises(json.JSONDecodeError):
            json.loads(parsed[1][0]["input"])
        return
    assert parsed == expected
