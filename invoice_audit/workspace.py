"""One assembled view of everything: sources, audits, flags, margin.

The CLI builds what each command needs and throws it away. A UI needs the whole
picture at once and needs it to stay consistent between requests, so this holds
the stores and the audited results together in one object the server can serve
from and re-scan on demand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .checks import Tolerances
from .credits import CsvCreditLog, NoCreditLog
from .engine import AuditEngine
from .flagstore import ESCALATION_DAYS, FlagStore
from .history import HistoryStore
from .intake import IntakeError
from .models import AuditResult
from .money import ZERO, fmt
from .orderdata import CsvOrderData, NoOrderData, PeriodKey
from .ratecards import RateCardStore
from .rerate import Rerate, SellCardStore, rerate
from .snapshots import CsvSnapshots, NoSnapshots

INVOICE_SUFFIXES = (".csv", ".pdf", ".png", ".jpg", ".jpeg")

#: Upload ceiling. The Claude API caps a PDF request at 32 MB, so anything
#: larger could not be transcribed even if we accepted it.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class UploadRejected(ValueError):
    """The file will not be accepted. Never a flag — the audit never ran."""


def safe_filename(raw: str) -> str:
    """Reduce a browser-supplied name to something that cannot escape a folder.

    Path separators, traversal and control characters are removed rather than
    escaped, because no legitimate invoice filename needs them.
    """
    name = Path(str(raw or "")).name.strip()
    name = _SAFE_NAME.sub("_", name).strip("._")
    if not name:
        raise UploadRejected("that file has no usable name")
    return name


@dataclass
class Paths:
    root: Path = Path(".")
    rate_cards: Path = Path("data/rate_cards")
    sell_cards: Path = Path("data/sell_cards")
    wms: Path = Path("data/wms/counts.csv")
    credits: Path = Path("data/credits/log.csv")
    snapshots: Path = Path("data/snapshots/pallets.csv")
    invoices: Path = Path("data/invoices")
    flags_db: Path | None = None
    history_db: Path | None = None


@dataclass
class Workspace:
    paths: Paths = field(default_factory=Paths)
    engine: AuditEngine | None = None
    sell_cards: SellCardStore | None = None
    results: list[AuditResult] = field(default_factory=list)
    rerates: dict[str, Rerate] = field(default_factory=dict)
    failures: list[dict[str, str]] = field(default_factory=list)
    scanned_at: datetime | None = None

    # -- construction ------------------------------------------------------

    @classmethod
    def load(cls, paths: Paths) -> "Workspace":
        engine = AuditEngine(
            store=RateCardStore.from_dir(paths.rate_cards),
            order_data=(
                CsvOrderData.from_file(paths.wms)
                if paths.wms.exists()
                else NoOrderData()
            ),
            credit_log=(
                CsvCreditLog.from_file(paths.credits)
                if paths.credits.exists()
                else NoCreditLog()
            ),
            snapshots=(
                CsvSnapshots.from_file(paths.snapshots)
                if paths.snapshots.exists()
                else NoSnapshots()
            ),
            history=HistoryStore(paths.history_db),
            flag_store=FlagStore(paths.flags_db),
        )
        sell_cards = (
            SellCardStore.from_dir(paths.sell_cards)
            if paths.sell_cards.exists()
            else None
        )
        workspace = cls(paths=paths, engine=engine, sell_cards=sell_cards)
        workspace.rescan()
        return workspace

    @property
    def flag_store(self) -> FlagStore:
        assert self.engine is not None
        return self.engine.flag_store

    # -- scanning ----------------------------------------------------------

    def invoice_paths(self) -> list[Path]:
        if not self.paths.invoices.exists():
            return []
        return sorted(
            p
            for p in self.paths.invoices.iterdir()
            if p.suffix.lower() in INVOICE_SUFFIXES
        )

    def rescan(self) -> None:
        """Re-audit every invoice on disk.

        History and the flag store are rebuilt from scratch so a rescan is
        idempotent: without that, re-reading the same folder would raise a
        DUPLICATE_INVOICE against every invoice for having been seen on the
        previous scan.
        """
        assert self.engine is not None
        self.engine.history = HistoryStore(self.paths.history_db)
        self.results = []
        self.rerates = {}
        self.failures = []

        for path in self.invoice_paths():
            try:
                result = self.engine.audit_path(path)
            except IntakeError as exc:
                self.failures.append({"path": str(path), "error": str(exc)})
                continue
            self.results.append(result)
            if self.sell_cards is not None:
                self.rerates[result.invoice.invoice_no] = rerate(
                    result, self.sell_cards
                )

        self.engine.flag_store.flush()
        self.engine.history.flush()
        self.scanned_at = datetime.now(timezone.utc)

    # -- upload ------------------------------------------------------------

    def ingest(self, filename: str, data: bytes) -> dict[str, Any]:
        """Accept one uploaded document, audit it, and return the verdict.

        Deliberately not a rescan: the new invoice is audited against the
        history already in the store, so re-uploading the same document is
        caught as DUPLICATE_INVOICE rather than quietly passing twice.
        """
        assert self.engine is not None

        if not data:
            raise UploadRejected("that file is empty")
        if len(data) > MAX_UPLOAD_BYTES:
            raise UploadRejected(
                f"that file is {len(data) // (1024 * 1024)} MB; the limit is "
                f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB"
            )

        name = safe_filename(filename)
        suffix = Path(name).suffix.lower()
        if suffix not in INVOICE_SUFFIXES:
            raise UploadRejected(
                f"{suffix or 'that file type'} is not an invoice format — "
                f"send {', '.join(INVOICE_SUFFIXES)}"
            )

        self.paths.invoices.mkdir(parents=True, exist_ok=True)
        target = self.paths.invoices / name
        if target.exists():
            # Overwriting a financial document silently is worse than refusing.
            raise UploadRejected(
                f"{name} is already here. Rename it if this is a different "
                "invoice, or delete the old one if it is a correction."
            )

        target.write_bytes(data)
        try:
            result = self.engine.audit_path(target)
        except IntakeError as exc:
            target.unlink(missing_ok=True)
            raise UploadRejected(str(exc)) from exc

        self.results.append(result)
        if self.sell_cards is not None:
            self.rerates[result.invoice.invoice_no] = rerate(result, self.sell_cards)

        self.engine.flag_store.flush()
        self.engine.history.flush()
        self.scanned_at = datetime.now(timezone.utc)

        payload = result.to_dict()
        payload["route"] = suffix.lstrip(".")
        payload["filename"] = name
        rr = self.rerates.get(result.invoice.invoice_no)
        payload["rerate"] = rr.to_dict() if rr else None
        return payload

    # -- views -------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        store = self.flag_store
        now = datetime.now(timezone.utc)
        open_records = store.open_records()
        escalated = [r for r in open_records if r.escalated(now)]
        exposure = sum((r.delta for r in open_records), ZERO)
        blocked = [r for r in self.results if r.blocked]
        clean = [r for r in self.results if r.clean]
        margin = sum(
            (r.margin for r in self.rerates.values() if not r.blocked), ZERO
        )
        return {
            "invoices": len(self.results),
            "clean": len(clean),
            "flagged": len(self.results) - len(clean),
            "blocked": len(blocked),
            "straight_through": (
                round(len(clean) / len(self.results) * 100, 1) if self.results else None
            ),
            "open_flags": len(open_records),
            "escalated": len(escalated),
            "exposure": str(exposure),
            "exposure_display": fmt(exposure),
            "margin": str(margin),
            "margin_display": fmt(margin),
            "escalation_days": ESCALATION_DAYS,
            "read_failures": len(self.failures),
            "scanned_at": self.scanned_at.isoformat() if self.scanned_at else None,
            "sources": self.sources(),
        }

    def sources(self) -> list[dict[str, Any]]:
        """Which reference feeds are wired up.

        A missing feed silently disables checks, so the UI has to be able to say
        which ones are off rather than implying a clean audit.
        """
        assert self.engine is not None
        return [
            {
                "name": "Buy rate cards",
                "path": str(self.paths.rate_cards),
                "present": True,
                "enables": "RATE_DRIFT, WRONG_CLIENT_RATES, UOM_MISMATCH",
            },
            {
                "name": "Sell rate cards",
                "path": str(self.paths.sell_cards),
                "present": self.sell_cards is not None,
                "enables": "re-rating and margin",
            },
            {
                "name": "WMS / OMS counts",
                "path": str(self.paths.wms),
                "present": not isinstance(self.engine.order_data, NoOrderData),
                "enables": "QTY_VARIANCE, MISSING_LINE",
            },
            {
                "name": "Dispute / credit log",
                "path": str(self.paths.credits),
                "present": not isinstance(self.engine.credit_log, NoCreditLog),
                "enables": "MISSING_CREDIT",
            },
            {
                "name": "Inventory snapshots",
                "path": str(self.paths.snapshots),
                "present": not isinstance(self.engine.snapshots, NoSnapshots),
                "enables": "STORAGE_AGING_ERROR",
            },
        ]

    def invoices(self) -> list[dict[str, Any]]:
        out = []
        for result in self.results:
            payload = result.to_dict()
            payload["route"] = Path(result.invoice.source_path or "").suffix.lstrip(".")
            rr = self.rerates.get(result.invoice.invoice_no)
            payload["rerate"] = rr.to_dict() if rr else None
            out.append(payload)
        return out

    def trends(self) -> list[dict[str, Any]]:
        rows = []
        for warehouse, months in sorted(self.flag_store.trends().items()):
            for month, row in sorted(months.items()):
                rows.append(
                    {
                        "warehouse_id": warehouse,
                        "month": month,
                        "count": row["count"],
                        "open": row["open"],
                        "exposure": str(row["exposure"]),
                        "exposure_display": fmt(row["exposure"]),
                        "by_flag": row["by_flag"],
                    }
                )
        return rows

    def repeats(self) -> list[dict[str, Any]]:
        return [
            {"warehouse_id": w, "flag": f, "months": n}
            for w, f, n in self.flag_store.repeat_offenders()
        ]
