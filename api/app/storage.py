import os
import re
import shutil
import uuid

from . import config

SAFE = re.compile(r"[^A-Za-z0-9._-]+")
PHOTO_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}\.webp$")


def safe(text: str, default: str = "_none") -> str:
    s = SAFE.sub("_", (text or "").strip()).strip("._")
    return s[:60] or default


def photos_root() -> str:
    return os.path.join(config.DATA_DIR, "photos")


def form_dir(form_type: str, list_number: str, username: str, form_id: str) -> str:
    return os.path.join(
        photos_root(), form_type, safe(list_number), f"{safe(username)}__{safe(form_id)}"
    )


def replace_dir(final_dir: str, files: dict[str, bytes]) -> None:
    """Write all files to a temp folder, then swap it in. Never leaves a half-written form."""
    parent = os.path.dirname(final_dir)
    os.makedirs(parent, exist_ok=True)
    tmp = os.path.join(parent, f".tmp-{uuid.uuid4().hex}")
    os.makedirs(tmp)
    try:
        for name, data in files.items():
            with open(os.path.join(tmp, name), "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        if os.path.isdir(final_dir):
            shutil.rmtree(final_dir)
        os.rename(tmp, final_dir)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
