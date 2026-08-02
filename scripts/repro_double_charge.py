#!/usr/bin/env python3
"""Reproduce the payment double-charge against the live stack (Week 5 spec evidence).

Dana's position is that the "I was charged twice" tickets are customer confusion. This
script settles that empirically rather than by reading code: it drives the real
`POST /payments` path through the gateway and reports how many `payments` rows and how much
balance movement one customer intent produces.

Two cases, matching the two failure modes the spec separates:

  A. RETRY      — one intent, two sequential POSTs (the 2.4s-then-retry pattern in the logs).
  B. CONCURRENT — one intent, N simultaneous POSTs on separate connections. This is the case
                  no application-level check can close, because both requests pass any
                  read-then-check before either writes.

Expected on a correct system: 1 payment row and 1x the amount debited, in both cases.
Expected on `main` today: N rows and Nx debited, in both cases.

Usage:
    python3 scripts/repro_double_charge.py                     # run, then clean up
    python3 scripts/repro_double_charge.py --keep              # leave the rows for inspection
    python3 scripts/repro_double_charge.py --parallel 5
    python3 scripts/repro_double_charge.py --cleanup-only      # remove rows from a --keep run

Requires the stack up (`make up`) and PROCESSOR_API_KEY set, or payment-service fails closed
at /health and refuses every charge. Note that no processor is actually contacted: the only
outbound call in `charge()` is the servicing apply hop (payment-service/app/payments.py:96).

Writes real rows to the local dev database and moves a real balance. Cleanup is on by
default and deletes only the ids this run created, then restores the opening balance.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

GATEWAY = "http://localhost:8000"
# Published test number, already present in db/init/002_seed.sql. Not real cardholder data.
TEST_PAN = "4111111111111111"


def api(method: str, path: str, token: str | None = None, body: dict | None = None):
    """One gateway call. Returns (status, parsed-json-or-text)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GATEWAY + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


def psql(sql: str) -> str:
    """Query the dev database directly. Used for the row census and for cleanup only."""
    out = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "meridian",
            "-d",
            "meridian",
            "-t",
            "-A",
            "-c",
            sql,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def balance_of(loan_id: int) -> float:
    return float(psql(f"SELECT balance FROM balances WHERE loan_id = {loan_id};"))


def payment_ids(loan_id: int) -> set[int]:
    raw = psql(f"SELECT id FROM payments WHERE loan_id = {loan_id};")
    return {int(x) for x in raw.split() if x}


def login(username: str, password: str) -> str:
    status, body = api(
        "POST", "/auth/login", body={"username": username, "password": password}
    )
    if status != 200:
        sys.exit(f"login failed ({status}): {body}")
    return body["token"]


def charge(token: str, loan_id: int, amount: float) -> tuple[int, object]:
    """One POST /payments. Byte-identical body every time — this is ONE customer intent."""
    return api(
        "POST",
        "/payments/payments",
        token,
        {
            "loan_id": loan_id,
            "pan": TEST_PAN,
            "cvv": "123",
            "amount": amount,
            "method": "card",
        },
    )


def report(
    case: str,
    attempts: int,
    new_ids: set[int],
    opening: float,
    closing: float,
    amount: float,
) -> bool:
    """Print the verdict for one case. Returns True if the system behaved correctly.

    Two independent defects are measured separately, because they have separate fixes:

      * DUPLICATE CAPTURE — rows created > 1 for one intent. No idempotency key, no unique
        charge reference (D19). Fixed by the key + unique index.
      * LOST UPDATE — credit applied to the balance < total captured. The balance mutation
        is an unlocked read-modify-write (servicing-service/app/main.py:80-85), so
        concurrent applies read the same opening value and overwrite each other. Fixed by
        making the mutation a single atomic statement, NOT by the idempotency key.

    Under concurrency the two compound in the customer's disfavour: money is captured and
    then not credited, and with no ledger (D2) there is nothing to reconcile it against.
    """
    charged = round(len(new_ids) * amount, 2)  # every row is a capture
    credited = round(opening - closing, 2)  # what actually reached the balance
    expected = round(amount, 2)

    rows_ok = len(new_ids) == 1
    credit_matches_charge = abs(credited - charged) < 0.005
    money_ok = abs(credited - expected) < 0.005 and rows_ok

    print(f"\n  {case}")
    print(f"    attempts sent      : {attempts} (identical body — one customer intent)")
    print(
        f"    payments rows made : {len(new_ids)}   {'OK' if rows_ok else 'DEFECT'}"
        f"   ids={sorted(new_ids)}"
    )
    print(f"    captured           : {charged:.2f}   expected {expected:.2f}")
    print(f"    balance            : {opening} -> {closing}")
    print(
        f"    credited to loan   : {credited:.2f}"
        f"   {'OK' if credit_matches_charge else 'DEFECT (lost update)'}"
    )
    if not rows_ok:
        print(
            f"    >> DUPLICATE CAPTURE: one intent produced {len(new_ids)} charges "
            f"({charged:.2f} against a {expected:.2f} payment)"
        )
    if not credit_matches_charge:
        print(
            f"    >> LOST UPDATE: {charged:.2f} captured, {credited:.2f} credited — "
            f"{charged - credited:.2f} taken and not applied to the loan"
        )
    return money_ok


def cleanup(loan_id: int, ids: set[int], restore_to: float) -> None:
    if ids:
        psql(
            f"DELETE FROM payments WHERE id IN ({','.join(str(i) for i in sorted(ids))});"
        )
    psql(f"UPDATE balances SET balance = {restore_to} WHERE loan_id = {loan_id};")
    print(f"\ncleanup: removed {len(ids)} row(s), balance restored to {restore_to}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--loan-id", type=int, default=4471)
    ap.add_argument("--amount", type=float, default=100.00)
    ap.add_argument(
        "--parallel",
        type=int,
        default=3,
        help="simultaneous requests for case B (default 3)",
    )
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="password")
    ap.add_argument("--keep", action="store_true", help="do not clean up afterwards")
    ap.add_argument(
        "--cleanup-only",
        action="store_true",
        help="delete rows for this loan above the recorded high-water id",
    )
    ap.add_argument(
        "--since-id",
        type=int,
        default=0,
        help="with --cleanup-only: delete rows with id > this",
    )
    args = ap.parse_args()

    if args.cleanup_only:
        doomed = {i for i in payment_ids(args.loan_id) if i > args.since_id}
        psql(
            f"DELETE FROM payments WHERE id IN ({','.join(str(i) for i in doomed) or '0'});"
        )
        print(f"removed {len(doomed)} row(s) for loan {args.loan_id}")
        return 0

    token = login(args.user, args.password)

    status, health = api("GET", "/payments/health", token)
    if status != 200:
        sys.exit(
            f"payment-service is not healthy ({status}): {health}\n"
            "Set PROCESSOR_API_KEY in .env and restart the service."
        )

    print(f"loan {args.loan_id}, {args.amount:.2f} per intent, gateway {GATEWAY}")
    opening_all = balance_of(args.loan_id)
    ids_before_all = payment_ids(args.loan_id)
    clean = True

    # --- Case A: sequential retry -------------------------------------------------
    opening = balance_of(args.loan_id)
    before = payment_ids(args.loan_id)
    for _ in range(2):
        st, _body = charge(token, args.loan_id, args.amount)
        if st not in (200, 201):
            sys.exit(f"charge failed ({st}): {_body}")
    clean &= report(
        "A. RETRY (2 sequential POSTs, one intent)",
        2,
        payment_ids(args.loan_id) - before,
        opening,
        balance_of(args.loan_id),
        args.amount,
    )

    # --- Case B: concurrent --------------------------------------------------------
    opening = balance_of(args.loan_id)
    before = payment_ids(args.loan_id)
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        results = list(
            pool.map(
                lambda _: charge(token, args.loan_id, args.amount), range(args.parallel)
            )
        )
    codes = [st for st, _ in results]
    clean &= report(
        f"B. CONCURRENT ({args.parallel} simultaneous POSTs, one intent)",
        args.parallel,
        payment_ids(args.loan_id) - before,
        opening,
        balance_of(args.loan_id),
        args.amount,
    )
    print(f"    response codes     : {codes}  (every attempt reported success)")

    print("\n" + "=" * 72)
    if clean:
        print(
            "VERDICT: no double-charge observed. The spec's premise does not hold here —"
        )
        print("         re-check the branch under test before presenting.")
    else:
        print(
            "VERDICT: DOUBLE-CHARGE CONFIRMED. One customer intent, multiple charges."
        )
        print("         Not customer confusion. Not a UI bug. Two+ successful inserts.")
        print("         See docs/scoping-payments-week5.md §3.1.")
    print("=" * 72)

    created = payment_ids(args.loan_id) - ids_before_all
    if args.keep:
        print(
            f"\n--keep: left {len(created)} row(s) {sorted(created)}; "
            f"opening balance was {opening_all}"
        )
    else:
        cleanup(args.loan_id, created, opening_all)
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
