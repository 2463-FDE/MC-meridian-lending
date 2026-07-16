// Status chip with consumer-lending color semantics:
//   current / approved        -> green
//   delinquent / past_due     -> red
//   denied                    -> red-muted
//   paid_off                  -> gray
//   refer / pending           -> amber
// Falls back to a neutral gray chip for any unknown status.

const TONE: Record<string, string> = {
  current: "chip-green",
  approve: "chip-green", // decision-service outcome enum
  approved: "chip-green",
  active: "chip-green",
  delinquent: "chip-red",
  past_due: "chip-red",
  default_: "chip-red",
  decline: "chip-red-muted", // decision-service outcome enum
  denied: "chip-red-muted",
  declined: "chip-red-muted",
  paid_off: "chip-gray",
  closed: "chip-gray",
  refer: "chip-amber",
  pending: "chip-amber",
  in_review: "chip-amber",
  review: "chip-amber",
};

function label(status: string): string {
  return status
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export default function StatusChip({ status }: { status?: string | null }) {
  const key = (status || "unknown").toLowerCase();
  const tone = TONE[key] || "chip-gray";
  return <span className={`chip ${tone}`}>{label(status || "Unknown")}</span>;
}
