"""The stream-parser registry (#514 stage 1/3): every registered name resolves to a parser that
satisfies the one interface, and a name nobody registered fails loudly instead of falling back."""

import pytest

from agent_os.streams import (
    STREAM_PARSERS,
    ResultUsage,
    StreamParser,
    StreamQuotaVerdict,
    UnknownStreamParserError,
    UsageSummary,
    get_stream_parser,
)

TODAYS_PARSERS = ("claude_jsonl", "qwen_jsonl")

A_FINISHED_RUN = [
    {"type": "assistant", "session_id": "s1", "message": {"usage": {"input_tokens": 1200}}},
    {"type": "result", "session_id": "s1", "usage": {"total_tokens": 1500, "output_tokens": 300}},
]


@pytest.mark.parametrize("name", TODAYS_PARSERS)
def test_a_registered_name_resolves_to_the_parser_of_that_name(name):
    assert get_stream_parser(name).name == name


def test_an_unregistered_name_raises_naming_what_is_registered():
    with pytest.raises(UnknownStreamParserError) as raised:
        get_stream_parser("foo_jsonl")
    message = str(raised.value)
    assert "'foo_jsonl'" in message
    for name in TODAYS_PARSERS:
        assert name in message


def test_the_registry_holds_exactly_todays_two_shapes():
    assert sorted(STREAM_PARSERS) == sorted(TODAYS_PARSERS)


@pytest.mark.parametrize("name", TODAYS_PARSERS)
def test_every_parser_satisfies_the_three_method_interface(name):
    parser = get_stream_parser(name)
    assert isinstance(parser, StreamParser)

    turns = parser.turn_usage(A_FINISHED_RUN)
    assert isinstance(turns, UsageSummary)
    assert (turns.session_id, turns.turns, turns.context) == ("s1", 1, 1200)

    result = parser.result_usage(A_FINISHED_RUN)
    assert isinstance(result, ResultUsage)
    assert (result.cost_usd, result.total_tokens) == (None, 1500)
    assert parser.result_usage(A_FINISHED_RUN[:1]) is None

    verdict = parser.quota_verdict(A_FINISHED_RUN)
    assert verdict == StreamQuotaVerdict("allowed", None)
