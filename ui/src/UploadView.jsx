import { useCallback, useRef, useState } from "react";
import * as api from "./api";
import { Empty } from "./components";
import { money, plural } from "./format";

const ACCEPT = ".csv,.pdf,.png,.jpg,.jpeg";

export default function UploadView({ canWrite, onAudited }) {
  const inputRef = useRef(null);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [results, setResults] = useState([]);

  const send = useCallback(
    async (files) => {
      if (!files?.length || busy) return;
      setBusy(true);
      setError(null);
      const done = [];
      for (const file of files) {
        try {
          done.push(await api.uploadInvoice(file));
        } catch (err) {
          setError(`${file.name}: ${err.message}`);
        }
      }
      if (done.length) {
        setResults((current) => [...done, ...current]);
        onAudited?.();
      }
      setBusy(false);
    },
    [busy, onAudited]
  );

  const onDrop = (event) => {
    event.preventDefault();
    setDragging(false);
    send(Array.from(event.dataTransfer.files ?? []));
  };

  if (!canWrite) {
    return (
      <Empty>
        <p><strong>Read-only.</strong></p>
        <p className="note">
          Uploading changes the audit, so it needs the access link. Open the URL
          the server printed when it started — it carries your token.
        </p>
      </Empty>
    );
  }

  return (
    <div className="stack">
      <div
        className={`dropzone${dragging ? " over" : ""}${busy ? " busy" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        onClick={() => !busy && inputRef.current?.click()}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") inputRef.current?.click();
        }}
        role="button"
        tabIndex={0}
        aria-label="Upload an invoice"
      >
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPT}
          multiple
          hidden
          onChange={(e) => {
            send(Array.from(e.target.files ?? []));
            e.target.value = "";
          }}
        />
        <div className="dz-icon" aria-hidden="true">⇪</div>
        <h2>{busy ? "Checking…" : "Drop an invoice here"}</h2>
        <p className="note">
          {busy
            ? "Reading the document and checking it against the rate card."
            : "or click to choose a file — CSV, PDF, or a photo of a scan"}
        </p>
        <p className="dz-hint">
          A PDF with a text layer is read exactly; a scan goes to the vision
          model. Either way the numbers are checked by the same code.
        </p>
      </div>

      {error && (
        <div className="card">
          <h3 className="bad-title">That file was not audited</h3>
          <p className="note">{error}</p>
        </div>
      )}

      {results.length === 0 && !busy && (
        <p className="note">
          Uploaded invoices join the queue and appear under Invoices with their
          margin. Nothing is deleted — a re-upload of the same document is
          caught as a duplicate rather than billed twice.
        </p>
      )}

      {results.map((r) => (
        <Verdict key={r.content_hash} result={r} />
      ))}
    </div>
  );
}

function Verdict({ result }) {
  const exposure = Number(result.exposure || 0);
  const rr = result.rerate;
  const clean = result.flag_count === 0;

  const tone = result.blocked_from_rerate ? "bad" : clean ? "good" : "warn";
  const headline = result.blocked_from_rerate
    ? "Blocked — do not pay or re-rate yet"
    : clean
      ? "Clean — nothing to query"
      : `${plural(result.flag_count, "issue")} found`;

  return (
    <div className={`verdict ${tone}`}>
      <div className="v-head">
        <div>
          <span className="eyebrow">{result.filename}</span>
          <h2>{headline}</h2>
          <p className="note">
            {result.invoice_no} · {result.warehouse_id}→{result.client_id} ·{" "}
            {result.period[0]} to {result.period[1]}
          </p>
        </div>
        <div className="v-nums">
          <div>
            <span className="k">billed</span>
            <span className="v">{money(result.stated_total, result.currency)}</span>
          </div>
          {exposure !== 0 && (
            <div>
              <span className="k">{exposure > 0 ? "over-billed" : "under-billed"}</span>
              <span className={`v ${exposure > 0 ? "neg" : ""}`}>
                {money(Math.abs(exposure), result.currency)}
              </span>
            </div>
          )}
          {rr && !rr.blocked && (
            <div>
              <span className="k">margin</span>
              <span className="v pos">{rr.margin_display}</span>
            </div>
          )}
        </div>
      </div>

      {result.flags.length > 0 && (
        <div className="rows">
          {result.flags.map((f) => (
            <div className={`row ${f.severity}`} key={f.fingerprint}>
              <div className="row-top">
                <span className="flagid">{f.flag}</span>
                <span className="where">
                  {f.line_no ? `line ${f.line_no}` : "invoice"}
                </span>
                {Number(f.delta) !== 0 && (
                  <span className={`delta ${Number(f.delta) > 0 ? "over" : "under"}`}>
                    {f.delta_display}
                  </span>
                )}
              </div>
              {f.line_description && <div className="desc">{f.line_description}</div>}
              <p className="msg">{f.message}</p>
            </div>
          ))}
        </div>
      )}

      {result.notes.map((n, i) => (
        <p className="note" key={i}>note: {n}</p>
      ))}

      {result.blocked_from_rerate && rr?.blocked_by?.length > 0 && (
        <p className="note">
          <strong>Why blocked:</strong> {rr.blocked_by.join("; ")}
        </p>
      )}
    </div>
  );
}
