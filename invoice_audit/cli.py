"""Command line entry point.

    python -m invoice_audit audit data/invoices/*.csv
    python -m invoice_audit queue --flags data/flags.json
    python -m invoice_audit resolve 3f2a --note "credit received" --by sam
    python -m invoice_audit trends --flags-db data/flags.json
    python -m invoice_audit serve
    python -m invoice_audit score
    python -m invoice_audit flags
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from . import env as dotenv
from . import flags as flagdefs
from .checks import Tolerances
from .credits import CsvCreditLog, NoCreditLog
from .engine import AuditEngine
from .flagstore import ESCALATION_DAYS, FlagStore, Status
from .history import HistoryStore
from .intake import IntakeError
from .money import ZERO, fmt, money
from .normalize import FuzzyMapper, NullMapper
from .orderdata import CsvOrderData, NoOrderData
from .ratecards import RateCardStore
from .snapshots import CsvSnapshots, NoSnapshots
from .report import render_batch, to_json
from .scoring import load_cases, render as render_score, score

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CARDS = Path("data/rate_cards")
DEFAULT_WMS = Path("data/wms/counts.csv")
DEFAULT_CREDITS = Path("data/credits/log.csv")
DEFAULT_SNAPSHOTS = Path("data/snapshots/pallets.csv")
DEFAULT_LABELS = Path("data/labeled")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="invoice_audit",
        description="Audit inbound warehouse invoices against the buy card.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_sources(p: argparse.ArgumentParser) -> None:
        p.add_argument("--rate-cards", type=Path, default=DEFAULT_CARDS, metavar="DIR")
        p.add_argument(
            "--wms",
            type=Path,
            default=DEFAULT_WMS,
            metavar="FILE",
            help="WMS/OMS counts export; quantity checks are skipped without it",
        )
        p.add_argument(
            "--history",
            type=Path,
            default=None,
            metavar="FILE",
            help="invoice history for duplicate detection; in-memory if omitted",
        )
        p.add_argument(
            "--flags-db",
            type=Path,
            default=None,
            metavar="FILE",
            help="persistent exception queue; in-memory if omitted",
        )
        p.add_argument(
            "--credits",
            type=Path,
            default=DEFAULT_CREDITS,
            metavar="FILE",
            help="dispute/credit log; MISSING_CREDIT is skipped without it",
        )
        p.add_argument(
            "--snapshots",
            type=Path,
            default=DEFAULT_SNAPSHOTS,
            metavar="FILE",
            help="pallet-level inventory; STORAGE_AGING_ERROR needs it",
        )
        p.add_argument(
            "--vision",
            choices=("gemini", "anthropic"),
            default=None,
            metavar="PROVIDER",
            help="model used for scans (default: gemini, or $INVOICE_AUDIT_VISION)",
        )
        p.add_argument(
            "--google-api-key",
            default=None,
            metavar="KEY",
            help="overrides $GOOGLE_API_KEY / $GEMINI_API_KEY",
        )
        p.add_argument(
            "--no-mapper",
            action="store_true",
            help="alias matching only, no fuzzy mapping (phase 1 behaviour)",
        )

    audit = sub.add_parser("audit", help="audit one or more invoices")
    audit.add_argument("invoices", nargs="+", type=Path)
    add_sources(audit)
    audit.add_argument("--json", type=Path, default=None, metavar="FILE")
    audit.add_argument(
        "--qty-tolerance", type=str, default="0", metavar="PCT",
        help="quantity variance allowed before flagging (default 0)",
    )
    audit.add_argument(
        "--min-exposure", type=str, default="1.00", metavar="AMOUNT",
        help="ignore under-billing smaller than this (default 1.00)",
    )
    audit.add_argument("-v", "--verbose", action="store_true")

    queue = sub.add_parser("queue", help="show the open exception queue")
    queue.add_argument("--flags-db", type=Path, required=True, metavar="FILE")
    queue.add_argument("--all", action="store_true", help="include resolved")
    queue.add_argument(
        "--escalated", action="store_true",
        help=f"only flags open {ESCALATION_DAYS}+ days",
    )

    resolve = sub.add_parser("resolve", help="close one flag in the queue")
    resolve.add_argument("fingerprint", help="full id or a unique prefix")
    resolve.add_argument("--flags-db", type=Path, required=True, metavar="FILE")
    resolve.add_argument("--note", required=True, help="what happened")
    resolve.add_argument("--by", required=True, help="who decided")
    resolve.add_argument(
        "--dismiss", action="store_true",
        help="mark as not a real finding rather than resolved",
    )

    trends = sub.add_parser("trends", help="flags per warehouse, per month")
    trends.add_argument("--flags-db", type=Path, required=True, metavar="FILE")

    serve = sub.add_parser("serve", help="open the audit UI in a browser")
    add_sources(serve)
    serve.add_argument(
        "--invoices", type=Path, default=Path("data/invoices"), metavar="DIR",
        help="folder the UI audits and rescans",
    )
    serve.add_argument(
        "--sell-cards", type=Path, default=Path("data/sell_cards"), metavar="DIR",
    )
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--host", default="127.0.0.1",
        help="loopback by default; writes are guarded by a token, not real auth",
    )
    serve.add_argument("--token", default=None, help="reuse a fixed token")
    serve.add_argument("--no-browser", action="store_true")
    serve.add_argument(
        "--demo",
        action="store_true",
        help="public demo: no token, disposable data, capped model calls",
    )

    scorer = sub.add_parser("score", help="score the engine against the labelled set")
    scorer.add_argument("--labels", type=Path, default=DEFAULT_LABELS, metavar="DIR")
    add_sources(scorer)
    scorer.add_argument("--json", type=Path, default=None, metavar="FILE")

    sub.add_parser("flags", help="print the flag taxonomy")

    return parser


def _engine(args: argparse.Namespace, **overrides) -> AuditEngine:
    store = RateCardStore.from_dir(args.rate_cards)

    provider = getattr(args, "vision", None)
    google_key = getattr(args, "google_api_key", None)
    if provider or google_key:
        # Rebuild the PDF/image path so the chosen provider reaches the
        # extractors rather than only the default one.
        from .intake import CsvExtractor, LazyVisionExtractor, PdfRouter

        vision_kwargs = {"provider": provider}
        if google_key:
            vision_kwargs["api_key"] = google_key
        overrides.setdefault(
            "extractors",
            (
                CsvExtractor(),
                PdfRouter(**vision_kwargs),
                LazyVisionExtractor(**vision_kwargs),
            ),
        )
    order_data = (
        CsvOrderData.from_file(args.wms)
        if getattr(args, "wms", None) and args.wms.exists()
        else NoOrderData()
    )
    if isinstance(order_data, NoOrderData):
        print(
            f"warning: no WMS export at {args.wms}; quantity checks are off",
            file=sys.stderr,
        )
    credits_path = getattr(args, "credits", None)
    snapshots_path = getattr(args, "snapshots", None)

    defaults = dict(
        store=store,
        order_data=order_data,
        credit_log=(
            CsvCreditLog.from_file(credits_path)
            if credits_path and credits_path.exists()
            else NoCreditLog()
        ),
        snapshots=(
            CsvSnapshots.from_file(snapshots_path)
            if snapshots_path and snapshots_path.exists()
            else NoSnapshots()
        ),
        history=HistoryStore(getattr(args, "history", None)),
        flag_store=FlagStore(getattr(args, "flags_db", None)),
        mapper=NullMapper() if getattr(args, "no_mapper", False) else FuzzyMapper(),
    )
    defaults.update(overrides)
    return AuditEngine(**defaults)


def _cmd_flags() -> int:
    print("Flag taxonomy\n")
    for phase, title in ((1, "shipped"), (2, "shipped"), (3, "shipped"), (4, "deferred")):
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
        engine = _engine(args)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    engine.tolerances = Tolerances(
        qty_tolerance_pct=Decimal(args.qty_tolerance),
        min_exposure=money(args.min_exposure),
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

    engine.history.flush()
    engine.flag_store.flush()

    if failed:
        return 2
    # 1 means "a human has work to do", which is the useful signal for a cron.
    return 1 if any(not r.clean for r in results) else 0


def _cmd_queue(args: argparse.Namespace) -> int:
    store = FlagStore(args.flags_db)
    now = datetime.now(timezone.utc)

    if args.escalated:
        records = store.escalated(now)
        title = f"Escalated ({ESCALATION_DAYS}+ days open)"
    elif args.all:
        records = store.all()
        title = "All flags"
    else:
        records = store.open_records()
        title = "Open flags"

    if not records:
        print(f"{title}: nothing.")
        return 0

    print(f"{title} — {len(records)}\n")
    exposure = ZERO
    for r in records:
        age = r.age_days(now)
        mark = "!!" if r.severity == "high" else " !" if r.severity == "medium" else "  "
        escalated = "  ESCALATED" if r.escalated(now) else ""
        state = "" if r.is_open else f"  [{r.status}]"
        where = f"line {r.line_no}" if r.line_no else "invoice"
        print(f"  {mark} {r.fingerprint}  {r.flag_id:<22} {r.invoice_no}  ({where})")
        print(f"       {r.message}")
        print(
            f"       {fmt(r.delta, r.currency)}  |  {r.warehouse_id}  |  "
            f"{age}d old{escalated}{state}"
        )
        if r.resolution:
            print(f"       resolved by {r.resolved_by}: {r.resolution}")
        print()
        if r.is_open:
            exposure += r.delta

    print(f"  open exposure {fmt(exposure, records[0].currency)}")
    if any(r.escalated(now) for r in records):
        print(
            f"  note: escalated flags are past {ESCALATION_DAYS} days and may be "
            "approaching a dispute deadline."
        )
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    store = FlagStore(args.flags_db)
    try:
        record = store.resolve(
            args.fingerprint, resolution=args.note, by=args.by, dismissed=args.dismiss
        )
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    store.flush()
    verb = "dismissed" if args.dismiss else "resolved"
    print(f"{record.fingerprint}  {record.flag_id} on {record.invoice_no} — {verb}.")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from .server import UI_DIST, serve as run_server
    from .workspace import Paths, Workspace

    if not UI_DIST.exists():
        print(
            "error: the UI bundle is missing. Build it once with:\n"
            "  npm --prefix ui install && npm --prefix ui run build",
            file=sys.stderr,
        )
        return 2

    try:
        workspace = Workspace.load(
            Paths(
                rate_cards=args.rate_cards,
                sell_cards=args.sell_cards,
                wms=args.wms,
                credits=args.credits,
                snapshots=args.snapshots,
                invoices=args.invoices,
                flags_db=args.flags_db,
                history_db=args.history,
            )
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # A host platform tells the process where to listen; its choice wins.
    host = os.environ.get("HOST") or args.host
    port = int(os.environ.get("PORT") or args.port)
    demo = args.demo or os.environ.get("INVOICE_AUDIT_DEMO") == "1"
    if demo and host == "127.0.0.1":
        host = "0.0.0.0"  # a demo nobody can reach is not a demo

    run_server(
        workspace,
        host=host,
        port=port,
        token=args.token,
        open_browser=not args.no_browser and not demo,
        demo=demo,
    )
    return 0


def _cmd_trends(args: argparse.Namespace) -> int:
    store = FlagStore(args.flags_db)
    trends = store.trends()
    if not trends:
        print("No flags recorded yet.")
        return 0

    for warehouse, months in sorted(trends.items()):
        print(f"{warehouse}")
        for month, row in sorted(months.items()):
            breakdown = ", ".join(
                f"{flag} x{n}" for flag, n in sorted(row["by_flag"].items())
            )
            print(
                f"  {month}   {row['count']:>3} flags  "
                f"{fmt(row['exposure']):>12}  ({row['open']} open)"
            )
            print(f"           {breakdown}")
        print()

    repeats = store.repeat_offenders()
    if repeats:
        print("Recurring patterns")
        for warehouse, flag_id, months in repeats:
            print(f"  {warehouse}  {flag_id}  in {months} separate months")
    return 0


def _cmd_score(args: argparse.Namespace) -> int:
    try:
        cases = load_cases(args.labels, ROOT)
        _engine(args)  # fail fast if the sources are missing
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # A fresh engine per case: scoring measures the engine, not the order the
    # fixtures happen to sit in.
    card = score(cases, lambda: _engine(args))
    print(render_score(card))

    if args.json:
        import json

        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(card.to_dict(), indent=2) + "\n", encoding="utf-8")
        print(f"scorecard written to {args.json}")

    if len(cases) < 30:
        print(
            f"note: {len(cases)} labelled cases. Phase 0 calls for ~30 before "
            "these numbers mean anything.",
            file=sys.stderr,
        )
    return 0 if card.false_negatives == 0 and card.false_positives == 0 else 1


def main(argv: list[str] | None = None) -> int:
    # Before parsing: --vision defaults read INVOICE_AUDIT_VISION, and the
    # backends read the API keys. A real shell variable still wins.
    dotenv.load()
    args = _build_parser().parse_args(argv)
    return {
        "flags": lambda: _cmd_flags(),
        "audit": lambda: _cmd_audit(args),
        "queue": lambda: _cmd_queue(args),
        "resolve": lambda: _cmd_resolve(args),
        "trends": lambda: _cmd_trends(args),
        "score": lambda: _cmd_score(args),
        "serve": lambda: _cmd_serve(args),
    }[args.command]()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
