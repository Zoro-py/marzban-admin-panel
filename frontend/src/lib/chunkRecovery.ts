import { lazy, type ComponentType } from 'react'

// A deploy replaces every hashed file under /assets/ in one swap. A tab that
// was loaded before the deploy — a phone's browser tab above all, which stays
// open for days — still runs against the OLD chunk graph, and its next lazy
// route import 404s ("Failed to fetch dynamically imported module"). The
// server already serves index.html with no-cache, so any fresh navigation
// heals itself; the one case code can still save is the already-stale tab:
// reload once into the fresh build instead of stranding the operator on a
// blank screen or an English "Application Error". The sessionStorage stamp
// makes that single reload loop-safe: if the reload itself cannot load the
// app (server down, stale proxy), the second attempt stops and the error
// boundary speaks instead.

const CHUNK_RELOAD_KEY = 'chunk_reload_at'
const CHUNK_RELOAD_WINDOW_MS = 15_000

export type PageModule = { default: ComponentType }

export function isChunkLoadError(err: unknown): boolean {
  const msg = err instanceof Error ? err.message : String(err ?? '')
  // Chrome / Edge / Firefox / Safari-iOS phrasings of the same failure.
  return /failed to fetch dynamically imported|importing a module script failed|error loading dynamically imported|loading chunk/i.test(
    msg,
  )
}

export function recoverableLazy(factory: () => Promise<PageModule>) {
  return lazy(() =>
    factory().catch((err: unknown) => {
      if (isChunkLoadError(err)) {
        const last = Number(sessionStorage.getItem(CHUNK_RELOAD_KEY) || 0)
        if (Date.now() - last > CHUNK_RELOAD_WINDOW_MS) {
          sessionStorage.setItem(CHUNK_RELOAD_KEY, String(Date.now()))
          window.location.reload()
        }
      }
      throw err instanceof Error ? err : new Error(String(err))
    }),
  )
}
