import type {
  ApiAlert, ImportInspection, ImportPreview, ImportResult, PortfolioCompany, PriceHistory,
  WorkerStatus,
} from "./types";

const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");

export async function fetchCompanies(signal?: AbortSignal): Promise<PortfolioCompany[]> {
  const response = await fetch(`${apiBaseUrl}/api/companies`, { signal });
  if (!response.ok) {
    throw await responseError(response);
  }
  return response.json() as Promise<PortfolioCompany[]>;
}

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

export async function fetchWorkerStatus(signal?: AbortSignal): Promise<WorkerStatus> {
  const response = await fetch(`${apiBaseUrl}/api/worker/status`, { signal });
  return expectJson<WorkerStatus>(response);
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
}

export async function previewImport(file: File, mapping: ImportMapping): Promise<ImportPreview> {
  const form = new FormData();
  form.append("file", file);
  Object.entries(mapping).forEach(([key, value]) => form.append(key, String(value)));
  return expectJson(await fetch(`${apiBaseUrl}/api/imports/preview`, { method: "POST", body: form }));
}

export async function confirmImport(
  importId: string,
  reviewCachedTargets: boolean,
): Promise<ImportResult> {
  return expectJson(await fetch(`${apiBaseUrl}/api/imports/${encodeURIComponent(importId)}/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ review_cached_targets: reviewCachedTargets }),
  }));
}

export async function updateCompanyTargets(
  company: PortfolioCompany,
  targets: { base_target: number | null; bull_target: number | null },
): Promise<PortfolioCompany> {
  return expectJson(await fetch(
    `${apiBaseUrl}/api/companies/${encodeURIComponent(company.exchange)}/${encodeURIComponent(company.symbol)}/targets`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_version: company.version, ...targets }),
    },
  ));
}
