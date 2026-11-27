import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./survey.db")
DATA_DIR = os.getenv("DATA_DIR", "./data")
SECRET_KEY = os.getenv("SECRET_KEY", "")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ACCESS_MINUTES = int(os.getenv("ACCESS_MINUTES", "60"))
REFRESH_DAYS = int(os.getenv("REFRESH_DAYS", "60"))
MAX_PHOTO_BYTES = int(os.getenv("MAX_PHOTO_BYTES", str(10 * 1024 * 1024)))
MAX_PHOTOS_PER_FORM = int(os.getenv("MAX_PHOTOS_PER_FORM", "30"))
# Daily purge of forms+photos older than RETENTION_DAYS (off by default while testing).
PURGE_ENABLED = os.getenv("PURGE_ENABLED", "false").lower() == "true"
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "183"))
GENERAL_TYPES = ("apms", "single", "three")
# Server-backed projects: each has a master list (circle/division/sub division/List Number/feeder)
# and its own survey form. A user may be limited to one of them (users.project).
PROJECTS = ("pesco", "lesco")
FORM_TYPES = GENERAL_TYPES + PROJECTS
# Projects whose master sheet has NO List Number: each row is identified by its TRF Code, and the
# surveyor picks a TRF Code. Internally the TRF Code is stored in the same "list_number" key
# (master_rows.list_number, forms.list_number, photo folders), so done / not-done, photo zips and
# filters work unchanged. LESCO still uses List Numbers.
TRF_PROJECTS = tuple(x for x in os.getenv("TRF_PROJECTS", "pesco").lower().replace(" ", "").split(",") if x in PROJECTS)

# Daily permission: the admin gives every surveyor permission for the day (Admin page ->
# "Give permission"). The user must open the app within PERMISSION_OPEN_HOURS of that, otherwise
# the account is locked until the admin gives permission again. Permission ends at midnight
# (TIMEZONE). Admins are never limited. Set PERMISSION_REQUIRED=false to switch it off.
PERMISSION_REQUIRED = os.getenv("PERMISSION_REQUIRED", "true").lower() == "true"
PERMISSION_OPEN_HOURS = float(os.getenv("PERMISSION_OPEN_HOURS", "2"))
TIMEZONE = os.getenv("TIMEZONE", "Asia/Karachi")
