// The token arrives as ?token=… from the CLI's launch URL and is kept in
// sessionStorage so a refresh does not lose write access. Reads are open on
// loopback; only mutations carry it.
const params = new URLSearchParams(location.search);
const fromUrl = params.get("token");
if (fromUrl) {
  sessionStorage.setItem("ia_token", fromUrl);
  params.delete("token");
  const rest = params.toString();
  history.replaceState({}, "", location.pathname + (rest ? "?" + rest : ""));
}

export const token = () => sessionStorage.getItem("ia_token") || "";
export const hasToken = () => Boolean(token());

async function request(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.method === "POST" ? { Authorization: `Bearer ${token()}` } : {}),
      ...options.headers,
    },
  });
  const text = await res.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    throw new Error(text.slice(0, 200) || `HTTP ${res.status}`);
  }
  if (!res.ok) throw new Error(payload?.error || `HTTP ${res.status}`);
  return payload;
}

export const getSummary = () => request("/api/summary");
export const getFlags = () => request("/api/flags");
export const getInvoices = () => request("/api/invoices");
export const getTrends = () => request("/api/trends");
export const getTaxonomy = () => request("/api/taxonomy");
export const getMode = () => request("/api/mode");

export const actOnFlag = (fingerprint, body) =>
  request(`/api/flags/${fingerprint}`, {
    method: "POST",
    body: JSON.stringify(body),
  });

export async function uploadInvoice(file) {
  // Raw bytes with the name in a header — no multipart parser on the server.
  const res = await fetch("/api/upload", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token()}`,
      "Content-Type": "application/octet-stream",
      "X-Filename": encodeURIComponent(file.name),
    },
    body: file,
  });
  const text = await res.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    throw new Error(text.slice(0, 200) || `HTTP ${res.status}`);
  }
  if (!res.ok) throw new Error(payload?.error || `HTTP ${res.status}`);
  return payload;
}

export const rescan = () =>
  request("/api/rescan", { method: "POST", body: JSON.stringify({}) });
