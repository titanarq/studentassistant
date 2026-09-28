"""Sanitizing an SVG drawing before it is stored or served (#511).

The tutor-editor draws diagrams as SVG, and an SVG is LLM output: it may carry a `<script>`, an
`onload=` handler, a link to an external resource or CSS that fetches one. `sanitize_svg` parses
the drawing and rebuilds it from an allow-list, so what reaches the vault (and what the read API
serves as `image/svg+xml`) is only static vector drawing:

- the input is parsed with the standard library after refusing any DOCTYPE or entity declaration
  (no entity expansion, no external entity) and anything above `MAX_SVG_BYTES`;
- the root must be `<svg>` (in the SVG namespace, or without a namespace: it is put in it);
- only the drawing elements of `ALLOWED_ELEMENTS` are kept; any other element (`script`,
  `foreignObject`, `image`, `a`, `iframe`, the animation elements, anything in another namespace)
  is dropped with its whole subtree;
- attributes are kept only when unqualified (plus `xlink:href`, `xml:space`, `xml:lang`), not an
  event handler (`on*`), and with a value that holds no `javascript:`/`vbscript:`/`data:` scheme,
  no `url(...)` other than `url(#id)` and no `@import`; `href`/`xlink:href` only as `#id`;
- `<style>` text and `style` attributes are CSS: kept only without `@import`, external `url()`,
  `expression(`, a backslash escape or a script scheme, else dropped;
- comments and processing instructions are dropped.

The output is UTF-8 XML (`<?xml ...?>` header, SVG as the default namespace), deterministic for a
given input. `SvgError` (a `ValueError` with a Spanish message) is raised for input that is not a
usable drawing; nothing here touches the disk or the network.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"
SVG_MEDIA_TYPE = "image/svg+xml"
SVG_EXTENSION = ".svg"
MAX_SVG_BYTES = 256 * 1024
"""The largest SVG accepted, before sanitizing (a diagram, not an embedded photo)."""

ALLOWED_ELEMENTS = frozenset(
    {
        "svg",
        "g",
        "defs",
        "title",
        "desc",
        "symbol",
        "use",
        "path",
        "rect",
        "circle",
        "ellipse",
        "line",
        "polyline",
        "polygon",
        "text",
        "tspan",
        "textPath",
        "marker",
        "linearGradient",
        "radialGradient",
        "stop",
        "pattern",
        "clipPath",
        "mask",
        "style",
        "filter",
        "feGaussianBlur",
        "feOffset",
        "feBlend",
        "feFlood",
        "feComposite",
        "feMerge",
        "feMergeNode",
        "feDropShadow",
        "feColorMatrix",
    }
)
_KEPT_QUALIFIED = {f"{{{XLINK_NS}}}href", f"{{{XML_NS}}}space", f"{{{XML_NS}}}lang"}
_DECLARATION = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_SCHEME = re.compile(r"(javascript|vbscript|data)\s*:", re.IGNORECASE)
_URL = re.compile(r"url\s*\(\s*['\"]?\s*(.?)", re.IGNORECASE)
# A backslash is a CSS escape (`u\\72l(`), which could spell any of these: refused outright.
_CSS_DANGER = re.compile(r"@import|expression\s*\(|behavior\s*:|-moz-binding|\\", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f\s]+")

INVALID_SVG_MESSAGE = "El dibujo no es un SVG válido."
TOO_LARGE_MESSAGE = f"El dibujo SVG es demasiado grande (máximo {MAX_SVG_BYTES // 1024} KiB)."
DECLARATION_MESSAGE = "El SVG no puede llevar declaraciones DOCTYPE ni ENTITY."
NOT_SVG_MESSAGE = "El dibujo debe tener un elemento raíz <svg>."
EMPTY_SVG_MESSAGE = "El dibujo SVG no tiene nada que dibujar."

ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)


class SvgError(ValueError):
    """The input is not an SVG drawing that can be kept (Spanish message)."""


def sanitize_svg(content: bytes | str) -> bytes:
    """The drawing rebuilt from the allow-list, as UTF-8 XML bytes.

    Raises:
        SvgError: the input is too large, declares a DOCTYPE or entity, is not well-formed XML,
            its root is not `<svg>`, or nothing drawable is left once sanitized.
    """
    data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    if len(data) > MAX_SVG_BYTES:
        raise SvgError(TOO_LARGE_MESSAGE)
    if _DECLARATION.search(data):
        raise SvgError(DECLARATION_MESSAGE)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise SvgError(INVALID_SVG_MESSAGE) from error
    if _local(root.tag) != "svg" or _namespace(root.tag) not in ("", SVG_NS):
        raise SvgError(NOT_SVG_MESSAGE)
    clean = _clean(root)
    if clean is None or (len(clean) == 0 and not (clean.text or "").strip()):
        raise SvgError(EMPTY_SVG_MESSAGE)
    return ET.tostring(clean, encoding="utf-8", xml_declaration=True)


def is_safe_svg(content: bytes) -> bool:
    """Whether `content` is already what `sanitize_svg` makes of it (a stored, sanitized SVG)."""
    try:
        return sanitize_svg(content) == content
    except SvgError:
        return False


def _namespace(tag: object) -> str:
    if not isinstance(tag, str):
        return "\x00"  # a comment or processing instruction
    return tag[1:].partition("}")[0] if tag.startswith("{") else ""


def _local(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rpartition("}")[2]


def _clean(element: ET.Element) -> ET.Element | None:
    """A sanitized copy of `element` and its kept children, or `None` when it is dropped."""
    name = _local(element.tag)
    if _namespace(element.tag) not in ("", SVG_NS) or name not in ALLOWED_ELEMENTS:
        return None
    copy = ET.Element(f"{{{SVG_NS}}}{name}")
    for key, value in element.attrib.items():
        kept = _attribute(key, value)
        if kept is not None:
            copy.set(key, kept)
    if name == "style":
        css = "".join(element.itertext())
        copy.text = css if _safe_css(css) else ""
        return copy
    copy.text = element.text
    for child in element:
        cleaned = _clean(child)
        if cleaned is not None:
            cleaned.tail = child.tail
            copy.append(cleaned)
        elif child.tail and child.tail.strip():
            # The dropped child's trailing text still belongs to this element.
            if len(copy):
                copy[-1].tail = (copy[-1].tail or "") + child.tail
            else:
                copy.text = (copy.text or "") + child.tail
    return copy


def _attribute(key: str, value: str) -> str | None:
    """The value to keep for one attribute, or `None` when it is dropped."""
    if key.startswith("{"):
        if key not in _KEPT_QUALIFIED:
            return None
    elif ":" in key:
        return None
    local = _local(key).lower()
    if local.startswith("on"):
        return None
    squeezed = _CONTROL.sub("", value)
    if local == "href":
        return value if squeezed.startswith("#") else None
    if _SCHEME.search(squeezed):
        return None
    if local == "style":
        return value if _safe_css(value) else None
    if _CSS_DANGER.search(value) or not _internal_urls(value):
        return None
    return value


def _internal_urls(value: str) -> bool:
    """Whether every `url(...)` in `value` points inside the drawing (`url(#id)`)."""
    return all(match.group(1) == "#" for match in _URL.finditer(value))


def _safe_css(css: str) -> bool:
    squeezed = _CONTROL.sub("", css)
    return not (_SCHEME.search(squeezed) or _CSS_DANGER.search(css) or not _internal_urls(css))


__all__ = [
    "ALLOWED_ELEMENTS",
    "MAX_SVG_BYTES",
    "SVG_EXTENSION",
    "SVG_MEDIA_TYPE",
    "SVG_NS",
    "SvgError",
    "is_safe_svg",
    "sanitize_svg",
]
