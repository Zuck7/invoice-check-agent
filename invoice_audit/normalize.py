"""Stage 03: map invoice line descriptions to rate-card keys.

The warehouse writes "Pick & pack - per order"; the card is keyed
``pickpack.per_order``. Bridging that is the only place in v1 where a model
would earn its place, so the seam is explicit:

1. deterministic alias match against the card (free, exact, auditable)
2. a :class:`LineMapper` for anything left over (phase 2)
3. anything still unmapped becomes UNKNOWN and reaches a human

The default mapper does nothing, so v1 runs fully deterministic and is honest
about what it could not identify.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, Sequence

from .models import InvoiceLine, RateCard, RateCardEntry

#: Below this, a model suggestion is not trusted: the line is left unmapped and
#: additionally carries LOW_CONFIDENCE_EXTRACTION.
CONFIDENCE_THRESHOLD = Decimal("0.80")

#: Phase 2 retry budget. Two refinements, then escalate rather than loop.
MAX_RETRIES = 2

_PUNCT = re.compile(r"[^a-z0-9]+")


def canonical(text: str) -> str:
    """Fold a description to a comparable form.

    Strips accents, punctuation and case so that "Pick & Pack · per order" and
    "pick and pack per order" collapse to the same string.
    """
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = folded.lower().replace("&", " and ")
    return _PUNCT.sub(" ", folded).strip()


@dataclass(frozen=True)
class Suggestion:
    rate_key: str
    confidence: Decimal
    rationale: str


class LineMapper(Protocol):
    """Phase 2 seam. Given a description and the card, name the key."""

    def suggest(
        self, description: str, candidates: Sequence[RateCardEntry]
    ) -> Suggestion | None: ...


class NullMapper:
    """v1 default: no model, no guesses."""

    def suggest(
        self, description: str, candidates: Sequence[RateCardEntry]
    ) -> Suggestion | None:
        return None


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

        suggestion = self.mapper.suggest(
            line.description, list(self.card.entries.values())
        )
        if suggestion is None:
            line.mapped_by = None
            line.map_rationale = "no alias on the buy card matches this description"
            return line

        line.map_confidence = suggestion.confidence
        line.map_rationale = suggestion.rationale
        if suggestion.confidence >= self.threshold:
            line.rate_key = suggestion.rate_key
            line.mapped_by = "model"
        else:
            # Deliberately left unmapped: a low-confidence guess that silently
            # priced a line would be worse than admitting we do not know.
            line.mapped_by = "model-rejected"
        return line

    def apply_all(self, lines: Sequence[InvoiceLine]) -> list[InvoiceLine]:
        return [self.apply(line) for line in lines]
