"""The exception queue, rendered.

Review never starts cold: each entry carries what we saw, what we expected,
the money at stake and the card version consulted, so the reviewer can open a
dispute without re-deriving the finding.
"""

from __future__ import annotations

import json
from typing import Iterable

from .flags import Severity
from .models import AuditResult
from .money import ZERO, fmt

_MARK = {Severity.HIGH: "!!", Severity.MEDIUM: " !", Severity.LOW: "  "}


def render(result: AuditResult, verbose: bool = False) -> str:
    inv = result.invoice
    cur = inv.currency
    out: list[str] = []

    header = f"{inv.invoice_no}  {inv.warehouse_id} -> {inv.client_id}"
    out.append(header)
    out.append("=" * len(header))
    out.append(
        f"  dated {inv.invoice_date.isoformat()}   period "
        f"{inv.period_start.isoformat()}..{inv.period_end.isoformat()}"
    )
    out.append(
        f"  total {fmt(inv.stated_total, cur)}   card "
        f"{result.rate_card.ref if result.rate_card else 'NONE'}"
    )

    if result.clean:
        out.append("")
        out.append("  CLEAN - no flags raised.")
    else:
        counts = ", ".join(
            f"{len(result.by_severity(s))} {s.value}"
            for s in Severity
            if result.by_severity(s)
        )
        out.append(
            f"  {len(result.flags)} flag(s): {counts}   "
            f"exposure {fmt(result.exposure, cur)}"
        )
        out.append("")
        for flag in result.sorted_flags():
            where = f"line {flag.line_no}" if flag.line_no else "invoice"
            out.append(f"  {_MARK[flag.severity]} {flag.flag_id}  ({where})")
            if flag.line_description:
                out.append(f"       {flag.line_description}")
            out.append(f"       {flag.message}")
            detail = []
            if flag.expected is not None:
                detail.append(f"expected {flag.expected}")
            if flag.actual is not None:
                detail.append(f"found {flag.actual}")
            if flag.delta != ZERO:
                direction = "over" if flag.delta > 0 else "under"
                detail.append(f"{fmt(abs(flag.delta), cur)} {direction}")
            if detail:
                out.append(f"       {'  |  '.join(detail)}")
            if verbose:
                out.append(f"       card {flag.rate_card_version or '-'}")
                if flag.evidence:
                    out.append(f"       evidence {json.dumps(flag.evidence)}")
            out.append("")

    if result.blocked:
        out.append("  BLOCKED from re-rating: unpriceable line(s) present.")

    for note in result.notes:
        out.append(f"  note: {note}")

    return "\n".join(out).rstrip() + "\n"


def render_batch(results: Iterable[AuditResult], verbose: bool = False) -> str:
    results = list(results)
    blocks = [render(r, verbose) for r in results]

    total = sum((r.exposure for r in results), ZERO)
    flagged = [r for r in results if not r.clean]
    blocked = [r for r in results if r.blocked]
    currency = results[0].invoice.currency if results else "USD"

    summary = [
        "",
        "-" * 60,
        f"{len(results)} invoice(s)   {len(flagged)} flagged   "
        f"{len(blocked)} blocked from re-rating",
        f"net exposure {fmt(total, currency)}"
        + ("  (positive = over-billed to us)" if total else ""),
    ]
    if results:
        straight_through = len(results) - len(flagged)
        rate = straight_through / len(results) * 100
        summary.append(f"straight-through {straight_through}/{len(results)} ({rate:.0f}%)")

    return "\n\n".join(blocks) + "\n".join(summary) + "\n"


def to_json(results: Iterable[AuditResult]) -> str:
    results = list(results)
    payload = {
        "invoices": [r.to_dict() for r in results],
        "summary": {
            "count": len(results),
            "flagged": sum(1 for r in results if not r.clean),
            "blocked": sum(1 for r in results if r.blocked),
            "exposure": str(sum((r.exposure for r in results), ZERO)),
        },
    }
    return json.dumps(payload, indent=2) + "\n"
