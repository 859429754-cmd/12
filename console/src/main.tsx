import React, { Component, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { App, AppError } from "./App";
import "./styles.css";

class ErrorBoundary extends Component<{ children: ReactNode }, { error: string }> {
  state = { error: "" };

  static getDerivedStateFromError(error: unknown) {
    return { error: error instanceof Error ? error.message : String(error) };
  }

  render() {
    if (this.state.error) return <AppError error={this.state.error} />;
    return <React.Suspense fallback={null}>{this.props.children}</React.Suspense>;
  }
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
);
