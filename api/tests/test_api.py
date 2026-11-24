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

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, purge_old  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Form  # noqa: E402


@pytest.fixture(scope="module")
def c():
    with TestClient(app) as client:
        yield client


def login(c, u, p):
    return c.post("/api/v1/auth/login", json={"username": u, "password": p})


def hdr(c, u, p):
    return {"Authorization": "Bearer " + login(c, u, p).json()["access_token"]}


def upload(c, h, form_id="100", list_number="50", form_type="apms", photos=2, bad_hash=False):
    blobs = [(f"{form_id}-{i}.webp", bytes([i]) * 100) for i in range(photos)]
    meta = {
        "form_type": form_type, "form_id": form_id, "list_number": list_number,
        "created_at": "2026-11-05T10:15:00.000", "data": {"listNumber": list_number, "circle": "Lahore"},
        "photos": [{"name": n, "sha256": ("0" * 64 if bad_hash else hashlib.sha256(b).hexdigest())} for n, b in blobs],
    }
    return c.post("/api/v1/forms", headers=h, data={"meta": json.dumps(meta)},
                  files=[("photos", (n, b, "image/webp")) for n, b in blobs])


def test_admin_bootstrap_and_login(c):
    r = login(c, "admin", "adminpass1")
    assert r.status_code == 200 and r.json()["role"] == "admin"
    assert login(c, "admin", "wrong").status_code == 401


def test_users_and_permissions(c):
    a = hdr(c, "admin", "adminpass1")
    assert c.post("/api/v1/admin/users", headers=a, json={"username": "ali", "password": "alipass123"}).status_code == 200
    assert c.post("/api/v1/admin/users", headers=a, json={"username": "ALI", "password": "alipass123"}).status_code == 409
    u = hdr(c, "ali", "alipass123")
    assert c.get("/api/v1/admin/users", headers=u).status_code == 403
    assert c.get("/api/v1/admin/users").status_code == 401


def test_refresh_and_disable(c):
    t = login(c, "ali", "alipass123").json()
    assert c.post("/api/v1/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 200
    assert c.post("/api/v1/auth/refresh", json={"refresh_token": t["access_token"]}).status_code == 401
    a = hdr(c, "admin", "adminpass1")
    c.post("/api/v1/admin/users", headers=a, json={"username": "temp", "password": "temppass12"})
    tt = login(c, "temp", "temppass12").json()
    c.post("/api/v1/admin/users/temp/active", headers=a, json={"active": False})
    assert c.post("/api/v1/auth/refresh", json={"refresh_token": tt["refresh_token"]}).status_code == 401
    assert login(c, "temp", "temppass12").status_code == 401


def test_upload_is_idempotent_and_stored(c):
    h = hdr(c, "ali", "alipass123")
    assert upload(c, h).status_code == 200
    assert upload(c, h).status_code == 200  # retry: same form again
    with SessionLocal() as db:
        assert db.query(Form).filter_by(form_id="100").count() == 1
    d = os.path.join(os.environ["DATA_DIR"], "photos", "apms", "50", "ali__100")
    assert sorted(os.listdir(d)) == ["100-0.webp", "100-1.webp"]


def test_upload_rejects_bad_checksum_and_anonymous(c):
    h = hdr(c, "ali", "alipass123")
    assert upload(c, h, form_id="101", bad_hash=True).status_code == 422
    assert upload(c, {}, form_id="102").status_code == 401
    assert upload(c, h, form_id="../x").status_code == 422
    with SessionLocal() as db:
        assert db.query(Form).filter(Form.form_id.in_(["101", "102"])).count() == 0


def test_summary_done_not_done_and_zip(c):
    a = hdr(c, "admin", "adminpass1")
    h = hdr(c, "ali", "alipass123")
    upload(c, h, form_id="200", list_number="51", form_type="single", photos=3)
    csvb = b"form_type,list_number\napms,50\napms,60\napms,70\nsingle,51\n"
    r = c.post("/api/v1/admin/expected", headers=a, files={"file": ("m.csv", csvb, "text/csv")})
    assert r.json()["added"] == 4
    s = c.get("/api/v1/admin/summary", headers=a).json()["types"]
    assert s["apms"]["expected"] == 3 and s["apms"]["expected_done"] == 1
    assert s["apms"]["not_done"] == ["60", "70"]
    assert s["single"]["not_done"] == []

    z = c.get("/api/v1/admin/photos.zip", headers=a, params={"list_number": "51"})
    assert z.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert len(names) == 3 and all(n.startswith("51/single-ali-200/") for n in names)
    assert c.get("/api/v1/admin/photos.zip", headers=a, params={"list_number": "999"}).status_code == 404
    assert c.get("/api/v1/admin/photos.zip", params={"list_number": "51"}).status_code == 401


def test_csv_export_and_forms_list(c):
    a = hdr(c, "admin", "adminpass1")
    r = c.get("/api/v1/admin/forms.csv", headers=a, params={"form_type": "apms"})
    assert r.status_code == 200 and "listNumber" in r.text and "ali" in r.text
    assert len(c.get("/api/v1/admin/forms", headers=a, params={"list_number": "50"}).json()) == 1


def test_purge_removes_old_forms_and_photos(c):
    from datetime import datetime, timedelta, timezone
    with SessionLocal() as db:
        f = db.query(Form).filter_by(form_id="100").first()
        f.received_at = datetime.now(timezone.utc) - timedelta(days=400)
        db.commit()
        assert purge_old(db) == 1
    assert not os.path.exists(os.path.join(os.environ["DATA_DIR"], "photos", "apms", "50", "ali__100"))


def test_admin_page_served(c):
    assert "Survey Admin" in c.get("/admin/").text


# ------------------------------------------------------------ PESCO / LESCO

def master_xlsx(rows, header=("List Number", "Circle", "Division", "Sub Division", "Feeder Name", "Feeder Code")):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(list(header))
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


MASTER = [
    (1001, "Peshawar Circle", "Peshawar Div 1", "Sub Div A", "Feeder One", "F-001"),
    ("1002", "Peshawar Circle", "Peshawar Div 1", "Sub Div B", "Feeder Two", "F-002"),
    (1003, "Mardan Circle", "Mardan Div", "Sub Div C", "", ""),
]
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def put_master(c, project="pesco", rows=MASTER, replace=False, **kw):
    admin = hdr(c, "admin", "adminpass1")
    return c.post(f"/api/v1/admin/projects/{project}/master?replace={str(replace).lower()}", headers=admin,
                  files={"file": ("master.xlsx", master_xlsx(rows, **kw), XLSX)})


def make_user(c, name, project=None):
    admin = hdr(c, "admin", "adminpass1")
    r = c.post("/api/v1/admin/users", headers=admin,
               json={"username": name, "password": "password1", "project": project})
    assert r.status_code == 200, r.text
    return hdr(c, name, "password1")


def project_upload(c, h, form_id, list_number="1001", reference="REF-1", form_type="pesco", photos=1):
    blobs = [(f"{form_id}-{i}.webp", bytes([i + 1]) * 50) for i in range(photos)]
    meta = {
        "form_type": form_type, "form_id": form_id, "list_number": list_number,
        "created_at": "2026-11-05T10:15:00.000",
        "data": {"listNumber": list_number, "referenceNumber": reference, "circle": "Peshawar Circle",
                 "tfRating": "50", "cableWidth": "120 mm", "poleType": "CONCRETE", "address": "St 1"},
        "photos": [{"name": n, "sha256": hashlib.sha256(b).hexdigest()} for n, b in blobs],
    }
    return c.post("/api/v1/forms", headers=h, data={"meta": json.dumps(meta)},
                  files=[("photos", (n, b, "image/webp")) for n, b in blobs])


def test_master_import_and_summary(c):
    r = put_master(c)
    assert r.status_code == 200, r.text
    assert r.json()["added"] == 3 and r.json()["updated"] == 0
    again = put_master(c).json()  # Excel numbers (1001.0) must not create new rows
    assert again["added"] == 0 and again["updated"] == 0
    s = c.get("/api/v1/admin/projects/pesco/master", headers=hdr(c, "admin", "adminpass1")).json()
    assert s == {"list_numbers": 3, "circles": 2, "divisions": 2, "sub_divisions": 3}


def test_master_import_rejects_bad_files(c):
    admin = hdr(c, "admin", "adminpass1")
    r = put_master(c, header=("List Number", "Circle", "Division"))
    assert r.status_code == 422 and "sub division" in r.json()["detail"].lower()
    r = put_master(c, rows=[("", "C", "D", "S", "", "")])
    assert r.status_code == 422 and "Row 2" in r.json()["detail"]
    r = put_master(c, rows=[(5, "C", "D", "S", "", ""), (5, "C", "D", "S", "", "")])
    assert r.status_code == 422 and "already" in r.json()["detail"]
    r = c.post("/api/v1/admin/projects/pesco/master", headers=admin, files={"file": ("x.txt", b"hi", "text/plain")})
    assert r.status_code == 422
    assert c.post("/api/v1/admin/projects/gepco/master", headers=admin,
                  files={"file": ("m.csv", b"a", "text/csv")}).status_code == 404


def test_project_access_is_limited_per_user(c):
    put_master(c)
    p = make_user(c, "pesco_user", "pesco")
    l = make_user(c, "lesco_user", "lesco")
    n = make_user(c, "plain_user", None)
    admin = hdr(c, "admin", "adminpass1")
    assert c.get("/api/v1/projects/pesco/master", headers=p).status_code == 200
    assert c.get("/api/v1/projects/pesco/master", headers=l).status_code == 403
    assert c.get("/api/v1/projects/pesco/master", headers=n).status_code == 403
    assert c.get("/api/v1/projects/pesco/master", headers=admin).status_code == 200
    assert c.get("/api/v1/projects/nope/master", headers=p).status_code == 404
    assert c.get("/api/v1/projects/pesco/master").status_code == 401
    assert login(c, "pesco_user", "password1").json()["project"] == "pesco"
    assert project_upload(c, l, "x1").status_code == 403
    assert project_upload(c, n, "x2").status_code == 403
    assert upload(c, n, form_id="g1").status_code == 200  # general forms are open to everyone
    assert project_upload(c, p, "x3").status_code == 200


def test_surveyors_cannot_use_admin_endpoints(c):
    p = make_user(c, "pesco_user2", "pesco")
    for path in ("/api/v1/admin/forms", "/api/v1/admin/forms/table?form_type=pesco",
                 "/api/v1/admin/photos.zip?list_number=1", "/api/v1/admin/photos_bulk.zip?form_type=pesco",
                 "/api/v1/admin/projects/pesco/options", "/api/v1/admin/forms.csv?form_type=pesco"):
        assert c.get(path, headers=p).status_code == 403, path


def test_duplicate_surveys_are_kept_and_counted(c):
    put_master(c)
    h = make_user(c, "dup_a", "pesco")
    h2 = make_user(c, "dup_b", "pesco")
    assert project_upload(c, h, "d1", "1002", "R-A").status_code == 200
    assert project_upload(c, h2, "d1", "1002", "R-B").status_code == 200  # same list number, other user
    assert project_upload(c, h, "d2", "1002", "R-C").status_code == 200  # same user, new entry
    assert project_upload(c, h, "d2", "1002", "R-C").status_code == 200  # a re-send is not a duplicate
    items = {i["list_number"]: i for i in c.get("/api/v1/projects/pesco/master", headers=h).json()["items"]}
    assert items["1002"]["surveyed"] == 3
    assert items["1003"]["surveyed"] == 0
    assert items["1001"]["feeder_name"] == "Feeder One" and items["1001"]["sub_division"] == "Sub Div A"


def test_options_defaults_and_admin_edit(c):
    h = make_user(c, "opt_user", "lesco")
    admin = hdr(c, "admin", "adminpass1")
    o = c.get("/api/v1/projects/lesco/master", headers=h).json()["options"]
    assert o["poleType"] == ["CONCRETE", "STEEL"] and o["earthing"] == ["Yes", "No"]
    assert {"value": "50", "cable_width": "120 mm"} in o["tfRating"]
    new = {"poleType": ["CONCRETE", "STEEL", "WOOD"],
           "tfRating": [{"value": "10", "cable_width": "70 mm"}, {"value": "300", "cable_width": "400 mm"}]}
    assert c.put("/api/v1/admin/projects/lesco/options", headers=admin, json=new).status_code == 200
    o = c.get("/api/v1/projects/lesco/master", headers=h).json()["options"]
    assert o["poleType"] == ["CONCRETE", "STEEL", "WOOD"]
    assert o["tfRating"][-1] == {"value": "300", "cable_width": "400 mm"}
    assert o["earthing"] == ["Yes", "No"]  # lists not sent stay unchanged
    # PESCO has its own lists
    assert "WOOD" not in c.get("/api/v1/admin/projects/pesco/options", headers=admin).json()["poleType"]
    for bad in ({"poleType": []}, {"poleType": ["A", "a"]}, {"tfRating": [{"value": "x", "cable_width": "1"}]},
                {"tfRating": [{"value": "5", "cable_width": ""}]}, {"colour": ["red"]}):
        assert c.put("/api/v1/admin/projects/lesco/options", headers=admin, json=bad).status_code == 422, bad


def test_user_project_can_be_changed_by_admin(c):
    admin = hdr(c, "admin", "adminpass1")
    make_user(c, "move_me", "pesco")
    r = c.post("/api/v1/admin/users/move_me/project", headers=admin, json={"project": "lesco"})
    assert r.json()["project"] == "lesco"
    assert login(c, "move_me", "password1").json()["project"] == "lesco"
    assert c.post("/api/v1/admin/users/move_me/project", headers=admin, json={"project": "gepco"}).status_code == 422
    assert c.post("/api/v1/admin/users/move_me/project", headers=admin, json={"project": None}).json()["project"] is None
    users = {u["username"]: u for u in c.get("/api/v1/admin/users", headers=admin).json()}
    assert users["move_me"]["project"] is None


def test_admin_table_has_one_column_per_field(c):
    admin = hdr(c, "admin", "adminpass1")
    h = make_user(c, "tbl_user", "pesco")
    project_upload(c, h, "t1", "1001", "REF-TBL")
    t = c.get("/api/v1/admin/forms/table?form_type=pesco&reference_number=REF-TBL", headers=admin).json()
    labels = [col["label"] for col in t["columns"]]
    assert labels[:3] == ["User", "Received", "Photos"]
    assert labels.index("Circle") < labels.index("List Number") < labels.index("Reference Number") < labels.index("TF Rating")
    assert len(t["rows"]) == 1 and t["rows"][0]["referenceNumber"] == "REF-TBL" and t["rows"][0]["cableWidth"] == "120 mm"
    csv_text = c.get("/api/v1/admin/forms.csv?form_type=pesco", headers=admin).content.decode("utf-8-sig")
    assert csv_text.splitlines()[0].startswith("username,form_id,received_at,photos,master_list,Circle,List Number,Reference Number")


def test_photos_by_list_and_reference_number(c):
    admin = hdr(c, "admin", "adminpass1")
    h = make_user(c, "ph_user", "pesco")
    project_upload(c, h, "p1", "1001", "REF-X", photos=2)
    project_upload(c, h, "p2", "1001", "REF-Y", photos=1)  # same List Number, other Reference Number

    def names(resp):
        assert resp.status_code == 200, resp.text
        return sorted(zipfile.ZipFile(io.BytesIO(resp.content)).namelist())

    by_ref = names(c.get("/api/v1/admin/photos.zip?reference_number=REF-X", headers=admin))
    assert len(by_ref) == 2 and all(n.startswith("REF-X/pesco-ph_user-p1/") for n in by_ref)
    by_list = names(c.get("/api/v1/admin/photos.zip?list_number=1001&form_type=pesco", headers=admin))
    assert {n.split("/")[0] for n in by_list} == {"1001"} and any("-p2/" in n for n in by_list)
    assert c.get("/api/v1/admin/photos.zip", headers=admin).status_code == 422

    bulk_ref = names(c.get("/api/v1/admin/photos_bulk.zip?form_type=pesco&by=reference", headers=admin))
    assert {"REF-X", "REF-Y"} <= {n.split("/")[0] for n in bulk_ref}
    bulk_list = names(c.get("/api/v1/admin/photos_bulk.zip?form_type=pesco&by=list", headers=admin))
    assert "1001" in {n.split("/")[0] for n in bulk_list}
    assert c.get("/api/v1/admin/photos_bulk.zip?form_type=pesco&by=zzz", headers=admin).status_code == 422


def test_summary_uses_master_list_for_projects(c):
    put_master(c)
    admin = hdr(c, "admin", "adminpass1")
    s = c.get("/api/v1/admin/summary", headers=admin).json()["types"]["pesco"]
    assert s["expected"] == 3 and "1003" in s["not_done"]


def test_master_replace_removes_missing_rows(c):
    put_master(c, project="lesco")
    r = put_master(c, project="lesco", rows=[MASTER[0]], replace=True).json()
    assert r["removed"] == 2 and r["total_rows"] == 1
    s = c.get("/api/v1/admin/projects/lesco/master", headers=hdr(c, "admin", "adminpass1")).json()
    assert s["list_numbers"] == 1


def test_list_number_not_in_master_is_saved_and_flagged(c):
    put_master(c)
    admin = hdr(c, "admin", "adminpass1")
    h = make_user(c, "flag_user", "pesco")
    assert project_upload(c, h, "f1", "1001", "REF-OK").status_code == 200
    assert project_upload(c, h, "f2", "9999", "REF-BAD").status_code == 200  # saved, never refused
    t = c.get("/api/v1/admin/forms/table?form_type=pesco&username=flag_user", headers=admin).json()
    by_ref = {r["referenceNumber"]: r for r in t["rows"]}
    assert by_ref["REF-OK"]["master_check"] == ""
    assert by_ref["REF-BAD"]["master_check"] == "List Number not in master list"
    assert any(col["key"] == "master_check" for col in t["columns"])
    listed = {f["form_id"]: f["in_master"] for f in c.get("/api/v1/admin/forms?username=flag_user", headers=admin).json()}
    assert listed == {"f1": True, "f2": False}
    s = c.get("/api/v1/admin/summary", headers=admin).json()["types"]["pesco"]
    assert "9999" in s["not_in_master_list"] and "1001" not in s["not_in_master_list"]
    # fixing the sheet clears the warning
    put_master(c, rows=MASTER + [(9999, "Mardan Circle", "Mardan Div", "Sub Div C", "", "")])
    t = c.get("/api/v1/admin/forms/table?form_type=pesco&username=flag_user", headers=admin).json()
    assert all(r["master_check"] == "" for r in t["rows"])
    csv_text = c.get("/api/v1/admin/forms.csv?form_type=pesco", headers=admin).content.decode("utf-8-sig")
    assert "master_list" in csv_text.splitlines()[0]


# ------------------------------------------------- daily permission (2-hour rule)

from datetime import datetime, timedelta, timezone  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from app import config as cfg  # noqa: E402
from app import permission  # noqa: E402
from app.models import User  # noqa: E402

# "Now" is pinned to 12:00 local time so the tests never cross midnight by accident.
NOON = datetime.now(ZoneInfo(cfg.TIMEZONE)).replace(hour=12, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


@pytest.fixture()
def noon(monkeypatch):
    monkeypatch.setattr(permission, "now", lambda: NOON)
    return NOON


def set_permission(username, granted_ago_hours=None, opened=False):
    """Pretend the admin gave permission N hours before NOON (None = never)."""
    with SessionLocal() as db:
        u = db.query(User).filter(User.username == username).first()
        u.permission_granted_at = None if granted_ago_hours is None else NOON - timedelta(hours=granted_ago_hours)
        u.permission_opened_at = (NOON - timedelta(hours=granted_ago_hours or 0)) if opened else None
        db.commit()


def perm_of(c, username):
    admin = hdr(c, "admin", "adminpass1")
    return {u["username"]: u["permission"] for u in c.get("/api/v1/admin/users", headers=admin).json()}[username]


def test_new_user_has_permission_and_opening_the_app_counts(c, noon):
    h = make_user(c, "perm_new", None)
    assert perm_of(c, "perm_new")["state"] == "ok"  # make_user logged in = opened the app
    me = c.get("/api/v1/me", headers=h).json()
    assert me["permission"]["state"] == "ok" and me["username"] == "perm_new"


def test_not_opening_within_two_hours_locks_until_admin_gives_permission(c, noon):
    admin = hdr(c, "admin", "adminpass1")
    h = make_user(c, "perm_lock", None)
    set_permission("perm_lock", granted_ago_hours=1.5)  # given 1.5 h ago, never opened
    assert perm_of(c, "perm_lock")["state"] == "waiting"
    set_permission("perm_lock", granted_ago_hours=2.5)  # 2.5 h ago, never opened
    state = perm_of(c, "perm_lock")
    assert state["state"] == "locked" and state["opened_at"] is None
    r = login(c, "perm_lock", "password1")
    assert r.status_code == 423 and "locked" in r.json()["detail"].lower()
    assert c.get("/api/v1/me", headers=h).status_code == 423         # the old token is refused too
    assert upload(c, h, form_id="pl1").status_code == 423
    # the admin gives permission again -> works again
    r = c.post("/api/v1/admin/users/perm_lock/permission", headers=admin)
    assert r.status_code == 200 and r.json()["permission"]["state"] == "waiting"
    assert login(c, "perm_lock", "password1").status_code == 200
    assert perm_of(c, "perm_lock")["state"] == "ok"
    assert upload(c, h, form_id="pl2").status_code == 200


def test_locked_user_cannot_refresh_the_token(c, noon):
    r = login(c, "perm_new", "password1")
    refresh = r.json()["refresh_token"]
    set_permission("perm_new", granted_ago_hours=3)
    assert c.post("/api/v1/auth/refresh", json={"refresh_token": refresh}).status_code == 423


def test_opened_in_time_stays_allowed_for_the_rest_of_the_day(c, noon):
    make_user(c, "perm_open", None)
    set_permission("perm_open", granted_ago_hours=5, opened=True)  # opened right away, 5 h ago
    assert perm_of(c, "perm_open")["state"] == "ok"  # long after the 2 hours, same day: fine


def test_permission_ends_with_the_day(c, noon):
    make_user(c, "perm_old", None)
    set_permission("perm_old", granted_ago_hours=26, opened=True)  # yesterday
    assert perm_of(c, "perm_old")["state"] == "expired"
    r = login(c, "perm_old", "password1")
    assert r.status_code == 423 and "earlier day" in r.json()["detail"]


def test_user_without_any_permission_is_refused(c, noon):
    make_user(c, "perm_none", None)
    set_permission("perm_none", None)
    assert perm_of(c, "perm_none")["state"] == "none"
    assert login(c, "perm_none", "password1").status_code == 423


def test_give_permission_to_all_and_admins_are_exempt(c, noon):
    admin = hdr(c, "admin", "adminpass1")
    make_user(c, "perm_a", None)
    make_user(c, "perm_b", "pesco")
    set_permission("perm_a", granted_ago_hours=30)
    set_permission("perm_b", granted_ago_hours=3)
    r = c.post("/api/v1/admin/permissions/grant_all", headers=admin)
    assert r.status_code == 200 and r.json()["users"] >= 2
    assert perm_of(c, "perm_a")["state"] == "waiting" and perm_of(c, "perm_b")["state"] == "waiting"
    assert perm_of(c, "admin")["state"] == "not_required"
    assert login(c, "admin", "adminpass1").status_code == 200  # admin is never locked out


def test_permission_can_be_switched_off(c, noon, monkeypatch):
    make_user(c, "perm_off", None)
    set_permission("perm_off", None)
    monkeypatch.setattr(cfg, "PERMISSION_REQUIRED", False)
    assert login(c, "perm_off", "password1").status_code == 200
    assert perm_of(c, "perm_off")["state"] == "not_required"
