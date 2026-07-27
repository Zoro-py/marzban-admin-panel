# AGENTS.md — VPN Reseller Dashboard Agent Rulebook

This file governs every agent working in this repo (Antigravity, Claude Code, Cursor, or any API driver). Read it before touching a single file.

---

## 1. What this project is

A management layer on top of Marzban: tracks ownership, billing, and pay-as-you-go group settlement. Stack: FastAPI + SQLite backend, React/Vite frontend, Telegram bot. See `README.md` for architecture.

**Critical domain:** This panel moves real money. A billing bug can silently overcharge or forgive real debt with no error shown anywhere.

---

## 2. Repo map

| Path | What it is |
|---|---|
| `backend/app/` | FastAPI app — models, routers, services, sync job |
| `frontend/src/` | React/Vite dashboard |
| `bot/` | Telegram bot |
| `scripts/` | Install/bootstrap scripts |
| `docker-compose.yml` | Deployment |
| `.env.example` | Reference for required env vars |

---

## 3. Session start protocol

Before any code change:
1. Run `git status` — if there are uncommitted changes you didn't make, stop and ask.
2. Read the docstring/comment at the top of any function you plan to modify.
3. Identify every caller of any function or endpoint you plan to change.

---

## 4. Non-negotiable execution discipline (anti-regression rules)

These rules exist because a prior agent (Antigravity, 2026-07-27, commit 765d11c) shipped 8 bugs in a single "audit" commit that were found by Claude Code in review (commit d4d950a). The failures are documented in `scratch_antigravity_feedback.txt`. Every rule below maps directly to a real failure.

### 4.1 — Verify before reporting done

"I wrote the code" is not "I confirmed it works." Before marking any task complete:
- Trace the full call path end-to-end for any change that touches more than one file.
- Run `npm run build` (frontend) and `python -m py_compile` (backend) at minimum.
- For security features: confirm the enforcement actually runs on the triggering path — not just that the enforcement code exists somewhere in the file.

**Root failure:** Rate-limiting was added to `auth.py` but `marzban_client.py` raised `HTTPException` on bad credentials before the attempt counter ever ran. The feature was completely inert.

### 4.2 — Read the docstring before changing behavior

Before modifying any function, read its docstring and inline comments. If your change would make any statement in that docstring false, **stop**. Either update the docstring deliberately, or reconsider the change.

**Root failures:**
- `reset_group_cycle` had a docstring explicitly stating it never calls external APIs. It was changed to call Marzban's live reset API per member, with no docstring update.
- `AccountAdjustRequest` documented that deltas are relative and negative values are valid. Validation was added rejecting negative values, breaking a live UI button ("-7 days").

### 4.3 — Money logic gets full before/after behavioral diff

Any function that computes or moves money (`billable_bytes`, `settle_group`, `mark_paid`, balance calculations, ledger writes) must be diffed line-by-line before and after. Ask explicitly: **"Does this change what gets charged or credited?"** even if that was not the stated goal of the edit.

**Root failures:**
- `billable_bytes`' negative-diff guard was removed during a refactor, causing negative usage diffs to bill the customer's entire used_traffic.
- `settle_group`'s `mark_paid` logic was changed to also post a CHARGE to cancel a pre-existing CREDIT — silently erasing money a customer was legitimately owed.

### 4.4 — Any new env var must be wired into deployment

When you introduce a new environment variable, immediately grep the repo for it: check `docker-compose.yml`, `.env.example`, and any `.env.*` files. If the var is unset in the real deployment, ensure you either add it to `docker-compose.yml`/`.env.example` yourself, or explicitly document it as a required manual step.

**Root failure:** `CORS_ALLOWED_ORIGINS` was added but never appears in `docker-compose.yml` or `.env.example`. The fallback value contained a literal `"your-frontend-domain.com"`. Deploying this would have blocked every frontend request — total outage.

### 4.5 — External calls inside DB transactions require partial-failure analysis

If a loop mixes DB writes with external API calls (Marzban, etc.), ask: what happens if the API call fails on iteration `k`, after succeeding for iterations `1..k-1`? A DB rollback cannot undo already-sent API calls. Either handle per-item failures without rolling back the whole transaction, or accept and document the split-brain risk.

**Root failure:** `reset_group_cycle` wrapped a per-member Marzban API call loop in one try/except that rolled back the entire DB transaction on failure — but Marzban calls that already succeeded for earlier members remained applied. The DB and Marzban state were permanently inconsistent.

### 4.6 — When adding a default-changing parameter, check every caller

When you add a parameter with a default that changes existing behavior (e.g., pagination with `limit=100`), grep every frontend and backend caller of that endpoint/function. If callers don't pass the new parameter, confirm the new default is safe for them — or update the callers.

**Root failure:** `limit=100` pagination was added to three list endpoints. No frontend caller passes `offset` or `limit`. Every list screen and dropdown silently truncated to 100 rows with no error or indicator.

### 4.7 — Schema changes on an already-deployed DB require explicit migrations

`SQLModel`/`SQLAlchemy`'s `create_all()` only creates missing tables — it never alters existing ones. Adding `index=True` to a column on an already-deployed database is silently inert. Any index, column, or constraint change on an existing table requires a hand-written `ALTER TABLE` or `CREATE INDEX` statement, matching the pattern already used in `db.py`.

**Root failure:** `index=True` was added to `LedgerEntry.date`, `LedgerEntry.type`, and `AccountEvent.date` with no migration. The indexes exist in the model but never in the real database.

### 4.8 — Grep before creating a new component or utility

Before creating a new React component, hook, or utility function, search the codebase for existing ones with similar purpose. A new file should be a last resort.

**Root failure:** Both `GlobalErrorBoundary.tsx` and `layout/ErrorBoundary.tsx` were created (near-identical). Both `EmptyState.tsx` and `ui/empty-state.tsx` were created (near-identical). Only one of each pair is imported anywhere.

---

## 5. How to report completion

When reporting a task as done, state **exactly what was verified**, not just "done and pushed." Silence implies verification happened. Examples of acceptable completion statements:
- "Done. Traced the call path from `POST /api/auth/token` through `verify_admin_login` — the rate limiter runs before the Marzban call. Verified with `py_compile`. Frontend build clean."
- "Done. Diffed `billable_bytes` before/after — the negative-diff clamp is preserved. No change to what gets charged."

If you did not verify something, say so explicitly.

---

## 6. Blast radius grading

Before starting any change, grade it:

| Grade | Examples | Required verification |
|---|---|---|
| **HIGH** (money, auth, external API calls, DB migrations) | `settle_group`, `billable_bytes`, login flow, Marzban API wrappers, schema changes | Full call-path trace, before/after behavioral diff, migration script |
| **MEDIUM** (API contracts, pagination, new env vars) | New endpoint parameters, new config keys, CORS changes | Check all callers, grep deployment config |
| **LOW** (UI polish, docstrings, logging, new isolated components) | Skeleton loading, page titles, error boundary wrappers | `npm run build` / `py_compile` pass |

HIGH-grade changes must never be batched in the same commit with LOW-grade changes.

---

## 7. Language and output

Communicate in Persian. Technical terms, code blocks, JSON, and SQL stay in English.

---

## 8. Git discipline

- Work on a `feat/<name>` or `fix/<name>` branch.
- Do not auto-push without explicit permission.
- Commit messages follow conventional commits: `type(scope): explanation`.
- Do not leave work uncommitted at end of session.
