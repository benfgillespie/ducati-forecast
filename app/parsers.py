"""Parse the two source xlsx files into rows the app can store."""
import re
from datetime import datetime
from openpyxl import load_workbook


COUNTRY_FROM_ORDER = {
    "UNITED KINGDOM": "UK",
    "CHANNEL ISLANDS": "UK",
    "IRELAND": "UK",
    "SWEDEN": "SWE",
    "NORWAY": "NOR",
}

PLAN_SHEETS = [
    ("DUK_26", "DUK", 2026),
    ("DUK_27", "DUK", 2027),
    ("UK_26", "UK", 2026),
    ("SWE_26", "SWE", 2026),
    ("NOR_26", "NOR", 2026),
    ("UK_27", "UK", 2027),
    ("SWE_27", "SWE", 2027),
    ("NOR_27", "NOR", 2027),
]


def _to_int(v):
    if v is None or v == "":
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0


_JAN_RE = re.compile(r"^\s*JAN\s+(\d{4})\s*$", re.IGNORECASE)
_SECTION_END_LABELS = {"Dealer Inventory", "SHIPMENT", "Shipment"}


def _detect_plan_layout(ws):
    """Return (jan_col, year, model_col, super_col) by scanning the first ~6 header rows
    for a cell that reads 'JAN <year>'. Cols are 0-indexed for iter_rows tuples.
    """
    for r in range(1, 7):
        for c in range(1, min(ws.max_column + 1, 30)):
            v = ws.cell(r, c).value
            if not v:
                continue
            m = _JAN_RE.match(str(v))
            if m:
                jan_col_idx = c - 1
                return jan_col_idx, int(m.group(1)), jan_col_idx - 1, jan_col_idx - 2
    return None


def parse_plan(path):
    """Yield (country, plan_super, plan_model, year, month, qty) rows from the Sell In block
    of each country sheet. Layout is detected per sheet (UK_27 is shifted one column
    versus UK_26)."""
    wb = load_workbook(path, data_only=True)
    out = []
    for sheet_name, country, year in PLAN_SHEETS:
        if sheet_name not in wb.sheetnames:
            continue
        ws = wb[sheet_name]
        layout = _detect_plan_layout(ws)
        if layout is None:
            continue
        jan_col, detected_year, model_col, super_col = layout
        if detected_year != year:
            year = detected_year  # trust the sheet over the sheet-name suffix

        in_block = False
        for row in ws.iter_rows(values_only=True):
            if len(row) <= jan_col + 11:
                continue
            label = row[model_col]
            label_str = str(label).strip() if label else ""
            if not in_block:
                if "Sell In" in label_str and str(year) in label_str:
                    in_block = True
                continue
            # In block.
            if label_str in _SECTION_END_LABELS:
                break
            model_h = row[model_col]
            if model_h in (None, "TOT", 0, "0"):
                continue
            super_g = row[super_col]
            model_clean = str(model_h).strip()
            super_clean = str(super_g).strip() if super_g else None
            if model_clean in ("TOT", "TOTAL BO") or model_clean.startswith("TOTAL"):
                continue
            for month_idx in range(12):
                qty = _to_int(row[jan_col + month_idx])
                if qty != 0:
                    out.append((country, super_clean, model_clean, year, month_idx + 1, qty))
    wb.close()
    return out


_MATERIAL_PREFIX_RE = re.compile(r"^\s*(\S+)")


def extract_material_prefix(material):
    if not material:
        return None
    m = _MATERIAL_PREFIX_RE.match(str(material))
    return m.group(1) if m else None


def _to_iso_date(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    try:
        return datetime.fromisoformat(str(v)).date().isoformat()
    except ValueError:
        return str(v)


def parse_allocations(path):
    """Yield allocation dicts from the Allocations workbook.

    The Allocations sheet has the same column structure as the orders Export
    sheet but is sometimes shifted one column right (a leading sequential ID
    column) and may contain blank separator rows. We auto-detect the column
    offset by finding 'Order Number' in the header row, so both layouts
    work."""
    wb = load_workbook(path, data_only=True, read_only=True)
    ws = wb.active  # Allocations file we've seen uses 'Sheet1', not 'Export'
    out = []
    header_offset = None
    for row in ws.iter_rows(values_only=True):
        if header_offset is None:
            # Locate 'Order Number' in the header — that's column 0 of the
            # canonical Export layout, so its position tells us the offset.
            for i, v in enumerate(row):
                if v and str(v).strip().lower() == "order number":
                    header_offset = i
                    break
            if header_offset is None:
                # No header on this row; keep scanning until we find one.
                continue
            continue
        # Skip entirely-blank rows.
        if not any(row):
            continue
        # Slice off the leading filler columns, then unpack like Export.
        payload = (row[header_offset:] + (None,) * 18)[:18]
        (order_number, _mat_code, material, super_model, bike_model,
         _bike_color, _bike_type, _ec_status, _ec_date, _request_date,
         _po_number, _status_group, country_raw, _dealer, dealer_code,
         _customer, _order_create_date, _confirmed_delivery_date) = payload

        if not material and not order_number:
            continue

        out.append({
            "order_number": str(order_number) if order_number else None,
            "material_prefix": extract_material_prefix(material),
            "material_full": str(material) if material else None,
            "bike_super_model": super_model,
            "bike_model": bike_model,
            "country": COUNTRY_FROM_ORDER.get(
                str(country_raw).strip().upper() if country_raw else "",
                country_raw,
            ),
            "dealer_code": str(dealer_code) if dealer_code else None,
        })
    wb.close()
    return out


def parse_orders(path):
    """Yield order dicts from the Export sheet, filtered to open orders."""
    wb = load_workbook(path, data_only=True, read_only=True)
    if "Export" not in wb.sheetnames:
        wb.close()
        raise ValueError("Orders workbook has no 'Export' sheet")
    ws = wb["Export"]
    out = []
    header_seen = False
    for row in ws.iter_rows(values_only=True):
        if not header_seen:
            header_seen = True
            continue
        if not any(row):
            continue
        (order_number, _mat_code, material, super_model, bike_model,
         bike_color, bike_type, ec_status, _ec_date, request_date,
         _po_number, status_group, country_raw, dealer, dealer_code,
         _customer, order_create_date, confirmed_delivery_date) = (row + (None,) * 18)[:18]

        if status_group and str(status_group).strip().lower() != "open":
            continue

        out.append({
            "order_number": str(order_number) if order_number else None,
            "material_prefix": extract_material_prefix(material),
            "material_full": str(material) if material else None,
            "bike_super_model": super_model,
            "bike_model": bike_model,
            "bike_color": bike_color,
            "bike_type": bike_type,
            "end_customer_status": ec_status,
            "country": COUNTRY_FROM_ORDER.get(str(country_raw).strip().upper() if country_raw else "", country_raw),
            "dealer": dealer,
            "dealer_code": str(dealer_code) if dealer_code else None,
            "request_date": _to_iso_date(request_date),
            "order_creation_date": _to_iso_date(order_create_date),
            "confirmed_delivery_date": _to_iso_date(confirmed_delivery_date),
            "order_status_group": status_group,
        })
    wb.close()
    return out
