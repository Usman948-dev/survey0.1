"""PESCO / LESCO project data: master sheet import, dropdown options, admin column order."""
import csv
import io
import re

from fastapi import HTTPException
from sqlalchemy.orm import Session

from . import config
from .models import ProjectOption

# ----------------------------------------------------------------- options

# Dropdown lists the admin can edit. TF rating carries the cable width the app fills in.
DEFAULT_OPTIONS = {
    "tfRating": [("10", "70 mm"), ("15", "70 mm"), ("25", "70 mm"),
                 ("50", "120 mm"), ("100", "185 mm"), ("200", "300 mm")],
    "poleType": [("CONCRETE", ""), ("STEEL", "")],
    "earthing": [("Yes", ""), ("No", "")],
}
EDITABLE_FIELDS = tuple(DEFAULT_OPTIONS)


def seed_options(db: Session) -> None:
    for project in config.PROJECTS:
        if db.query(ProjectOption).filter_by(project=project).first():
            continue
        for field, items in DEFAULT_OPTIONS.items():
            for pos, (value, extra) in enumerate(items):
                db.add(ProjectOption(project=project, field=field, value=value, extra=extra, position=pos))
    db.commit()


def read_options(db: Session, project: str) -> dict:
    rows = (db.query(ProjectOption).filter_by(project=project)
            .order_by(ProjectOption.field, ProjectOption.position, ProjectOption.id).all())
    out: dict = {}
    for field in EDITABLE_FIELDS:
        mine = [r for r in rows if r.field == field]
        if not mine:  # never leave the app without choices
            mine = [ProjectOption(field=field, value=v, extra=e) for v, e in DEFAULT_OPTIONS[field]]
        if field == "tfRating":
            out[field] = [{"value": r.value, "cable_width": r.extra} for r in mine]
        else:
            out[field] = [r.value for r in mine]
    return out


def write_options(db: Session, project: str, body: dict) -> dict:
    """Replace the lists named in [body]; lists not named stay as they are."""
    unknown = set(body) - set(EDITABLE_FIELDS)
    if unknown:
        raise HTTPException(422, f"Unknown option list: {', '.join(sorted(unknown))}")
    new_rows: dict[str, list[tuple[str, str]]] = {}
    for field, items in body.items():
        if not isinstance(items, list) or not items:
            raise HTTPException(422, f"{field}: at least one option is required")
        seen, rows = set(), []
        for it in items:
            if field == "tfRating":
                if not isinstance(it, dict):
                    raise HTTPException(422, "tfRating: use {value, cable_width}")
                value, extra = str(it.get("value", "")).strip(), str(it.get("cable_width", "")).strip()
                if not re.fullmatch(r"\d{1,4}", value):
                    raise HTTPException(422, f"tfRating: '{value}' must be a whole number")
                if not extra:
                    raise HTTPException(422, f"tfRating {value}: cable width is required")
            else:
                value, extra = str(it).strip(), ""
                if not value:
                    raise HTTPException(422, f"{field}: empty option")
            if len(value) > 60 or len(extra) > 60:
                raise HTTPException(422, f"{field}: '{value}' is too long")
            if value.lower() in seen:
                raise HTTPException(422, f"{field}: '{value}' is listed twice")
            seen.add(value.lower())
            rows.append((value, extra))
        new_rows[field] = rows
    for field, rows in new_rows.items():
        db.query(ProjectOption).filter_by(project=project, field=field).delete()
        for pos, (value, extra) in enumerate(rows):
            db.add(ProjectOption(project=project, field=field, value=value, extra=extra, position=pos))
    db.commit()
    return read_options(db, project)


# ------------------------------------------------------------ master sheet

_HEADERS = {
    "listnumber": "list_number", "circle": "circle", "division": "division",
    "subdivision": "sub_division", "feedername": "feeder_name", "feedercode": "feeder_code",
}
_REQUIRED = ("list_number", "circle", "division", "sub_division")


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():  # Excel stores 1001 as 1001.0
        return str(int(v))
    return str(v).strip()


def _sheet_rows(data: bytes, filename: str):
    name = (filename or "").lower()
    if name.endswith(".csv"):
        text = data.decode("utf-8-sig")
        return [list(r) for r in csv.reader(io.StringIO(text))]
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        try:
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception:
            raise HTTPException(422, "Could not read that Excel file")
        return [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    raise HTTPException(422, "Upload an .xlsx or .csv file")


def parse_master(data: bytes, filename: str) -> list[dict]:
    """Header row: List Number, Circle, Division, Sub Division, Feeder Name, Feeder Code."""
    raw = _sheet_rows(data, filename)
    if not raw:
        raise HTTPException(422, "The file is empty")
    cols: dict[int, str] = {}
    for i, h in enumerate(raw[0]):
        key = _HEADERS.get(re.sub(r"[^a-z]", "", _cell(h).lower()))
        if key:
            cols[i] = key
    missing = [k for k in _REQUIRED if k not in cols.values()]
    if missing:
        raise HTTPException(422, "Missing column(s): " + ", ".join(m.replace("_", " ") for m in missing))
    rows, seen = [], {}
    for n, r in enumerate(raw[1:], start=2):
        rec = {k: _cell(r[i]) if i < len(r) else "" for i, k in cols.items()}
        if not any(rec.values()):
            continue  # blank line
        for k in _REQUIRED:
            if not rec.get(k):
                raise HTTPException(422, f"Row {n}: {k.replace('_', ' ')} is empty")
        rec.setdefault("feeder_name", "")
        rec.setdefault("feeder_code", "")
        if rec["list_number"] in seen:
            raise HTTPException(422, f"Row {n}: List Number {rec['list_number']} is already on row {seen[rec['list_number']]}")
        seen[rec["list_number"]] = n
        rows.append(rec)
    if not rows:
        raise HTTPException(422, "No data rows found")
    return rows


# -------------------------------------------------------- admin data view

# Column order + labels of the PESCO / LESCO form (JSONB does not keep key order).
PROJECT_COLUMNS = [
    ("circle", "Circle"), ("division", "Division"), ("subDivision", "Sub Division"),
    ("listNumber", "List Number"), ("referenceNumber", "Reference Number"), ("address", "Address"),
    ("feederName", "Feeder Name"), ("feederCode", "Feeder Code"), ("lsName", "LS Name"),
    ("lsNumber", "LS Number"), ("fsName", "FS Name"), ("electricianName", "Electrician Name"),
    ("tfRating", "TF Rating"), ("cableWidth", "Cable Width"), ("cableLength", "Cable Length (m)"),
    ("poleType", "Pole Type"), ("thimble", "Thimble (Qty)"), ("pg", "PG (Qty)"),
    ("armingBolt", "Arming Bolt (Qty)"), ("shrinkingSleeves", "Shrinking Sleeves (inches)"),
    ("telco", "Telco (Qty)"), ("backstrips", "Backstrips (Qty)"), ("earthing", "Earthing"),
    ("latitude", "Latitude"), ("longitude", "Longitude"),
]
_HIDDEN = {"photoPaths", "project", "id", "username", "createdAt"}


def data_columns(form_type: str, forms) -> list[tuple[str, str]]:
    """[(key, label)] for the fields of [forms]: known order first, anything else after."""
    present: list[str] = []
    for f in forms:
        for k in f.data:
            if k not in present and k not in _HIDDEN:
                present.append(k)
    cols: list[tuple[str, str]] = []
    if form_type in config.PROJECTS:
        known = dict(PROJECT_COLUMNS)
        cols = [(k, known[k]) for k, _ in PROJECT_COLUMNS if k in present]
        cols += [(k, k) for k in present if k not in known]
    else:
        cols = [(k, k) for k in present]
    return cols
