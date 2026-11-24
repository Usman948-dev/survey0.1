import asyncio
import csv
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form as FormField, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, inspect, text
from sqlalchemy.orm import Session

from . import config, permission, projects, storage
from .db import Base, SessionLocal, engine, get_db
from .models import Expected, Form, MasterRow, User
from .security import (
    admin_user, current_user, decode_token, hash_password, make_token, project_user,
    throttle_check, throttle_clear, throttle_fail, verify_password,
)

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def bootstrap():
    if not config.SECRET_KEY or len(config.SECRET_KEY) < 32:
        raise RuntimeError("SECRET_KEY must be set (at least 32 characters)")
    Base.metadata.create_all(engine)
    # create_all never alters an existing table: add the users.project column to older databases
    have = {c["name"] for c in inspect(engine).get_columns("users")}
    for col, ddl in (("project", "VARCHAR(10)"),
                     ("permission_granted_at", "TIMESTAMP WITH TIME ZONE"),
                     ("permission_opened_at", "TIMESTAMP WITH TIME ZONE")):
        if col not in have:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {ddl}"))
    os.makedirs(storage.photos_root(), exist_ok=True)
    with SessionLocal() as db:
        projects.seed_options(db)
        if not db.query(User).filter(User.role == "admin").first():
            if not config.ADMIN_PASSWORD:
                raise RuntimeError("ADMIN_PASSWORD must be set for the first start")
            db.add(User(username=config.ADMIN_USER, password_hash=hash_password(config.ADMIN_PASSWORD), role="admin"))
            db.commit()


def purge_old(db: Session) -> int:
    """Delete forms (and their photos) received more than RETENTION_DAYS ago."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.RETENTION_DAYS)
    old = db.query(Form).filter(Form.received_at < cutoff).all()
    for f in old:
        shutil.rmtree(storage.form_dir(f.form_type, f.list_number, f.username, f.form_id), ignore_errors=True)
        db.delete(f)
    db.commit()
    return len(old)


async def _purge_loop():
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            with SessionLocal() as db:
                purge_old(db)
        except Exception as e:  # keep the server alive
            print("purge failed:", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap()
    task = asyncio.create_task(_purge_loop()) if config.PURGE_ENABLED else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Survey server", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/api/v1/health")
def health():
    return {"status": "ok"}


# ------------------------------------------------------------------ auth

class LoginIn(BaseModel):
    username: str
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


def _tokens(user: User, refresh: str | None = None):
    return {
        "username": user.username,
        "role": user.role,
        "project": user.project,
        "access_token": make_token(user.username, "access"),
        "refresh_token": refresh or make_token(user.username, "refresh"),
    }


@app.post("/api/v1/auth/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    key = f"{body.username.lower()}|{request.client.host if request.client else ''}"
    throttle_check(key)
    user = db.query(User).filter(func.lower(User.username) == body.username.strip().lower()).first()
    if not user or not user.active or not verify_password(body.password, user.password_hash):
        throttle_fail(key)
        raise HTTPException(401, "Wrong username or password")
    throttle_clear(key)
    permission.require(db, user)  # 423 when locked / no permission; a login counts as opening the app
    return _tokens(user)


@app.post("/api/v1/auth/refresh")
def refresh(body: RefreshIn, db: Session = Depends(get_db)):
    username = decode_token(body.refresh_token, "refresh")
    user = db.query(User).filter(User.username == username).first()
    if not user or not user.active:
        raise HTTPException(401, "Account disabled")
    permission.require(db, user)
    return _tokens(user, refresh=body.refresh_token)


@app.get("/api/v1/me")
def me(user: User = Depends(current_user)):
    """The app calls this when it opens: it counts as "opening the app" for the daily permission
    and tells the phone where the account stands (a locked account gets 423 instead)."""
    return {"username": user.username, "role": user.role, "project": user.project,
            "permission": permission.view(user)}


# ----------------------------------------------------------- form upload

@app.post("/api/v1/forms")
async def upload_form(
    meta: str = FormField(...),
    photos: list[UploadFile] = File(default=[]),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    try:
        m = json.loads(meta)
        form_type = m["form_type"]
        form_id = str(m["form_id"])
        list_number = str(m.get("list_number", "")).strip()
        created_at = datetime.fromisoformat(str(m["created_at"]))
        data = m["data"]
        declared = {p["name"]: p["sha256"] for p in m.get("photos", [])}
    except (ValueError, KeyError, TypeError):
        raise HTTPException(422, "Bad meta")
    if form_type not in config.FORM_TYPES or not ID_RE.match(form_id) or not isinstance(data, dict):
        raise HTTPException(422, "Bad meta")
    if user.role != "admin" and form_type in config.PROJECTS and user.project != form_type:
        raise HTTPException(403, "No access to this project")
    if created_at.tzinfo is None:
        created_at = created_at.astimezone()  # phone local time
    if len(photos) > config.MAX_PHOTOS_PER_FORM:
        raise HTTPException(413, "Too many photos")

    files: dict[str, bytes] = {}
    for up in photos:
        name = os.path.basename(up.filename or "")
        if not storage.PHOTO_NAME.match(name):
            raise HTTPException(422, f"Bad photo name: {name}")
        blob = await up.read(config.MAX_PHOTO_BYTES + 1)
        if len(blob) > config.MAX_PHOTO_BYTES:
            raise HTTPException(413, f"Photo too large: {name}")
        want = declared.get(name)
        if want is None or hashlib.sha256(blob).hexdigest() != want.lower():
            raise HTTPException(422, f"Photo checksum mismatch: {name}")
        files[name] = blob
    if set(declared) != set(files):
        raise HTTPException(422, "Photos listed in meta do not match the files sent")

    # files first (atomic folder swap), then the database row; 200 only when both are safe
    storage.replace_dir(storage.form_dir(form_type, list_number, user.username, form_id), files)
    photo_meta = [{"name": n, "sha256": declared[n], "size": len(b)} for n, b in files.items()]
    row = db.query(Form).filter(Form.username == user.username, Form.form_id == form_id).first()
    if row:
        row.form_type, row.list_number, row.created_at = form_type, list_number, created_at
        row.data, row.photos = data, photo_meta
        row.received_at = datetime.now(timezone.utc)
    else:
        db.add(Form(username=user.username, form_id=form_id, form_type=form_type,
                    list_number=list_number, created_at=created_at, data=data, photos=photo_meta))
    db.commit()
    return {"status": "ok", "form_id": form_id, "photos": len(files)}


# ------------------------------------------------- project data (surveyors)

def _natural(s: str):
    return (len(s), s)


@app.get("/api/v1/projects/{project}/master")
def project_master(project: str, _: User = Depends(project_user), db: Session = Depends(get_db)):
    """Everything the phone needs for the PESCO / LESCO form, in one download:
    every List Number with its circle / division / sub division / feeder, how many
    surveys each already has, and the dropdown options."""
    surveyed = dict(
        db.query(Form.list_number, func.count(Form.id)).filter(Form.form_type == project)
        .group_by(Form.list_number).all()
    )
    rows = db.query(MasterRow).filter(MasterRow.project == project).all()
    rows.sort(key=lambda r: _natural(r.list_number))
    return {
        "project": project,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": [
            {"list_number": r.list_number, "circle": r.circle, "division": r.division,
             "sub_division": r.sub_division, "feeder_name": r.feeder_name, "feeder_code": r.feeder_code,
             "surveyed": surveyed.get(r.list_number, 0)}
            for r in rows
        ],
        "options": projects.read_options(db, project),
    }


# ----------------------------------------------------------------- admin

class UserIn(BaseModel):
    username: str
    password: str
    role: str = "user"
    project: str | None = None


class ProjectIn(BaseModel):
    project: str | None = None


def _check_project(value: str | None) -> str | None:
    value = (value or "").strip().lower() or None
    if value is not None and value not in config.PROJECTS:
        raise HTTPException(422, "Project must be " + " or ".join(config.PROJECTS) + " (or empty)")
    return value


class PasswordIn(BaseModel):
    password: str


class ActiveIn(BaseModel):
    active: bool


@app.get("/api/v1/admin/users")
def list_users(_: User = Depends(admin_user), db: Session = Depends(get_db)):
    counts = dict(db.query(Form.username, func.count(Form.id)).group_by(Form.username).all())
    return [
        {"username": u.username, "role": u.role, "active": u.active, "project": u.project,
         "forms": counts.get(u.username, 0), "permission": permission.view(u)}
        for u in db.query(User).order_by(User.username)
    ]


@app.post("/api/v1/admin/users")
def create_user(body: UserIn, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    name = body.username.strip()
    if not re.match(r"^[A-Za-z0-9_.-]{2,40}$", name):
        raise HTTPException(422, "Username: 2-40 letters, digits, . _ -")
    if len(body.password) < 8:
        raise HTTPException(422, "Password must be at least 8 characters")
    if body.role not in ("user", "admin"):
        raise HTTPException(422, "Bad role")
    if db.query(User).filter(func.lower(User.username) == name.lower()).first():
        raise HTTPException(409, "Username already exists")
    project = _check_project(body.project)
    u = User(username=name, password_hash=hash_password(body.password), role=body.role, project=project)
    db.add(u)
    db.commit()
    permission.grant(db, u)  # a new user starts with permission for today
    return {"status": "ok"}


@app.post("/api/v1/admin/users/{username}/password")
def set_password(username: str, body: PasswordIn, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    u = db.query(User).filter(User.username == username).first()
    if not u:
        raise HTTPException(404, "No such user")
    if len(body.password) < 8:
        raise HTTPException(422, "Password must be at least 8 characters")
    u.password_hash = hash_password(body.password)
    db.commit()
    return {"status": "ok"}


@app.post("/api/v1/admin/users/{username}/permission")
def give_permission(username: str, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    """Gives this user permission for today (also unlocks a locked account)."""
    u = db.query(User).filter(User.username == username).first()
    if not u:
        raise HTTPException(404, "No such user")
    permission.grant(db, u)
    return {"status": "ok", "permission": permission.view(u)}


@app.post("/api/v1/admin/permissions/grant_all")
def give_permission_to_all(_: User = Depends(admin_user), db: Session = Depends(get_db)):
    """The daily button: permission for today for every active surveyor."""
    users = db.query(User).filter(User.role != "admin", User.active.is_(True)).all()
    for u in users:
        u.permission_granted_at, u.permission_opened_at = permission.now(), None
    db.commit()
    return {"status": "ok", "users": len(users)}


@app.post("/api/v1/admin/users/{username}/project")
def set_project(username: str, body: ProjectIn, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    u = db.query(User).filter(User.username == username).first()
    if not u:
        raise HTTPException(404, "No such user")
    u.project = _check_project(body.project)
    db.commit()
    return {"status": "ok", "project": u.project}


@app.post("/api/v1/admin/users/{username}/active")
def set_active(username: str, body: ActiveIn, me: User = Depends(admin_user), db: Session = Depends(get_db)):
    u = db.query(User).filter(User.username == username).first()
    if not u:
        raise HTTPException(404, "No such user")
    if u.username == me.username and not body.active:
        raise HTTPException(422, "You cannot disable yourself")
    u.active = body.active
    db.commit()
    return {"status": "ok"}


@app.post("/api/v1/admin/expected")
async def upload_expected(file: UploadFile = File(...), _: User = Depends(admin_user), db: Session = Depends(get_db)):
    """CSV with header `form_type,list_number` (apms / single / three). Adds missing rows.
    PESCO / LESCO expected lists come from the project master sheet instead."""
    text = (await file.read()).decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    added = 0
    for r in rows:
        t = (r.get("form_type") or "").strip().lower()
        ln = (r.get("list_number") or "").strip()
        if t not in config.GENERAL_TYPES or not ln:
            raise HTTPException(422, f"Bad row: {r} (PESCO / LESCO use the project master upload)")
        if not db.query(Expected).filter_by(form_type=t, list_number=ln).first():
            db.add(Expected(form_type=t, list_number=ln))
            added += 1
    db.commit()
    return {"status": "ok", "added": added, "total_rows": len(rows)}


@app.get("/api/v1/admin/summary")
def summary(_: User = Depends(admin_user), db: Session = Depends(get_db)):
    out = {}
    for t in config.FORM_TYPES:
        done_lists = {x for (x,) in db.query(Form.list_number).filter(Form.form_type == t).distinct()}
        if t in config.PROJECTS:
            expected = {x for (x,) in db.query(MasterRow.list_number).filter(MasterRow.project == t)}
        else:
            expected = {x for (x,) in db.query(Expected.list_number).filter(Expected.form_type == t)}
        out[t] = {
            "forms_received": db.query(func.count(Form.id)).filter(Form.form_type == t).scalar(),
            "list_numbers_done": len(done_lists),
            "expected": len(expected),
            "expected_done": len(expected & done_lists),
            "not_done": sorted(expected - done_lists, key=lambda s: (len(s), s)),
            # surveys whose List Number is not in the list (PESCO / LESCO: the master sheet)
            "not_in_master_list": sorted(done_lists - expected, key=lambda s: (len(s), s))
            if (expected or t in config.PROJECTS) else [],
        }
    since = datetime.now(timezone.utc) - timedelta(days=1)
    per_user = db.query(Form.username, func.count(Form.id)).filter(Form.received_at >= since).group_by(Form.username).all()
    return {"types": out, "last_24h_by_user": {u: n for u, n in per_user}}


def _filtered_forms(db: Session, form_type=None, list_number=None, reference_number=None, username=None):
    q = db.query(Form)
    if list_number:
        q = q.filter(Form.list_number == list_number)
    if form_type:
        q = q.filter(Form.form_type == form_type)
    if username:
        q = q.filter(Form.username == username)
    forms = q.order_by(Form.received_at.desc()).all()
    if reference_number:  # stored inside the form data, so filtered here
        forms = [f for f in forms if str(f.data.get("referenceNumber", "")).strip() == reference_number.strip()]
    return forms


def _master_check(db: Session, forms) -> dict[int, bool]:
    """form.id -> True when a PESCO / LESCO form's List Number is in that project's master sheet.
    Computed when asked (not stored), so it is right again as soon as the sheet is fixed.
    Other form types have no master sheet and are not in the result."""
    known = {
        p: {x for (x,) in db.query(MasterRow.list_number).filter(MasterRow.project == p)}
        for p in {f.form_type for f in forms} & set(config.PROJECTS)
    }
    return {f.id: f.list_number in known[f.form_type] for f in forms if f.form_type in known}


def _reference(f: Form) -> str:
    return str(f.data.get("referenceNumber", "") or "").strip()


@app.get("/api/v1/admin/forms")
def list_forms(list_number: str | None = None, form_type: str | None = None, username: str | None = None,
               reference_number: str | None = None, limit: int = 200,
               _: User = Depends(admin_user), db: Session = Depends(get_db)):
    forms = _filtered_forms(db, form_type, list_number, reference_number, username)[: min(limit, 1000)]
    check = _master_check(db, forms)
    return [
        {"username": f.username, "form_id": f.form_id, "form_type": f.form_type, "list_number": f.list_number,
         "reference_number": _reference(f), "in_master": check.get(f.id),
         "created_at": f.created_at.isoformat(), "received_at": f.received_at.isoformat(), "photos": len(f.photos or [])}
        for f in forms
    ]


@app.get("/api/v1/admin/forms/table")
def forms_table(form_type: str, list_number: str | None = None, reference_number: str | None = None,
                username: str | None = None, limit: int = 500,
                _: User = Depends(admin_user), db: Session = Depends(get_db)):
    """Every field of every form of one type as its own column (for the admin data view)."""
    if form_type not in config.FORM_TYPES:
        raise HTTPException(422, "Bad form_type")
    forms = _filtered_forms(db, form_type, list_number, reference_number, username)[: min(limit, 2000)]
    cols = projects.data_columns(form_type, forms)
    check = _master_check(db, forms)
    head = [{"key": "username", "label": "User"}, {"key": "received_at", "label": "Received"},
            {"key": "photos", "label": "Photos"}]
    if form_type in config.PROJECTS:
        head.append({"key": "master_check", "label": "Master list"})
    return {
        "columns": head + [{"key": k, "label": l} for k, l in cols],
        "rows": [
            {"username": f.username, "received_at": f.received_at.isoformat(), "photos": len(f.photos or []),
             "master_check": "" if check.get(f.id, True) else "List Number not in master list",
             **{k: f.data.get(k, "") for k, _l in cols}}
            for f in forms
        ],
    }


@app.get("/api/v1/admin/forms.csv")
def forms_csv(form_type: str, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    if form_type not in config.FORM_TYPES:
        raise HTTPException(422, "Bad form_type")
    forms = db.query(Form).filter(Form.form_type == form_type).order_by(Form.received_at).all()
    cols = projects.data_columns(form_type, forms)
    buf = io.StringIO()
    w = csv.writer(buf)
    check = _master_check(db, forms)
    flag = form_type in config.PROJECTS
    w.writerow(["username", "form_id", "received_at", "photos"] + (["master_list"] if flag else [])
               + [l for _k, l in cols])
    for f in forms:
        w.writerow([f.username, f.form_id, f.received_at.isoformat(), len(f.photos or [])]
                   + (["" if check.get(f.id, True) else "List Number not in master list"] if flag else [])
                   + [f.data.get(k, "") for k, _l in cols])
    data = ("\ufeff" + buf.getvalue()).encode("utf-8")
    return StreamingResponse(io.BytesIO(data), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{form_type}_forms.csv"'})


def _zip_photos(forms, folder_of, background: BackgroundTasks, filename: str):
    """Zip the photos of [forms] as <folder_of(form)>/<type>-<user>-<form id>/<photo>.webp."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    n = 0
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED) as z:  # WebP is already compressed
        for f in forms:
            d = storage.form_dir(f.form_type, f.list_number, f.username, f.form_id)
            for p in f.photos or []:
                path = os.path.join(d, p["name"])
                if os.path.isfile(path):
                    z.write(path, f"{folder_of(f)}/{f.form_type}-{storage.safe(f.username)}-{f.form_id}/{p['name']}")
                    n += 1
    if n == 0:
        os.unlink(tmp.name)
        raise HTTPException(404, "Those forms have no photos")
    background.add_task(os.unlink, tmp.name)
    return FileResponse(tmp.name, media_type="application/zip", filename=filename)


@app.get("/api/v1/admin/photos.zip")
def photos_zip(background: BackgroundTasks, list_number: str | None = None, reference_number: str | None = None,
               form_type: str | None = None, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    """Every photo of one List Number and/or one Reference Number. Duplicate surveys of the same
    number stay in separate sub folders: <number>/<type>-<user>-<form>/<photo>.webp"""
    if not list_number and not reference_number:
        raise HTTPException(422, "Give a List Number or a Reference Number")
    forms = _filtered_forms(db, form_type, list_number, reference_number)
    if not forms:
        raise HTTPException(404, "No forms found")
    forms.sort(key=lambda f: f.created_at)
    key = reference_number or list_number
    return _zip_photos(forms, lambda f: storage.safe(key), background, f"{storage.safe(key)}.zip")


@app.get("/api/v1/admin/photos_bulk.zip")
def photos_bulk(form_type: str, background: BackgroundTasks, by: str = "list",
                _: User = Depends(admin_user), db: Session = Depends(get_db)):
    """All photos of a form type, sorted into one folder per List Number (by=list) or per
    Reference Number (by=reference)."""
    if form_type not in config.FORM_TYPES:
        raise HTTPException(422, "Bad form_type")
    if by not in ("list", "reference"):
        raise HTTPException(422, "by must be list or reference")
    forms = _filtered_forms(db, form_type)
    forms.sort(key=lambda f: f.created_at)
    folder = (lambda f: storage.safe(f.list_number)) if by == "list" else (lambda f: storage.safe(_reference(f), "_no_reference"))
    return _zip_photos(forms, folder, background, f"{form_type}_photos_by_{by}.zip")


# ------------------------------------------------- admin: project master data

@app.post("/api/v1/admin/projects/{project}/master")
async def upload_master(project: str, file: UploadFile = File(...), replace: bool = False,
                        _: User = Depends(admin_user), db: Session = Depends(get_db)):
    """Excel (.xlsx) or CSV with the header: List Number, Circle, Division, Sub Division,
    Feeder Name, Feeder Code. New List Numbers are added, existing ones updated. With
    replace=true List Numbers missing from the file are removed (surveys already received stay)."""
    if project not in config.PROJECTS:
        raise HTTPException(404, "No such project")
    rows = projects.parse_master(await file.read(), file.filename or "")
    existing = {m.list_number: m for m in db.query(MasterRow).filter(MasterRow.project == project)}
    added = updated = 0
    for r in rows:
        m = existing.get(r["list_number"])
        if m is None:
            db.add(MasterRow(project=project, **r))
            added += 1
        else:
            if any(getattr(m, k) != v for k, v in r.items()):
                updated += 1
            for k, v in r.items():
                setattr(m, k, v)
    removed = 0
    if replace:
        keep = {r["list_number"] for r in rows}
        for ln, m in existing.items():
            if ln not in keep:
                db.delete(m)
                removed += 1
    db.commit()
    return {"status": "ok", "added": added, "updated": updated, "removed": removed, "total_rows": len(rows)}


@app.get("/api/v1/admin/projects/{project}/master")
def master_summary(project: str, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    if project not in config.PROJECTS:
        raise HTTPException(404, "No such project")
    rows = db.query(MasterRow).filter(MasterRow.project == project).all()
    return {
        "list_numbers": len(rows),
        "circles": len({r.circle for r in rows}),
        "divisions": len({(r.circle, r.division) for r in rows}),
        "sub_divisions": len({(r.circle, r.division, r.sub_division) for r in rows}),
    }


@app.get("/api/v1/admin/projects/{project}/options")
def get_options(project: str, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    if project not in config.PROJECTS:
        raise HTTPException(404, "No such project")
    return projects.read_options(db, project)


@app.put("/api/v1/admin/projects/{project}/options")
def put_options(project: str, body: dict, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    """Replace dropdown lists, e.g. {"poleType": ["CONCRETE","STEEL","WOOD"],
    "tfRating": [{"value":"10","cable_width":"70 mm"}, ...]}. Phones pick the change up on their next download."""
    if project not in config.PROJECTS:
        raise HTTPException(404, "No such project")
    return projects.write_options(db, project, body)


app.mount("/admin", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True), name="admin")
