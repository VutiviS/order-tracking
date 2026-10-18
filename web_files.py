"""Is the website up to date? Each online app reports a short fingerprint of every file it runs (on /api/health);
the desktop app compares them with the files on this PC and gathers the ones that differ into one folder, ready to
drag into GitHub. Shared by the websites (a copy sits in each online folder: sync_web_files.py) and the desktop."""

import hashlib
import shutil
from datetime import datetime
from pathlib import Path

KINDS = {".py", ".html", ".js", ".json", ".txt", ".png", ".md", ".css", ".ico", ".svg"}
TEXT = {".py", ".html", ".js", ".json", ".txt", ".md", ".css", ".svg"}
NAMES = {"Procfile", "CNAME"}          # CNAME: the company website's own address (inventra.co.za) on GitHub Pages


def deployable(folder) -> list:
    """The files an online app needs (top level only: no caches, no hidden files)."""
    folder = Path(folder)
    return sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")
                  and (p.suffix.lower() in KINDS or p.name in NAMES))


def fingerprint(path) -> str:
    data = Path(path).read_bytes()
    if Path(path).suffix.lower() in TEXT or Path(path).name in NAMES:
        data = data.replace(b"\r\n", b"\n")           # GitHub / Windows line endings don't count as a change
    return hashlib.sha256(data).hexdigest()[:12]


def fingerprints(folder) -> dict:
    return {p.name: fingerprint(p) for p in deployable(folder)}


def compare(local_folder, online: dict) -> dict:
    """online: the "files" map from /api/health (None when the website is older than this check).
    Returns {"up_to_date": bool, "upload": [names], "known": bool}."""
    mine = fingerprints(local_folder)
    if not online:
        return {"up_to_date": False, "upload": sorted(mine), "known": False}
    upload = sorted(n for n, h in mine.items() if online.get(n) != h)
    return {"up_to_date": not upload, "upload": upload, "known": True}


def prepare_upload(local_folder, names, out_root) -> Path:
    """Copy the files to upload into a new folder (e.g. Documents/INVENTRA/Website upload/2026-10-15 0612)."""
    out = Path(out_root) / datetime.now().strftime("%Y-%m-%d %H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    for n in names:
        shutil.copy2(Path(local_folder) / n, out / n)
    return out
