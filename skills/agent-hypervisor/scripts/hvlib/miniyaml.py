"""Tiny YAML subset parser (stdlib only).

hv ships without third-party dependencies, and PyYAML is not guaranteed on a
host, so artifact front-matter is read with this parser. The subset is
deliberately narrow and fails loudly on anything it does not understand
instead of guessing:

  * mappings and nested mappings (indentation based)
  * lists of scalars and lists of mappings
  * double- or single-quoted scalars, bare scalars, inline lists of scalars
    (``[a, "b"]``)
  * ``#`` comment lines and blank lines

Not supported, on purpose: anchors, multi-document files, block scalars
(``|``/``>``), flow mappings (``{a: b}``).
"""

from __future__ import annotations

import re

__all__ = ["MiniYAMLError", "parse", "parse_front_matter"]


class MiniYAMLError(ValueError):
    """Raised when the input falls outside the supported YAML subset."""


_KEY_RE = re.compile(r"^([A-Za-z0-9_.\-]+)\s*:(?:\s+(.*))?$")


def _strip_comment(line: str) -> str:
    """Drop a trailing ``#`` comment that is outside quotes."""
    quote = None
    escaped = False
    for idx, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ('"', "'"):
            quote = char
            continue
        if char == "#" and (idx == 0 or line[idx - 1] in " \t"):
            return line[:idx]
    return line


def _tokenize(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise MiniYAMLError("tabs are not allowed for indentation")
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        lines.append((indent, line.strip()))
    return lines


def _split_kv(content: str) -> tuple[str, str]:
    match = _KEY_RE.match(content)
    if not match:
        raise MiniYAMLError(f"expected 'key: value', got {content!r}")
    return match.group(1), (match.group(2) or "").strip()


def _split_flow(text: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quote = None
    escaped = False
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and quote == '"':
            current.append(char)
            escaped = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in ('"', "'"):
            quote = char
            current.append(char)
            continue
        if char == ",":
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return [part for part in (item.strip() for item in parts) if part != ""]


def _scalar(value: str):
    value = value.strip()
    if not value:
        return None
    if value in ("[]", "[ ]"):
        return []
    if value in ("{}", "{ }"):
        return {}
    if value.startswith("[") and value.endswith("]"):
        return [_scalar(item) for item in _split_flow(value[1:-1].strip())]
    if value.startswith('"'):
        if not value.endswith('"') or len(value) < 2:
            raise MiniYAMLError(f"unterminated double-quoted scalar: {value!r}")
        body = value[1:-1]
        return body.replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n")
    if value.startswith("'"):
        if not value.endswith("'") or len(value) < 2:
            raise MiniYAMLError(f"unterminated single-quoted scalar: {value!r}")
        return value[1:-1].replace("''", "'")
    if value.startswith("[") or value.startswith("{"):
        raise MiniYAMLError(f"flow collections are not supported: {value!r}")
    if value in ("true", "True"):
        return True
    if value in ("false", "False"):
        return False
    if value in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value


def _parse_map(lines: list[tuple[int, str]], index: int, indent: int):
    out: dict = {}
    while index < len(lines) and lines[index][0] == indent:
        if lines[index][1].startswith("- "):
            break
        key, raw = _split_kv(lines[index][1])
        if raw == "":
            index += 1
            if index < len(lines) and lines[index][0] > indent:
                out[key], index = _parse_block(lines, index, lines[index][0])
            elif (
                index < len(lines)
                and lines[index][0] == indent
                and lines[index][1].startswith("- ")
            ):
                out[key], index = _parse_list(lines, index, indent)
            else:
                out[key] = None
        else:
            out[key] = _scalar(raw)
            index += 1
    return out, index


def _parse_list(lines: list[tuple[int, str]], index: int, indent: int):
    out: list = []
    while index < len(lines) and lines[index][0] == indent and lines[index][1].startswith("- "):
        content = lines[index][1][2:].strip()
        if not content:
            index += 1
            if index < len(lines) and lines[index][0] > indent:
                value, index = _parse_block(lines, index, lines[index][0])
            else:
                value = None
            out.append(value)
            continue
        if _KEY_RE.match(content):
            item: dict = {}
            key, raw = _split_kv(content)
            if raw == "":
                index += 1
                if index < len(lines) and lines[index][0] > indent:
                    item[key], index = _parse_block(lines, index, lines[index][0])
                else:
                    item[key] = None
            else:
                item[key] = _scalar(raw)
                index += 1
            if index < len(lines) and lines[index][0] > indent:
                if lines[index][1].startswith("- "):
                    raise MiniYAMLError(
                        "a list item cannot start a nested list without a key"
                    )
                rest, index = _parse_map(lines, index, lines[index][0])
                for extra_key, extra_value in rest.items():
                    if extra_key in item:
                        raise MiniYAMLError(f"duplicate key {extra_key!r} in list item")
                    item[extra_key] = extra_value
            out.append(item)
        else:
            out.append(_scalar(content))
            index += 1
    return out, index


def _parse_block(lines: list[tuple[int, str]], index: int, indent: int):
    if lines[index][1].startswith("- "):
        return _parse_list(lines, index, indent)
    return _parse_map(lines, index, indent)


def parse(text: str) -> dict:
    """Parse a YAML subset into plain Python objects."""
    lines = _tokenize(text)
    if not lines:
        return {}
    value, index = _parse_block(lines, 0, lines[0][0])
    if index != len(lines):
        raise MiniYAMLError(
            f"unexpected line {index + 1}: {lines[index][1]!r} (indent {lines[index][0]})"
        )
    if not isinstance(value, dict):
        raise MiniYAMLError("top level must be a mapping")
    return value


FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)


def parse_front_matter(text: str) -> tuple[dict, str]:
    """Split leading ``---`` front-matter from a document.

    Returns ``(mapping, body)``; a document without front-matter yields
    ``({}, text)``. A malformed block raises :class:`MiniYAMLError`.
    """
    match = FRONT_MATTER_RE.match(text)
    if not match:
        return {}, text
    return parse(match.group(1)), text[match.end():]
