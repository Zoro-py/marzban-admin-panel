import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider, MutationCache, QueryCache } from '@tanstack/react-query'
import { Toaster, toast } from 'sonner'
import './index.css'
import App from './App.tsx'
import { AuthProvider } from './lib/auth.tsx'
import { ThemeProvider } from './lib/theme.tsx'
import { TooltipProvider } from './components/ui/tooltip.tsx'
import { apiErrorMessage } from './lib/api'

// Every error toast is keyed by its own message. Sonner treats a repeat id as
// an UPDATE to the existing toast rather than a second one, which is what stops
// the same failure being reported twice: most mutations in this app declare
// their own `onError: toast.error(...)` AND fall through to the cache-level
// handler below, so an error used to stack two identical toasts on top of each
// other. Different messages still get their own toast — this collapses
// duplicates, it doesn't hide a second, different problem.
const toastError = (error: unknown) => {
  const message = apiErrorMessage(error)
  toast.error(message, { id: `err:${message}` })
}

const queryClient = new QueryClient({
  mutationCache: new MutationCache({
    onError: toastError,
  }),
  queryCache: new QueryCache({
    onError: (error, query) => {
      // A query that renders its own error inline (e.g. the bulk-account name
      // preview, which validates as you type) sets meta.silentError. Toasting
      // on top of an inline message says the same thing twice, and for a
      // debounced live preview it would fire on every pause in typing.
      if (query.meta?.silentError) return
      toastError(error)
    },
  }),
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 5 * 60 * 1000,
    },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <TooltipProvider delayDuration={200}>
          <AuthProvider>
            <BrowserRouter>
              <App />
              <Toaster richColors position="top-right" />
            </BrowserRouter>
          </AuthProvider>
        </TooltipProvider>
      </ThemeProvider>
    </QueryClientProvider>
  </StrictMode>,
)
