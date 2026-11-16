import { useEffect, useState } from "react";
import { KeyRound, Plus, RefreshCw, ShieldCheck, UserRoundPlus, UserRoundX } from "lucide-react";
import { createEmployee, fetchUsers, resetUserPassword, updateUser } from "./authService";
import type { ManagedUser } from "./authService";
import { useAuth } from "./AuthContext";

export default function AdminUsersPage() {
  const { user: currentUser } = useAuth();
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [oneTimePassword, setOneTimePassword] = useState<{ name: string; password: string } | null>(null);

  const refresh = async () => {
    setLoading(true);
    try {
      setUsers(await fetchUsers());
      setError("");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Could not load users.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void refresh(); }, []);

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSaving(true);
    setError("");
    try {
      await createEmployee(name, password, email);
      setName("");
      setEmail("");
      setPassword("");
      await refresh();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Could not create employee.");
    } finally {
      setSaving(false);
    }
  };

  const toggleActive = async (managedUser: ManagedUser) => {
    try {
      const updated = await updateUser(managedUser.id, { is_active: !managedUser.is_active });
      setUsers((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Could not update account.");
    }
  };

  const resetPassword = async (managedUser: ManagedUser) => {
    setError("");
    try {
      const result = await resetUserPassword(managedUser.id);
      setUsers((current) => current.map((item) => item.id === result.user.id ? result.user : item));
      setOneTimePassword({ name: result.user.name, password: result.temporary_password });
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Could not reset password.");
    }
  };

  return (
    <div className="admin-page">
      <div className="page-heading">
        <div><div className="eyebrow">ACCESS CONTROL & ALERT RECIPIENTS</div><h1>User Management</h1><p>Create employee accounts, assign notification email IDs, and manage access to the shared portfolio.</p></div>
        <button className="button secondary" onClick={() => void refresh()} disabled={loading}><RefreshCw size={14} /> Refresh</button>
      </div>

      <section className="card admin-create-card">
        <div className="section-head"><div><h2>Add Employee</h2><p>The employee&apos;s email ID is automatically added to price target alert notifications.</p></div><UserRoundPlus size={17} className="muted-icon" /></div>
        <form className="admin-create-form" onSubmit={(event) => void submit(event)}>
          <label>
            <span>Employee name</span>
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="e.g. Employee 1"
              maxLength={100}
              required
            />
          </label>
          <label>
            <span>Email ID (Alert Notifications)</span>
            <input
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="employee@plutuswealth.com"
              maxLength={320}
              required
            />
            <small>Receives Base &amp; Bull target alert emails.</small>
          </label>
          <label>
            <span>Initial password</span>
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              minLength={12}
              autoComplete="new-password"
              placeholder="12+ characters"
              required
            />
            <small>12+ characters; use at least three character types.</small>
          </label>
          <button className="button primary" type="submit" disabled={saving}><Plus size={14} /> {saving ? "Creating…" : "Create Employee"}</button>
        </form>
        {error && <div className="inline-error" role="alert">{error}</div>}
      </section>

      <section className="card admin-users-card">
        <div className="section-head"><div><h2>Registered Users</h2><p>{users.length} accounts · active user emails automatically receive alert notifications</p></div><ShieldCheck size={17} className="muted-icon" /></div>
        {loading ? <div className="empty-state"><RefreshCw size={18} /><b>Loading users…</b></div> : (
          <div className="table-scroll">
            <table className="portfolio-table admin-users-table">
              <thead><tr><th>Name</th><th>Email ID (Notifications)</th><th>Role</th><th>Status</th><th>Created</th><th>Last Login</th><th>Actions</th></tr></thead>
              <tbody>{users.map((managedUser) => (
                <tr key={managedUser.id}>
                  <td><b>{managedUser.name}</b></td>
                  <td>{managedUser.email || <span className="cell-sub">Uses Settings Email</span>}</td>
                  <td>{managedUser.role === "ADMIN" ? "Administrator" : "Employee"}</td>
                  <td><span className={`status-pill ${managedUser.is_active ? "buy" : "neutral"}`}>{managedUser.is_active ? "Active" : "Inactive"}</span></td>
                  <td>{new Date(managedUser.created_at).toLocaleDateString("en-IN")}</td>
                  <td>{managedUser.last_login_at ? new Date(managedUser.last_login_at).toLocaleString("en-IN") : "Never"}</td>
                  <td><div className="row-actions">
                    {managedUser.role === "EMPLOYEE" && <>
                      <button className="button secondary small" onClick={() => void resetPassword(managedUser)}><KeyRound size={13} /> Reset Password</button>
                      <button className="button secondary small" onClick={() => void toggleActive(managedUser)}>
                        {managedUser.is_active ? <UserRoundX size={13} /> : <UserRoundPlus size={13} />}
                        {managedUser.is_active ? "Deactivate" : "Activate"}
                      </button>
                    </>}
                    {managedUser.id === currentUser?.id && <small className="cell-sub">Current account</small>}
                  </div></td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </section>

      {oneTimePassword && (
        <div className="password-dialog-backdrop" role="presentation" onClick={() => setOneTimePassword(null)}>
          <section className="password-dialog one-time-password" role="dialog" aria-modal="true" onClick={(event) => event.stopPropagation()}>
            <div className="eyebrow">ONE-TIME CREDENTIAL</div>
            <h2>Password reset for {oneTimePassword.name}</h2>
            <p>This password is shown once. Share it with the employee through a separate secure channel.</p>
            <code>{oneTimePassword.password}</code>
            <button className="button primary" onClick={() => setOneTimePassword(null)}>Done</button>
          </section>
        </div>
      )}
    </div>
  );
}