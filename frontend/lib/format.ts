// Money + percentage formatting helpers — used across the whole app so that
// every dollar figure renders identically (consumer-lending convention).

const USD = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
});

/** Format a number as USD, e.g. 12345.6 -> "$12,345.60". Tolerates undefined/null/strings. */
export function usd(value: number | string | null | undefined): string {
  const n = typeof value === "string" ? parseFloat(value) : value;
  if (n === null || n === undefined || Number.isNaN(n as number)) return "$0.00";
  return USD.format(n as number);
}

/** Format a numeric rate as a percentage, e.g. 7.99 -> "7.99%". */
export function pct(value: number | string | null | undefined, digits = 2): string {
  const n = typeof value === "string" ? parseFloat(value) : value;
  if (n === null || n === undefined || Number.isNaN(n as number)) return "—";
  return `${(n as number).toFixed(digits)}%`;
}

/**
 * Mask an SSN for display, e.g. "123-45-6789" -> "***-**-6789". Enough for the applicant to
 * confirm they typed the right number without putting all nine digits back on screen after
 * the input masked them. Fails closed: anything that is not exactly nine digits (with or
 * without dashes) is masked entirely rather than partially echoed, so a half-typed or
 * malformed value never leaks a prefix. This decides what is DISPLAYED only — the apply
 * wizard's own validation is what rejects a malformed SSN.
 */
export function maskSsn(ssn: string): string {
  const digits = ssn.replace(/\D/g, "");
  if (digits.length !== 9) return ssn ? "•••••••••" : "";
  return `***-**-${digits.slice(5)}`;
}

/** Format an ISO date / date string as a short US date, e.g. "May 25, 2026". */
export function shortDate(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}
