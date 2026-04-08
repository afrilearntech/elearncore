import os
import json
import hashlib
import base64
from dataclasses import dataclass
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

from elearncore.sysutils.constants import Status as StatusEnum, UserRole

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
# NOTE: Offline boxes may have slow/unstable links; a 30s read timeout is often
# too aggressive for large sync pages.
REQUEST_TIMEOUT = float(os.getenv("SYNC_TIMEOUT", "120"))
VERIFY_SSL = (os.getenv("SYNC_VERIFY_SSL", "true").strip().lower() not in {"0", "false", "no"})
PAGE_LIMIT = int(os.getenv("SYNC_PAGE_LIMIT", "500"))
UPSYNC_BATCH_SIZE = int(os.getenv("SYNC_UPSYNC_BATCH_SIZE", "200"))
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


def _request_multipart_json(
    session: requests.Session,
    *,
    method: str,
    url: str,
    data: dict[str, Any],
    file_field_name: str,
    file_path: str,
    state: dict,
    retry_on_unauthorized: bool = True,
) -> dict[str, Any]:
    """Make a multipart request (with a single file) and return JSON.

    Includes the same single auth-refresh retry behavior as `_request_json`.
    """

    def _do_request() -> requests.Response:
        with open(file_path, "rb") as f:
            files = {file_field_name: f}
            return session.request(
                method,
                url,
                data=data,
                files=files,
                timeout=REQUEST_TIMEOUT,
                verify=VERIFY_SSL,
            )

    res = _do_request()
    if retry_on_unauthorized and res.status_code == 401 and (SYNC_LOGIN_IDENTIFIER and SYNC_LOGIN_PASSWORD):
        log("Token expired/unauthorized; re-authenticating...")
        try:
            token = _login_and_get_token(session)
            _set_session_token(session, token)
            _store_token(state, token)
            save_state(state)
        except Exception as e:
            raise RuntimeError(f"Re-authentication failed: {e}") from e

        res = _do_request()

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
            # Size wasn't provided by the API. Do a best-effort HEAD check to
            # avoid re-downloading unchanged files (common with remote storage).
            try:
                head = requests.head(
                    url,
                    allow_redirects=True,
                    timeout=REQUEST_TIMEOUT,
                    verify=VERIFY_SSL,
                )
                if head.status_code == 200:
                    cl = head.headers.get("Content-Length")
                    if cl and path.stat().st_size == int(cl):
                        return
            except Exception:
                # If HEAD fails (not supported, auth, etc), fall back to download.
                pass
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
    resource_endpoint: str,
    since: str | None,
    cursor: str | None,
) -> dict[str, Any]:
    url = f"{API_BASE_URL}/sync/{resource_endpoint}/"
    limit = max(1, int(PAGE_LIMIT))

    # Adaptive retry: if the server takes too long to respond for large pages,
    # reduce the page size and retry a few times.
    for attempt in range(1, 5):
        params: dict[str, Any] = {"limit": limit}
        if since:
            params["since"] = since
        if cursor:
            params["cursor"] = cursor

        try:
            return _request_json(session, method="GET", url=url, params=params, state=state)
        except requests.exceptions.ReadTimeout as e:
            if attempt >= 4:
                raise
            if limit <= 50:
                raise
            limit = max(50, limit // 2)
            log(f"Read timeout syncing {resource_endpoint}; retrying with limit={limit} ({e})")


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


def perform_upsync(*, session: requests.Session, state: dict, media_root: Path) -> None:
    """Push locally-created usage data from the offline box to central."""

    if not API_BASE_URL:
        raise RuntimeError("SYNC_API_BASE_URL is required (must include /api-v1).")

    from django.db.models import Q  # noqa: WPS433
    from django.utils.dateparse import parse_datetime  # noqa: WPS433
    from django.utils import timezone  # noqa: WPS433
    from django.core.exceptions import ValidationError  # noqa: WPS433
    from django.core.validators import validate_email  # noqa: WPS433

    from accounts.models import Student, User  # noqa: WPS433
    from content.models import (  # noqa: WPS433
        AssessmentSolution,
        GamePlay,
        GeneralAssessmentGrade,
        LessonAssessmentGrade,
        LessonAssessmentSolution,
        TakeLesson,
    )

    def _parse_dt(value):
        if value in (None, ""):
            return None
        dt = parse_datetime(str(value))
        if dt is None:
            return None
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone=timezone.utc)
        return dt

    def _dt_iso(dt):
        if dt is None:
            return None
        try:
            return dt.isoformat()
        except Exception:
            return str(dt)

    def _clean_email(value: Any) -> str | None:
        if value in (None, ""):
            return None
        try:
            candidate = str(value).strip()
        except Exception:
            return None
        if not candidate:
            return None
        try:
            validate_email(candidate)
        except ValidationError:
            return None
        return candidate

    def _post_batch(endpoint: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        url = f"{API_BASE_URL}/upsync/{endpoint}/"
        return _request_json(session, method="POST", url=url, json={"items": items}, state=state)

    def _upload_attachment(*, endpoint: str, data: dict[str, Any], file_path: Path) -> dict[str, Any]:
        url = f"{API_BASE_URL}/upsync/{endpoint}/"
        return _request_multipart_json(
            session,
            method="POST",
            url=url,
            data=data,
            file_field_name="attachment",
            file_path=str(file_path),
            state=state,
        )

    upsync = state.setdefault("upsync", {})
    cursors: dict[str, str | None] = upsync.setdefault("cursors", {})
    attachment_hashes: dict[str, str] = upsync.setdefault("attachment_hashes", {})

    cutoff_dt = timezone.now()
    cutoff_iso = cutoff_dt.isoformat()
    batch_size = max(1, int(UPSYNC_BATCH_SIZE))

    def _cursor_dt(resource: str):
        return _parse_dt(cursors.get(resource))

    def _window_filter(*, qs, cursor_dt):
        if cursor_dt is not None:
            qs = qs.filter(updated_at__gte=cursor_dt)
        return qs.filter(updated_at__lt=cutoff_dt)

    def _advance(resource: str) -> None:
        cursors[resource] = cutoff_iso
        save_state(state)

    log(f"Upsync cutoff: {cutoff_iso}")

    # --- students (identity prerequisite)
    students_resource = "students"
    student_cursor = _cursor_dt(students_resource)
    student_qs = Student.objects.select_related("profile").filter(profile__role=UserRole.STUDENT.value)
    if student_cursor is not None:
        student_qs = student_qs.filter(Q(updated_at__gte=student_cursor) | Q(profile__updated_at__gte=student_cursor))
    # Freeze a consistent window.
    student_qs = student_qs.filter(updated_at__lt=cutoff_dt, profile__updated_at__lt=cutoff_dt).order_by("updated_at", "id")

    student_errors = 0
    batch: list[dict[str, Any]] = []
    try:
        for s in student_qs.iterator(chunk_size=batch_size):
            u = getattr(s, "profile", None)
            if not u:
                continue
            if not getattr(u, "sync_uuid", None):
                continue

            phone = str(getattr(u, "phone", "") or "").strip()
            name = str(getattr(u, "name", "") or "").strip()
            if not phone or not name:
                continue

            # Central serializer enforces valid email syntax. Some boxes may have
            # legacy/placeholder values; treat email as best-effort.
            cleaned_email = _clean_email(getattr(u, "email", None))
            if cleaned_email is None and getattr(u, "email", None):
                log(f"Upsync students: dropping invalid email for {u.phone}")

            dob_val = getattr(u, "dob", None)
            try:
                dob_str = dob_val.isoformat() if dob_val is not None else None
            except Exception:
                dob_str = None

            batch.append(
                {
                    "sync_uuid": str(u.sync_uuid),
                    "phone": phone,
                    "name": name,
                    "email": cleaned_email,
                    "dob": dob_str,
                    "gender": getattr(u, "gender", None) or None,
                    "grade": getattr(s, "grade", None) or None,
                    "school_id": getattr(s, "school_id", None),
                }
            )
            if len(batch) >= batch_size:
                resp = _post_batch("students", batch)
                student_errors += int(resp.get("errors") or 0)
                # Apply canonical UUID mappings.
                for r in (resp.get("results") or []):
                    if not isinstance(r, dict) or r.get("status") != "ok":
                        continue
                    c_uuid = r.get("client_sync_uuid")
                    s_uuid = r.get("server_sync_uuid")
                    if c_uuid and s_uuid and c_uuid != s_uuid:
                        try:
                            user = User.objects.filter(sync_uuid=c_uuid).first()
                            if user is not None:
                                user.sync_uuid = s_uuid
                                user.save(update_fields=["sync_uuid", "updated_at"])
                        except Exception:
                            # Best-effort; if this fails, the next run will retry.
                            pass
                batch = []

        if batch:
            resp = _post_batch("students", batch)
            student_errors += int(resp.get("errors") or 0)
            for r in (resp.get("results") or []):
                if not isinstance(r, dict) or r.get("status") != "ok":
                    continue
                c_uuid = r.get("client_sync_uuid")
                s_uuid = r.get("server_sync_uuid")
                if c_uuid and s_uuid and c_uuid != s_uuid:
                    try:
                        user = User.objects.filter(sync_uuid=c_uuid).first()
                        if user is not None:
                            user.sync_uuid = s_uuid
                            user.save(update_fields=["sync_uuid", "updated_at"])
                    except Exception:
                        pass

    except requests.exceptions.HTTPError as e:
        # Backwards-compatible: allow old servers without upsync routes.
        status_code = getattr(getattr(e, "response", None), "status_code", None)
        if status_code == 404:
            log("Upsync endpoints not found (404). Skipping upsync.")
            return
        raise

    if student_errors:
        log(f"Upsync students had {student_errors} errors; not advancing cursor.")
        return

    _advance(students_resource)
    log("Upsync students: ok")

    # --- login streaks
    streak_resource = "login_streaks"
    streak_cursor_dt = _cursor_dt(streak_resource)
    streak_cursor_date = streak_cursor_dt.date() if streak_cursor_dt is not None else None
    cutoff_date = timezone.localdate()

    streak_qs = (
        Student.objects
        .select_related("profile")
        .filter(profile__role=UserRole.STUDENT.value)
        .exclude(last_login_activity_date__isnull=True)
        .filter(last_login_activity_date__lte=cutoff_date)
        .order_by("last_login_activity_date", "id")
    )
    if streak_cursor_date is not None:
        # Use >= to avoid missing same-day activity if upsync runs multiple times.
        streak_qs = streak_qs.filter(last_login_activity_date__gte=streak_cursor_date)

    streak_errors = 0
    skip_streaks = False
    batch = []
    for s in streak_qs.iterator(chunk_size=batch_size):
        u = getattr(s, "profile", None)
        if not u or not getattr(u, "sync_uuid", None):
            continue
        last_day = getattr(s, "last_login_activity_date", None)
        if not last_day:
            continue
        batch.append(
            {
                "student_sync_uuid": str(u.sync_uuid),
                "last_login_activity_date": str(last_day),
                "current_login_streak": int(getattr(s, "current_login_streak", 0) or 0),
                "max_login_streak": int(getattr(s, "max_login_streak", 0) or 0),
            }
        )
        if len(batch) >= batch_size:
            try:
                resp = _post_batch("login-streaks", batch)
            except requests.exceptions.HTTPError as e:
                status_code = getattr(getattr(e, "response", None), "status_code", None)
                if status_code == 404:
                    log("Upsync login-streaks endpoint not found (404). Skipping login streak upsync.")
                    skip_streaks = True
                    batch = []
                    break
                raise
            streak_errors += int(resp.get("errors") or 0)
            batch = []

    if (not skip_streaks) and batch:
        try:
            resp = _post_batch("login-streaks", batch)
        except requests.exceptions.HTTPError as e:
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            if status_code == 404:
                log("Upsync login-streaks endpoint not found (404). Skipping login streak upsync.")
                skip_streaks = True
                resp = None
            else:
                raise
        if resp is not None:
            streak_errors += int(resp.get("errors") or 0)

    if skip_streaks:
        pass
    elif streak_errors:
        log(f"Upsync login_streaks had {streak_errors} errors; not advancing cursor.")
    else:
        _advance(streak_resource)
        log("Upsync login_streaks: ok")

    # --- taken lessons
    take_resource = "taken_lessons"
    take_cursor = _cursor_dt(take_resource)
    take_qs = TakeLesson.objects.select_related("student__profile").all()
    take_qs = _window_filter(qs=take_qs, cursor_dt=take_cursor).order_by("updated_at", "id")

    take_errors = 0
    batch = []
    for tl in take_qs.iterator(chunk_size=batch_size):
        stu = getattr(tl, "student", None)
        u = getattr(stu, "profile", None) if stu is not None else None
        if not u or not getattr(u, "sync_uuid", None):
            continue
        batch.append(
            {
                "student_sync_uuid": str(u.sync_uuid),
                "lesson_id": getattr(tl, "lesson_id", None),
                "occurred_at": _dt_iso(getattr(tl, "created_at", None)),
            }
        )
        if len(batch) >= batch_size:
            resp = _post_batch("taken-lessons", batch)
            take_errors += int(resp.get("errors") or 0)
            batch = []
    if batch:
        resp = _post_batch("taken-lessons", batch)
        take_errors += int(resp.get("errors") or 0)

    if take_errors:
        log(f"Upsync taken_lessons had {take_errors} errors; not advancing cursor.")
    else:
        _advance(take_resource)
        log("Upsync taken_lessons: ok")

    # --- gameplays
    gp_resource = "gameplays"
    gp_cursor = _cursor_dt(gp_resource)
    gp_qs = GamePlay.objects.select_related("student__profile").all()
    gp_qs = _window_filter(qs=gp_qs, cursor_dt=gp_cursor).order_by("updated_at", "id")

    gp_errors = 0
    skip_gameplays = False
    batch = []
    for gp in gp_qs.iterator(chunk_size=batch_size):
        stu = getattr(gp, "student", None)
        u = getattr(stu, "profile", None) if stu is not None else None
        if not u or not getattr(u, "sync_uuid", None):
            continue
        batch.append(
            {
                "student_sync_uuid": str(u.sync_uuid),
                "game_id": getattr(gp, "game_id", None),
                "last_played_at": _dt_iso(getattr(gp, "last_played_at", None)),
            }
        )
        if len(batch) >= batch_size:
            try:
                resp = _post_batch("gameplays", batch)
            except requests.exceptions.HTTPError as e:
                status_code = getattr(getattr(e, "response", None), "status_code", None)
                if status_code == 404:
                    log("Upsync gameplays endpoint not found (404). Skipping gameplays upsync.")
                    skip_gameplays = True
                    batch = []
                    break
                raise
            gp_errors += int(resp.get("errors") or 0)
            batch = []

    if (not skip_gameplays) and batch:
        try:
            resp = _post_batch("gameplays", batch)
        except requests.exceptions.HTTPError as e:
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            if status_code == 404:
                log("Upsync gameplays endpoint not found (404). Skipping gameplays upsync.")
                skip_gameplays = True
                resp = None
            else:
                raise
        if resp is not None:
            gp_errors += int(resp.get("errors") or 0)

    if skip_gameplays:
        pass
    elif gp_errors:
        log(f"Upsync gameplays had {gp_errors} errors; not advancing cursor.")
    else:
        _advance(gp_resource)
        log("Upsync gameplays: ok")

    # --- general assessment solutions + attachments
    ga_sol_resource = "general_assessment_solutions"
    ga_sol_cursor = _cursor_dt(ga_sol_resource)
    ga_sol_qs = AssessmentSolution.objects.select_related("student__profile").all()
    ga_sol_qs = _window_filter(qs=ga_sol_qs, cursor_dt=ga_sol_cursor).order_by("updated_at", "id")

    ga_sol_errors = 0
    batch_items: list[dict[str, Any]] = []
    batch_objs: list[AssessmentSolution] = []
    for sol in ga_sol_qs.iterator(chunk_size=batch_size):
        stu = getattr(sol, "student", None)
        u = getattr(stu, "profile", None) if stu is not None else None
        if not u or not getattr(u, "sync_uuid", None):
            continue
        batch_objs.append(sol)
        batch_items.append(
            {
                "student_sync_uuid": str(u.sync_uuid),
                "assessment_id": getattr(sol, "assessment_id", None),
                "solution": getattr(sol, "solution", "") or "",
                "submitted_at": _dt_iso(getattr(sol, "submitted_at", None)),
            }
        )
        if len(batch_items) >= batch_size:
            resp = _post_batch("general-assessment-solutions", batch_items)
            ga_sol_errors += int(resp.get("errors") or 0)
            # Attachments (best effort but blocks cursor advance on HTTP failures).
            for obj in batch_objs:
                try:
                    name = getattr(getattr(obj, "attachment", None), "name", "") or ""
                    if not name:
                        continue
                    file_path = _safe_local_path(media_root, name)
                    if not file_path.exists():
                        log(f"WARN: missing attachment file: {file_path}")
                        continue
                    h = file_hash(str(file_path)) or ""
                    key = f"ga:{obj.assessment_id}:{obj.student_id}"
                    if h and attachment_hashes.get(key) == h:
                        continue
                    stu = getattr(obj, "student", None)
                    u = getattr(stu, "profile", None) if stu is not None else None
                    if not u or not getattr(u, "sync_uuid", None):
                        continue
                    _upload_attachment(
                        endpoint="general-assessment-solutions/attachment",
                        data={
                            "student_sync_uuid": str(u.sync_uuid),
                            "assessment_id": str(getattr(obj, "assessment_id", "")),
                        },
                        file_path=file_path,
                    )
                    if h:
                        attachment_hashes[key] = h
                    save_state(state)
                except requests.exceptions.HTTPError as e:
                    ga_sol_errors += 1
                    log(f"Attachment upload failed (general assessment): {e}")
                except Exception as e:
                    ga_sol_errors += 1
                    log(f"Attachment upload error (general assessment): {e}")

            batch_items = []
            batch_objs = []

    if batch_items:
        resp = _post_batch("general-assessment-solutions", batch_items)
        ga_sol_errors += int(resp.get("errors") or 0)
        for obj in batch_objs:
            try:
                name = getattr(getattr(obj, "attachment", None), "name", "") or ""
                if not name:
                    continue
                file_path = _safe_local_path(media_root, name)
                if not file_path.exists():
                    log(f"WARN: missing attachment file: {file_path}")
                    continue
                h = file_hash(str(file_path)) or ""
                key = f"ga:{obj.assessment_id}:{obj.student_id}"
                if h and attachment_hashes.get(key) == h:
                    continue
                stu = getattr(obj, "student", None)
                u = getattr(stu, "profile", None) if stu is not None else None
                if not u or not getattr(u, "sync_uuid", None):
                    continue
                _upload_attachment(
                    endpoint="general-assessment-solutions/attachment",
                    data={
                        "student_sync_uuid": str(u.sync_uuid),
                        "assessment_id": str(getattr(obj, "assessment_id", "")),
                    },
                    file_path=file_path,
                )
                if h:
                    attachment_hashes[key] = h
                save_state(state)
            except requests.exceptions.HTTPError as e:
                ga_sol_errors += 1
                log(f"Attachment upload failed (general assessment): {e}")
            except Exception as e:
                ga_sol_errors += 1
                log(f"Attachment upload error (general assessment): {e}")

    if ga_sol_errors:
        log(f"Upsync general_assessment_solutions had {ga_sol_errors} errors; not advancing cursor.")
    else:
        _advance(ga_sol_resource)
        log("Upsync general_assessment_solutions: ok")

    # --- lesson assessment solutions + attachments
    la_sol_resource = "lesson_assessment_solutions"
    la_sol_cursor = _cursor_dt(la_sol_resource)
    la_sol_qs = LessonAssessmentSolution.objects.select_related("student__profile").all()
    la_sol_qs = _window_filter(qs=la_sol_qs, cursor_dt=la_sol_cursor).order_by("updated_at", "id")

    la_sol_errors = 0
    batch_items = []
    batch_objs = []
    for sol in la_sol_qs.iterator(chunk_size=batch_size):
        stu = getattr(sol, "student", None)
        u = getattr(stu, "profile", None) if stu is not None else None
        if not u or not getattr(u, "sync_uuid", None):
            continue
        batch_objs.append(sol)
        batch_items.append(
            {
                "student_sync_uuid": str(u.sync_uuid),
                "lesson_assessment_id": getattr(sol, "lesson_assessment_id", None),
                "solution": getattr(sol, "solution", "") or "",
                "submitted_at": _dt_iso(getattr(sol, "submitted_at", None)),
            }
        )
        if len(batch_items) >= batch_size:
            resp = _post_batch("lesson-assessment-solutions", batch_items)
            la_sol_errors += int(resp.get("errors") or 0)
            for obj in batch_objs:
                try:
                    name = getattr(getattr(obj, "attachment", None), "name", "") or ""
                    if not name:
                        continue
                    file_path = _safe_local_path(media_root, name)
                    if not file_path.exists():
                        log(f"WARN: missing attachment file: {file_path}")
                        continue
                    h = file_hash(str(file_path)) or ""
                    key = f"la:{obj.lesson_assessment_id}:{obj.student_id}"
                    if h and attachment_hashes.get(key) == h:
                        continue
                    stu = getattr(obj, "student", None)
                    u = getattr(stu, "profile", None) if stu is not None else None
                    if not u or not getattr(u, "sync_uuid", None):
                        continue
                    _upload_attachment(
                        endpoint="lesson-assessment-solutions/attachment",
                        data={
                            "student_sync_uuid": str(u.sync_uuid),
                            "lesson_assessment_id": str(getattr(obj, "lesson_assessment_id", "")),
                        },
                        file_path=file_path,
                    )
                    if h:
                        attachment_hashes[key] = h
                    save_state(state)
                except requests.exceptions.HTTPError as e:
                    la_sol_errors += 1
                    log(f"Attachment upload failed (lesson assessment): {e}")
                except Exception as e:
                    la_sol_errors += 1
                    log(f"Attachment upload error (lesson assessment): {e}")

            batch_items = []
            batch_objs = []

    if batch_items:
        resp = _post_batch("lesson-assessment-solutions", batch_items)
        la_sol_errors += int(resp.get("errors") or 0)
        for obj in batch_objs:
            try:
                name = getattr(getattr(obj, "attachment", None), "name", "") or ""
                if not name:
                    continue
                file_path = _safe_local_path(media_root, name)
                if not file_path.exists():
                    log(f"WARN: missing attachment file: {file_path}")
                    continue
                h = file_hash(str(file_path)) or ""
                key = f"la:{obj.lesson_assessment_id}:{obj.student_id}"
                if h and attachment_hashes.get(key) == h:
                    continue
                stu = getattr(obj, "student", None)
                u = getattr(stu, "profile", None) if stu is not None else None
                if not u or not getattr(u, "sync_uuid", None):
                    continue
                _upload_attachment(
                    endpoint="lesson-assessment-solutions/attachment",
                    data={
                        "student_sync_uuid": str(u.sync_uuid),
                        "lesson_assessment_id": str(getattr(obj, "lesson_assessment_id", "")),
                    },
                    file_path=file_path,
                )
                if h:
                    attachment_hashes[key] = h
                save_state(state)
            except requests.exceptions.HTTPError as e:
                la_sol_errors += 1
                log(f"Attachment upload failed (lesson assessment): {e}")
            except Exception as e:
                la_sol_errors += 1
                log(f"Attachment upload error (lesson assessment): {e}")

    if la_sol_errors:
        log(f"Upsync lesson_assessment_solutions had {la_sol_errors} errors; not advancing cursor.")
    else:
        _advance(la_sol_resource)
        log("Upsync lesson_assessment_solutions: ok")

    # --- grades
    ga_grade_resource = "general_assessment_grades"
    ga_grade_cursor = _cursor_dt(ga_grade_resource)
    ga_grade_qs = GeneralAssessmentGrade.objects.select_related("student__profile").all()
    ga_grade_qs = _window_filter(qs=ga_grade_qs, cursor_dt=ga_grade_cursor).order_by("updated_at", "id")

    ga_grade_errors = 0
    batch = []
    for g in ga_grade_qs.iterator(chunk_size=batch_size):
        stu = getattr(g, "student", None)
        u = getattr(stu, "profile", None) if stu is not None else None
        if not u or not getattr(u, "sync_uuid", None):
            continue
        batch.append(
            {
                "student_sync_uuid": str(u.sync_uuid),
                "assessment_id": getattr(g, "assessment_id", None),
                "score": float(getattr(g, "score", 0.0) or 0.0),
                "created_at": _dt_iso(getattr(g, "created_at", None)),
            }
        )
        if len(batch) >= batch_size:
            resp = _post_batch("general-assessment-grades", batch)
            ga_grade_errors += int(resp.get("errors") or 0)
            batch = []
    if batch:
        resp = _post_batch("general-assessment-grades", batch)
        ga_grade_errors += int(resp.get("errors") or 0)

    if ga_grade_errors:
        log(f"Upsync general_assessment_grades had {ga_grade_errors} errors; not advancing cursor.")
    else:
        _advance(ga_grade_resource)
        log("Upsync general_assessment_grades: ok")

    la_grade_resource = "lesson_assessment_grades"
    la_grade_cursor = _cursor_dt(la_grade_resource)
    la_grade_qs = LessonAssessmentGrade.objects.select_related("student__profile").all()
    la_grade_qs = _window_filter(qs=la_grade_qs, cursor_dt=la_grade_cursor).order_by("updated_at", "id")

    la_grade_errors = 0
    batch = []
    for g in la_grade_qs.iterator(chunk_size=batch_size):
        stu = getattr(g, "student", None)
        u = getattr(stu, "profile", None) if stu is not None else None
        if not u or not getattr(u, "sync_uuid", None):
            continue
        batch.append(
            {
                "student_sync_uuid": str(u.sync_uuid),
                "lesson_assessment_id": getattr(g, "lesson_assessment_id", None),
                "score": float(getattr(g, "score", 0.0) or 0.0),
                "created_at": _dt_iso(getattr(g, "created_at", None)),
            }
        )
        if len(batch) >= batch_size:
            resp = _post_batch("lesson-assessment-grades", batch)
            la_grade_errors += int(resp.get("errors") or 0)
            batch = []
    if batch:
        resp = _post_batch("lesson-assessment-grades", batch)
        la_grade_errors += int(resp.get("errors") or 0)

    if la_grade_errors:
        log(f"Upsync lesson_assessment_grades had {la_grade_errors} errors; not advancing cursor.")
    else:
        _advance(la_grade_resource)
        log("Upsync lesson_assessment_grades: ok")

    log("Upsync complete")


def sync():
    if not API_BASE_URL:
        raise RuntimeError(
            "SYNC_API_BASE_URL is not set. Example: https://elearnapi.example.com/api-v1"
        )

    state = load_state()
    state.setdefault("cursors", {})
    state.setdefault("auth", {})

    state.setdefault("upsync", {})
    state["upsync"].setdefault("cursors", {})
    state["upsync"].setdefault("attachment_hashes", {})
    last_sync = state.get("last_sync")
    last_upsync = state.get("upsync", {}).get("cursors", {}).get("students")

    log(f"Server base: {API_BASE_URL}")
    log(f"Last sync cutoff: {last_sync}")
    log(f"Last upsync cursor (students): {last_upsync}")

    # Boot local Django (offline box) and point downloads into MEDIA_ROOT.
    settings = _django_setup()
    media_root = Path(getattr(settings, "MEDIA_ROOT", DATA_DIR)).resolve()
    media_root.mkdir(parents=True, exist_ok=True)

    # Import models only after Django is ready.
    from accounts.models import County, District, School, Student, User  # noqa: WPS433
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
    from django.utils.dateparse import parse_date, parse_datetime  # noqa: WPS433
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

    def _parse_date(value):
        if value in (None, ""):
            return None
        try:
            return parse_date(str(value))
        except Exception:
            return None

    def _to_int(value):
        try:
            if value in (None, ""):
                return None
            return int(value)
        except Exception:
            return None

    session = _build_session()
    _ensure_authenticated_session(session, state)

    # Push local usage (offline) to central before pulling fresh content.
    try:
        perform_upsync(session=session, state=state, media_root=media_root)
    except Exception as e:
        # Upsync is best-effort; keep pulling fresh content/accounts even if
        # local usage payloads are temporarily invalid or the server is down.
        log(f"Upsync failed; continuing with downsync. ({e})")

    # IMPORTANT: we store the cutoff timestamp from the FIRST sync call.
    # This prevents missing updates that happen while we are mid-sync.
    sync_cutoff: str | None = None

    download_tasks: list[DownloadTask] = []

    # Sync order matters because of FK dependencies.
    # (endpoint_path, resource_key, model)
    # Endpoint paths follow the server routes (hyphenated actions).
    # Resource keys are stable internal names used for state + upserts.
    sync_plan = [
        ("counties", "counties", County),
        ("districts", "districts", District),
        ("schools", "schools", School),
        ("student-users", "student_users", User),
        ("students", "students", Student),
        ("subjects", "subjects", Subject),
        ("topics", "topics", Topic),
        ("periods", "periods", Period),
        ("lessons", "lessons", LessonResource),
        ("games", "games", GameModel),
        ("general-assessments", "general_assessments", GeneralAssessment),
        ("lesson-assessments", "lesson_assessments", LessonAssessment),
        ("questions", "questions", Question),
        ("options", "options", Option),
    ]

    for endpoint, resource, model in sync_plan:
        is_new_resource = resource not in state["cursors"]
        cursor = state["cursors"].get(resource)
        since_for_resource = None if is_new_resource else last_sync
        total_items = 0
        log(f"Syncing {resource}...")

        if is_new_resource and last_sync:
            log(f"{resource}: first-time sync; ignoring last_sync cutoff to backfill existing data")

        while True:
            payload = _fetch_page(
                session,
                state=state,
                resource_endpoint=endpoint,
                since=since_for_resource,
                cursor=cursor,
            )
            if sync_cutoff is None:
                sync_cutoff = payload.get("server_time")

            items = payload.get("items") or []
            next_cursor = payload.get("next_cursor")

            # Upsert into local DB
            if resource == "counties":
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    County.objects.update_or_create(
                        id=it.get("id"),
                        defaults={
                            "name": it.get("name") or "",
                            "status": it.get("status") or StatusEnum.APPROVED.value,
                            "moderation_comment": it.get("moderation_comment") or "",
                            "created_by": None,
                        },
                    )

            elif resource == "districts":
                county_ids = {_to_int(it.get("county_id")) for it in items if isinstance(it, dict)}
                county_ids.discard(None)
                existing_counties = set(
                    County.objects.filter(id__in=county_ids).values_list("id", flat=True)
                )

                skipped = 0
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    obj_id = _to_int(it.get("id"))
                    county_id = _to_int(it.get("county_id"))
                    if obj_id is None or county_id is None or county_id not in existing_counties:
                        skipped += 1
                        continue

                    District.objects.update_or_create(
                        id=obj_id,
                        defaults={
                            "county_id": county_id,
                            "name": it.get("name") or "",
                            "status": it.get("status") or StatusEnum.APPROVED.value,
                            "moderation_comment": it.get("moderation_comment") or "",
                        },
                    )

                if skipped:
                    log(f"districts: skipped {skipped} items (missing/invalid county)")

            elif resource == "schools":
                district_ids = {_to_int(it.get("district_id")) for it in items if isinstance(it, dict)}
                district_ids.discard(None)
                existing_districts = set(
                    District.objects.filter(id__in=district_ids).values_list("id", flat=True)
                )

                skipped = 0
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    obj_id = _to_int(it.get("id"))
                    district_id = _to_int(it.get("district_id"))
                    if obj_id is None or district_id is None or district_id not in existing_districts:
                        skipped += 1
                        continue

                    School.objects.update_or_create(
                        id=obj_id,
                        defaults={
                            "district_id": district_id,
                            "name": it.get("name") or "",
                            "status": it.get("status") or StatusEnum.APPROVED.value,
                            "moderation_comment": it.get("moderation_comment") or "",
                        },
                    )

                if skipped:
                    log(f"schools: skipped {skipped} items (missing/invalid district)")

            elif resource == "student_users":
                skipped = 0
                errors = 0

                for it in items:
                    if not isinstance(it, dict):
                        continue

                    sync_uuid = (it.get("sync_uuid") or "").strip()
                    phone = (it.get("phone") or "").strip()
                    password_hash = (it.get("password_hash") or "").strip()

                    if not sync_uuid or not phone or not password_hash:
                        skipped += 1
                        continue

                    try:
                        user = User.objects.filter(sync_uuid=sync_uuid).first()
                        if user is None:
                            # Best-effort fallback by phone (handles legacy local rows).
                            user = User.objects.filter(phone=phone).first()

                        if user is None:
                            user = User(sync_uuid=sync_uuid)
                        else:
                            if str(getattr(user, "sync_uuid", "") or "") != sync_uuid:
                                user.sync_uuid = sync_uuid

                        user.phone = phone
                        user.email = (it.get("email") or None) or None
                        user.name = (it.get("name") or "").strip() or getattr(user, "name", None) or phone
                        user.role = it.get("role") or UserRole.STUDENT.value
                        user.is_active = bool(it.get("is_active", True))
                        user.deleted = bool(it.get("deleted", False))
                        user.dob = it.get("dob") or None
                        user.gender = it.get("gender") or None

                        # Central password hash is required for fully-offline roaming login.
                        user.password = password_hash

                        # Hardening: never allow student users to become staff/superuser via sync.
                        user.is_staff = False
                        user.is_superuser = False

                        user.save()
                    except Exception as e:
                        errors += 1
                        log(f"student_users: failed upsert for {phone or sync_uuid}: {e}")

                if skipped:
                    log(f"student_users: skipped {skipped} items (missing sync_uuid/phone/password_hash)")
                if errors:
                    log(f"student_users: {errors} upsert failures")

            elif resource == "students":
                school_ids = {_to_int(it.get("school_id")) for it in items if isinstance(it, dict)}
                school_ids.discard(None)
                existing_schools = set(School.objects.filter(id__in=school_ids).values_list("id", flat=True))

                skipped = 0
                errors = 0

                for it in items:
                    if not isinstance(it, dict):
                        continue

                    profile_sync_uuid = (it.get("profile_sync_uuid") or "").strip()
                    if not profile_sync_uuid:
                        skipped += 1
                        continue

                    try:
                        user = User.objects.filter(sync_uuid=profile_sync_uuid).first()
                        if user is None:
                            skipped += 1
                            continue

                        remote_points = _to_int(it.get("points")) or 0
                        remote_current = _to_int(it.get("current_login_streak")) or 0
                        remote_max = _to_int(it.get("max_login_streak")) or 0
                        remote_last = _parse_date(it.get("last_login_activity_date"))

                        student = Student.objects.filter(profile=user).first()

                        if student is None:
                            incoming_student_id = (it.get("student_id") or "").strip() or None
                            if incoming_student_id:
                                # `student_id` is derived from the DB PK (e.g., STU0000022) and
                                # can collide across different offline boxes. Keep it best-effort.
                                if Student.objects.filter(student_id=incoming_student_id).exists():
                                    log(
                                        f"students: dropping remote student_id {incoming_student_id} "
                                        "due to local collision"
                                    )
                                    incoming_student_id = None

                            school_id = _to_int(it.get("school_id"))
                            school_id = school_id if school_id in existing_schools else None

                            student = Student.objects.create(
                                profile=user,
                                student_id=incoming_student_id,
                                school_id=school_id,
                                grade=(it.get("grade") or "") or "",
                                points=int(remote_points),
                                current_login_streak=int(remote_current),
                                max_login_streak=max(int(remote_max), int(remote_current)),
                                last_login_activity_date=remote_last,
                                status=(it.get("status") or "") or StatusEnum.APPROVED.value,
                                moderation_comment=it.get("moderation_comment") or "",
                            )
                            continue

                        # Canonical student profile fields.
                        incoming_student_id = (it.get("student_id") or "").strip() or None
                        if incoming_student_id and incoming_student_id != (getattr(student, "student_id", None) or None):
                            if Student.objects.filter(student_id=incoming_student_id).exclude(pk=student.pk).exists():
                                log(
                                    f"students: ignoring remote student_id {incoming_student_id} "
                                    "due to local collision"
                                )
                            else:
                                student.student_id = incoming_student_id

                        incoming_grade = it.get("grade")
                        if incoming_grade is not None:
                            student.grade = incoming_grade

                        incoming_school_id = _to_int(it.get("school_id"))
                        if incoming_school_id in existing_schools:
                            student.school_id = incoming_school_id
                        elif incoming_school_id is None:
                            student.school_id = None

                        incoming_status = it.get("status")
                        if incoming_status is not None:
                            student.status = incoming_status
                        student.moderation_comment = it.get("moderation_comment") or ""

                        # Merge points safely: never move backwards.
                        try:
                            student.points = max(int(getattr(student, "points", 0) or 0), int(remote_points or 0))
                        except Exception:
                            pass

                        # Merge login streaks safely: never move the last-login date backwards.
                        local_last = getattr(student, "last_login_activity_date", None)
                        local_current = int(getattr(student, "current_login_streak", 0) or 0)
                        local_max = int(getattr(student, "max_login_streak", 0) or 0)

                        if remote_last is not None:
                            if local_last is None or remote_last > local_last:
                                student.last_login_activity_date = remote_last
                                student.current_login_streak = int(remote_current)
                                student.max_login_streak = max(local_max, int(remote_max), int(remote_current))
                            elif remote_last == local_last:
                                student.current_login_streak = max(local_current, int(remote_current))
                                student.max_login_streak = max(local_max, int(remote_max), student.current_login_streak)

                        student.save()
                    except Exception as e:
                        errors += 1
                        log(f"students: failed upsert for {profile_sync_uuid}: {e}")

                if skipped:
                    log(f"students: skipped {skipped} items (missing profile_sync_uuid or user)")
                if errors:
                    log(f"students: {errors} upsert failures")

            elif resource == "subjects":
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
                subject_ids = {_to_int(it.get("subject_id")) for it in items if isinstance(it, dict)}
                subject_ids.discard(None)
                existing_subjects = set(
                    Subject.objects.filter(id__in=subject_ids).values_list("id", flat=True)
                )

                skipped = 0
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    obj_id = _to_int(it.get("id"))
                    subject_id = _to_int(it.get("subject_id"))
                    if obj_id is None or subject_id is None or subject_id not in existing_subjects:
                        skipped += 1
                        continue
                    Topic.objects.update_or_create(
                        id=obj_id,
                        defaults={
                            "subject_id": subject_id,
                            "name": it.get("name") or "",
                        },
                    )
                if skipped:
                    log(f"topics: skipped {skipped} items (missing/invalid subject)")

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
                subject_ids = {_to_int(it.get("subject_id")) for it in items if isinstance(it, dict)}
                subject_ids.discard(None)
                existing_subjects = set(
                    Subject.objects.filter(id__in=subject_ids).values_list("id", flat=True)
                )

                topic_ids = {_to_int(it.get("topic_id")) for it in items if isinstance(it, dict)}
                topic_ids.discard(None)
                existing_topics = set(
                    Topic.objects.filter(id__in=topic_ids).values_list("id", flat=True)
                )

                period_ids = {_to_int(it.get("period_id")) for it in items if isinstance(it, dict)}
                period_ids.discard(None)
                existing_periods = set(
                    Period.objects.filter(id__in=period_ids).values_list("id", flat=True)
                )

                skipped = 0
                for it in items:
                    if not isinstance(it, dict):
                        continue

                    obj_id = _to_int(it.get("id"))
                    subject_id = _to_int(it.get("subject_id"))
                    if obj_id is None or subject_id is None or subject_id not in existing_subjects:
                        skipped += 1
                        continue

                    topic_id = _to_int(it.get("topic_id"))
                    if topic_id is not None and topic_id not in existing_topics:
                        topic_id = None

                    period_id = _to_int(it.get("period_id"))
                    if period_id is not None and period_id not in existing_periods:
                        period_id = None

                    res_file = it.get("resource") or {}
                    thumb = it.get("thumbnail") or {}

                    LessonResource.objects.update_or_create(
                        id=obj_id,
                        defaults={
                            "subject_id": subject_id,
                            "topic_id": topic_id,
                            "period_id": period_id,
                            "instructor_name": it.get("instructor_name") or "",
                            "title": it.get("title") or "",
                            "description": it.get("description") or "",
                            "type": it.get("type") or "VIDEO",
                            "status": it.get("status") or StatusEnum.APPROVED.value,
                            "duration_minutes": it.get("duration_minutes"),
                            "moderation_comment": it.get("moderation_comment") or "",
                            "resource": (res_file.get("path") if isinstance(res_file, dict) else None) or "",
                            "thumbnail": (thumb.get("path") if isinstance(thumb, dict) else None) or None,
                            "created_by": None,
                        },
                    )
                if skipped:
                    log(f"lessons: skipped {skipped} items (missing/invalid subject)")

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
                lesson_ids = {_to_int(it.get("lesson_id")) for it in items if isinstance(it, dict)}
                lesson_ids.discard(None)
                existing_lessons = set(
                    LessonResource.objects.filter(id__in=lesson_ids).values_list("id", flat=True)
                )

                skipped = 0
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    obj_id = _to_int(it.get("id"))
                    lesson_id = _to_int(it.get("lesson_id"))
                    if obj_id is None or lesson_id is None or lesson_id not in existing_lessons:
                        skipped += 1
                        continue

                    LessonAssessment.objects.update_or_create(
                        id=obj_id,
                        defaults={
                            "lesson_id": lesson_id,
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

                if skipped:
                    log(f"lesson_assessments: skipped {skipped} items (missing/invalid lesson)")

            elif resource == "questions":
                ga_ids = {_to_int(it.get("general_assessment_id")) for it in items if isinstance(it, dict)}
                ga_ids.discard(None)
                existing_ga = set(
                    GeneralAssessment.objects.filter(id__in=ga_ids).values_list("id", flat=True)
                )

                la_ids = {_to_int(it.get("lesson_assessment_id")) for it in items if isinstance(it, dict)}
                la_ids.discard(None)
                existing_la = set(
                    LessonAssessment.objects.filter(id__in=la_ids).values_list("id", flat=True)
                )

                skipped = 0
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    obj_id = _to_int(it.get("id"))
                    ga_id = _to_int(it.get("general_assessment_id"))
                    la_id = _to_int(it.get("lesson_assessment_id"))

                    # Enforce XOR to satisfy the DB constraint.
                    if obj_id is None or bool(ga_id) == bool(la_id):
                        skipped += 1
                        continue

                    # Enforce parent existence to satisfy FK constraints.
                    if ga_id is not None and ga_id not in existing_ga:
                        skipped += 1
                        continue
                    if la_id is not None and la_id not in existing_la:
                        skipped += 1
                        continue

                    Question.objects.update_or_create(
                        id=obj_id,
                        defaults={
                            "general_assessment_id": ga_id,
                            "lesson_assessment_id": la_id,
                            "type": it.get("type") or "SHORT_ANSWER",
                            "question": it.get("question") or "",
                            "answer": it.get("answer") or "",
                        },
                    )

                if skipped:
                    log(f"questions: skipped {skipped} items (missing/invalid parent assessment)")

            elif resource == "options":
                q_ids = {_to_int(it.get("question_id")) for it in items if isinstance(it, dict)}
                q_ids.discard(None)
                existing_questions = set(
                    Question.objects.filter(id__in=q_ids).values_list("id", flat=True)
                )

                skipped = 0
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    obj_id = _to_int(it.get("id"))
                    question_id = _to_int(it.get("question_id"))
                    if obj_id is None or question_id is None or question_id not in existing_questions:
                        skipped += 1
                        continue
                    Option.objects.update_or_create(
                        id=obj_id,
                        defaults={
                            "question_id": question_id,
                            "value": it.get("value") or "",
                        },
                    )

                if skipped:
                    log(f"options: skipped {skipped} items (missing/invalid question)")

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
    except requests.exceptions.HTTPError as e:
        resp = getattr(e, "response", None)
        if resp is not None:
            try:
                body = resp.text
            except Exception:
                body = None
            log(f"HTTP {getattr(resp, 'status_code', None)} for {getattr(resp, 'url', '')}")
            if body:
                body = str(body).strip()
                if len(body) > 4000:
                    body = body[:4000] + "... (truncated)"
                log(f"Response body: {body}")
        log(f"ERROR: {e}")
    except Exception as e:
        log(f"ERROR: {e}")


# NOTE TO SELF: LOOK INTO PARALLEL DOWNLOADS WITH THREADING/ASYNCIO IF SYNC TIME IS TOO LONG: 
# from concurrent.futures import ThreadPoolExecutor
# OR USE RSYNC OVER SSH FOR MORE EFFICIENT FILE TRANSFERS IF SERVER SUPPORTS IT