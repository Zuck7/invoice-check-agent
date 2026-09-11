"""Score the engine against the labelled set.

This is what phase 0 exists to enable. Without it "success rate" degrades to
"it flagged some things"; with it, every change to the mapper or the checks has
a number attached before it ships.

A labelled case is a JSON file naming an invoice and the flags a human decided
are genuinely on it:

    {
      "invoice": "data/invoices/INV-4482.csv",
      "note": "one seeded error per flag",
      "prior": ["data/invoices/INV-4471.csv"],
      "expected": [{"flag": "RATE_DRIFT", "line_no": 1}, ...]
    }

Each case runs against a fresh engine. Cases that need history -- the duplicate
checks -- declare it in ``prior``, which is audited first and discarded.
Without that isolation two unrelated cases covering the same service period
contaminate each other, and the scorecard measures the fixture rather than the
engine.

Matching is on (flag, line_no), and for invoice-level flags on the rate key
they name instead -- otherwise four separate MISSING_LINE findings collapse
into one and the scorecard undercounts. Deltas are not compared: a flag raised on the
right line for the wrong amount is still a catch, and the amount is asserted in
the unit tests instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .models import AuditResult

Key = tuple[str, "int | str | None"]


#: Evidence fields that identify an invoice-level finding, most specific first.
DISCRIMINATORS = ("credit_ref", "rate_key")


def flag_key(flag_id: str, line_no: int | None, discriminator: str | None) -> Key:
    """Identity of a finding for scoring purposes.

    Line-level flags are keyed by line. Invoice-level ones have no line, so
    they fall back to whatever names the specific thing they concern -- which
    credit is missing, which rate key was never billed. Without that, several
    distinct findings would collapse into the single key ``(flag, None)``.
    """
    if line_no is not None:
        return (flag_id, line_no)
    return (flag_id, discriminator)


def discriminator_of(evidence: dict[str, Any]) -> str | None:
    for field in DISCRIMINATORS:
        value = evidence.get(field)
        if value:
            return str(value)
    return None


@dataclass(frozen=True)
class LabelledCase:
    path: Path
    invoice_path: Path
    expected: frozenset[Key]
    note: str = ""
    prior: tuple[Path, ...] = ()

    @classmethod
    def load(cls, path: Path, root: Path) -> "LabelledCase":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        expected = frozenset(
            flag_key(
                item["flag"],
                item.get("line_no"),
                discriminator_of(item),
            )
            for item in raw.get("expected", [])
        )
        return cls(
            path=Path(path),
            invoice_path=root / raw["invoice"],
            expected=expected,
            note=raw.get("note", ""),
            prior=tuple(root / p for p in raw.get("prior", [])),
        )


@dataclass
class CaseScore:
    case: LabelledCase
    caught: set[Key] = field(default_factory=set)
    missed: set[Key] = field(default_factory=set)
    spurious: set[Key] = field(default_factory=set)
    error: str | None = None

    @property
    def perfect(self) -> bool:
        return not self.missed and not self.spurious and self.error is None


@dataclass
class Scorecard:
    cases: list[CaseScore]

    @property
    def true_positives(self) -> int:
        return sum(len(c.caught) for c in self.cases)

    @property
    def false_negatives(self) -> int:
        return sum(len(c.missed) for c in self.cases)

    @property
    def false_positives(self) -> int:
        return sum(len(c.spurious) for c in self.cases)

    @property
    def recall(self) -> float | None:
        denominator = self.true_positives + self.false_negatives
        return self.true_positives / denominator if denominator else None

    @property
    def precision(self) -> float | None:
        denominator = self.true_positives + self.false_positives
        return self.true_positives / denominator if denominator else None

    @property
    def straight_through(self) -> float | None:
        """Share of labelled invoices the engine correctly left alone.

        The benchmark target is 60-75% after tuning, but on a labelled set
        deliberately weighted toward errors this number is not comparable to
        the production one -- it is only meaningful against real month traffic.
        """
        if not self.cases:
            return None
        clean = sum(1 for c in self.cases if not c.case.expected)
        if not clean:
            return None
        correct = sum(
            1 for c in self.cases if not c.case.expected and c.perfect
        )
        return correct / clean

    def per_flag(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for case in self.cases:
            for bucket, keys in (
                ("caught", case.caught),
                ("missed", case.missed),
                ("spurious", case.spurious),
            ):
                for flag_id, _ in keys:
                    row = out.setdefault(
                        flag_id, {"caught": 0, "missed": 0, "spurious": 0}
                    )
                    row[bucket] += 1
        return dict(sorted(out.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "cases": len(self.cases),
            "true_positives": self.true_positives,
            "false_negatives": self.false_negatives,
            "false_positives": self.false_positives,
            "recall": self.recall,
            "precision": self.precision,
            "straight_through": self.straight_through,
            "per_flag": self.per_flag(),
            "failures": [
                {
                    "case": str(c.case.path),
                    "missed": sorted(f"{f}:{l}" for f, l in c.missed),
                    "spurious": sorted(f"{f}:{l}" for f, l in c.spurious),
                    "error": c.error,
                }
                for c in self.cases
                if not c.perfect
            ],
        }


def observed(result: AuditResult) -> set[Key]:
    return {
        flag_key(f.flag_id, f.line_no, discriminator_of(f.evidence))
        for f in result.flags
    }


def score_case(case: LabelledCase, make_engine) -> CaseScore:
    """``make_engine`` returns a fresh engine, so cases cannot contaminate
    each other through shared history."""
    try:
        engine = make_engine()
        for prior in case.prior:
            engine.audit_path(prior)
        result = engine.audit_path(case.invoice_path)
    except Exception as exc:  # an unreadable case is a scored failure
        return CaseScore(case=case, missed=set(case.expected), error=str(exc))

    found = observed(result)
    return CaseScore(
        case=case,
        caught=set(case.expected & found),
        missed=set(case.expected - found),
        spurious=set(found - case.expected),
    )


def load_cases(directory: Path, root: Path) -> list[LabelledCase]:
    paths = sorted(Path(directory).glob("*.json"))
    if not paths:
        raise FileNotFoundError(f"no labelled cases in {directory}")
    return [LabelledCase.load(p, root) for p in paths]


def score(cases: Iterable[LabelledCase], make_engine) -> Scorecard:
    return Scorecard([score_case(case, make_engine) for case in cases])


def render(card: Scorecard) -> str:
    def show(value: float | None) -> str:
        return "n/a" if value is None else f"{value * 100:.1f}%"

    out = [
        "Scorecard",
        "=========",
        f"  cases            {len(card.cases)}",
        f"  true positives   {card.true_positives}",
        f"  missed           {card.false_negatives}",
        f"  false flags      {card.false_positives}",
        "",
        f"  recall           {show(card.recall)}",
        f"  precision        {show(card.precision)}",
        f"  straight-through {show(card.straight_through)}  (clean cases only)",
        "",
    ]

    rows = card.per_flag()
    if rows:
        out.append("  per flag              caught  missed  false")
        for flag_id, row in rows.items():
            out.append(
                f"    {flag_id:<20} {row['caught']:>6} "
                f"{row['missed']:>7} {row['spurious']:>6}"
            )
        out.append("")

    failures = [c for c in card.cases if not c.perfect]
    if failures:
        out.append("  failing cases")
        for case in failures:
            out.append(f"    {case.case.invoice_path.name}")
            if case.error:
                out.append(f"      error: {case.error}")
            for flag_id, line_no in sorted(case.missed):
                out.append(f"      missed   {flag_id} (line {line_no})")
            for flag_id, line_no in sorted(case.spurious):
                out.append(f"      false    {flag_id} (line {line_no})")
    else:
        out.append("  every labelled case scored exactly.")

    return "\n".join(out) + "\n"
