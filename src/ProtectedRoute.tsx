import type { ReactNode } from "react";
import { useAuth } from "./AuthContext";
import LoginPage from "./LoginPage";

export default function ProtectedRoute({ children }: { children: ReactNode }) {
  const auth = useAuth();
  if (auth.loading) {
    return <div className="auth-loading" role="status">Checking secure session…</div>;
  }
  if (!auth.user) {
    return (
      <LoginPage
        onLogin={auth.login}
        onSetupAdmin={auth.setupAdmin}
        initialSetupRequired={!auth.adminConfigured}
      />
    );
  }
  return children;
}