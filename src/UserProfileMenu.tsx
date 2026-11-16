import { useState } from "react";
import { Check, ChevronDown, KeyRound, LogOut, UserRound, Users } from "lucide-react";
import { useAuth } from "./AuthContext";

export default function UserProfileMenu({
  onManageUsers,
}: {
  onManageUsers?: () => void;
}) {
  const { user, logout, changePassword } = useAuth();
  const [open, setOpen] = useState(false);
  const [passwordOpen, setPasswordOpen] = useState(false);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");

  if (!user) return null;
  const initials = user.name.split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toUpperCase();

  const submitPassword = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError("");
    if (newPassword !== confirmPassword) {
      setError("New passwords do not match.");
      return;
    }
    setSaving(true);
    try {
      await changePassword(currentPassword, newPassword);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setPasswordOpen(false);
      setNotice("Password updated. Other active sessions were signed out.");
      window.setTimeout(() => setNotice(""), 5000);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Could not change password.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="user-profile-wrap">
      <button
        className="profile-button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        aria-label="Open account menu"
      >
        <span className="avatar small-avatar">{initials}</span>
        <span className="profile-button-name">{user.name}</span>
        <ChevronDown size={14} className={`profile-chevron ${open ? "profile-chevron-open" : ""}`} />
      </button>
      {open && (
        <div className="user-profile-menu">
          <div className="user-menu-identity">
            <b>{user.name}</b>
            <small>{user.role === "ADMIN" ? "Administrator" : "Team Member"}</small>
          </div>
          <button onClick={() => { setPasswordOpen(false); setOpen(false); setNotice(`Signed in as ${user.name}`); }}>
            <UserRound size={15} /> My Account
          </button>
          {user.role === "ADMIN" && onManageUsers && (
            <button onClick={() => { setOpen(false); onManageUsers(); }}>
              <Users size={15} /> Manage Team Users
            </button>
          )}
          <button onClick={() => { setPasswordOpen(true); setOpen(false); setError(""); }}>
            <KeyRound size={15} /> Change Password
          </button>
          <button
            onClick={() => void logout().catch((requestError: unknown) => setError(
              requestError instanceof Error ? requestError.message : "Could not sign out.",
            ))}
          >
            <LogOut size={15} /> Log Out
          </button>
        </div>
      )}
      {notice && <div className="user-profile-notice" role="status">{notice}</div>}
      {passwordOpen && (
        <div className="password-dialog-backdrop" role="presentation" onClick={() => setPasswordOpen(false)}>
          <form className="password-dialog" onSubmit={(event) => void submitPassword(event)} onClick={(event) => event.stopPropagation()}>
            <div className="password-dialog-head">
              <div><small>ACCOUNT SECURITY</small><h2>Change Password</h2></div>
              <button type="button" className="icon-button" aria-label="Close" onClick={() => setPasswordOpen(false)}>×</button>
            </div>
            <label><span>Current password</span><input type="password" autoComplete="current-password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} required /></label>
            <label><span>New password</span><input type="password" autoComplete="new-password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} minLength={12} required /></label>
            <label><span>Confirm new password</span><input type="password" autoComplete="new-password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} minLength={12} required /></label>
            {error && <div className="inline-error" role="alert">{error}</div>}
            <button className="button primary" type="submit" disabled={saving}>
              <Check size={14} /> {saving ? "Updating…" : "Update Password"}
            </button>
          </form>
        </div>
      )}
    </div>
  );
}