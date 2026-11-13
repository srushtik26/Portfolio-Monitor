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
  Command,
  Download,
  FileSpreadsheet,
  LayoutDashboard,
  LineChart,
  Menu,
  MoreHorizontal,
  PanelLeftClose,
  RefreshCw,
  RotateCcw,
  Search,
  Settings,
  ShieldCheck,
  Sun,
  Target,
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
  previewTestAlertEmail,
  resendAlertNotification,
  resetWorkspaceData,
  restoreImportVersion,
  sendSampleAlertEmail,
  triggerOnDemandPoll,
  triggerOutboxDrain,
  updateCompanyTargets,
  updateNotificationPreferences,
} from "./api";
import BackendUploadPage from "./UploadPage";
import { useLiveMarketStream } from "./useLiveMarketStream";
import type {
  AlertEmailPreviewResponse,
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

function isQuoteFreshnessUnknown(quote: PortfolioCompany["latest_quote"]): boolean {
  if (!quote) return false;
  return (
    quote.status === "freshness_unknown" ||
    quote.freshness_unknown === true ||
    quote.quoted_at == null
  );
}

function isAlertActive(alert: ApiAlert): boolean {
  const st = alert.lifecycle_status ?? "active";
  return st === "active" || st === "triggered";
}

function alertStatusBadge(company: PortfolioCompany) {
  const quote = company.latest_quote;
  const freshnessUnknown =
    isQuoteFreshnessUnknown(quote) || company.alert_status === "freshness_unknown";
  if (freshnessUnknown) {
    return (
      <span className="status-badge badge-approaching">
        <i />
        FRESHNESS UNKNOWN
      </span>
    );
  }
  const isStale = quote != null && !quote.is_fresh && !quote.is_simulated;
  if (isStale) {
    return <span className="status-badge badge-approaching"><i />STALE QUOTE</span>;
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
  if (st === "inactive" || st === "paused") {
    return <span className="status-badge badge-neutral"><i />Paused</span>;
  }
  if (company.nearest_target_pct != null && company.nearest_target_pct <= 2.5) {
    return <span className="status-badge badge-approaching"><i />Approaching ({company.nearest_target_pct.toFixed(1)}%)</span>;
  }
  return <span className="status-badge badge-neutral"><i />Monitoring</span>;
}

function playAlertChime(isBull: boolean) {
  try {
    const AudioCtx = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AudioCtx) return;
    const ctx = new AudioCtx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.setValueAtTime(isBull ? 660 : 528, ctx.currentTime);
    osc.frequency.exponentialRampToValueAtTime(isBull ? 880 : 660, ctx.currentTime + 0.16);
    gain.gain.setValueAtTime(0.08, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.28);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.3);
  } catch {
    // ignore audio restrictions
  }
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

  const [collapsed, setCollapsed] = useState(false);
  const [mobileNav, setMobileNav] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [selectedCompany, setSelectedCompany] = useState<PortfolioCompany | null>(null);
  const [editing, setEditing] = useState<PortfolioCompany | null>(null);
  const [emailPreviewItem, setEmailPreviewItem] = useState<ApiAlert | AlertEmailPreviewResponse | null>(null);
  const [soundEnabled, setSoundEnabled] = useState(true);
  const [toast, setToast] = useState("");
  const [query, setQuery] = useState("");
  const [compactTables, setCompactTables] = useState(true);
  const [isPollingNow, setIsPollingNow] = useState(false);

  const notify = (message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(""), 3500);
  };

  const liveStream = useLiveMarketStream({
    setPortfolioCompanies,
    setApiAlerts,
    onOutboxAlert: (outboxNotif) => {
      const targetLabel = outboxNotif.alert.target_kind === "base" ? "Base Target" : "Bull Target";
      if (soundEnabled) {
        playAlertChime(outboxNotif.alert.target_kind === "bull");
      }
      notify(
        `Live Alert: ${outboxNotif.alert.symbol} reached ${targetLabel} (${money(
          outboxNotif.alert.price,
        )})`,
      );
    },
  });

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

  // Periodic background refresh of dashboard view (and automatic yfinance backup poll when companies are present)
  useEffect(() => {
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          if (portfolioCompanies.length > 0 && workerStatus?.provider !== "shoonya") {
            await triggerOnDemandPoll().catch(() => null);
          }
          const [c, a, w] = await Promise.all([
            fetchCompanies(),
            fetchAlerts(),
            fetchWorkerStatus(),
          ]);
          setPortfolioCompanies(c);
          setApiAlerts(a);
          setWorkerStatus(w);
        } catch {
          // ignore background refresh error
        }
      })();
    }, 30000);
    return () => window.clearInterval(timer);
  }, [portfolioCompanies.length, workerStatus?.provider]);

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

  const handleDrainOutbox = async () => {
    try {
      const res = await triggerOutboxDrain();
      notify(`Outbox drained: ${res.sent} sent, ${res.retried} retried, ${res.failed} failed`);
      reloadAll();
    } catch (err) {
      notify(err instanceof Error ? err.message : "Could not drain outbox");
    }
  };

  const handleResendAlert = async (alertId: number) => {
    try {
      const res = await resendAlertNotification(alertId);
      notify(`Re-dispatched alert #${alertId}: ${res.sent} sent, ${res.retried} retried`);
      reloadAll();
    } catch (err) {
      notify(err instanceof Error ? err.message : "Could not resend alert");
    }
  };

  const handleOpenTestPreview = async (targetKind: "base" | "bull" = "base") => {
    try {
      const preview = await previewTestAlertEmail({ targetKind });
      setEmailPreviewItem(preview);
    } catch (err) {
      notify(err instanceof Error ? err.message : "Could not generate email preview");
    }
  };

  const activeAlertsCount = apiAlerts.filter(isAlertActive).length;
  const freshnessUnknownCompanies = portfolioCompanies.filter((c) =>
    isQuoteFreshnessUnknown(c.latest_quote),
  );
  const staleCompanies = portfolioCompanies.filter(
    (c) =>
      c.latest_quote != null &&
      !isQuoteFreshnessUnknown(c.latest_quote) &&
      !c.latest_quote.is_fresh &&
      !c.latest_quote.is_simulated,
  );
  const freshCompaniesCount = portfolioCompanies.filter(
    (c) =>
      c.latest_quote != null &&
      !isQuoteFreshnessUnknown(c.latest_quote) &&
      c.latest_quote.is_fresh &&
      !c.latest_quote.is_simulated,
  ).length;

  // (a) Browser-to-backend transport status (SSE / WS / polling)
  const browserTransportStatus =
    portfolioStatus === "error"
      ? "disconnected"
      : liveStream.state === "connected"
        ? liveStream.transport
        : "polling";

  // (b) Broker/provider feed health (connected, last quote time, freshness, gaps)
  const brokerProviderName = workerStatus?.provider || "unknown";
  const providerConnected =
    workerStatus?.state === "running" &&
    (workerStatus?.connection_status === "connected" ||
      workerStatus?.connection_status === "polling") &&
    workerStatus?.feed_ready === true;
  const marketClosed = portfolioStatus !== "error" && workerStatus?.market_open === false;
  const brokerFeedLive =
    portfolioStatus === "ready" &&
    providerConnected &&
    freshCompaniesCount > 0 &&
    staleCompanies.length === 0 &&
    freshnessUnknownCompanies.length === 0 &&
    !workerStatus?.lag_detected;

  const headerShortStatusLabel =
    portfolioStatus === "error"
      ? "API Down"
      : marketClosed
        ? "Market Closed"
        : brokerFeedLive
          ? "Live"
          : freshnessUnknownCompanies.length > 0
            ? "Unknown Time"
            : staleCompanies.length > 0
              ? "Stale"
              : workerStatus?.state === "running"
                ? "Awaiting Quotes"
                : "Feed Offline";

  return (
    <div
      className={`app-shell min-h-screen ${compactTables ? "compact-tables" : ""}`}
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
            <div
              className={`connection ${brokerFeedLive ? "connected" : "disconnected"} ${marketClosed ? "connection-closed" : ""}`}
              title={`Market session: ${marketClosed ? "closed" : workerStatus?.market_open === true ? "open" : "unknown"} · Broker Feed: ${brokerProviderName} (${workerStatus?.connection_status ?? "stopped"}) · UI Transport: ${browserTransportStatus.toUpperCase()}`}
            >
              <span className="status-dot" />
              <span className="connection-mobile-label">{headerShortStatusLabel}</span>
              <span className="connection-label">
                {portfolioStatus === "error"
                  ? "Backend Disconnected"
                  : marketClosed
                    ? `Market Closed · Last quote ${workerStatus?.last_poll_at ? new Date(workerStatus.last_poll_at).toLocaleTimeString("en-IN") : "unavailable"} · UI ${browserTransportStatus.toUpperCase()}`
                    : brokerFeedLive
                      ? `Live · Provider ${brokerProviderName} (${freshCompaniesCount} fresh) · UI ${browserTransportStatus.toUpperCase()}`
                      : freshnessUnknownCompanies.length > 0
                        ? `Provider ${brokerProviderName} · ${freshnessUnknownCompanies.length} Freshness Unknown · UI ${browserTransportStatus.toUpperCase()}`
                        : staleCompanies.length > 0
                          ? `Provider ${brokerProviderName} · ${staleCompanies.length} Stale Quote(s) · UI ${browserTransportStatus.toUpperCase()}`
                          : `Provider ${brokerProviderName} · Worker ${workerStatus?.state ?? "stopped"} (Not Ready) · UI ${browserTransportStatus.toUpperCase()}`}
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
            workerStatus?.state === "error") && (
            <div className="feed-banner feed-error" role="alert">
              <div className="feed-main-icon">
                <AlertTriangle size={18} />
              </div>
              <div>
                <b>
                  {portfolioStatus === "error"
                    ? "Market Monitoring API Disconnected"
                    : `Market Feed Warning: ${workerStatus?.last_error}`}
                </b>
                <small>
                  Check the feed connection and worker status before relying on live market data.
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
              onResetWorkspace={async () => {
                await resetWorkspaceData();
                setSelectedCompany(null);
                notify("All workspace data cleared");
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
              onDrainOutbox={handleDrainOutbox}
              onResendAlert={handleResendAlert}
              onPreviewEmail={(item) => setEmailPreviewItem(item)}
              onPreviewTestEmail={handleOpenTestPreview}
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

          {page === "Upload Excel" && (
            <BackendUploadPage
              onCompleted={() => {
                reloadAll();
                void handlePollNow();
              }}
              onNavigatePortfolio={() => pickPage("Portfolio")}
            />
          )}

          {page === "Settings" && (
            <SettingsPage
              compactTables={compactTables}
              setCompactTables={setCompactTables}
              soundEnabled={soundEnabled}
              setSoundEnabled={setSoundEnabled}
              workerStatus={workerStatus}
              notify={notify}
              onPreviewTestEmail={handleOpenTestPreview}
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
            onPreviewEmail={(item) => setEmailPreviewItem(item)}
          />
        )}
      </AnimatePresence>

      <AnimatePresence>
        {emailPreviewItem && (
          <EmailPreviewModal
            item={emailPreviewItem}
            onClose={() => setEmailPreviewItem(null)}
            onResend={
              "id" in emailPreviewItem && emailPreviewItem.id > 0
                ? () => void handleResendAlert(emailPreviewItem.id)
                : undefined
            }
            onSwitchSample={
              !("id" in emailPreviewItem)
                ? (kind) => void handleOpenTestPreview(kind)
                : undefined
            }
            notify={notify}
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
        {liveStream.liveNotifications.length > 0 && (
          <div className="live-outbox-toast-stack" role="region" aria-label="Live Target Alerts">
            {liveStream.liveNotifications.map((item) => {
              const isBase = item.alert.target_kind === "base";
              const breachAbs =
                item.alert.breach_abs ??
                (item.alert.target != null ? item.alert.price - item.alert.target : null);
              const breachPct =
                item.alert.breach_pct ??
                (item.alert.target != null && item.alert.target > 0
                  ? ((item.alert.price - item.alert.target) / item.alert.target) * 100
                  : null);
              return (
                <motion.div
                  key={item.outboxId}
                  className={`live-outbox-toast ${isBase ? "outbox-base" : "outbox-bull"}`}
                  initial={{ opacity: 0, x: 24, scale: 0.96 }}
                  animate={{ opacity: 1, x: 0, scale: 1 }}
                  exit={{ opacity: 0, x: 24, scale: 0.96 }}
                >
                  <div className="live-outbox-toast-header">
                    <span className={`status-badge ${isBase ? "badge-buy" : "badge-sell"}`}>
                      <i />
                      {isBase ? "Base Target Triggered" : "Bull Target Triggered"}
                    </span>
                    <button
                      className="icon-button small-icon"
                      aria-label="Dismiss alert notification"
                      onClick={() => liveStream.dismissLiveNotification(item.outboxId)}
                    >
                      <X size={14} />
                    </button>
                  </div>
                  <div className="live-outbox-toast-body">
                    <b>
                      {item.alert.company} ({item.alert.exchange}:{item.alert.symbol})
                    </b>
                    <p>
                      CMP <strong>{money(item.alert.price)}</strong> crossed{" "}
                      {isBase ? "Base" : "Bull"} Target{" "}
                      <strong>
                        {item.alert.target != null ? money(item.alert.target) : "—"}
                      </strong>{" "}
                      {breachAbs != null && breachPct != null && (
                        <span className={isBase ? "positive" : "negative"}>
                          ({formatDistance(breachAbs, breachPct)})
                        </span>
                      )}
                    </p>
                  </div>
                  <div className="live-outbox-toast-actions">
                    <button
                      className="button secondary small"
                      onClick={() => {
                        setEmailPreviewItem(item.alert);
                      }}
                    >
                      View Email
                    </button>
                    <button
                      className="button secondary small"
                      onClick={() => {
                        const match = portfolioCompanies.find(
                          (c) =>
                            c.exchange === item.alert.exchange &&
                            c.symbol === item.alert.symbol,
                        );
                        if (match) setSelectedCompany(match);
                        liveStream.dismissLiveNotification(item.outboxId);
                      }}
                    >
                      Inspect Symbol
                    </button>
                    <button
                      className="button primary small"
                      onClick={() => {
                        void handleAlertLifecycle(item.alert.id, "acknowledge");
                        liveStream.dismissLiveNotification(item.outboxId);
                      }}
                    >
                      Acknowledge
                    </button>
                  </div>
                </motion.div>
              );
            })}
          </div>
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
  onResetWorkspace,
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
  onResetWorkspace: () => Promise<void>;
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
  const freshQuotesCount = companies.filter(
    (c) =>
      c.latest_quote != null &&
      !isQuoteFreshnessUnknown(c.latest_quote) &&
      c.latest_quote.is_fresh &&
      !c.latest_quote.is_simulated,
  ).length;
  const workerFeedReady =
    workerStatus?.state === "running" &&
    workerStatus?.feed_ready === true &&
    freshQuotesCount > 0;
  const workerTileValue =
    workerStatus?.state === "error"
      ? "FEED ERROR"
      : workerFeedReady
        ? (workerStatus?.provider ?? "unknown").toUpperCase()
        : "NOT READY";
  const workerTileTone =
    workerStatus?.state === "error" ? "red" : workerFeedReady ? "green" : "amber";
  const workerTileHint =
    workerStatus?.state === "error"
      ? workerStatus.last_error || "Background worker reported an error"
      : workerFeedReady
        ? workerStatus?.last_poll_at
          ? `Last quote: ${new Date(workerStatus.last_poll_at).toLocaleTimeString("en-IN")}`
          : `${freshQuotesCount} fresh live quotes`
        : workerStatus?.state === "running"
          ? "Worker running · No fresh quotes yet"
          : "Worker stopped · No live quotes";

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
            {(companies.length > 0 || importHistory.length > 0) && (
              <button
                className="button secondary"
                onClick={() => void onResetWorkspace()}
              >
                <RotateCcw size={14} /> Clear All Data
              </button>
            )}
            <button
              className="button secondary"
              onClick={() => void onPollNow()}
              disabled={isPollingNow || companies.length === 0}
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
          value={workerTileValue}
          change={`${pendingConfigCount} pending config`}
          icon={Activity}
          tone={workerTileTone}
          hint={workerTileHint}
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

        <section className="card target-list-card market-health-card">
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
              <b>
                {workerStatus?.provider ?? "unknown"} (
                {workerStatus?.connection_status ?? workerStatus?.state ?? "stopped"})
              </b>
            </div>
            <div>
              <small>Feed Readiness & Backlog</small>
              <b>
                {workerStatus?.feed_ready ? "Ready" : "Not Ready"} · Queue:{" "}
                {workerStatus?.backlog_state ?? "normal"} (
                {workerStatus?.queue_overflow_drops ?? 0} drops)
              </b>
            </div>
            <div>
              <small>Market Session</small>
              <b
                className={
                  workerStatus?.market_open === false ? "market-session-closed" : ""
                }
              >
                {workerStatus?.market_open === false
                  ? "Closed"
                  : workerStatus?.market_open === true
                    ? "Open"
                    : "Status unavailable"}
              </b>
              <small className="market-health-last-quote">
                Last quote:{" "}
                {workerStatus?.last_poll_at
                  ? new Date(workerStatus.last_poll_at).toLocaleString("en-IN")
                  : "No live quotes recorded"}
              </small>
            </div>
            <div>
              <small>Freshness & Gaps</small>
              <b>
                {workerStatus?.stale_symbols_count ?? 0} stale ·{" "}
                {workerStatus?.freshness_unknown_symbols_count ?? 0} unknown ·{" "}
                {workerStatus?.reconnect_gaps_count ?? 0} reconnect gap(s)
              </b>
            </div>
          </div>
        </section>

        <section className="card target-list-card import-history-card">
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
    <>
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
            const freshnessUnknown = isQuoteFreshnessUnknown(quote);
            const isStale =
              quote != null && !freshnessUnknown && !quote.is_fresh && !quote.is_simulated;
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
                  background: isStale
                    ? "rgba(180, 83, 9, 0.06)"
                    : freshnessUnknown
                      ? "rgba(231, 173, 90, 0.08)"
                      : undefined,
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
                      <small
                        className={`quote-freshness ${
                          freshnessUnknown || isStale ? "quote-warning" : ""
                        }`}
                      >
                        {freshnessUnknown
                          ? "⚠ Freshness unknown"
                          : isStale
                            ? "⚠ STALE QUOTE"
                            : quote.is_simulated
                              ? "Simulated"
                              : "Live Validated"}
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
                  <span className="text-muted">
                    {formatDistance(company.distance_to_base_abs, company.distance_to_base_pct)}
                  </span>
                </td>
                <td>
                  <span className="text-muted">
                    {formatDistance(company.distance_to_bull_abs, company.distance_to_bull_pct)}
                  </span>
                </td>
                <td>{alertStatusBadge(company)}</td>
                <td>
                  {quote ? (
                    <div>
                      <div>
                        {quote.quoted_at
                          ? new Date(quote.quoted_at).toLocaleTimeString("en-IN")
                          : "Freshness unknown"}
                      </div>
                      <small className="cell-sub">
                        {quote.provider} ·{" "}
                        {freshnessUnknown
                          ? "No source timestamp"
                          : isStale
                            ? "STALE"
                            : "Fresh"}
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

      <div className="mobile-company-cards" aria-label="Monitored companies mobile cards">
        {companies.map((company) => {
          const quote = company.latest_quote;
          const freshnessUnknown = isQuoteFreshnessUnknown(quote);
          const isStale =
            quote != null && !freshnessUnknown && !quote.is_fresh && !quote.is_simulated;
          return (
            <button
              key={`mobile-${company.exchange}:${company.symbol}`}
              type="button"
              className="mobile-company-card"
              onClick={() => onSelectCompany(company)}
            >
              <div className="mobile-card-head">
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
                      </span>
                    </small>
                  </span>
                </div>
                {alertStatusBadge(company)}
              </div>

              <div className="mobile-card-grid">
                <div className="mobile-card-metric">
                  <small>Latest Price</small>
                  <b>{quote ? money(quote.price) : "Awaiting quote"}</b>
                  <span
                    className={`cell-sub ${
                      freshnessUnknown || isStale ? "quote-warning" : ""
                    }`}
                  >
                    {quote
                      ? freshnessUnknown
                        ? "Freshness unknown"
                        : isStale
                          ? "STALE QUOTE"
                          : quote.quoted_at
                            ? new Date(quote.quoted_at).toLocaleTimeString("en-IN")
                            : "Fresh"
                      : `Ref CMP: ${company.cmp == null ? "—" : money(company.cmp)}`}
                  </span>
                </div>

                <div className="mobile-card-metric">
                  <small>Base Target</small>
                  <b className="target-base-price">
                    {company.base_target == null ? "—" : money(company.base_target)}
                  </b>
                  <span className="cell-sub">
                    {company.base_direction === "below"
                      ? "Trigger ≤ Base"
                      : company.base_direction === "above"
                        ? "Trigger ≥ Base"
                        : "Pending direction"}
                  </span>
                </div>

                <div className="mobile-card-metric">
                  <small>Bull Target</small>
                  <b className="target-bull-price">
                    {company.bull_target == null ? "—" : money(company.bull_target)}
                  </b>
                  <span className="cell-sub">
                    {company.bull_direction === "above"
                      ? "Trigger ≥ Bull"
                      : company.bull_direction === "below"
                        ? "Trigger ≤ Bull"
                        : "Pending direction"}
                  </span>
                </div>

                <div className="mobile-card-metric">
                  <small>Distance to Base / Bull</small>
                  <b>
                    Base: {formatDistance(company.distance_to_base_abs, company.distance_to_base_pct)}
                  </b>
                  <span className="cell-sub">
                    Bull: {formatDistance(company.distance_to_bull_abs, company.distance_to_bull_pct)}
                  </span>
                </div>
              </div>
            </button>
          );
        })}
      </div>
    </>
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
        const freshnessUnknown = isQuoteFreshnessUnknown(c.latest_quote);
        const isStale =
          c.latest_quote != null &&
          !freshnessUnknown &&
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
          (alertFilter === "Freshness Unknown" && freshnessUnknown) ||
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
              <option>Freshness Unknown</option>
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
  onDrainOutbox,
  onResendAlert,
  onPreviewEmail,
  onPreviewTestEmail,
}: {
  apiAlerts: ApiAlert[];
  apiStatus: "loading" | "ready" | "error";
  apiError: string;
  workerStatus: WorkerStatus | null;
  onRefresh: () => void;
  onAction: (id: number, action: "acknowledge" | "resolve" | "reset") => Promise<void>;
  onDrainOutbox: () => Promise<void>;
  onResendAlert: (id: number) => Promise<void>;
  onPreviewEmail: (alert: ApiAlert) => void;
  onPreviewTestEmail: (kind: "base" | "bull") => Promise<void>;
}) {
  const [targetFilter, setTargetFilter] = useState("All targets");
  const [lifecycleFilter, setLifecycleFilter] = useState("All states");
  const [deliveryFilter, setDeliveryFilter] = useState("All deliveries");
  const [query, setQuery] = useState("");
  const [draining, setDraining] = useState(false);

  const activeAlerts = apiAlerts.filter(isAlertActive);

  const filtered = apiAlerts.filter((a) => {
    const matchesTarget =
      targetFilter === "All targets" ||
      (targetFilter === "Base" && a.target_kind === "base") ||
      (targetFilter === "Bull" && a.target_kind === "bull");
    const st = a.lifecycle_status ?? "active";
    const matchesLifecycle =
      lifecycleFilter === "All states" ||
      (lifecycleFilter === "Triggered" && isAlertActive(a)) ||
      (lifecycleFilter === "Resolved" && (st === "resolved" || st === "reset")) ||
      st === lifecycleFilter.toLowerCase();
    const matchesDelivery =
      deliveryFilter === "All deliveries" ||
      (deliveryFilter === "Delivered" && a.delivery_status === "sent") ||
      (deliveryFilter === "Dry Run" && a.delivery_mode === "dry_run") ||
      (deliveryFilter === "Queued / Retry" &&
        (a.delivery_status === "queued" || a.delivery_status === "retry")) ||
      (deliveryFilter === "Failed" && a.delivery_status === "failed");
    const matchesQuery =
      !query ||
      `${a.company} ${a.symbol}`.toLowerCase().includes(query.toLowerCase());
    return matchesTarget && matchesLifecycle && matchesDelivery && matchesQuery;
  });

  const handleDrain = async () => {
    setDraining(true);
    try {
      await onDrainOutbox();
    } finally {
      setDraining(false);
    }
  };

  return (
    <>
      <PageHeading
        eyebrow="ALERT ENGINE & NOTIFICATION OUTBOX"
        title="Price Target Alerts"
        description="Triggered Base and Bull target events with exact trigger prices, breach variance, formatted email previews, and re-arm controls."
        action={
          <div className="heading-actions">
            <button
              className="button secondary"
              onClick={() => void onPreviewTestEmail("base")}
            >
              <Bell size={14} /> Preview Alert Email
            </button>
            {activeAlerts.length > 0 && (
              <button
                className="button secondary"
                onClick={() => {
                  activeAlerts.forEach((a) => void onAction(a.id, "acknowledge"));
                }}
              >
                <CheckCheck size={14} /> Acknowledge All ({activeAlerts.length})
              </button>
            )}
            <button
              className="button secondary"
              onClick={() => void handleDrain()}
              disabled={draining}
            >
              <Zap size={14} /> {draining ? "Draining Outbox…" : "Drain / Retry Outbox"}
            </button>
            <button
              className="button primary"
              onClick={onRefresh}
              disabled={apiStatus === "loading"}
            >
              <RefreshCw size={14} /> Refresh Alerts
            </button>
          </div>
        }
      />

      <div className="alert-summary-row">
        <div className="alert-summary">
          <span className="summary-dot red" />
          <div>
            <b>{activeAlerts.length}</b>
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
              {
                apiAlerts.filter(
                  (a) =>
                    a.lifecycle_status === "resolved" ||
                    a.lifecycle_status === "reset",
                ).length
              }
            </b>
            <small>Resolved / Reset</small>
          </div>
        </div>
        <div className="alert-summary">
          <span className="summary-dot amber" />
          <div>
            <b>{apiAlerts.filter((a) => a.delivery_status === "sent").length}</b>
            <small>Notifications Processed</small>
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
              <b>Persisted Target Alert Events & Mail Outbox</b>
              <small>
                Worker: {workerStatus?.state ?? "stopped"} · Notification mode:{" "}
                {workerStatus?.notification_mode ?? "dry_run"}
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
            <select
              value={deliveryFilter}
              onChange={(e) => setDeliveryFilter(e.target.value)}
            >
              <option>All deliveries</option>
              <option>Delivered</option>
              <option>Dry Run</option>
              <option>Queued / Retry</option>
              <option>Failed</option>
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
                  <th>Trigger Price / Target & Variance</th>
                  <th>Direction</th>
                  <th>Trigger Time</th>
                  <th>Mail & Dashboard Delivery</th>
                  <th>Lifecycle Status</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((alert) => {
                  const lifecycle = alert.lifecycle_status ?? "active";
                  const active = isAlertActive(alert);
                  const isBase = alert.target_kind === "base";
                  const breachAbs =
                    alert.breach_abs ??
                    (alert.target != null ? alert.price - alert.target : null);
                  const breachPct =
                    alert.breach_pct ??
                    (alert.target != null && alert.target > 0
                      ? ((alert.price - alert.target) / alert.target) * 100
                      : null);
                  return (
                    <tr key={alert.id}>
                      <td>
                        <span className={`status-badge ${isBase ? "badge-buy" : "badge-sell"}`}>
                          <i />
                          {isBase ? "Base Target Alert" : "Bull Target Alert"}
                        </span>
                        <small className="cell-sub" style={{ display: "block", marginTop: 4 }}>
                          {alert.is_simulated ? "SIMULATED" : "LIVE MARKET"} · Cycle #{alert.trigger_cycle ?? 1}
                        </small>
                      </td>
                      <td>
                        <b>{alert.company}</b>
                        <small className="cell-sub">
                          {alert.exchange}:{alert.symbol}
                          {alert.saved_cmp != null ? ` · Ref CMP: ${money(alert.saved_cmp)}` : ""}
                        </small>
                      </td>
                      <td>
                        <div>
                          <b>{money(alert.price)}</b> /{" "}
                          {alert.target == null ? "—" : money(alert.target)}
                        </div>
                        {breachAbs != null && breachPct != null && (
                          <small className={isBase ? "positive" : "negative"}>
                            Breach: {formatDistance(breachAbs, breachPct)}
                          </small>
                        )}
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
                        {alert.recipients && alert.recipients.length > 0 && (
                          <small className="cell-sub" style={{ display: "block", marginTop: 3 }}>
                            To: {alert.recipients.join(", ")}
                          </small>
                        )}
                        {alert.last_error && (
                          <small className="worker-error">{alert.last_error}</small>
                        )}
                      </td>
                      <td>
                        <span className={`alert-state state-${active ? "triggered" : lifecycle}`}>
                          {lifecycle.toUpperCase()}
                        </span>
                      </td>
                      <td>
                        <div className="row-actions">
                          <button
                            className="button secondary small"
                            onClick={() => onPreviewEmail(alert)}
                          >
                            View Email
                          </button>
                          <button
                            className="button secondary small"
                            title="Re-dispatch notification via configured channels"
                            onClick={() => void onResendAlert(alert.id)}
                          >
                            Resend
                          </button>
                          {active && (
                            <button
                              className="button secondary small"
                              onClick={() => void onAction(alert.id, "acknowledge")}
                            >
                              Acknowledge
                            </button>
                          )}
                          {lifecycle !== "resolved" && lifecycle !== "reset" && (
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
                <span>
                  Triggered Base and Bull alerts will appear here automatically. Click &ldquo;Preview Alert Email&rdquo; above to inspect the formatted email template.
                </span>
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
  const freshnessUnknownList = companies.filter((c) =>
    isQuoteFreshnessUnknown(c.latest_quote),
  );
  const staleList = companies.filter(
    (c) =>
      c.latest_quote != null &&
      !isQuoteFreshnessUnknown(c.latest_quote) &&
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
          <b>{workerStatus?.provider ?? "unknown"}</b>
          <small className="market-uptime">
            <i className="status-dot" /> Exchange Calendar:{" "}
            {workerStatus?.exchange_calendar ?? "unknown"}
          </small>
        </div>
        <div className="card market-stat">
          <small>Provider Connection & Queue</small>
          <b>
            {workerStatus?.connection_status ?? workerStatus?.state ?? "stopped"} (
            {workerStatus?.feed_ready ? "Ready" : "Not Ready"})
          </b>
          <small className="market-uptime">
            Backlog: {workerStatus?.backlog_state ?? "normal"} (
            {workerStatus?.queue_overflow_drops ?? 0} overflow drops)
          </small>
        </div>
        <div className="card market-stat">
          <small>Protocol & Reconnect Gaps</small>
          <b>
            {workerStatus?.reconnect_gaps_count ?? 0}{" "}
            <span className="market-stat-sub">reconnect gap(s)</span>
          </b>
          <small className="market-uptime">
            Seq gap check: {workerStatus?.protocol_gap_detection ?? "disabled"} (
            {workerStatus?.protocol_sequence_gaps ?? 0} gaps)
          </small>
        </div>
        <div className="card market-stat">
          <small>Quote Freshness Summary</small>
          <b>
            {workerStatus?.fresh_symbols_count ??
              companies.filter((c) => c.latest_quote?.is_fresh).length}{" "}
            <span className="market-stat-sub">fresh quotes</span>
          </b>
          <small className="market-uptime">
            {staleList.length} stale · {freshnessUnknownList.length} freshness unknown ·{" "}
            {workerStatus?.last_poll_failed ?? 0} failed
          </small>
        </div>
      </div>

      <div className="market-content-grid">
        <section className="card market-movers">
          <div className="section-head">
            <div>
              <h2>Stale, Freshness-Unknown, or Missing Quote Watchlist</h2>
              <p>Symbols requiring fresh quote verification</p>
            </div>
          </div>
          {staleList.length === 0 &&
          freshnessUnknownList.length === 0 &&
          missingQuoteList.length === 0 ? (
            <p className="empty-copy">
              All monitored symbols have fresh validated market quotes.
            </p>
          ) : (
            <div className="movers-list">
              {[...freshnessUnknownList, ...staleList, ...missingQuoteList]
                .slice(0, 15)
                .map((c) => (
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
                    <span
                      className={`status-badge ${
                        isQuoteFreshnessUnknown(c.latest_quote)
                          ? "badge-approaching"
                          : "badge-sell"
                      }`}
                    >
                      <i />
                      {isQuoteFreshnessUnknown(c.latest_quote)
                        ? "FRESHNESS UNKNOWN"
                        : c.latest_quote
                          ? "STALE"
                          : "NO QUOTE"}
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
  compactTables,
  setCompactTables,
  soundEnabled,
  setSoundEnabled,
  workerStatus,
  notify,
  onPreviewTestEmail,
}: {
  compactTables: boolean;
  setCompactTables: (v: boolean) => void;
  soundEnabled: boolean;
  setSoundEnabled: (v: boolean) => void;
  workerStatus: WorkerStatus | null;
  notify: (message: string) => void;
  onPreviewTestEmail: (kind: "base" | "bull") => Promise<void>;
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
        description="Configure dashboard and email alert delivery channels, recipients, audio chimes, and table density."
        action={
          <div className="heading-actions">
            <button
              className="button secondary"
              onClick={() => void onPreviewTestEmail("base")}
            >
              <Bell size={14} /> Preview Base Email Template
            </button>
            <button
              className="button secondary"
              onClick={() => void onPreviewTestEmail("bull")}
            >
              <Zap size={14} /> Preview Bull Email Template
            </button>
          </div>
        }
      />
      <div className="settings-layout">
        <div className="settings-main">
          <section className="card settings-card">
            <div className="settings-section-title">
              <div className="settings-section-icon amber">
                <Bell size={16} />
              </div>
              <div>
                <h2>Alert Notification Channels (Mail & Dashboard)</h2>
                <p>Persisted in the backend notification outbox with automatic retry backoff.</p>
              </div>
            </div>
            {row(
              "Dashboard notifications",
              "Deliver instant alerts to the dashboard notification center & live banner.",
              toggle(prefs.dashboard_enabled, (v) =>
                setPrefs({ ...prefs, dashboard_enabled: v }),
              ),
            )}
            {row(
              "Audio chime on live target trigger",
              "Play a subtle chime when a new Base or Bull target alert arrives.",
              toggle(soundEnabled, setSoundEnabled),
            )}
            {row(
              "Email notifications",
              "Send formatted HTML & plain-text price target alert emails via SMTP / spool.",
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
                <p>Configure table density and inspect worker status.</p>
              </div>
            </div>
            {row(
              "Compact data tables",
              "Information-dense rows for monitoring 40+ companies.",
              toggle(compactTables, setCompactTables),
            )}
            {row(
              "Background Monitoring Service",
              `Provider: ${workerStatus?.provider ?? "unknown"} · Calendar: ${
                workerStatus?.exchange_calendar ?? "unknown"
              }`,
              <span className="local-only-tag">
                {workerStatus?.state?.toUpperCase() ?? "STOPPED"}
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
  onPreviewEmail,
}: {
  alerts: ApiAlert[];
  onClose: () => void;
  onAction: (id: number, action: "acknowledge" | "resolve" | "reset") => Promise<void>;
  onPreviewEmail: (alert: ApiAlert) => void;
}) {
  const [unreadOnly, setUnreadOnly] = useState(false);
  const unreadAlerts = alerts.filter(isAlertActive);
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
              const isNew = isAlertActive(a);
              const isBase = a.target_kind === "base";
              const breachAbs =
                a.breach_abs ??
                (a.target != null ? a.price - a.target : null);
              const breachPct =
                a.breach_pct ??
                (a.target != null && a.target > 0
                  ? ((a.price - a.target) / a.target) * 100
                  : null);
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
                      {breachAbs != null && breachPct != null && (
                        <span className={isBase ? "positive" : "negative"}>
                          Breach: {formatDistance(breachAbs, breachPct)}
                        </span>
                      )}
                    </div>
                    <div className="drawer-alert-time">
                      {new Date(a.triggered_at).toLocaleString("en-IN")} ·{" "}
                      {a.delivery_label}
                      <button onClick={() => onPreviewEmail(a)}>
                        View Email
                      </button>
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

function EmailPreviewModal({
  item,
  onClose,
  onResend,
  onSwitchSample,
  notify,
}: {
  item: ApiAlert | AlertEmailPreviewResponse;
  onClose: () => void;
  onResend?: () => void;
  onSwitchSample?: (kind: "base" | "bull") => void;
  notify: (msg: string) => void;
}) {
  const [viewMode, setViewMode] = useState<"rendered" | "plain">("rendered");
  const [sendingSample, setSendingSample] = useState(false);
  const isBase = item.target_kind === "base";
  const subject =
    item.email_subject ||
    `Price Target Alert – ${item.symbol} (${isBase ? "Base" : "Bull"} Target Reached)`;
  const bodyText = item.email_body || "";
  const recipients =
    item.recipients && item.recipients.length > 0
      ? item.recipients.join(", ")
      : "alerts@plutuswealth.local (Default)";
  const breachAbs =
    item.breach_abs ??
    (item.target != null ? item.price - item.target : null);
  const breachPct =
    item.breach_pct ??
    (item.target != null && item.target > 0
      ? ((item.price - item.target) / item.target) * 100
      : null);

  const copyEmail = async () => {
    try {
      await navigator.clipboard.writeText(`Subject: ${subject}\nTo: ${recipients}\n\n${bodyText}`);
      notify("Formatted email copied to clipboard");
    } catch {
      notify("Could not copy email to clipboard");
    }
  };

  const handleSendSample = async () => {
    setSendingSample(true);
    try {
      const res = await sendSampleAlertEmail({
        symbol: item.symbol,
        targetKind: item.target_kind,
      });
      notify(`Sample email sent to ${res.recipients.join(", ")} (${res.status})`);
    } catch (err) {
      notify(err instanceof Error ? err.message : "Failed to send sample email");
    } finally {
      setSendingSample(false);
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
        aria-label="Close email preview"
      />
      <motion.div
        className="edit-modal email-preview-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="email-preview-title"
        initial={{ opacity: 0, scale: 0.96, y: 12 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.96, y: 12 }}
      >
        <div className="modal-head">
          <div>
            <div className="eyebrow">
              EMAIL & DASHBOARD NOTIFICATION PREVIEW · {item.exchange}:{item.symbol}
            </div>
            <h2 id="email-preview-title">{subject}</h2>
            <p>
              To: <strong>{recipients}</strong> · Triggered{" "}
              {new Date(item.triggered_at).toLocaleString("en-IN")}
            </p>
          </div>
          <button
            className="icon-button"
            aria-label="Close email preview"
            onClick={onClose}
          >
            <X size={17} />
          </button>
        </div>

        <div className="email-preview-toolbar">
          <div className="theme-options">
            <button
              className={viewMode === "rendered" ? "theme-option selected" : "theme-option"}
              onClick={() => setViewMode("rendered")}
            >
              Rendered Email Card
            </button>
            <button
              className={viewMode === "plain" ? "theme-option selected" : "theme-option"}
              onClick={() => setViewMode("plain")}
            >
              Plain-Text Email (.eml)
            </button>
          </div>
          {onSwitchSample && (
            <div className="row-actions">
              <button
                className={`button secondary small ${isBase ? "primary" : ""}`}
                onClick={() => onSwitchSample("base")}
              >
                Sample Base Alert
              </button>
              <button
                className={`button secondary small ${!isBase ? "primary" : ""}`}
                onClick={() => onSwitchSample("bull")}
              >
                Sample Bull Alert
              </button>
            </div>
          )}
        </div>

        {viewMode === "rendered" ? (
          <div className="email-preview-card">
            <div
              className={`email-preview-banner ${
                isBase ? "email-preview-banner-base" : "email-preview-banner-bull"
              }`}
            >
              <div>
                <span className={`status-badge ${isBase ? "badge-buy" : "badge-sell"}`}>
                  <i />
                  {isBase ? "BASE TARGET ALERT" : "BULL TARGET ALERT"}
                </span>
                <h3>
                  {item.company} ({item.exchange}:{item.symbol})
                </h3>
                <p>
                  {item.company} has reached its configured {isBase ? "Base" : "Bull"} Target Price.
                </p>
              </div>
              <div className="email-preview-cmp">
                <small>CURRENT MARKET PRICE</small>
                <b>{money(item.price)}</b>
              </div>
            </div>

            <div className="email-preview-metrics">
              <div>
                <small>CONFIGURED TARGET</small>
                <b>{item.target != null ? money(item.target) : "—"}</b>
              </div>
              <div>
                <small>BREACH / VARIANCE</small>
                <b className={isBase ? "positive" : "negative"}>
                  {breachAbs != null && breachPct != null
                    ? formatDistance(breachAbs, breachPct)
                    : "At Target"}
                </b>
              </div>
              <div>
                <small>SAVED REF CMP</small>
                <b>{item.saved_cmp != null ? money(item.saved_cmp) : "—"}</b>
              </div>
              <div>
                <small>DIRECTION RULE</small>
                <b>
                  {item.comparison_direction === "below"
                    ? "Downward (CMP ≤ Base)"
                    : "Upward (CMP ≥ Bull)"}
                </b>
              </div>
            </div>
          </div>
        ) : (
          <pre className="email-preview-pre">{bodyText}</pre>
        )}

        {"delivery_gate_reason" in item && (
          <div className="modal-note" style={{ marginBottom: 4 }}>
            <ShieldCheck size={14} /> Delivery Status: {item.delivery_gate_reason}
          </div>
        )}

        <div className="modal-actions">
          <button className="button secondary" onClick={() => void copyEmail()}>
            Copy Email Text
          </button>
          <button
            className="button secondary"
            onClick={() => void handleSendSample()}
            disabled={sendingSample}
          >
            <Bell size={14} /> {sendingSample ? "Sending Email…" : "Send Sample Email to Inbox"}
          </button>
          {onResend && (
            <button className="button secondary" onClick={onResend}>
              <RefreshCw size={14} /> Re-send / Drain Outbox
            </button>
          )}
          <button className="button primary" onClick={onClose}>
            Done
          </button>
        </div>
      </motion.div>
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
  const alertList = details?.alerts ?? details?.alert_history ?? [];
  const revisionList = details?.revisions ?? details?.target_revisions ?? [];
  const chartSeries = useMemo(() => {
    const hist = details?.price_history ?? details?.quote_history ?? [];
    if (hist.length > 0) {
      return [...hist]
        .reverse()
        .map((q) => ({
          label: q.quoted_at
            ? new Date(q.quoted_at).toLocaleTimeString("en-IN", {
                hour: "2-digit",
                minute: "2-digit",
              })
            : "Unknown time",
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
                  isQuoteFreshnessUnknown(quote)
                    ? "Freshness unknown (no source timestamp)"
                    : quote.is_fresh
                      ? "Fresh"
                      : "STALE"
                } · ${
                  quote.quoted_at
                    ? `Quoted ${new Date(quote.quoted_at).toLocaleString("en-IN")}`
                    : `Received ${new Date(quote.received_at).toLocaleString("en-IN")} (diagnostic)`
                }`
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
          ) : alertList.length > 0 ? (
            alertList.map((a) => (
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
                    {a.lifecycle_status ?? "active"}
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
          {revisionList.length > 0 ? (
            revisionList.slice(0, 5).map((rev) => (
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
