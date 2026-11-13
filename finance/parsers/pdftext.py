"""Word positions from a PDF (pdfplumber) and the line grouping every deterministic parser starts from."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Word:
    x0: float
    x1: float
    top: float
    text: str


def read_pages(path: Path, password: str | None) -> list[list[Word]]:
    import pdfplumber
    with pdfplumber.open(str(path), password=password or None) as pdf:
        return [[Word(w["x0"], w["x1"], w["top"], w["text"]) for w in page.extract_words()] for page in pdf.pages]


def group_lines(words: list[Word]) -> list[list[Word]]:
    """Words on the same baseline (within 1.5pt) form a line; lines run top to bottom, words left to right."""
    out: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.top, w.x0)):
        if out and abs(w.top - out[-1][0].top) <= 1.5:
            out[-1].append(w)
        else:
            out.append([w])
    return [sorted(line, key=lambda w: w.x0) for line in out]


def line_text(line: list[Word]) -> str:
    return " ".join(w.text for w in line)
