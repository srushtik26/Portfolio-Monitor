import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";

interface ErrorBoundaryState {
  hasError: boolean;
  errorMessage: string;
}

class ProductionErrorBoundary extends React.Component<
  { children: React.ReactNode },
  ErrorBoundaryState
> {
  constructor(props: { children: React.ReactNode }) {
    super(props);
    this.state = { hasError: false, errorMessage: "" };
  }

  static getDerivedStateFromError(error: unknown): ErrorBoundaryState {
    return {
      hasError: true,
      errorMessage: error instanceof Error ? error.message : "Unexpected application error",
    };
  }

  override render() {
    if (this.state.hasError) {
      return (
        <div
          style={{
            minHeight: "100vh",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            background: "#080c14",
            color: "#f1f5f9",
            padding: 24,
            fontFamily: "Inter, system-ui, sans-serif",
          }}
        >
          <div
            style={{
              maxWidth: 460,
              padding: 28,
              borderRadius: 14,
              border: "1px solid #1e293b",
              background: "#0f172a",
              textAlign: "center",
            }}
          >
            <h2 style={{ margin: "0 0 8px", fontSize: "1.15rem" }}>
              Northstar Portfolio Monitor
            </h2>
            <p style={{ margin: "0 0 18px", color: "#94a3b8", fontSize: "0.86rem" }}>
              {this.state.errorMessage}
            </p>
            <button
              onClick={() => window.location.reload()}
              style={{
                padding: "9px 18px",
                borderRadius: 8,
                border: "none",
                background: "#3b82f6",
                color: "#ffffff",
                fontWeight: 600,
                cursor: "pointer",
              }}
            >
              Reload Dashboard
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ProductionErrorBoundary>
      <App />
    </ProductionErrorBoundary>
  </React.StrictMode>,
);

