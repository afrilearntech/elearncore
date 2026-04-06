import os
import json
import hashlib
import base64
from dataclasses import dataclass
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

from elearncore.sysutils.constants import Status as StatusEnum

from dotenv import load_dotenv, find_dotenv

# Load environment variables.
#
# In this repo, deployments often keep the env file at `elearncore/.env`.
# `python-dotenv` only auto-discovers a file literally named `.env` in the
# current/parent dirs, so we load both locations.
load_dotenv(find_dotenv(), override=False)
_alt_dotenv = Path(__file__).resolve().parent / "elearncore" / ".env"
if _alt_dotenv.exists():
    load_dotenv(_alt_dotenv, override=False)


# -----------------------------
# CONFIG
# -----------------------------
# Central server API base (must include the /api-v1 prefix)
API_BASE_URL = (os.getenv("SYNC_API_BASE_URL") or "").rstrip("/")

# Knox token (Authorization: Token <token>)
SYNC_TOKEN = os.getenv("SYNC_TOKEN") or ""

# Optional auto-login (recommended for boxes that may stay offline for days/weeks)
# If SYNC_TOKEN isn't set (or expires), the engine will login using these creds.
SYNC_LOGIN_KIND = (os.getenv("SYNC_LOGIN_KIND") or "content").strip().lower()  # content|admin|student|parent
SYNC_LOGIN_IDENTIFIER = (os.getenv("SYNC_LOGIN_IDENTIFIER") or "").strip()
SYNC_LOGIN_PASSWORD = os.getenv("SYNC_LOGIN_PASSWORD") or ""
SYNC_LOGIN_URL = (os.getenv("SYNC_LOGIN_URL") or "").strip()  # optional override

# Persist tokens into the sync state file so cron runs don't require env SYNC_TOKEN.
STORE_TOKEN_IN_STATE = (os.getenv("SYNC_STORE_TOKEN", "true").strip().lower() not in {"0", "false", "no"})

# Request tuning
REQUEST_TIMEOUT = float(os.getenv("SYNC_TIMEOUT", "30"))
VERIFY_SSL = (os.getenv("SYNC_VERIFY_SSL", "true").strip().lower() not in {"0", "false", "no"})
PAGE_LIMIT = int(os.getenv("SYNC_PAGE_LIMIT", "500"))
DOWNLOAD_THREADS = int(os.getenv("SYNC_DOWNLOAD_THREADS", "6"))

# Where to store state + (optionally) other downloaded artifacts.
# Default keeps Linux boxes using /afriboxdata while Windows/dev uses a local folder.
_default_data_dir = Path("/afriboxdata") if os.name != "nt" else (Path(__file__).resolve().parent / "afriboxdata")
DATA_DIR = Path(os.getenv("SYNC_DATA_DIR", str(_default_data_dir)))
STATE_FILE = Path(os.getenv("SYNC_STATE_FILE", str(DATA_DIR / "sync_state.json")))

DATA_DIR.mkdir(parents=True, exist_ok=True)


# -----------------------------
# UTILS
# -----------------------------
def log(msg):
    print(f"[SYNC] {msg}")


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"last_sync": None, "cursors": {}}
    return {"last_sync": None, "cursors": {}}


def save_state(state):
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def file_hash(path):
    if not os.path.exists(path):
        return None

    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            h.update(chunk)
    return h.hexdigest()


def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "elearncore-offline-sync/1.0"})
    return s


def _set_session_token(session: requests.Session, token: str | None) -> None:
    if token:
        session.headers.update({"Authorization": f"Token {token}"})
    else:
        session.headers.pop("Authorization", None)


def _token_from_state(state: dict) -> str | None:
    try:
        token = state.get("auth", {}).get("token")
        token = str(token).strip() if token else ""
        return token or None
    except Exception:
        return None


def _store_token(state: dict, token: str) -> None:
    if not STORE_TOKEN_IN_STATE:
        return
    state.setdefault("auth", {})
    state["auth"]["token"] = token


def _login_and_get_token(session: requests.Session) -> str:
    if not API_BASE_URL:
        raise RuntimeError("SYNC_API_BASE_URL is required (must include /api-v1).")

    if not SYNC_LOGIN_IDENTIFIER or not SYNC_LOGIN_PASSWORD:
        raise RuntimeError(
            "Auto-login requires SYNC_LOGIN_IDENTIFIER and SYNC_LOGIN_PASSWORD (or set SYNC_TOKEN)."
        )

    kind = SYNC_LOGIN_KIND or "content"
    if kind not in {"content", "admin", "student", "parent"}:
        raise RuntimeError("SYNC_LOGIN_KIND must be one of: content, admin, student, parent")

    login_url = SYNC_LOGIN_URL or f"{API_BASE_URL}/auth/{kind}/"

    # IMPORTANT: Do not send Authorization on login.
    # DRF authentication can reject requests with invalid/expired tokens even
    # when the view permission is AllowAny.
    session.headers.pop("Authorization", None)

    payload = {"identifier": SYNC_LOGIN_IDENTIFIER, "password": SYNC_LOGIN_PASSWORD}
    res = session.post(login_url, json=payload, timeout=REQUEST_TIMEOUT, verify=VERIFY_SSL)
    res.raise_for_status()
    data = res.json() if res.content else {}
    token = (data.get("token") or "").strip() if isinstance(data, dict) else ""
    if not token:
        raise RuntimeError("Login succeeded but no token was returned.")
    return token


def _ensure_authenticated_session(session: requests.Session, state: dict) -> None:
    """Ensure the session has an Authorization token set.

    Order of precedence:
    1) SYNC_TOKEN env var
    2) token stored in sync_state.json
    3) auto-login using SYNC_LOGIN_* env vars
    """

    # Explicit token always wins.
    if SYNC_TOKEN:
        _set_session_token(session, SYNC_TOKEN)
        _store_token(state, SYNC_TOKEN)
        return

    saved = _token_from_state(state)
    if saved:
        _set_session_token(session, saved)
        return

    # No token available; login and persist.
    log("No token configured; performing login...")
    token = _login_and_get_token(session)
    _set_session_token(session, token)
    _store_token(state, token)
    save_state(state)


def _request_json(
    session: requests.Session,
    *,
    method: str,
    url: str,
    state: dict,
    retry_on_unauthorized: bool = True,
    **kwargs,
) -> dict[str, Any]:
    """Make an HTTP request and return JSON with a single auth-refresh retry."""

    res = session.request(method, url, timeout=REQUEST_TIMEOUT, verify=VERIFY_SSL, **kwargs)
    if retry_on_unauthorized and res.status_code == 401 and (SYNC_LOGIN_IDENTIFIER and SYNC_LOGIN_PASSWORD):
        # Token likely expired/invalid. Re-login and retry once.
        log("Token expired/unauthorized; re-authenticating...")
        try:
            token = _login_and_get_token(session)
            _set_session_token(session, token)
            _store_token(state, token)
            save_state(state)
        except Exception as e:
            raise RuntimeError(f"Re-authentication failed: {e}") from e

        res = session.request(method, url, timeout=REQUEST_TIMEOUT, verify=VERIFY_SSL, **kwargs)

    res.raise_for_status()
    return res.json() if res.content else {}


def _safe_local_path(root: Path, rel_path: str) -> Path:
    """Prevent path traversal when writing files under MEDIA_ROOT."""
    rel_path = str(rel_path or "").strip().lstrip("/\\")
    if not rel_path:
        raise ValueError("empty path")
    if ".." in Path(rel_path).parts:
        raise ValueError("unsafe path")
    target = (root / rel_path).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError("unsafe path")
    return target


def _django_setup():
    """Boot Django so we can write to the local DB and resolve MEDIA_ROOT."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", os.getenv("DJANGO_SETTINGS_MODULE") or "elearncore.settings")
    import django  # noqa: WPS433 (runtime import is intentional for scripts)

    django.setup()
    from django.conf import settings  # noqa: WPS433

    return settings


# -----------------------------
# DOWNLOAD (RESUMABLE)
# -----------------------------
@dataclass(frozen=True)
class DownloadTask:
    url: str
    path: Path
    expected_size: int | None = None


def download_file(url: str, path: Path, *, expected_size: int | None = None):
    """Download a file with resume support.

    - Writes to `<path>.part` first
    - Resumes if partial file exists
    - Atomically replaces final file on success
    """

    if not url:
        return

    # If we already have the file with matching size, skip.
    try:
        if path.exists() and expected_size is not None and path.stat().st_size == int(expected_size):
            return
        if path.exists() and expected_size is None:
            return
    except Exception:
        pass

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".part")

    headers: dict[str, str] = {}
    downloaded = 0
    if temp_path.exists():
        try:
            downloaded = temp_path.stat().st_size
            if downloaded > 0:
                headers["Range"] = f"bytes={downloaded}-"
        except Exception:
            downloaded = 0

    with requests.get(url, stream=True, headers=headers, timeout=REQUEST_TIMEOUT, verify=VERIFY_SSL) as r:
        r.raise_for_status()

        mode = "ab" if temp_path.exists() else "wb"
        with open(temp_path, mode) as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

    os.replace(temp_path, path)


def download_files_parallel(tasks: list[DownloadTask], *, max_workers: int) -> list[tuple[DownloadTask, Exception]]:
    if not tasks:
        return []

    failures: list[tuple[DownloadTask, Exception]] = []
    workers = max(1, int(max_workers))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(download_file, t.url, t.path, expected_size=t.expected_size): t
            for t in tasks
        }

        for fut in as_completed(future_map):
            task = future_map[fut]
            try:
                fut.result()
            except Exception as e:
                failures.append((task, e))

    return failures


# -----------------------------
# SYNC LOGIC
# -----------------------------
def _fetch_page(
    session: requests.Session,
    *,
    state: dict,
    resource: str,
    since: str | None,
    cursor: str | None,
) -> dict[str, Any]:
    url = f"{API_BASE_URL}/sync/{resource}/"
    params: dict[str, Any] = {"limit": PAGE_LIMIT}
    if since:
        params["since"] = since
    if cursor:
        params["cursor"] = cursor

    return _request_json(session, method="GET", url=url, params=params, state=state)


def _collect_downloads(*, media_root: Path, items: list[dict[str, Any]]) -> list[DownloadTask]:
    tasks: list[DownloadTask] = []

    def _maybe_add(file_obj: dict[str, Any] | None):
        if not file_obj:
            return
        path = file_obj.get("path")
        url = file_obj.get("url")
        size = file_obj.get("size")
        if not path or not url:
            return
        try:
            local_path = _safe_local_path(media_root, str(path))
        except Exception:
            return
        try:
            expected_size = int(size) if size is not None else None
        except Exception:
            expected_size = None
        tasks.append(DownloadTask(url=str(url), path=local_path, expected_size=expected_size))

    for item in items:
        if not isinstance(item, dict):
            continue
        _maybe_add(item.get("thumbnail"))
        _maybe_add(item.get("resource"))
        _maybe_add(item.get("image"))

    # Deduplicate by local path
    dedup: dict[str, DownloadTask] = {}
    for t in tasks:
        dedup[str(t.path)] = t
    return list(dedup.values())


def sync():
    if not API_BASE_URL:
        raise RuntimeError(
            "SYNC_API_BASE_URL is not set. Example: https://elearnapi.example.com/api-v1"
        )

    state = load_state()
    state.setdefault("cursors", {})
    state.setdefault("auth", {})
    last_sync = state.get("last_sync")

    log(f"Server base: {API_BASE_URL}")
    log(f"Last sync cutoff: {last_sync}")

    # Boot local Django (offline box) and point downloads into MEDIA_ROOT.
    settings = _django_setup()
    media_root = Path(getattr(settings, "MEDIA_ROOT", DATA_DIR)).resolve()
    media_root.mkdir(parents=True, exist_ok=True)

    # Import models only after Django is ready.
    from content.models import (  # noqa: WPS433
        Subject,
        Topic,
        Period,
        LessonResource,
        GameModel,
        GeneralAssessment,
        LessonAssessment,
        Question,
        Option,
    )
    from django.utils.dateparse import parse_datetime  # noqa: WPS433
    from django.utils import timezone  # noqa: WPS433

    def _parse_dt(value):
        if value in (None, ""):
            return None
        dt = parse_datetime(str(value))
        if dt is None:
            return None
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone=timezone.utc)
        return dt

    session = _build_session()
    _ensure_authenticated_session(session, state)

    # IMPORTANT: we store the cutoff timestamp from the FIRST sync call.
    # This prevents missing updates that happen while we are mid-sync.
    sync_cutoff: str | None = None

    download_tasks: list[DownloadTask] = []

    # Sync order matters because of FK dependencies.
    sync_plan = [
        ("subjects", Subject),
        ("topics", Topic),
        ("periods", Period),
        ("lessons", LessonResource),
        ("games", GameModel),
        ("general_assessments", GeneralAssessment),
        ("lesson_assessments", LessonAssessment),
        ("questions", Question),
        ("options", Option),
    ]

    for resource, model in sync_plan:
        cursor = state["cursors"].get(resource)
        total_items = 0
        log(f"Syncing {resource}...")

        while True:
            payload = _fetch_page(session, state=state, resource=resource, since=last_sync, cursor=cursor)
            if sync_cutoff is None:
                sync_cutoff = payload.get("server_time")

            items = payload.get("items") or []
            next_cursor = payload.get("next_cursor")

            # Upsert into local DB
            if resource == "subjects":
                for it in items:
                    thumb = (it.get("thumbnail") or {}) if isinstance(it, dict) else {}
                    Subject.objects.update_or_create(
                        id=it.get("id"),
                        defaults={
                            "name": it.get("name") or "",
                            "grade": it.get("grade") or "",
                            "description": it.get("description") or "",
                            "objectives": it.get("objectives") or "",
                            "status": it.get("status") or StatusEnum.APPROVED.value,
                            "moderation_comment": it.get("moderation_comment") or "",
                            "thumbnail": thumb.get("path") or None,
                            "created_by": None,
                        },
                    )

            elif resource == "topics":
                for it in items:
                    Topic.objects.update_or_create(
                        id=it.get("id"),
                        defaults={
                            "subject_id": it.get("subject_id"),
                            "name": it.get("name") or "",
                        },
                    )

            elif resource == "periods":
                for it in items:
                    Period.objects.update_or_create(
                        id=it.get("id"),
                        defaults={
                            "name": it.get("name") or "",
                            "start_month": it.get("start_month"),
                            "end_month": it.get("end_month"),
                        },
                    )

            elif resource == "lessons":
                for it in items:
                    res_file = (it.get("resource") or {}) if isinstance(it, dict) else {}
                    thumb = (it.get("thumbnail") or {}) if isinstance(it, dict) else {}
                    LessonResource.objects.update_or_create(
                        id=it.get("id"),
                        defaults={
                            "subject_id": it.get("subject_id"),
                            "topic_id": it.get("topic_id") or None,
                            "period_id": it.get("period_id") or None,
                            "instructor_name": it.get("instructor_name") or "",
                            "title": it.get("title") or "",
                            "description": it.get("description") or "",
                            "type": it.get("type") or "VIDEO",
                            "status": it.get("status") or StatusEnum.APPROVED.value,
                            "duration_minutes": it.get("duration_minutes"),
                            "moderation_comment": it.get("moderation_comment") or "",
                            "resource": res_file.get("path") or "",
                            "thumbnail": thumb.get("path") or None,
                            "created_by": None,
                        },
                    )

            elif resource == "games":
                for it in items:
                    img = (it.get("image") or {}) if isinstance(it, dict) else {}
                    GameModel.objects.update_or_create(
                        id=it.get("id"),
                        defaults={
                            "name": it.get("name") or "",
                            "instructions": it.get("instructions") or "",
                            "description": it.get("description") or "",
                            "grade": it.get("grade") or "GRADE 2",
                            "hint": it.get("hint") or "",
                            "correct_answer": it.get("correct_answer") or "",
                            "type": it.get("type") or "WORD_PUZZLE",
                            "status": it.get("status") or StatusEnum.APPROVED.value,
                            "image": img.get("path") or None,
                            "created_by": None,
                        },
                    )

            elif resource == "general_assessments":
                for it in items:
                    GeneralAssessment.objects.update_or_create(
                        id=it.get("id"),
                        defaults={
                            "title": it.get("title") or "",
                            "instructions": it.get("instructions") or "",
                            "type": it.get("type") or "ASSIGNMENT",
                            "marks": it.get("marks") or 0.0,
                            "due_at": _parse_dt(it.get("due_at")),
                            "grade": it.get("grade") or None,
                            "ai_recommended": bool(it.get("ai_recommended") or False),
                            "is_targeted": bool(it.get("is_targeted") or False),
                            "status": it.get("status") or StatusEnum.APPROVED.value,
                            "moderation_comment": it.get("moderation_comment") or "",
                        },
                    )

            elif resource == "lesson_assessments":
                for it in items:
                    LessonAssessment.objects.update_or_create(
                        id=it.get("id"),
                        defaults={
                            "lesson_id": it.get("lesson_id"),
                            "title": it.get("title") or "",
                            "instructions": it.get("instructions") or "",
                            "type": it.get("type") or "QUIZ",
                            "marks": it.get("marks") or 0.0,
                            "due_at": _parse_dt(it.get("due_at")),
                            "ai_recommended": bool(it.get("ai_recommended") or False),
                            "is_targeted": bool(it.get("is_targeted") or False),
                            "status": it.get("status") or StatusEnum.APPROVED.value,
                            "moderation_comment": it.get("moderation_comment") or "",
                        },
                    )

            elif resource == "questions":
                for it in items:
                    ga_id = it.get("general_assessment_id") or None
                    la_id = it.get("lesson_assessment_id") or None
                    # Enforce XOR to satisfy the DB constraint.
                    if bool(ga_id) == bool(la_id):
                        continue
                    Question.objects.update_or_create(
                        id=it.get("id"),
                        defaults={
                            "general_assessment_id": ga_id,
                            "lesson_assessment_id": la_id,
                            "type": it.get("type") or "SHORT_ANSWER",
                            "question": it.get("question") or "",
                            "answer": it.get("answer") or "",
                        },
                    )

            elif resource == "options":
                for it in items:
                    Option.objects.update_or_create(
                        id=it.get("id"),
                        defaults={
                            "question_id": it.get("question_id"),
                            "value": it.get("value") or "",
                        },
                    )

            total_items += len(items)

            # Collect downloads for this page
            download_tasks.extend(_collect_downloads(media_root=media_root, items=items))

            cursor = next_cursor
            state["cursors"][resource] = cursor
            save_state(state)

            if not cursor:
                break

        state["cursors"][resource] = None
        save_state(state)
        log(f"{resource}: synced {total_items} items")

    # Download files after DB is updated (so the portal can reference paths immediately).
    # If anything fails, we intentionally DO NOT advance last_sync so the next run retries.
    log(f"Downloading {len(download_tasks)} files (threads={DOWNLOAD_THREADS})")
    failures = download_files_parallel(download_tasks, max_workers=DOWNLOAD_THREADS)
    if failures:
        for task, err in failures[:10]:
            log(f"DOWNLOAD FAILED: {task.url} -> {task.path} ({err})")
        raise RuntimeError(f"{len(failures)} downloads failed")

    state["last_sync"] = sync_cutoff
    save_state(state)
    log("Sync complete")


# -----------------------------
# ENTRY
# -----------------------------
if __name__ == "__main__":
    try:
        sync()
    except Exception as e:
        log(f"ERROR: {e}")


# NOTE TO SELF: LOOK INTO PARALLEL DOWNLOADS WITH THREADING/ASYNCIO IF SYNC TIME IS TOO LONG: 
# from concurrent.futures import ThreadPoolExecutor
# OR USE RSYNC OVER SSH FOR MORE EFFICIENT FILE TRANSFERS IF SERVER SUPPORTS IT