"""
integrations/drive.py — moving work in and out of Google Drive.

Two directions, and they do NOT have the same requirements:

*Uploading* finished videos needs a **Shared Drive**, not a folder in someone's
My Drive. A service account has no Drive storage quota of its own. Sharing a My
Drive folder with it looks like it should work and then fails on the first
upload with a storage quota error. A Shared Drive is owned by the organisation,
so files the service account creates there count against the *organisation's*
storage. This is not a preference — it is the only arrangement that works.

*Downloading* source clips has no such constraint: reading consumes nobody's
quota, so an ordinary My Drive folder shared with the service account is a
perfectly good source. The download half is therefore deliberately routed
around resolve_target() and its Shared Drive rules — see the download section.

Credentials come from Application Default Credentials, which on the VM means
its attached service account: no key file to store, rotate or leak. The VM must
be created (or updated) with the Drive scope, because the metadata server only
issues tokens for scopes the instance was configured with —
`cloud-platform` alone is NOT enough. BVG_DRIVE_CREDENTIALS_FILE overrides with
an explicit key file for local development.

Thread safety: googleapiclient services wrap an httplib2 connection, which is
not thread-safe. Each thread gets its own service object via thread-local
storage, which is the documented approach.
"""

from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable, Optional

import config
import workspace

logger = logging.getLogger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"

# Full Drive scope. `drive.file` (files this app created) would be tighter, but
# it cannot see a folder tree made by hand in the web UI, which is exactly how
# the destination folder tends to get set up.
SCOPES = ["https://www.googleapis.com/auth/drive"]

_local = threading.local()
_folder_lock = threading.Lock()


class DriveError(RuntimeError):
    """Raised for configuration problems worth showing the user verbatim."""


def _no_credentials_help(what: str) -> str:
    """Google's own 'default credentials were not found' says nothing about
    what to do, and the answer differs between the VM and a laptop."""
    return (
        f"No Google credentials found, so {what} can't be reached.\n\n"
        "**On the GCP VM** this means the instance has no service account "
        "attached, or was created without the right scopes — `cloud-platform` "
        "alone does NOT include Drive. See DEPLOYMENT.md section 5b.\n\n"
        "**On this machine** there are simply no credentials yet. Download a "
        "JSON key for the service account and point at it:\n\n"
        "```\n"
        "GOOGLE_APPLICATION_CREDENTIALS=C:/path/to/service-account-key.json\n"
        "```\n\n"
        "Use the **service account** key rather than `gcloud auth "
        "application-default login`. Signing in as yourself would test *your* "
        "access, not the service account's — so the check could pass locally "
        "and still fail on the VM, which is the opposite of useful."
    )


def _import_google():
    try:
        import google.auth
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:  # pragma: no cover
        raise DriveError(
            "Google API libraries are missing. Install them with:\n"
            "    pip install google-api-python-client google-auth"
        ) from exc
    return google.auth, service_account, build, MediaFileUpload


def _credentials():
    google_auth, service_account, _, _ = _import_google()
    if config.DRIVE_CREDENTIALS_FILE:
        path = Path(config.DRIVE_CREDENTIALS_FILE)
        if not path.is_file():
            raise DriveError(f"Service-account key not found: {path}")
        return service_account.Credentials.from_service_account_file(
            str(path), scopes=SCOPES)
    creds, _project = google_auth.default(scopes=SCOPES)
    return creds


def service():
    """The Drive client for the calling thread."""
    existing = getattr(_local, "service", None)
    if existing is not None:
        return existing
    _, _, build, _ = _import_google()
    built = build("drive", "v3", credentials=_credentials(), cache_discovery=False)
    _local.service = built
    return built


_URL_ID = re.compile(r"/(?:folders|drive/u/\d+/folders|d)/([A-Za-z0-9_-]{10,})")


def extract_id(value: str) -> str:
    """Pull an id out of whatever the user pasted.

    Accepts a bare id or a full Drive URL, with or without the `?usp=sharing`
    tail. Pasting the URL straight from the browser is the obvious thing to do,
    so it is what the app accepts."""
    raw = (value or "").strip()
    if not raw:
        return ""
    match = _URL_ID.search(raw)
    if match:
        return match.group(1)
    # A bare id, possibly with a stray query string.
    return raw.split("?")[0].rstrip("/").split("/")[-1]


def looks_like_shared_drive(drive_id: str) -> bool:
    """Shared Drive ids start with '0A'; folder ids start with '1'.

    A useful first guess before spending an API call, and the thing that
    catches the most common setup mistake — pasting a My Drive folder link."""
    return str(drive_id or "").startswith("0A")


def set_target(value: Optional[str]) -> None:
    """Override the destination for this thread, from a job's own settings.

    Editing `.env` on the VM to change where a batch lands is impractical, so a
    job may carry its own folder link. Thread-local rather than a parameter
    threaded through every function, and cleared with set_target(None)."""
    _local.override = (value or "").strip() or None
    # A changed destination invalidates the cached resolution.
    _local.target = None


def _configured_target() -> str:
    """The destination for this call: the job's own link, else the env default."""
    return getattr(_local, "override", None) or config.DRIVE_SHARED_DRIVE_ID


def resolve_target() -> tuple[str, str]:
    """Work out (shared_drive_id, parent_folder_id) from what was configured.

    The configured value may be either a Shared Drive id or a folder id — the
    user pastes a link and should not have to know the difference. A folder is
    resolved to the Shared Drive that encloses it. A folder with no enclosing
    Shared Drive lives in someone's My Drive, which a service account cannot
    write to, so that is reported here rather than at upload time."""
    configured = extract_id(_configured_target())
    if not configured:
        raise DriveError(
            "No Drive destination configured. Set BVG_DRIVE_SHARED_DRIVE_ID — "
            "you can paste the folder URL straight from your browser."
        )

    cached = getattr(_local, "target", None)
    if cached and cached[0] == configured:
        return cached[1], cached[2]

    svc = service()
    drive_id = parent_id = ""

    # Is it a Shared Drive itself?
    try:
        svc.drives().get(driveId=configured, fields="id").execute()
        drive_id, parent_id = configured, configured
    except Exception:
        # Then it should be a folder. Ask which Shared Drive contains it.
        try:
            meta = svc.files().get(
                fileId=configured, fields="id, name, mimeType, driveId",
                supportsAllDrives=True).execute()
        except Exception as exc:  # noqa: BLE001
            raise DriveError(
                f"Could not open {configured}. Either the id is wrong, or the "
                "service account has not been given access to it. Share the "
                "folder with the service account as a Content Manager.\n\n"
                f"Raw error: {exc}"
            ) from exc

        if meta.get("mimeType") != FOLDER_MIME:
            raise DriveError(
                f"{configured} is a file ({meta.get('name')}), not a folder. "
                "Paste the URL of the folder you want videos uploaded into."
            )
        if not meta.get("driveId"):
            raise DriveError(
                f"The folder “{meta.get('name')}” is in a personal My Drive, "
                "not a Shared Drive.\n\n"
                "A service account has no storage of its own, so uploads there "
                "fail no matter how the folder is shared. Create a Shared Drive "
                "(drive.google.com → Shared drives → New), move this folder "
                "into it, share it with the service account as an Editor, and "
                "paste the new link."
            )
        drive_id, parent_id = meta["driveId"], configured

    _local.target = (configured, drive_id, parent_id)
    return drive_id, parent_id


def _require_config() -> str:
    """The enclosing Shared Drive id, for queries that need `driveId`."""
    return resolve_target()[0]


def root_folder_id() -> str:
    """Where this app writes: the configured folder, or an explicit override."""
    if config.DRIVE_ROOT_FOLDER_ID:
        return extract_id(config.DRIVE_ROOT_FOLDER_ID)
    return resolve_target()[1]


# --------------------------------------------------------------------------- folders

def _escape(name: str) -> str:
    """Drive query strings are single-quoted; escape the quotes inside."""
    return name.replace("\\", "\\\\").replace("'", "\\'")


def find_folder(name: str, parent_id: str) -> Optional[str]:
    drive_id = _require_config()
    query = (
        f"name = '{_escape(name)}' and '{parent_id}' in parents "
        f"and mimeType = '{FOLDER_MIME}' and trashed = false"
    )
    response = service().files().list(
        q=query, spaces="drive", fields="files(id, name)",
        corpora="drive", driveId=drive_id,
        includeItemsFromAllDrives=True, supportsAllDrives=True,
        pageSize=10,
    ).execute()
    files = response.get("files", [])
    return files[0]["id"] if files else None


def ensure_folder(name: str, parent_id: str) -> str:
    """Find or create one folder. Serialised, because two threads creating the
    same folder concurrently would leave two folders with the same name —
    Drive allows that, which is exactly the trap this avoids."""
    with _folder_lock:
        existing = find_folder(name, parent_id)
        if existing:
            return existing
        created = service().files().create(
            body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
            fields="id", supportsAllDrives=True,
        ).execute()
        logger.info("Created Drive folder %s under %s", name, parent_id)
        return created["id"]


def ensure_path(parts: Iterable[str], parent_id: Optional[str] = None) -> str:
    """Create a nested folder path, returning the deepest folder's id."""
    current = parent_id or root_folder_id()
    for part in parts:
        clean = str(part).strip().strip("/")
        if clean:
            current = ensure_folder(clean, current)
    return current


def folder_link(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


# --------------------------------------------------------------------------- upload

def unique_names(names: Iterable[str]) -> list[str]:
    """De-duplicate a list of filenames, returned parallel to the input.

    Google Drive happily stores twelve files called `001_Sale.mp4` in the same
    folder, so uniqueness has to be enforced here or a batch silently ends up
    with duplicates that are impossible to tell apart. Returns a list rather
    than a dict precisely because the interesting case is repeated names, which
    a name-keyed mapping would collapse."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        count = seen.get(name, 0)
        seen[name] = count + 1
        if count == 0:
            out.append(name)
            continue
        stem, dot, ext = name.rpartition(".")
        base = stem if dot else name
        suffix = f".{ext}" if dot else ""
        out.append(f"{base}_{count + 1}{suffix}")
    return out


def upload_file(path: Path, parent_id: str, name: Optional[str] = None) -> dict:
    """Resumable upload of one file. Returns {id, name, webViewLink}."""
    _, _, _, MediaFileUpload = _import_google()
    path = Path(path)
    if not path.is_file():
        raise DriveError(f"File to upload does not exist: {path}")

    media = MediaFileUpload(
        str(path), chunksize=config.DRIVE_CHUNK_BYTES, resumable=True)
    request = service().files().create(
        body={"name": name or path.name, "parents": [parent_id]},
        media_body=media,
        fields="id, name, webViewLink",
        supportsAllDrives=True,
    )
    response = None
    while response is None:
        # num_retries gives us exponential backoff on 5xx/429 for free.
        _status, response = request.next_chunk(num_retries=3)
    return response


def copy_file(file_id: str, new_name: str, parent_id: Optional[str] = None) -> dict:
    """Duplicate an already-uploaded file under a different name.

    This is a **server-side** copy: Drive clones the bytes internally and
    nothing is re-uploaded. That is why each video is uploaded once and copied,
    rather than uploaded twice — at 10,000 videos of ~8 MB, uploading both
    names would push ~160 GB over the wire instead of ~80 GB, and the copy
    calls are metadata-speed by comparison."""
    body: dict = {"name": new_name}
    if parent_id:
        body["parents"] = [parent_id]
    # Deliberately NO num_retries here. files.copy is a non-idempotent POST,
    # and the client's retry loop re-issues it on transport failures (resets,
    # read timeouts) that can land AFTER Drive committed the copy — silently
    # duplicating the file inside one apparently-healthy call. A failed copy
    # is retried at the item level instead, behind a find_file() check that
    # looks for the ambiguous previous attempt before copying again.
    return service().files().copy(
        fileId=file_id, body=body,
        fields="id, name, webViewLink",
        supportsAllDrives=True,
    ).execute()


def find_file(name: str, parent_id: str,
              drive_id: Optional[str] = None) -> Optional[dict]:
    """First non-trashed file with this exact name under `parent_id`, as
    {id, name, webViewLink} — or None.

    Exists for the ambiguous-copy check in the render runner: a files.copy
    whose response was lost may or may not have committed, and the only way to
    find out is to look. `drive_id` should be passed by callers running on
    worker threads — resolve_target() is thread-local, so resolving here on a
    pool thread would ignore a job's own destination override."""
    drive_id = drive_id or _require_config()
    query = (
        f"name = '{_escape(name)}' and '{parent_id}' in parents "
        f"and mimeType != '{FOLDER_MIME}' and trashed = false"
    )
    response = service().files().list(
        q=query, spaces="drive", fields="files(id, name, webViewLink)",
        corpora="drive", driveId=drive_id,
        includeItemsFromAllDrives=True, supportsAllDrives=True,
        pageSize=1,
    ).execute()
    files = response.get("files", [])
    return files[0] if files else None


def upload_with_copy(path: Path, parent_id: str, primary_name: str,
                     secondary_name: str) -> tuple[dict, dict]:
    """Publish one video under two names, transferring the bytes once."""
    uploaded = upload_file(path, parent_id, primary_name)
    copied = copy_file(uploaded["id"], secondary_name, parent_id)
    return uploaded, copied


def upload_many(
    files: list[tuple[Path, str]],
    parent_id: str,
    concurrency: Optional[int] = None,
    on_result: Optional[Callable[[Path, str, Optional[dict], Optional[Exception]], None]] = None,
) -> tuple[int, int]:
    """Upload (path, drive_name) pairs in parallel.

    `on_result` is called per file as it settles so the caller can record
    per-item state immediately — a crash mid-upload then resumes from what
    actually landed rather than re-uploading everything.

    Returns (succeeded, failed). Never raises for a single bad file."""
    if not files:
        return 0, 0
    workers = concurrency or config.DRIVE_UPLOAD_CONCURRENCY
    ok = failed = 0

    def _one(item):
        path, drive_name = item
        try:
            return item, upload_file(path, parent_id, drive_name), None
        except Exception as exc:  # noqa: BLE001 — reported per file
            return item, None, exc

    # as_completed, not map: Executor.map yields strictly in submission order,
    # so one slow upload would hold back the on_result callback — and therefore
    # the per-item state it persists — for every file that finished behind it.
    # A crash at that moment would leave files in Drive still marked pending,
    # and the resumed run would upload them a second time.
    from concurrent.futures import as_completed

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_one, item) for item in files]
        for future in as_completed(futures):
            (path, drive_name), response, error = future.result()
            if error is None:
                ok += 1
            else:
                failed += 1
                logger.warning("Drive upload failed for %s: %s", path.name, error)
            if on_result:
                on_result(path, drive_name, response, error)
    return ok, failed


# --------------------------------------------------------------------------- download

SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

# How far down a source folder is walked. People organise clip libraries in
# sub-folders, so recursing is the useful default; the cap only stops a
# pathological tree from turning into thousands of listing calls.
MAX_SOURCE_DEPTH = 5

# Drive filenames may contain characters Windows forbids outright.
_ILLEGAL_IN_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Part-files live in a sub-folder rather than beside their target, because
# workspace_from_dir() treats *every file* in a cta_slot_N folder as a clip —
# a stray `foo.mp4.part` would be handed to FFmpeg as if it were one. It skips
# directories, so a folder of half-written bytes is invisible to it.
_INCOMING = ".incoming"


def safe_name(name: str) -> str:
    """A Drive filename made safe to write locally.

    Not only about storage: a clip's local filename is how an Excel
    `CTA_Clip_<n>` cell pins it, so the name has to survive the trip."""
    clean = _ILLEGAL_IN_FILENAME.sub("_", str(name or "").strip()).strip(". ")
    return clean or "clip"


def _looks_like_video(name: str, mime: str) -> bool:
    """Whether a Drive entry is a clip worth fetching.

    mimeType first, but a file that arrived through Drive-for-desktop sync
    often carries application/octet-stream — so the extension decides in that
    case, using the same list the renderer accepts on disk."""
    if str(mime or "").startswith("video/"):
        return True
    return workspace.is_video(str(name or ""))


def _children(folder_id: str) -> list[dict]:
    """Every non-trashed child of one folder, following pagination.

    Deliberately no `corpora`/`driveId`: a *source* folder may live in a Shared
    Drive or in someone's My Drive, and pinning a driveId makes the My Drive
    case return nothing at all rather than an error."""
    svc = service()
    out: list[dict] = []
    token = None
    while True:
        response = svc.files().list(
            q=f"'{_escape(folder_id)}' in parents and trashed = false",
            fields=("nextPageToken, files(id, name, mimeType, size, "
                    "shortcutDetails)"),
            includeItemsFromAllDrives=True, supportsAllDrives=True,
            pageSize=1000, pageToken=token,
        ).execute()
        out.extend(response.get("files", []))
        token = response.get("nextPageToken")
        if not token:
            return out


def _resolved(entry: dict) -> tuple[str, str]:
    """(id, mimeType) for an entry, following a shortcut to its target.

    Shortcuts are what you get by dragging a folder into another Drive, so a
    source folder full of them is an ordinary thing rather than an edge case."""
    if entry.get("mimeType") == SHORTCUT_MIME:
        details = entry.get("shortcutDetails") or {}
        return details.get("targetId") or "", details.get("targetMimeType") or ""
    return entry.get("id") or "", entry.get("mimeType") or ""


def folder_info(folder_id: str) -> dict:
    """Metadata for a folder we intend to READ, with a usable error if we can't.

    resolve_target() is not used here on purpose — it insists on a Shared Drive
    because it is about writing. Reading needs no quota, so refusing a My Drive
    folder here would reject a source that works perfectly well."""
    if not folder_id:
        raise DriveError("No Google Drive folder link was given.")
    try:
        meta = service().files().get(
            fileId=folder_id, fields="id, name, mimeType, shortcutDetails",
            supportsAllDrives=True).execute()
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "default credentials were not found" in message.lower():
            raise DriveError(_no_credentials_help("Drive")) from exc
        raise DriveError(
            f"Could not open the Drive folder `{folder_id}`. Either the link is "
            "wrong, or the service account has not been given access to it — "
            "share the folder with the service account's address (Viewer is "
            f"enough to read clips from it).\n\nRaw error: {exc}"
        ) from exc

    target_id, target_mime = _resolved(meta)
    if target_mime != FOLDER_MIME:
        raise DriveError(
            f"“{meta.get('name')}” is a file, not a folder. Paste the link of "
            "the folder that holds the clips — open it in Drive and copy the "
            "URL from the address bar."
        )
    return {"id": target_id, "name": meta.get("name") or target_id}


def list_videos(folder_id: str, recursive: bool = True,
                max_files: Optional[int] = None) -> list[dict]:
    """Every video file under a source folder, as [{id, name, size}].

    Ordering is deterministic — by folder path, then filename — because the
    names handed to the downloader are de-duplicated positionally. A resumed
    job has to rebuild the identical list or it would download the same clip
    again under a different name."""
    root = folder_info(folder_id)["id"]
    limit = max_files if max_files is not None else config.DRIVE_MAX_SOURCE_FILES

    found: list[tuple[tuple, str, dict]] = []
    stack: list[tuple[str, tuple]] = [(root, ())]
    seen_folders = {root}

    while stack:
        current, path = stack.pop()
        for entry in _children(current):
            entry_id, mime = _resolved(entry)
            if not entry_id:
                continue                      # a shortcut to a deleted file
            name = entry.get("name") or entry_id
            if mime == FOLDER_MIME:
                # Depth-capped, and a cycle of shortcuts must not loop forever.
                if (recursive and len(path) < MAX_SOURCE_DEPTH
                        and entry_id not in seen_folders):
                    seen_folders.add(entry_id)
                    stack.append((entry_id, path + (name,)))
                continue
            if not _looks_like_video(name, mime):
                continue
            found.append((path, name, {
                "id": entry_id,
                "name": name,
                "size": int(entry.get("size") or 0),
            }))

    found.sort(key=lambda f: (f[0], f[1], f[2]["id"]))
    if len(found) > limit:
        raise DriveError(
            f"That folder holds {len(found):,} videos, more than the "
            f"{limit:,}-file limit. Point at a folder with just the clips you "
            "want, or raise BVG_DRIVE_MAX_SOURCE_FILES."
        )
    return [f[2] for f in found]


def local_names(files: list[dict]) -> list[str]:
    """Filesystem-safe, collision-free local names, parallel to `files`.

    Drive is happy to hold three files called `clip.mp4` in one folder, and
    recursing merges sub-folders into a single flat pool — so uniqueness has to
    be imposed here or clips would silently overwrite each other."""
    return unique_names([safe_name(f.get("name") or f.get("id")) for f in files])


def download_file(file_id: str, dest: Path, expected_size: int = 0) -> bool:
    """Stream one Drive file to `dest`. True if bytes moved, False if skipped.

    The bytes land in a `.incoming` sub-folder and are renamed into place only
    once the transfer completes, so an interrupted download can never be
    mistaken for a finished clip — by the resume check below, or by the
    renderer, which would otherwise feed a truncated file to FFmpeg."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Checked before anything reaches for the network, so a resumed job that
    # already has every clip costs nothing at all.
    if dest.is_file() and (not expected_size or dest.stat().st_size == expected_size):
        return False

    from googleapiclient.http import MediaIoBaseDownload

    staging = dest.parent / _INCOMING
    staging.mkdir(exist_ok=True)
    part = staging / (dest.name + ".part")

    request = service().files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(part, "wb") as handle:
        downloader = MediaIoBaseDownload(
            handle, request, chunksize=config.DRIVE_CHUNK_BYTES)
        done = False
        while not done:
            # num_retries gives exponential backoff on 5xx/429 for free, and a
            # GET is safe to repeat.
            _status, done = downloader.next_chunk(num_retries=3)
    part.replace(dest)
    return True


def download_folder(
    folder_id: str,
    dest_dir: Path,
    concurrency: Optional[int] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Download every video under a Drive folder into `dest_dir`.

    Returns {folder, files, downloaded, skipped, failed, bytes, errors}.
    `skipped` counts clips a previous attempt already fetched: the whole point
    of the size check is that a re-run of a killed job costs only what was
    actually missing. One bad file is reported rather than raised, exactly as
    upload_many() does — a single unreadable clip should not throw away a
    download that otherwise succeeded."""
    from concurrent.futures import as_completed

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    info = folder_info(folder_id)
    files = list_videos(info["id"])
    names = local_names(files)
    total = len(files)

    report = {"folder": info["name"], "folder_id": info["id"], "files": total,
              "downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0,
              "errors": []}
    if not total:
        return report

    def _one(pair):
        entry, name = pair
        try:
            moved = download_file(entry["id"], dest_dir / name, entry["size"])
            return name, moved, entry["size"], None
        except Exception as exc:  # noqa: BLE001 — reported per file
            return name, False, 0, exc

    workers = concurrency or config.DRIVE_DOWNLOAD_CONCURRENCY
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_one, pair) for pair in zip(files, names)]
        for future in as_completed(futures):
            name, moved, size, error = future.result()
            done += 1
            if error is not None:
                report["failed"] += 1
                report["errors"].append(f"{name}: {error}")
                logger.warning("Drive download failed for %s: %s", name, error)
            elif moved:
                report["downloaded"] += 1
                report["bytes"] += size
            else:
                report["skipped"] += 1
            if on_progress:
                on_progress(done, total)

    # Best effort: an empty staging folder is litter, and a non-empty one holds
    # only the debris of failed transfers.
    staging = dest_dir / _INCOMING
    if staging.is_dir():
        import shutil

        shutil.rmtree(staging, ignore_errors=True)
    return report


def check_source(link: str) -> tuple[bool, str]:
    """Can we READ this folder, and what is in it?

    Separate from check_access() on purpose: that proves we can *write* into a
    Shared Drive, which a source folder never has to allow."""
    folder_id = extract_id(link or "")
    if not folder_id:
        return False, "Paste a Google Drive folder link first."
    try:
        info = folder_info(folder_id)
        files = list_videos(info["id"])
    except DriveError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not read that folder: {exc}"

    if not files:
        return False, (
            f"“{info['name']}” opened fine, but there are no video files in it "
            f"(looked in its sub-folders too). {folder_link(info['id'])}"
        )
    size_gb = sum(f["size"] for f in files) / 1024 ** 3
    return True, (
        f"“{info['name']}” — **{len(files):,} clips**, {size_gb:.2f} GB. "
        "They'll be downloaded straight to the server when the batch runs."
    )


# --------------------------------------------------------------------------- checks

def check_access(target: Optional[str] = None) -> tuple[bool, str]:
    """Diagnostic for the setup page: can we actually write where we're aimed?

    `target` tests one specific folder link instead of the configured default —
    worth doing before pointing a multi-hour job at somewhere new. The override
    is always cleared afterwards so it cannot leak into later calls on this
    thread."""
    if target is not None:
        set_target(target)
    try:
        return _check_access_inner()
    finally:
        if target is not None:
            set_target(None)


def _check_access_inner() -> tuple[bool, str]:
    if not _configured_target():
        return False, ("No Drive destination configured — set "
                       "BVG_DRIVE_SHARED_DRIVE_ID (a pasted folder URL is fine), "
                       "or paste a link above to test one directly.")
    try:
        drive_id, parent_id = resolve_target()
        info = service().drives().get(driveId=drive_id, fields="id, name").execute()
    except DriveError as exc:
        # These already carry a specific, actionable explanation.
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "default credentials were not found" in message.lower():
            return False, _no_credentials_help("Drive")
        if "insufficient" in message.lower() or "403" in message:
            return False, (
                "Access denied. Two usual causes: the service account isn't a "
                "member of the Shared Drive, or the VM was created without the "
                "Drive scope (cloud-platform alone is not enough).\n\n"
                f"Raw error: {message}"
            )
        return False, f"Drive check failed: {message}"

    where = ("the Shared Drive root" if parent_id == drive_id
             else f"folder `{parent_id}`")
    try:
        target = ensure_path(["_connection_test"], parent_id=parent_id)
    except Exception as exc:  # noqa: BLE001
        return False, (
            f"Can see Shared Drive “{info.get('name')}” but cannot create "
            "folders in it — the service account probably has Viewer or "
            f"Contributor access instead of Content Manager.\n\nRaw error: {exc}"
        )

    # Upload and copy are what the app actually does, and they are tested
    # separately so a failure names the operation instead of guessing. Deleting
    # is only cleanup — the app never deletes anything — so it must NOT be able
    # to fail the check. Reporting "uploading failed" when it was the tidy-up
    # that failed sends you looking in exactly the wrong place.
    import io as _io

    from googleapiclient.http import MediaIoBaseUpload

    try:
        probe = service().files().create(
            body={"name": "_write_test.txt", "parents": [target]},
            media_body=MediaIoBaseUpload(_io.BytesIO(b"ok"), mimetype="text/plain"),
            fields="id", supportsAllDrives=True).execute()
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        hint = ""
        if "quota" in message.lower():
            hint = ("\n\nA storage-quota error here means the destination is a "
                    "personal My Drive, not a Shared Drive — a service account "
                    "has no storage of its own.")
        elif "403" in message:
            hint = ("\n\nAdd the service account to the Shared Drive's members "
                    "as an Editor — or, if you added it to the Shared Drive itself "
                    "rather than the folder, a Content Manager. Viewer and "
                    "Commenter cannot upload.")
        return False, (
            f"Folders can be created in “{info.get('name')}”, but UPLOADING a "
            f"file failed.{hint}\n\nRaw error: {exc}")

    try:
        copy = copy_file(probe["id"], "_copy_test.txt", target)
    except Exception as exc:  # noqa: BLE001
        return False, (
            f"Upload works in “{info.get('name')}”, but the server-side COPY "
            "failed. Every video is published twice using files.copy, so this "
            "one matters.\n\n"
            f"Raw error: {exc}")

    # Best effort. A failure here leaves two tiny test files behind and is
    # worth mentioning, but it is not a reason to call the setup broken.
    leftovers = []
    for name, file_id in (("_write_test.txt", probe["id"]),
                          ("_copy_test.txt", copy.get("id"))):
        if not file_id:
            continue
        try:
            service().files().delete(fileId=file_id, supportsAllDrives=True).execute()
        except Exception:  # noqa: BLE001
            leftovers.append(name)

    # The probe folder is litter if it survives; removing it is best effort.
    try:
        service().files().delete(fileId=target, supportsAllDrives=True).execute()
    except Exception:  # noqa: BLE001
        pass

    note = ""
    if leftovers:
        note = (f"\n\n(Couldn't delete the test file(s) {', '.join(leftovers)} — "
                "harmless, and the app never deletes anything. Tidy them up by "
                "hand if you like. It usually means the service account is a "
                "an Editor on the folder rather than a member of the Shared "
                "Drive itself, "
                "which is enough for everything this app does.)")

    return True, (
        f"Connected to Shared Drive “{info.get('name')}”, writing into {where}. "
        f"Upload and server-side copy both verified. {folder_link(target)}{note}"
    )
