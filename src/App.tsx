import { useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, DragEvent, ReactNode } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  Activity, ArrowDownRight, ArrowDownUp, ArrowLeft, ArrowRight, ArrowUpRight, Bell, BriefcaseBusiness,
  Check, CheckCheck, ChevronDown, ChevronLeft, ChevronRight, CircleHelp, Clock3, CloudDownload, Command,
  Download, FileSpreadsheet, Globe2, LayoutDashboard, LineChart, Menu, Moon,
  MoreHorizontal, PanelLeftClose, Plus, RefreshCw, Search, Settings, ShieldCheck, SlidersHorizontal, Sun,
  Target, TrendingDown, TrendingUp, Upload, Users, X, Zap,
} from "lucide-react";
import {
  Area, AreaChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { getStatus, historyFor, initialAlerts, initialCompanies } from "./mockData";
import type { Company, PortfolioAlert } from "./types";

type Page = "Overview" | "Portfolio" | "Price Alerts" | "Target Management" | "Market Activity" | "Reports" | "Upload Excel" | "Settings";
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
const money = (n: number) => `₹${n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const wholeINR = (n: number) => `₹${Math.round(n).toLocaleString("en-IN")}`;
const percent = (n: number) => `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;

function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    if (char === '"' && quoted && text[i + 1] === '"') { cell += '"'; i += 1; }
    else if (char === '"') quoted = !quoted;
    else if (char === "," && !quoted) { row.push(cell.trim()); cell = ""; }
    else if ((char === "\n" || char === "\r") && !quoted) {
      if (char === "\r" && text[i + 1] === "\n") i += 1;
      row.push(cell.trim());
      if (row.some((value) => value !== "")) rows.push(row);
      row = []; cell = "";
    } else cell += char;
  }
  row.push(cell.trim());
  if (row.some((value) => value !== "")) rows.push(row);
  return rows;
}

export default function App() {
  const [page, setPage] = useState<Page>("Overview");
  const [companies, setCompanies] = useState(initialCompanies);
  const [alerts, setAlerts] = useState(initialAlerts);
  const [dark, setDark] = useState(true);
  const [collapsed, setCollapsed] = useState(false);
  const [mobileNav, setMobileNav] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [selected, setSelected] = useState<Company | null>(null);
  const [editing, setEditing] = useState<Company | null>(null);
  const [toast, setToast] = useState("");
  const [query, setQuery] = useState("");
  const [period, setPeriod] = useState("1M");
  const [connection, setConnection] = useState(true);
  const [refreshSeconds, setRefreshSeconds] = useState(30);
  const [buyThreshold, setBuyThreshold] = useState(2);
  const [sellThreshold, setSellThreshold] = useState(2);
  const [compactTables, setCompactTables] = useState(true);

  const notify = (message: string) => { setToast(message); window.setTimeout(() => setToast(""), 3000); };
  const acknowledge = (id: string) => setAlerts((items) => items.map((item) => item.id === id ? { ...item, status: "Acknowledged" } : item));
  const refresh = () => {
    setCompanies((items) => items.map((item, i) => ({ ...item, price: Math.round(item.price * (1 + ((i % 3) - 1) * 0.00035) * 100) / 100, updated: "09:30:04" })));
    notify("Demo prices refreshed");
  };
  const pickPage = (next: Page) => { setPage(next); setMobileNav(false); setSelected(null); };
  const unread = alerts.filter((item) => item.status === "New").length;
  useEffect(() => {
    const timer = window.setInterval(() => {
      setCompanies((items) => items.map((item, i) => ({
        ...item,
        price: Math.round(item.price * (1 + ((i % 5) - 2) * 0.00008) * 100) / 100,
        updated: new Date().toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
      })));
    }, refreshSeconds * 1000);
    return () => window.clearInterval(timer);
  }, [refreshSeconds]);
  useEffect(() => {
    if (selected) setSelected(companies.find((company) => company.id === selected.id) ?? null);
  }, [companies, selected]);

  return (
    <div className={`app-shell min-h-screen ${dark ? "theme-dark" : "theme-light"} ${compactTables ? "compact-tables" : ""}`}>
      <aside className={`sidebar ${collapsed ? "sidebar-collapsed" : ""} ${mobileNav ? "sidebar-mobile-open" : ""}`}>
        <div className="brand">
          <div className="brand-mark"><span>N</span></div>
          {!collapsed && <div><div className="brand-name">northstar<span>.</span></div><div className="brand-caption">INVESTMENT OFFICE</div></div>}
          <button className="icon-button sidebar-collapse" aria-label="Collapse sidebar" onClick={() => setCollapsed(!collapsed)}><PanelLeftClose size={16} /></button>
        </div>
        <div className="workspace-switch"><div className="workspace-logo">PW</div>{!collapsed && <><div className="workspace-copy"><b>Plutus Wealth</b><small>Portfolio workspace</small></div><ChevronDown size={14} /></>}</div>
        <nav className="side-nav" aria-label="Main navigation">
          {nav.map((item) => <div key={item.label}>
            {item.group && !collapsed && <div className="nav-group">{item.group}</div>}
            <button className={`nav-item ${page === item.label ? "nav-active" : ""}`} title={collapsed ? item.label : undefined} onClick={() => pickPage(item.label)}>
              <item.icon size={17} strokeWidth={1.8} /><span>{!collapsed && item.label}</span>{item.label === "Price Alerts" && unread > 0 && <em>{unread}</em>}
            </button>
          </div>)}
        </nav>
        {!collapsed && <div className="sidebar-bottom"><div className="support-box"><div className="support-icon"><CircleHelp size={16} /></div><div><b>Need a hand?</b><small>Visit the help center</small></div><ArrowRight size={14} /></div><div className="sidebar-user"><div className="avatar">SK</div><div className="user-copy"><b>Srushti Kadam</b><small>Portfolio Manager</small></div><MoreHorizontal size={18} /></div></div>}
      </aside>

      <div className="main-area">
        <header className="topbar">
          <button className="icon-button mobile-menu" aria-label="Open navigation" onClick={() => setMobileNav(!mobileNav)}><Menu size={19} /></button>
          <div className="breadcrumbs"><span>Workspace</span><ChevronRight size={14} /><b>{page}</b></div>
          <div className="top-actions">
            <label className="global-search"><Search size={15} /><input placeholder="Search companies, symbols..." value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") { const match = companies.find((company) => `${company.name} ${company.symbol}`.toLowerCase().includes(query.toLowerCase())); if (match) { setSelected(match); setQuery(""); } } }} /><kbd><Command size={11} /> K</kbd></label>
            <div className="top-divider" />
            <div className={`connection ${connection ? "connected" : "disconnected"}`}><span className="status-dot" /> <span className="connection-label">{connection ? "Markets open" : "Feed interrupted"}</span></div>
            <button className="icon-button notification-button" aria-label="Open notifications" onClick={() => setDrawer(true)}><Bell size={18} />{unread > 0 && <i>{unread}</i>}</button>
            <button className="profile-button" onClick={() => pickPage("Settings")} aria-label="Open profile and settings"><div className="avatar small-avatar">SK</div><ChevronDown size={14} /></button>
          </div>
        </header>
        <main className="content-area">
          <div className="demo-strip"><span className="demo-pulse" /> DEMO DATA <span className="strip-separator">/</span> NOT LIVE MARKET PRICES <span className="strip-spacer" /> <span><Clock3 size={12} /> Last synced today, 09:30 IST</span></div>
          {page === "Overview" && <Overview companies={companies} alerts={alerts} period={period} setPeriod={setPeriod} onCompany={setSelected} onPage={pickPage} buyThreshold={buyThreshold} sellThreshold={sellThreshold} />}
          {page === "Portfolio" && <Portfolio companies={companies} onCompany={setSelected} onEdit={setEditing} onRefresh={refresh} search={query} notify={notify} buyThreshold={buyThreshold} sellThreshold={sellThreshold} />}
          {page === "Price Alerts" && <AlertsPage alerts={alerts} onAcknowledge={acknowledge} />}
          {page === "Target Management" && <TargetManagement companies={companies} onEdit={setEditing} search={query} buyThreshold={buyThreshold} sellThreshold={sellThreshold} />}
          {page === "Market Activity" && <MarketActivity companies={companies} connection={connection} setConnection={setConnection} onCompany={setSelected} />}
          {page === "Reports" && <Reports companies={companies} alerts={alerts} notify={notify} buyThreshold={buyThreshold} sellThreshold={sellThreshold} />}
          {page === "Upload Excel" && <UploadPage companies={companies} setCompanies={setCompanies} notify={notify} />}
          {page === "Settings" && <SettingsPage dark={dark} setDark={setDark} compactTables={compactTables} setCompactTables={setCompactTables} refreshSeconds={refreshSeconds} setRefreshSeconds={setRefreshSeconds} buyThreshold={buyThreshold} sellThreshold={sellThreshold} setBuyThreshold={setBuyThreshold} setSellThreshold={setSellThreshold} notify={notify} />}
        </main>
        <footer className="app-footer"><span>Northstar Portfolio Monitor <span className="footer-version">v1.0.4</span></span><span><ShieldCheck size={13} /> Secure workspace <i /> All prices are illustrative</span></footer>
      </div>

      <AnimatePresence>{drawer && <NotificationDrawer alerts={alerts} onClose={() => setDrawer(false)} onAcknowledge={acknowledge} />}</AnimatePresence>
      <AnimatePresence>{selected && <DetailPanel company={selected} alerts={alerts} buyThreshold={buyThreshold} sellThreshold={sellThreshold} onClose={() => setSelected(null)} />}</AnimatePresence>
      <AnimatePresence>{editing && <EditTargetModal company={editing} onClose={() => setEditing(null)} onSave={(buyTarget, sellTarget) => { setCompanies((items) => items.map((item) => item.id === editing.id ? { ...item, buyTarget, sellTarget } : item)); setEditing(null); notify("Target prices updated"); }} />}</AnimatePresence>
      <AnimatePresence>{toast && <motion.div className="toast" initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 8 }}><Check size={15} />{toast}</motion.div>}</AnimatePresence>
      {mobileNav && <button className="mobile-backdrop" aria-label="Close navigation" onClick={() => setMobileNav(false)} />}
    </div>
  );
}

function PageHeading({ eyebrow, title, description, action }: { eyebrow: string; title: string; description: string; action?: ReactNode }) {
  return <div className="page-heading"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{description}</p></div>{action && <div className="heading-actions">{action}</div>}</div>;
}

function MetricCard({ label, value, change, icon: Icon, tone, hint }: { label: string; value: string; change?: string; icon: typeof Users; tone: string; hint: string }) {
  return <motion.div className="metric-card" whileHover={{ y: -2 }} transition={{ duration: 0.15 }}>
    <div className="metric-top"><span>{label}</span><div className={`metric-icon ${tone}`}><Icon size={17} /></div></div>
    <div className="metric-value">{value}</div><div className="metric-foot">{change && <span className={tone === "green" ? "text-green" : tone === "red" ? "text-red" : "text-muted"}>{change}</span>}<span>{hint}</span></div>
  </motion.div>;
}

function chartData(companies: Company[], period = "1M") {
  const count = period === "1D" ? 20 : period === "1W" ? 28 : period === "1Y" ? 48 : 36;
  const base = companies.length ? companies.reduce((sum, company) => sum + company.price, 0) / companies.length : 1920;
  const averageChange = companies.length ? companies.reduce((sum, company) => sum + company.change, 0) / companies.length : 0;
  return Array.from({ length: count }, (_, i) => {
    const val = base * (1 + Math.sin(i * 0.3) * 0.018 + Math.cos(i * 0.13) * 0.013 + ((i / count) - 0.45) * (averageChange / 100));
    return { label: period === "1D" ? `${9 + Math.floor(i / 2)}:${i % 2 ? "30" : "00"}` : `${["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][i % 12]} ${4 + i}`, price: Math.round(val * 100) / 100 };
  });
}

function PriceChart({ company, portfolio, period, setPeriod, compact: small = false, targets = false }: { company?: Company; portfolio?: Company[]; period: string; setPeriod?: (v: string) => void; compact?: boolean; targets?: boolean }) {
  const data = useMemo(() => company ? historyFor(company, period) : chartData(portfolio ?? [], period), [company, portfolio, period]);
  const series = data;
  const values = series.map((point) => point.price);
  const minValue = Math.min(...values, ...(targets && company ? [company.buyTarget] : []));
  const maxValue = Math.max(...values, ...(targets && company ? [company.sellTarget] : []));
  const padding = (maxValue - minValue) * 0.08;
  const averageChange = portfolio?.length ? portfolio.reduce((sum, item) => sum + item.change, 0) / portfolio.length : 0;
  const color = (company?.change ?? averageChange) < 0 ? "#ef727b" : "#70a9ff";
  return <div className={`chart-wrap ${small ? "chart-small" : ""}`}>
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={series} margin={{ top: 8, right: 4, left: -22, bottom: 0 }}>
        <defs><linearGradient id="priceFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor={color} stopOpacity={0.2} /><stop offset="100%" stopColor={color} stopOpacity={0} /></linearGradient></defs>
        <CartesianGrid strokeDasharray="3 5" vertical={false} stroke="var(--chart-grid)" />
        <XAxis dataKey="label" tickLine={false} axisLine={false} tick={{ fill: "var(--muted)", fontSize: 10 }} minTickGap={34} />
        <YAxis orientation="right" tickLine={false} axisLine={false} tick={{ fill: "var(--muted)", fontSize: 10 }} tickFormatter={(v: number) => v >= 1000 ? `₹${(v / 1000).toFixed(1)}k` : `₹${Math.round(v)}`} domain={[minValue - padding, maxValue + padding]} />
        <Tooltip contentStyle={{ background: "var(--surface-raised)", border: "1px solid var(--border)", borderRadius: 8, color: "var(--text)", fontSize: 12 }} formatter={(v) => [money(Number(v)), "Price"]} />
        {targets && company && <><ReferenceLine y={company.buyTarget} stroke="#44c49a" strokeDasharray="5 4" label={{ value: "BUY", fill: "#44c49a", fontSize: 9, position: "insideTopLeft" }} /><ReferenceLine y={company.sellTarget} stroke="#e97880" strokeDasharray="5 4" label={{ value: "SELL", fill: "#e97880", fontSize: 9, position: "insideTopLeft" }} /></>}
        <Area type="monotone" dataKey={company ? "price" : "price"} stroke={color} strokeWidth={2} fill="url(#priceFill)" activeDot={{ r: 4, strokeWidth: 0 }} />
      </AreaChart>
    </ResponsiveContainer>
    {!small && setPeriod && <div className="chart-range">{["1D", "1W", "1M", "3M", "1Y"].map((range) => <button key={range} className={period === range ? "range-active" : ""} onClick={() => setPeriod(range)}>{range}</button>)}</div>}
  </div>;
}

function Overview({ companies, alerts, period, setPeriod, onCompany, onPage, buyThreshold, sellThreshold }: { companies: Company[]; alerts: PortfolioAlert[]; period: string; setPeriod: (v: string) => void; onCompany: (c: Company) => void; onPage: (p: Page) => void; buyThreshold: number; sellThreshold: number }) {
  const buyReached = companies.filter((c) => getStatus(c, buyThreshold, sellThreshold) === "Buy zone").length;
  const sellReached = companies.filter((c) => getStatus(c, buyThreshold, sellThreshold) === "Sell zone").length;
  const portfolioValue = companies.reduce((sum, company) => sum + company.price * 14, 0);
  const averageChange = companies.reduce((sum, company) => sum + company.change, 0) / companies.length;
  const buyClose = [...companies].sort((a, b) => Math.abs((a.price - a.buyTarget) / a.buyTarget) - Math.abs((b.price - b.buyTarget) / b.buyTarget)).slice(0, 5);
  const sellClose = [...companies].sort((a, b) => Math.abs((a.sellTarget - a.price) / a.sellTarget) - Math.abs((b.sellTarget - b.price) / b.sellTarget)).slice(0, 5);
  return <>
    <PageHeading eyebrow="THURSDAY, OCTOBER 15, 2026" title="Good morning, Srushti" description="Here's what's happening across your portfolio today." action={<button className="button secondary" onClick={() => onPage("Reports")}><Download size={15} /> Generate report</button>} />
    <div className="metric-grid">
      <MetricCard label="Companies tracked" value={String(companies.length)} icon={BriefcaseBusiness} tone="blue" hint="Across NSE & BSE" />
      <MetricCard label="Buy targets reached" value={String(buyReached)} change="+2 this week" icon={ArrowDownRight} tone="green" hint="Price at or below target" />
      <MetricCard label="Sell targets reached" value={String(sellReached)} change="1 new today" icon={ArrowUpRight} tone="red" hint="Price at or above target" />
      <MetricCard label="Market data status" value="Connected" change="09:30:04 IST" icon={Activity} tone="green" hint="NSE & BSE · Demo feed" />
    </div>
    <div className="overview-main-grid">
      <section className="card market-chart-card">
        <div className="section-head"><div><h2>Portfolio market overview</h2><p>Weighted portfolio performance · Demo prices</p></div><button className="select-button">NIFTY 50 <ChevronDown size={13} /></button></div>
        <div className="chart-summary"><b>{wholeINR(portfolioValue)}</b><span className={averageChange >= 0 ? "positive" : "negative"}>{averageChange >= 0 ? <TrendingUp size={14} /> : <TrendingDown size={14} />} {percent(averageChange)}</span><small>Illustrative portfolio value</small></div>
        <PriceChart portfolio={companies} period={period} setPeriod={setPeriod} compact />
      </section>
      <section className="card exchange-card">
        <div className="section-head"><div><h2>Market pulse</h2><p>Exchange snapshot · 09:30 IST</p></div><span className="live-pill"><span /> OPEN</span></div>
        <div className="exchange-row"><div className="exchange-logo nse-logo">N</div><div className="exchange-info"><b>NIFTY 50</b><small>NSE · 50 constituents</small></div><div className="exchange-value"><b>25,176.40</b><span className="positive">+0.72%</span></div></div>
        <div className="exchange-row"><div className="exchange-logo bse-logo">B</div><div className="exchange-info"><b>SENSEX</b><small>BSE · 30 constituents</small></div><div className="exchange-value"><b>82,634.18</b><span className="positive">+0.64%</span></div></div>
        <div className="market-status-foot"><span><i className="status-dot" /> NSE feed connected</span><span><i className="status-dot" /> BSE feed connected</span></div>
      </section>
    </div>
    <div className="overview-lower-grid">
      <section className="card target-list-card">
        <div className="section-head"><div><h2>Closest to buy targets</h2><p>Companies nearing their buy price</p></div><button className="text-link" onClick={() => onPage("Portfolio")}>View portfolio <ArrowRight size={13} /></button></div>
        <div className="mini-stock-list">{buyClose.map((company) => <MiniStock key={company.id} company={company} direction="buy" onClick={() => onCompany(company)} />)}</div>
      </section>
      <section className="card target-list-card">
        <div className="section-head"><div><h2>Closest to sell targets</h2><p>Companies nearing their sell price</p></div><button className="text-link" onClick={() => onPage("Target Management")}>Manage targets <ArrowRight size={13} /></button></div>
        <div className="mini-stock-list">{sellClose.map((company) => <MiniStock key={company.id} company={company} direction="sell" onClick={() => onCompany(company)} />)}</div>
      </section>
      <section className="card activity-card">
        <div className="section-head"><div><h2>Recent activity</h2><p>Latest portfolio alerts</p></div><button className="text-link" onClick={() => onPage("Price Alerts")}>All activity <ArrowRight size={13} /></button></div>
        <div className="activity-list">{alerts.slice(0, 4).map((alert) => <div className="activity-row" key={alert.id}><span className={`activity-marker ${alert.type.includes("Sell") ? "marker-red" : alert.type.includes("Buy") ? "marker-green" : "marker-amber"}`}><Activity size={12} /></span><div><b>{alert.company}</b><small>{alert.type}</small></div><time>{alert.time}</time></div>)}</div>
      </section>
    </div>
  </>;
}

function TargetProximityBar({ company }: { company: Company }) {
  const buyGap = Math.max(0, (company.price - company.buyTarget) / company.buyTarget * 100);
  const sellGap = Math.max(0, (company.sellTarget - company.price) / company.sellTarget * 100);
  const distance = Math.min(buyGap, sellGap);
  const progress = Math.max(5, Math.min(100, 100 - distance * 8));
  return <div className="proximity"><span className="proximity-track"><i style={{ width: `${progress}%`, background: distance < 3 ? "#e4a94f" : "#7185aa" }} /></span><small>{distance.toFixed(1)}%</small></div>;
}
function distanceLabel(price: number, target: number) {
  return `${Math.abs((price - target) / target * 100).toFixed(2)}% ${price >= target ? "above" : "below"}`;
}

function StatusBadge({ company, buyThreshold = 2, sellThreshold = 2 }: { company: Company; buyThreshold?: number; sellThreshold?: number }) {
  const status = getStatus(company, buyThreshold, sellThreshold);
  const cls = status === "Buy zone" ? "badge-buy" : status === "Sell zone" ? "badge-sell" : status === "Approaching" ? "badge-approaching" : "badge-neutral";
  return <span className={`status-badge ${cls}`}><i />{status}</span>;
}

function MiniStock({ company, direction, onClick }: { company: Company; direction: "buy" | "sell"; onClick: () => void }) {
  const target = direction === "buy" ? company.buyTarget : company.sellTarget;
  const gap = Math.abs((company.price - target) / target * 100);
  return <button className="mini-stock" onClick={onClick}><div className="mini-ticker">{company.symbol.slice(0, 2)}</div><div className="mini-stock-name"><b>{company.name}</b><small>{company.symbol} <i>·</i> {company.exchange}</small></div><div className="mini-proximity"><span className="proximity-track"><i style={{ width: `${Math.max(10, 100 - gap * 7)}%` }} /></span><small>{gap.toFixed(1)}% away</small></div><div className="mini-price"><b>{money(company.price)}</b><small className={company.change >= 0 ? "positive" : "negative"}>{percent(company.change)}</small></div></button>;
}

function Portfolio({ companies, onCompany, onEdit, onRefresh, search, notify, buyThreshold, sellThreshold }: { companies: Company[]; onCompany: (c: Company) => void; onEdit: (c: Company) => void; onRefresh: () => void; search: string; notify: (message: string) => void; buyThreshold: number; sellThreshold: number }) {
  const [exchange, setExchange] = useState("All exchanges");
  const [status, setStatus] = useState("All statuses");
  const [movement, setMovement] = useState("Any movement");
  const [sort, setSort] = useState("name");
  const [ascending, setAscending] = useState(true);
  const filtered = useMemo(() => companies.filter((c) => {
    const term = search.toLowerCase();
    return (!term || `${c.name} ${c.symbol}`.toLowerCase().includes(term)) && (exchange === "All exchanges" || c.exchange === exchange) && (status === "All statuses" || getStatus(c, buyThreshold, sellThreshold) === status) && (movement === "Any movement" || (movement === "Gainers" ? c.change > 0 : c.change < 0));
  }).sort((a, b) => {
    const result = sort === "name" ? a.name.localeCompare(b.name) : sort === "price" ? a.price - b.price : sort === "change" ? a.change - b.change : sort === "buy" ? Math.abs(a.price - a.buyTarget) - Math.abs(b.price - b.buyTarget) : Math.abs(a.sellTarget - a.price) - Math.abs(b.sellTarget - b.price);
    return ascending ? result : -result;
  }), [companies, search, exchange, status, movement, sort, ascending, buyThreshold, sellThreshold]);
  const chooseSort = (key: string) => { setAscending(sort === key ? !ascending : true); setSort(key); };
  return <>
    <PageHeading eyebrow="PORTFOLIO" title="Portfolio" description={`${companies.length} companies across NSE and BSE · Prices are illustrative demo data`} action={<button className="button primary" onClick={onRefresh}><RefreshCw size={14} /> Refresh prices</button>} />
    <section className="card portfolio-card">
      <div className="table-toolbar"><div className="table-toolbar-title"><div className="table-icon"><BriefcaseBusiness size={15} /></div><div><b>Tracked companies</b><small>{filtered.length} of {companies.length} companies</small></div></div><div className="table-filters"><label className="filter-search"><Search size={14} /><input value={search} readOnly placeholder="Search companies..." /></label><select aria-label="Exchange filter" value={exchange} onChange={(e) => setExchange(e.target.value)}><option>All exchanges</option><option>NSE</option><option>BSE</option></select><select aria-label="Status filter" value={status} onChange={(e) => setStatus(e.target.value)}><option>All statuses</option><option>Buy zone</option><option>Sell zone</option><option>Approaching</option><option>On track</option></select><select aria-label="Movement filter" value={movement} onChange={(e) => setMovement(e.target.value)}><option>Any movement</option><option>Gainers</option><option>Losers</option></select><button className="icon-button filter-button" title="Filters"><SlidersHorizontal size={15} /></button></div></div>
      <div className="table-scroll"><table className="portfolio-table"><thead><tr><th><button onClick={() => chooseSort("name")}>Company <ArrowDownUp size={11} /></button></th><th>Symbol</th><th>Exchange</th><th><button onClick={() => chooseSort("price")}>Market price <ArrowDownUp size={11} /></button></th><th><button onClick={() => chooseSort("change")}>Day change <ArrowDownUp size={11} /></button></th><th>Buy target</th><th><button onClick={() => chooseSort("buy")}>Buy distance <ArrowDownUp size={11} /></button></th><th>Sell target</th><th><button onClick={() => chooseSort("sell")}>Sell distance <ArrowDownUp size={11} /></button></th><th>Nearest target</th><th>Target status</th><th>Updated</th><th /></tr></thead>
        <tbody>{filtered.map((company) => <tr key={company.id} onClick={() => onCompany(company)}><td><div className="company-name-cell"><div className="company-avatar">{company.symbol.slice(0, 1)}</div><span><b>{company.name}</b><small>{company.sector}</small></span></div></td><td className="symbol-cell">{company.symbol}</td><td><span className={`exchange-tag ${company.exchange === "BSE" ? "bse-tag" : ""}`}>{company.exchange}</span></td><td className="price-cell">{money(company.price)}</td><td><span className={`change-value ${company.change >= 0 ? "positive" : "negative"}`}>{company.change >= 0 ? <ArrowUpRight size={12} /> : <ArrowDownRight size={12} />}{percent(company.change)}</span></td><td>{money(company.buyTarget)}</td><td className="distance-cell">{distanceLabel(company.price, company.buyTarget)}</td><td>{money(company.sellTarget)}</td><td className="distance-cell">{distanceLabel(company.price, company.sellTarget)}</td><td><TargetProximityBar company={company} /></td><td><StatusBadge company={company} buyThreshold={buyThreshold} sellThreshold={sellThreshold} /></td><td className="updated-cell">{company.updated}</td><td><button className="row-action" aria-label={`Edit ${company.symbol} targets`} onClick={(e) => { e.stopPropagation(); onEdit(company); }}><MoreHorizontal size={17} /></button></td></tr>)}</tbody>
      </table>{filtered.length === 0 && <div className="empty-state"><Search size={22} /><b>No companies found</b><span>Try adjusting your search or filters.</span></div>}</div>
      <div className="table-footer"><span>Showing <b>{filtered.length ? 1 : 0}–{filtered.length}</b> of <b>{filtered.length}</b> results <span className="footer-demo">· Demonstration prices</span></span><div className="pagination"><button disabled><ChevronLeft size={14} /></button><button className="page-current">1</button><span>of 1</span><button disabled><ChevronRight size={14} /></button></div></div>
    </section>
    <div className="table-note"><i className="status-dot" /> Simulated market feed <span>·</span> Last update 09:30:04 IST <span>·</span> Data may not reflect actual market prices</div>
    <div className="table-bottom-controls"><button className="button secondary" onClick={onRefresh}><RefreshCw size={14} /> Refresh data</button><button className="button quiet" onClick={() => notify("Portfolio export prepared")}>Export view <Download size={14} /></button></div>
  </>;
}

function AlertsPage({ alerts, onAcknowledge }: { alerts: PortfolioAlert[]; onAcknowledge: (id: string) => void }) {
  const [type, setType] = useState("All alert types");
  const [state, setState] = useState("All statuses");
  const [query, setQuery] = useState("");
  const [mail, setMail] = useState(true);
  const shown = alerts.filter((a) => (type === "All alert types" || a.type === type) && (state === "All statuses" || a.status === state) && `${a.company} ${a.symbol}`.toLowerCase().includes(query.toLowerCase()));
  return <>
    <PageHeading eyebrow="MONITORING CENTER" title="Price alerts" description="Monitor target events and manage alert acknowledgement." action={<button className={`button ${mail ? "secondary" : "quiet"}`} onClick={() => setMail(!mail)}><span className={`mini-toggle ${mail ? "toggle-on" : ""}`} /> Email notifications {mail ? "on" : "off"}</button>} />
    <div className="alert-summary-row"><div className="alert-summary"><span className="summary-dot red" /><div><b>{alerts.filter((a) => a.status === "New").length}</b><small>New alerts</small></div></div><div className="alert-summary"><span className="summary-dot amber" /><div><b>{alerts.filter((a) => a.type === "Approaching target").length}</b><small>Approaching targets</small></div></div><div className="alert-summary"><span className="summary-dot blue" /><div><b>{alerts.filter((a) => a.status === "Acknowledged").length}</b><small>Acknowledged</small></div></div><div className="alert-summary"><span className="summary-dot green" /><div><b>{alerts.filter((a) => a.status === "Resolved").length}</b><small>Resolved today</small></div></div></div>
    <section className="card alerts-card"><div className="table-toolbar"><div className="table-toolbar-title"><div className="table-icon amber-icon"><Bell size={15} /></div><div><b>Alert center</b><small>{shown.length} alerts · Demo notifications only</small></div></div><div className="table-filters"><label className="filter-search"><Search size={14} /><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search alerts..." /></label><select value={type} onChange={(e) => setType(e.target.value)}><option>All alert types</option>{["Buy target reached", "Sell target reached", "Approaching target", "Stale price", "Feed interruption"].map((x) => <option key={x}>{x}</option>)}</select><select value={state} onChange={(e) => setState(e.target.value)}><option>All statuses</option><option>New</option><option>Acknowledged</option><option>Resolved</option></select></div></div>
      <div className="table-scroll"><table className="alerts-table"><thead><tr><th>Alert</th><th>Company</th><th>Current price</th><th>Target price</th><th>Time</th><th>Email status</th><th>Status</th><th /></tr></thead><tbody>{shown.map((a) => <tr key={a.id} className={a.status === "New" ? "alert-new-row" : ""}><td><span className={`alert-type-icon ${a.type.includes("Sell") ? "alert-red" : a.type.includes("Buy") ? "alert-green" : a.type.includes("Stale") || a.type.includes("Feed") ? "alert-amber" : "alert-blue"}`}><Zap size={13} /></span><b>{a.type}</b></td><td><b>{a.company}</b><small className="cell-sub">{a.symbol}</small></td><td>{money(a.price)}</td><td>{money(a.target)}</td><td className="updated-cell">{a.time}</td><td><span className={`email-status ${a.channel.toLowerCase()}`}><i />{a.channel}</span></td><td><span className={`alert-state state-${a.status.toLowerCase()}`}>{a.status}</span></td><td>{a.status === "New" && <button className="ack-button" onClick={() => onAcknowledge(a.id)}><CheckCheck size={13} /> Acknowledge</button>}</td></tr>)}</tbody></table>{shown.length === 0 && <div className="empty-state"><Bell size={22} /><b>No alerts match</b><span>Try another filter.</span></div>}</div>
    </section>
    <div className="info-note"><ShieldCheck size={15} /><span><b>Demo notification delivery</b> — Email status is simulated in this preview. No messages are sent.</span></div>
  </>;
}

function TargetManagement({ companies, onEdit, search, buyThreshold, sellThreshold }: { companies: Company[]; onEdit: (c: Company) => void; search: string; buyThreshold: number; sellThreshold: number }) {
  const [filter, setFilter] = useState("All companies");
  const list = companies.filter((c) => `${c.name} ${c.symbol}`.toLowerCase().includes(search.toLowerCase()) && (filter === "All companies" || getStatus(c, buyThreshold, sellThreshold) === filter));
  const buyCount = companies.filter((c) => getStatus(c, buyThreshold, sellThreshold) === "Buy zone").length;
  const sellCount = companies.filter((c) => getStatus(c, buyThreshold, sellThreshold) === "Sell zone").length;
  return <>
    <PageHeading eyebrow="MONITORING" title="Target management" description="Review and adjust portfolio buy and sell target prices." action={<button className="button primary" onClick={() => onEdit(companies[0])}><Plus size={15} /> Edit targets</button>} />
    <div className="target-stat-grid"><div className="card target-stat"><span className="metric-icon green"><ArrowDownRight size={16} /></span><div><small>Buy targets in range</small><b>{buyCount}</b></div><span className="target-stat-note">≤{buyThreshold}% from target</span></div><div className="card target-stat"><span className="metric-icon red"><ArrowUpRight size={16} /></span><div><small>Sell targets in range</small><b>{sellCount}</b></div><span className="target-stat-note">≤{sellThreshold}% from target</span></div><div className="card target-stat"><span className="metric-icon amber"><Target size={16} /></span><div><small>Approaching a target</small><b>{companies.filter((c) => getStatus(c, buyThreshold, sellThreshold) === "Approaching").length}</b></div><span className="target-stat-note">Within 7% of target</span></div></div>
    <section className="card portfolio-card target-table-card"><div className="table-toolbar"><div className="table-toolbar-title"><div className="table-icon"><Target size={15} /></div><div><b>Target prices</b><small>Local demo changes only</small></div></div><select className="standalone-select" value={filter} onChange={(e) => setFilter(e.target.value)}><option>All companies</option><option>Buy zone</option><option>Sell zone</option><option>Approaching</option><option>On track</option></select></div>
      <div className="table-scroll"><table className="portfolio-table target-table"><thead><tr><th>Company</th><th>Symbol</th><th>Current price</th><th>Buy target</th><th>Distance</th><th>Sell target</th><th>Distance</th><th>Status</th><th /></tr></thead><tbody>{list.map((c) => { const buyGap = ((c.price - c.buyTarget) / c.buyTarget) * 100; const sellGap = ((c.sellTarget - c.price) / c.sellTarget) * 100; return <tr key={c.id}><td><div className="company-name-cell"><div className="company-avatar">{c.symbol[0]}</div><span><b>{c.name}</b><small>{c.sector}</small></span></div></td><td className="symbol-cell">{c.symbol}</td><td className="price-cell">{money(c.price)}</td><td className="target-buy-price">{money(c.buyTarget)}</td><td className="target-distance-cell">{Math.abs(buyGap).toFixed(1)}% {buyGap >= 0 ? "above" : "below"}</td><td className="target-sell-price">{money(c.sellTarget)}</td><td className="target-distance-cell">{Math.abs(sellGap).toFixed(1)}% {sellGap >= 0 ? "below" : "above"}</td><td><StatusBadge company={c} buyThreshold={buyThreshold} sellThreshold={sellThreshold} /></td><td><button className="button tiny secondary" onClick={() => onEdit(c)}>Edit <ArrowRight size={12} /></button></td></tr>; })}</tbody></table>{list.length === 0 && <div className="empty-state"><Target size={22} /><b>No companies found</b></div>}</div>
    </section>
  </>;
}

function MarketActivity({ companies, connection, setConnection, onCompany }: { companies: Company[]; connection: boolean; setConnection: (v: boolean) => void; onCompany: (c: Company) => void }) {
  const latest = [...companies].sort((a, b) => Math.abs(b.change) - Math.abs(a.change)).slice(0, 10);
  const near = [...companies].sort((a, b) => Math.min(Math.abs(a.price - a.buyTarget) / a.buyTarget, Math.abs(a.sellTarget - a.price) / a.sellTarget) - Math.min(Math.abs(b.price - b.buyTarget) / b.buyTarget, Math.abs(b.sellTarget - b.price) / b.sellTarget)).slice(0, 5);
  const [filter, setFilter] = useState("All exchanges");
  return <>
    <PageHeading eyebrow="MARKET MONITOR" title="Market activity" description="Simulated price movement and exchange feed monitoring." action={<button className={`button ${connection ? "secondary" : "primary"}`} onClick={() => setConnection(!connection)}>{connection ? <><RefreshCw size={14} /> Simulate interruption</> : <><Activity size={14} /> Reconnect feed</>}</button>} />
    <div className={`feed-banner ${connection ? "" : "feed-error"}`}><div className="feed-main-icon">{connection ? <Activity size={18} /> : <TrendingDown size={18} />}</div><div><b>{connection ? "Demo market feed connected" : "Market feed interrupted"}</b><small>{connection ? "NSE and BSE simulated prices are updating normally." : "No new prices received. Displayed prices may be stale."}</small></div><div className="feed-banner-right"><span className={`feed-status ${connection ? "feed-on" : "feed-off"}`}><i />{connection ? "CONNECTED" : "INTERRUPTED"}</span><small>Last received: {connection ? "09:30:04 IST" : "09:12:46 IST"}</small></div></div>
    <div className="market-stat-grid"><div className="card market-stat"><small>NIFTY 50</small><b>25,176.40 <span className="positive">+0.72%</span></b><div className="stat-spark"><Sparkline /></div></div><div className="card market-stat"><small>SENSEX</small><b>82,634.18 <span className="positive">+0.64%</span></b><div className="stat-spark"><Sparkline /></div></div><div className="card market-stat"><small>NSE tracked</small><b>{companies.filter((c) => c.exchange === "NSE").length} <span className="market-stat-sub">companies</span></b><small className="market-uptime"><i className="status-dot" /> Feed healthy</small></div><div className="card market-stat"><small>BSE tracked</small><b>{companies.filter((c) => c.exchange === "BSE").length} <span className="market-stat-sub">companies</span></b><small className="market-uptime"><i className="status-dot" /> Feed healthy</small></div></div>
    <div className="market-content-grid"><section className="card market-movers"><div className="section-head"><div><h2>Latest price changes</h2><p>Largest simulated moves today</p></div><select className="compact-select" value={filter} onChange={(e) => setFilter(e.target.value)}><option>All exchanges</option><option>NSE</option><option>BSE</option></select></div><div className="movers-list">{latest.filter((c) => filter === "All exchanges" || c.exchange === filter).map((c) => <button className="mover-row" key={c.id} onClick={() => onCompany(c)}><div className={`mover-arrow ${c.change >= 0 ? "mover-up" : "mover-down"}`}>{c.change >= 0 ? <ArrowUpRight size={14} /> : <ArrowDownRight size={14} />}</div><div className="mover-company"><b>{c.symbol}</b><small>{c.name}</small></div><span className="mover-exchange">{c.exchange}</span><b className="mover-price">{money(c.price)}</b><span className={c.change >= 0 ? "positive" : "negative"}>{percent(c.change)}</span></button>)}</div></section>
      <section className="card market-near"><div className="section-head"><div><h2>Approaching targets</h2><p>Nearest price targets</p></div><Target size={16} className="muted-icon" /></div>{near.map((c) => <button className="near-row" key={c.id} onClick={() => onCompany(c)}><div><b>{c.symbol}</b><small>{c.name}</small></div><div><TargetProximityBar company={c} /></div></button>)}</section></div>
  </>;
}
function Sparkline() { return <svg viewBox="0 0 120 28" preserveAspectRatio="none" aria-label="Market trend"><path d="M0 22 C12 18 15 20 25 13 S42 18 50 11 S62 14 72 9 S85 16 95 8 S108 11 120 3" fill="none" stroke="#65c5a2" strokeWidth="2" /></svg>; }

function Reports({ companies, alerts, notify, buyThreshold, sellThreshold }: { companies: Company[]; alerts: PortfolioAlert[]; notify: (message: string) => void; buyThreshold: number; sellThreshold: number }) {
  const [exchange, setExchange] = useState("All exchanges");
  const [period, setPeriod] = useState("Today");
  const rows = companies.filter((c) => exchange === "All exchanges" || c.exchange === exchange);
  const exportCsv = () => {
    const header = ["Company", "Symbol", "Exchange", "Current price (INR)", "Daily change (%)", "Buy target", "Sell target", "Target status"];
    const body = rows.map((c) => [c.name, c.symbol, c.exchange, c.price, c.change, c.buyTarget, c.sellTarget, getStatus(c, buyThreshold, sellThreshold)]);
    const csv = [header, ...body].map((line) => line.map((cell) => `"${String(cell).replace(/"/g, '""')}"`).join(",")).join("\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    const link = document.createElement("a"); link.href = url; link.download = "northstar-portfolio-report.csv"; link.click(); URL.revokeObjectURL(url);
    notify("CSV report exported");
  };
  return <>
    <PageHeading eyebrow="INSIGHTS & EXPORTS" title="Reports" description="Portfolio summaries built from illustrative market data." action={<button className="button primary" onClick={exportCsv}><Download size={14} /> Export CSV</button>} />
    <section className="card report-filters"><div><small>REPORT PERIOD</small><select value={period} onChange={(e) => setPeriod(e.target.value)}><option>Today</option><option>This week</option><option>This month</option><option>Custom range</option></select></div><div><small>EXCHANGE</small><select value={exchange} onChange={(e) => setExchange(e.target.value)}><option>All exchanges</option><option>NSE</option><option>BSE</option></select></div><div><small>AS OF</small><b>15 October 2026</b></div><div className="report-filter-spacer" /><button className="button secondary" onClick={() => window.print()}><Download size={14} /> Print report</button></section>
    <div className="report-summary-grid"><div className="card report-summary"><span className="report-summary-icon blue"><BriefcaseBusiness size={17} /></span><small>Companies monitored</small><b>{rows.length}</b><span>Across selected exchanges</span></div><div className="card report-summary"><span className="report-summary-icon green"><ArrowDownRight size={17} /></span><small>Buy target alerts</small><b>{alerts.filter((a) => a.type.includes("Buy")).length}</b><span>In selected period</span></div><div className="card report-summary"><span className="report-summary-icon red"><ArrowUpRight size={17} /></span><small>Sell target alerts</small><b>{alerts.filter((a) => a.type.includes("Sell")).length}</b><span>In selected period</span></div><div className="card report-summary"><span className="report-summary-icon amber"><Activity size={17} /></span><small>Market availability</small><b>99.2%</b><span>Simulated feed uptime</span></div></div>
    <section className="card report-chart-card"><div className="section-head"><div><h2>Daily portfolio summary</h2><p>Performance overview · {period}</p></div><span className="report-date-tag">DEMO REPORT</span></div><div className="report-chart"><PriceChart portfolio={companies} period="1M" compact /></div></section>
    <section className="card portfolio-card report-table-card"><div className="section-head report-table-heading"><div><h2>Companies closest to targets</h2><p>Ranked by proximity to nearest target</p></div></div><div className="table-scroll"><table className="portfolio-table"><thead><tr><th>Company</th><th>Symbol</th><th>Exchange</th><th>Market price</th><th>Buy target</th><th>Sell target</th><th>Nearest target</th><th>Status</th></tr></thead><tbody>{[...rows].sort((a, b) => Math.min(Math.abs(a.price - a.buyTarget) / a.buyTarget, Math.abs(a.sellTarget - a.price) / a.sellTarget) - Math.min(Math.abs(b.price - b.buyTarget) / b.buyTarget, Math.abs(b.sellTarget - b.price) / b.sellTarget)).slice(0, 8).map((c) => <tr key={c.id}><td><b>{c.name}</b></td><td className="symbol-cell">{c.symbol}</td><td>{c.exchange}</td><td>{money(c.price)}</td><td>{money(c.buyTarget)}</td><td>{money(c.sellTarget)}</td><td><TargetProximityBar company={c} /></td><td><StatusBadge company={c} buyThreshold={buyThreshold} sellThreshold={sellThreshold} /></td></tr>)}</tbody></table></div></section>
    <div className="info-note"><ShieldCheck size={15} /><span>Reports are generated from mock data for demonstration. No information is sent to external services.</span></div>
  </>;
}

interface ImportPreviewRow {
  company: string;
  symbol: string;
  exchange: string;
  buyTarget: number;
  sellTarget: number;
  currentBuy: number | null;
  currentSell: number | null;
  errors: string[];
}

function UploadPage({ companies, setCompanies, notify }: { companies: Company[]; setCompanies: React.Dispatch<React.SetStateAction<Company[]>>; notify: (message: string) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [validated, setValidated] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [previewRows, setPreviewRows] = useState<ImportPreviewRow[]>([]);
  const [uploadRecord, setUploadRecord] = useState<{ name: string; count: number } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const chooseFile = (next: File | undefined) => {
    if (!next) return;
    if (!/\.(xlsx|csv)$/i.test(next.name)) { notify("Choose an .xlsx or .csv file"); return; }
    if (next.size > 10 * 1024 * 1024) { notify("File exceeds the 10 MB limit"); return; }
    setFile(next); setValidated(false); setPreviewRows([]);
  };
  const onDrop = (e: DragEvent<HTMLDivElement>) => { e.preventDefault(); setDragging(false); chooseFile(e.dataTransfer.files[0]); };
  const sampleRows = companies.slice(0, 4);
  const validate = async () => {
    if (!file) { notify("Choose a .xlsx or .csv file first"); return; }
    try {
      const isCsv = /\.csv$/i.test(file.name);
      const values: unknown[][] = isCsv
        ? parseCsv(await file.text())
        : [["Company", "Symbol", "Exchange", "Buy Target", "Sell Target"], ...companies.slice(0, 4).map((company, index) => [company.name, company.symbol, company.exchange, company.buyTarget + (index + 1) * 20, company.sellTarget + (index + 1) * 25])];
      if (values.length < 2) { notify("The selected file has no target rows"); return; }
      const headers = (values[0] ?? []).map((value) => String(value).trim().toLowerCase().replace(/[_\s-]+/g, ""));
      const column = (...names: string[]) => headers.findIndex((header) => names.includes(header));
      const symbolCol = column("symbol", "stocksymbol", "ticker");
      const exchangeCol = column("exchange");
      const buyCol = column("buytarget", "targetbuyprice", "buyprice");
      const sellCol = column("selltarget", "targetsellprice", "sellprice");
      const companyCol = column("company", "companyname", "name");
      if ([symbolCol, exchangeCol, buyCol, sellCol].some((index) => index < 0)) {
        notify("Required columns: Symbol, Exchange, Buy Target and Sell Target");
        return;
      }
      const data = values.slice(1).filter((row) => row.some((cell) => String(cell).trim() !== ""));
      if (data.length === 0) { notify("The selected file has no target rows"); return; }
      const seen = new Set<string>();
      const parsed = data.map((row): ImportPreviewRow => {
        const symbol = String(row[symbolCol] ?? "").trim().toUpperCase();
        const exchange = String(row[exchangeCol] ?? "").trim().toUpperCase();
        const buyTarget = Number(row[buyCol]);
        const sellTarget = Number(row[sellCol]);
        const existing = companies.find((company) => company.symbol.toUpperCase() === symbol);
        const errors: string[] = [];
        if (!symbol) errors.push("Missing symbol");
        if (symbol && seen.has(symbol)) errors.push("Duplicate symbol");
        if (symbol) seen.add(symbol);
        if (exchange !== "NSE" && exchange !== "BSE") errors.push("Unsupported exchange");
        if (!Number.isFinite(buyTarget) || buyTarget <= 0) errors.push("Invalid buy price");
        if (!Number.isFinite(sellTarget) || sellTarget <= 0 || sellTarget <= buyTarget) errors.push("Invalid sell price");
        if (symbol && !existing) errors.push("Symbol is not in the tracked portfolio");
        return {
          company: String((companyCol >= 0 ? row[companyCol] : existing?.name) ?? "").trim() || existing?.name || "—",
          symbol, exchange, buyTarget, sellTarget,
          currentBuy: existing?.buyTarget ?? null, currentSell: existing?.sellTarget ?? null, errors,
        };
      });
      setPreviewRows(parsed);
      setValidated(true);
      const errorCount = parsed.reduce((total, row) => total + row.errors.length, 0);
      notify(errorCount ? `Validation found ${errorCount} issue${errorCount === 1 ? "" : "s"}` : `Validation complete · ${parsed.length} rows ready${isCsv ? "" : " (simulated XLSX preview)"}`);
    } catch {
      notify("Could not read this file. Check that it is a valid .xlsx or .csv file.");
    }
  };
  const errorCount = previewRows.reduce((total, row) => total + row.errors.length, 0);
  const confirm = () => {
    if (!validated || errorCount > 0 || !file) return;
    const validRows = previewRows;
    setCompanies((current) => current.map((company) => {
      const update = validRows.find((row) => row.symbol === company.symbol && row.errors.length === 0);
      return update ? { ...company, buyTarget: update.buyTarget, sellTarget: update.sellTarget } : company;
    }));
    setUploadRecord({ name: file.name, count: validRows.length });
    setFile(null); setValidated(false); setPreviewRows([]);
    if (inputRef.current) inputRef.current.value = "";
    notify("Demo import confirmed · changes apply in this session only");
  };
  const downloadTemplate = () => {
    const csv = "Company,Symbol,Exchange,Buy Target,Sell Target\nReliance Industries,RELIANCE,NSE,2780,3180\nHDFC Bank,HDFCBANK,NSE,1575,1820\n";
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" })); const a = document.createElement("a"); a.href = url; a.download = "northstar-target-template.csv"; a.click(); URL.revokeObjectURL(url);
  };
  return <>
    <PageHeading eyebrow="DATA MANAGEMENT" title="Upload targets" description="Import manager-provided target updates from an Excel or CSV file." action={<button className="button secondary" onClick={downloadTemplate}><Download size={14} /> Download template</button>} />
    <div className="upload-layout"><div className="upload-main">
      <section className="card upload-card"><div className="section-head"><div><h2>Upload a file</h2><p>Import a new target list into this demo workspace.</p></div><span className="local-only-tag">LOCAL DEMO</span></div>
        <div className={`dropzone ${dragging ? "dropzone-active" : ""}`} onDragOver={(e) => { e.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={onDrop}><input ref={inputRef} type="file" accept=".xlsx,.csv" hidden onChange={(e: ChangeEvent<HTMLInputElement>) => chooseFile(e.target.files?.[0])} /><div className="upload-icon"><CloudDownload size={21} /></div><b>Drop your file here, or <button onClick={() => inputRef.current?.click()}>browse</button></b><small>Excel (.xlsx) or CSV · Maximum file size 10 MB</small></div>
        {file && <div className="selected-file"><div className="file-icon"><FileSpreadsheet size={17} /></div><div><b>{file.name}</b><small>{(file.size / 1024).toFixed(1)} KB · {validated ? `${previewRows.length} rows checked` : "Ready to validate"}</small></div><span className={`file-state ${validated && errorCount === 0 ? "file-success" : ""}`}>{validated ? errorCount ? `${errorCount} issues` : <><Check size={12} /> Validated</> : "Selected"}</span><button className="icon-button" aria-label="Remove file" onClick={() => { setFile(null); setValidated(false); setPreviewRows([]); if (inputRef.current) inputRef.current.value = ""; }}><X size={15} /></button></div>}
        <div className="upload-actions"><button className="button secondary" onClick={() => { setFile(null); setValidated(false); setPreviewRows([]); if (inputRef.current) inputRef.current.value = ""; }}>Cancel</button><button className="button primary" disabled={validated && errorCount > 0} onClick={validated ? confirm : validate}>{validated ? <><Check size={14} /> Confirm import</> : <><ShieldCheck size={14} /> Validate file</>}</button></div>
      </section>
      <section className="card import-preview"><div className="section-head"><div><h2>{validated ? "Import preview" : "Preview format"}</h2><p>{validated ? `${previewRows.length} rows · Compare existing and incoming targets.` : "A preview of the data columns we expect."}</p></div>{validated && <span className={errorCount ? "validation-errors" : "validation-ok"}>{errorCount ? `${errorCount} validation issues` : <><Check size={12} /> No validation errors</>}</span>}</div><div className="table-scroll"><table className="portfolio-table"><thead><tr><th>Company</th><th>Symbol</th><th>Exchange</th><th>Current buy target</th><th>New buy target</th><th>Current sell target</th><th>New sell target</th><th>Validation</th></tr></thead><tbody>{validated ? previewRows.map((row, i) => <tr key={`${row.symbol}-${i}`}><td><b>{row.company}</b></td><td className="symbol-cell">{row.symbol || "—"}</td><td>{row.exchange || "—"}</td><td>{row.currentBuy == null ? "—" : money(row.currentBuy)}</td><td className={row.errors.length ? "preview-error" : "preview-new"}>{Number.isFinite(row.buyTarget) && row.buyTarget > 0 ? money(row.buyTarget) : "Invalid"}</td><td>{row.currentSell == null ? "—" : money(row.currentSell)}</td><td className={row.errors.length ? "preview-error" : "preview-new"}>{Number.isFinite(row.sellTarget) && row.sellTarget > 0 ? money(row.sellTarget) : "Invalid"}</td><td className={row.errors.length ? "preview-error" : "validation-ok"}>{row.errors.length ? row.errors.join(", ") : "Valid"}</td></tr>) : sampleRows.map((c) => <tr key={c.id}><td><b>{c.name}</b></td><td className="symbol-cell">{c.symbol}</td><td>{c.exchange}</td><td>{money(c.buyTarget)}</td><td>—</td><td>{money(c.sellTarget)}</td><td>—</td><td><span className="muted-icon">Awaiting file</span></td></tr>)}</tbody></table>{validated && previewRows.length === 0 && <div className="empty-state"><FileSpreadsheet size={22} /><b>No target rows</b></div>}</div><div className="preview-note"><CircleHelp size={14} /> CSV contents are parsed in-browser. XLSX validation uses simulated sample rows in this demo. Confirming only updates this local session — no server or database is used.</div></section>
    </div><aside className="upload-aside"><section className="card upload-checklist"><h3>Before you upload</h3><div className="checklist-item"><Check size={13} /><span>Include company symbols in a <b>Symbol</b> column</span></div><div className="checklist-item"><Check size={13} /><span>Use valid NSE or BSE exchanges</span></div><div className="checklist-item"><Check size={13} /><span>Target prices must be greater than zero</span></div><div className="checklist-item"><Check size={13} /><span>Remove duplicate symbol rows</span></div><button className="text-link" onClick={downloadTemplate}>Download sample template <ArrowRight size={13} /></button></section><section className="card upload-history"><div className="section-head"><div><h3>Upload history</h3><p>Recent demo activity</p></div><Clock3 size={15} className="muted-icon" /></div>{uploadRecord ? <div className="history-entry"><div className="file-icon"><FileSpreadsheet size={15} /></div><div><b>{uploadRecord.name}</b><small>{uploadRecord.count} rows · Just now · Srushti Kadam</small></div><span className="history-imported">Imported</span></div> : <div className="upload-empty"><FileSpreadsheet size={20} /><span>No uploads yet</span></div>}</section></aside></div>
  </>;
}

function SettingsPage({ dark, setDark, compactTables, setCompactTables, refreshSeconds, setRefreshSeconds, buyThreshold, sellThreshold, setBuyThreshold, setSellThreshold, notify }: { dark: boolean; setDark: (v: boolean) => void; compactTables: boolean; setCompactTables: (v: boolean) => void; refreshSeconds: number; setRefreshSeconds: (v: number) => void; buyThreshold: number; sellThreshold: number; setBuyThreshold: (v: number) => void; setSellThreshold: (v: number) => void; notify: (message: string) => void }) {
  const [email, setEmail] = useState(true); const [approaching, setApproaching] = useState(true); const [ack, setAck] = useState(false);
  const toggle = (value: boolean, setter: (v: boolean) => void) => <button className={`switch ${value ? "switch-on" : ""}`} role="switch" aria-checked={value} onClick={() => setter(!value)}><i /></button>;
  const row = (title: string, detail: string, control: ReactNode) => <div className="setting-row"><div><b>{title}</b><small>{detail}</small></div>{control}</div>;
  return <>
    <PageHeading eyebrow="PREFERENCES" title="Settings" description="Configure your local dashboard and alert preferences." action={<span className="local-only-tag"><ShieldCheck size={12} /> LOCAL DEMO SETTINGS</span>} />
    <div className="settings-layout"><div className="settings-main">
      <section className="card settings-card"><div className="settings-section-title"><div className="settings-section-icon"><Sun size={16} /></div><div><h2>Appearance</h2><p>Personalize your dashboard experience.</p></div></div>
        {row("Color theme", "Choose the dashboard appearance.", <div className="theme-options"><button className={!dark ? "theme-option selected" : "theme-option"} onClick={() => setDark(false)}><Sun size={14} /> Light</button><button className={dark ? "theme-option selected" : "theme-option"} onClick={() => setDark(true)}><Moon size={14} /> Dark</button></div>)}
        {row("Compact data tables", "Show more rows with reduced spacing.", toggle(compactTables, setCompactTables))}
      </section>
      <section className="card settings-card"><div className="settings-section-title"><div className="settings-section-icon green"><Target size={16} /></div><div><h2>Target monitoring</h2><p>Set how close a price should be to trigger an alert.</p></div></div>
        {row("Buy target proximity", "Update the buy-zone threshold across portfolio statuses.", <div className="number-input"><input type="number" min="0.5" max="20" step="0.5" value={buyThreshold} onChange={(e) => setBuyThreshold(Math.max(0.5, Math.min(20, Number(e.target.value))))} /><span>%</span></div>)}
        {row("Sell target proximity", "Update the sell-zone threshold across portfolio statuses.", <div className="number-input"><input type="number" min="0.5" max="20" step="0.5" value={sellThreshold} onChange={(e) => setSellThreshold(Math.max(0.5, Math.min(20, Number(e.target.value))))} /><span>%</span></div>)}
        {row("Alert on approach", "Notify when companies are nearing a target.", toggle(approaching, setApproaching))}
      </section>
      <section className="card settings-card"><div className="settings-section-title"><div className="settings-section-icon amber"><Bell size={16} /></div><div><h2>Notifications & market data</h2><p>Configure simulated delivery and feed preferences.</p></div></div>
        {row("Email notifications", "Demo delivery status only. No email will be sent.", toggle(email, setEmail))}
        {row("Acknowledge to resolve", "Require acknowledgement before closing an alert.", toggle(ack, setAck))}
        {row("Refresh simulation interval", "How often the demo feed refreshes prices.", <select className="setting-select" value={refreshSeconds} onChange={(e) => setRefreshSeconds(Number(e.target.value))}><option value={10}>10 seconds</option><option value={30}>30 seconds</option><option value={60}>1 minute</option><option value={300}>5 minutes</option></select>)}
      </section>
      <button className="button primary settings-save" onClick={() => notify("Preferences saved in this demo session")}>Save preferences</button>
    </div><aside className="settings-aside"><section className="card profile-card"><div className="profile-large">SK</div><div><b>Srushti Kadam</b><span>Portfolio Manager</span></div><div className="profile-meta"><span>WORKSPACE</span><b>Plutus Wealth Management</b><span>ROLE</span><b>Investment Manager</b></div><span className="role-tag"><ShieldCheck size={12} /> Manager access</span></section><section className="card local-settings-note"><div className="local-note-icon"><Globe2 size={16} /></div><b>Local demo preferences</b><p>Changes made here apply only in this browser session. No settings are sent to a server or saved to a database.</p><span>DEMO ENVIRONMENT</span></section></aside></div>
  </>;
}

function NotificationDrawer({ alerts, onClose, onAcknowledge }: { alerts: PortfolioAlert[]; onClose: () => void; onAcknowledge: (id: string) => void }) {
  const [unreadOnly, setUnreadOnly] = useState(false);
  const visibleAlerts = unreadOnly ? alerts.filter((a) => a.status === "New") : alerts;
  return <><motion.button className="drawer-backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} aria-label="Close notifications" /><motion.aside className="notification-drawer" initial={{ x: 420 }} animate={{ x: 0 }} exit={{ x: 420 }} transition={{ type: "spring", damping: 28, stiffness: 260 }}><div className="drawer-head"><div><h2>Notifications</h2><p>{alerts.filter((a) => a.status === "New").length} unread alerts</p></div><button className="icon-button" onClick={onClose} aria-label="Close drawer"><X size={18} /></button></div><div className="drawer-tabs"><button className={!unreadOnly ? "drawer-tab-active" : ""} onClick={() => setUnreadOnly(false)}>All alerts</button><button className={unreadOnly ? "drawer-tab-active" : ""} onClick={() => setUnreadOnly(true)}>Unread <span>{alerts.filter((a) => a.status === "New").length}</span></button><button className="mark-all" onClick={() => alerts.filter((a) => a.status === "New").forEach((a) => onAcknowledge(a.id))}><CheckCheck size={13} /> Mark all read</button></div><div className="drawer-content">{visibleAlerts.length ? visibleAlerts.map((a) => <div className={`drawer-alert ${a.status === "New" ? "drawer-alert-new" : ""}`} key={a.id}><div className={`drawer-alert-icon ${a.type.includes("Sell") ? "alert-red" : a.type.includes("Buy") ? "alert-green" : "alert-amber"}`}><Zap size={14} /></div><div className="drawer-alert-content"><div className="drawer-alert-title"><b>{a.type}</b>{a.status === "New" && <i />}</div><p><strong>{a.symbol}</strong> · {a.company}</p><div className="drawer-alert-meta"><span>{money(a.price)}</span><span>Target {money(a.target)}</span></div><div className="drawer-alert-time">{a.time}{a.status === "New" && <button onClick={() => onAcknowledge(a.id)}>Acknowledge</button>}</div></div></div>) : <div className="empty-state"><CheckCheck size={22} /><b>No unread alerts</b><span>You're all caught up.</span></div>}</div><div className="drawer-footer"><span><ShieldCheck size={13} /> Demo alerts · Email delivery simulated</span></div></motion.aside></>;
}

function DetailPanel({ company, alerts, buyThreshold, sellThreshold, onClose }: { company: Company; alerts: PortfolioAlert[]; buyThreshold: number; sellThreshold: number; onClose: () => void }) {
  const [period, setPeriod] = useState("1M");
  const ownAlerts = alerts.filter((a) => a.symbol === company.symbol);
  const buyDistance = ((company.price - company.buyTarget) / company.buyTarget) * 100;
  const sellDistance = ((company.sellTarget - company.price) / company.sellTarget) * 100;
  return <><motion.button className="drawer-backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} aria-label="Close stock detail" /><motion.aside className="detail-panel" initial={{ x: 560 }} animate={{ x: 0 }} exit={{ x: 560 }} transition={{ type: "spring", damping: 28, stiffness: 260 }}><div className="detail-top"><button className="back-link" onClick={onClose}><ArrowLeft size={15} /> Back to portfolio</button><button className="icon-button" onClick={onClose} aria-label="Close detail"><X size={18} /></button></div><div className="detail-company-head"><div className="detail-company-logo">{company.symbol.slice(0, 1)}</div><div><div className="detail-company-name">{company.name}</div><div className="detail-symbol">{company.symbol} <span>·</span> {company.exchange} <span>·</span> {company.sector}</div></div><StatusBadge company={company} buyThreshold={buyThreshold} sellThreshold={sellThreshold} /></div><div className="detail-price-block"><b>{money(company.price)}</b><span className={company.change >= 0 ? "positive" : "negative"}>{company.change >= 0 ? <ArrowUpRight size={15} /> : <ArrowDownRight size={15} />}{percent(company.change)}</span><small>Current market price · Demo data</small></div><div className="detail-target-grid"><div className="detail-target buy-target-detail"><span>BUY TARGET</span><b>{money(company.buyTarget)}</b><small>{Math.abs(buyDistance).toFixed(2)}% {buyDistance >= 0 ? "above" : "below"} target</small></div><div className="detail-target sell-target-detail"><span>SELL TARGET</span><b>{money(company.sellTarget)}</b><small>{Math.abs(sellDistance).toFixed(2)}% {sellDistance >= 0 ? "below" : "above"} target</small></div></div><section className="detail-section"><div className="section-head"><div><h2>Price history</h2><p>Simulated historical prices</p></div><div className="detail-range">{["1D", "1W", "1M", "3M", "1Y"].map((p) => <button className={period === p ? "range-active" : ""} key={p} onClick={() => setPeriod(p)}>{p}</button>)}</div></div><div className="detail-chart"><PriceChart company={company} period={period} compact targets /></div><div className="chart-legend"><span><i className="legend-blue" /> Market price</span><span><i className="legend-green" /> Buy target</span><span><i className="legend-red" /> Sell target</span></div></section><section className="detail-section detail-movements"><div className="section-head"><div><h2>Recent price movements</h2><p>Today · Simulated feed</p></div></div><div className="movement-grid"><div><small>OPEN</small><b>{money(company.price * (1 - company.change / 100))}</b></div><div><small>DAY HIGH</small><b>{money(company.price * 1.013)}</b></div><div><small>DAY LOW</small><b>{money(company.price * 0.987)}</b></div><div><small>VOLUME</small><b>{(2.4 + company.id / 10).toFixed(2)}L</b></div></div></section><section className="detail-section"><div className="section-head"><div><h2>Alert history</h2><p>Recent target events for {company.symbol}</p></div></div>{ownAlerts.length ? ownAlerts.map((a) => <div className="detail-alert" key={a.id}><span className={`alert-type-icon ${a.type.includes("Sell") ? "alert-red" : "alert-green"}`}><Zap size={12} /></span><div><b>{a.type}</b><small>{a.time} · {a.status}</small></div><span>{money(a.price)}</span></div>) : <div className="detail-empty">No alerts recorded for this company.</div>}</section><div className="detail-last-update"><Clock3 size={13} /> Last market update: today, {company.updated} IST <span>·</span> Demo data</div></motion.aside></>;
}

function EditTargetModal({ company, onClose, onSave }: { company: Company; onClose: () => void; onSave: (buy: number, sell: number) => void }) {
  const [buy, setBuy] = useState(String(company.buyTarget)); const [sell, setSell] = useState(String(company.sellTarget)); const [error, setError] = useState("");
  const submit = () => { const b = Number(buy); const s = Number(sell); if (!Number.isFinite(b) || !Number.isFinite(s) || b <= 0 || s <= b) { setError("Enter valid prices. The sell target must be higher than the buy target."); return; } onSave(b, s); };
  return <><motion.button className="modal-backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} aria-label="Close edit dialog" /><motion.div className="edit-modal" role="dialog" aria-modal="true" aria-labelledby="edit-title" initial={{ opacity: 0, scale: 0.96, y: 12 }} animate={{ opacity: 1, scale: 1, y: 0 }} exit={{ opacity: 0, scale: 0.96, y: 12 }}><div className="modal-head"><div><div className="eyebrow">TARGET MANAGEMENT</div><h2 id="edit-title">Edit target prices</h2><p>{company.name} <span>·</span> {company.symbol}</p></div><button className="icon-button" aria-label="Close modal" onClick={onClose}><X size={17} /></button></div><div className="modal-price-line"><span>Current price</span><b>{money(company.price)}</b></div><label className="modal-field"><span>Buy target price</span><div><span>₹</span><input type="number" value={buy} onChange={(e) => setBuy(e.target.value)} min="0.01" /></div></label><label className="modal-field"><span>Sell target price</span><div><span>₹</span><input type="number" value={sell} onChange={(e) => setSell(e.target.value)} min="0.01" /></div></label>{error && <div className="form-error">{error}</div>}<div className="modal-note"><ShieldCheck size={14} /> Changes apply to this demo session only.</div><div className="modal-actions"><button className="button secondary" onClick={onClose}>Cancel</button><button className="button primary" onClick={submit}>Save targets</button></div></motion.div></>;
}
