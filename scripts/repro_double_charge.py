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
    python3 scripts/repro_double_charge.py --cleanup-only --ids 118,119 --restore-balance 24800.00 --high-water 119

Requires the stack up (`make up`) and PROCESSOR_API_KEY set, or payment-service fails closed
at /health and refuses every charge. Note that no processor is actually contacted: the only
outbound call in `charge()` is the servicing apply hop (payment-service/app/payments.py:96).

Writes real rows to the local dev database and moves a real balance. Cleanup is on by default
and deletes ONLY the payment ids this run's own POST responses returned — never a database
census diff, so a concurrent unrelated payment for the same loan is never swept into the
delete set. If such a concurrent write IS detected, cleanup is refused ENTIRELY — neither the
rows nor the balance are touched — because deleting our rows would orphan their debit in the
shared balance and restoring the opening would stomp the foreign movement, and there is no
ledger (D2) to reconcile either automatically; the full state is printed for a human.
`--cleanup-only` cleans up after a `--keep` run and needs the explicit `--ids` list, the
opening balance, the high-water id AND the closing-balance watermark that run printed. It
refuses without them; refuses if any payment row for the loan has an id past the high-water — a
payment that landed after the `--keep` run, which restoring a stale absolute balance would
erase; and refuses if the live balance no longer equals the watermark, checked under a row lock
in the same transaction as the delete and the restore. That last check is not redundant: the
balance also moves with no `payments` row behind it, via `POST /accounts/{id}/adjust-balance`
and `/waive-fee` (`servicing-service/app/main.py`), so an id high-water alone would let the
absolute restore overwrite a legitimate adjustment.

Each case's verdict counts ONLY the rows this run's own responses returned, never a census
diff: a payment another actor inserts for the same loan mid-case would otherwise be counted as
a charge from this one customer intent and inflate the reported row count. If such a row
appears, the case reports CONTAMINATED and the run's verdict is INVALID rather than
DOUBLE-CHARGE CONFIRMED.

This script is the pre-fix RED reproduction only. It posts the legacy `pan`/`cvv` body with
no `Idempotency-Key`; once the Week 5 fix lands, `POST /payments` rejects that body with `400`
by design (spec criteria 4, 11; vectors T1/T2/R4/R5), so this script is EXPECTED to fail at
the first charge and is not the green verification. The green side is the R/A/T contract-test
suite (build-week criterion 18), which sends a hosted-field token plus an `Idempotency-Key`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

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


def created_id(status: int, body: object) -> int | None:
    """The `payment_id` a successful charge inserted, or None. This is the only run-owned
    handle on a row: cleanup deletes the ids the responses returned, never a database census
    diff, so a concurrent unrelated payment for the same loan is never swept into the delete
    set."""
    if status not in (200, 201) or not isinstance(body, dict):
        return None
    pid = body.get("payment_id")
    return pid if isinstance(pid, int) else None


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


def invalidate(case: str, foreign: set[int]) -> None:
    """A row this run did not create appeared inside `case`'s census window, so the case has no
    evidentiary value: the count of charges produced by one customer intent is no longer
    separable from another actor's writes, and the balance movement mixes both. Report the
    contamination instead of a verdict — an inflated DOUBLE-CHARGE count is worse than no
    count, because the whole branch rests on that number."""
    print(f"\n  {case}")
    print(
        f"    CONTAMINATED: payment row(s) {sorted(foreign)} appeared on this loan during the "
        f"case but came from no response of this run. Not counted, and no verdict reported for "
        f"this case."
    )


class CleanupMismatch(Exception):
    """Raised when cleanup refused server-side: the DELETE below did not remove exactly `ids`
    for `loan_id` (a mistyped --loan-id, a stale id, or an id that belongs to a different
    loan), or the loan's balance no longer matches the watermark the caller recorded. Both
    checks, the DELETE and the balance UPDATE run inside one DO block, so a refusal raises
    before either mutation executes and the whole statement is rolled back: nothing is
    deleted or restored."""


def cleanup(
    loan_id: int, ids: set[int], restore_to: float, expect_balance: float
) -> None:
    """Delete this run's payment rows and restore `restore_to`, or refuse and mutate nothing.

    `expect_balance` is the balance the caller last observed for this loan. It is re-read
    FOR UPDATE inside the same DO block as the delete and the restore, and any difference
    raises before either mutation. A payment-id high-water does NOT cover this: the balance
    has mutation paths that insert no `payments` row at all — `POST /accounts/{id}/adjust-balance`
    and `POST /accounts/{id}/waive-fee` (servicing-service/app/main.py:101,113) write
    `balances.balance` directly. Restoring an absolute saved balance over such a movement
    erases it, and there is no ledger (D2) to recover it from.

    Only ever called once the caller has proven no foreign payment row appeared on this loan:
    deleting our rows and restoring an absolute balance are safe together ONLY then. A foreign
    write present means deleting rows would leave their debit orphaned in the shared balance
    AND restoring the opening would stomp the foreign movement — so both callers refuse
    cleanup entirely in that case rather than pass a "restore or not" flag here.
    """
    # The DELETE is scoped to loan_id (not id alone) and counts what actually came out. A
    # mistyped --loan-id with otherwise-valid ids used to delete those payment rows (wherever
    # they actually lived) while unconditionally resetting THIS loan_id's balance — orphaning
    # one loan's rows and stomping another's balance in one call. Both guards and both
    # mutations run in one DO block so every check happens before any write, in the same
    # server-side transaction: the psql CLI has no cross-invocation transaction, so this is the
    # only way to keep "verify before mutate" atomic against a real refusal instead of a
    # Python-side check that runs after the DELETE has already committed.
    delete_stanza = ""
    if ids:
        id_list = ",".join(str(i) for i in sorted(ids))
        delete_stanza = f"""
    DELETE FROM payments WHERE loan_id = {loan_id} AND id IN ({id_list});
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    IF deleted_count <> {len(ids)} THEN
        RAISE EXCEPTION 'cleanup mismatch: expected % row(s) for loan %, deleted %',
            {len(ids)}, {loan_id}, deleted_count;
    END IF;"""
    sql = f"""
DO $$
DECLARE
    deleted_count int;
    current_balance double precision;
BEGIN
    SELECT balance INTO current_balance FROM balances WHERE loan_id = {loan_id} FOR UPDATE;
    IF current_balance IS NULL THEN
        RAISE EXCEPTION 'cleanup balance drift: loan % has no balances row', {loan_id};
    END IF;
    IF abs(current_balance - {expect_balance}) > 0.005 THEN
        RAISE EXCEPTION 'cleanup balance drift: loan % balance is %, expected % — a balance '
            'change with no payment row behind it (adjust-balance / waive-fee) landed since; '
            'refusing to restore %', {loan_id}, current_balance, {expect_balance}, {restore_to};
    END IF;{delete_stanza}
    UPDATE balances SET balance = {restore_to} WHERE loan_id = {loan_id};
END $$;
"""
    try:
        psql(sql)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        if "balance drift" in stderr:
            raise CleanupMismatch(
                f"cleanup refused: loan {loan_id} no longer holds the {expect_balance} this "
                f"run recorded, so restoring {restore_to} would erase a balance change made "
                f"since — nothing was deleted or restored. Reconcile by hand: this run's rows "
                f"are {sorted(ids)}. {stderr}"
            ) from exc
        raise CleanupMismatch(
            f"cleanup refused: not all of {sorted(ids)} are payments on loan {loan_id} "
            f"— nothing was deleted or restored. {stderr}"
        ) from exc
    print(f"\ncleanup: removed {len(ids)} row(s), balance restored to {restore_to}")


def refuse_cleanup(
    loan_id: int, our_ids: set[int], foreign: set[int], opening: float
) -> None:
    """A concurrent write to this loan was detected. Touch nothing — leave our rows AND the
    balance exactly as they are — and hand the operator the full state. Deleting our rows now
    would orphan their debit in the shared balance; restoring the opening would stomp the
    foreign movement. There is no ledger (D2) to reconcile either automatically."""
    print(
        f"\ncleanup REFUSED: a concurrent write to loan {loan_id} was detected "
        f"(rows {sorted(foreign)} are not ours).\n"
        f"  Left untouched: this run's {len(our_ids)} row(s) {sorted(our_ids)} and the current "
        f"balance.\n"
        f"  Reconcile by hand: the opening balance before this run was {opening}; this run's "
        f"rows are listed above and the foreign row(s) must be preserved."
    )


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
        help="clean up a prior --keep run: needs --ids and --restore-balance",
    )
    ap.add_argument(
        "--ids",
        default=None,
        help="with --cleanup-only: comma-separated payment ids to delete (required). These "
        "are the exact ids the --keep run created and printed — never a range.",
    )
    ap.add_argument(
        "--restore-balance",
        type=float,
        default=None,
        help="with --cleanup-only: the opening balance the --keep run reported (required)",
    )
    ap.add_argument(
        "--high-water",
        type=int,
        default=None,
        help="with --cleanup-only: the max payment id at the end of the --keep run (required). "
        "Any row with a larger id landed after that run and means the loan moved since — "
        "cleanup then refuses rather than restore a stale absolute balance over it.",
    )
    ap.add_argument(
        "--expect-balance",
        type=float,
        default=None,
        help="with --cleanup-only: the CLOSING balance the --keep run printed (required). "
        "Cleanup re-reads the balance under a row lock in the same transaction as the delete "
        "and the restore and refuses on any difference. The payment-id high-water does not "
        "cover this: adjust-balance and waive-fee move the balance with no payment row.",
    )
    args = ap.parse_args()

    if args.cleanup_only:
        # PR review: this used to DELETE and return, skipping cleanup()'s balance restore.
        # The documented use is cleaning up after --keep, and that run has already moved the
        # balance -- so deleting the rows alone leaves the loan debited with no payment
        # behind it, and the next run reads that as its opening balance and reports lost-update
        # arithmetic that is silently wrong. Both halves or neither.
        #
        # --ids is an EXPLICIT list, not a range: an earlier --since-id form deleted every row
        # with id greater than a high-water mark, which took any legitimate payment another
        # actor inserted for the same loan after that mark. Delete exactly the ids the --keep
        # run created and printed, nothing inferred.
        if (
            args.ids is None
            or args.restore_balance is None
            or args.high_water is None
            or args.expect_balance is None
        ):
            sys.exit(
                "--cleanup-only needs --ids, --restore-balance, --high-water and "
                "--expect-balance. Deleting the rows without restoring the balance leaves the "
                "loan debited with no payment behind it; restoring a stale absolute balance "
                "over a payment that landed since the --keep run would erase that payment; and "
                "a payment-id high-water alone does not see a balance change with no payment "
                "row behind it (adjust-balance / waive-fee), which the absolute restore would "
                "also erase. The --keep run printed all four."
            )
        try:
            doomed = {int(x) for x in args.ids.split(",") if x.strip()}
        except ValueError:
            sys.exit(
                f"--ids must be a comma-separated list of integers, got {args.ids!r}"
            )
        # Restoring an ABSOLUTE opening balance is safe only if the loan has not moved since the
        # --keep run. Serial ids are monotonic, so any row with id > the recorded high-water
        # landed after that run and is a legitimate movement this cleanup must not stomp. Our
        # own --ids are all <= high-water (they were created during the --keep run), so this
        # never flags them. On any such movement, refuse entirely: deleting our rows would
        # orphan their debit and the absolute restore would erase the newer payment.
        #
        # The id high-water covers only movement that INSERTED a payment row. A balance change
        # with no such row — adjust-balance / waive-fee — is invisible to it, so cleanup() also
        # verifies --expect-balance against the live balance under a row lock, inside the same
        # transaction as the delete and the restore, and refuses there.
        moved = {i for i in payment_ids(args.loan_id) if i > args.high_water}
        if moved:
            refuse_cleanup(args.loan_id, doomed, moved, args.restore_balance)
            return 1
        try:
            cleanup(args.loan_id, doomed, args.restore_balance, args.expect_balance)
        except CleanupMismatch as exc:
            print(f"\n{exc}")
            return 1
        return 0

    token = login(args.user, args.password)

    status, health = api("GET", "/payments/health", token)
    if status != 200:
        sys.exit(
            f"payment-service is not healthy ({status}): {health}\n"
            "Set PROCESSOR_API_KEY in .env and restart the service."
        )

    print(f"loan {args.loan_id}, {args.amount:.2f} per intent, gateway {GATEWAY}")
    # Record the opening balance and the pre-run row set BEFORE any charge, so the finally
    # block can always undo whatever the mutation phase inserted -- even a partial run.
    opening_all = balance_of(args.loan_id)
    ids_before_all = payment_ids(args.loan_id)
    clean = True
    charge_failure = None
    # Rows that appeared inside a case's census window but came from no response of ours. Their
    # presence invalidates the run's evidence rather than adding to it.
    contaminated_run: set[int] = set()
    # The rows this run conclusively created, keyed by the payment_id each response returned.
    # Cleanup deletes from THIS set only -- never a database census diff, which would sweep a
    # concurrent unrelated payment for the same loan into the delete set.
    created_ids: set[int] = set()

    # Everything below mutates balances. A charge that returns non-2xx, or an uncaught
    # transport exception in the concurrent block, must NOT bail out with rows inserted and
    # the balance debited: the next run would read that as its opening balance and report
    # wrong arithmetic. So the mutation phase runs under try/finally and the finally always
    # restores (unless --keep was asked for). Do not sys.exit inside the try.
    try:
        # --- Case A: sequential retry ---------------------------------------------
        opening = balance_of(args.loan_id)
        before = payment_ids(args.loan_id)
        # Per-case run-owned ids. The verdict is computed from these, NOT from a
        # (current census - before) diff: a payment another actor inserted for this loan
        # during the case would land in that diff and be counted as a charge produced by this
        # one customer intent, inflating the row count and the DOUBLE-CHARGE verdict the
        # branch's argument rests on.
        case_ids: set[int] = set()
        for _ in range(2):
            st, _body = charge(token, args.loan_id, args.amount)
            pid = created_id(st, _body)
            if pid is not None:
                case_ids.add(pid)
                created_ids.add(pid)
            if st not in (200, 201):
                charge_failure = f"charge failed ({st}): {_body}"
                break
        if charge_failure is None:
            after = payment_ids(args.loan_id)
            closing = balance_of(args.loan_id)
            contaminated_run |= (after - before) - case_ids
            if contaminated_run:
                invalidate(
                    "A. RETRY (2 sequential POSTs, one intent)", contaminated_run
                )
            else:
                clean &= report(
                    "A. RETRY (2 sequential POSTs, one intent)",
                    2,
                    case_ids,
                    opening,
                    closing,
                    args.amount,
                )

        # --- Case B: concurrent ----------------------------------------------------
        # Skipped on a Case A charge failure or on a contaminated Case A: the balance census
        # Case B would open with is already polluted, so its arithmetic would be junk too.
        if charge_failure is None and not contaminated_run:
            opening = balance_of(args.loan_id)
            before = payment_ids(args.loan_id)
            case_ids = set()
            with ThreadPoolExecutor(max_workers=args.parallel) as pool:
                futures = [
                    pool.submit(charge, token, args.loan_id, args.amount)
                    for _ in range(args.parallel)
                ]
                results = []
                for fut in as_completed(futures):
                    try:
                        results.append(fut.result())
                    except OSError as exc:
                        # A transport failure (timeout, connection reset, ...) on one worker
                        # must not stop successful siblings from being recorded. The old
                        # `list(pool.map(...))` raised this out of the whole block before any
                        # response was processed into created_ids, so the finally clause saw
                        # those already-inserted rows as (current_ids - ids_before_all) -
                        # created_ids and refused cleanup on the script's own rows.
                        results.append((None, f"transport error: {exc}"))
            for st, body in results:
                pid = created_id(st, body)
                if pid is not None:
                    case_ids.add(pid)
                    created_ids.add(pid)
            codes = [st for st, _ in results]
            failures = sum(1 for st, _ in results if st is None)
            print(
                f"    response codes     : {codes}"
                + (
                    "  (every attempt reported success)"
                    if failures == 0
                    else f"  ({failures} transport failure(s) among {args.parallel} attempts)"
                )
            )
            after = payment_ids(args.loan_id)
            closing = balance_of(args.loan_id)
            case_b = f"B. CONCURRENT ({args.parallel} simultaneous POSTs, one intent)"
            contaminated_run |= (after - before) - case_ids
            if contaminated_run:
                invalidate(case_b, contaminated_run)
            else:
                clean &= report(
                    case_b, args.parallel, case_ids, opening, closing, args.amount
                )

        print("\n" + "=" * 72)
        if charge_failure is not None:
            pass  # the failure itself is reported by the sys.exit below
        elif contaminated_run:
            print(
                "VERDICT: INVALID — a payment row this run did not create appeared on loan "
                f"{args.loan_id}"
            )
            print(
                f"         mid-case (rows {sorted(contaminated_run)}). One intent's charges "
                "are no longer separable from"
            )
            print(
                "         another actor's, so neither case proves anything. Re-run on a "
                "quiet loan."
            )
        elif clean:
            print(
                "VERDICT: no double-charge observed. The spec's premise does not hold here —"
            )
            print("         re-check the branch under test before presenting.")
        else:
            print(
                "VERDICT: DOUBLE-CHARGE CONFIRMED. One customer intent, multiple charges."
            )
            print(
                "         Not customer confusion. Not a UI bug. Two+ successful inserts."
            )
            print("         See docs/scoping/payments-week5.md §3.1.")
        print("=" * 72)
    finally:
        # Rows that appeared during the run but were NOT created by our own POSTs: a concurrent
        # actor writing to the same loan. Their presence makes BOTH halves of cleanup unsafe --
        # deleting our rows would orphan their debit in the shared balance, and restoring the
        # opening would stomp the foreign movement (no ledger to net either, D2) -- so we refuse
        # cleanup entirely and leave our rows AND the balance for a human.
        current_ids = payment_ids(args.loan_id)
        foreign = (current_ids - ids_before_all) - created_ids
        if foreign:
            # A high-water mark computed now would have to be >= the foreign row's id just to
            # let THIS run pass its own check, which means it would also let a later
            # --cleanup-only silently accept that same foreign row instead of refusing it. No
            # --cleanup-only command is safe to print here — refuse the same way the
            # non-keep path does, whether or not --keep was asked for.
            refuse_cleanup(args.loan_id, created_ids, foreign, opening_all)
        elif args.keep:
            # Print an EXPLICIT id list and the high-water mark: --cleanup-only deletes exactly
            # these ids and refuses if any row landed past the high-water, so neither a
            # concurrent row nor a later legitimate payment is ever taken or stomped.
            # The CLOSING balance is the watermark for the restore: --cleanup-only re-reads the
            # live balance and refuses if it has moved since. The high-water id alone cannot see
            # a balance change that inserted no payment row (adjust-balance / waive-fee), which
            # the absolute restore of `opening_all` would otherwise erase.
            id_list = ",".join(str(i) for i in sorted(created_ids))
            high_water = max(current_ids | created_ids, default=0)
            closing_all = balance_of(args.loan_id)
            print(
                f"\n--keep: left {len(created_ids)} row(s) {sorted(created_ids)}; "
                f"opening balance was {opening_all}, balance now {closing_all}"
            )
            print(
                f"        clean up with: python3 scripts/repro_double_charge.py "
                f"--cleanup-only --loan-id {args.loan_id} --ids {id_list or '<none>'} "
                f"--restore-balance {opening_all} --high-water {high_water} "
                f"--expect-balance {closing_all}"
            )
        else:
            try:
                # The balance read here is the watermark for the restore in the same call: it
                # makes the delete-and-restore atomic against a write that lands between this
                # read and the DO block. A non-payment balance change made EARLIER in the run
                # (adjust-balance / waive-fee) is not detectable from here — there is no ledger
                # (D2) to attribute balance movement to rows — but such a run's own arithmetic
                # is already contaminated and its verdict is reported as such.
                cleanup(
                    args.loan_id, created_ids, opening_all, balance_of(args.loan_id)
                )
            except CleanupMismatch as exc:
                print(f"\n{exc}")

    if charge_failure is not None:
        sys.exit(charge_failure)
    # A contaminated run exits non-zero even though it reported no defect: it proved nothing
    # either way, and exit 0 would read as "the system behaved correctly".
    return 0 if (clean and not contaminated_run) else 1


if __name__ == "__main__":
    sys.exit(main())
