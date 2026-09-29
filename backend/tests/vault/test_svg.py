"""The SVG sanitizer and `.svg` images in the vault (#511)."""

from __future__ import annotations

import pytest

from studentassistant.vault import (
    SvgError,
    Vault,
    create_subject,
    create_topic,
    is_safe_svg,
    list_sources,
    put_pasted_image,
    put_source,
    read_source,
    remove_source,
    sanitize_svg,
)
from studentassistant.vault.sources import SourceError
from studentassistant.vault.svg import MAX_SVG_BYTES

NS = 'xmlns="http://www.w3.org/2000/svg"'
XLINK = 'xmlns:xlink="http://www.w3.org/1999/xlink"'


def _clean(body: str, attributes: str = "") -> str:
    return sanitize_svg(f'<svg {NS} {XLINK} viewBox="0 0 10 10" {attributes}>{body}</svg>').decode()


def test_a_plain_drawing_is_kept_and_sanitizing_twice_changes_nothing() -> None:
    svg = (
        f'<svg {NS} viewBox="0 0 100 50"><title>Recta</title>'
        '<defs><marker id="m"><path d="M0,0 L5,2 z"/></marker></defs>'
        '<line x1="0" y1="0" x2="90" y2="40" stroke="#222" marker-end="url(#m)"/>'
        '<text x="5" y="45" font-size="6">y = <tspan>2x</tspan> + 1</text></svg>'
    )

    clean = sanitize_svg(svg)

    text = clean.decode()
    assert text.startswith("<?xml")
    for kept in ("<title>Recta</title>", 'marker-end="url(#m)"', "<tspan>2x</tspan>", "<marker"):
        assert kept in text
    assert sanitize_svg(clean) == clean and is_safe_svg(clean)
    assert not is_safe_svg(svg.encode())


@pytest.mark.parametrize(
    ("body", "attributes", "gone"),
    [
        ("<script>alert(1)</script><rect/>", "", "alert"),
        ('<rect onclick="x()" onmouseover="y()"/>', 'onload="z()"', "on"),
        (
            '<foreignObject><div xmlns="http://www.w3.org/1999/xhtml">x</div></foreignObject><g/>',
            "",
            "div",
        ),
        ('<a href="https://evil.example"><rect/></a><g/>', "", "evil"),
        ('<image href="https://evil.example/x.png"/><g/>', "", "evil"),
        ('<use xlink:href="https://evil.example/s.svg#a"/>', "", "evil"),
        ('<use href="javascript:alert(1)"/>', "", "javascript"),
        ('<rect fill="url(https://evil.example/p)"/>', "", "evil"),
        ("<rect style=\"fill: url('https://evil.example/p')\"/>", "", "evil"),
        ("<style>@import url(https://evil.example/c.css);</style><g/>", "", "evil"),
        ("<style>rect { background: u\\72l(https://evil.example) }</style><g/>", "", "evil"),
        ('<animate attributeName="href" to="javascript:alert(1)"/><set/><g/>', "", "animate"),
        ('<rect fill="java&#10;script:alert(1)"/>', "", "script"),
        ('<iframe src="https://evil.example"/><g/>', "", "iframe"),
    ],
)
def test_scripts_handlers_and_external_references_are_stripped(
    body: str, attributes: str, gone: str
) -> None:
    assert gone not in _clean(body, attributes).split("?>", 1)[1].replace("xmlns", "")


def test_internal_references_are_kept() -> None:
    text = _clean('<g id="a"/><use href="#a"/><use xlink:href="#a"/><rect fill="url(#g)"/>')
    assert 'href="#a"' in text and 'xlink:href="#a"' in text and 'fill="url(#g)"' in text


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("no es xml", "no es un SVG válido"),
        ("<svg", "no es un SVG válido"),
        ("<html><body/></html>", "raíz <svg>"),
        (f"<!DOCTYPE svg [<!ENTITY a 'x'>]><svg {NS}>&a;</svg>", "DOCTYPE"),
        (f"<svg {NS}><script>x</script></svg>", "nada que dibujar"),
        (f"<svg {NS}>" + " " * MAX_SVG_BYTES + "</svg>", "demasiado grande"),
    ],
)
def test_input_that_is_not_a_usable_drawing_is_refused(content: str, message: str) -> None:
    with pytest.raises(SvgError, match=message):
        sanitize_svg(content)


def test_an_svg_without_a_namespace_is_put_in_the_svg_namespace() -> None:
    text = sanitize_svg('<svg viewBox="0 0 1 1"><circle r="1"/></svg>').decode()
    assert 'xmlns="http://www.w3.org/2000/svg"' in text and "<circle" in text


@pytest.mark.parametrize(
    "css",
    [
        'rect { background: image-set("http://evil.example/x.png" 1x) }',
        "rect { background: -webkit-image-set('https://evil.example/x.png' 1x) }",
        'rect { background: IMAGE-SET("//evil.example/x.png" 1x) }',
        'rect { background-image: src("https://evil.example/x.png") }',
        '@font-face { font-family: f; src: local(Arial), "https://evil.example/f.woff" }',
        "@font-face { font-family: f; src: local(Arial) }",
        "rect { background: element(#a) }",
        "rect { background: cross-fade(50% x, 50% y) }",
        "rect { background: paint(worklet) }",
        "rect { mask: image('https://evil.example/m.png') }",
        "@namespace x 'https://evil.example/';",
        '@document url-prefix("https://evil.example/") { rect { fill: red } }',
        'rect { cursor: "https://evil.example/c.cur" }',
        "rect { fill: some-future-fetch(x) }",
    ],
)
def test_css_that_could_fetch_anything_is_dropped(css: str) -> None:
    # In a stylesheet the whole text goes; in a `style=` attribute the attribute goes.
    in_sheet = _clean(f"<style>{css}</style><g/>")
    assert "<style />" in in_sheet or "<style></style>" in in_sheet
    assert "style=" not in _clean(f"<rect style='{css.replace(chr(39), chr(34))}'/>")


def test_a_fetching_function_in_a_presentation_attribute_is_dropped() -> None:
    text = _clean('<rect mask="image-set(\'https://evil.example/m.png\' 1x)" width="2"/>')
    assert "mask=" not in text and 'width="2"' in text


def test_harmless_css_is_kept() -> None:
    css = (
        "rect { fill: rgb(10, 20, 30); stroke: url(#g); transform: rotate(45deg) "
        "translate(calc(1px + 2px), 0); filter: drop-shadow(1px 1px 2px hsl(0 0% 0%)) } "
        "@media (min-width: 10px) { text { font-family: 'DejaVu Sans' } }"
    )
    assert css in _clean(f"<style>{css}</style><g/>")
    assert 'transform="rotate(30) scale(2)"' in _clean('<g transform="rotate(30) scale(2)"/>')


UTF16_ENTITY = f'<!DOCTYPE s [<!ENTITY a "zzz">]><svg {NS}><text>&a;</text></svg>'


@pytest.mark.parametrize(
    "data",
    [
        UTF16_ENTITY.encode("utf-16"),  # with BOM
        UTF16_ENTITY.encode("utf-16-le"),
        UTF16_ENTITY.encode("utf-16-be"),
        UTF16_ENTITY.encode("utf-32"),
        ('<?xml version="1.0" encoding="UTF-16"?>' + UTF16_ENTITY).encode("utf-16"),
        f'<?xml version="1.0" encoding="UTF-16"?><svg {NS}><g/></svg>'.encode("utf-16"),
        f'<?xml version="1.0" encoding="ISO-8859-1"?><svg {NS}><g/></svg>'.encode(),
        f'<?xml version="1.0" encoding="UTF-7"?><svg {NS}><g/></svg>'.encode(),
        f"<svg {NS}><text>\xe9</text></svg>".encode("latin-1"),  # not valid UTF-8
    ],
)
def test_input_that_is_not_utf8_is_refused(data: bytes) -> None:
    with pytest.raises(SvgError, match="UTF-8"):
        sanitize_svg(data)


def test_a_str_that_has_no_utf8_form_is_refused() -> None:
    content = f"<svg {NS}><text>\ud800</text></svg>"  # lone surrogate
    with pytest.raises(SvgError, match="UTF-8"):
        sanitize_svg(content)
    assert is_safe_svg(content) is False  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "content",
    [
        UTF16_ENTITY,
        UTF16_ENTITY.encode(),
        b"\xef\xbb\xbf" + UTF16_ENTITY.encode(),
        f'<?xml version="1.0"?>\n<!doctype svg><svg {NS}><g/></svg>',
        f"<!--x--><!DOCTYPE svg SYSTEM 'file:///etc/passwd'><svg {NS}><g/></svg>",
    ],
)
def test_declarations_are_refused_whatever_the_wrapping(content: str | bytes) -> None:
    with pytest.raises(SvgError, match="DOCTYPE"):
        sanitize_svg(content)


def test_utf8_input_with_a_bom_or_a_utf8_declaration_is_accepted() -> None:
    body = f"<svg {NS}><text>áé</text></svg>"
    expected = sanitize_svg(body)
    assert sanitize_svg(b"\xef\xbb\xbf" + body.encode()) == expected
    assert sanitize_svg(f'<?xml version="1.0" encoding="utf-8"?>{body}'.encode()) == expected


@pytest.fixture
def topic(tmp_vault: Vault) -> Vault:
    create_subject(tmp_vault, "Mates")
    create_topic(tmp_vault, "mates", "Derivadas")
    return tmp_vault


def test_an_svg_image_is_stored_sanitized_listed_read_and_removed(topic: Vault) -> None:
    raw = f'<svg {NS} onload="x()"><script>x()</script><circle r="3"/></svg>'

    path = put_source(
        topic, "mates", "derivadas", "images", "diagram.svg", raw, {"origin": "drawn"}
    )

    assert path.name == "img-001.svg"
    relative = path.relative_to(topic.path).as_posix()
    stored = read_source(topic, relative)
    assert stored.media_type == "image/svg+xml" and stored.content == sanitize_svg(raw)
    assert stored.meta == {"origin": "drawn"}
    assert [s.path for s in list_sources(topic, "mates", "derivadas")] == [relative]
    remove_source(topic, relative)
    assert list_sources(topic, "mates", "derivadas") == []


def test_an_unusable_svg_image_writes_nothing(topic: Vault) -> None:
    with pytest.raises(SvgError):
        put_source(topic, "mates", "derivadas", "images", "d.svg", "<p/>", {"origin": "drawn"})
    assert list_sources(topic, "mates", "derivadas") == []


def test_a_pasted_image_is_never_an_svg(topic: Vault) -> None:
    with pytest.raises(SourceError):
        put_pasted_image(topic, "mates", "derivadas", f"<svg {NS}/>".encode(), "image/svg+xml")
