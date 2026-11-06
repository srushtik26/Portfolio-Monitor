import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  Activity,
  AlertTriangle,
  ArrowDownRight,
  ArrowDownUp,
  ArrowLeft,
  ArrowRight,
  ArrowUpRight,
  Bell,
  BriefcaseBusiness,
  Check,
  CheckCheck,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  Clock3,
  Command,
  Download,
  FileSpreadsheet,
  History,
  LayoutDashboard,
  LineChart,
  Menu,
  Moon,
  MoreHorizontal,
  PanelLeftClose,
  RefreshCw,
  RotateCcw,
  Search,
  Settings,
  ShieldCheck,
  Sun,
  Target,
  TrendingDown,
  TrendingUp,
  Upload,
  Users,
  X,
  Zap,
} from "lucide-react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  fetchAlerts,
  fetchCompanies,
  fetchCompanyDetails,
  fetchImportHistory,
  fetchNotificationPreferences,
  fetchWorkerStatus,
  performAlertAction,
  restoreImportVersion,
  triggerOnDemandPoll,
  updateCompanyTargets,
  updateNotificationPreferences,
} from "./api";
import BackendUploadPage from "./UploadPage";
import type {
  ApiAlert,
  CompanyDetailsResponse,
  ImportBatchHistoryItem,
  NotificationPreference,
  PortfolioCompany,
  RearmPolicy,
  TargetDirection,
  TriggerMode,
  WorkerStatus,
} from "./types";

type Page =
  | "Overview"
  | "Portfolio"
  | "Price Alerts"
  | "Target Management"
  | "Market Activity"
  | "Reports"
  | "Upload Excel"
  | "Settings";

const nav: Array<{ label: Page; icon: typeof LayoutDashboard; group?: string }> = [
  { label: "Overview", icon: LayoutDashboard, group: "WORKSPACE" },
  { label: "Portfolio", icon: BriefcaseBusiness },
  { label: "Price Alerts", icon: Bell },
  { label: "Target Management", icon: Target, group: "MONITORING" },
  { label: "Market Activity", icon: Activity },
  { label: "Reports", icon: LineChart, group: "TOOLS" },
  { label: "Upload Excel", icon: Upload },
  { label: "Settings", icon: Settings, group: "PREFERENCES" },
];

const money = (n: number) =>
  `₹${n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const percent = (n: number) => `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;

function formatDistance(absVal?: number | null, pctVal?: number | null) {
  if (absVal == null || pctVal == null) return "—";
  const sign = absVal > 0 ? "+" : "";
  return `${sign}${money(absVal)} (${percent(pctVal)})`;
}

function alertStatusBadge(company: PortfolioCompany) {
  const quote = company.latest_quote;
  const isStale = quote != null && !quote.is_fresh && !quote.is_simulated;
  if (isStale) {
    return <span className="status-badge badge-sell"><i />STALE QUOTE</span>;
  }
  const st = company.alert_status ?? "monitoring";
  if (st === "both_triggered") {
    return <span className="status-badge badge-sell"><i />Base & Bull Triggered</span>;
  }
  if (st === "base_triggered") {
    return <span className="status-badge badge-buy"><i />Base Target Reached</span>;
  }
  if (st === "bull_triggered") {
    return <span className="status-badge badge-sell"><i />Bull Target Reached</span>;
  }
  if (st === "pending_configuration") {
    return <span className="status-badge badge-approaching"><i />Pending Config</span>;
  }
  if (st === "inactive") {
    return <span className="status-badge badge-neutral"><i />Paused</span>;
  }
  if (company.nearest_target_pct != null && company.nearest_target_pct <= 2.5) {
    return <span className="status-badge badge-approaching"><i />Approaching ({company.nearest_target_pct.toFixed(1)}%)</span>;
  }
  return <span className="status-badge badge-neutral"><i />Monitoring</span>;
}

export default function App() {
  const [page, setPage] = useState<Page>("Overview");
  const [portfolioCompanies, setPortfolioCompanies] = useState<PortfolioCompany[]>([]);
  const [portfolioStatus, setPortfolioStatus] = useState<"loading" | "ready" | "error">("loading");
  const [portfolioError, setPortfolioError] = useState("");
  const [portfolioRequest, setPortfolioRequest] = useState(0);

  const [apiAlerts, setApiAlerts] = useState<ApiAlert[]>([]);
  const [apiAlertStatus, setApiAlertStatus] = useState<"loading" | "ready" | "error">("loading");
  const [apiAlertError, setApiAlertError] = useState("");
  const [workerStatus, setWorkerStatus] = useState<WorkerStatus | null>(null);
  const [importHistory, setImportHistory] = useState<ImportBatchHistoryItem[]>([]);

  const [dark, setDark] = useState(true);
  const [collapsed, setCollapsed] = useState(false);
  const [mobileNav, setMobileNav] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [selectedCompany, setSelectedCompany] = useState<PortfolioCompany | null>(null);
  const [editing, setEditing] = useState<PortfolioCompany | null>(null);
  const [toast, setToast] = useState("");
  const [query, setQuery] = useState("");
  const [compactTables, setCompactTables] = useState(true);
  const [isPollingNow, setIsPollingNow] = useState(false);

  const notify = (message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(""), 3500);
  };

  const reloadAll = () => setPortfolioRequest((r) => r + 1);

  const pickPage = (next: Page) => {
    setPage(next);
    setMobileNav(false);
  };

  useEffect(() => {
    const controller = new AbortController();
    setPortfolioStatus("loading");
    setPortfolioError("");
    setApiAlertStatus("loading");
    setApiAlertError("");

    Promise.all([
      fetchCompanies(controller.signal),
      fetchAlerts(controller.signal),
      fetchWorkerStatus(controller.signal),
      fetchImportHistory(controller.signal).catch(() => [] as ImportBatchHistoryItem[]),
    ])
      .then(([companiesData, alertsData, workerData, importsData]) => {
        setPortfolioCompanies(companiesData);
        setApiAlerts(alertsData);
        setWorkerStatus(workerData);
        setImportHistory(importsData);
        setPortfolioStatus("ready");
        setApiAlertStatus("ready");
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        const msg = error instanceof Error ? error.message : "Failed to load monitoring data.";
        setPortfolioError(msg);
        setApiAlertError(msg);
        setPortfolioStatus("error");
        setApiAlertStatus("error");
      });

    return () => controller.abort();
  }, [portfolioRequest]);

  // Periodic background refresh of dashboard view every 30s
  useEffect(() => {
    const timer = window.setInterval(() => {
      void Promise.all([fetchCompanies(), fetchAlerts(), fetchWorkerStatus()])
        .then(([c, a, w]) => {
          setPortfolioCompanies(c);
          setApiAlerts(a);
          setWorkerStatus(w);
        })
        .catch(() => null);
    }, 30000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (selectedCompany) {
      const updated = portfolioCompanies.find(
        (c) => c.exchange === selectedCompany.exchange && c.symbol === selectedCompany.symbol,
      );
      if (updated) setSelectedCompany(updated);
    }
  }, [portfolioCompanies, selectedCompany]);

  const handlePollNow = async () => {
    setIsPollingNow(true);
    try {
      const res = await triggerOnDemandPoll();
      notify(`Fetched ${res.recorded}/${res.attempted} live quotes`);
      reloadAll();
    } catch (err) {
      notify(err instanceof Error ? err.message : "Quote poll failed");
    } finally {
      setIsPollingNow(false);
    }
  };

  const handleAlertLifecycle = async (
    alertId: number,
    action: "acknowledge" | "resolve" | "reset",
  ) => {
    try {
      await performAlertAction(alertId, action);
      notify(`Alert ${action}d`);
      reloadAll();
    } catch (err) {
      notify(err instanceof Error ? err.message : "Could not update alert");
    }
  };

  const activeAlertsCount = apiAlerts.filter(
    (a) => (a.lifecycle_status ?? "triggered") === "triggered",
  ).length;
  const staleCompanies = portfolioCompanies.filter(
    (c) => c.latest_quote != null && !c.latest_quote.is_fresh && !c.latest_quote.is_simulated,
  );
  const feedHealthy =
    portfolioStatus === "ready" &&
    workerStatus?.state !== "error" &&
    staleCompanies.length === 0;

  return (
    <div
      className={`app-shell min-h-screen ${dark ? "theme-dark" : "theme-light"} ${
        compactTables ? "compact-tables" : ""
      }`}
    >
      <aside
        className={`sidebar ${collapsed ? "sidebar-collapsed" : ""} ${
          mobileNav ? "sidebar-mobile-open" : ""
        }`}
      >
        <div className="brand">
          <div className="brand-mark">
            <span>N</span>
          </div>
          {!collapsed && (
            <div>
              <div className="brand-name">
                northstar<span>.</span>
              </div>
              <div className="brand-caption">INVESTMENT OFFICE</div>
            </div>
          )}
          <button
            className="icon-button sidebar-collapse"
            aria-label="Collapse sidebar"
            onClick={() => setCollapsed(!collapsed)}
          >
            <PanelLeftClose size={16} />
          </button>
        </div>
        <div className="workspace-switch">
          <div className="workspace-logo">PW</div>
          {!collapsed && (
            <>
              <div className="workspace-copy">
                <b>Plutus Wealth</b>
                <small>NSE & BSE Monitoring</small>
              </div>
              <ChevronDown size={14} />
            </>
          )}
        </div>
        <nav className="side-nav" aria-label="Main navigation">
          {nav.map((item) => (
            <div key={item.label}>
              {item.group && !collapsed && <div className="nav-group">{item.group}</div>}
              <button
                className={`nav-item ${page === item.label ? "nav-active" : ""}`}
                title={collapsed ? item.label : undefined}
                onClick={() => pickPage(item.label)}
              >
                <item.icon size={17} strokeWidth={1.8} />
                <span>{!collapsed && item.label}</span>
                {item.label === "Price Alerts" && activeAlertsCount > 0 && (
                  <em>{activeAlertsCount}</em>
                )}
              </button>
            </div>
          ))}
        </nav>
        {!collapsed && (
          <div className="sidebar-bottom">
            <div className="support-box">
              <div className="support-icon">
                <ShieldCheck size={16} />
              </div>
              <div>
                <b>Monitoring Only</b>
                <small>Zero order execution</small>
              </div>
              <ArrowRight size={14} />
            </div>
            <div className="sidebar-user">
              <div className="avatar">SK</div>
              <div className="user-copy">
                <b>Srushti Kadam</b>
                <small>Portfolio Manager</small>
              </div>
              <MoreHorizontal size={18} />
            </div>
          </div>
        )}
      </aside>

      <div className="main-area">
        <header className="topbar">
          <button
            className="icon-button mobile-menu"
            aria-label="Open navigation"
            onClick={() => setMobileNav(!mobileNav)}
          >
            <Menu size={19} />
          </button>
          <div className="breadcrumbs">
            <span>Workspace</span>
            <ChevronRight size={14} />
            <b>{page}</b>
          </div>
          <div className="top-actions">
            <label className="global-search">
              <Search size={15} />
              <input
                placeholder="Search companies or symbols..."
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && query.trim()) {
                    const match = portfolioCompanies.find((c) =>
                      `${c.name} ${c.symbol}`.toLowerCase().includes(query.toLowerCase()),
                    );
                    if (match) {
                      setSelectedCompany(match);
                    }
                  }
                }}
              />
              <kbd>
                <Command size={11} /> K
              </kbd>
            </label>
            <div className="top-divider" />
            <div className={`connection ${feedHealthy ? "connected" : "disconnected"}`}>
              <span className="status-dot" />
              <span className="connection-label">
                {portfolioStatus === "error"
                  ? "Backend Disconnected"
                  : staleCompanies.length > 0
                    ? `${staleCompanies.length} Stale Quote(s)`
                    : `Feed ${workerStatus?.provider ?? "yfinance"} · ${
                        workerStatus?.state ?? "ready"
                      }`}
              </span>
            </div>
            <button
              className="icon-button notification-button"
              aria-label="Open notifications"
              onClick={() => setDrawer(true)}
            >
              <Bell size={18} />
              {activeAlertsCount > 0 && <i>{activeAlertsCount}</i>}
            </button>
            <button
              className="profile-button"
              onClick={() => pickPage("Settings")}
              aria-label="Open profile and settings"
            >
              <div className="avatar small-avatar">SK</div>
              <ChevronDown size={14} />
            </button>
          </div>
        </header>

        <main className="content-area">
          {(portfolioStatus === "error" ||
            workerStatus?.state === "error" ||
            staleCompanies.length > 0) && (
            <div className="feed-banner feed-error" role="alert">
              <div className="feed-main-icon">
                <AlertTriangle size={18} />
              </div>
              <div>
                <b>
                  {portfolioStatus === "error"
                    ? "Market Monitoring API Disconnected"
                    : workerStatus?.state === "error"
                      ? `Market Feed Warning: ${workerStatus.last_error}`
                      : `${staleCompanies.length} Symbol(s) Have Stale Quotes`}
                </b>
                <small>
                  Never treat stale or reference CMP values as live market prices. Click "Poll Live Quotes" to refresh from the market data provider.
                </small>
              </div>
              <div className="feed-banner-right">
                <button
                  className="button primary small"
                  onClick={() => void handlePollNow()}
                  disabled={isPollingNow}
                >
                  <RefreshCw size={13} /> {isPollingNow ? "Polling…" : "Poll Live Quotes"}
                </button>
              </div>
            </div>
          )}

          {page === "Overview" && (
            <OverviewPage
              companies={portfolioCompanies}
              alerts={apiAlerts}
              workerStatus={workerStatus}
              importHistory={importHistory}
              onSelectCompany={setSelectedCompany}
              onPage={pickPage}
              onPollNow={handlePollNow}
              isPollingNow={isPollingNow}
              onRestoreVersion={async (id) => {
                await restoreImportVersion(id);
                notify("Restored earlier approved target version");
                reloadAll();
              }}
              search={query}
            />
          )}

          {page === "Portfolio" && (
            <PortfolioPage
              companies={portfolioCompanies}
              requestStatus={portfolioStatus}
              error={portfolioError}
              onRefresh={reloadAll}
              onPollNow={handlePollNow}
              isPollingNow={isPollingNow}
              onSelectCompany={setSelectedCompany}
              onEditCompany={setEditing}
              search={query}
              onSearchChange={setQuery}
            />
          )}

          {page === "Price Alerts" && (
            <AlertsPage
              apiAlerts={apiAlerts}
              apiStatus={apiAlertStatus}
              apiError={apiAlertError}
              workerStatus={workerStatus}
              onRefresh={reloadAll}
              onAction={handleAlertLifecycle}
            />
          )}

          {page === "Target Management" && (
            <TargetManagement
              companies={portfolioCompanies}
              requestStatus={portfolioStatus}
              error={portfolioError}
              onRefresh={reloadAll}
              onImport={() => pickPage("Upload Excel")}
              onEdit={setEditing}
              onSelectCompany={setSelectedCompany}
              search={query}
            />
          )}

          {page === "Market Activity" && (
            <MarketActivityPage
              companies={portfolioCompanies}
              workerStatus={workerStatus}
              onPollNow={handlePollNow}
              isPollingNow={isPollingNow}
              onSelectCompany={setSelectedCompany}
            />
          )}

          {page === "Reports" && (
            <ReportsPage
              companies={portfolioCompanies}
              alerts={apiAlerts}
              notify={notify}
              onSelectCompany={setSelectedCompany}
            />
          )}

          {page === "Upload Excel" && <BackendUploadPage onCompleted={reloadAll} />}

          {page === "Settings" && (
            <SettingsPage
              dark={dark}
              setDark={setDark}
              compactTables={compactTables}
              setCompactTables={setCompactTables}
              workerStatus={workerStatus}
              notify={notify}
            />
          )}
        </main>

        <footer className="app-footer">
          <span>
            Northstar Portfolio Monitor <span className="footer-version">v2.0 Production</span>
          </span>
          <span>
            <ShieldCheck size={13} /> Price-Monitoring & Target-Alert System <i /> No Order Execution
          </span>
        </footer>
      </div>

      <AnimatePresence>
        {drawer && (
          <NotificationDrawer
            alerts={apiAlerts}
            onClose={() => setDrawer(false)}
            onAction={handleAlertLifecycle}
          />
        )}
      </AnimatePresence>

      <AnimatePresence>
        {selectedCompany && (
          <DetailPanel
            company={selectedCompany}
            onClose={() => setSelectedCompany(null)}
            onEditTargets={() => {
              setEditing(selectedCompany);
            }}
            onAction={handleAlertLifecycle}
          />
        )}
      </AnimatePresence>

      <AnimatePresence>
        {editing && (
          <EditTargetModal
            key={`${editing.exchange}:${editing.symbol}:${editing.version}`}
            company={editing}
            onClose={() => setEditing(null)}
            onSaved={() => {
              setEditing(null);
              notify("Target configuration saved");
              reloadAll();
            }}
          />
        )}
      </AnimatePresence>

      <AnimatePresence>
        {toast && (
          <motion.div
            className="toast"
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 8 }}
          >
            <Check size={15} />
            {toast}
          </motion.div>
        )}
      </AnimatePresence>
      {mobileNav && (
        <button
          className="mobile-backdrop"
          aria-label="Close navigation"
          onClick={() => setMobileNav(false)}
        />
      )}
    </div>
  );
}

function PageHeading({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow: string;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="page-heading">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action && <div className="heading-actions">{action}</div>}
    </div>
  );
}

function MetricCard({
  label,
  value,
  change,
  icon: Icon,
  tone,
  hint,
}: {
  label: string;
  value: string;
  change?: string;
  icon: typeof Users;
  tone: string;
  hint: string;
}) {
  return (
    <motion.div className="metric-card" whileHover={{ y: -2 }} transition={{ duration: 0.15 }}>
      <div className="metric-top">
        <span>{label}</span>
        <div className={`metric-icon ${tone}`}>
          <Icon size={17} />
        </div>
      </div>
      <div className="metric-value">{value}</div>
      <div className="metric-foot">
        {change && (
          <span
            className={
              tone === "green" ? "text-green" : tone === "red" ? "text-red" : "text-muted"
            }
          >
            {change}
          </span>
        )}
        <span>{hint}</span>
      </div>
    </motion.div>
  );
}

function OverviewPage({
  companies,
  alerts,
  workerStatus,
  importHistory,
  onSelectCompany,
  onPage,
  onPollNow,
  isPollingNow,
  onRestoreVersion,
  search,
}: {
  companies: PortfolioCompany[];
  alerts: ApiAlert[];
  workerStatus: WorkerStatus | null;
  importHistory: ImportBatchHistoryItem[];
  onSelectCompany: (c: PortfolioCompany) => void;
  onPage: (p: Page) => void;
  onPollNow: () => Promise<void>;
  isPollingNow: boolean;
  onRestoreVersion: (id: string) => Promise<void>;
  search: string;
}) {
  const activeMonitoringCount = companies.filter(
    (c) => c.is_approved !== false && c.monitoring_active !== false,
  ).length;
  const baseAlertsCount = companies.filter(
    (c) => c.alert_status === "base_triggered" || c.alert_status === "both_triggered",
  ).length;
  const bullAlertsCount = companies.filter(
    (c) => c.alert_status === "bull_triggered" || c.alert_status === "both_triggered",
  ).length;
  const pendingConfigCount =
    companies.filter((c) => c.alert_status === "pending_configuration").length +
    importHistory.filter((b) => b.status === "preview").length;

  const nearestCompanies = useMemo(() => {
    return [...companies]
      .filter((c) => !search || `${c.name} ${c.symbol}`.toLowerCase().includes(search.toLowerCase()))
      .sort((a, b) => (a.nearest_target_pct ?? 9999) - (b.nearest_target_pct ?? 9999))
      .slice(0, 12);
  }, [companies, search]);

  return (
    <>
      <PageHeading
        eyebrow="PORTFOLIO MONITORING COMMAND CENTER"
        title="Portfolio Overview"
        description="Real-time NSE & BSE price monitoring, configurable Base/Bull target alerts, and feed telemetry."
        action={
          <div className="heading-actions">
            <button
              className="button secondary"
              onClick={() => void onPollNow()}
              disabled={isPollingNow}
            >
              <RefreshCw size={14} /> {isPollingNow ? "Polling Quotes…" : "Poll Live Quotes"}
            </button>
            <button className="button primary" onClick={() => onPage("Upload Excel")}>
              <Upload size={14} /> Upload Excel Targets
            </button>
          </div>
        }
      />

      <div className="metric-grid">
        <MetricCard
          label="Total Monitored Companies"
          value={String(companies.length)}
          change={`${activeMonitoringCount} active`}
          icon={BriefcaseBusiness}
          tone="blue"
          hint="Approved NSE & BSE stocks"
        />
        <MetricCard
          label="Base Target Alerts"
          value={String(baseAlertsCount)}
          change={`${alerts.filter((a) => a.target_kind === "base").length} total events`}
          icon={ArrowDownRight}
          tone="green"
          hint="Configured Base condition met"
        />
        <MetricCard
          label="Bull Target Alerts"
          value={String(bullAlertsCount)}
          change={`${alerts.filter((a) => a.target_kind === "bull").length} total events`}
          icon={ArrowUpRight}
          tone="red"
          hint="Configured Bull condition met"
        />
        <MetricCard
          label="Pending / Feed Status"
          value={workerStatus?.provider ? workerStatus.provider.toUpperCase() : "READY"}
          change={`${pendingConfigCount} pending config`}
          icon={Activity}
          tone={workerStatus?.state === "error" ? "red" : "green"}
          hint={
            workerStatus?.last_poll_at
              ? `Last poll: ${new Date(workerStatus.last_poll_at).toLocaleTimeString("en-IN")}`
              : "Independent worker ready"
          }
        />
      </div>

      <section className="card portfolio-card" style={{ marginBottom: 20 }}>
        <div className="section-head">
          <div>
            <h2>Main Monitoring Table (Nearest to Target)</h2>
            <p>Click any company row to open full target configuration, price history, and Excel cell source details.</p>
          </div>
          <button className="text-link" onClick={() => onPage("Portfolio")}>
            Open full monitoring table ({companies.length}) <ArrowRight size={13} />
          </button>
        </div>
        <div className="table-scroll">
          <MonitoringTable
            companies={nearestCompanies}
            onSelectCompany={onSelectCompany}
          />
        </div>
      </section>

      <div className="overview-lower-grid">
        <section className="card activity-card">
          <div className="section-head">
            <div>
              <h2>Alert Activity</h2>
              <p>Recently triggered Base & Bull alerts</p>
            </div>
            <button className="text-link" onClick={() => onPage("Price Alerts")}>
              All alerts ({alerts.length}) <ArrowRight size={13} />
            </button>
          </div>
          <div className="activity-list">
            {alerts.length === 0 ? (
              <p className="empty-copy">No triggered alerts yet.</p>
            ) : (
              alerts.slice(0, 6).map((alert) => (
                <div className="activity-row" key={alert.id}>
                  <span
                    className={`activity-marker ${
                      alert.target_kind === "bull" ? "marker-red" : "marker-green"
                    }`}
                  >
                    <Zap size={12} />
                  </span>
                  <div>
                    <b>
                      {alert.symbol} ({alert.exchange}) · {alert.target_kind.toUpperCase()} Target
                    </b>
                    <small>
                      Trigger: {money(alert.price)} vs Target{" "}
                      {alert.target == null ? "—" : money(alert.target)} · Delivery:{" "}
                      {alert.delivery_status.toUpperCase()}
                    </small>
                  </div>
                  <time>{new Date(alert.triggered_at).toLocaleTimeString("en-IN")}</time>
                </div>
              ))
            )}
          </div>
        </section>

        <section className="card target-list-card">
          <div className="section-head">
            <div>
              <h2>Market Feed Health</h2>
              <p>Provider & worker telemetry</p>
            </div>
            <button className="text-link" onClick={() => onPage("Market Activity")}>
              Feed details <ArrowRight size={13} />
            </button>
          </div>
          <div className="worker-status-grid" style={{ padding: "8px 0" }}>
            <div>
              <small>Provider Connection</small>
              <b>{workerStatus?.provider ?? "yfinance (NSE/BSE)"}</b>
            </div>
            <div>
              <small>WebSocket / Transport</small>
              <b>{workerStatus?.websocket_status ?? "Polling fallback active"}</b>
            </div>
            <div>
              <small>Last Successful Update</small>
              <b>
                {workerStatus?.last_poll_at
                  ? new Date(workerStatus.last_poll_at).toLocaleString("en-IN")
                  : "Awaiting poll"}
              </b>
            </div>
            <div>
              <small>Stale / Failed Quotes</small>
              <b>
                {workerStatus?.stale_symbols_count ?? 0} stale ·{" "}
                {workerStatus?.last_poll_failed ?? 0} failed
              </b>
            </div>
          </div>
        </section>

        <section className="card target-list-card">
          <div className="section-head">
            <div>
              <h2>Import History</h2>
              <p>Versioned Excel uploads</p>
            </div>
            <button className="text-link" onClick={() => onPage("Upload Excel")}>
              Manage imports <ArrowRight size={13} />
            </button>
          </div>
          <div className="activity-list">
            {importHistory.length === 0 ? (
              <p className="empty-copy">No Excel imports recorded yet.</p>
            ) : (
              importHistory.slice(0, 5).map((batch) => (
                <div className="activity-row" key={batch.id}>
                  <span className="activity-marker marker-amber">
                    <FileSpreadsheet size={12} />
                  </span>
                  <div>
                    <b>
                      {batch.version_number ? `v${batch.version_number}` : "Draft"} ·{" "}
                      {batch.filename}
                    </b>
                    <small>
                      {batch.status.toUpperCase()} · {batch.uploaded_by}
                    </small>
                  </div>
                  {batch.status === "confirmed" &&
                    batch.changes_snapshot &&
                    batch.changes_snapshot.length > 0 && (
                      <button
                        className="button secondary small"
                        onClick={() => void onRestoreVersion(batch.id)}
                      >
                        <RotateCcw size={11} /> Restore
                      </button>
                    )}
                </div>
              ))
            )}
          </div>
        </section>
      </div>
    </>
  );
}

function MonitoringTable({
  companies,
  onSelectCompany,
  onSort,
}: {
  companies: PortfolioCompany[];
  onSelectCompany: (c: PortfolioCompany) => void;
  onSort?: (key: string) => void;
}) {
  if (companies.length === 0) {
    return (
      <div className="empty-state">
        <BriefcaseBusiness size={22} />
        <b>No matching companies</b>
        <span>Upload an Excel workbook or adjust your filters.</span>
      </div>
    );
  }
  return (
    <table className="portfolio-table">
      <thead>
        <tr>
          <th>
            {onSort ? (
              <button onClick={() => onSort("name")}>
                Company <ArrowDownUp size={11} />
              </button>
            ) : (
              "Company"
            )}
          </th>
          <th>
            {onSort ? (
              <button onClick={() => onSort("price")}>
                Latest Price <ArrowDownUp size={11} />
              </button>
            ) : (
              "Latest Price"
            )}
          </th>
          <th>
            {onSort ? (
              <button onClick={() => onSort("base")}>
                Base Target <ArrowDownUp size={11} />
              </button>
            ) : (
              "Base Target"
            )}
          </th>
          <th>
            {onSort ? (
              <button onClick={() => onSort("bull")}>
                Bull Target <ArrowDownUp size={11} />
              </button>
            ) : (
              "Bull Target"
            )}
          </th>
          <th>Distance to Base</th>
          <th>Distance to Bull</th>
          <th>
            {onSort ? (
              <button onClick={() => onSort("nearest")}>
                Alert Status <ArrowDownUp size={11} />
              </button>
            ) : (
              "Alert Status"
            )}
          </th>
          <th>Last Quote</th>
        </tr>
      </thead>
      <tbody>
        {companies.map((company) => {
          const quote = company.latest_quote;
          const isStale = quote != null && !quote.is_fresh && !quote.is_simulated;
          const vsSavedCmp =
            quote && company.cmp && company.cmp > 0
              ? ((quote.price - company.cmp) / company.cmp) * 100
              : null;
          return (
            <tr
              key={`${company.exchange}:${company.symbol}`}
              onClick={() => onSelectCompany(company)}
              style={{
                cursor: "pointer",
                background: isStale ? "rgba(239, 114, 123, 0.08)" : undefined,
              }}
            >
              <td>
                <div className="company-name-cell">
                  <div className="company-avatar">{company.symbol.slice(0, 2)}</div>
                  <span>
                    <b>{company.name}</b>
                    <small>
                      {company.symbol} ·{" "}
                      <span
                        className={`exchange-tag ${
                          company.exchange === "BSE" ? "bse-tag" : ""
                        }`}
                      >
                        {company.exchange}
                      </span>{" "}
                      · Ref CMP: {company.cmp == null ? "—" : money(company.cmp)}
                    </small>
                  </span>
                </div>
              </td>
              <td>
                {quote ? (
                  <div>
                    <b>{money(quote.price)}</b>{" "}
                    {vsSavedCmp != null && (
                      <span className={vsSavedCmp >= 0 ? "positive" : "negative"}>
                        {vsSavedCmp >= 0 ? (
                          <ArrowUpRight size={12} />
                        ) : (
                          <ArrowDownRight size={12} />
                        )}
                        {percent(vsSavedCmp)}
                      </span>
                    )}
                    <small className="quote-freshness">
                      {isStale ? "⚠ STALE QUOTE" : quote.is_simulated ? "Simulated" : "Live Validated"}
                    </small>
                  </div>
                ) : (
                  <span className="quote-freshness">Awaiting live quote</span>
                )}
              </td>
              <td className="target-base-price">
                <div>
                  <b>{company.base_target == null ? "—" : money(company.base_target)}</b>
                </div>
                <small className="cell-sub">
                  {company.base_direction === "below"
                    ? "Trigger ≤ Base"
                    : company.base_direction === "above"
                      ? "Trigger ≥ Base"
                      : "Pending direction"}
                </small>
              </td>
              <td className="target-bull-price">
                <div>
                  <b>{company.bull_target == null ? "—" : money(company.bull_target)}</b>
                </div>
                <small className="cell-sub">
                  {company.bull_direction === "above"
                    ? "Trigger ≥ Bull"
                    : company.bull_direction === "below"
                      ? "Trigger ≤ Bull"
                      : "Pending direction"}
                </small>
              </td>
              <td>
                <span
                  className={
                    (company.distance_to_base_pct ?? 0) <= 0 ? "positive" : "text-muted"
                  }
                >
                  {formatDistance(company.distance_to_base_abs, company.distance_to_base_pct)}
                </span>
              </td>
              <td>
                <span
                  className={
                    (company.distance_to_bull_pct ?? 0) >= 0 ? "positive" : "text-muted"
                  }
                >
                  {formatDistance(company.distance_to_bull_abs, company.distance_to_bull_pct)}
                </span>
              </td>
              <td>{alertStatusBadge(company)}</td>
              <td>
                {quote ? (
                  <div>
                    <div>{new Date(quote.quoted_at).toLocaleTimeString("en-IN")}</div>
                    <small className="cell-sub">
                      {quote.provider} · {isStale ? "STALE" : "Fresh"}
                    </small>
                  </div>
                ) : (
                  <small className="cell-sub">No quote yet</small>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function PortfolioPage({
  companies,
  requestStatus,
  error,
  onRefresh,
  onPollNow,
  isPollingNow,
  onSelectCompany,
  search,
  onSearchChange,
}: {
  companies: PortfolioCompany[];
  requestStatus: "loading" | "ready" | "error";
  error: string;
  onRefresh: () => void;
  onPollNow: () => Promise<void>;
  isPollingNow: boolean;
  onSelectCompany: (c: PortfolioCompany) => void;
  onEditCompany: (c: PortfolioCompany) => void;
  search: string;
  onSearchChange: (v: string) => void;
}) {
  const [exchange, setExchange] = useState("All exchanges");
  const [alertFilter, setAlertFilter] = useState("All statuses");
  const [sort, setSort] = useState("nearest");
  const [ascending, setAscending] = useState(true);

  const filtered = useMemo(() => {
    return companies
      .filter((c) => {
        const term = search.toLowerCase();
        const matchesSearch =
          !term || `${c.name} ${c.symbol}`.toLowerCase().includes(term);
        const matchesExchange =
          exchange === "All exchanges" || c.exchange === exchange;
        const isStale =
          c.latest_quote != null &&
          !c.latest_quote.is_fresh &&
          !c.latest_quote.is_simulated;
        const matchesAlert =
          alertFilter === "All statuses" ||
          (alertFilter === "Triggered" &&
            (c.alert_status === "base_triggered" ||
              c.alert_status === "bull_triggered" ||
              c.alert_status === "both_triggered")) ||
          (alertFilter === "Base Triggered" &&
            (c.alert_status === "base_triggered" ||
              c.alert_status === "both_triggered")) ||
          (alertFilter === "Bull Triggered" &&
            (c.alert_status === "bull_triggered" ||
              c.alert_status === "both_triggered")) ||
          (alertFilter === "Pending Config" &&
            c.alert_status === "pending_configuration") ||
          (alertFilter === "Stale Quote" && isStale) ||
          (alertFilter === "Monitoring" && c.alert_status === "monitoring");
        return matchesSearch && matchesExchange && matchesAlert;
      })
      .sort((a, b) => {
        let result = 0;
        if (sort === "name") {
          result = a.name.localeCompare(b.name);
        } else if (sort === "price") {
          result = (a.latest_quote?.price ?? 0) - (b.latest_quote?.price ?? 0);
        } else if (sort === "base") {
          result = (a.base_target ?? 0) - (b.base_target ?? 0);
        } else if (sort === "bull") {
          result = (a.bull_target ?? 0) - (b.bull_target ?? 0);
        } else {
          result = (a.nearest_target_pct ?? 9999) - (b.nearest_target_pct ?? 9999);
        }
        return ascending ? result : -result;
      });
  }, [companies, search, exchange, alertFilter, sort, ascending]);

  const chooseSort = (key: string) => {
    setAscending(sort === key ? !ascending : true);
    setSort(key);
  };

  return (
    <>
      <PageHeading
        eyebrow="MAIN MONITORING TABLE · LIVE NSE & BSE FEED"
        title="Portfolio Price Monitoring"
        description={`${companies.length} monitored companies · Click any company row for detailed target configuration, price history, and Excel cell lineage.`}
        action={
          <div className="heading-actions">
            <button
              className="button secondary"
              onClick={() => void onPollNow()}
              disabled={isPollingNow}
            >
              <Activity size={14} /> {isPollingNow ? "Polling Live Feed…" : "Poll Live Quotes Now"}
            </button>
            <button
              className="button primary"
              onClick={onRefresh}
              disabled={requestStatus === "loading"}
            >
              <RefreshCw size={14} /> Refresh Table
            </button>
          </div>
        }
      />

      <section className="card portfolio-card">
        <div className="table-toolbar">
          <div className="table-toolbar-title">
            <div className="table-icon">
              <BriefcaseBusiness size={15} />
            </div>
            <div>
              <b>Monitored Companies</b>
              <small>
                {requestStatus === "ready"
                  ? `${filtered.length} of ${companies.length} companies · Sorted by ${sort}`
                  : "Loading from backend"}
              </small>
            </div>
          </div>
          <div className="table-filters">
            <label className="filter-search">
              <Search size={14} />
              <input
                value={search}
                onChange={(e) => onSearchChange(e.target.value)}
                placeholder="Search company or symbol..."
              />
            </label>
            <select
              aria-label="Exchange filter"
              value={exchange}
              onChange={(e) => setExchange(e.target.value)}
            >
              <option>All exchanges</option>
              <option>NSE</option>
              <option>BSE</option>
            </select>
            <select
              aria-label="Alert status filter"
              value={alertFilter}
              onChange={(e) => setAlertFilter(e.target.value)}
            >
              <option>All statuses</option>
              <option>Triggered</option>
              <option>Base Triggered</option>
              <option>Bull Triggered</option>
              <option>Monitoring</option>
              <option>Pending Config</option>
              <option>Stale Quote</option>
            </select>
            <select
              aria-label="Sort by"
              value={sort}
              onChange={(e) => setSort(e.target.value)}
            >
              <option value="nearest">Sort: Nearest Target</option>
              <option value="name">Sort: Company Name</option>
              <option value="price">Sort: Latest Price</option>
              <option value="base">Sort: Base Target</option>
              <option value="bull">Sort: Bull Target</option>
            </select>
          </div>
        </div>

        <div className="table-scroll">
          {requestStatus === "loading" ? (
            <div className="empty-state" role="status">
              <RefreshCw size={22} />
              <b>Loading portfolio</b>
              <span>Connecting to the backend API…</span>
            </div>
          ) : requestStatus === "error" ? (
            <div className="empty-state" role="alert">
              <CircleHelp size={22} />
              <b>Could not load portfolio</b>
              <span>{error}</span>
              <button className="button secondary" onClick={onRefresh}>
                Retry
              </button>
            </div>
          ) : (
            <MonitoringTable
              companies={filtered}
              onSelectCompany={onSelectCompany}
              onSort={chooseSort}
            />
          )}
        </div>

        <div className="table-footer">
          <span>
            Showing <b>{filtered.length}</b> of <b>{companies.length}</b> companies · Saved Excel CMP is displayed for reference only and never treated as live market price.
          </span>
        </div>
      </section>
    </>
  );
}

function AlertsPage({
  apiAlerts,
  apiStatus,
  apiError,
  workerStatus,
  onRefresh,
  onAction,
}: {
  apiAlerts: ApiAlert[];
  apiStatus: "loading" | "ready" | "error";
  apiError: string;
  workerStatus: WorkerStatus | null;
  onRefresh: () => void;
  onAction: (id: number, action: "acknowledge" | "resolve" | "reset") => Promise<void>;
}) {
  const [targetFilter, setTargetFilter] = useState("All targets");
  const [lifecycleFilter, setLifecycleFilter] = useState("All states");
  const [query, setQuery] = useState("");

  const filtered = apiAlerts.filter((a) => {
    const matchesTarget =
      targetFilter === "All targets" ||
      (targetFilter === "Base" && a.target_kind === "base") ||
      (targetFilter === "Bull" && a.target_kind === "bull");
    const st = a.lifecycle_status ?? "triggered";
    const matchesLifecycle =
      lifecycleFilter === "All states" || st === lifecycleFilter.toLowerCase();
    const matchesQuery =
      !query ||
      `${a.company} ${a.symbol}`.toLowerCase().includes(query.toLowerCase());
    return matchesTarget && matchesLifecycle && matchesQuery;
  });

  return (
    <>
      <PageHeading
        eyebrow="ALERT ENGINE & NOTIFICATION OUTBOX"
        title="Price Target Alerts"
        description="Triggered Base and Bull target events with exact trigger prices, directions, delivery tracking, and re-arm controls."
        action={
          <button
            className="button secondary"
            onClick={onRefresh}
            disabled={apiStatus === "loading"}
          >
            <RefreshCw size={14} /> Refresh Alerts
          </button>
        }
      />

      <div className="alert-summary-row">
        <div className="alert-summary">
          <span className="summary-dot red" />
          <div>
            <b>
              {
                apiAlerts.filter(
                  (a) => (a.lifecycle_status ?? "triggered") === "triggered",
                ).length
              }
            </b>
            <small>Active Triggered</small>
          </div>
        </div>
        <div className="alert-summary">
          <span className="summary-dot blue" />
          <div>
            <b>
              {
                apiAlerts.filter((a) => a.lifecycle_status === "acknowledged")
                  .length
              }
            </b>
            <small>Acknowledged</small>
          </div>
        </div>
        <div className="alert-summary">
          <span className="summary-dot green" />
          <div>
            <b>
              {apiAlerts.filter((a) => a.lifecycle_status === "resolved").length}
            </b>
            <small>Resolved / Reset</small>
          </div>
        </div>
        <div className="alert-summary">
          <span className="summary-dot amber" />
          <div>
            <b>{apiAlerts.filter((a) => a.delivery_status === "sent").length}</b>
            <small>Notifications Delivered</small>
          </div>
        </div>
      </div>

      <section className="card alerts-card api-alerts-card">
        <div className="table-toolbar">
          <div className="table-toolbar-title">
            <div className="table-icon amber-icon">
              <Bell size={15} />
            </div>
            <div>
              <b>Persisted Target Alert Events</b>
              <small>
                Worker: {workerStatus?.state ?? "ready"} · Notification mode:{" "}
                {workerStatus?.notification_mode ?? "live"}
              </small>
            </div>
          </div>
          <div className="table-filters">
            <label className="filter-search">
              <Search size={14} />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search company or symbol..."
              />
            </label>
            <select
              value={targetFilter}
              onChange={(e) => setTargetFilter(e.target.value)}
            >
              <option>All targets</option>
              <option>Base</option>
              <option>Bull</option>
            </select>
            <select
              value={lifecycleFilter}
              onChange={(e) => setLifecycleFilter(e.target.value)}
            >
              <option>All states</option>
              <option>Triggered</option>
              <option>Acknowledged</option>
              <option>Resolved</option>
            </select>
          </div>
        </div>

        {apiStatus === "loading" ? (
          <div className="empty-state" role="status">
            <RefreshCw size={20} />
            <b>Loading persisted alerts…</b>
          </div>
        ) : apiStatus === "error" ? (
          <div className="empty-state" role="alert">
            <CircleHelp size={20} />
            <b>Backend events unavailable</b>
            <span>{apiError}</span>
          </div>
        ) : (
          <div className="table-scroll">
            <table className="alerts-table">
              <thead>
                <tr>
                  <th>Alert Type</th>
                  <th>Company</th>
                  <th>Trigger Price / Target</th>
                  <th>Direction</th>
                  <th>Trigger Time</th>
                  <th>Notification Delivery</th>
                  <th>Lifecycle Status</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((alert) => {
                  const lifecycle = alert.lifecycle_status ?? "triggered";
                  return (
                    <tr key={alert.id}>
                      <td>
                        <b>
                          {alert.target_kind === "base"
                            ? "Base Target Alert"
                            : "Bull Target Alert"}
                        </b>
                        <small className="cell-sub">
                          {alert.is_simulated ? "SIMULATED" : "LIVE MARKET"}
                        </small>
                      </td>
                      <td>
                        <b>{alert.company}</b>
                        <small className="cell-sub">
                          {alert.exchange}:{alert.symbol}
                        </small>
                      </td>
                      <td>
                        <b>{money(alert.price)}</b> /{" "}
                        {alert.target == null ? "—" : money(alert.target)}
                      </td>
                      <td>
                        <span className="direction-pill">
                          {alert.price_direction ??
                            (alert.comparison_direction === "below"
                              ? "Downward (≤)"
                              : "Upward (≥)")}
                        </span>
                      </td>
                      <td className="updated-cell">
                        {new Date(alert.triggered_at).toLocaleString("en-IN")}
                      </td>
                      <td>
                        <span
                          className={`email-status ${
                            alert.delivery_status === "failed"
                              ? "disabled"
                              : alert.delivery_status === "sent"
                                ? "delivered"
                                : "queued"
                          }`}
                        >
                          <i />
                          {alert.delivery_label} ({alert.attempts}/{alert.max_attempts})
                        </span>
                        {alert.last_error && (
                          <small className="worker-error">{alert.last_error}</small>
                        )}
                      </td>
                      <td>
                        <span className={`alert-state state-${lifecycle}`}>
                          {lifecycle.toUpperCase()}
                        </span>
                      </td>
                      <td>
                        <div className="row-actions">
                          {lifecycle === "triggered" && (
                            <button
                              className="button secondary small"
                              onClick={() => void onAction(alert.id, "acknowledge")}
                            >
                              Acknowledge
                            </button>
                          )}
                          {lifecycle !== "resolved" && (
                            <button
                              className="button secondary small"
                              onClick={() => void onAction(alert.id, "resolve")}
                            >
                              Resolve
                            </button>
                          )}
                          <button
                            className="button secondary small"
                            title="Reset & re-arm target alert state"
                            onClick={() => void onAction(alert.id, "reset")}
                          >
                            Reset & Re-arm
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {filtered.length === 0 && (
              <div className="empty-state">
                <Bell size={22} />
                <b>No alerts match your filters</b>
                <span>Triggered Base and Bull alerts will appear here automatically.</span>
              </div>
            )}
          </div>
        )}
      </section>
    </>
  );
}

function TargetManagement({
  companies,
  requestStatus,
  error,
  onRefresh,
  onImport,
  onEdit,
  onSelectCompany,
  search,
}: {
  companies: PortfolioCompany[];
  requestStatus: "loading" | "ready" | "error";
  error: string;
  onRefresh: () => void;
  onImport: () => void;
  onEdit: (company: PortfolioCompany) => void;
  onSelectCompany: (company: PortfolioCompany) => void;
  search: string;
}) {
  const list = companies.filter((company) =>
    `${company.name} ${company.symbol}`.toLowerCase().includes(search.toLowerCase()),
  );
  return (
    <>
      <PageHeading
        eyebrow="CONFIGURABLE TARGET DIRECTIONS & RE-ARM POLICIES"
        title="Target Management"
        description="Configure Base and Bull target prices, comparison directions (above/below/pending), trigger conditions, and re-trigger policies."
        action={
          <div className="heading-actions">
            <button className="button secondary" onClick={onRefresh}>
              <RefreshCw size={14} /> Refresh
            </button>
            <button className="button primary" onClick={onImport}>
              <Upload size={14} /> Import Excel Workbook
            </button>
          </div>
        }
      />

      <section className="card portfolio-card target-table-card">
        <div className="table-scroll">
          {requestStatus === "loading" ? (
            <div className="empty-state">
              <RefreshCw size={22} />
              <b>Loading targets…</b>
            </div>
          ) : requestStatus === "error" ? (
            <div className="empty-state">
              <CircleHelp size={22} />
              <b>Could not load targets</b>
              <span>{error}</span>
            </div>
          ) : (
            <table className="portfolio-table target-table">
              <thead>
                <tr>
                  <th>Company</th>
                  <th>Exchange</th>
                  <th>Saved CMP (Ref)</th>
                  <th>Latest Live Price</th>
                  <th>Base Target & Rule</th>
                  <th>Bull Target & Rule</th>
                  <th>Re-trigger Policy</th>
                  <th>Version</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {list.map((company) => (
                  <tr key={`${company.exchange}:${company.symbol}`}>
                    <td
                      style={{ cursor: "pointer" }}
                      onClick={() => onSelectCompany(company)}
                    >
                      <div className="company-name-cell">
                        <div className="company-avatar">{company.symbol[0]}</div>
                        <span>
                          <b>{company.name}</b>
                          <small>{company.symbol}</small>
                        </span>
                      </div>
                    </td>
                    <td>{company.exchange}</td>
                    <td>{company.cmp == null ? "—" : money(company.cmp)}</td>
                    <td>
                      {company.latest_quote
                        ? money(company.latest_quote.price)
                        : "No quote"}
                    </td>
                    <td>
                      <b>
                        {company.base_target == null
                          ? "Not set"
                          : money(company.base_target)}
                      </b>
                      <small className="cell-sub">
                        Direction: {company.base_direction ?? "below"} ·{" "}
                        {company.base_trigger_mode ?? "at_or_beyond"}
                      </small>
                    </td>
                    <td>
                      <b>
                        {company.bull_target == null
                          ? "Not set"
                          : money(company.bull_target)}
                      </b>
                      <small className="cell-sub">
                        Direction: {company.bull_direction ?? "above"} ·{" "}
                        {company.bull_trigger_mode ?? "at_or_beyond"}
                      </small>
                    </td>
                    <td>
                      <span className="direction-pill">
                        {company.rearm_policy ?? "once_per_revision"}
                      </span>
                    </td>
                    <td>v{company.version}</td>
                    <td>
                      <button
                        className="button tiny secondary"
                        onClick={() => onEdit(company)}
                      >
                        Configure <ArrowRight size={12} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>
    </>
  );
}

function MarketActivityPage({
  companies,
  workerStatus,
  onPollNow,
  isPollingNow,
  onSelectCompany,
}: {
  companies: PortfolioCompany[];
  workerStatus: WorkerStatus | null;
  onPollNow: () => Promise<void>;
  isPollingNow: boolean;
  onSelectCompany: (c: PortfolioCompany) => void;
}) {
  const staleList = companies.filter(
    (c) =>
      c.latest_quote != null &&
      !c.latest_quote.is_fresh &&
      !c.latest_quote.is_simulated,
  );
  const missingQuoteList = companies.filter((c) => c.latest_quote == null);

  return (
    <>
      <PageHeading
        eyebrow="INDEPENDENT BACKGROUND MONITORING SERVICE"
        title="Market Feed Health & Reliability"
        description="Real-time connection telemetry, last quote timestamps, stale-price detection, and NSE/BSE polling status."
        action={
          <button
            className="button primary"
            onClick={() => void onPollNow()}
            disabled={isPollingNow}
          >
            <RefreshCw size={14} />{" "}
            {isPollingNow ? "Polling Market Feed…" : "Poll Live Quotes Now"}
          </button>
        }
      />

      <div className="market-stat-grid">
        <div className="card market-stat">
          <small>Feed Provider</small>
          <b>{workerStatus?.provider ?? "yfinance"}</b>
          <small className="market-uptime">
            <i className="status-dot" /> Exchange Calendar:{" "}
            {workerStatus?.exchange_calendar ?? "NSE/BSE IST"}
          </small>
        </div>
        <div className="card market-stat">
          <small>WebSocket / Polling Mode</small>
          <b>{workerStatus?.websocket_status ?? "Polling Fallback"}</b>
          <small className="market-uptime">
            Last poll:{" "}
            {workerStatus?.last_poll_at
              ? new Date(workerStatus.last_poll_at).toLocaleTimeString("en-IN")
              : "On-demand ready"}
          </small>
        </div>
        <div className="card market-stat">
          <small>NSE Symbols Monitored</small>
          <b>
            {companies.filter((c) => c.exchange === "NSE").length}{" "}
            <span className="market-stat-sub">companies</span>
          </b>
          <small className="market-uptime">
            {
              companies.filter(
                (c) => c.exchange === "NSE" && c.latest_quote?.is_fresh,
              ).length
            }{" "}
            fresh quotes
          </small>
        </div>
        <div className="card market-stat">
          <small>BSE Symbols Monitored</small>
          <b>
            {companies.filter((c) => c.exchange === "BSE").length}{" "}
            <span className="market-stat-sub">companies</span>
          </b>
          <small className="market-uptime">
            {staleList.length} stale · {workerStatus?.last_poll_failed ?? 0} failed
          </small>
        </div>
      </div>

      <div className="market-content-grid">
        <section className="card market-movers">
          <div className="section-head">
            <div>
              <h2>Stale or Missing Quote Watchlist</h2>
              <p>Symbols requiring fresh quote verification</p>
            </div>
          </div>
          {staleList.length === 0 && missingQuoteList.length === 0 ? (
            <p className="empty-copy">
              All monitored symbols have fresh validated market quotes.
            </p>
          ) : (
            <div className="movers-list">
              {[...staleList, ...missingQuoteList].slice(0, 15).map((c) => (
                <button
                  className="mover-row"
                  key={`${c.exchange}:${c.symbol}`}
                  onClick={() => onSelectCompany(c)}
                >
                  <div className="mover-company">
                    <b>
                      {c.symbol} ({c.exchange})
                    </b>
                    <small>{c.name}</small>
                  </div>
                  <span className="status-badge badge-sell">
                    <i />
                    {c.latest_quote ? "STALE" : "NO QUOTE"}
                  </span>
                </button>
              ))}
            </div>
          )}
        </section>

        <section className="card market-near">
          <div className="section-head">
            <div>
              <h2>Closest to Base or Bull Target</h2>
              <p>Ranked by percentage distance to nearest configured target</p>
            </div>
            <Target size={16} className="muted-icon" />
          </div>
          {[...companies]
            .filter((c) => c.nearest_target_pct != null)
            .sort((a, b) => (a.nearest_target_pct ?? 999) - (b.nearest_target_pct ?? 999))
            .slice(0, 10)
            .map((c) => (
              <button
                className="near-row"
                key={`${c.exchange}:${c.symbol}`}
                onClick={() => onSelectCompany(c)}
              >
                <div>
                  <b>
                    {c.symbol} ({c.exchange})
                  </b>
                  <small>{c.name}</small>
                </div>
                <div>
                  <b>{c.nearest_target_pct?.toFixed(2)}% away</b>
                </div>
              </button>
            ))}
        </section>
      </div>
    </>
  );
}

function ReportsPage({
  companies,
  alerts,
  notify,
  onSelectCompany,
}: {
  companies: PortfolioCompany[];
  alerts: ApiAlert[];
  notify: (msg: string) => void;
  onSelectCompany: (c: PortfolioCompany) => void;
}) {
  const [exchange, setExchange] = useState("All exchanges");
  const rows = companies.filter(
    (c) => exchange === "All exchanges" || c.exchange === exchange,
  );

  const exportCsv = () => {
    const header = [
      "Company",
      "Symbol",
      "Exchange",
      "Saved CMP (Ref)",
      "Latest Live Price",
      "Base Target",
      "Base Direction",
      "Distance to Base (%)",
      "Bull Target",
      "Bull Direction",
      "Distance to Bull (%)",
      "Alert Status",
      "Last Quote Time",
    ];
    const body = rows.map((c) => [
      c.name,
      c.symbol,
      c.exchange,
      c.cmp ?? "",
      c.latest_quote?.price ?? "",
      c.base_target ?? "",
      c.base_direction ?? "below",
      c.distance_to_base_pct ?? "",
      c.bull_target ?? "",
      c.bull_direction ?? "above",
      c.distance_to_bull_pct ?? "",
      c.alert_status ?? "monitoring",
      c.latest_quote?.quoted_at ?? "",
    ]);
    const csv = [header, ...body]
      .map((line) =>
        line.map((cell) => `"${String(cell).replace(/"/g, '""')}"`).join(","),
      )
      .join("\n");
    const url = URL.createObjectURL(
      new Blob([csv], { type: "text/csv;charset=utf-8" }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = "northstar-monitoring-report.csv";
    link.click();
    URL.revokeObjectURL(url);
    notify("Live monitoring CSV report exported");
  };

  return (
    <>
      <PageHeading
        eyebrow="PORTFOLIO MONITORING EXPORTS"
        title="Monitoring Reports"
        description="Export live price distances, target configurations, and triggered alert summaries."
        action={
          <button className="button primary" onClick={exportCsv}>
            <Download size={14} /> Export CSV Report
          </button>
        }
      />

      <div className="report-summary-grid">
        <div className="card report-summary">
          <small>Monitored Companies</small>
          <b>{rows.length}</b>
          <span>NSE & BSE</span>
        </div>
        <div className="card report-summary">
          <small>Base Target Alerts</small>
          <b>{alerts.filter((a) => a.target_kind === "base").length}</b>
          <span>Persisted events</span>
        </div>
        <div className="card report-summary">
          <small>Bull Target Alerts</small>
          <b>{alerts.filter((a) => a.target_kind === "bull").length}</b>
          <span>Persisted events</span>
        </div>
        <div className="card report-summary">
          <small>Filter Exchange</small>
          <select
            value={exchange}
            onChange={(e) => setExchange(e.target.value)}
            style={{ marginTop: 6 }}
          >
            <option>All exchanges</option>
            <option>NSE</option>
            <option>BSE</option>
          </select>
        </div>
      </div>

      <section className="card portfolio-card report-table-card">
        <div className="table-scroll">
          <MonitoringTable companies={rows} onSelectCompany={onSelectCompany} />
        </div>
      </section>
    </>
  );
}

function SettingsPage({
  dark,
  setDark,
  compactTables,
  setCompactTables,
  workerStatus,
  notify,
}: {
  dark: boolean;
  setDark: (v: boolean) => void;
  compactTables: boolean;
  setCompactTables: (v: boolean) => void;
  workerStatus: WorkerStatus | null;
  notify: (message: string) => void;
}) {
  const [prefs, setPrefs] = useState<NotificationPreference>({
    dashboard_enabled: true,
    email_enabled: false,
    email_recipients: "alerts@plutuswealth.com",
    notify_on_base: true,
    notify_on_bull: true,
    notify_on_stale: true,
    updated_at: null,
  });
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    void fetchNotificationPreferences()
      .then((data) => {
        const recipientsStr = Array.isArray(data.email_recipients)
          ? data.email_recipients.join(", ")
          : String(data.email_recipients || "");
        setPrefs({ ...data, email_recipients: recipientsStr });
      })
      .catch(() => null);
  }, []);

  const toggle = (value: boolean, onChange: (v: boolean) => void) => (
    <button
      className={`switch ${value ? "switch-on" : ""}`}
      role="switch"
      aria-checked={value}
      onClick={() => onChange(!value)}
    >
      <i />
    </button>
  );

  const row = (title: string, detail: string, control: ReactNode) => (
    <div className="setting-row">
      <div>
        <b>{title}</b>
        <small>{detail}</small>
      </div>
      {control}
    </div>
  );

  const save = async () => {
    setSaving(true);
    try {
      const updated = await updateNotificationPreferences(prefs);
      const recipientsStr = Array.isArray(updated.email_recipients)
        ? updated.email_recipients.join(", ")
        : String(updated.email_recipients || "");
      setPrefs({ ...updated, email_recipients: recipientsStr });
      notify("Notification preferences saved to database");
    } catch (err) {
      notify(err instanceof Error ? err.message : "Could not save preferences");
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <PageHeading
        eyebrow="SYSTEM CONFIGURATION"
        title="Notification & Display Preferences"
        description="Configure dashboard and email alert delivery channels, recipients, and table density."
      />
      <div className="settings-layout">
        <div className="settings-main">
          <section className="card settings-card">
            <div className="settings-section-title">
              <div className="settings-section-icon amber">
                <Bell size={16} />
              </div>
              <div>
                <h2>Alert Notification Channels</h2>
                <p>Persisted in the backend notification outbox with automatic retry backoff.</p>
              </div>
            </div>
            {row(
              "Dashboard notifications",
              "Deliver instant alerts to the dashboard notification center.",
              toggle(prefs.dashboard_enabled, (v) =>
                setPrefs({ ...prefs, dashboard_enabled: v }),
              ),
            )}
            {row(
              "Email notifications",
              "Send formatted price target alert emails via SMTP / spool.",
              toggle(prefs.email_enabled, (v) =>
                setPrefs({ ...prefs, email_enabled: v }),
              ),
            )}
            {row(
              "Approved Email Recipients",
              "Comma-separated list of recipient email addresses.",
              <input
                className="inline-input"
                style={{ minWidth: 260 }}
                value={prefs.email_recipients}
                onChange={(e) =>
                  setPrefs({ ...prefs, email_recipients: e.target.value })
                }
                placeholder="alerts@plutuswealth.com"
              />,
            )}
            {row(
              "Notify on Base Target",
              "Queue alerts when a stock reaches its configured Base Target.",
              toggle(prefs.notify_on_base, (v) =>
                setPrefs({ ...prefs, notify_on_base: v }),
              ),
            )}
            {row(
              "Notify on Bull Target",
              "Queue alerts when a stock reaches its configured Bull Target.",
              toggle(prefs.notify_on_bull, (v) =>
                setPrefs({ ...prefs, notify_on_bull: v }),
              ),
            )}
            {row(
              "Notify on Stale Quotes",
              "Flag warnings when a monitored stock's quote becomes stale.",
              toggle(prefs.notify_on_stale, (v) =>
                setPrefs({ ...prefs, notify_on_stale: v }),
              ),
            )}
          </section>

          <section className="card settings-card">
            <div className="settings-section-title">
              <div className="settings-section-icon">
                <Sun size={16} />
              </div>
              <div>
                <h2>Appearance & Worker Telemetry</h2>
                <p>Customize visual layout and inspect worker status.</p>
              </div>
            </div>
            {row(
              "Color theme",
              "Choose Light or Dark appearance.",
              <div className="theme-options">
                <button
                  className={!dark ? "theme-option selected" : "theme-option"}
                  onClick={() => setDark(false)}
                >
                  <Sun size={14} /> Light
                </button>
                <button
                  className={dark ? "theme-option selected" : "theme-option"}
                  onClick={() => setDark(true)}
                >
                  <Moon size={14} /> Dark
                </button>
              </div>,
            )}
            {row(
              "Compact data tables",
              "Information-dense rows for monitoring 40+ companies.",
              toggle(compactTables, setCompactTables),
            )}
            {row(
              "Background Monitoring Service",
              `Provider: ${workerStatus?.provider ?? "yfinance"} · Calendar: ${
                workerStatus?.exchange_calendar ?? "nse_bse"
              }`,
              <span className="local-only-tag">
                {workerStatus?.state?.toUpperCase() ?? "READY"}
              </span>,
            )}
          </section>

          <button
            className="button primary settings-save"
            onClick={() => void save()}
            disabled={saving}
          >
            {saving ? "Saving…" : "Save Notification Preferences"}
          </button>
        </div>
      </div>
    </>
  );
}

function NotificationDrawer({
  alerts,
  onClose,
  onAction,
}: {
  alerts: ApiAlert[];
  onClose: () => void;
  onAction: (id: number, action: "acknowledge" | "resolve" | "reset") => Promise<void>;
}) {
  const [unreadOnly, setUnreadOnly] = useState(false);
  const unreadAlerts = alerts.filter(
    (a) => (a.lifecycle_status ?? "triggered") === "triggered",
  );
  const visibleAlerts = unreadOnly ? unreadAlerts : alerts;

  return (
    <>
      <motion.button
        className="drawer-backdrop"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        onClick={onClose}
        aria-label="Close notifications"
      />
      <motion.aside
        className="notification-drawer"
        initial={{ x: 420 }}
        animate={{ x: 0 }}
        exit={{ x: 420 }}
        transition={{ type: "spring", damping: 28, stiffness: 260 }}
      >
        <div className="drawer-head">
          <div>
            <h2>Target Alert Notifications</h2>
            <p>{unreadAlerts.length} active triggered alerts</p>
          </div>
          <button
            className="icon-button"
            onClick={onClose}
            aria-label="Close drawer"
          >
            <X size={18} />
          </button>
        </div>
        <div className="drawer-tabs">
          <button
            className={!unreadOnly ? "drawer-tab-active" : ""}
            onClick={() => setUnreadOnly(false)}
          >
            All ({alerts.length})
          </button>
          <button
            className={unreadOnly ? "drawer-tab-active" : ""}
            onClick={() => setUnreadOnly(true)}
          >
            Active <span>{unreadAlerts.length}</span>
          </button>
          <button
            className="mark-all"
            onClick={() =>
              unreadAlerts.forEach((a) => void onAction(a.id, "acknowledge"))
            }
          >
            <CheckCheck size={13} /> Acknowledge all
          </button>
        </div>
        <div className="drawer-content">
          {visibleAlerts.length ? (
            visibleAlerts.map((a) => {
              const isNew = (a.lifecycle_status ?? "triggered") === "triggered";
              return (
                <div
                  className={`drawer-alert ${isNew ? "drawer-alert-new" : ""}`}
                  key={a.id}
                >
                  <div
                    className={`drawer-alert-icon ${
                      a.target_kind === "bull" ? "alert-red" : "alert-green"
                    }`}
                  >
                    <Zap size={14} />
                  </div>
                  <div className="drawer-alert-content">
                    <div className="drawer-alert-title">
                      <b>
                        Price Target Alert – {a.symbol} ({a.target_kind.toUpperCase()})
                      </b>
                      {isNew && <i />}
                    </div>
                    <p>
                      <strong>{a.company}</strong> ({a.exchange})
                    </p>
                    <div className="drawer-alert-meta">
                      <span>Current: {money(a.price)}</span>
                      <span>
                        Target: {a.target == null ? "—" : money(a.target)}
                      </span>
                    </div>
                    <div className="drawer-alert-time">
                      {new Date(a.triggered_at).toLocaleString("en-IN")} ·{" "}
                      {a.delivery_label}
                      {isNew && (
                        <button onClick={() => void onAction(a.id, "acknowledge")}>
                          Acknowledge
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              );
            })
          ) : (
            <div className="empty-state">
              <CheckCheck size={22} />
              <b>No active alerts</b>
              <span>All target alerts have been reviewed.</span>
            </div>
          )}
        </div>
      </motion.aside>
    </>
  );
}

function DetailPanel({
  company,
  onClose,
  onEditTargets,
  onAction,
}: {
  company: PortfolioCompany;
  onClose: () => void;
  onEditTargets: () => void;
  onAction: (id: number, action: "acknowledge" | "resolve" | "reset") => Promise<void>;
}) {
  const [details, setDetails] = useState<CompanyDetailsResponse | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    fetchCompanyDetails(company.exchange, company.symbol, controller.signal)
      .then((res) => {
        setDetails(res);
        setLoading(false);
      })
      .catch(() => setLoading(false));
    return () => controller.abort();
  }, [company.exchange, company.symbol, company.version]);

  const quote = company.latest_quote;
  const chartSeries = useMemo(() => {
    const hist = details?.price_history ?? [];
    if (hist.length > 0) {
      return [...hist]
        .reverse()
        .map((q) => ({
          label: new Date(q.quoted_at).toLocaleTimeString("en-IN", {
            hour: "2-digit",
            minute: "2-digit",
          }),
          price: q.price,
        }));
    }
    if (quote) {
      return [{ label: "Latest", price: quote.price }];
    }
    return [];
  }, [details, quote]);

  const cellRefs = (company.source_metadata?.cell_refs ?? {}) as Record<string, string>;

  return (
    <>
      <motion.button
        className="drawer-backdrop"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        onClick={onClose}
        aria-label="Close stock detail"
      />
      <motion.aside
        className="detail-panel"
        initial={{ x: 560 }}
        animate={{ x: 0 }}
        exit={{ x: 560 }}
        transition={{ type: "spring", damping: 28, stiffness: 260 }}
      >
        <div className="detail-top">
          <button className="back-link" onClick={onClose}>
            <ArrowLeft size={15} /> Back to monitoring table
          </button>
          <div className="row-actions">
            <button className="button secondary small" onClick={onEditTargets}>
              Configure Targets
            </button>
            <button
              className="icon-button"
              onClick={onClose}
              aria-label="Close detail"
            >
              <X size={18} />
            </button>
          </div>
        </div>

        <div className="detail-company-head">
          <div className="detail-company-logo">{company.symbol.slice(0, 2)}</div>
          <div>
            <div className="detail-company-name">{company.name}</div>
            <div className="detail-symbol">
              {company.symbol} <span>·</span> {company.exchange} <span>·</span>{" "}
              {company.sector ?? "Equity"} <span>·</span> v{company.version}
            </div>
          </div>
          {alertStatusBadge(company)}
        </div>

        <div className="detail-price-block">
          <b>{quote ? money(quote.price) : "No Live Quote"}</b>
          <small>
            {quote
              ? `Provider: ${quote.provider} · ${
                  quote.is_fresh ? "Fresh" : "STALE"
                } · Quoted ${new Date(quote.quoted_at).toLocaleString("en-IN")}`
              : "Poll live quotes to record current market price"}{" "}
            · Saved Reference CMP: {company.cmp == null ? "—" : money(company.cmp)}
          </small>
        </div>

        <div className="detail-target-grid">
          <div className="detail-target buy-target-detail">
            <span>
              BASE TARGET ({company.base_direction === "below" ? "≤ BELOW" : company.base_direction === "above" ? "≥ ABOVE" : "PENDING"})
            </span>
            <b>
              {company.base_target == null ? "Not Set" : money(company.base_target)}
            </b>
            <small>
              Distance:{" "}
              {formatDistance(
                company.distance_to_base_abs,
                company.distance_to_base_pct,
              )}
              {cellRefs.base_target ? ` · Excel Cell ${cellRefs.base_target}` : ""}
            </small>
          </div>
          <div className="detail-target sell-target-detail">
            <span>
              BULL TARGET ({company.bull_direction === "above" ? "≥ ABOVE" : company.bull_direction === "below" ? "≤ BELOW" : "PENDING"})
            </span>
            <b>
              {company.bull_target == null ? "Not Set" : money(company.bull_target)}
            </b>
            <small>
              Distance:{" "}
              {formatDistance(
                company.distance_to_bull_abs,
                company.distance_to_bull_pct,
              )}
              {cellRefs.bull_target ? ` · Excel Cell ${cellRefs.bull_target}` : ""}
            </small>
          </div>
        </div>

        <section className="detail-section">
          <div className="section-head">
            <div>
              <h2>Target Configuration & Excel Lineage</h2>
              <p>Active comparison rules & cell coordinates</p>
            </div>
          </div>
          <div className="movement-grid">
            <div>
              <small>BASE CONDITION</small>
              <b>
                {company.base_direction ?? "below"} (
                {company.base_trigger_mode ?? "at_or_beyond"})
              </b>
            </div>
            <div>
              <small>BULL CONDITION</small>
              <b>
                {company.bull_direction ?? "above"} (
                {company.bull_trigger_mode ?? "at_or_beyond"})
              </b>
            </div>
            <div>
              <small>RE-ARM POLICY</small>
              <b>{company.rearm_policy ?? "once_per_revision"}</b>
            </div>
            <div>
              <small>EXCEL CELLS</small>
              <b>
                {Object.keys(cellRefs).length > 0
                  ? Object.entries(cellRefs)
                      .map(([k, v]) => `${k}: ${v}`)
                      .join(", ")
                  : "Manual / Direct"}
              </b>
            </div>
          </div>
        </section>

        {chartSeries.length > 0 && (
          <section className="detail-section">
            <div className="section-head">
              <div>
                <h2>Recorded Price Snapshots</h2>
                <p>{chartSeries.length} recorded quote snapshot(s)</p>
              </div>
            </div>
            <div className="detail-chart" style={{ height: 180 }}>
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart
                  data={chartSeries}
                  margin={{ top: 8, right: 8, left: -18, bottom: 0 }}
                >
                  <CartesianGrid
                    strokeDasharray="3 5"
                    vertical={false}
                    stroke="var(--chart-grid)"
                  />
                  <XAxis
                    dataKey="label"
                    tickLine={false}
                    axisLine={false}
                    tick={{ fill: "var(--muted)", fontSize: 10 }}
                  />
                  <YAxis
                    orientation="right"
                    tickLine={false}
                    axisLine={false}
                    tick={{ fill: "var(--muted)", fontSize: 10 }}
                    domain={["auto", "auto"]}
                  />
                  <Tooltip
                    formatter={(v) => [money(Number(v)), "Quote Price"]}
                  />
                  {company.base_target != null && (
                    <ReferenceLine
                      y={company.base_target}
                      stroke="#44c49a"
                      strokeDasharray="5 4"
                    />
                  )}
                  {company.bull_target != null && (
                    <ReferenceLine
                      y={company.bull_target}
                      stroke="#e97880"
                      strokeDasharray="5 4"
                    />
                  )}
                  <Area
                    type="monotone"
                    dataKey="price"
                    stroke="#70a9ff"
                    strokeWidth={2}
                    fillOpacity={0.15}
                    fill="#70a9ff"
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </section>
        )}

        <section className="detail-section">
          <div className="section-head">
            <div>
              <h2>Alert History</h2>
              <p>Persisted target triggers for {company.symbol}</p>
            </div>
          </div>
          {loading ? (
            <div className="detail-empty">Loading company alert history…</div>
          ) : details && details.alerts.length > 0 ? (
            details.alerts.map((a) => (
              <div className="detail-alert" key={a.id}>
                <span
                  className={`alert-type-icon ${
                    a.target_kind === "bull" ? "alert-red" : "alert-green"
                  }`}
                >
                  <Zap size={12} />
                </span>
                <div>
                  <b>
                    {a.target_kind.toUpperCase()} Target Reached at {money(a.price)}
                  </b>
                  <small>
                    {new Date(a.triggered_at).toLocaleString("en-IN")} ·{" "}
                    {a.lifecycle_status ?? "triggered"}
                  </small>
                </div>
                <button
                  className="button secondary small"
                  onClick={() => void onAction(a.id, "reset")}
                >
                  Reset
                </button>
              </div>
            ))
          ) : (
            <div className="detail-empty">No alerts triggered for this company yet.</div>
          )}
        </section>

        <section className="detail-section">
          <div className="section-head">
            <div>
              <h2>Target Revision History</h2>
              <p>Audit trail of approved target changes</p>
            </div>
          </div>
          {details && details.revisions.length > 0 ? (
            details.revisions.slice(0, 5).map((rev) => (
              <div className="detail-alert" key={rev.id}>
                <div>
                  <b>
                    v{rev.version} · Base:{" "}
                    {rev.base_target == null ? "—" : money(rev.base_target)} (
                    {rev.base_direction}) · Bull:{" "}
                    {rev.bull_target == null ? "—" : money(rev.bull_target)} (
                    {rev.bull_direction})
                  </b>
                  <small>
                    Source: {rev.source} ·{" "}
                    {rev.changed_at
                      ? new Date(rev.changed_at).toLocaleString("en-IN")
                      : "—"}
                  </small>
                </div>
              </div>
            ))
          ) : (
            <div className="detail-empty">Initial revision active.</div>
          )}
        </section>
      </motion.aside>
    </>
  );
}

function EditTargetModal({
  company,
  onClose,
  onSaved,
}: {
  company: PortfolioCompany;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [baseTarget, setBaseTarget] = useState(
    company.base_target == null ? "" : String(company.base_target),
  );
  const [bullTarget, setBullTarget] = useState(
    company.bull_target == null ? "" : String(company.bull_target),
  );
  const [baseDirection, setBaseDirection] = useState<TargetDirection>(
    company.base_direction ?? "below",
  );
  const [bullDirection, setBullDirection] = useState<TargetDirection>(
    company.bull_direction ?? "above",
  );
  const [baseTriggerMode, setBaseTriggerMode] = useState<TriggerMode>(
    company.base_trigger_mode ?? "at_or_beyond",
  );
  const [bullTriggerMode, setBullTriggerMode] = useState<TriggerMode>(
    company.bull_trigger_mode ?? "at_or_beyond",
  );
  const [rearmPolicy, setRearmPolicy] = useState<RearmPolicy>(
    company.rearm_policy ?? "once_per_revision",
  );
  const [cooldownMinutes, setCooldownMinutes] = useState(
    String(company.cooldown_minutes ?? 60),
  );
  const [monitoringActive, setMonitoringActive] = useState(
    company.monitoring_active !== false,
  );
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const submit = async () => {
    const base = baseTarget.trim() ? Number(baseTarget) : null;
    const bull = bullTarget.trim() ? Number(bullTarget) : null;
    if (
      (base !== null && (!Number.isFinite(base) || base <= 0)) ||
      (bull !== null && (!Number.isFinite(bull) || bull <= 0))
    ) {
      setError("Targets must be positive numbers.");
      return;
    }
    setSaving(true);
    setError("");
    try {
      await updateCompanyTargets(company, {
        base_target: base,
        bull_target: bull,
        base_direction: baseDirection,
        bull_direction: bullDirection,
        base_trigger_mode: baseTriggerMode,
        bull_trigger_mode: bullTriggerMode,
        rearm_policy: rearmPolicy,
        cooldown_minutes: Math.max(1, Number(cooldownMinutes) || 60),
        monitoring_active: monitoringActive,
      });
      onSaved();
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "Could not update target configuration.",
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <motion.button
        className="modal-backdrop"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        onClick={onClose}
        aria-label="Close edit dialog"
      />
      <motion.div
        className="edit-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="edit-title"
        initial={{ opacity: 0, scale: 0.96, y: 12 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.96, y: 12 }}
      >
        <div className="modal-head">
          <div>
            <div className="eyebrow">
              TARGET & ALERT CONFIGURATION · VERSION {company.version}
            </div>
            <h2 id="edit-title">{company.name}</h2>
            <p>
              {company.exchange}:{company.symbol} · Saved Reference CMP:{" "}
              {company.cmp == null ? "—" : money(company.cmp)}
            </p>
          </div>
          <button
            className="icon-button"
            aria-label="Close modal"
            onClick={onClose}
          >
            <X size={17} />
          </button>
        </div>

        <label className="modal-field">
          <span>Base Target Price (INR)</span>
          <div>
            <span>₹</span>
            <input
              type="number"
              value={baseTarget}
              onChange={(event) => setBaseTarget(event.target.value)}
              min="0.01"
              step="0.01"
              placeholder="Leave blank to clear"
            />
          </div>
        </label>

        <div className="mapping-controls" style={{ marginBottom: 12 }}>
          <label className="mapping-field">
            <span>Base Comparison Direction</span>
            <select
              value={baseDirection}
              onChange={(e) => setBaseDirection(e.target.value as TargetDirection)}
            >
              <option value="below">Price falls to or below target (≤)</option>
              <option value="above">Price reaches or exceeds target (≥)</option>
              <option value="pending">Pending configuration</option>
            </select>
          </label>
          <label className="mapping-field">
            <span>Base Trigger Mode</span>
            <select
              value={baseTriggerMode}
              onChange={(e) => setBaseTriggerMode(e.target.value as TriggerMode)}
            >
              <option value="at_or_beyond">At or beyond target (including initial)</option>
              <option value="crosses">Crosses target threshold</option>
            </select>
          </label>
        </div>

        <label className="modal-field">
          <span>Bull Target Price (INR)</span>
          <div>
            <span>₹</span>
            <input
              type="number"
              value={bullTarget}
              onChange={(event) => setBullTarget(event.target.value)}
              min="0.01"
              step="0.01"
              placeholder="Leave blank to clear"
            />
          </div>
        </label>

        <div className="mapping-controls" style={{ marginBottom: 12 }}>
          <label className="mapping-field">
            <span>Bull Comparison Direction</span>
            <select
              value={bullDirection}
              onChange={(e) => setBullDirection(e.target.value as TargetDirection)}
            >
              <option value="above">Price reaches or exceeds target (≥)</option>
              <option value="below">Price falls to or below target (≤)</option>
              <option value="pending">Pending configuration</option>
            </select>
          </label>
          <label className="mapping-field">
            <span>Bull Trigger Mode</span>
            <select
              value={bullTriggerMode}
              onChange={(e) => setBullTriggerMode(e.target.value as TriggerMode)}
            >
              <option value="at_or_beyond">At or beyond target (including initial)</option>
              <option value="crosses">Crosses target threshold</option>
            </select>
          </label>
        </div>

        <div className="mapping-controls" style={{ marginBottom: 12 }}>
          <label className="mapping-field">
            <span>Alert Re-trigger Policy</span>
            <select
              value={rearmPolicy}
              onChange={(e) => setRearmPolicy(e.target.value as RearmPolicy)}
            >
              <option value="once_per_revision">Once per target revision</option>
              <option value="rearm_on_reset">Re-arm after manual Reset/Resolve</option>
              <option value="rearm_after_recovery">Re-arm automatically after price recovery</option>
              <option value="cooldown">Re-arm after cooldown minutes</option>
            </select>
          </label>
          {rearmPolicy === "cooldown" && (
            <label className="mapping-field">
              <span>Cooldown (minutes)</span>
              <input
                type="number"
                min="1"
                value={cooldownMinutes}
                onChange={(e) => setCooldownMinutes(e.target.value)}
              />
            </label>
          )}
        </div>

        <label className="formula-review" style={{ marginBottom: 12 }}>
          <input
            type="checkbox"
            checked={monitoringActive}
            onChange={(e) => setMonitoringActive(e.target.checked)}
          />{" "}
          Active in background price monitoring
        </label>

        {error && (
          <div className="form-error" role="alert">
            {error}
          </div>
        )}

        <div className="modal-note">
          <ShieldCheck size={14} /> Version-checked transaction; creates a new target revision if target prices change.
        </div>

        <div className="modal-actions">
          <button className="button secondary" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button className="button primary" onClick={() => void submit()} disabled={saving}>
            {saving ? "Saving…" : "Save Configuration"}
          </button>
        </div>
      </motion.div>
    </>
  );
}
