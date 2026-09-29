import * as React from 'react'

/** Minimal bilingual layer (2026-09-29 checklist row «زبان دوزبانه»): the
 * panel is English-only today while the operator works in Persian, so the
 * two highest-traffic money surfaces — Balance-since and History — got a
 * FA/EN toggle with localStorage memory. Deliberately NOT react-i18next:
 * one hook, a two-argument tr() helper, and no provider wrapping the tree —
 * a full framework is disproportionate for two surfaces and would invite
 * half-translated screens everywhere else.
 *
 * Default is 'en' (the panel's current language) so nothing changes until
 * the operator explicitly switches. */

export type Lang = 'fa' | 'en'

const LANG_KEY = 'vpn_dashboard_lang'

function readStored(): Lang {
  try {
    const v = window.localStorage.getItem(LANG_KEY)
    if (v === 'fa' || v === 'en') return v
  } catch {
    // Private mode / quota — default below.
  }
  return 'en'
}

let current: Lang = readStored()
const listeners = new Set<() => void>()

export function setLang(next: Lang): void {
  current = next
  try {
    window.localStorage.setItem(LANG_KEY, next)
  } catch {
    // The in-session language still switches; only the memory is lost.
  }
  listeners.forEach((fn) => fn())
}

/** Subscribes the component to language changes; [lang, setLang]. */
export function useLang(): [Lang, (next: Lang) => void] {
  const lang = React.useSyncExternalStore(
    (cb) => {
      listeners.add(cb)
      return () => listeners.delete(cb)
    },
    () => current,
    () => current,
  )
  return [lang, setLang]
}

/** Picks the string for the active language — fa first at the call site so
 * the Persian wording is the reference and English mirrors it. */
export function tr(lang: Lang, fa: string, en: string): string {
  return lang === 'fa' ? fa : en
}
