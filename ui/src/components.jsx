import { useEffect, useRef, useState } from "react";
import { money } from "./format";

export function Tiles({ items }) {
  return (
    <div className="tiles">
      {items.map((t) => (
        <div className="tile" key={t.k}>
          <span className="k">{t.k}</span>
          <span className={`v${t.tone ? " " + t.tone : ""}`}>{t.v}</span>
          {t.sub ? <span className="sub">{t.sub}</span> : null}
        </div>
      ))}
    </div>
  );
}

export function Segmented({ options, value, onChange, label }) {
  return (
    <div className="seg" role="group" aria-label={label}>
      {options.map((o) => (
        <button
          key={o.value}
          aria-pressed={value === o.value}
          onClick={() => onChange(o.value)}
          type="button"
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Empty({ children }) {
  return <div className="empty">{children}</div>;
}

export function Toast({ message, tone, onDone }) {
  useEffect(() => {
    if (!message) return;
    const id = setTimeout(onDone, 4000);
    return () => clearTimeout(id);
  }, [message, onDone]);
  if (!message) return null;
  return <div className={`toast${tone === "bad" ? " bad" : ""}`}>{message}</div>;
}

export function FlagRow({ flag, onAct, canWrite }) {
  const where = flag.line_no ? `line ${flag.line_no}` : "invoice";
  const delta = Number(flag.delta || 0);
  const facts = [
    flag.expected && ["expected", flag.expected],
    flag.actual && ["found", flag.actual],
    flag.rate_card_version && ["card", flag.rate_card_version],
    flag.source_page && ["page", String(flag.source_page)],
    ["age", `${flag.age_days}d`],
  ].filter(Boolean);

  return (
    <article className={`row ${flag.severity}${flag.status !== "open" ? " done" : ""}`}>
      <div className="row-top">
        <span className="flagid">{flag.flag}</span>
        <span className="where">
          {flag.invoice_no} · {where} · {flag.warehouse_id}→{flag.client_id}
        </span>
        {delta !== 0 && (
          <span className={`delta ${delta > 0 ? "over" : "under"}`}>
            {money(Math.abs(delta), flag.currency)} {delta > 0 ? "over" : "under"}
          </span>
        )}
      </div>
      {flag.line_description && <div className="desc">{flag.line_description}</div>}
      <p className="msg">{flag.message}</p>
      <div className="facts">
        {facts.map(([k, v]) => (
          <span className="fact" key={k}>
            <b>{k}</b> {v}
          </span>
        ))}
        {flag.escalated && flag.status === "open" && (
          <span className="fact esc">escalated</span>
        )}
      </div>
      <div className="row-foot">
        <span className="fp">{flag.fingerprint.slice(0, 8)}</span>
        {flag.resolution && (
          <span className="resolution">
            {flag.status} by {flag.resolved_by}: {flag.resolution}
          </span>
        )}
        <span className="acts">
          {flag.status === "open" ? (
            <>
              <button
                className="btn primary"
                disabled={!canWrite}
                onClick={() => onAct(flag, "resolve")}
                type="button"
              >
                Resolve
              </button>
              <button
                className="btn"
                disabled={!canWrite}
                onClick={() => onAct(flag, "dismiss")}
                type="button"
              >
                Dismiss
              </button>
            </>
          ) : (
            <button
              className="btn"
              disabled={!canWrite}
              onClick={() => onAct(flag, "reopen")}
              type="button"
            >
              Reopen
            </button>
          )}
        </span>
      </div>
    </article>
  );
}

export function ResolveDialog({ open, action, flag, onSubmit, onClose }) {
  const ref = useRef(null);
  const [who, setWho] = useState(() => localStorage.getItem("ia_reviewer") || "");
  const [note, setNote] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    if (open && !node.open) node.showModal();
    if (!open && node.open) node.close();
    if (open) {
      setNote("");
      setError("");
    }
  }, [open]);

  if (!flag) return null;
  const dismissing = action === "dismiss";

  const submit = (event) => {
    event.preventDefault();
    if (!who.trim() || !note.trim()) {
      // The server refuses these too. Saying so here saves a round trip and
      // explains why, rather than surfacing a bare 400.
      setError("Both fields are required — a resolution with no name or reason is not an audit trail.");
      return;
    }
    localStorage.setItem("ia_reviewer", who.trim());
    onSubmit({ action, by: who.trim(), note: note.trim() });
  };

  return (
    <dialog ref={ref} onClose={onClose} onCancel={onClose}>
      <form className="dlg" onSubmit={submit}>
        <h2>{dismissing ? "Dismiss" : "Resolve"} {flag.flag}</h2>
        <p className="note">
          {flag.invoice_no}
          {flag.line_no ? ` · line ${flag.line_no}` : ""} — {flag.message}
        </p>
        <div>
          <label htmlFor="who">Who decided</label>
          <input id="who" value={who} onChange={(e) => setWho(e.target.value)} autoComplete="name" />
        </div>
        <div>
          <label htmlFor="note">
            {dismissing ? "Why this is not a real finding" : "What happened"}
          </label>
          <textarea
            id="note"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder={
              dismissing
                ? "Our card was stale; the warehouse billed correctly."
                : "Credit CN-118 received, applied to the September invoice."
            }
          />
        </div>
        {error && <p className="err">{error}</p>}
        <div className="btns">
          <button className="btn" type="button" onClick={onClose}>Cancel</button>
          <button className={`btn ${dismissing ? "danger" : "primary"}`} type="submit">
            {dismissing ? "Dismiss" : "Resolve"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
