import { Component, type ReactNode } from 'react';
import { AlertTriangle } from 'lucide-react';
export class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch() {
    /* Keep application content out of browser logs. */
  }
  render() {
    if (this.state.failed)
      return (
        <main className="fatal-error">
          <AlertTriangle size={32} />
          <h1>The workspace could not be displayed.</h1>
          <p>Your saved conversations remain on the server. Reload the page to reconnect.</p>
          <button className="button" onClick={() => window.location.reload()}>
            Reload workspace
          </button>
        </main>
      );
    return this.props.children;
  }
}
