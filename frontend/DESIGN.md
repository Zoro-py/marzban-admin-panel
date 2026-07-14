# Design system — "Console"

The visual language for the whole dashboard. Tokens live in `src/index.css`
(`@theme` + `:root` / `.dark`); primitives in `src/components/ui/`; the
semantic components that enforce the rules below in `src/components/`
(`Money`, `StatusDot`, `UsageBar`, `StatCard`).

## Principles

1. **Neutrals carry the layout; color carries meaning.** Saturated color never
   decorates — if something is red, it is a problem; if it's amber, it needs
   attention soon.
2. **Money is text, not badges.** Amounts render as plain tabular figures,
   colored by meaning (see `<Money/>`). Badges are for state *words*.
3. **Dialogs are for creating and confirming.** Day-to-day per-account actions
   live in the inspector panel (`AccountInspector`), not modals.
4. **Density over air.** 13px UI base, 36px table rows, 32px controls. This is
   an ops tool read at arm's length, dozens of times a day.
5. **Depth = layering.** Flat cards with hairline borders in-flow; shadows only
   on true overlays (popover, dialog, inspector).

## Color semantics (used identically everywhere)

| Token | Light / dark base | Means |
|---|---|---|
| `destructive` | red | debt (they owe you), expired, exhausted quota |
| `warning` | amber | expiring ≤3d, ≥80% quota, pending/unsettled amounts, cycle due, "not set" rate |
| `success` | green | active/healthy, payments received, all-clear |
| `credit` | violet | credit balances — money owed *back* to a customer (a liability, so not green; not their debt, so not red) |
| `primary` | iris | interactive accent: buttons, focus, active nav, healthy usage fill |
| `muted-foreground` | gray | secondary text, disabled/unknown, zero balances |

Status dots (`StatusDot`): active=green · on_hold=amber · limited/expired=red ·
disabled/unknown=gray.

Usage meters (`UsageBar`): fill carries severity (primary → warning ≥80% →
destructive ≥100%); the track is a light step of the same hue.

Chart series (Finance) use the validated dataviz palette (blue `#2a78d6`/`#3987e5`
= collected, aqua `#1baf7a`/`#199e70` = charged), *not* the status colors — a
series is an identity, not a state.

## Type scale

| Use | Size |
|---|---|
| Page title | 18px semibold (`text-lg`) |
| Section/card title | 13px semibold |
| Body / table cells | 13px |
| Secondary/meta | 12px (`text-xs`) |
| Table headers, chips | 11px, headers uppercase +tracking |
| Identifiers (usernames) & column figures | JetBrains Mono, `tabular-nums` |
| Stat-tile values | 18px semibold, proportional figures (no `tabular-nums` on large standalone numbers) |

## Spacing & shape

- Page gutter 20px (`p-5`), max content width 1200px, page sections `gap-4`.
- Radius: 8px surfaces (`rounded-lg`), 6px controls (`rounded-md`).
- Controls: 32px default (`h-8`), 28px small; table rows ~36px (`py-2`).

## Dark mode

Class-driven (`.dark` on `<html>`, stamped pre-paint by `index.html` and
managed by `src/lib/theme.tsx`: light / dark / system). Never use
`prefers-color-scheme` directly in component styles — the toggle must win.

## Interaction patterns

- **Row click = open.** Tables navigate (list pages) or open the account
  inspector (`?acct=<id>` search param — works on any page, keeps context).
  Inner links `stopPropagation()`.
- **⌘K / Ctrl-K** opens the command palette: pages, accounts, customers,
  groups, sync, theme.
- Deep links to accounts use `?acct=<id>`; legacy `?highlight=` still works on
  /accounts (mapped on mount).
- Empty states say what "empty" means ("Nothing expired"), not just "no data".
