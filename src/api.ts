import type {
  ApiAlert,
  CompanyDetailsResponse,
  ImportBatchHistoryItem,
  ImportInspection,
  ImportPreview,
  ImportResult,
  NotificationPreference,
  PortfolioCompany,
  PriceHistory,
  RearmPolicy,
  TargetDirection,
  TriggerMode,
  WorkerStatus,
} from "./types";

const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");

async function responseError(response: Response): Promise<Error> {
  const body: unknown = await response.json().catch(() => null);
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return new Error(detail);
  }
  return new Error(`Request failed with status ${response.status}`);
}

async function expectJson<T>(response: Response): Promise<T> {
  if (!response.ok) throw await responseError(response);
  return response.json() as Promise<T>;
}

export async function fetchCompanies(signal?: AbortSignal): Promise<PortfolioCompany[]> {
  const response = await fetch(`${apiBaseUrl}/api/companies`, { signal });
  return expectJson<PortfolioCompany[]>(response);
}

export async function fetchCompanyDetails(
  exchange: string,
  symbol: string,
  signal?: AbortSignal,
): Promise<CompanyDetailsResponse> {
  const response = await fetch(
    `${apiBaseUrl}/api/companies/${encodeURIComponent(exchange)}/${encodeURIComponent(symbol)}/details`,
    { signal },
  );
  return expectJson<CompanyDetailsResponse>(response);
}

export async function fetchCompanyHistory(
  exchange: string,
  symbol: string,
  limit = 100,
  signal?: AbortSignal,
): Promise<PriceHistory> {
  const response = await fetch(
    `${apiBaseUrl}/api/companies/${encodeURIComponent(exchange)}/${encodeURIComponent(symbol)}/history?limit=${limit}`,
    { signal },
  );
  return expectJson<PriceHistory>(response);
}

export async function fetchAlerts(signal?: AbortSignal): Promise<ApiAlert[]> {
  const response = await fetch(`${apiBaseUrl}/api/alerts`, { signal });
  return expectJson<ApiAlert[]>(response);
}

export async function performAlertAction(
  alertId: number,
  action: "acknowledge" | "resolve" | "reset",
  actor = "analyst",
): Promise<ApiAlert> {
  const response = await fetch(`${apiBaseUrl}/api/alerts/${alertId}/action`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, actor }),
  });
  return expectJson<ApiAlert>(response);
}

export async function fetchWorkerStatus(signal?: AbortSignal): Promise<WorkerStatus> {
  const response = await fetch(`${apiBaseUrl}/api/worker/status`, { signal });
  return expectJson<WorkerStatus>(response);
}

export async function triggerOnDemandPoll(): Promise<{
  attempted: number;
  recorded: number;
  failed: number;
  alerts_enabled: boolean;
}> {
  const response = await fetch(`${apiBaseUrl}/api/price-feed/poll`, { method: "POST" });
  return expectJson(response);
}

export async function inspectImport(file: File, headerRow: number): Promise<ImportInspection> {
  const form = new FormData();
  form.append("file", file);
  form.append("header_row", String(headerRow));
  return expectJson(await fetch(`${apiBaseUrl}/api/imports/inspect`, { method: "POST", body: form }));
}

export interface ImportMapping {
  sheet_name: string;
  header_row: number;
  exchange_column: string;
  symbol_column: string;
  name_column: string;
  base_target_column?: string;
  bull_target_column?: string;
  cmp_column?: string;
  sector_column?: string;
  uploaded_by?: string;
}

export async function previewImport(file: File, mapping: ImportMapping): Promise<ImportPreview> {
  const form = new FormData();
  form.append("file", file);
  Object.entries(mapping).forEach(([key, value]) => {
    if (value !== undefined) form.append(key, String(value));
  });
  return expectJson(await fetch(`${apiBaseUrl}/api/imports/preview`, { method: "POST", body: form }));
}

export async function updateImportPreviewRow(
  importId: string,
  rowNumber: number,
  patch: {
    exchange?: string | null;
    symbol?: string | null;
    name?: string | null;
    sector?: string | null;
    cmp?: number | null;
    base_target?: number | null;
    bull_target?: number | null;
    base_direction?: TargetDirection | null;
    bull_direction?: TargetDirection | null;
    approval_status?: "approved" | "rejected";
  },
): Promise<ImportPreview> {
  return expectJson(
    await fetch(
      `${apiBaseUrl}/api/imports/${encodeURIComponent(importId)}/rows/${rowNumber}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      },
    ),
  );
}

export async function confirmImport(
  importId: string,
  reviewCachedTargets: boolean,
  uploadedBy = "admin",
): Promise<ImportResult> {
  return expectJson(
    await fetch(`${apiBaseUrl}/api/imports/${encodeURIComponent(importId)}/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        review_cached_targets: reviewCachedTargets,
        uploaded_by: uploadedBy,
      }),
    }),
  );
}

export async function rejectImport(importId: string): Promise<{ id: string; status: string }> {
  return expectJson(
    await fetch(`${apiBaseUrl}/api/imports/${encodeURIComponent(importId)}/reject`, {
      method: "POST",
    }),
  );
}

export async function fetchImportHistory(signal?: AbortSignal): Promise<ImportBatchHistoryItem[]> {
  return expectJson(await fetch(`${apiBaseUrl}/api/imports`, { signal }));
}

export async function restoreImportVersion(importId: string): Promise<ImportResult> {
  return expectJson(
    await fetch(`${apiBaseUrl}/api/imports/${encodeURIComponent(importId)}/restore`, {
      method: "POST",
    }),
  );
}

export async function updateCompanyTargets(
  company: PortfolioCompany,
  targets: {
    base_target: number | null;
    bull_target: number | null;
    base_direction?: TargetDirection;
    bull_direction?: TargetDirection;
    base_trigger_mode?: TriggerMode;
    bull_trigger_mode?: TriggerMode;
    rearm_policy?: RearmPolicy;
    cooldown_minutes?: number;
    monitoring_active?: boolean;
  },
): Promise<PortfolioCompany> {
  return expectJson(
    await fetch(
      `${apiBaseUrl}/api/companies/${encodeURIComponent(company.exchange)}/${encodeURIComponent(company.symbol)}/targets`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_version: company.version, ...targets }),
      },
    ),
  );
}

export async function fetchNotificationPreferences(
  signal?: AbortSignal,
): Promise<NotificationPreference> {
  return expectJson(await fetch(`${apiBaseUrl}/api/notifications/preferences`, { signal }));
}

export async function updateNotificationPreferences(
  prefs: Partial<NotificationPreference>,
): Promise<NotificationPreference> {
  return expectJson(
    await fetch(`${apiBaseUrl}/api/notifications/preferences`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(prefs),
    }),
  );
}
