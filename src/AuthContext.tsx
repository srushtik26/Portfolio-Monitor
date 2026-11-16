import { createContext, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";
import * as authService from "./authService";
import type { AuthUser } from "./authService";

interface AuthContextValue {
  user: AuthUser | null;
  loading: boolean;
  adminConfigured: boolean;
  login: (name: string, password: string) => Promise<void>;
  setupAdmin: (name: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  changePassword: (currentPassword: string, newPassword: string) => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [adminConfigured, setAdminConfigured] = useState(true);

  useEffect(() => {
    let active = true;
    Promise.all([
      authService.fetchCurrentUser().catch(() => null),
      authService.fetchSetupStatus().catch(() => ({ admin_configured: true })),
    ])
      .then(([session, setup]) => {
        if (!active) return;
        setUser(session?.user ?? null);
        setAdminConfigured(setup.admin_configured);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, []);

  const value: AuthContextValue = {
    user,
    loading,
    adminConfigured,
    async login(name, password) {
      const session = await authService.login(name, password);
      setUser(session.user);
      setAdminConfigured(true);
    },
    async setupAdmin(name, password) {
      const session = await authService.setupInitialAdmin(name, password);
      setUser(session.user);
      setAdminConfigured(true);
    },
    async logout() {
      await authService.logout();
      setUser(null);
    },
    async changePassword(currentPassword, newPassword) {
      const session = await authService.changePassword(currentPassword, newPassword);
      setUser(session.user);
    },
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used within AuthProvider.");
  return context;
}