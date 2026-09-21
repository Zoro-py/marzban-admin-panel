/* Jalali (Shamsi) date helpers — display-only conversion between a JS Date's
 * Gregorian calendar and the Jalali calendar, with no new runtime dependency
 * (react-multi-date-picker is only used for PICKING; formatting/deriving at
 * render time goes through the plain integer algorithm below, which is the
 * well-known jalaali-js math, MIT, valid for Jalali years 1178–1634 /
 * Gregorian 1799–2256 — far wider than any date this panel shows).
 *
 * Latin digits on purpose: the panel's UI language is English and Persian
 * digits inside mono/tabular columns break alignment. */

export interface JalaliYmd {
  jy: number
  jm: number
  jd: number
}

function div(a: number, b: number): number {
  return Math.trunc(a / b)
}

function mod(a: number, b: number): number {
  return a - b * Math.floor(a / b)
}

function jalCal(jy: number): { leap: number; gy: number; march: number } {
  const breaks = [
    -61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210, 1635, 2060, 2097, 2192, 2262, 2324, 2394, 2456, 3178,
  ]
  const gy = jy + 621
  let leapJ = -14
  let jp = breaks[0]!
  let jump = 0
  for (let i = 1; i < breaks.length; i++) {
    const jm = breaks[i]!
    jump = jm - jp
    if (jy < jm) break
    leapJ += div(jump, 33) * 8 + div(mod(jump, 33), 4)
    jp = jm
  }
  let n = jy - jp
  leapJ += div(n, 33) * 8 + div(mod(n, 33) + 3, 4)
  if (mod(jump, 33) === 4 && jump - n === 4) leapJ += 1
  const leapG = div(gy, 4) - div((div(gy, 100) + 1) * 3, 4) - 150
  const march = 20 + leapJ - leapG
  if (jump - n < 6) n = n - jump + div(jump + 4, 33) * 33
  let leap = mod(mod(n + 1, 33) - 1, 4)
  if (leap === -1) leap = 4
  return { leap, gy, march }
}

function g2d(gy: number, gm: number, gd: number): number {
  let d =
    div((gy + div(gm - 8, 6) + 100100) * 1461, 4) + div(153 * mod(gm + 9, 12) + 2, 5) + gd - 34840408
  d = d - div(div(gy + 100100 + div(gm - 8, 6), 100) * 3, 4) + 752
  return d
}

function d2g(jdn: number): { gy: number; gm: number; gd: number } {
  let j = 4 * jdn + 139361631
  j = j + div(div(4 * jdn + 183187720, 146097) * 3, 4) * 4 - 3908
  const i = div(mod(j, 1461), 4) * 5 + 308
  const gd = div(mod(i, 153), 5) + 1
  const gm = mod(div(i, 153), 12) + 1
  const gy = div(j, 1461) - 100100 + div(8 - gm, 6)
  return { gy, gm, gd }
}

function d2j(jdn: number): JalaliYmd {
  const { gy } = d2g(jdn)
  let jy = gy - 621
  const r = jalCal(jy)
  const jdn1f = g2d(gy, 3, r.march)
  let k = jdn - jdn1f
  if (k >= 0) {
    if (k <= 185) {
      // The first six Jalali months have 31 days.
      return { jy, jm: 1 + div(k, 31), jd: mod(k, 31) + 1 }
    }
    k -= 186
  } else {
    jy -= 1
    k += 179
    if (r.leap === 1) k += 1
  }
  return { jy, jm: 7 + div(k, 30), jd: mod(k, 30) + 1 }
}

function j2d(jy: number, jm: number, jd: number): number {
  const r = jalCal(jy)
  return g2d(r.gy, 3, r.march) + (jm - 1) * 31 - div(jm, 7) * (jm - 7) + jd - 1
}

/** Gregorian Y/M/D (in the browser's local calendar fields of `date`) → Jalali. */
export function toJalali(date: Date): JalaliYmd {
  return d2j(g2d(date.getFullYear(), date.getMonth() + 1, date.getDate()))
}

/** "1405/06/30" — Latin digits, zero-padded, the display form used next to
 * Gregorian dates everywhere in the history view. */
export function formatJalali(date: Date): string {
  const { jy, jm, jd } = toJalali(date)
  return `${jy}/${String(jm).padStart(2, '0')}/${String(jd).padStart(2, '0')}`
}

/** Local-midnight Date of the 1st of the Jalali month `date` falls in — the
 * "from the start of the current Jalali month" range preset. */
export function jalaliMonthStartLocal(date: Date): Date {
  const { jy, jm } = toJalali(date)
  const { gy, gm, gd } = d2g(j2d(jy, jm, 1))
  const out = new Date(gy, gm - 1, gd)
  out.setHours(0, 0, 0, 0)
  return out
}

/** Local date of `date` as "YYYY-MM-DD" — local components, never
 * toISOString() (which would shift the day by the timezone offset). */
export function toLocalYmd(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}
