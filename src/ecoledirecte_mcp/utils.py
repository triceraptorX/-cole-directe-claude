"""Petites fonctions utilitaires : base64, HTML, nombres au format français."""

from __future__ import annotations

import base64
import binascii
import html
import re
from html.parser import HTMLParser


def b64_decode(value: str | None) -> str:
    """Décode une chaîne base64 renvoyée par ÉcoleDirecte (padding toléré)."""
    if not value:
        return ""
    cleaned = value.strip().rstrip("=")
    cleaned += "=" * (-len(cleaned) % 4)
    try:
        return base64.b64decode(cleaned).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return value


def b64_encode(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


class _TextExtractor(HTMLParser):
    _BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "li":
            self.parts.append("\n- ")
        elif tag in self._BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.parts.append(f"[{href}] ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK_TAGS and tag != "br":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(value: str | None) -> str:
    """Convertit un fragment HTML en texte lisible."""
    if not value:
        return ""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    text = html.unescape("".join(parser.parts)).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def b64_html_to_text(value: str | None) -> str:
    return html_to_text(b64_decode(value))


def parse_fr_number(value: str | float | int | None) -> float | None:
    """Convertit "12,5" ou "12.5" en float ; renvoie None si ce n'est pas un nombre."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value.strip().replace(",", "."))
    except (ValueError, AttributeError):
        return None
