# Proposal — remediation for the adjust double-billing (MONEY-ADJ-1) on live data

**Status: PROPOSAL ONLY — nothing here has been executed on live data** (red line: no live
financial writes from this session). The code fix shipped on branch
`fix/vpn-full-review-2026-09-27` (commit `e1bf05b`); it prevents FUTURE doubles but does not
touch already-billed rows or the currently-wrong `billed_data_limit` baselines. Those need the
owner's word and an operator-executed run.

**Evidence base:** read-only copy of the live France DB taken 2026-09-27
(`backend/vpn_audit_copy_20260927.db`, integrity ok; 163 accounts / 155 customers / 329 ledger
rows / signed sum 17,729,176.09 T). Root cause and the code contract: D14 in `docs/DECISIONS.md`.

## How the double-billing works (one paragraph)

`/api/accounts/{id}/adjust` grew `data_limit` but never `billed_data_limit`. Both callers that
bill an extension immediately (dashboard Adjust section with "record a debt" checked; bot
`/extend <u> <days> <gb>`) post a charge for the added GB — but the same GB also stayed in
`pending` (data_limit − billed_data_limit), so the NEXT prepay settle charged it a second time.

## Confirmed historical damage — account 39 «Benyamin» (customer 35)

AccountEvent trail between the two charges shows NO reset of any kind, and both amounts are
exactly 60 GB × 5,000 T/GB:

| ledger id | date | row | amount | what it is |
|---|---|---|---|---|
| 59 | 07-28 16:13 | charge "Package settlement…" | 400,000 | the regular 80 GB package (correct) |
| 60 | 07-28 16:13 | credit "Payment received…" | 400,000 | paid (correct) |
| 62 | 07-28 20:28 | charge "+60GB for Benyamin" | **300,000** | adjust extension, invoiced immediately |
| 63 | 07-28 20:28 | credit (manual) | 299,998 | customer paid it |
| 103 | 08-10 11:15 | charge "Package settlement for cycle ending 2026-08-10" | **300,000** | the SAME 60 GB billed AGAIN by the settle (data 140 − billed 80 = 60 GB) |
| 104 | 08-10 11:15 | credit "Payment received…" | 300,002 | customer paid it AGAIN |

**Overcharge: 300,000 Toman real money** (the customer paid twice for one +60 GB).

### Proposed remediation — owner decides, then operator executes

```sql
-- Option A: credit the overcharge back (a refund for the double-billed 60 GB).
-- BEFORE: Benyamin posted balance = X.  AFTER: X - 300000.
INSERT INTO ledgerentry
    (date, type, amount, customer_id, account_id, note, source, created_by)
SELECT datetime('now'), 'credit', 300000.0, 35, 39,
       'Refund: +60GB of 2026-07-28 was billed twice (adjust charge #62 and settlement charge #103)',
       'web', 'operator:manual-remediation-2026-09';
```

Option B (do nothing): the owner may already have compensated this customer outside the panel.
This is a business decision, not a technical one — the panel cannot know.

## Currently at-risk state — will double-bill at the NEXT prepay settle if not corrected

Both accounts have a paid adjust-charge whose GB is still inside `pending` (billed_data_limit=0
because the current package was auto-activated fresh and the adjust flag didn't exist yet):

| account | customer | state today | what the next settle would bill | correct target |
|---|---|---|---|---|
| 108 `Sobar_new` (customer 100) | prepay, data 75 GB, billed 0 GB, adjust-charge **125,000** (25 GB, id 276, 09-20) | pending = 75 GB | 75 GB — the 25 GB adjust portion **a second time** | settle should bill only the 50 GB plan |
| 128 `erfan-blue` (customer 120) | prepay, data 50 GB, billed 0 GB, adjust-charge **150,000** (30 GB, id 243, 09-08) | pending = 50 GB | 50 GB — the 30 GB adjust portion **a second time** | settle should bill only the 20 GB plan |

### Proposed correction (billing baseline only — it writes NO ledger row and changes no past money)

```sql
-- Mark the already-invoiced adjust GB as billed, exactly what bill_added_gb=true
-- does going forward. BEFORE/after pending shown per row.
UPDATE account SET billed_data_limit = 25 * 1073741824 WHERE id = 108;  -- pending 75 GB -> 50 GB
UPDATE account SET billed_data_limit = 30 * 1073741824 WHERE id = 128;  -- pending 50 GB -> 20 GB
```

Verification query to run BEFORE and AFTER (numbers must move exactly as stated above, ledger
signed sum must stay 17,729,176.09):

```sql
SELECT id, marzban_username, data_limit/1073741824.0 AS data_gb,
       billed_data_limit/1073741824.0 AS billed_gb,
       (data_limit - billed_data_limit)/1073741824.0 AS pending_gb
FROM account WHERE id IN (108, 128);
SELECT ROUND(SUM(CASE WHEN type='charge' THEN amount ELSE -amount END),2) FROM ledgerentry;
```

## Reviewed and NOT at risk (checked today)

- 99 `agha_reza`: adjust-charge 50,000 (08-01) then auto-settle "old plan billed 100,000"
  (08-24). Reconstruction is ambiguous (two external +10 GB increases detected meanwhile);
  run the ledger replay for this account before deciding anything. Flagged for review, no
  assertion made.
- 46 `Seyed_brother2`: adjust-charge 175,000 (07-28) but the 08-12 settle was only 1,750 —
  a reset cleared the baseline in between; no visible double.
- 17 `Azno`, 96 `Mokaramat_YAZDi_new`, 41 `DehqanTez`: switched to PAYG after the adjust, so
  the prepay pending path no longer applies; no double possible today.
