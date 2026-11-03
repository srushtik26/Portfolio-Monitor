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

from .models import Company, ImportBatch, ImportRow

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
        result = Decimal(str(value).strip().replace(",", ""))
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


def create_preview(
    session: Session,
    *,
    filename: str,
    content: bytes,
    sheet_name: str | None,
    header_row: int,
    mapping: dict[str, str | None],
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
            return f"{mapping[field]}{row_number}"

        def formula_for(field: str) -> bool:
            index = column_indexes[field]
            return bool(
                formulas is not None
                and index is not None
                and index < len(formulas)
                and formulas[index] is not None
            )

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
        rows=rows,
    )
    session.add(batch)
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
                "current_cmp": float(existing.cmp) if existing and existing.cmp is not None else None,
                "current_base_target": float(existing.base_target) if existing and existing.base_target is not None else None,
                "current_bull_target": float(existing.bull_target) if existing and existing.bull_target is not None else None,
                "current_version": existing.version if existing else None,
                "is_new_company": existing is None,
                "errors": row.errors,
                "warnings": row.warnings,
            }
        )
    return {
        "id": batch.id,
        "filename": batch.filename,
        "status": batch.status,
        "formula_review_required": batch.formula_review_required,
        "rows": rows,
        "result": batch.result,
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
