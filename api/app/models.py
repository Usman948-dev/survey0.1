from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

JsonType = JSON().with_variant(JSONB(), "postgresql")


def now():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(10), default="user")  # user | admin
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # None = no PESCO/LESCO access; "pesco" / "lesco" = limited to that project (admins ignore it)
    project: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # daily permission (see permission.py): when the admin last gave it, and when the user
    # first opened the app after that
    permission_granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    permission_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Form(Base):
    __tablename__ = "forms"
    __table_args__ = (UniqueConstraint("username", "form_id", name="uq_user_form"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    form_id: Mapped[str] = mapped_column(String(64))
    form_type: Mapped[str] = mapped_column(String(10), index=True)
    list_number: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # on the phone
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    data: Mapped[dict] = mapped_column(JsonType)
    photos: Mapped[list] = mapped_column(JsonType, default=list)  # [{name, sha256, size}]


class Expected(Base):
    """Master list: which List Numbers SHOULD be surveyed (for done / not-done)."""
    __tablename__ = "expected"
    __table_args__ = (UniqueConstraint("form_type", "list_number", name="uq_expected"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    form_type: Mapped[str] = mapped_column(String(10), index=True)
    list_number: Mapped[str] = mapped_column(String(64))


class MasterRow(Base):
    """One row of the project master sheet: a List Number with its circle, division,
    sub division and feeder. The circle -> division -> sub division tree is derived from these."""
    __tablename__ = "master_rows"
    __table_args__ = (UniqueConstraint("project", "list_number", name="uq_master"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project: Mapped[str] = mapped_column(String(10), index=True)
    list_number: Mapped[str] = mapped_column(String(64))
    circle: Mapped[str] = mapped_column(String(120))
    division: Mapped[str] = mapped_column(String(120))
    sub_division: Mapped[str] = mapped_column(String(120))
    feeder_name: Mapped[str] = mapped_column(String(160), default="")
    feeder_code: Mapped[str] = mapped_column(String(80), default="")
    location: Mapped[str] = mapped_column(String(255), default="")
    trf_code: Mapped[str] = mapped_column(String(80), default="")
    capacity_kva: Mapped[str] = mapped_column(String(40), default="")
    reference_no: Mapped[str] = mapped_column(String(80), default="")


class ProjectOption(Base):
    """Admin-editable dropdown choices of a project form (TF rating, pole type, earthing)."""
    __tablename__ = "project_options"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project: Mapped[str] = mapped_column(String(10), index=True)
    field: Mapped[str] = mapped_column(String(30))
    value: Mapped[str] = mapped_column(String(60))
    extra: Mapped[str] = mapped_column(String(60), default="")  # TF rating -> cable width
    position: Mapped[int] = mapped_column(Integer, default=0)
