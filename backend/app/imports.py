import csv
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
from pathlib import PurePath
from typing import Any
from uuid import uuid4
from zipfile import BadZipFile

from fastapi import HTTPException, UploadFile
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import AuditLog, Company, ImportBatch, ImportRow, TargetRevision
from .schemas import ImportRowUpdate

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_IMPORT_ROWS = 2000
ALLOWED_EXTENSIONS = {".xlsx", ".csv"}
MAPPED_FIELDS = (
    "exchange_column",
    "symbol_column",
    "name_column",
    "base_target_column",
    "bull_target_column",
    "cmp_column",
    "sector_column",
)


def _column_index(column: str | None) -> int | None:
    if not column:
        return None
    value = column.strip().upper()
    if not re.fullmatch(r"[A-Z]{1,3}", value):
        raise HTTPException(status_code=422, detail=f"Invalid column reference: {column}")
    index = 0
    for char in value:
        index = index * 26 + ord(char) - ord("A") + 1
    return index - 1


def _column_name(index: int) -> str:
    name = ""
    while index >= 0:
        index, remainder = divmod(index, 26)
        name = chr(65 + remainder) + name
        index -= 1
    return name


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _decimal(value: Any) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise InvalidOperation
    try:
        cleaned = str(value).strip().replace(",", "").replace("₹", "").strip()
        result = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        raise InvalidOperation from None
    if not result.is_finite():
        raise InvalidOperation
    return result


async def read_upload(file: UploadFile) -> tuple[str, bytes]:
    filename = PurePath(file.filename or "").name
    extension = PurePath(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Only .xlsx and .csv files are supported.")
    content = await file.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="Upload exceeds the 10 MB limit.")
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    return filename or f"upload{extension}", content


def inspect_upload(content: bytes, filename: str, header_row: int) -> dict[str, Any]:
    if header_row < 1:
        raise HTTPException(status_code=422, detail="Header row must be 1 or greater.")
    if PurePath(filename).suffix.lower() == ".csv":
        rows = _read_csv(content)
        header_values = rows[header_row - 1] if len(rows) >= header_row else []
        sheets = [{"name": "CSV", "headers": _headers(header_values)}]
        return {"file_type": "csv", "header_row": header_row, "sheets": sheets}
    try:
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=False)
        sheets = []
        for sheet in workbook.worksheets:
            values = next(
                sheet.iter_rows(
                    min_row=header_row, max_row=header_row, values_only=True
                ),
                (),
            )
            sheets.append({"name": sheet.title, "headers": _headers(values)})
        workbook.close()
    except (ValueError, OSError, KeyError, BadZipFile, InvalidFileException) as error:
        raise HTTPException(status_code=400, detail=f"Could not read workbook: {error}") from error
    if not sheets:
        raise HTTPException(status_code=400, detail="The workbook has no worksheets.")
    return {"file_type": "xlsx", "header_row": header_row, "sheets": sheets}


def _headers(values: Any) -> list[dict[str, str | None]]:
    return [
        {"column": _column_name(index), "header": _text(value)}
        for index, value in enumerate(values)
    ]


def _read_csv(content: bytes) -> list[list[str]]:
    try:
        text = content.decode("utf-8-sig")
        return list(csv.reader(StringIO(text, newline="")))
    except (UnicodeDecodeError, csv.Error) as error:
        raise HTTPException(status_code=400, detail=f"Could not read CSV: {error}") from error


def _source_rows(
    content: bytes, filename: str, sheet_name: str | None, header_row: int
) -> tuple[list[tuple[int, tuple[Any, ...], tuple[Any, ...] | None]], list[str]]:
    if header_row < 1:
        raise HTTPException(status_code=422, detail="Header row must be 1 or greater.")
    if PurePath(filename).suffix.lower() == ".csv":
        rows = _read_csv(content)
        if header_row > len(rows):
            raise HTTPException(status_code=422, detail="The selected header row does not exist.")
        values = [(number, tuple(row), None) for number, row in enumerate(rows, start=1)]
        return values, ["CSV"]
    try:
        formulas_book = load_workbook(BytesIO(content), read_only=True, data_only=False)
        cached_book = load_workbook(BytesIO(content), read_only=True, data_only=True)
    except (ValueError, OSError, KeyError, BadZipFile, InvalidFileException) as error:
        raise HTTPException(status_code=400, detail=f"Could not read workbook: {error}") from error
    names = formulas_book.sheetnames
    selected_sheet = sheet_name or (names[0] if names else None)
    if selected_sheet not in names:
        formulas_book.close()
        cached_book.close()
        raise HTTPException(status_code=422, detail="Select a worksheet present in the workbook.")
    formula_sheet = formulas_book[selected_sheet]
    cached_sheet = cached_book[selected_sheet]
    rows = []
    data_count = 0
    formula_rows = formula_sheet.iter_rows(min_row=1)
    cached_rows = cached_sheet.iter_rows(min_row=1)
    for number, (cells, cached_cells) in enumerate(zip(formula_rows, cached_rows), start=1):
        values = tuple(
            cached_cells[index].value if index < len(cached_cells) else None
            for index in range(len(cells))
        )
        formulas = tuple(cell.value if cell.data_type == "f" else None for cell in cells)
        populated = any(value is not None for value in values) or any(
            formula is not None for formula in formulas
        )
        if number == header_row or populated:
            rows.append((number, values, formulas))
        if number > header_row and populated:
            data_count += 1
            if data_count > MAX_IMPORT_ROWS:
                formulas_book.close()
                cached_book.close()
                raise HTTPException(
                    status_code=413,
                    detail=f"Import exceeds the {MAX_IMPORT_ROWS} data-row limit.",
                )
    formulas_book.close()
    cached_book.close()
    return rows, names


def _infer_target_direction(
    target_val: Decimal | None,
    cmp_val: Decimal | None,
    default_dir: str = "above",
) -> str:
    """Infer target direction when CMP is available in Excel, or use default."""
    if target_val is None:
        return "pending"
    if cmp_val is not None and cmp_val > 0:
        return "below" if target_val < cmp_val else "above"
    return default_dir


def create_preview(
    session: Session,
    *,
    filename: str,
    content: bytes,
    sheet_name: str | None,
    header_row: int,
    mapping: dict[str, str | None],
    uploaded_by: str = "Srushti Kadam",
) -> dict[str, Any]:
    if not mapping.get("exchange_column") or not mapping.get("symbol_column") or not mapping.get("name_column"):
        raise HTTPException(
            status_code=422,
            detail="Map exchange, symbol, and company name columns.",
        )
    if not mapping.get("base_target_column") and not mapping.get("bull_target_column"):
        raise HTTPException(
            status_code=422,
            detail="Map at least one base target or bull target column.",
        )
    column_indexes = {
        field: _column_index(mapping.get(field)) for field in MAPPED_FIELDS
    }
    configured = [index for index in column_indexes.values() if index is not None]
    if len(configured) != len(set(configured)):
        raise HTTPException(status_code=422, detail="Each mapped field must use a different column.")

    source_rows, sheet_names = _source_rows(content, filename, sheet_name, header_row)
    if not source_rows or not any(number == header_row for number, _, _ in source_rows):
        raise HTTPException(status_code=422, detail="The selected header row does not exist.")
    if PurePath(filename).suffix.lower() == ".xlsx" and sheet_name not in sheet_names:
        raise HTTPException(status_code=422, detail="Select a worksheet present in the workbook.")

    header_values = next(values for number, values, _ in source_rows if number == header_row)
    for field in MAPPED_FIELDS:
        index = column_indexes[field]
        if index is not None and index >= len(header_values):
            raise HTTPException(
                status_code=422,
                detail=f"Mapped column {mapping[field]} is not present in the selected header row.",
            )
    company_lookup = {
        (company.exchange, company.symbol): company
        for company in session.scalars(select(Company))
    }
    rows: list[ImportRow] = []
    seen: dict[tuple[str | None, str | None], list[ImportRow]] = {}
    formula_review_required = False

    data_count = 0
    for row_number, values, formulas in source_rows:
        if row_number <= header_row or not any(value is not None and str(value).strip() for value in values):
            continue
        data_count += 1
        if data_count > MAX_IMPORT_ROWS:
            raise HTTPException(
                status_code=413,
                detail=f"Import exceeds the {MAX_IMPORT_ROWS} data-row limit.",
            )

        def value_for(field: str) -> Any:
            index = column_indexes[field]
            return values[index] if index is not None and index < len(values) else None

        def coordinate(field: str) -> str:
            col = (mapping.get(field) or "").strip().upper()
            return f"{col}{row_number}" if col else f"Row {row_number}"

        def formula_for(field: str) -> bool:
            index = column_indexes[field]
            return bool(
                formulas is not None
                and index is not None
                and index < len(formulas)
                and formulas[index] is not None
            )

        cell_refs: dict[str, str] = {}
        for field in MAPPED_FIELDS:
            col = (mapping.get(field) or "").strip().upper()
            if col:
                short_key = field.replace("_column", "")
                cell_refs[short_key] = f"{col}{row_number}"

        errors: list[str] = []
        warnings: list[str] = []
        exchange = (_text(value_for("exchange_column")) or "").upper()
        symbol = (_text(value_for("symbol_column")) or "").upper()
        name = _text(value_for("name_column"))
        if exchange not in {"NSE", "BSE"}:
            errors.append(f"{coordinate('exchange_column')}: exchange must be NSE or BSE.")
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9.&_-]{0,31}", symbol):
            errors.append(
                f"{coordinate('symbol_column')}: symbol must be 1-32 letters, digits, or . & _ - characters."
            )
        if not name or len(name) > 200:
            errors.append(f"{coordinate('name_column')}: company name is required and must be at most 200 characters.")

        parsed_values: dict[str, Decimal | None] = {}
        update_flags = {
            "cmp": column_indexes["cmp_column"] is not None,
            "base_target": column_indexes["base_target_column"] is not None,
            "bull_target": column_indexes["bull_target_column"] is not None,
        }
        for field in ("cmp", "base_target", "bull_target"):
            mapping_field = f"{field}_column"
            index = column_indexes[mapping_field]
            if index is None:
                parsed_values[field] = None
                continue
            raw_value = value_for(mapping_field)
            has_formula = formula_for(mapping_field)
            try:
                parsed = _decimal(raw_value)
            except InvalidOperation:
                errors.append(f"{coordinate(mapping_field)}: {field.replace('_', ' ').upper()} must be a finite number.")
                parsed = None
            parsed_values[field] = parsed
            if has_formula and parsed is None:
                errors.append(
                    f"{coordinate(mapping_field)}: formula result is missing; calculate and save the workbook before importing."
                )
            elif has_formula and field != "cmp" and parsed is not None:
                formula_review_required = True
                warnings.append(
                    f"{coordinate(mapping_field)}: cached formula value needs freshness review."
                )
            if parsed is not None and parsed <= 0:
                errors.append(f"{coordinate(mapping_field)}: {field.replace('_', ' ').upper()} must be greater than zero.")
            if parsed is not None and (
                parsed.as_tuple().exponent < -2 or parsed >= Decimal("1000000000000")
            ):
                errors.append(
                    f"{coordinate(mapping_field)}: {field.replace('_', ' ').upper()} supports at most 12 integer digits and 2 decimal places."
                )
        if not any(parsed_values[field] is not None for field in ("base_target", "bull_target")):
            errors.append(f"Row {row_number}: at least one target value is required.")

        sector = _text(value_for("sector_column"))
        if sector is not None and len(sector) > 100:
            errors.append(f"{coordinate('sector_column')}: sector must be at most 100 characters.")
        identity = (exchange, symbol)
        existing = company_lookup.get(identity) if exchange in {"NSE", "BSE"} and symbol else None
        if existing and name and existing.name.strip().casefold() != name.strip().casefold():
            errors.append(
                f"Row {row_number}: company name does not match the existing {exchange}:{symbol} identity."
            )

        base_dir = (
            existing.base_direction
            if existing and existing.base_direction != "pending" and parsed_values["cmp"] is None
            else _infer_target_direction(parsed_values["base_target"], parsed_values["cmp"], "above")
        )
        bull_dir = (
            existing.bull_direction
            if existing and existing.bull_direction != "pending" and parsed_values["cmp"] is None
            else _infer_target_direction(parsed_values["bull_target"], parsed_values["cmp"], "above")
        )

        row = ImportRow(
            row_number=row_number,
            exchange=exchange or None,
            symbol=symbol or None,
            name=name,
            sector=sector,
            expected_version=existing.version if existing else None,
            cmp=parsed_values["cmp"],
            base_target=parsed_values["base_target"],
            bull_target=parsed_values["bull_target"],
            update_cmp=update_flags["cmp"],
            update_sector=column_indexes["sector_column"] is not None,
            update_base_target=update_flags["base_target"],
            update_bull_target=update_flags["bull_target"],
            errors=errors,
            warnings=warnings,
            base_direction=base_dir,
            bull_direction=bull_dir,
            cell_refs=cell_refs,
            approval_status="approved",
        )
        rows.append(row)
        seen.setdefault(identity, []).append(row)

    if not rows:
        raise HTTPException(status_code=422, detail="The selected worksheet has no data rows.")
    for identity, duplicates in seen.items():
        if len(duplicates) > 1:
            label = ":".join(part or "" for part in identity)
            for row in duplicates:
                row.errors.append(
                    f"Row {row.row_number}: duplicate company identity {label} in this file."
                )

    batch = ImportBatch(
        id=str(uuid4()),
        filename=filename[:255],
        status="preview",
        formula_review_required=formula_review_required,
        created_at=datetime.now(timezone.utc),
        uploaded_by=uploaded_by,
        sheet_name=sheet_name or ("CSV" if PurePath(filename).suffix.lower() == ".csv" else None),
        header_row=header_row,
        rows=rows,
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)
    return serialize_batch(batch, company_lookup)


def revalidate_batch_rows(session: Session, batch: ImportBatch) -> dict[tuple[str, str], Company]:
    company_lookup = {
        (company.exchange, company.symbol): company
        for company in session.scalars(select(Company))
    }
    seen: dict[tuple[str | None, str | None], list[ImportRow]] = {}
    for row in batch.rows:
        if row.approval_status == "rejected":
            row.errors = []
            continue
        errors: list[str] = []
        exchange = (row.exchange or "").strip().upper()
        symbol = (row.symbol or "").strip().upper()
        name = (row.name or "").strip()
        row.exchange = exchange or None
        row.symbol = symbol or None
        row.name = name or None

        if exchange not in {"NSE", "BSE"}:
            errors.append(f"Row {row.row_number}: exchange must be NSE or BSE.")
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9.&_-]{0,31}", symbol):
            errors.append(
                f"Row {row.row_number}: symbol must be 1-32 letters, digits, or . & _ - characters."
            )
        if not name or len(name) > 200:
            errors.append(f"Row {row.row_number}: company name is required and must be at most 200 characters.")

        for field_name, val in (("cmp", row.cmp), ("base_target", row.base_target), ("bull_target", row.bull_target)):
            if val is not None:
                if val <= 0:
                    errors.append(f"Row {row.row_number}: {field_name.replace('_', ' ').upper()} must be greater than zero.")
                if val.as_tuple().exponent < -2 or val >= Decimal("1000000000000"):
                    errors.append(
                        f"Row {row.row_number}: {field_name.replace('_', ' ').upper()} supports at most 12 integer digits and 2 decimal places."
                    )
        if row.base_target is None and row.bull_target is None:
            errors.append(f"Row {row.row_number}: at least one target value is required.")

        if row.sector is not None and len(row.sector) > 100:
            errors.append(f"Row {row.row_number}: sector must be at most 100 characters.")

        identity = (exchange, symbol)
        existing = company_lookup.get(identity) if exchange in {"NSE", "BSE"} and symbol else None
        if existing and name and existing.name.strip().casefold() != name.strip().casefold():
            errors.append(
                f"Row {row.row_number}: company name does not match the existing {exchange}:{symbol} identity."
            )
        row.expected_version = existing.version if existing else None
        row.errors = errors
        seen.setdefault(identity, []).append(row)

    for identity, duplicates in seen.items():
        if len(duplicates) > 1:
            label = ":".join(part or "" for part in identity)
            for row in duplicates:
                row.errors = [*row.errors, f"Row {row.row_number}: duplicate company identity {label} in this file."]
    return company_lookup


def update_preview_row(
    session: Session,
    batch_id: str,
    row_number: int,
    update_data: ImportRowUpdate,
) -> dict[str, Any]:
    batch = get_batch(session, batch_id)
    if batch.status != "preview":
        raise HTTPException(status_code=409, detail="Only preview imports can be edited.")
    target_row = next((r for r in batch.rows if r.row_number == row_number), None)
    if target_row is None:
        raise HTTPException(status_code=404, detail=f"Row {row_number} not found in import batch.")

    fields = update_data.model_fields_set
    if "exchange" in fields:
        target_row.exchange = (update_data.exchange or "").strip().upper() or None
    if "symbol" in fields:
        target_row.symbol = (update_data.symbol or "").strip().upper() or None
    if "name" in fields:
        target_row.name = (update_data.name or "").strip() or None
    if "sector" in fields:
        target_row.sector = (update_data.sector or "").strip() or None
        target_row.update_sector = True
    if "cmp" in fields:
        target_row.cmp = update_data.cmp
        target_row.update_cmp = True
    if "base_target" in fields:
        target_row.base_target = update_data.base_target
        target_row.update_base_target = True
    if "bull_target" in fields:
        target_row.bull_target = update_data.bull_target
        target_row.update_bull_target = True
    if "base_direction" in fields:
        target_row.base_direction = update_data.base_direction
    if "bull_direction" in fields:
        target_row.bull_direction = update_data.bull_direction
    if "approval_status" in fields and update_data.approval_status is not None:
        target_row.approval_status = update_data.approval_status

    company_lookup = revalidate_batch_rows(session, batch)
    session.commit()
    session.refresh(batch)
    return serialize_batch(batch, company_lookup)


def serialize_batch(
    batch: ImportBatch, company_lookup: dict[tuple[str, str], Company] | None = None
) -> dict[str, Any]:
    company_lookup = company_lookup or {}
    rows = []
    for row in batch.rows:
        existing = company_lookup.get((row.exchange or "", row.symbol or ""))
        rows.append(
            {
                "row_number": row.row_number,
                "exchange": row.exchange,
                "symbol": row.symbol,
                "name": row.name,
                "sector": row.sector,
                "cmp": float(row.cmp) if row.cmp is not None else None,
                "base_target": float(row.base_target) if row.base_target is not None else None,
                "bull_target": float(row.bull_target) if row.bull_target is not None else None,
                "base_direction": row.base_direction or (existing.base_direction if existing else "above"),
                "bull_direction": row.bull_direction or (existing.bull_direction if existing else "above"),
                "cell_refs": row.cell_refs or {},
                "approval_status": row.approval_status or "approved",
                "current_cmp": float(existing.cmp) if existing and existing.cmp is not None else None,
                "current_base_target": float(existing.base_target) if existing and existing.base_target is not None else None,
                "current_bull_target": float(existing.bull_target) if existing and existing.bull_target is not None else None,
                "current_version": existing.version if existing else None,
                "is_new_company": existing is None,
                "errors": row.errors,
                "warnings": row.warnings,
            }
        )
    active_rows = [r for r in rows if r["approval_status"] != "rejected"]
    invalid_rows = sum(1 for r in active_rows if r["errors"])
    valid_rows = len(active_rows) - invalid_rows
    return {
        "id": batch.id,
        "filename": batch.filename,
        "status": batch.status,
        "version_number": batch.version_number,
        "uploaded_by": batch.uploaded_by,
        "approved_by": batch.approved_by,
        "approved_at": batch.approved_at.isoformat() if batch.approved_at else None,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "sheet_name": batch.sheet_name,
        "header_row": batch.header_row,
        "formula_review_required": batch.formula_review_required,
        "total_rows": len(rows),
        "valid_rows": valid_rows,
        "invalid_rows": invalid_rows,
        "rows": rows,
        "result": batch.result,
        "changes_snapshot": batch.changes_snapshot,
    }


def get_batch(session: Session, batch_id: str) -> ImportBatch:
    batch = session.scalar(
        select(ImportBatch)
        .options(selectinload(ImportBatch.rows))
        .where(ImportBatch.id == batch_id)
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="Import preview not found.")
    return batch


def restore_import_version(
    session: Session,
    batch_id: str,
    *,
    actor: str = "Srushti Kadam",
) -> dict[str, Any]:
    batch = get_batch(session, batch_id)
    if batch.status not in ("confirmed", "restored") or not batch.changes_snapshot:
        raise HTTPException(
            status_code=409,
            detail="Only confirmed import versions with a saved snapshot can be restored.",
        )
    now = datetime.now(timezone.utc)
    restored_count = 0
    for item in batch.changes_snapshot:
        exchange = item.get("exchange")
        symbol = item.get("symbol")
        if not exchange or not symbol:
            continue
        company = session.get(Company, (exchange, symbol))
        if company is None:
            continue
        new_base = Decimal(str(item["new_base_target"])) if item.get("new_base_target") is not None else None
        new_bull = Decimal(str(item["new_bull_target"])) if item.get("new_bull_target") is not None else None
        old_targets = (company.base_target, company.bull_target)
        company.base_target = new_base
        company.bull_target = new_bull
        if item.get("base_direction"):
            company.base_direction = item["base_direction"]
        if item.get("bull_direction"):
            company.bull_direction = item["bull_direction"]
        if old_targets != (company.base_target, company.bull_target):
            company.version += 1
            restored_count += 1
            session.add(
                TargetRevision(
                    exchange=company.exchange,
                    symbol=company.symbol,
                    version=company.version,
                    previous_base_target=old_targets[0],
                    previous_bull_target=old_targets[1],
                    base_target=company.base_target,
                    bull_target=company.bull_target,
                    base_direction=company.base_direction,
                    bull_direction=company.bull_direction,
                    source="restore",
                    import_id=batch.id,
                    changed_by=actor,
                    changed_at=now,
                )
            )
    session.add(
        AuditLog(
            action="import_version_restored",
            actor=actor,
            entity_type="import_batch",
            entity_id=batch.id,
            details={
                "filename": batch.filename,
                "version_number": batch.version_number,
                "restored_companies": restored_count,
            },
            created_at=now,
        )
    )
    session.commit()
    return {
        "id": batch.id,
        "version_number": batch.version_number,
        "status": "restored",
        "restored_companies": restored_count,
    }
