"""Immutable OCR model shared by all detectors. Coordinates are normalized."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Word:
    page: int
    index: int
    text: str
    confidence: float
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class Line:
    words: tuple[Word, ...]

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)

    @property
    def x(self) -> float:
        return min(word.x for word in self.words)

    @property
    def y(self) -> float:
        return min(word.y for word in self.words)

    @property
    def bottom(self) -> float:
        return max(word.y + word.h for word in self.words)

    def words_for_span(self, start: int, end: int) -> tuple[Word, ...]:
        return words_for_span(self.words, start, end)


def words_for_span(words, start, end):
    selected = []
    offset = 0
    for word in words:
        stop = offset + len(word.text)
        if start < stop and end > offset:
            selected.append(word)
        offset = stop + 1
    return tuple(selected)


@dataclass(frozen=True)
class Page:
    index: int
    width: int
    height: int
    words: tuple[Word, ...]
    lines: tuple[Line, ...]

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)

    def find(self, pattern: str, *, field: str = "text", flags: int = re.IGNORECASE):
        """Map regex matches back to words; zero-width matches are ignored."""
        for match in re.finditer(pattern, self.text, flags):
            if match.end() > match.start():
                yield Finding(
                    field, words_for_span(self.words, match.start(), match.end())
                )


@dataclass(frozen=True)
class Document:
    name: str
    sha256: str
    pages: tuple[Page, ...]

    @property
    def text(self) -> str:
        return "\n\n".join(page.text for page in self.pages)

    @classmethod
    def from_ocr(cls, name, sha256, pages):
        result = []
        for page in pages:
            raw = page["words"]
            words = tuple(
                Word(
                    page["index"],
                    i,
                    word["text"],
                    word.get("confidence", -1),
                    word["x"],
                    word["y"],
                    word["w"],
                    word["h"],
                )
                for i, word in enumerate(raw)
            )
            groups = {}
            for word, item in zip(words, raw, strict=True):
                if all(key in item for key in ("block", "paragraph", "line")):
                    key = (item["block"], item["paragraph"], item["line"])
                else:
                    # Compatibility with older OCR JSON and simple detector fixtures.
                    key = round(word.y / max(word.h / 2, 0.005))
                groups.setdefault(key, []).append(word)
            lines = []
            for group in groups.values():
                segment = []
                for word in sorted(group, key=lambda w: w.x):
                    # OCR sometimes combines two columns into one line.
                    if segment and word.x - (segment[-1].x + segment[-1].w) > 0.08:
                        lines.append(Line(tuple(segment)))
                        segment = []
                    segment.append(word)
                if segment:
                    lines.append(Line(tuple(segment)))
            result.append(
                Page(
                    page["index"],
                    page["width"],
                    page["height"],
                    words,
                    tuple(sorted(lines, key=lambda line: (line.y, line.x))),
                )
            )
        return cls(name, sha256, tuple(result))


@dataclass(frozen=True)
class Finding:
    """A semantic decision. Return original Word objects, not pixel rectangles."""

    field: str
    words: tuple[Word, ...]
    reason: str = ""
