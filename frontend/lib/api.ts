export const GATEWAY_URL =
  process.env.NEXT_PUBLIC_GATEWAY_URL || "http://localhost:8000";

export async function apiGet(path: string) {
  const res = await fetch(`${GATEWAY_URL}${path}`, { cache: "no-store" });
  return res.json();
}

export async function apiPost(path: string, body: unknown) {
  const res = await fetch(`${GATEWAY_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}
