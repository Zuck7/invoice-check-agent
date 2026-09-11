"""The flag taxonomy.

This is the closed list from ``spec.md``. The agent never emits a flag that is
not registered here — free-text findings cannot be counted, routed or measured.

``phase`` records which build phase delivers the check, so ``FLAGS`` doubles as
the traceability table between the spec and the code.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(Enum):
    """Drives routing. HIGH reaches a human today."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {"high": 0, "medium": 1, "low": 2}[self.value]


class Detection(Enum):
    DETERMINISTIC = "deterministic"
    MODEL = "model"
    MODEL_RULES = "model+rules"


@dataclass(frozen=True)
class FlagDef:
    id: str
    severity: Severity
    description: str
    detection: Detection
    inputs: str
    phase: int


def _d(*args: object) -> FlagDef:  # narrow helper, keeps the table readable
    return FlagDef(*args)  # type: ignore[arg-type]


FLAGS: dict[str, FlagDef] = {
    f.id: f
    for f in [
        _d(
            "RATE_DRIFT",
            Severity.HIGH,
            "line rate != buy card rate for the effective date",
            Detection.DETERMINISTIC,
            "buy card + effective dates",
            1,
        ),
        _d(
            "QTY_VARIANCE",
            Severity.HIGH,
            "billed qty != WMS/OMS count, outside tolerance",
            Detection.DETERMINISTIC,
            "WMS/OMS counts",
            1,
        ),
        _d(
            "UNKNOWN",
            Severity.HIGH,
            "line item that is on no rate card at all",
            Detection.MODEL,
            "rate card",
            2,
        ),
        _d(
            "RATE_CARD_VERSION_STALE",
            Severity.HIGH,
            "service period not covered by any effective card",
            Detection.DETERMINISTIC,
            "buy card versions",
            1,
        ),
        _d(
            "WRONG_CLIENT_RATES",
            Severity.HIGH,
            "rate matches a different client's card at the same warehouse",
            Detection.DETERMINISTIC,
            "all buy cards",
            1,
        ),
        _d(
            "UOM_MISMATCH",
            Severity.HIGH,
            "billed on a different unit than the card prices",
            Detection.MODEL_RULES,
            "rate card UoM",
            1,
        ),
        _d(
            "MATH_ERROR",
            Severity.HIGH,
            "qty x rate != extended, or lines != invoice total",
            Detection.DETERMINISTIC,
            "invoice only",
            1,
        ),
        _d(
            "DUPLICATE_CHARGE",
            Severity.HIGH,
            "same service billed twice in or across periods",
            Detection.DETERMINISTIC,
            "invoice history",
            3,
        ),
        _d(
            "DUPLICATE_INVOICE",
            Severity.HIGH,
            "invoice number or content hash seen before",
            Detection.DETERMINISTIC,
            "invoice history",
            1,
        ),
        _d(
            "MISSING_LINE",
            Severity.MEDIUM,
            "activity present in WMS, absent from the invoice",
            Detection.DETERMINISTIC,
            "WMS/OMS counts",
            1,
        ),
        _d(
            "MISSING_CREDIT",
            Severity.MEDIUM,
            "expected credit from the dispute log is absent",
            Detection.DETERMINISTIC,
            "dispute/credit log",
            3,
        ),
        _d(
            "SURCHARGE_BASE_WRONG",
            Severity.MEDIUM,
            "percentage surcharge computed off the wrong base",
            Detection.DETERMINISTIC,
            "carrier cost + card",
            1,
        ),
        _d(
            "STORAGE_AGING_ERROR",
            Severity.MEDIUM,
            "long-term penalty applied to already-shipped inventory",
            Detection.DETERMINISTIC,
            "WMS snapshots",
            3,
        ),
        _d(
            "LOW_CONFIDENCE_EXTRACTION",
            Severity.LOW,
            "model confidence below threshold after retries",
            Detection.MODEL,
            "-",
            2,
        ),
    ]
}

#: Flags that stop an invoice from being re-rated to the client's sell card.
#: A line we cannot price is a line we cannot mark up, so it always reaches a
#: human even when the invoice total is otherwise correct.
BLOCKING = frozenset({"UNKNOWN"})

#: Delivered by this release: the whole taxonomy.
V1_PHASES = frozenset({1, 2, 3})


def implemented() -> list[FlagDef]:
    return [f for f in FLAGS.values() if f.phase in V1_PHASES]


def deferred() -> list[FlagDef]:
    return [f for f in FLAGS.values() if f.phase not in V1_PHASES]


def get(flag_id: str) -> FlagDef:
    try:
        return FLAGS[flag_id]
    except KeyError:
        raise KeyError(
            f"{flag_id!r} is not in the taxonomy; add it to flags.FLAGS first"
        ) from None
