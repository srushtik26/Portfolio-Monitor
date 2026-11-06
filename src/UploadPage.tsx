import { useEffect, useRef, useState } from "react";
import type { ChangeEvent, DragEvent } from "react";
import {
  Check,
  CircleHelp,
  CloudDownload,
  Download,
  Edit3,
  FileSpreadsheet,
  History,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Upload,
  X,
} from "lucide-react";
import {
  confirmImport,
  fetchImportHistory,
  inspectImport,
  previewImport,
  rejectImport,
  restoreImportVersion,
  updateImportPreviewRow,
} from "./api";
import type { ImportMapping } from "./api";
import type {
  ImportBatchHistoryItem,
  ImportInspection,
  ImportPreview,
  ImportPreviewRow,
  TargetDirection,
} from "./types";

type MappingFields = Omit<ImportMapping, "sheet_name" | "header_row">;

const emptyMapping: MappingFields = {
  exchange_column: "",
  symbol_column: "",
  name_column: "",
  base_target_column: "",
  bull_target_column: "",
  cmp_column: "",
  sector_column: "",
};

const mappingFields: Array<{ key: keyof MappingFields; label: string; required: boolean }> = [
  { key: "exchange_column", label: "Exchange", required: true },
  { key: "symbol_column", label: "Symbol", required: true },
  { key: "name_column", label: "Company name", required: true },
  { key: "base_target_column", label: "Base target", required: false },
  { key: "bull_target_column", label: "Bull target", required: false },
  { key: "cmp_column", label: "Saved CMP (reference)", required: false },
  { key: "sector_column", label: "Sector (optional)", required: false },
];

const money = (value: number) =>
  `₹${value.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export default function UploadPage({ onCompleted }: { onCompleted: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [inspection, setInspection] = useState<ImportInspection | null>(null);
  const [sheetName, setSheetName] = useState("");
  const [headerRow, setHeaderRow] = useState(1);
  const [mapping, setMapping] = useState<MappingFields>(emptyMapping);
  const [uploadedBy, setUploadedBy] = useState("Portfolio Admin");
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [editingRowNumber, setEditingRowNumber] = useState<number | null>(null);
  const [rowDraft, setRowDraft] = useState<{
    exchange: string;
    symbol: string;
    name: string;
    cmp: string;
    base_target: string;
    bull_target: string;
    base_direction: TargetDirection;
    bull_direction: TargetDirection;
  } | null>(null);
  const [reviewedCachedTargets, setReviewedCachedTargets] = useState(false);
  const [isInspecting, setIsInspecting] = useState(false);
  const [isPreviewing, setIsPreviewing] = useState(false);
  const [isConfirming, setIsConfirming] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");
  const [history, setHistory] = useState<ImportBatchHistoryItem[]>([]);
  const [restoringId, setRestoringId] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const loadImportHistory = async () => {
    try {
      const items = await fetchImportHistory();
      setHistory(items);
    } catch {
      // ignore background error
    }
  };

  useEffect(() => {
    void loadImportHistory();
  }, []);

  const resetPreview = () => {
    setPreview(null);
    setEditingRowNumber(null);
    setRowDraft(null);
    setReviewedCachedTargets(false);
    setError("");
  };

  const autoDetectMapping = (headers: Array<{ column: string; header: string | null }>) => {
    const next: MappingFields = { ...emptyMapping };
    for (const h of headers) {
      const label = (h.header || "").toLowerCase();
      if (!next.exchange_column && (label.includes("exchange") || label === "exch")) {
        next.exchange_column = h.column;
      } else if (!next.symbol_column && (label.includes("symbol") || label.includes("ticker") || label.includes("scrip"))) {
        next.symbol_column = h.column;
      } else if (!next.name_column && (label.includes("company") || label.includes("name"))) {
        next.name_column = h.column;
      } else if (!next.cmp_column && (label.includes("cmp") || label.includes("saved price"))) {
        next.cmp_column = h.column;
      } else if (!next.base_target_column && label.includes("base")) {
        next.base_target_column = h.column;
      } else if (!next.bull_target_column && label.includes("bull")) {
        next.bull_target_column = h.column;
      } else if (!next.sector_column && label.includes("sector")) {
        next.sector_column = h.column;
      }
    }
    setMapping(next);
  };

  const inspectFile = async (selectedFile: File, row: number) => {
    setIsInspecting(true);
    setError("");
    try {
      const result = await inspectImport(selectedFile, row);
      setInspection(result);
      const selectedSheet = result.sheets.some((sheet) => sheet.name === sheetName)
        ? sheetName
        : result.sheets[0]?.name ?? "";
      setSheetName(selectedSheet);
      const sheetHeaders = result.sheets.find((s) => s.name === selectedSheet)?.headers ?? [];
      autoDetectMapping(sheetHeaders);
    } catch (requestError) {
      setInspection(null);
      setError(requestError instanceof Error ? requestError.message : "Could not inspect this file.");
    } finally {
      setIsInspecting(false);
    }
  };

  const chooseFile = (selectedFile?: File) => {
    if (!selectedFile) return;
    if (!/\.(xlsx|csv)$/i.test(selectedFile.name)) {
      setError("Choose an .xlsx or .csv file.");
      return;
    }
    if (selectedFile.size > 10 * 1024 * 1024) {
      setError("File exceeds the 10 MB limit.");
      return;
    }
    setFile(selectedFile);
    setInspection(null);
    setSheetName("");
    setHeaderRow(1);
    setMapping(emptyMapping);
    resetPreview();
    void inspectFile(selectedFile, 1);
  };

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    chooseFile(event.dataTransfer.files[0]);
  };

  const loadHeaderRow = () => {
    if (!file) return;
    resetPreview();
    void inspectFile(file, headerRow);
  };

  const headers = inspection?.sheets.find((sheet) => sheet.name === sheetName)?.headers ?? [];
  const previewImportFile = async () => {
    if (!file || !inspection) return;
    resetPreview();
    setIsPreviewing(true);
    const requestMapping: ImportMapping = {
      sheet_name: sheetName,
      header_row: headerRow,
      exchange_column: mapping.exchange_column,
      symbol_column: mapping.symbol_column,
      name_column: mapping.name_column,
      uploaded_by: uploadedBy || "Portfolio Admin",
      ...(mapping.base_target_column ? { base_target_column: mapping.base_target_column } : {}),
      ...(mapping.bull_target_column ? { bull_target_column: mapping.bull_target_column } : {}),
      ...(mapping.cmp_column ? { cmp_column: mapping.cmp_column } : {}),
      ...(mapping.sector_column ? { sector_column: mapping.sector_column } : {}),
    };
    try {
      setPreview(await previewImport(file, requestMapping));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Could not preview this file.");
    } finally {
      setIsPreviewing(false);
    }
  };

  const startEditingRow = (row: ImportPreviewRow) => {
    setEditingRowNumber(row.row_number);
    setRowDraft({
      exchange: row.exchange || "NSE",
      symbol: row.symbol || "",
      name: row.name || "",
      cmp: row.cmp == null ? "" : String(row.cmp),
      base_target: row.base_target == null ? "" : String(row.base_target),
      bull_target: row.bull_target == null ? "" : String(row.bull_target),
      base_direction: row.base_direction || "below",
      bull_direction: row.bull_direction || "above",
    });
  };

  const saveEditedRow = async (rowNumber: number) => {
    if (!preview || !rowDraft) return;
    setError("");
    try {
      const updated = await updateImportPreviewRow(preview.id, rowNumber, {
        exchange: rowDraft.exchange.trim().toUpperCase(),
        symbol: rowDraft.symbol.trim().toUpperCase(),
        name: rowDraft.name.trim(),
        cmp: rowDraft.cmp.trim() === "" ? null : Number(rowDraft.cmp),
        base_target: rowDraft.base_target.trim() === "" ? null : Number(rowDraft.base_target),
        bull_target: rowDraft.bull_target.trim() === "" ? null : Number(rowDraft.bull_target),
        base_direction: rowDraft.base_direction,
        bull_direction: rowDraft.bull_direction,
        approval_status: "approved",
      });
      setPreview(updated);
      setEditingRowNumber(null);
      setRowDraft(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update row.");
    }
  };

  const toggleRowApproval = async (row: ImportPreviewRow) => {
    if (!preview) return;
    const nextStatus = row.approval_status === "rejected" ? "approved" : "rejected";
    try {
      const updated = await updateImportPreviewRow(preview.id, row.row_number, {
        approval_status: nextStatus,
      });
      setPreview(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not change row approval.");
    }
  };

  const confirm = async () => {
    if (!preview) return;
    setIsConfirming(true);
    setError("");
    try {
      const result = await confirmImport(preview.id, reviewedCachedTargets, uploadedBy || "Portfolio Admin");
      setPreview({ ...preview, status: "confirmed", result, version_number: result.version_number });
      await loadImportHistory();
      onCompleted();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Could not confirm this import.");
    } finally {
      setIsConfirming(false);
    }
  };

  const handleRejectBatch = async () => {
    if (!preview) return;
    try {
      await rejectImport(preview.id);
      setPreview({ ...preview, status: "rejected" });
      await loadImportHistory();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not reject import.");
    }
  };

  const handleRestoreVersion = async (batchId: string) => {
    setRestoringId(batchId);
    setError("");
    try {
      await restoreImportVersion(batchId);
      await loadImportHistory();
      onCompleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not restore version.");
    } finally {
      setRestoringId(null);
    }
  };

  const downloadTemplate = () => {
    const csv =
      "Exchange,Symbol,Company Name,Saved CMP,Base Target,Bull Target,Sector\n" +
      "NSE,BHEL,Bharat Heavy Electricals Ltd,433.70,426.91,561.09,Capital Goods\n" +
      "NSE,TEGA,Tega Industries Ltd,1975.00,1950.00,2240.00,Industrial Manufacturing\n";
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "portfolio-target-template.csv";
    anchor.click();
    URL.revokeObjectURL(url);
  };

  const activeRows = preview?.rows.filter((r) => r.approval_status !== "rejected") ?? [];
  const errorCount = activeRows.reduce((total, row) => total + row.errors.length, 0);
  const canMap = Boolean(
    file &&
      inspection &&
      sheetName &&
      mapping.exchange_column &&
      mapping.symbol_column &&
      mapping.name_column &&
      (mapping.base_target_column || mapping.bull_target_column),
  );

  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">DATA MANAGEMENT · VERSION-CONTROLLED WORKFLOW</div>
          <h1>Upload → Parse → Preview → Validate → Approve → Monitor</h1>
          <p>
            Inspect an Excel workbook or CSV, review extracted cell references, edit or reject individual records, and create a new versioned target revision.
          </p>
        </div>
        <div className="heading-actions">
          <button className="button secondary" onClick={downloadTemplate}>
            <Download size={14} /> Download sample template
          </button>
        </div>
      </div>

      <div className="upload-layout">
        <div className="upload-main">
          <section className="card upload-card">
            <div className="section-head">
              <div>
                <h2>1. Upload & Map Excel Sheet</h2>
                <p>Saved CMP is stored strictly as historical reference and never treated as live market price.</p>
              </div>
              <span className="local-only-tag">10 MB · 2,000 ROWS MAX</span>
            </div>

            <div
              className={`dropzone ${dragging ? "dropzone-active" : ""}`}
              onDragOver={(event) => {
                event.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={onDrop}
            >
              <input
                ref={inputRef}
                type="file"
                accept=".xlsx,.csv"
                hidden
                onChange={(event: ChangeEvent<HTMLInputElement>) => chooseFile(event.target.files?.[0])}
              />
              <div className="upload-icon">
                <CloudDownload size={21} />
              </div>
              <b>
                Drop your Excel (.xlsx) or CSV here, or{" "}
                <button onClick={() => inputRef.current?.click()}>browse</button>
              </b>
              <small>Supports ~40+ NSE/BSE companies with cell-reference tracking</small>
            </div>

            {file && (
              <div className="selected-file">
                <div className="file-icon">
                  <FileSpreadsheet size={17} />
                </div>
                <div>
                  <b>{file.name}</b>
                  <small>
                    {(file.size / 1024).toFixed(1)} KB ·{" "}
                    {isInspecting
                      ? "Reading sheet headers…"
                      : inspection
                        ? "Ready for column mapping"
                        : "Inspection failed"}
                  </small>
                </div>
                <button
                  className="icon-button"
                  aria-label="Remove file"
                  onClick={() => {
                    setFile(null);
                    setInspection(null);
                    setPreview(null);
                    setError("");
                    setMapping(emptyMapping);
                    if (inputRef.current) inputRef.current.value = "";
                  }}
                >
                  <X size={15} />
                </button>
              </div>
            )}

            {error && (
              <div className="form-error" role="alert">
                {error}
              </div>
            )}

            {inspection && (
              <div className="mapping-options">
                <div className="mapping-controls">
                  {inspection.file_type === "xlsx" && (
                    <label className="mapping-field">
                      <span>Worksheet</span>
                      <select
                        value={sheetName}
                        onChange={(event) => {
                          setSheetName(event.target.value);
                          const sh =
                            inspection.sheets.find((s) => s.name === event.target.value)?.headers ?? [];
                          autoDetectMapping(sh);
                          resetPreview();
                        }}
                      >
                        <option value="">Select worksheet</option>
                        {inspection.sheets.map((sheet) => (
                          <option key={sheet.name} value={sheet.name}>
                            {sheet.name}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  <label className="mapping-field">
                    <span>Header row</span>
                    <input
                      type="number"
                      min="1"
                      value={headerRow}
                      onChange={(event) => {
                        setHeaderRow(Math.max(1, Number(event.target.value)));
                        resetPreview();
                      }}
                    />
                  </label>
                  <label className="mapping-field">
                    <span>Uploaded by</span>
                    <input
                      type="text"
                      value={uploadedBy}
                      onChange={(event) => setUploadedBy(event.target.value)}
                    />
                  </label>
                  <button className="button secondary" onClick={loadHeaderRow} disabled={isInspecting}>
                    {isInspecting ? "Reading…" : "Load headers"}
                  </button>
                </div>

                <div className="mapping-grid">
                  {mappingFields.map((field) => (
                    <label className="mapping-field" key={field.key}>
                      <span>
                        {field.label}
                        {field.required ? " *" : ""}
                      </span>
                      <select
                        value={mapping[field.key] ?? ""}
                        onChange={(event) => {
                          setMapping((current) => ({
                            ...current,
                            [field.key]: event.target.value || undefined,
                          }));
                          resetPreview();
                        }}
                      >
                        <option value="">Select column</option>
                        {headers.map((header) => (
                          <option key={header.column} value={header.column}>
                            {header.column} — {header.header || "(blank header)"}
                          </option>
                        ))}
                      </select>
                    </label>
                  ))}
                </div>

                <button
                  className="button primary"
                  onClick={previewImportFile}
                  disabled={!canMap || isPreviewing || isInspecting}
                >
                  {isPreviewing ? (
                    <>
                      <RefreshCw size={14} /> Parsing & Validating…
                    </>
                  ) : (
                    <>
                      <ShieldCheck size={14} /> Parse & Preview Import
                    </>
                  )}
                </button>
              </div>
            )}
          </section>

          {preview && (
            <section className="card import-preview">
              <div className="section-head">
                <div>
                  <h2>2. Preview, Edit & Approve Extracted Records</h2>
                  <p>
                    {preview.filename} · {activeRows.length} active rows ({preview.rows.length} total) ·
                    Only approved rows will participate in live price monitoring.
                  </p>
                </div>
                <span className={errorCount ? "validation-errors" : "validation-ok"}>
                  {errorCount ? (
                    `${errorCount} validation error(s) in active rows`
                  ) : (
                    <>
                      <Check size={12} /> Validated & ready for approval
                    </>
                  )}
                </span>
              </div>

              <div className="table-scroll">
                <table className="portfolio-table">
                  <thead>
                    <tr>
                      <th>Company</th>
                      <th>Symbol</th>
                      <th>Exchange</th>
                      <th>Saved CMP</th>
                      <th>Base Target</th>
                      <th>Bull Target</th>
                      <th>Validation Status</th>
                      <th>Row Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.rows.map((row) => {
                      const isEditing = editingRowNumber === row.row_number && rowDraft !== null;
                      const isRejected = row.approval_status === "rejected";
                      return (
                        <tr
                          key={row.row_number}
                          style={{ opacity: isRejected ? 0.5 : 1 }}
                        >
                          <td>
                            {isEditing ? (
                              <input
                                className="inline-input"
                                value={rowDraft.name}
                                onChange={(e) => setRowDraft({ ...rowDraft, name: e.target.value })}
                              />
                            ) : (
                              <>
                                <b>{row.name || "—"}</b>
                                <small className="cell-sub">
                                  Row {row.row_number}{" "}
                                  {row.cell_refs?.name ? `(${row.cell_refs.name})` : ""} ·{" "}
                                  {row.is_new_company ? "New company" : `v${row.current_version ?? 1}`}
                                </small>
                              </>
                            )}
                          </td>
                          <td>
                            {isEditing ? (
                              <input
                                className="inline-input"
                                value={rowDraft.symbol}
                                onChange={(e) => setRowDraft({ ...rowDraft, symbol: e.target.value })}
                              />
                            ) : (
                              <>
                                <b>{row.symbol || "—"}</b>
                                {row.cell_refs?.symbol && (
                                  <small className="cell-sub">Cell {row.cell_refs.symbol}</small>
                                )}
                              </>
                            )}
                          </td>
                          <td>
                            {isEditing ? (
                              <select
                                value={rowDraft.exchange}
                                onChange={(e) => setRowDraft({ ...rowDraft, exchange: e.target.value })}
                              >
                                <option value="NSE">NSE</option>
                                <option value="BSE">BSE</option>
                              </select>
                            ) : (
                              <span className="exchange-tag">{row.exchange || "—"}</span>
                            )}
                          </td>
                          <td>
                            {isEditing ? (
                              <input
                                className="inline-input"
                                type="number"
                                step="0.01"
                                value={rowDraft.cmp}
                                onChange={(e) => setRowDraft({ ...rowDraft, cmp: e.target.value })}
                              />
                            ) : (
                              <>
                                <div>{row.cmp == null ? "—" : money(row.cmp)}</div>
                                <small className="cell-sub">
                                  {row.cell_refs?.cmp ? `Cell ${row.cell_refs.cmp} · ` : ""}
                                  Prev: {row.current_cmp == null ? "—" : money(row.current_cmp)}
                                </small>
                              </>
                            )}
                          </td>
                          <td>
                            {isEditing ? (
                              <div className="inline-edit-stack">
                                <input
                                  className="inline-input"
                                  type="number"
                                  step="0.01"
                                  value={rowDraft.base_target}
                                  onChange={(e) =>
                                    setRowDraft({ ...rowDraft, base_target: e.target.value })
                                  }
                                />
                                <select
                                  value={rowDraft.base_direction}
                                  onChange={(e) =>
                                    setRowDraft({
                                      ...rowDraft,
                                      base_direction: e.target.value as TargetDirection,
                                    })
                                  }
                                >
                                  <option value="below">Alert ≤ Target (Below)</option>
                                  <option value="above">Alert ≥ Target (Above)</option>
                                  <option value="pending">Pending Config</option>
                                </select>
                              </div>
                            ) : (
                              <>
                                <div>
                                  <b>{row.base_target == null ? "—" : money(row.base_target)}</b>{" "}
                                  <span className="direction-pill">
                                    {row.base_direction === "below"
                                      ? "≤ Below"
                                      : row.base_direction === "above"
                                        ? "≥ Above"
                                        : "Pending"}
                                  </span>
                                </div>
                                <small className="cell-sub">
                                  {row.cell_refs?.base_target
                                    ? `Cell ${row.cell_refs.base_target} · `
                                    : ""}
                                  Prev:{" "}
                                  {row.current_base_target == null
                                    ? "—"
                                    : money(row.current_base_target)}
                                </small>
                              </>
                            )}
                          </td>
                          <td>
                            {isEditing ? (
                              <div className="inline-edit-stack">
                                <input
                                  className="inline-input"
                                  type="number"
                                  step="0.01"
                                  value={rowDraft.bull_target}
                                  onChange={(e) =>
                                    setRowDraft({ ...rowDraft, bull_target: e.target.value })
                                  }
                                />
                                <select
                                  value={rowDraft.bull_direction}
                                  onChange={(e) =>
                                    setRowDraft({
                                      ...rowDraft,
                                      bull_direction: e.target.value as TargetDirection,
                                    })
                                  }
                                >
                                  <option value="above">Alert ≥ Target (Above)</option>
                                  <option value="below">Alert ≤ Target (Below)</option>
                                  <option value="pending">Pending Config</option>
                                </select>
                              </div>
                            ) : (
                              <>
                                <div>
                                  <b>{row.bull_target == null ? "—" : money(row.bull_target)}</b>{" "}
                                  <span className="direction-pill">
                                    {row.bull_direction === "above"
                                      ? "≥ Above"
                                      : row.bull_direction === "below"
                                        ? "≤ Below"
                                        : "Pending"}
                                  </span>
                                </div>
                                <small className="cell-sub">
                                  {row.cell_refs?.bull_target
                                    ? `Cell ${row.cell_refs.bull_target} · `
                                    : ""}
                                  Prev:{" "}
                                  {row.current_bull_target == null
                                    ? "—"
                                    : money(row.current_bull_target)}
                                </small>
                              </>
                            )}
                          </td>
                          <td className={row.errors.length && !isRejected ? "preview-error" : "validation-ok"}>
                            {isRejected ? (
                              <span className="status-pill neutral">Rejected (Skipped)</span>
                            ) : row.errors.length ? (
                              row.errors.map((message) => <div key={message}>{message}</div>)
                            ) : (
                              <span>✓ Validated</span>
                            )}
                            {row.warnings.map((message) => (
                              <div className="formula-warning" key={message}>
                                {message}
                              </div>
                            ))}
                          </td>
                          <td>
                            {preview.status === "preview" && (
                              <div className="row-actions">
                                {isEditing ? (
                                  <>
                                    <button
                                      className="button primary small"
                                      onClick={() => void saveEditedRow(row.row_number)}
                                    >
                                      Save
                                    </button>
                                    <button
                                      className="button secondary small"
                                      onClick={() => {
                                        setEditingRowNumber(null);
                                        setRowDraft(null);
                                      }}
                                    >
                                      Cancel
                                    </button>
                                  </>
                                ) : (
                                  <>
                                    <button
                                      className="icon-button"
                                      title="Edit row values & directions"
                                      onClick={() => startEditingRow(row)}
                                    >
                                      <Edit3 size={14} />
                                    </button>
                                    <button
                                      className="button secondary small"
                                      onClick={() => void toggleRowApproval(row)}
                                    >
                                      {isRejected ? "Approve" : "Reject"}
                                    </button>
                                  </>
                                )}
                              </div>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              {preview.formula_review_required && (
                <label className="formula-review">
                  <input
                    type="checkbox"
                    checked={reviewedCachedTargets}
                    onChange={(event) => setReviewedCachedTargets(event.target.checked)}
                  />{" "}
                  I reviewed cached formula target values; they may be stale because spreadsheets are not recalculated by the importer.
                </label>
              )}

              {errorCount > 0 && (
                <p className="form-error">
                  Edit the invalid rows inline or reject them before approving the import batch.
                </p>
              )}

              {preview.result && (
                <div className="preview-note" role="status">
                  Import Version v{preview.result.version_number ?? preview.version_number ?? 1} Approved:{" "}
                  {preview.result.created} created, {preview.result.updated} targets updated,{" "}
                  {preview.result.unchanged} unchanged. Active monitoring has started for all approved companies.
                </div>
              )}

              <div className="upload-actions">
                <button
                  className="button primary"
                  disabled={
                    isConfirming ||
                    preview.status !== "preview" ||
                    activeRows.length === 0 ||
                    errorCount > 0 ||
                    (preview.formula_review_required && !reviewedCachedTargets)
                  }
                  onClick={confirm}
                >
                  {isConfirming ? (
                    "Approving…"
                  ) : (
                    <>
                      <Check size={14} /> Approve Import & Start Monitoring ({activeRows.length} companies)
                    </>
                  )}
                </button>
                {preview.status === "preview" && (
                  <button className="button secondary" onClick={() => void handleRejectBatch()}>
                    <X size={14} /> Reject Entire Upload
                  </button>
                )}
              </div>

              <div className="preview-note">
                <CircleHelp size={14} /> Every approved upload creates a new version snapshot and preserves previous target history. Saved CMP is reference-only and never treated as a live market quote.
              </div>
            </section>
          )}

          <section className="card import-history-card">
            <div className="section-head">
              <div>
                <h2>3. Import Version History & Rollback</h2>
                <p>Review previous Excel uploads, target changes, and restore any earlier approved version.</p>
              </div>
              <History size={16} className="muted-icon" />
            </div>
            {history.length === 0 ? (
              <p className="empty-copy">No import versions recorded yet.</p>
            ) : (
              <div className="table-scroll">
                <table className="portfolio-table">
                  <thead>
                    <tr>
                      <th>Version</th>
                      <th>Filename</th>
                      <th>Uploaded By</th>
                      <th>Timestamp</th>
                      <th>Status</th>
                      <th>Target Modifications</th>
                      <th>Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {history.map((item) => (
                      <tr key={item.id}>
                        <td>
                          <b>{item.version_number ? `v${item.version_number}` : "Draft"}</b>
                        </td>
                        <td>
                          <b>{item.filename}</b>
                          <small className="cell-sub">{item.sheet_name || item.file_type.toUpperCase()}</small>
                        </td>
                        <td>{item.uploaded_by}</td>
                        <td>
                          {item.confirmed_at
                            ? new Date(item.confirmed_at).toLocaleString("en-IN")
                            : item.created_at
                              ? new Date(item.created_at).toLocaleString("en-IN")
                              : "—"}
                        </td>
                        <td>
                          <span
                            className={`status-pill ${
                              item.status === "confirmed"
                                ? "buy"
                                : item.status === "rejected"
                                  ? "sell"
                                  : "neutral"
                            }`}
                          >
                            {item.status.toUpperCase()}
                          </span>
                        </td>
                        <td>
                          {item.changes_snapshot && item.changes_snapshot.length > 0 ? (
                            <small className="cell-sub">
                              {item.changes_snapshot.length} company target snapshot(s) saved
                            </small>
                          ) : (
                            <small className="cell-sub">—</small>
                          )}
                        </td>
                        <td>
                          {item.status === "confirmed" &&
                            item.changes_snapshot &&
                            item.changes_snapshot.length > 0 && (
                              <button
                                className="button secondary small"
                                disabled={restoringId === item.id}
                                onClick={() => void handleRestoreVersion(item.id)}
                              >
                                <RotateCcw size={12} />{" "}
                                {restoringId === item.id ? "Restoring…" : "Restore Version"}
                              </button>
                            )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>

        <aside className="upload-aside">
          <section className="card upload-checklist">
            <h3>Versioned Import Safeguards</h3>
            <div className="checklist-item">
              <Check size={13} />
              <span>Original Excel cell coordinates (e.g. D2, E2, F2) are displayed for every target.</span>
            </div>
            <div className="checklist-item">
              <Check size={13} />
              <span>Saved CMP is stored as reference data and never used as live market price.</span>
            </div>
            <div className="checklist-item">
              <Check size={13} />
              <span>Inline editor lets you fix invalid values or configure Base/Bull comparison directions before approval.</span>
            </div>
            <div className="checklist-item">
              <Check size={13} />
              <span>Never silently overwrites targets—every approved upload creates a restorable version.</span>
            </div>
            <button className="text-link" onClick={downloadTemplate}>
              Download sample CSV template <Upload size={13} />
            </button>
          </section>
        </aside>
      </div>
    </>
  );
}
