export type UserRole = "ADMIN" | "EMPLOYEE";

export interface AuthUser {
  id: number;
  name: string;
  email: string | null;
  role: UserRole;
  is_active: boolean;
  created_at: string;
  updated_at: string;
  last_login_at: string | null;
}

export interface ManagedUser extends AuthUser {}

export interface ActivityEntry {
  id: number;
  user_id: number | null;
  name: string | null;
  action: string;
  details: Record<string, unknown> | null;
  timestamp: string;
}

const configuredApiBase = (import.meta.env.VITE_API_BASE_URL ?? "").trim().replace(/\/+$/, "");
const apiBaseUrl = configuredApiBase || (import.meta.env.DEV ? "http://127.0.0.1:8000" : "");
let csrfToken = "";

function apiUrl(path: string) {
  return `${apiBaseUrl}/api${path}`;
}

async function errorFrom(response: Response): Promise<Error> {
  const body: unknown = await response.json().catch(() => null);
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return new Error(detail);
  }
  return new Error(`Request failed with status ${response.status}`);
}

export async function authFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const method = (init.method || "GET").toUpperCase();
  const isMutating = !new Set(["GET", "HEAD", "OPTIONS"]).has(method);
  if (isMutating && !csrfToken) {
    await fetchCurrentUser().catch(() => null);
  }
  if (isMutating && csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(input, { ...init, headers, credentials: "include" });
  if (response.status === 401) {
    const reqUrl = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    if (!reqUrl.includes("/api/auth/login") && !reqUrl.includes("/api/auth/setup-admin")) {
      csrfToken = "";
      if (typeof window !== "undefined") {
        window.dispatchEvent(new CustomEvent("portfolio:auth-expired"));
      }
    }
  }
  return response;
}

export async function fetchCurrentUser(): Promise<{ user: AuthUser; csrf_token: string } | null> {
  const response = await fetch(apiUrl("/auth/me"), { credentials: "include" });
  if (response.status === 401) {
    csrfToken = "";
    return null;
  }
  if (!response.ok) throw await errorFrom(response);
  const result = (await response.json()) as { user: AuthUser; csrf_token: string };
  csrfToken = result.csrf_token;
  return result;
}

export async function fetchSetupStatus(): Promise<{ admin_configured: boolean }> {
  const response = await fetch(apiUrl("/auth/setup-status"), { credentials: "include" });
  if (!response.ok) return { admin_configured: true };
  return (await response.json()) as { admin_configured: boolean };
}

export async function setupInitialAdmin(
  name: string,
  password: string,
): Promise<{ user: AuthUser; csrf_token: string }> {
  const response = await fetch(apiUrl("/auth/setup-admin"), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, password }),
  });
  if (!response.ok) throw await errorFrom(response);
  const result = (await response.json()) as { user: AuthUser; csrf_token: string };
  csrfToken = result.csrf_token;
  return result;
}

export async function login(name: string, password: string): Promise<{ user: AuthUser; csrf_token: string }> {
  const response = await fetch(apiUrl("/auth/login"), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, password }),
  });
  if (!response.ok) throw await errorFrom(response);
  const result = (await response.json()) as { user: AuthUser; csrf_token: string };
  csrfToken = result.csrf_token;
  return result;
}

export async function logout(): Promise<void> {
  const response = await authFetch(apiUrl("/auth/logout"), { method: "POST" });
  if (!response.ok) throw await errorFrom(response);
  csrfToken = "";
}

export async function changePassword(current_password: string, new_password: string) {
  const response = await authFetch(apiUrl("/auth/change-password"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ current_password, new_password }),
  });
  if (!response.ok) throw await errorFrom(response);
  const result = (await response.json()) as { user: AuthUser; csrf_token: string };
  csrfToken = result.csrf_token;
  return result;
}

async function readJson<T>(response: Response): Promise<T> {
  if (!response.ok) throw await errorFrom(response);
  return response.json() as Promise<T>;
}

export async function fetchUsers(): Promise<ManagedUser[]> {
  return readJson(await authFetch(apiUrl("/users")));
}

export async function createEmployee(
  name: string,
  password: string,
  email?: string,
): Promise<ManagedUser> {
  return readJson(
    await authFetch(apiUrl("/users"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, password, ...(email ? { email } : {}) }),
    }),
  );
}

export async function updateUser(
  userId: number,
  patch: { name?: string; email?: string | null; is_active?: boolean },
): Promise<ManagedUser> {
  return readJson(
    await authFetch(apiUrl(`/users/${userId}`), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  );
}

export async function resetUserPassword(userId: number): Promise<{ user: ManagedUser; temporary_password: string }> {
  return readJson(await authFetch(apiUrl(`/users/${userId}/reset-password`), { method: "POST" }));
}

export async function fetchActivity(): Promise<ActivityEntry[]> {
  return readJson(await authFetch(apiUrl("/activity?limit=300")));
}