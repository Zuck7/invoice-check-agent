const SYMBOL = { USD: "$", GBP: "£", EUR: "€" };

// Money crosses the wire as a string so it never passes through a float. It is
// only converted here, for display, and never fed back into a calculation.
export function money(value, currency = "USD") {
  const n = Number(value ?? 0);
  const sym = SYMBOL[currency] ?? `${currency} `;
  return (
    (n < 0 ? "-" : "") +
    sym +
    Math.abs(n).toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })
  );
}

export const pct = (value) =>
  value == null ? "—" : `${Number(value).toFixed(1)}%`;

export const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

export function relative(iso) {
  if (!iso) return "never";
  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}
