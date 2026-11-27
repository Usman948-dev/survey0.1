"""PESCO is keyed by TRF Code (no List Number). Uses the real sheet layout sent by the client."""
import hashlib
import io
import json
import os
import tempfile
import zipfile

import pytest

_tmp = tempfile.mkdtemp()
os.environ.update(
    DATABASE_URL=f"sqlite:///{_tmp}/t.db", DATA_DIR=_tmp + "/data",
    SECRET_KEY="x" * 40, ADMIN_USER="admin", ADMIN_PASSWORD="adminpass1",
)
os.environ.pop("TRF_PROJECTS", None)  # default: pesco

from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import Workbook  # noqa: E402

from app.main import app  # noqa: E402

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
HEAD = ["List Number", "Sub Division", "Feeder", "Location", "TRF Code", "Capacity(KVA)", "Reference No."]
ROWS = [
    [1, 26126, 87614, "FOUJI GUJAR ABAD PESHAWAR (L2600024892)", "L2600024892", 100, 14261269000160],
    [2, 26126, 87614, "M.IQBAL MARKET (L2600024915)", "L2600024915", 100, 7261269000250],
    [3, 26127, 87615, "SHAH SAWOOD GENERAL STORE (L2600024882)", "L2600024882", 200, 14261269000130],
]


@pytest.fixture(scope="module")
def c():
    with TestClient(app) as client:
        yield client


def hdr(c, u, p):
    return {"Authorization": "Bearer " + c.post("/api/v1/auth/login", json={"username": u, "password": p}).json()["access_token"]}


def sheet(header=HEAD, rows=ROWS):
    wb = Workbook(); ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()


def put(c, data, replace=False, circle="Peshawar", division="Peshawar Div", project="pesco"):
    return c.post(f"/api/v1/admin/projects/{project}/master?replace={str(replace).lower()}&circle={circle}&division={division}",
                  headers=hdr(c, "admin", "adminpass1"), files={"file": ("m.xlsx", data, XLSX)})


def user(c, name, project):
    c.post("/api/v1/admin/users", headers=hdr(c, "admin", "adminpass1"),
           json={"username": name, "password": "password1", "project": project})
    return hdr(c, name, "password1")


def survey(c, h, form_id, trf, form_type="pesco", key="trf_code"):
    blob = b"\x01" * 40
    meta = {"form_type": form_type, "form_id": form_id, "created_at": "2026-11-05T10:15:00.000",
            "data": {"referenceNumber": "R1", "tfRating": "100"},
            "photos": [{"name": f"{form_id}.webp", "sha256": hashlib.sha256(blob).hexdigest()}]}
    if key:
        meta[key] = trf
    return c.post("/api/v1/forms", headers=h, data={"meta": json.dumps(meta)},
                  files=[("photos", (f"{form_id}.webp", blob, "image/webp"))])


def test_master_has_trf_codes_and_all_details_no_list_number(c):
    r = put(c, sheet())
    assert r.status_code == 200 and r.json()["added"] == 3, r.text
    h = user(c, "u1", "pesco")
    j = c.get("/api/v1/projects/pesco/master", headers=h).json()
    assert j["key"] == "trf_code" and len(j["items"]) == 3
    by = {i["trf_code"]: i for i in j["items"]}
    one = by["L2600024892"]
    assert "list_number" not in one
    assert one["sub_division"] == "26126" and one["feeder_code"] == "87614"
    assert one["location"].startswith("FOUJI") and one["capacity_kva"] == "100"
    assert one["reference_no"] == "14261269000160" and one["circle"] == "Peshawar" and one["division"] == "Peshawar Div"
    # every user sees every TRF code
    h2 = user(c, "u2", "pesco")
    assert len(c.get("/api/v1/projects/pesco/master", headers=h2).json()["items"]) == 3


def test_sheet_without_list_number_column_works_and_trf_is_required(c):
    head = [x for x in HEAD if x != "List Number"]
    rows = [r[1:] for r in ROWS]
    assert put(c, sheet(head, rows)).status_code == 200
    assert put(c, sheet([x for x in head if x != "TRF Code"], [r[:3] + r[4:] for r in rows])).status_code == 422
    bad = [list(r) for r in ROWS]; bad[1][4] = ""
    r = put(c, sheet(HEAD, bad)); assert r.status_code == 422 and "Row 3" in r.json()["detail"] and "trf code" in r.json()["detail"].lower()
    dup = [list(r) for r in ROWS]; dup[2][4] = dup[0][4]
    r = put(c, sheet(HEAD, dup)); assert r.status_code == 422 and "TRF Code" in r.json()["detail"]
    assert put(c, sheet(), replace=True).status_code == 200  # restore


def test_survey_by_trf_code_counts_folders_and_progress(c):
    put(c, sheet(), replace=True)
    h, h2 = user(c, "s1", "pesco"), user(c, "s2", "pesco")
    assert survey(c, h, "f1", "L2600024892").status_code == 200
    assert survey(c, h2, "f1", "L2600024892").status_code == 200  # another user, same TRF: kept
    assert survey(c, h, "f2", "L2600024915").status_code == 200
    assert survey(c, h, "f3", "L2600024892", key=None).status_code == 422  # TRF code missing
    items = {i["trf_code"]: i for i in c.get("/api/v1/projects/pesco/master", headers=h).json()["items"]}
    assert items["L2600024892"]["surveyed"] == 2 and items["L2600024915"]["surveyed"] == 1
    assert items["L2600024882"]["surveyed"] == 0
    a = hdr(c, "admin", "adminpass1")
    s = c.get("/api/v1/admin/summary", headers=a).json()["types"]["pesco"]
    assert s["not_done"] == ["L2600024882"] and s["expected"] == 3 and s["expected_done"] == 2
    assert s["not_in_master_list"] == []
    t = c.get("/api/v1/admin/forms/table?form_type=pesco", headers=a).json()
    assert "TRF Code" in [x["label"] for x in t["columns"]] and "List Number" not in [x["label"] for x in t["columns"]]
    assert all(r["trfCode"] for r in t["rows"])
    z = c.get("/api/v1/admin/photos.zip?list_number=L2600024892&form_type=pesco", headers=a)
    assert z.status_code == 200
    assert {n.split("/")[0] for n in zipfile.ZipFile(io.BytesIO(z.content)).namelist()} == {"L2600024892"}
    bulk = c.get("/api/v1/admin/photos_bulk.zip?form_type=pesco&by=list", headers=a)
    assert {n.split("/")[0] for n in zipfile.ZipFile(io.BytesIO(bulk.content)).namelist()} == {"L2600024892", "L2600024915"}


def test_trf_in_data_or_old_list_number_field_also_accepted(c):
    h = user(c, "s3", "pesco")
    assert survey(c, h, "g1", "L2600024882", key="list_number").status_code == 200


def test_lesco_still_uses_list_numbers(c):
    r = put(c, sheet(HEAD, ROWS), project="lesco")
    assert r.status_code == 200
    h = user(c, "l1", "lesco")
    j = c.get("/api/v1/projects/lesco/master", headers=h).json()
    assert j["key"] == "list_number" and {i["list_number"] for i in j["items"]} == {"1", "2", "3"}
    assert survey(c, h, "lf1", "2", form_type="lesco", key="list_number").status_code == 200
