export type Exchange = "NSE" | "BSE";
export type TargetStatus = "Buy zone" | "Sell zone" | "On track" | "Approaching";

export interface PortfolioCompany {
  name: string;
  symbol: string;
  exchange: Exchange;
  sector: string | null;
  cmp: number | null;
  base_target: number | null;
  bull_target: number | null;
  version: number;
  latest_quote: PriceQuote | null;
}

export interface PriceQuote {
  id: number;
  exchange: Exchange;
  symbol: string;
  price: number;
  currency: string;
  provider: string;
  quoted_at: string;
  received_at: string;
  is_simulated: boolean;
  status: "fresh" | "stale" | "out_of_order" | "simulated";
  is_fresh: boolean;
}

export interface PriceHistory {
  exchange: Exchange;
  symbol: string;
  items: PriceQuote[];
}

export interface ApiAlert {
  id: number;
  exchange: Exchange;
  symbol: string;
  company: string;
  target_kind: "base" | "bull";
  target: number | null;
  price: number;
  triggered_at: string;
  target_revision_id: number;
  is_simulated: boolean;
  event_type: "fixture" | "live";
  quote_status: "fresh" | "stale" | "out_of_order" | "simulated";
  quote_is_fresh: boolean;
  provider: string;
  delivery_status: "not_queued" | "queued" | "retry" | "sending" | "sent" | "failed";
  delivery_mode: "dry_run" | "live" | null;
  channel: string | null;
  delivery_label: string;
  attempts: number;
  max_attempts: number;
  last_error: string | null;
  sent_at: string | null;
  provider_message_id: string | null;
}

export interface WorkerStatus {
  state: "running" | "stopped" | "error";
  provider: string | null;
  exchange_calendar: string | null;
  last_started_at: string | null;
  last_poll_at: string | null;
  last_error: string | null;
  live_provider_configured: boolean;
  authoritative_calendar_configured: boolean;
  live_alerts_enabled: boolean;
  notification_mode: "dry_run_only";
  note: string;
}

export interface ImportColumn {
  column: string;
  header: string | null;
}

export interface ImportSheet {
  name: string;
  headers: ImportColumn[];
}

export interface ImportInspection {
  file_type: "csv" | "xlsx";
  header_row: number;
  sheets: ImportSheet[];
}

export interface ImportPreviewRow {
  row_number: number;
  exchange: string | null;
  symbol: string | null;
  name: string | null;
  sector: string | null;
  cmp: number | null;
  base_target: number | null;
  bull_target: number | null;
  current_cmp: number | null;
  current_base_target: number | null;
  current_bull_target: number | null;
  current_version: number | null;
  is_new_company: boolean;
  errors: string[];
  warnings: string[];
}

export interface ImportPreview {
  id: string;
  filename: string;
  status: "preview" | "confirmed";
  formula_review_required: boolean;
  rows: ImportPreviewRow[];
  result: ImportResult | null;
}

export interface ImportResult {
  id: string;
  status: "confirmed";
  created: number;
  updated: number;
  unchanged: number;
}

export interface Company {
  id: number;
  name: string;
  symbol: string;
  exchange: Exchange;
  price: number;
  change: number;
  buyTarget: number;
  sellTarget: number;
  updated: string;
  sector: string;
}

export interface PortfolioAlert {
  id: string;
  company: string;
  symbol: string;
  type: "Buy target reached" | "Sell target reached" | "Approaching target" | "Stale price" | "Feed interruption";
  price: number;
  target: number;
  time: string;
  status: "New" | "Acknowledged" | "Resolved";
  channel: "Delivered" | "Queued" | "Disabled";
}
