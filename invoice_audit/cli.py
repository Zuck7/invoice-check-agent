"""Command line entry point.

    python -m invoice_audit audit data/invoices/*.csv
    python -m invoice_audit flags
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

from . import flags as flagdefs
from .checks import Tolerances
from .engine import AuditEngine
from .history import HistoryStore
from .intake import IntakeError
from .money import money
from .orderdata import CsvOrderData, NoOrderData
from .ratecards import RateCardStore
from .report import render_batch, to_json

DEFAULT_CARDS = Path("data/rate_cards")
DEFAULT_WMS = Path("data/wms/counts.csv")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="invoice_audit",
        description="Audit inbound warehouse invoices against the buy card.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="audit one or more invoices")
    audit.add_argument("invoices", nargs="+", type=Path)
    audit.add_argument(
        "--rate-cards", type=Path, default=DEFAULT_CARDS, metavar="DIR"
    )
    audit.add_argument(
        "--wms",
        type=Path,
        default=DEFAULT_WMS,
        metavar="FILE",
        help="WMS/OMS counts export; quantity checks are skipped without it",
    )
    audit.add_argument(
        "--history",
        type=Path,
        default=None,
        metavar="FILE",
        help="invoice history for duplicate detection; in-memory if omitted",
    )
    audit.add_argument("--json", type=Path, default=None, metavar="FILE")
    audit.add_argument(
        "--qty-tolerance",
        type=str,
        default="0",
        metavar="PCT",
        help="quantity variance allowed before flagging (default 0)",
    )
    audit.add_argument(
        "--min-exposure",
        type=str,
        default="1.00",
        metavar="AMOUNT",
        help="ignore under-billing smaller than this (default 1.00)",
    )
    audit.add_argument("-v", "--verbose", action="store_true")

    sub.add_parser("flags", help="print the flag taxonomy")

    return parser


def _cmd_flags() -> int:
    print("Flag taxonomy\n")
    for phase, title in ((1, "shipped"), (2, "shipped"), (3, "deferred")):
        defs = [f for f in flagdefs.FLAGS.values() if f.phase == phase]
        if not defs:
            continue
        print(f"  phase {phase} ({title})")
        for f in sorted(defs, key=lambda d: (d.severity.rank, d.id)):
            print(
                f"    {f.severity.value:<6} {f.id:<26} "
                f"{f.detection.value:<13} {f.description}"
            )
        print()
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    try:
        store = RateCardStore.from_dir(args.rate_cards)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    order_data = (
        CsvOrderData.from_file(args.wms) if args.wms and args.wms.exists() else NoOrderData()
    )
    if isinstance(order_data, NoOrderData):
        print(
            f"warning: no WMS export at {args.wms}; quantity checks are off",
            file=sys.stderr,
        )

    history = HistoryStore(args.history)

    engine = AuditEngine(
        store=store,
        order_data=order_data,
        history=history,
        tolerances=Tolerances(
            qty_tolerance_pct=Decimal(args.qty_tolerance),
            min_exposure=money(args.min_exposure),
        ),
    )

    results = []
    failed = False
    for path in sorted(args.invoices):
        try:
            results.append(engine.audit_path(path))
        except IntakeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            failed = True

    if results:
        print(render_batch(results, verbose=args.verbose))
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(to_json(results), encoding="utf-8")
            print(f"exception queue written to {args.json}")

    history.flush()

    if failed:
        return 2
    # 1 means "a human has work to do", which is the useful signal for a cron.
    return 1 if any(not r.clean for r in results) else 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "flags":
        return _cmd_flags()
    if args.command == "audit":
        return _cmd_audit(args)
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
