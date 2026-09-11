"""Stage 03: map invoice line descriptions to rate-card keys.

The warehouse writes "Pick & pack - per order"; the card is keyed
``pickpack.per_order``. Bridging that is the only place in v1 where a model
would earn its place, so the ladder is explicit:

1. deterministic alias match against the card (free, exact, auditable)
2. a :class:`LineMapper`, retried against progressively cleaner readings of
   the description until the retry budget runs out
3. anything still unmapped becomes UNKNOWN and reaches a human

:class:`FuzzyMapper` ships as the default mapper: it scores descriptions
against card aliases without a model, which covers most real wording drift.
An LLM mapper implements the same protocol and slots in beside it.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, Sequence

from .models import InvoiceLine, RateCard, RateCardEntry

#: Below this, a suggestion is not trusted: the line is left unmapped and
#: additionally carries LOW_CONFIDENCE_EXTRACTION.
CONFIDENCE_THRESHOLD = Decimal("0.80")

#: A rejected suggestion below this scored so poorly that "we nearly had it"
#: would be a lie. Those lines are simply UNKNOWN; only the band between this
#: and CONFIDENCE_THRESHOLD is worth telling a reviewer we were close.
PLAUSIBLE_FLOOR = Decimal("0.55")

#: Retry budget. Two refinements after the first attempt, then escalate to a
#: human rather than loop. Agent loops that retry without a ceiling are how
#: this kind of pipeline quietly becomes expensive.
MAX_RETRIES = 2

_PUNCT = re.compile(r"[^a-z0-9]+")
_NOISE = re.compile(
    r"\b("
    r"\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?"        # dates
    r"|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
    r"|january|february|march|april|june|july|august|september"
    r"|october|november|december"
    r"|q[1-4]|fy\d{2,4}|\d{4}"                  # periods and years
    r"|ref|inv|invoice|no|line|item|total|charge|charges|fee|fees"
    r")\b"
)


def canonical(text: str) -> str:
    """Fold a description to a comparable form.

    Strips accents, punctuation and case so that "Pick & Pack · per order" and
    "pick and pack per order" collapse to the same string.
    """
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = folded.lower().replace("&", " and ")
    return _PUNCT.sub(" ", folded).strip()


def refinements(description: str) -> list[str]:
    """Progressively cleaner readings of one description.

    Attempt 0 is the description as written. Attempt 1 drops billing noise --
    dates, periods, the words "fee" and "charge" that carry no meaning against
    a rate card. Attempt 2 keeps only alphabetic tokens, which is the last
    honest simplification before guessing.
    """
    base = canonical(description)
    attempts = [base]

    stripped = _PUNCT.sub(" ", _NOISE.sub(" ", base)).strip()
    if stripped and stripped != base:
        attempts.append(stripped)

    alpha = " ".join(t for t in stripped.split() if t.isalpha())
    if alpha and alpha not in attempts:
        attempts.append(alpha)

    return attempts[: MAX_RETRIES + 1]


@dataclass(frozen=True)
class Suggestion:
    rate_key: str
    confidence: Decimal
    rationale: str


class LineMapper(Protocol):
    """The seam. ``attempt`` is the retry index, 0 for the first try."""

    def suggest(
        self,
        description: str,
        candidates: Sequence[RateCardEntry],
        attempt: int = 0,
    ) -> Suggestion | None: ...


class NullMapper:
    """No model, no guesses. Alias matching only."""

    def suggest(
        self,
        description: str,
        candidates: Sequence[RateCardEntry],
        attempt: int = 0,
    ) -> Suggestion | None:
        return None


def _tokens(text: str) -> set[str]:
    return {t for t in text.split() if len(t) > 1}


def _score(text: str, alias: str) -> Decimal:
    """Similarity in [0, 1]: sequence shape plus token overlap.

    Sequence ratio alone rewards coincidental letter runs; token F1 alone
    ignores word order. Weighting both is steadier than either.
    """
    ratio = difflib.SequenceMatcher(None, text, alias).ratio()

    left, right = _tokens(text), _tokens(alias)
    if left and right:
        shared = len(left & right)
        precision = shared / len(left)
        recall = shared / len(right)
        f1 = 0.0 if shared == 0 else 2 * precision * recall / (precision + recall)
    else:
        f1 = 0.0

    return Decimal(str(round(0.55 * ratio + 0.45 * f1, 4)))


@dataclass
class FuzzyMapper:
    """Deterministic scoring against card aliases. No model, no network.

    Ambiguity lowers confidence rather than being resolved by coin flip: when
    the runner-up scores nearly as well, the margin is subtracted from the
    result, which usually pushes it under the threshold and into a human's
    queue.
    """

    margin_weight: Decimal = Decimal("0.5")

    def suggest(
        self,
        description: str,
        candidates: Sequence[RateCardEntry],
        attempt: int = 0,
    ) -> Suggestion | None:
        text = canonical(description)
        if not text or not candidates:
            return None

        scored: list[tuple[Decimal, str, str]] = []
        for entry in candidates:
            best = max(
                (
                    (_score(text, canonical(alias)), alias)
                    for alias in (entry.key, entry.label, *entry.aliases)
                    if canonical(alias)
                ),
                default=(Decimal(0), ""),
            )
            scored.append((best[0], entry.key, best[1]))

        scored.sort(key=lambda row: row[0], reverse=True)
        top = scored[0]
        if top[0] <= 0:
            return None

        runner_up = scored[1][0] if len(scored) > 1 else Decimal(0)
        gap = top[0] - runner_up
        confidence = top[0]
        if gap < Decimal("0.15"):
            # Two card entries fit almost equally well. Say so by discounting.
            confidence -= (Decimal("0.15") - gap) * self.margin_weight

        confidence = max(Decimal(0), min(Decimal(1), confidence))
        return Suggestion(
            rate_key=top[1],
            confidence=confidence,
            rationale=(
                f'scored {top[0]} against alias "{top[2]}" '
                f"(runner-up {runner_up}, attempt {attempt})"
            ),
        )


def build_alias_index(card: RateCard) -> dict[str, str]:
    """Canonical alias -> rate key, including each entry's key and label."""
    index: dict[str, str] = {}
    for entry in card.entries.values():
        for candidate in (entry.key, entry.label, *entry.aliases):
            key = canonical(candidate)
            if key:
                index.setdefault(key, entry.key)
    return index


@dataclass
class Normalizer:
    card: RateCard
    mapper: LineMapper = NullMapper()
    threshold: Decimal = CONFIDENCE_THRESHOLD

    def __post_init__(self) -> None:
        self._index = build_alias_index(self.card)

    def _alias_match(self, description: str) -> str | None:
        text = canonical(description)
        if not text:
            return None
        if text in self._index:
            return self._index[text]
        # An alias fully contained in the description is still exact enough to
        # trust: "storage per pallet mo aug" contains "storage per pallet mo".
        containment = [
            (alias, key)
            for alias, key in self._index.items()
            if len(alias) >= 8 and alias in text
        ]
        if len(containment) == 1:
            return containment[0][1]
        if containment:
            # Ambiguous: the longest alias wins only if it is strictly longer
            # than the runner-up, otherwise leave it for a human.
            containment.sort(key=lambda pair: len(pair[0]), reverse=True)
            if len(containment[0][0]) > len(containment[1][0]):
                return containment[0][1]
        return None

    def apply(self, line: InvoiceLine) -> InvoiceLine:
        key = self._alias_match(line.description)
        if key is not None:
            line.rate_key = key
            line.mapped_by = "alias"
            line.map_confidence = Decimal("1.0")
            line.map_rationale = "exact alias match on the buy card"
            return line

        candidates = list(self.card.entries.values())
        best: Suggestion | None = None
        attempts = refinements(line.description)

        for attempt, text in enumerate(attempts):
            suggestion = self.mapper.suggest(text, candidates, attempt=attempt)
            if suggestion is None:
                continue
            if best is None or suggestion.confidence > best.confidence:
                best = suggestion
            if suggestion.confidence >= self.threshold:
                line.rate_key = suggestion.rate_key
                line.mapped_by = "model" if attempt == 0 else f"model:retry{attempt}"
                line.map_confidence = suggestion.confidence
                line.map_rationale = suggestion.rationale
                return line

        if best is None:
            line.mapped_by = None
            line.map_rationale = "no alias on the buy card matches this description"
            return line

        # Deliberately left unmapped: a low-confidence guess that silently
        # priced a line would be worse than admitting we do not know.
        line.map_confidence = best.confidence
        line.map_rationale = (
            f"{best.rationale}; below threshold {self.threshold} after "
            f"{len(attempts)} attempt(s)"
        )
        line.mapped_by = "model-rejected"
        return line

    def apply_all(self, lines: Sequence[InvoiceLine]) -> list[InvoiceLine]:
        return [self.apply(line) for line in lines]
