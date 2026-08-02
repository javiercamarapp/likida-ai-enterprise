# -*- coding: utf-8 -*-
"""
csv_parser.py — Parser de estados de cuenta bancarios en CSV.

Soporta los formatos CSV más comunes de bancos mexicanos:
  - BBVA: fecha, descripción, monto, saldo
  - Banorte: fecha, referencia, descripción, cargo, abono
  - HSBC: fecha, descripción, importe, saldo
  - Genérico: fecha, descripción, monto

Maneja montos formateados con comas y signo de pesos ($1,000.50).
Auto-detecta el delimitador (coma, tabulador, punto y coma).
"""
from __future__ import annotations

import csv
import io
import re
import uuid as _uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from b2b_ai.features.conciliacion.models import BankTransaction


def parse_amount(raw: str) -> float:
    """Parse a Mexican-formatted amount string to float.

    Handles: "$1,000.50", "1,000.50", "-$500.00", "(500.00)", "1000,50"

    Returns:
        float value of the parsed amount.

    Raises:
        ValueError: If the string cannot be parsed as an amount.
    """
    if not raw or not raw.strip():
        raise ValueError("Empty amount string")

    cleaned = raw.strip()

    # Detect negative: parentheses or leading minus
    negative = False
    if cleaned.startswith("(") and cleaned.endswith(")"):
        negative = True
        cleaned = cleaned[1:-1]
    elif cleaned.startswith("-"):
        negative = True
        cleaned = cleaned[1:]

    # Remove currency symbols and whitespace
    cleaned = re.sub(r'[$\s]', '', cleaned)

    # Handle Mexican format: dots as thousands separator, comma as decimal
    # e.g., "1.234.567,89" -> "1234567.89"
    if re.match(r'^\d{1,3}(\.\d{3})+(,\d+)?$', cleaned):
        cleaned = cleaned.replace('.', '').replace(',', '.')
    # Handle standard format: comma as thousands separator, dot as decimal
    # e.g., "1,234,567.89" -> "1234567.89"
    elif ',' in cleaned and '.' in cleaned:
        cleaned = cleaned.replace(',', '')
    # Handle comma-only (ambiguous): assume comma is thousands separator
    # e.g., "1,000" -> "1000"
    elif ',' in cleaned:
        # If comma is followed by exactly 2 digits, treat as decimal
        if re.match(r'^\d+,\d{2}$', cleaned):
            cleaned = cleaned.replace(',', '.')
        else:
            cleaned = cleaned.replace(',', '')

    result = float(cleaned)
    return -result if negative else result


def _detect_delimiter(sample: str) -> str:
    """Auto-detect CSV delimiter from a sample of the file."""
    # Count occurrences of common delimiters
    delimiters = {',': 0, '\t': 0, ';': 0, '|': 0}
    for ch in sample[:1000]:
        if ch in delimiters:
            delimiters[ch] += 1

    # Return the most common delimiter
    best = max(delimiters, key=lambda k: delimiters[k])
    return best if delimiters[best] > 0 else ','


def _detect_date_format(date_str: str) -> Optional[str]:
    """Detect date format from a sample date string."""
    formats = [
        "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y",
        "%d/%m/%y", "%m/%d/%y", "%Y/%m/%d",
        "%d-%b-%Y", "%d-%b-%y",  # e.g., 15-Jan-2024
    ]
    for fmt in formats:
        try:
            datetime.strptime(date_str.strip(), fmt)
            return fmt
        except ValueError:
            continue
    return None


def parse_bank_csv(
    csv_content: str,
    date_column: Optional[str] = None,
    amount_column: Optional[str] = None,
    description_column: Optional[str] = None,
    reference_column: Optional[str] = None,
    debit_column: Optional[str] = None,
    credit_column: Optional[str] = None,
) -> List[BankTransaction]:
    """Parse a bank CSV statement into BankTransaction objects.

    Auto-detects delimiter, date format, and column mapping.

    Args:
        csv_content: Raw CSV string content.
        date_column: Name or index of date column (auto-detected if None).
        amount_column: Name or index of amount column (auto-detected if None).
        description_column: Name or index of description column.
        reference_column: Name or index of reference column.
        debit_column: Name or index of debit column (for banks that split).
        credit_column: Name or index of credit column (for banks that split).

    Returns:
        List of BankTransaction objects.

    Raises:
        ValueError: If CSV format cannot be detected or parsed.
    """
    if not csv_content or not csv_content.strip():
        raise ValueError("Empty CSV content")

    # Detect delimiter
    delimiter = _detect_delimiter(csv_content)

    # Try parsing with headers first
    reader = csv.DictReader(io.StringIO(csv_content), delimiter=delimiter)
    if reader.fieldnames is None:
        # Try without headers (positional)
        return _parse_positional_csv(csv_content, delimiter)

    # Normalize column names (lowercase, strip)
    col_map = {col.strip().lower(): col for col in reader.fieldnames}

    # Auto-detect columns
    date_col = date_column or _find_column(col_map, [
        'fecha', 'date', 'fecha operacion', 'fecha_movimiento',
        'transaction date', 'posting date', 'fec', 'f. operacion',
    ])
    amount_col = amount_column or _find_column(col_map, [
        'monto', 'amount', 'importe', 'total', 'valor',
        'transaction amount', 'importe neto',
    ])
    desc_col = description_column or _find_column(col_map, [
        'descripcion', 'description', 'concepto', 'detalle',
        'narrativa', 'referencia bancaria', 'glosa',
    ])
    ref_col = reference_column or _find_column(col_map, [
        'referencia', 'reference', 'folio', 'no. referencia',
        'referencia numerica', 'num referencia',
    ])
    debit_col = debit_column or _find_column(col_map, [
        'cargo', 'debit', 'retiro', 'abono cargo',
    ])
    credit_col = credit_column or _find_column(col_map, [
        'abono', 'credit', 'deposito', 'credito',
    ])

    # Detect date format from first row
    date_fmt = None
    transactions: List[BankTransaction] = []

    for i, row in enumerate(reader):
        try:
            # Parse date
            date_str = row.get(date_col, '').strip() if date_col else ''
            if not date_str:
                continue

            if date_fmt is None:
                date_fmt = _detect_date_format(date_str)
                if date_fmt is None:
                    raise ValueError(
                        f"Cannot detect date format for '{date_str}'"
                    )

            parsed_date = datetime.strptime(date_str, date_fmt)
            txn_date = parsed_date.strftime("%Y-%m-%d")

            # Parse amount
            amount = 0.0
            if amount_col and row.get(amount_col, '').strip():
                amount = parse_amount(row[amount_col])
            elif debit_col and credit_col:
                # Separate debit/credit columns
                debit_str = row.get(debit_col, '').strip() or '0'
                credit_str = row.get(credit_col, '').strip() or '0'
                debit = parse_amount(debit_str) if debit_str and debit_str != '0' else 0.0
                credit = parse_amount(credit_str) if credit_str and credit_str != '0' else 0.0
                amount = credit - debit

            # Parse description and reference
            description = row.get(desc_col, '').strip() if desc_col else ''
            reference = row.get(ref_col, '').strip() if ref_col else ''

            # Skip empty or header-like rows
            if amount == 0 and not description:
                continue

            transactions.append(BankTransaction(
                id=f"CSV-{_uuid.uuid4().hex[:8]}",
                date=txn_date,
                amount=round(amount, 2),
                description=description,
                reference=reference,
                raw_row=i,
            ))

        except (ValueError, KeyError) as e:
            # Skip unparseable rows with warning
            continue

    if not transactions:
        raise ValueError(
            "No valid transactions found in CSV. "
            "Check that the file has date and amount columns."
        )

    return transactions


def _find_column(col_map: Dict[str, str], candidates: List[str]) -> Optional[str]:
    """Find a column by trying candidate names."""
    for candidate in candidates:
        if candidate in col_map:
            return col_map[candidate]
    return None


def _parse_positional_csv(
    content: str,
    delimiter: str,
) -> List[BankTransaction]:
    """Fallback: parse CSV without headers, assuming date, description, amount."""
    reader = csv.reader(io.StringIO(content), delimiter=delimiter)
    transactions: List[BankTransaction] = []
    date_fmt = None

    for i, row in enumerate(reader):
        if len(row) < 3:
            continue

        # Assume: date, description, amount
        date_str = row[0].strip()
        if not date_fmt:
            date_fmt = _detect_date_format(date_str)
            if not date_fmt:
                continue

        try:
            parsed_date = datetime.strptime(date_str, date_fmt)
            txn_date = parsed_date.strftime("%Y-%m-%d")
            description = row[1].strip()
            amount = parse_amount(row[2])

            transactions.append(BankTransaction(
                id=f"CSV-{_uuid.uuid4().hex[:8]}",
                date=txn_date,
                amount=round(amount, 2),
                description=description,
                raw_row=i,
            ))
        except (ValueError, IndexError):
            continue

    return transactions
