"""`CommandMatcher`: scripted partial/final sequences against the default grammar and small ones."""

from __future__ import annotations

import pytest

from studentassistant.stt.commands import (
    CommandGrammar,
    CommandMatcher,
    FiredCommand,
    load_grammar,
)

# One utterance: its (text, is_final) steps in arrival order, and the commands expected to fire at
# each step. Every step is fed to the same matcher under the same segment id.
Step = tuple[str, bool, list[str]]

P, F = False, True  # partial, final


def run(matcher: CommandMatcher, steps: list[Step], segment_id: str = "seg-1") -> None:
    for i, (text, is_final, expected) in enumerate(steps):
        fired = [c.command for c in matcher.match(segment_id, text, is_final)]
        assert fired == expected, f"step {i} {text!r} (final={is_final}): {fired} != {expected}"


@pytest.fixture(scope="module")
def default_grammar() -> CommandGrammar:
    return load_grammar()


# Every phrase of the default grammar, said as a whole final utterance.
EVERY_DEFAULT_COMMAND: list[tuple[str, str]] = [
    ("mira aquí", "capture"),
    ("mira esto", "capture"),
    ("captura", "capture"),
    ("haz foto", "capture"),
    ("siguiente", "next_page"),
    ("pasamos página", "next_page"),
    ("importante", "important"),
    ("ahora el libro", "source_book"),
    ("mira el libro", "source_book"),
    ("vuelvo a mis apuntes", "source_notes"),
    ("ahora el pdf", "source_pdf"),
    ("mira el pdf", "source_pdf"),
    ("pausa", "pause"),
    ("para un momento", "pause"),
    ("reanuda", "resume"),
    ("seguimos", "resume"),
    ("ya está, prepárame el tema", "end_and_prepare"),
]


@pytest.mark.parametrize(("text", "command"), EVERY_DEFAULT_COMMAND)
def test_every_default_phrase_fires_its_command(
    default_grammar: CommandGrammar, text: str, command: str
) -> None:
    run(CommandMatcher(default_grammar), [(text, F, [command])])


def test_esto_es_importante_fires_important_once(default_grammar: CommandGrammar) -> None:
    # Two phrases of the same command in one text still fire it once.
    run(CommandMatcher(default_grammar), [("esto es importante", F, ["important"])])


def test_web_search_fires_on_final_with_query(default_grammar: CommandGrammar) -> None:
    matcher = CommandMatcher(default_grammar)
    assert matcher.match("s", "busca en internet la fotosíntesis", False) == []
    assert matcher.match("s", "Busca en Internet: la Fotosíntesis C4 ", True) == [
        FiredCommand(
            "web_search", "s", "Busca en Internet: la Fotosíntesis C4 ", "la Fotosíntesis C4"
        )
    ]


@pytest.mark.parametrize(
    ("steps"),
    [
        # Accents, case and punctuation variants.
        [("MIRA AQUI", F, ["capture"])],
        [("¡Mira, aquí!", F, ["capture"])],
        [("mira...  aquí", F, ["capture"])],
        [("Pasamos pagina.", F, ["next_page"])],
        [("Ya esta prepárame el TEMA", F, ["end_and_prepare"])],
        [("vale, ahora el PDF por favor", F, ["source_pdf"])],
        # Whole-word near misses.
        [("esto es muy importantes", F, [])],
        [("capturar", F, [])],
        [("siguientes", F, [])],
        [("pausado", F, [])],
        [("miraaquí", F, [])],
        [("mira hacia aquí", F, [])],
        [("reanudamos", F, [])],
        # A command in the middle of other speech.
        [("bueno siguiente que esto ya está", F, ["next_page"])],
        # Two commands in one utterance fire together, in grammar order.
        [("importante, siguiente", F, ["next_page", "important"])],
    ],
)
def test_normalisation_and_whole_words(default_grammar: CommandGrammar, steps: list[Step]) -> None:
    run(CommandMatcher(default_grammar), steps)


@pytest.mark.parametrize(
    ("steps"),
    [
        # The exclusion arriving as a growing partial never fires capture.
        [
            ("mira", P, []),
            ("mira aquí", P, []),
            ("mira aquí no", P, []),
            ("mira, aquí no", F, []),
        ],
        # Same prefix, but the utterance continues differently: fires as soon as it is safe.
        [
            ("mira", P, []),
            ("mira aquí", P, []),
            ("mira aquí esta fórmula", P, ["capture"]),
            ("mira aquí esta fórmula", F, []),
        ],
        # A deferred match that ends the utterance fires on the final.
        [
            ("mira aquí", P, []),
            ("mira aquí", F, ["capture"]),
        ],
        # Phrases that cannot grow into the exclusion fire at the first partial.
        [
            ("captura", P, ["capture"]),
            ("captura", F, []),
        ],
        [
            ("mira esto", P, ["capture"]),
            ("mira esto no", F, []),
        ],
        # Repeated partials fire once.
        [
            ("siguiente", P, ["next_page"]),
            ("siguiente", P, []),
            ("siguiente página", P, []),
            ("siguiente página", F, []),
        ],
        # The exclusion said later in the utterance suppresses what has not fired yet.
        [
            ("mira aquí", P, []),
            ("mira aquí no", F, []),
        ],
        # web_search: never on a partial, query from the final, empty query does not fire.
        [
            ("busca en internet", P, []),
            ("busca en internet mitocondria", P, []),
            ("busca en internet mitocondria", F, ["web_search"]),
        ],
        [
            ("busca en internet", P, []),
            ("busca en internet.", F, []),
        ],
    ],
)
def test_partial_sequences(default_grammar: CommandGrammar, steps: list[Step]) -> None:
    run(CommandMatcher(default_grammar), steps)


def test_debounce_is_per_segment(default_grammar: CommandGrammar) -> None:
    matcher = CommandMatcher(default_grammar)
    run(matcher, [("siguiente", P, ["next_page"]), ("siguiente", F, [])], segment_id="a")
    run(matcher, [("siguiente", F, ["next_page"])], segment_id="b")
    run(matcher, [("siguiente", F, [])], segment_id="a")


def test_forget_drops_the_segment_state(default_grammar: CommandGrammar) -> None:
    matcher = CommandMatcher(default_grammar)
    run(matcher, [("pausa", F, ["pause"])], segment_id="a")
    matcher.forget("a")
    matcher.forget("never-seen")
    run(matcher, [("pausa", F, ["pause"])], segment_id="a")


def test_fired_command_carries_the_text_as_received(default_grammar: CommandGrammar) -> None:
    matcher = CommandMatcher(default_grammar)
    assert matcher.match("s", "¡Mira esto!", False) == [FiredCommand("capture", "s", "¡Mira esto!")]


@pytest.mark.parametrize(
    ("text", "query"),
    [
        ("busca en internet qué es el ATP", "qué es el ATP"),
        ("vale, busca en internet: ciclo de Krebs", "ciclo de Krebs"),
        ("Búsca en internet   la ley de Ohm?  ", "la ley de Ohm?"),
        # The earliest occurrence of the phrase starts the query.
        ("busca en internet busca en internet x", "busca en internet x"),
    ],
)
def test_web_search_query_is_the_original_text_after_the_phrase(
    default_grammar: CommandGrammar, text: str, query: str
) -> None:
    assert CommandMatcher(default_grammar).match("s", text, True) == [
        FiredCommand("web_search", "s", text, query)
    ]


@pytest.mark.parametrize(
    "text", ["busca en internet", "busca en internet ¿?", "busca internet algo"]
)
def test_web_search_without_query_does_not_fire(default_grammar: CommandGrammar, text: str) -> None:
    assert CommandMatcher(default_grammar).match("s", text, True) == []


def test_web_search_query_command_obeys_exclude() -> None:
    grammar = CommandGrammar.model_validate(
        {"commands": {"web_search": {"phrases": ["busca"], "exclude": ["no busques"]}}}
    )
    matcher = CommandMatcher(grammar)
    assert matcher.match("s", "no busques, busca nada", True) == []


def test_custom_grammar_multi_word_exclusion_deferral() -> None:
    # An exclude phrase that extends the phrase by several words defers across partials.
    grammar = CommandGrammar.model_validate(
        {"commands": {"stop": {"phrases": ["para"], "exclude": ["para nada en absoluto"]}}}
    )
    run(
        CommandMatcher(grammar),
        [
            ("para", P, []),
            ("para nada", P, []),
            ("para nada en", P, []),
            ("para nada en absoluto", F, []),
        ],
    )
    run(
        CommandMatcher(grammar),
        [("para", P, []), ("para nada", P, []), ("para nada más", P, ["stop"])],
    )
