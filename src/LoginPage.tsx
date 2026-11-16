import { useState } from "react";
import { Eye, EyeOff, LockKeyhole, ShieldCheck } from "lucide-react";

export default function LoginPage({
  onLogin,
  onSetupAdmin,
  initialSetupRequired = false,
}: {
  onLogin: (name: string, password: string) => Promise<void>;
  onSetupAdmin?: (name: string, password: string) => Promise<void>;
  initialSetupRequired?: boolean;
}) {
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError("");
    if (initialSetupRequired && onSetupAdmin) {
      if (password !== confirmPassword) {
        setError("Passwords do not match.");
        return;
      }
      setLoading(true);
      try {
        await onSetupAdmin(name, password);
      } catch (requestError) {
        setError(requestError instanceof Error ? requestError.message : "Could not create administrator account.");
      } finally {
        setLoading(false);
      }
      return;
    }
    setLoading(true);
    try {
      await onLogin(name, password);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Sign in failed.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="login-page">
      <div className="login-orbit login-orbit-one" />
      <div className="login-orbit login-orbit-two" />
      <section className="login-panel">
        <div className="login-brand">
          <div className="login-brand-mark">N</div>
          <div>
            <b>Portfolio Monitor</b>
            <span>PLUTUS WEALTH MANAGEMENT</span>
          </div>
        </div>
        <div className="login-intro">
          <span className="login-kicker">
            <ShieldCheck size={14} />{" "}
            {initialSetupRequired ? "ONE-TIME INITIAL SETUP" : "SECURE TEAM ACCESS"}
          </span>
          <h1>{initialSetupRequired ? "Create Administrator Account" : "Welcome back."}</h1>
          <p>
            {initialSetupRequired
              ? "No administrator is configured yet. Create your Administrator account (Name + Password, 12+ characters with at least 3 character types) to unlock Portfolio Monitor and add employee accounts."
              : "Sign in with your team name and password to open the shared portfolio workspace."}
          </p>
        </div>
        <form onSubmit={(event) => void submit(event)} className="login-form">
          <label>
            <span>{initialSetupRequired ? "Administrator Name" : "Name"}</span>
            <input
              autoComplete="username"
              autoFocus
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder={initialSetupRequired ? "e.g. Srushti Kadam" : "Your name"}
              required
            />
          </label>
          <label>
            <span>Password</span>
            <div className="login-password-field">
              <LockKeyhole size={16} />
              <input
                autoComplete={initialSetupRequired ? "new-password" : "current-password"}
                type={showPassword ? "text" : "password"}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder={
                  initialSetupRequired
                    ? "12+ chars (uppercase, lowercase, number, symbol)"
                    : "Enter your password"
                }
                minLength={initialSetupRequired ? 12 : 1}
                required
              />
              <button
                type="button"
                aria-label={showPassword ? "Hide password" : "Show password"}
                onClick={() => setShowPassword((visible) => !visible)}
              >
                {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
          </label>
          {initialSetupRequired && (
            <label>
              <span>Confirm Password</span>
              <div className="login-password-field">
                <LockKeyhole size={16} />
                <input
                  autoComplete="new-password"
                  type={showPassword ? "text" : "password"}
                  value={confirmPassword}
                  onChange={(event) => setConfirmPassword(event.target.value)}
                  placeholder="Confirm administrator password"
                  minLength={12}
                  required
                />
              </div>
            </label>
          )}
          {error && <div className="login-error" role="alert">{error}</div>}
          <button className="login-submit" type="submit" disabled={loading}>
            {loading
              ? initialSetupRequired
                ? "Creating Administrator…"
                : "Signing in…"
              : initialSetupRequired
                ? "Create Administrator & Sign In"
                : "Sign In"}
          </button>
        </form>
        <div className="login-footnote"><span /> Monitoring workspace · No order execution</div>
      </section>
    </main>
  );
}