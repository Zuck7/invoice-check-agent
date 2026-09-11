import { useCallback, useEffect, useState } from "react";
import * as api from "./api";
import { ResolveDialog, Toast } from "./components";
import { relative } from "./format";
import UploadView from "./UploadView";
import { CoverageView, InvoicesView, QueueView, TrendsView } from "./views";

const TABS = [
  { id: "check", label: "Check an invoice" },
  { id: "queue", label: "Queue" },
  { id: "invoices", label: "Invoices" },
  { id: "trends", label: "Trends" },
  { id: "coverage", label: "Coverage" },
];

export default function App() {
  const [tab, setTab] = useState("check");
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState(null);
  const [pending, setPending] = useState(null);

  const canWrite = api.hasToken();

  const load = useCallback(async () => {
    try {
      const [summary, flags, invoices, trends, taxonomy] = await Promise.all([
        api.getSummary(),
        api.getFlags(),
        api.getInvoices(),
        api.getTrends(),
        api.getTaxonomy(),
      ]);
      setData({ summary, flags, invoices, trends, taxonomy });
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const act = useCallback(
    (flag, action) => {
      if (action === "reopen") {
        submit(flag, { action: "reopen" });
        return;
      }
      setPending({ flag, action });
    },
    [] // eslint-disable-line react-hooks/exhaustive-deps
  );

  async function submit(flag, body) {
    try {
      const updated = await api.actOnFlag(flag.fingerprint, body);
      setData((current) => ({
        ...current,
        flags: current.flags.map((f) =>
          f.fingerprint === updated.fingerprint ? updated : f
        ),
      }));
      setToast({ message: `${updated.flag} ${updated.status}`, tone: "ok" });
    } catch (err) {
      setToast({ message: `Could not save: ${err.message}`, tone: "bad" });
    } finally {
      setPending(null);
    }
  }

  async function doRescan() {
    setBusy(true);
    try {
      await api.rescan();
      await load();
      setToast({ message: "Re-audited every invoice on disk", tone: "ok" });
    } catch (err) {
      setToast({ message: `Rescan failed: ${err.message}`, tone: "bad" });
    } finally {
      setBusy(false);
    }
  }

  if (error) {
    return (
      <div className="app">
        <div className="empty">
          <p><strong>Cannot reach the audit server.</strong></p>
          <p className="note">{error}</p>
          <p className="note">
            Start it with <code>invoice-audit serve</code>, then reload.
          </p>
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="app">
        <div className="empty">Loading the audit…</div>
      </div>
    );
  }

  const { summary, flags, invoices, trends, taxonomy } = data;
  const openCount = flags.filter((f) => f.status === "open").length;
  const escalated = flags.filter((f) => f.status === "open" && f.escalated).length;
  const missingFeeds = (summary.sources ?? []).filter((s) => !s.present).length;

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <p className="eyebrow">AMZ Prep · buy-side invoice audit</p>
          <h1>Invoice Audit</h1>
        </div>
        <div className="top-right">
          {!canWrite && (
            <span className="chip warn" title="Open the URL the CLI printed, including its token">
              read only
            </span>
          )}
          <span className="chip">scanned {relative(summary.scanned_at)}</span>
          <button className="btn" onClick={doRescan} disabled={busy} type="button">
            {busy ? "Rescanning…" : "Rescan"}
          </button>
        </div>
      </header>

      <nav className="tabs">
        {TABS.map((t) => (
          <button
            key={t.id}
            aria-current={tab === t.id ? "page" : undefined}
            onClick={() => setTab(t.id)}
            type="button"
          >
            {t.label}
            {t.id === "queue" && openCount > 0 && (
              <span className={`badge${escalated ? " alarm" : ""}`}>{openCount}</span>
            )}
            {t.id === "coverage" && missingFeeds > 0 && (
              <span className="badge alarm">{missingFeeds}</span>
            )}
          </button>
        ))}
      </nav>

      {tab === "check" && <UploadView canWrite={canWrite} onAudited={load} />}
      {tab === "queue" && (
        <QueueView flags={flags} summary={summary} onAct={act} canWrite={canWrite} />
      )}
      {tab === "invoices" && <InvoicesView invoices={invoices} />}
      {tab === "trends" && <TrendsView trends={trends} />}
      {tab === "coverage" && (
        <CoverageView summary={summary} taxonomy={taxonomy} failures={summary.read_failures ? data.failures : []} />
      )}

      <footer>
        {summary.invoices} invoices audited · straight-through {summary.straight_through ?? "—"}%
        · escalation at {summary.escalation_days} days, inside the shortest dispute window.
        {!canWrite && " Reopen the link the CLI printed to resolve flags."}
      </footer>

      <ResolveDialog
        open={Boolean(pending)}
        action={pending?.action}
        flag={pending?.flag}
        onSubmit={(body) => submit(pending.flag, body)}
        onClose={() => setPending(null)}
      />
      <Toast
        message={toast?.message}
        tone={toast?.tone}
        onDone={() => setToast(null)}
      />
    </div>
  );
}
