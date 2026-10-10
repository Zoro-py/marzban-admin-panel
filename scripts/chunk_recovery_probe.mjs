/**
 * Chunk-stale recovery probe (2026-09-30 P0 "panel won't open on mobile").
 * Runs against scripts/panel_harness.py. Simulates a deploy stranding an open
 * tab: a lazy route chunk is aborted mid-session.
 *   A) chunk fails ONCE  -> one automatic reload -> route heals, no boundary.
 *   B) chunk fails ALWAYS-> exactly one auto-reload (loop-guard), then the
 *      Persian stale-version screen with a reload button (no white screen).
 */
import { createRequire } from 'node:module'
const { chromium } = createRequire(new URL('../frontend/package.json', import.meta.url))('playwright')

const BASE = process.argv[2] ?? 'http://127.0.0.1:8031'
let failures = 0
const check = (name, ok, extra = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${extra ? ' :: ' + extra : ''}`)
  if (!ok) failures++
}

const browser = await chromium.launch()

// --- Scenario A
{
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } })
  const page = await ctx.newPage()
  const pageErrors = []
  page.on('pageerror', e => pageErrors.push(String(e)))
  await page.addInitScript(() => localStorage.setItem('vpn_dashboard_token', 'sweep-token'))
  let loads = 0
  page.on('load', () => loads++)
  await page.goto(BASE + '/', { waitUntil: 'load' })
  await page.waitForTimeout(1000)
  check('A0 mounted flag set', await page.evaluate(() => window.__VPN_PANEL_MOUNTED__ === true))
  check('A0 boot-guard banner absent', (await page.locator('#vpn-boot-guard').count()) === 0)

  let abortedOnce = false
  await page.route('**/assets/FinancePage-*.js', route => {
    if (!abortedOnce) { abortedOnce = true; return route.abort() }
    return route.continue()
  })
  // At 390px the sidebar is a hamburger; navigate in-app instead of clicking.
  await page.evaluate(() => {
    window.history.pushState({}, '', '/finance')
    window.dispatchEvent(new PopStateEvent('popstate'))
  })
  await page.waitForTimeout(4500)
  check('A1 auto-reload happened (initial + reload = 2 loads)', loads >= 2, `loads=${loads}`)
  check('A1 landed on finance', page.url().includes('/finance'), page.url())
  check('A1 reload guard stamped', !!(await page.evaluate(() => sessionStorage.getItem('chunk_reload_at'))))
  const text = await page.evaluate(() => document.body.innerText)
  check('A1 finance actually rendered', /finance/i.test(text))
  check('A1 no error boundary', !/Application Error|نسخهٔ پنل به‌روز/.test(text))
  check('A1 zero pageerrors', pageErrors.length === 0, pageErrors.join(' | ').slice(0, 200))
  await ctx.close()
}

// --- Scenario B
{
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } })
  const page = await ctx.newPage()
  await page.addInitScript(() => localStorage.setItem('vpn_dashboard_token', 'sweep-token'))
  let loads = 0
  page.on('load', () => loads++)
  await page.route('**/assets/FinancePage-*.js', route => route.abort())
  await page.goto(BASE + '/finance', { waitUntil: 'load' })
  await page.waitForTimeout(5000)
  check('B1 exactly ONE auto-reload (initial + 1 = 2 loads, no loop)', loads === 2, `loads=${loads}`)
  const text = await page.evaluate(() => document.body.innerText)
  check('B1 Persian stale-version screen', text.includes('نسخهٔ پنل به‌روز شده است'), text.slice(0, 120).replace(/\n/g, ' '))
  check('B1 reload button present', (await page.getByRole('button', { name: /بارگذاری نسخهٔ تازه/ }).count()) >= 1)
  await ctx.close()
}

await browser.close()
console.log(failures === 0 ? 'ALL PROBES GREEN' : `FAILURES: ${failures}`)
process.exit(failures === 0 ? 0 : 1)
