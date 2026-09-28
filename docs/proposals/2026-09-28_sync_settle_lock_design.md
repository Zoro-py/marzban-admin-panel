# Design — closing the sync↔settle window (the D8 remainder, audit-1.4)

**Status: DESIGN, not implemented** — D8 deferred this on purpose ("پنجرهٔ رقابت باریک و ریسک
تغییر بزرگ"); this document is the mandated design so the decision is no longer pending. The
change itself is a small, isolated diff when the owner approves it.

## The two writers

Every money path on the API side runs under one shared in-process lock:
`services.billing_lock` (an `asyncio.Lock`, services.py ~line 520) applied by
`@serialise_billing` — accounts settle/reset/adjust, the delegate trio, payg_monthly, and every
other money-moving endpoint.

The sync job has **its own** lock: `sync_job._sync_lock` gates whole sync *runs* against each
other (and against a manual /api/sync/run), but the money it posts inside a run holds only that:

- `_maybe_settle_payg_cap_hit` (~line 392) — payg cap-hit charge + usage reset;
- `_activate_next_plan` (~line 544) — the ended plan's auto-settle charge, captured from
  `used_traffic` before the reset, with its consumption attribution.

An API settle for the **same account** interleaving between a sync-side read of the meter and
its commit+reset can double-count or split the same consumption across two charges (or reset a
meter twice while only one charge exists). The window is narrow — the same account must be
touched by both within one sync tick — which is exactly why it survived two audits as "known,
documented" instead of being reproduced in the wild.

## Why an asyncio lock is the right mechanism here

The deployment is **one process over SQLite**. Row-level locks (`with_for_update`) are
effectively a no-op against SQLite's whole-database write locking, and multi-process
serialization is not a real threat model for this panel. The honest serialization point is the
event loop itself — the same reason `serialise_billing` exists at all.

**Deadlock safety (the key correctness argument):** the lock order is always
`_sync_lock → billing_lock` (a sync run takes its own lock first, then the billing lock for a
money site), while API endpoints take `billing_lock` alone, and `/api/sync/run` takes
`_sync_lock` alone. No path acquires `billing_lock → _sync_lock`, so no cycle exists.

## The critical section

It must span **read → compute → write → commit → Marzban reset**:

1. read the account's meter and baseline;
2. compute the billable amount and consumption attribution;
3. write the LedgerEntry + advance the baseline/epoch;
4. commit;
5. call Marzban's reset (the reset is what makes the *next* reader see a clean meter).

Releasing after step 4 but before step 5 reopens the window (a settle between commit and reset
reads the same usage). Holding an asyncio lock across an outbound HTTP call mirrors exactly what
`serialise_billing` endpoints already do — their handlers call Marzban inside the lock, bounded
by the client's timeout. Worst case cost: one slow Marzban call delays one concurrent settle by
up to that timeout. That risk already exists API-vs-API today; this change does not enlarge it.

## The change, when approved

- `sync_job.py`: `from app.services import billing_lock`; wrap the two sites above in
  `async with billing_lock:` — nothing else moves. No DB change, no env change, no frontend
  change. One HIGH commit, isolated, with the before/after money diff in its message.
- Red test first, in the `test_delegate_concurrency.py` style: run
  `_maybe_settle_payg_cap_hit` and the settle endpoint concurrently via `asyncio.gather` on one
  account, with a suspension point injected at the Marzban await so the interleaving actually
  happens; before the fix the ledger shows doubled/missing consumption, after it exactly the
  two expected charges. Then the full suite + the ledger invariant.

## Rejected alternatives

- **Unify every money path into one `settle_core()`** used by API and sync alike — the cleaner
  long-term shape, but it rewrites every money path at once; that blast radius is why D8 was
  deferred in the first place. The two-line lock share delivers the same correctness now;
  unification can follow later, on top of it.
- **`with_for_update` row locks** — meaningless on SQLite (see above).
- **Idempotency fingerprints on settlements** — a schema change and a dedup-policy decision
  (which retry is "the same" settlement?) for a window the lock closes outright.
