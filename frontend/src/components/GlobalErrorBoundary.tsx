import { Component, type ErrorInfo, type ReactNode } from 'react';
import { Button } from '@/components/ui/button';
import { isChunkLoadError } from '@/lib/chunkRecovery';

interface Props {
  children?: ReactNode;
}

interface State {
  hasError: boolean;
  error?: Error;
}

export class GlobalErrorBoundary extends Component<Props, State> {
  public state: State = {
    hasError: false
  };

  public static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  public componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error('Uncaught error:', error, errorInfo);
  }

  public render() {
    if (this.state.hasError) {
      // A stale tab asking for a chunk the last deploy replaced lands here
      // when the one automatic reload was already spent (or the reload itself
      // failed). Say what actually happened, in the operator's language, with
      // the one action that fixes it — not a generic English crash screen.
      if (isChunkLoadError(this.state.error)) {
        return (
          <div className="flex flex-col items-center justify-center min-h-[400px] h-screen p-6 text-center space-y-4" dir="rtl">
            <h2 className="text-2xl font-bold">نسخهٔ پنل به‌روز شده است</h2>
            <p className="text-sm text-muted-foreground max-w-md">
              این صفحه با نسخهٔ قدیمی باز شده بود و بخشی از آن پیدا نشد.
              <br />
              The panel was updated since this page was opened. Load the fresh version:
            </p>
            <Button onClick={() => window.location.reload()}>
              بارگذاری نسخهٔ تازه / Reload
            </Button>
          </div>
        );
      }
      return (
        <div className="flex flex-col items-center justify-center min-h-[400px] h-screen p-6 text-center space-y-4">
          <h2 className="text-2xl font-bold text-destructive">Application Error</h2>
          <p className="text-sm text-muted-foreground max-w-md">
            {this.state.error?.message || 'An unexpected error occurred in the application.'}
          </p>
          <Button onClick={() => window.location.reload()} variant="outline">
            Reload Application
          </Button>
        </div>
      );
    }

    return this.props.children;
  }
}
