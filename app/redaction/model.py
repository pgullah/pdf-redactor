"""Immutable OCR model shared by all detectors. Coordinates are normalized."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Character:
    text: str
    x: float
    y: float
    w: float
    h: float


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
    characters: tuple[Character, ...] = ()


@dataclass(frozen=True)
class WordSlice:
    """An exact character span backed by the original OCR word and glyph boxes."""

    word: Word
    start: int
    end: int

    def __post_init__(self):
        if not (
            type(self.start) is int
            and type(self.end) is int
            and 0 <= self.start < self.end <= len(self.word.text)
        ):
            raise ValueError("Invalid OCR character span.")
        if (
            len(self.word.characters) != len(self.word.text)
            or "".join(c.text for c in self.word.characters) != self.word.text
        ):
            raise ValueError(
                "Cannot locate characters in a merged OCR word accurately "
                f"(page {self.word.page + 1}, OCR word ID {self.word.index}). "
                "Re-run OCR on a clearer scan or use an explicit region for this field."
            )

    @property
    def page(self):
        return self.word.page

    @property
    def index(self):
        return self.word.index

    @property
    def text(self):
        return self.word.text[self.start : self.end]

    @property
    def confidence(self):
        return self.word.confidence

    @property
    def characters(self):
        return self.word.characters[self.start : self.end]

    @property
    def x(self):
        return min(c.x for c in self.characters)

    @property
    def y(self):
        return min(c.y for c in self.characters)

    @property
    def w(self):
        return max(c.x + c.w for c in self.characters) - self.x

    @property
    def h(self):
        return max(c.y + c.h for c in self.characters) - self.y

    @property
    def left_limit(self):
        if not self.start:
            return 0.0
        previous = self.word.characters[self.start - 1]
        return min(self.x, (previous.x + previous.w + self.x) / 2)

    @property
    def right_limit(self):
        if self.end == len(self.word.text):
            return 1.0
        following = self.word.characters[self.end]
        return max(self.x + self.w, (self.x + self.w + following.x) / 2)


def slice_word(word, start, end):
    if start == 0 and end == len(word.text):
        return word
    if isinstance(word, WordSlice):
        return WordSlice(word.word, word.start + start, word.start + end)
    return WordSlice(word, start, end)


@dataclass(frozen=True)
class Line:
    words: tuple[Word | WordSlice, ...]

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

    def words_for_span(self, start: int, end: int, *, precise=False):
        return words_for_span(self.words, start, end, precise=precise)


def words_for_span(words, start, end, *, precise=False):
    selected = []
    offset = 0
    for word in words:
        stop = offset + len(word.text)
        if start < stop and end > offset:
            selected.append(
                slice_word(
                    word, max(0, start - offset), min(len(word.text), end - offset)
                )
                if precise
                else word
            )
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
                    tuple(Character(**char) for char in word.get("characters", [])),
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
    words: tuple[Word | WordSlice, ...]
    reason: str = ""
