"""Versioned buy rate card store.

Cards are addressed by (warehouse, client) and selected by effective date, so
an invoice is always priced against the card that was in force on the day it
was issued rather than whatever is current.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from .models import RateCard, RateCardEntry
from .money import money


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def load_card(path: Path) -> RateCard:
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries: dict[str, RateCardEntry] = {}
    for key, spec in raw["entries"].items():
        entries[key] = RateCardEntry(
            key=key,
            label=spec["label"],
            uom=spec["uom"],
            kind=spec.get("kind", "per_unit"),
            rate=money(spec.get("rate", "0")),
            percent=Decimal(str(spec.get("percent", "0"))),
            aliases=tuple(spec.get("aliases", ())),
        )
    effective_from = _parse_date(raw["effective_from"])
    assert effective_from is not None, f"{path} has no effective_from"
    return RateCard(
        card_id=raw["card_id"],
        version=raw["version"],
        warehouse_id=raw["warehouse_id"],
        client_id=raw["client_id"],
        effective_from=effective_from,
        effective_to=_parse_date(raw.get("effective_to")),
        entries=entries,
    )


@dataclass
class RateMatch:
    """A rate found on some card — used to diagnose WRONG_CLIENT_RATES."""

    card: RateCard
    entry: RateCardEntry


class RateCardStore:
    def __init__(self, cards: list[RateCard]) -> None:
        self._cards = list(cards)

    @classmethod
    def from_dir(cls, directory: Path) -> "RateCardStore":
        paths = sorted(Path(directory).glob("*.json"))
        if not paths:
            raise FileNotFoundError(f"no rate cards found in {directory}")
        return cls([load_card(p) for p in paths])

    def __len__(self) -> int:
        return len(self._cards)

    def for_invoice(
        self, warehouse_id: str, client_id: str, on: date
    ) -> RateCard | None:
        """The card in force for this pair on this date, if any."""
        candidates = [
            c
            for c in self._cards
            if c.warehouse_id == warehouse_id
            and c.client_id == client_id
            and c.covers(on)
        ]
        if not candidates:
            return None
        # Latest effective_from wins when windows overlap (an amendment).
        return max(candidates, key=lambda c: c.effective_from)

    def all_for_pair(self, warehouse_id: str, client_id: str) -> list[RateCard]:
        return sorted(
            (
                c
                for c in self._cards
                if c.warehouse_id == warehouse_id and c.client_id == client_id
            ),
            key=lambda c: c.effective_from,
        )

    def find_rate_owners(
        self,
        warehouse_id: str,
        key: str,
        rate: Decimal,
        on: date,
        exclude_client: str,
    ) -> list[RateMatch]:
        """Which *other* clients at this warehouse price ``key`` at ``rate``.

        A hit here turns a plain RATE_DRIFT into WRONG_CLIENT_RATES, which is a
        materially different conversation with the warehouse: not "you used the
        wrong number" but "you billed us on someone else's account".
        """
        matches: list[RateMatch] = []
        for card in self._cards:
            if card.warehouse_id != warehouse_id:
                continue
            if card.client_id == exclude_client:
                continue
            if not card.covers(on):
                continue
            entry = card.entry(key)
            if entry is not None and not entry.is_percent and entry.rate == rate:
                matches.append(RateMatch(card=card, entry=entry))
        return matches
