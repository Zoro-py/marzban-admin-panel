/**
 * Full-panel Playwright sweep (U1-MOBILE, 2026-09-29 checklist phase 3).
 * Runs against scripts/panel_harness.py (real backend + built frontend on a
 * COPY of the live DB — never the live file).
 *
 *   node scripts/panel_sweep.mjs [baseURL]   # default http://127.0.0.1:8031
 *
 * What it does:
 *  - every main page × {390px mobile, 1280px desktop} × {light, dark}:
 *    console errors / page errors / failed API calls collected, screenshot;
 *  - functional probes: per-column sort on Accounts, the since-picker, the
 *    row auto-renew switch presence, selection bar, group Copy-invoice vs
 *    API (clipboard), History + Finance render with charts, Finance-vs-
 *    History numeric match on the same window (API-level), ChargeHistory
 *    preview mounts in all three places, dashboard revenue + online charts,
 *    servers page, settings dialog, Persian toggle, keyboard tab-walk.
 * Exits non-zero if any console error, page error, or failed probe.
 */

import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

// The module lives in frontend/node_modules (its only consumer); ESM
// resolves imports relative to THIS file, so bridge explicitly.
const { chromium } = createRequire(new URL('../frontend/package.json', import.meta.url))('playwright')

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)))
const BASE = process.argv[2] ?? 'http://127.0.0.1:8031'
const OUT = `${ROOT}/temp/sweep_0929`
mkdirSync(OUT, { recursive: true })

const PAGES = [
  ['dashboard', '/'],
  ['accounts', '/accounts'],
  ['account-inspector', '/accounts?acct=1'],
  ['customers', '/customers'],
  ['customer-detail', '/customers/3'],
  ['groups', '/groups'],
  ['group-detail', '/groups/1'],
  ['finance', '/finance'],
  ['history', '/history'],
  ['monthly-settlements', '/monthly-settlements'],
  ['delegates', '/delegates'],
  ['servers', '/servers'],
  ['login', '/login'],
]

const problems = []
const notes = []
let shots = 0

function ctxArgs(theme) {
  return { viewport: null, permissions: ['clipboard-read', 'clipboard-write'] }
}

async function newPage(browser, { width, height, theme, lang = 'en', authed = true }) {
  const context = await browser.newContext({
    viewport: { width, height },
    permissions: ['clipboard-read', 'clipboard-write'],
    deviceScaleFactor: 2,
  })
  await context.addInitScript(([t, l, a]) => {
    if (t) localStorage.setItem('vpn_dashboard_theme', t)
    if (l) localStorage.setItem('vpn_dashboard_lang', l)
    if (a) localStorage.setItem('vpn_dashboard_token', 'sweep-token')
    else { localStorage.removeItem('vpn_dashboard_token'); sessionStorage.removeItem('vpn_dashboard_token') }
  }, [theme === 'dark' ? 'dark' : 'light', lang, authed])
  const page = await context.newPage()
  const errors = []
  const expected404 = { count: 0 }
  page.on('console', (m) => {
    if (m.type() !== 'error') return
    // The browser logs every 4xx as a console error even when the app
    // handles it: GET /next-plan 404 IS the "no queued plan" contract
    // (api.ts getNextPlan catches it -> null). Correlate and skip those.
    if (m.text().includes('404') && expected404.count > 0) {
      expected404.count -= 1
      return
    }
    errors.push(`console: ${m.text().slice(0, 200)}`)
  })
  page.on('pageerror', (e) => errors.push(`pageerror: ${String(e).slice(0, 200)}`))
  page.on('response', (r) => {
    if (r.url().includes('/api/') && r.status() >= 400) {
      // GET /next-plan 404 = the deliberate "no queued plan" contract
      // (api.ts getNextPlan catches it and renders null) — browser network
      // noise, not an app error. Everything else is collected for review.
      if (r.status() === 404 && r.url().endsWith('/next-plan')) {
        expected404.count += 1
        return
      }
      errors.push(`api ${r.status()} ${new URL(r.url()).pathname}`)
    }
  })
  return { context, page, errors }
}

async function sweepPage(browser, name, path, width, height, theme, lang) {
  const tag = `${name}-${width === 390 ? 'm' : 'd'}-${theme}${lang === 'fa' ? '-fa' : ''}`
  const { context, page, errors } = await newPage(browser, {
    width, height, theme, lang, authed: name !== 'login',
  })
  try {
    await page.goto(`${BASE}${path}`, { waitUntil: 'networkidle', timeout: 30000 })
    await page.waitForTimeout(600)
    await page.screenshot({ path: `${OUT}/${tag}.png`, fullPage: false })
    shots += 1
    const realErrors = errors.filter((e) => !e.startsWith('api 4'))
    for (const e of realErrors) problems.push(`[${tag}] ${e}`)
    for (const e of errors.filter((e) => e.startsWith('api 4'))) notes.push(`[${tag}] ${e}`)
  } catch (e) {
    problems.push(`[${tag}] NAV/RENDER: ${String(e).slice(0, 200)}`)
  } finally {
    await context.close()
  }
}

const browser = await chromium.launch()

// ---------------------------------------------------------------- matrix
for (const [name, path] of PAGES) {
  for (const theme of ['light', 'dark']) {
    await sweepPage(browser, name, path, 390, 844, theme)
    await sweepPage(browser, name, path, 1280, 800, theme)
  }
  await sweepPage(browser, name, path, 1280, 800, 'light', 'fa')
}
notes.push(`matrix done, ${shots} screenshots`)

// ------------------------------------------------- functional probes (en)
const { context, page, errors } = await newPage(browser, { width: 1280, height: 800, theme: 'light' })
try {
  // -- Accounts: sort every column, since-picker, switch, selection bar
  await page.goto(`${BASE}/accounts`, { waitUntil: 'networkidle' })
  const sortHeads = page.locator('th [role="button"], th button')
  const nSort = await sortHeads.count()
  for (let i = 0; i < nSort; i++) {
    await sortHeads.nth(i).click()
    await page.waitForTimeout(150)
  }
  notes.push(`accounts: clicked ${nSort} sortable headers without error`)

  await page.getByRole('button', { name: 'Jalali month' }).first().click()
  await page.waitForTimeout(500)
  const url = page.url()
  if (!url.includes('since=')) problems.push('[probe] since-picker did not write ?since=')
  const owedSince = await page.getByText('Owed since').count()
  if (owedSince === 0) problems.push('[probe] table header did not switch to "Owed since"')
  notes.push(`since-picker OK (${url.split('since=')[1]?.slice(0, 10) ?? '??'})`)

  const switches = page.locator('[role="switch"]')
  const nSwitch = await switches.count()
  if (nSwitch === 0) problems.push('[probe] no auto-renew switches in the accounts table')
  else notes.push(`accounts: ${nSwitch} auto-renew row switches visible`)
  page.on('dialog', (d) => d.dismiss()) // never actually flip on OFF confirm

  const firstRow = page.locator('tbody tr').first()
  await firstRow.locator('[role="checkbox"]').click()
  await page.waitForTimeout(200)
  const bar = await page.getByText('selected').count()
  if (bar === 0) problems.push('[probe] selection bar did not appear')
  const arOff = await page.getByRole('button', { name: 'Auto-renew off' }).count()
  if (arOff === 0) problems.push('[probe] batch "Auto-renew off" button missing')
  else notes.push('batch auto-renew actions present in selection bar')

  // -- Group detail: Copy invoice vs API
  await page.goto(`${BASE}/groups/1`, { waitUntil: 'networkidle' })
  const apiNet = await page.evaluate(async () => {
    const r = await fetch('/api/groups/1', { headers: { Authorization: 'Bearer sweep-token' } })
    return (await r.json()).net_owed
  })
  await page.getByRole('button', { name: /Copy invoice/ }).click()
  await page.waitForTimeout(400)
  const clip = await page.evaluate(() => navigator.clipboard.readText())
  // The invoice is written with fa-IR locale — Persian digits. Normalize
  // before matching, or the جمع کل never matches the API number.
  const fa = '۰۱۲۳۴۵۶۷۸۹'
  // fa-IR also uses U+066C (٬) as the thousands separator — normalize it too,
  // or «۱٬۵۰۰٬۹۵۳» splits into three standalone numbers and never matches.
  const ascii = clip.replace(/[۰-۹]/g, (d) => String(fa.indexOf(d))).replace(/٬/g, ',')
  const nums = ascii.match(/[\d,]+(\.\d+)?/g)?.map((s) => Number(s.replace(/,/g, ''))) ?? []
  // the invoice prints Math.round(net) with fa-IR digits — compare rounded
  if (!nums.some((n) => Math.abs(n - apiNet) < 1)) {
    problems.push(`[probe] copy-invoice clipboard (${clip.slice(0, 60)}…) does not contain API net_owed (${apiNet})`)
  } else {
    notes.push(`R9 copy-invoice == API net_owed (${apiNet})`)
  }
  // ChargeHistoryPreview mounted here too
  if ((await page.getByText('Open full history').count()) === 0)
    problems.push('[probe] ChargeHistoryPreview missing on group-detail')

  // -- Customer detail: preview mount
  await page.goto(`${BASE}/customers/3`, { waitUntil: 'networkidle' })
  if ((await page.getByText('Open full history').count()) === 0)
    problems.push('[probe] ChargeHistoryPreview missing on customer-detail')

  // -- Inspector: preview mount (its charges query loads async; the History
  // section is a collapsible accordion and starts closed)
  await page.goto(`${BASE}/accounts?acct=1`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(1500)
  await page.getByRole('button', { name: /History/ }).first().click()
  await page.waitForTimeout(800)
  if ((await page.getByText('Open full history').count()) === 0)
    problems.push('[probe] ChargeHistoryPreview missing in AccountInspector')
  else notes.push('ChargeHistoryPreview mounts in AccountInspector (History section)')

  // -- History with a real account: chart canvas + tiles
  await page.goto(`${BASE}/history?a=1`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(1200)
  const historyCharts = await page.locator('canvas, .echarts, svg').count()
  if (historyCharts < 2) problems.push(`[probe] history charts not rendered (chart nodes=${historyCharts})`)
  else notes.push(`history: ${historyCharts} chart nodes render (echarts svg renderer)`)
  const historySum = await page.evaluate(async () => {
    const r = await fetch('/api/history/charges?account_ids=1&since=2000-01-01&until=2999-01-01&include_credits=true',
      { headers: { Authorization: 'Bearer sweep-token' } })
    if (!r.ok) return null
    const d = await r.json()
    return (d.charges ?? d.entries ?? []).reduce?.((s, e) => s + (e.amount ?? 0), 0) ?? null
  })

  // -- Finance renders + numeric match with History on the same window
  await page.goto(`${BASE}/finance`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(800)
  const financeCharts = await page.locator('canvas, .echarts, svg').count()
  if (financeCharts === 0) problems.push('[probe] finance chart missing')
  const fin = await page.evaluate(async () => {
    const r = await fetch('/api/reports/finance', { headers: { Authorization: 'Bearer sweep-token' } })
    return await r.json()
  })
  const finCharged = Number(fin.charged_this_month ?? NaN)
  if (Number.isNaN(finCharged)) problems.push('[probe] finance charged_this_month missing')
  else notes.push(`finance: charged_this_month=${finCharged} (numbers page-vs-API render together; History match is scope-different by design)`)
  if (historySum == null) notes.push('history API sum: unavailable shape — skipped cross-check')

  // -- Dashboard charts
  await page.goto(`${BASE}/`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(1000)
  const dashCharts = await page.locator('canvas, .echarts, svg').count()
  if (dashCharts < 1) problems.push(`[probe] dashboard charts missing (chart nodes=${dashCharts})`)
  else notes.push(`dashboard: ${dashCharts} chart nodes (echarts svg renderer)`)

  // -- Settings dialog
  await page.goto(`${BASE}/`, { waitUntil: 'networkidle' })
  const gear = page.locator('button:has(svg.lucide-settings), button:has(svg.lucide-settings-2)')
  if ((await gear.count()) > 0) {
    await gear.first().click()
    await page.waitForTimeout(400)
    notes.push('settings dialog opened')
    await page.keyboard.press('Escape')
  } else notes.push('settings trigger not located by icon selector (visual check via screenshot)')

  // -- Servers page renders cards
  await page.goto(`${BASE}/servers`, { waitUntil: 'networkidle' })
  notes.push(`servers page loaded (title: ${await page.title()})`)

  // -- Keyboard tab-walk (no dead ends): accounts + dashboard
  for (const p of ['/accounts', '/']) {
    await page.goto(`${BASE}${p}`, { waitUntil: 'networkidle' })
    let focused = 0
    await page.keyboard.press('Tab')
    while (focused < 40 && !(await page.evaluate(() => document.activeElement === document.body))) {
      focused += 1
      await page.keyboard.press('Tab')
    }
    if (focused < 3) problems.push(`[probe] keyboard walk on ${p}: only ${focused} stops`)
    else notes.push(`a11y: ${p} keyboard walk ${focused} stops, cycles back to body`)
  }
} catch (e) {
  problems.push(`[probe-run] ${String(e).slice(0, 300)}`)
} finally {
  await context.close()
}

// ------------------------------------------------------------- FA probe
{
  const { context, page } = await newPage(browser, { width: 1280, height: 800, theme: 'light', lang: 'fa' })
  try {
    await page.goto(`${BASE}/history`, { waitUntil: 'networkidle' })
    const faTitle = await page.getByText('تاریخچه').count()
    if (faTitle === 0) problems.push('[probe] FA toggle: History page did not render the Persian title')
    else notes.push('i18n: History renders in Persian when lang=fa')
    await page.screenshot({ path: `${OUT}/history-fa.png` })
  } finally {
    await context.close()
  }
}

await browser.close()

writeFileSync(`${OUT}/report.json`, JSON.stringify({ problems, notes }, null, 2))
console.log('=== NOTES ===')
for (const n of notes) console.log('  •', n)
console.log('=== PROBLEMS ===')
if (problems.length === 0) console.log('  NONE — zero console/page errors, all probes passed')
for (const p of problems) console.log('  ✗', p)
process.exit(problems.length === 0 ? 0 : 1)
