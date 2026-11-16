import { useEffect, useState } from "react";
import { Activity, RefreshCw } from "lucide-react";
import { fetchActivity } from "./authService";
import type { ActivityEntry } from "./authService";

export default function ActivityPage() {
  const [items, setItems] = useState<ActivityEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const refresh = async () => {
    setLoading(true);
    try {
      setItems(await fetchActivity());
      setError("");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Could not load activity.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void refresh(); }, []);

  return (
    <div className="admin-page">
      <div className="page-heading">
        <div><div className="eyebrow">ADMINISTRATION</div><h1>Activity Log</h1><p>Authentication, account, import, approval, and portfolio actions.</p></div>
        <button className="button secondary" onClick={() => void refresh()} disabled={loading}><RefreshCw size={14} /> Refresh</button>
      </div>
      <section className="card activity-log-card">
        <div className="section-head"><div><h2>Recent Activity</h2><p>Newest events first</p></div><Activity size={17} className="muted-icon" /></div>
        {loading ? <div className="empty-state"><RefreshCw size={18} /><b>Loading activity…</b></div> : error ? <div className="empty-state" role="alert">{error}</div> : items.length === 0 ? <div className="empty-state"><b>No activity recorded yet.</b></div> : (
          <div className="table-scroll">
            <table className="portfolio-table activity-table">
              <thead><tr><th>Time</th><th>User</th><th>Action</th><th>Details</th></tr></thead>
              <tbody>{items.map((item) => (
                <tr key={item.id}>
                  <td>{new Date(item.timestamp).toLocaleString("en-IN")}</td>
                  <td>{item.name || "System"}</td>
                  <td><span className="activity-action">{item.action.replace(/_/g, " ")}</span></td>
                  <td><code>{item.details ? JSON.stringify(item.details) : "—"}</code></td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}