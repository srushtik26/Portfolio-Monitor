import { useRef, useState } from "react";
import type { ChangeEvent, DragEvent } from "react";
import { Check, CircleHelp, CloudDownload, Download, FileSpreadsheet, RefreshCw, ShieldCheck, Upload, X } from "lucide-react";
import { confirmImport, inspectImport, previewImport } from "./api";
import type { ImportMapping } from "./api";
import type { ImportInspection, ImportPreview } from "./types";

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
  { key: "cmp_column", label: "CMP (optional)", required: false },
  { key: "sector_column", label: "Sector (optional)", required: false },
];

const money = (value: number) => `₹${value.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export default function UploadPage({ onCompleted }: { onCompleted: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [inspection, setInspection] = useState<ImportInspection | null>(null);
  const [sheetName, setSheetName] = useState("");
  const [headerRow, setHeaderRow] = useState(1);
  const [mapping, setMapping] = useState<MappingFields>(emptyMapping);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [reviewedCachedTargets, setReviewedCachedTargets] = useState(false);
  const [isInspecting, setIsInspecting] = useState(false);
  const [isPreviewing, setIsPreviewing] = useState(false);
  const [isConfirming, setIsConfirming] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  const resetPreview = () => {
    setPreview(null);
    setReviewedCachedTargets(false);
    setError("");
  };

  const inspectFile = async (selectedFile: File, row: number) => {
    setIsInspecting(true);
    setError("");
    try {
      const result = await inspectImport(selectedFile, row);
      setInspection(result);
      setSheetName((current) => result.sheets.some((sheet) => sheet.name === current)
        ? current
        : result.sheets[0]?.name ?? "");
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

  const confirm = async () => {
    if (!preview) return;
    setIsConfirming(true);
    setError("");
    try {
      const result = await confirmImport(preview.id, reviewedCachedTargets);
      setPreview({ ...preview, status: "confirmed", result });
      onCompleted();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Could not confirm this import.");
    } finally {
      setIsConfirming(false);
    }
  };

  const downloadTemplate = () => {
    const csv = "Exchange,Symbol,Company Name,CMP,Base Target,Bull Target,Sector\n";
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "portfolio-target-template.csv";
    anchor.click();
    URL.revokeObjectURL(url);
  };

  const errorCount = preview?.rows.reduce((total, row) => total + row.errors.length, 0) ?? 0;
  const canMap = Boolean(
    file && inspection && sheetName && mapping.exchange_column && mapping.symbol_column
    && mapping.name_column && (mapping.base_target_column || mapping.bull_target_column),
  );

  return <>
    <div className="page-heading"><div><div className="eyebrow">DATA MANAGEMENT · BACKEND API</div><h1>Import targets</h1><p>Inspect a workbook, map its columns, review each row, then explicitly confirm changes.</p></div><div className="heading-actions"><button className="button secondary" onClick={downloadTemplate}><Download size={14} /> Download blank template</button></div></div>
    <div className="upload-layout"><div className="upload-main">
      <section className="card upload-card"><div className="section-head"><div><h2>Choose a workbook or CSV</h2><p>Files are parsed on the backend; active targets change only after confirmation.</p></div><span className="local-only-tag">10 MB · 2,000 DATA ROWS MAX</span></div>
        <div className={`dropzone ${dragging ? "dropzone-active" : ""}`} onDragOver={(event) => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={onDrop}><input ref={inputRef} type="file" accept=".xlsx,.csv" hidden onChange={(event: ChangeEvent<HTMLInputElement>) => chooseFile(event.target.files?.[0])} /><div className="upload-icon"><CloudDownload size={21} /></div><b>Drop your file here, or <button onClick={() => inputRef.current?.click()}>browse</button></b><small>Excel (.xlsx) or CSV · Maximum file size 10 MB</small></div>
        {file && <div className="selected-file"><div className="file-icon"><FileSpreadsheet size={17} /></div><div><b>{file.name}</b><small>{(file.size / 1024).toFixed(1)} KB · {isInspecting ? "Reading sheet headers…" : inspection ? "Ready for column mapping" : "Inspection failed"}</small></div><button className="icon-button" aria-label="Remove file" onClick={() => { setFile(null); setInspection(null); setPreview(null); setError(""); setMapping(emptyMapping); if (inputRef.current) inputRef.current.value = ""; }}><X size={15} /></button></div>}
        {error && <div className="form-error" role="alert">{error}</div>}
        {inspection && <div className="mapping-options">
          <div className="mapping-controls">
            {inspection.file_type === "xlsx" && <label className="mapping-field"><span>Worksheet</span><select value={sheetName} onChange={(event) => { setSheetName(event.target.value); setMapping(emptyMapping); resetPreview(); }}><option value="">Select worksheet</option>{inspection.sheets.map((sheet) => <option key={sheet.name} value={sheet.name}>{sheet.name}</option>)}</select></label>}
            <label className="mapping-field"><span>Header row</span><input type="number" min="1" value={headerRow} onChange={(event) => { setHeaderRow(Math.max(1, Number(event.target.value))); resetPreview(); }} /></label>
            <button className="button secondary" onClick={loadHeaderRow} disabled={isInspecting}>{isInspecting ? "Reading…" : "Load headers"}</button>
          </div>
          <div className="mapping-grid">{mappingFields.map((field) => <label className="mapping-field" key={field.key}><span>{field.label}{field.required ? " *" : ""}</span><select value={mapping[field.key] ?? ""} onChange={(event) => { setMapping((current) => ({ ...current, [field.key]: event.target.value || undefined })); resetPreview(); }}><option value="">Select column</option>{headers.map((header) => <option key={header.column} value={header.column}>{header.column} — {header.header || "(blank header)"}</option>)}</select></label>)}</div>
          <button className="button primary" onClick={previewImportFile} disabled={!canMap || isPreviewing || isInspecting}>{isPreviewing ? <><RefreshCw size={14} /> Previewing…</> : <><ShieldCheck size={14} /> Preview import</>}</button>
        </div>}
      </section>
      {preview && <section className="card import-preview"><div className="section-head"><div><h2>Import preview</h2><p>{preview.filename} · {preview.rows.length} rows · Preview does not modify active companies or targets.</p></div><span className={errorCount ? "validation-errors" : "validation-ok"}>{errorCount ? `${errorCount} row errors` : <><Check size={12} /> Ready to review</>}</span></div><div className="table-scroll"><table className="portfolio-table"><thead><tr><th>Row</th><th>Company identity</th><th>Exchange</th><th>Current CMP</th><th>Imported CMP</th><th>Current base</th><th>Imported base</th><th>Current bull</th><th>Imported bull</th><th>Validation</th></tr></thead><tbody>{preview.rows.map((row) => <tr key={row.row_number}><td>{row.row_number}</td><td><b>{row.name || "—"}</b><small className="cell-sub">{row.symbol || "Missing symbol"} {row.is_new_company ? "· New company" : ""}</small></td><td>{row.exchange || "—"}</td><td>{row.current_cmp == null ? "—" : money(row.current_cmp)}</td><td>{row.cmp == null ? "—" : money(row.cmp)}</td><td>{row.current_base_target == null ? "—" : money(row.current_base_target)}</td><td>{row.base_target == null ? "—" : money(row.base_target)}</td><td>{row.current_bull_target == null ? "—" : money(row.current_bull_target)}</td><td>{row.bull_target == null ? "—" : money(row.bull_target)}</td><td className={row.errors.length ? "preview-error" : "validation-ok"}>{row.errors.length ? row.errors.map((message) => <div key={message}>{message}</div>) : "Valid"}{row.warnings.map((message) => <div className="formula-warning" key={message}>{message}</div>)}</td></tr>)}</tbody></table></div>
        {preview.formula_review_required && <label className="formula-review"><input type="checkbox" checked={reviewedCachedTargets} onChange={(event) => setReviewedCachedTargets(event.target.checked)} /> I reviewed cached formula target values; they may be stale because spreadsheets are not recalculated by the importer.</label>}
        {errorCount > 0 && <p className="form-error">Fix the row errors in the workbook and create a new preview before confirming.</p>}
        {preview.result && <div className="preview-note" role="status">Import confirmed: {preview.result.created} created, {preview.result.updated} targets updated, {preview.result.unchanged} unchanged.</div>}
        <div className="upload-actions"><button className="button primary" disabled={isConfirming || preview.status === "confirmed" || errorCount > 0 || (preview.formula_review_required && !reviewedCachedTargets)} onClick={confirm}>{isConfirming ? "Confirming…" : <><Check size={14} /> Confirm import</>}</button></div>
        <div className="preview-note"><CircleHelp size={14} /> Blank CMP values remain missing. Formula results without cached values are blocked; cached target formulas require explicit review. Unchanged targets create no new history revision.</div>
      </section>}
    </div><aside className="upload-aside"><section className="card upload-checklist"><h3>Map your own workbook</h3><div className="checklist-item"><Check size={13} /><span>Select the worksheet and header row; map each column explicitly.</span></div><div className="checklist-item"><Check size={13} /><span>Company identity is the exchange plus symbol pair.</span></div><div className="checklist-item"><Check size={13} /><span>Map at least one base or bull target; CMP and sector are optional.</span></div><div className="checklist-item"><Check size={13} /><span>Duplicate identities and invalid values are reported with row/cell references.</span></div><button className="text-link" onClick={downloadTemplate}>Download blank CSV template <Upload size={13} /></button></section><section className="card upload-history"><div className="section-head"><div><h3>Import safeguards</h3><p>Review and explicit confirmation</p></div><ShieldCheck size={15} className="muted-icon" /></div><div className="preview-note">Maximum 10 MB and 2,000 data rows. Confirmation is atomic and repeat-safe; target edits use version checks.</div></section></aside></div>
  </>;
}
