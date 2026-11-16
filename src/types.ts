export type Exchange = "NSE" | "BSE";
export type TargetDirection = "below" | "above" | "pending";
export type TriggerMode = "at_or_beyond" | "crosses";
export type RearmPolicy = "once_per_revision" | "rearm_on_reset" | "rearm_after_recovery" | "cooldown";

export interface TargetAlertStateInfo {
  target_kind: "base" | "bull";
  state: "armed" | "triggered" | "acknowledged" | "resolved" | "pending_configuration";
  comparison_direction: TargetDirection;
  trigger_mode: TriggerMode;
  rearm_policy: RearmPolicy;
  last_evaluated_price: number | null;
  last_evaluated_at: string | null;
  last_triggered_price: number | null;
  last_triggered_at: string | null;
  acknowledged_at: string | null;
  resolved_at: string | null;
}

export interface PortfolioCompany {
  name: string;
  symbol: string;
  exchange: Exchange;
  sector: string | null;
  cmp: number | null;
  base_target: number | null;
  bull_target: number | null;
  version: number;
  is_approved?: boolean;
  monitoring_active?: boolean;
  base_direction?: TargetDirection;
  bull_direction?: TargetDirection;
  base_trigger_mode?: TriggerMode;
  bull_trigger_mode?: TriggerMode;
  rearm_policy?: RearmPolicy;
  cooldown_minutes?: number;
  source_metadata?: Record<string, unknown> | null;
  latest_quote: PriceQuote | null;
  distance_to_base_abs?: number | null;
  distance_to_base_pct?: number | null;
  distance_to_bull_abs?: number | null;
  distance_to_bull_pct?: number | null;
  nearest_target_pct?: number | null;
  alert_status?:
    | "monitoring"
    | "base_triggered"
    | "bull_triggered"
    | "both_triggered"
    | "pending_configuration"
    | "inactive"
    | "freshness_unknown"
    | "stale_quote"
    | "approaching"
    | "paused"
    | "pending_approval";
  base_alert_state?: TargetAlertStateInfo | null;
  bull_alert_state?: TargetAlertStateInfo | null;
}

export interface PriceQuote {
  id: number;
  exchange: Exchange;
  symbol: string;
  price: number;
  currency: string;
  provider: string;
  quote_type: "last_trade" | "candle_close" | "fixture" | "provider_quote" | "websocket_tick";
  quoted_at: string | null;
  received_at: string;
  is_simulated: boolean;
  freshness_seconds: number;
  status: "fresh" | "stale" | "out_of_order" | "simulated" | "freshness_unknown";
  is_fresh: boolean;
  freshness_unknown?: boolean;
}

export interface PriceHistory {
  exchange: Exchange;
  symbol: string;
  items: PriceQuote[];
}

export interface AlertChannelDelivery {
  id: string;
  channel: string;
  destination: string | null;
  delivery_mode: string;
  status: string;
  attempts: number;
  max_attempts: number;
  last_error: string | null;
  sent_at: string | null;
}

export interface ApiAlert {
  id: number;
  exchange: Exchange;
  symbol: string;
  company: string;
  sector?: string | null;
  saved_cmp?: number | null;
  target_kind: "base" | "bull";
  target: number | null;
  price: number;
  breach_abs?: number | null;
  breach_pct?: number | null;
  comparison_direction?: "below" | "above";
  price_direction?: "Upward" | "Downward";
  trigger_cycle?: number;
  lifecycle_status?: "active" | "triggered" | "acknowledged" | "resolved" | "reset";
  acknowledged_at?: string | null;
  acknowledged_by?: string | null;
  resolved_at?: string | null;
  resolved_by?: string | null;
  reset_at?: string | null;
  triggered_at: string;
  target_revision_id: number;
  is_simulated: boolean;
  event_type: "fixture" | "live";
  quote_status: "fresh" | "stale" | "out_of_order" | "simulated" | "freshness_unknown";
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
  email_subject?: string;
  email_body?: string;
  email_html?: string;
  recipients?: string[];
  channels?: AlertChannelDelivery[];
}

export interface AlertEmailPreviewResponse {
  exchange: Exchange;
  symbol: string;
  company: string;
  sector: string | null;
  saved_cmp: number | null;
  target_kind: "base" | "bull";
  target: number;
  price: number;
  breach_abs: number;
  breach_pct: number;
  comparison_direction: "below" | "above";
  triggered_at: string;
  email_subject: string;
  email_body: string;
  email_html: string;
  recipients: string[];
  smtp_configured: boolean;
  missing_smtp: string[];
  live_delivery_ready: boolean;
  delivery_gate_reason: string;
}

export interface WorkerStatus {
  state: "running" | "stopped" | "error";
  provider: string | null;
  exchange_calendar: string | null;
  last_started_at: string | null;
  last_poll_at: string | null;
  last_error: string | null;
  last_poll_attempted?: number;
  last_poll_recorded?: number;
  last_poll_failed?: number;
  fresh_symbols_count?: number;
  stale_symbols_count?: number;
  freshness_unknown_symbols_count?: number;
  freshness_unknown_symbols?: string[];
  websocket_status?: string;
  connection_status?: string;
  market_open?: boolean;
  feed_ready?: boolean;
  awaiting_snapshot?: boolean;
  backlog_state?: "normal" | "overflow";
  lag_detected?: boolean;
  queue_overflow_drops?: number;
  protocol_gap_detection?: "disabled" | "enabled";
  protocol_gap_reason?: string;
  protocol_sequence_gaps?: number;
  reconnect_count?: number;
  reconnect_gaps_count?: number;
  gap_history?: Array<Record<string, unknown>>;
  live_provider_configured: boolean;
  authoritative_calendar_configured: boolean;
  live_alerts_enabled: boolean;
  notification_mode: "dry_run_only" | "live_channels" | "live" | "dry_run";
  notification_gate_reason?: string;
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
  suggested_header_row?: number;
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
  base_direction?: TargetDirection;
  bull_direction?: TargetDirection;
  approval_status?: "approved" | "rejected" | "pending";
  cell_refs?: Record<string, string>;
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
  status: "preview" | "pending_approval" | "confirmed" | "rejected";
  version_number?: number | null;
  uploaded_by?: string;
  created_at?: string | null;
  confirmed_at?: string | null;
  formula_review_required: boolean;
  total_rows?: number;
  valid_rows?: number;
  invalid_rows?: number;
  rows: ImportPreviewRow[];
  result: ImportResult | null;
}

export interface ImportResult {
  id: string;
  status: "pending_approval" | "confirmed";
  version_number?: number | null;
  created?: number;
  updated?: number;
  unchanged?: number;
  rejected?: number;
}

export interface ImportBatchHistoryItem {
  id: string;
  filename: string;
  file_type?: string;
  sheet_name: string | null;
  status: "preview" | "pending_approval" | "confirmed" | "rejected";
  version_number: number | null;
  uploaded_by: string;
  uploader_user_id?: number | null;
  approved_by?: string | null;
  approver_user_id?: number | null;
  rejector_user_id?: number | null;
  created_at: string | null;
  approved_at?: string | null;
  confirmed_at?: string | null;
  result: Record<string, unknown> | null;
  changes_snapshot: Array<Record<string, unknown>>;
}

export interface NotificationPreference {
  dashboard_enabled: boolean;
  email_enabled: boolean;
  email_recipients: string | string[];
  notify_on_base: boolean;
  notify_on_bull: boolean;
  notify_on_stale: boolean;
  updated_at: string | null;
}

export type CompanyRevisionItem = {
  id: number;
  version: number;
  base_target: number | null;
  bull_target: number | null;
  previous_base_target?: number | null;
  previous_bull_target?: number | null;
  base_direction: TargetDirection;
  bull_direction: TargetDirection;
  source: string;
  changed_by?: string;
  import_batch_id?: string | null;
  import_id?: string | null;
  changed_at: string | null;
};

export interface CompanyDetailsResponse {
  company: PortfolioCompany;
  revisions?: CompanyRevisionItem[];
  target_revisions?: CompanyRevisionItem[];
  alerts?: ApiAlert[];
  alert_history?: ApiAlert[];
  price_history?: PriceQuote[];
  quote_history?: PriceQuote[];
}

export interface LivePriceTickEvent {
  type: "price_tick";
  exchange: Exchange;
  symbol: string;
  company_name: string;
  quote: PriceQuote;
  base_target: number | null;
  bull_target: number | null;
  base_direction?: TargetDirection;
  bull_direction?: TargetDirection;
  distance_to_base_abs: number | null;
  distance_to_base_pct: number | null;
  distance_to_bull_abs: number | null;
  distance_to_bull_pct: number | null;
  nearest_target_pct: number | null;
  alert_status?: PortfolioCompany["alert_status"];
  base_alert_state?: TargetAlertStateInfo | null;
  bull_alert_state?: TargetAlertStateInfo | null;
}

export interface LiveOutboxAlertEvent {
  type: "outbox_alert";
  outbox_id: string;
  channel: string;
  delivery_mode: "dry_run" | "live";
  status: string;
  created_at: string;
  alert: ApiAlert;
}

export interface LiveStreamConnectedEvent {
  type: "connected";
  cursor_quote_id: number;
  timestamp: string;
}

export interface LiveStreamHeartbeatEvent {
  type: "heartbeat";
  timestamp: string;
}

export type LiveMarketStreamEvent =
  | LivePriceTickEvent
  | LiveOutboxAlertEvent
  | LiveStreamConnectedEvent
  | LiveStreamHeartbeatEvent;

export interface LiveUiAlertNotification {
  outboxId: string;
  alert: ApiAlert;
  channel: string;
  createdAt: string;
}
