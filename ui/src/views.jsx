import { useMemo, useState } from "react";
import { Empty, FlagRow, Segmented, Tiles } from "./components";
import { money, pct, plural } from "./format";

/* ---------------------------------------------------------------- Queue */

export function QueueView({ flags, summary, onAct, canWrite }) {
  const [filter, setFilter] = useState("open");
  const [search, setSearch] = useState("");

  const shown = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return flags.filter((f) => {
      if (filter === "open" && f.status !== "open") return false;
      if (filter === "escalated" && !(f.status === "open" && f.escalated)) return false;
      if (!needle) return true;
      return [f.flag, f.invoice_no, f.warehouse_id, f.client_id, f.line_description, f.message]
        .join(" ")
        .toLowerCase()
        .includes(needle);
    });
  }, [flags, filter, search]);

  const open = flags.filter((f) => f.status === "open");
  const escalated = open.filter((f) => f.escalated);
  const exposure = open.reduce((a, f) => a + Number(f.delta || 0), 0);
  const blocked = new Set(open.filter((f) => f.flag === "UNKNOWN").map((f) => f.invoice_no));

  return (
    <>
      <Tiles
        items={[
          { k: "open flags", v: open.length },
          {
            k: "net exposure",
            v: money(exposure),
            sub: exposure > 0 ? "over-billed to us" : exposure < 0 ? "under-billed" : "",
            tone: exposure > 0 ? "alarm" : undefined,
          },
          {
            k: "escalated",
            v: escalated.length,
            sub: escalated.length
              ? `past ${summary?.escalation_days ?? 21} days`
              : "none overdue",
            tone: escalated.length ? "alarm" : "good",
          },
          {
            k: "blocked invoices",
            v: blocked.size,
            sub: blocked.size ? "cannot re-rate" : "none",
          },
        ]}
      />

      <div className="bar">
        <Segmented
          label="Filter flags"
          value={filter}
          onChange={setFilter}
          options={[
            { value: "open", label: "Open" },
            { value: "escalated", label: "Escalated" },
            { value: "all", label: "All" },
          ]}
        />
        <input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="filter by flag, invoice, warehouse…"
          aria-label="Search flags"
        />
        <span className="count">{plural(shown.length, "flag")} shown</span>
      </div>

      {shown.length ? (
        <div className="rows">
          {shown.map((f) => (
            <FlagRow key={f.fingerprint} flag={f} onAct={onAct} canWrite={canWrite} />
          ))}
        </div>
      ) : (
        <Empty>
          {filter === "open"
            ? "The queue is clear. Nothing is waiting on a human."
            : "Nothing matches that filter."}
        </Empty>
      )}
    </>
  );
}

/* ------------------------------------------------------------- Invoices */

export function InvoicesView({ invoices }) {
  const [selected, setSelected] = useState(invoices[0]?.invoice_no ?? null);
  const invoice = invoices.find((i) => i.invoice_no === selected) ?? invoices[0];

  if (!invoices.length) return <Empty>No invoices in the watched folder.</Empty>;

  return (
    <div className="stack">
      <div className="tablewrap">
        <table>
          <thead>
            <tr>
              <th>Invoice</th>
              <th>Period</th>
              <th>Route</th>
              <th className="num">Billed</th>
              <th className="num">Flags</th>
              <th className="num">Exposure</th>
              <th className="num">Margin</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {invoices.map((inv) => {
              const exposure = Number(inv.exposure || 0);
              const rr = inv.rerate;
              return (
                <tr
                  key={inv.invoice_no}
                  onClick={() => setSelected(inv.invoice_no)}
                  style={{ cursor: "pointer" }}
                >
                  <td className="mono">{inv.invoice_no}</td>
                  <td className="mono">{inv.period[0]} → {inv.period[1]}</td>
                  <td className="mono">{inv.route}</td>
                  <td className="num">{money(inv.stated_total, inv.currency)}</td>
                  <td className="num">{inv.flag_count || "—"}</td>
                  <td className={`num ${exposure > 0 ? "neg" : ""}`}>
                    {exposure ? money(exposure, inv.currency) : "—"}
                  </td>
                  <td className={`num ${rr && !rr.blocked ? "pos" : ""}`}>
                    {rr && !rr.blocked ? rr.margin_display : "—"}
                  </td>
                  <td>
                    {inv.blocked_from_rerate ? (
                      <span className="fact esc">blocked</span>
                    ) : inv.flag_count ? (
                      <span className="fact">flagged</span>
                    ) : (
                      <span className="fact">clean</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {invoice && <InvoiceDetail invoice={invoice} />}
    </div>
  );
}

function InvoiceDetail({ invoice }) {
  const rr = invoice.rerate;
  return (
    <div className="grid2">
      <div className="card">
        <h2>{invoice.invoice_no} — re-rate</h2>
        {!rr ? (
          <p className="note">No sell card is configured, so margin cannot be computed.</p>
        ) : rr.blocked ? (
          <>
            <p className="note">
              <strong>Blocked.</strong> A line we cannot price cannot be marked up,
              so this invoice does not go out to the client yet.
            </p>
            <ul className="note">
              {rr.blocked_by.map((b, i) => <li key={i}>{b}</li>)}
            </ul>
          </>
        ) : (
          <>
            <div className="tablewrap">
              <table>
                <thead>
                  <tr>
                    <th>Line</th>
                    <th className="num">Qty</th>
                    <th className="num">Buy</th>
                    <th className="num">Sell</th>
                    <th className="num">Margin</th>
                  </tr>
                </thead>
                <tbody>
                  {rr.lines.map((l) => (
                    <tr key={l.rate_key}>
                      <td>
                        {l.label}
                        {l.quantity_source !== "invoice" && (
                          <div className="desc">{l.note}</div>
                        )}
                      </td>
                      <td className="num">{l.quantity}</td>
                      <td className="num">{l.buy_display}</td>
                      <td className="num">{l.sell_display}</td>
                      <td className="num pos">{l.margin_display}</td>
                    </tr>
                  ))}
                  <tr className="total">
                    <td>Total</td>
                    <td className="num"></td>
                    <td className="num">{rr.buy_display}</td>
                    <td className="num">{rr.sell_display}</td>
                    <td className="num">{rr.margin_display} ({rr.margin_pct}%)</td>
                  </tr>
                </tbody>
              </table>
            </div>
            {rr.warnings.map((w, i) => (
              <p className="note" key={i}>⚠ {w}</p>
            ))}
          </>
        )}
      </div>

      <div className="card">
        <h2>Findings</h2>
        {invoice.flags.length === 0 ? (
          <p className="note">Clean. Nothing raised against this invoice.</p>
        ) : (
          <div className="sources">
            {invoice.flags.map((f) => (
              <div className="source" key={f.fingerprint}>
                <span className="nm">
                  <span className={`sev-dot ${f.severity}`} />
                  {f.flag}
                </span>
                <span className="pth">{f.line_no ? `line ${f.line_no}` : "invoice"}</span>
                <span className="en">{f.delta_display}</span>
              </div>
            ))}
          </div>
        )}
        {invoice.notes.map((n, i) => (
          <p className="note" key={i}>note: {n}</p>
        ))}
      </div>
    </div>
  );
}

/* --------------------------------------------------------------- Trends */

export function TrendsView({ trends }) {
  const months = trends?.months ?? [];
  const repeats = trends?.repeats ?? [];
  if (!months.length) return <Empty>No flag history yet.</Empty>;

  const peak = Math.max(...months.map((m) => Math.abs(Number(m.exposure))), 1);

  return (
    <div className="stack">
      <div className="card">
        <h2>Exposure by month</h2>
        <div className="stack" style={{ gap: 6 }}>
          {months.map((m) => {
            const value = Number(m.exposure);
            const width = Math.max(2, (Math.abs(value) / peak) * 100);
            return (
              <div key={`${m.warehouse_id}-${m.month}`}>
                <div className="bar" style={{ gap: 8 }}>
                  <span className="fact">{m.warehouse_id}</span>
                  <span className="fact">{m.month}</span>
                  <span className="count">
                    {plural(m.count, "flag")} · {m.open} open · {m.exposure_display}
                  </span>
                </div>
                <div
                  style={{
                    height: 8,
                    borderRadius: 2,
                    background: "var(--surface-2)",
                    marginTop: 4,
                    overflow: "hidden",
                  }}
                >
                  <div
                    style={{
                      width: `${width}%`,
                      height: "100%",
                      background: value > 0 ? "var(--high)" : "var(--accent)",
                    }}
                  />
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="card">
        <h2>Recurring patterns</h2>
        {repeats.length ? (
          <>
            <p className="note">
              The same fault in more than one month is a process problem at the
              warehouse, not a typo. These are the conversations worth having.
            </p>
            <div className="sources">
              {repeats.map((r) => (
                <div className="source" key={`${r.warehouse_id}-${r.flag}`}>
                  <span className="nm">{r.warehouse_id}</span>
                  <span className="pth">{r.flag}</span>
                  <span className="en">{plural(r.months, "month")}</span>
                </div>
              ))}
            </div>
          </>
        ) : (
          <p className="note">
            Nothing has recurred across months yet. This fills in as history accumulates.
          </p>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------- Coverage */

export function CoverageView({ summary, taxonomy, failures }) {
  const sources = summary?.sources ?? [];
  const off = sources.filter((s) => !s.present);

  return (
    <div className="stack">
      <div className="card">
        <h2>Reference data</h2>
        <p className="note">
          A missing feed silently disables checks. Anything marked off is not
          being audited at all — the invoices are not clean, they are unchecked.
        </p>
        <div className="sources">
          {sources.map((s) => (
            <div className="source" key={s.name}>
              <span className="nm">{s.name}</span>
              <span className="pth">{s.path}</span>
              <span className={`st ${s.present ? "on" : "off"}`}>
                {s.present ? "wired" : "missing"}
              </span>
              <span className="en">{s.enables}</span>
            </div>
          ))}
        </div>
        {off.length > 0 && (
          <p className="note">
            ⚠ {plural(off.length, "feed")} missing: {off.map((s) => s.enables).join("; ")}.
          </p>
        )}
      </div>

      {failures?.length > 0 && (
        <div className="card">
          <h2>Documents that could not be read</h2>
          <p className="note">
            These never reached the audit. A read failure is not a clean invoice.
          </p>
          <div className="sources">
            {failures.map((f) => (
              <div className="source" key={f.path}>
                <span className="pth">{f.path}</span>
                <span className="en">{f.error}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="card">
        <h2>Flag taxonomy</h2>
        <div className="tablewrap">
          <table>
            <thead>
              <tr>
                <th>Flag</th>
                <th>Severity</th>
                <th>Detection</th>
                <th>Needs</th>
                <th>What it means</th>
              </tr>
            </thead>
            <tbody>
              {(taxonomy ?? []).map((f) => (
                <tr key={f.id}>
                  <td className="mono">{f.id}</td>
                  <td>
                    <span className={`sev-dot ${f.severity}`} />
                    {f.severity}
                  </td>
                  <td className="mono">{f.detection}</td>
                  <td className="mono">{f.inputs}</td>
                  <td>{f.description}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
