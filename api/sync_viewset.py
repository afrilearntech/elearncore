from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Type

from django.db.models import Model, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from elearncore.sysutils.constants import Status as StatusEnum, UserRole
from accounts.models import County, District, School, Student, User
from content.models import (
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

from .sync_serializers import (
    SyncStudentSerializer,
    SyncStudentUserSerializer,
    SyncSubjectSerializer,
    SyncTopicSerializer,
    SyncPeriodSerializer,
    SyncLessonResourceSerializer,
    SyncGameSerializer,
    SyncGeneralAssessmentSerializer,
    SyncLessonAssessmentSerializer,
    SyncQuestionSerializer,
    SyncOptionSerializer,
    SyncCountySerializer,
    SyncDistrictSerializer,
    SyncSchoolSerializer,
)


_ALLOWED_ACCOUNT_SYNC_ROLES = {
    UserRole.ADMIN.value,
    UserRole.CONTENTCREATOR.value,
    UserRole.CONTENTVALIDATOR.value,
    UserRole.TEACHER.value,
    UserRole.HEADTEACHER.value,
}


@dataclass(frozen=True)
class _Cursor:
    updated_at: str
    id: int


def _encode_cursor(cursor: _Cursor) -> str:
    payload = {"updated_at": cursor.updated_at, "id": cursor.id}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(value: str) -> _Cursor | None:
    if not value:
        return None
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        updated_at = str(payload.get("updated_at") or "").strip()
        obj_id = int(payload.get("id"))
        if not updated_at:
            return None
        return _Cursor(updated_at=updated_at, id=obj_id)
    except Exception:
        return None


def _parse_since(value: str | None):
    if not value:
        return None
    try:
        dt = parse_datetime(str(value))
    except Exception:
        dt = None
    if dt is None:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _parse_int(value: str | None, *, default: int, min_value: int, max_value: int) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(min_value, min(max_value, parsed))


class SyncViewSet(viewsets.ViewSet):
    """Offline sync endpoints.

    Endpoints are exposed as sub-actions:
    - GET /api-v1/sync/subjects/
    - GET /api-v1/sync/counties/
    - GET /api-v1/sync/districts/
    - GET /api-v1/sync/schools/
    - GET /api-v1/sync/student-users/  (student accounts; includes password hashes)
    - GET /api-v1/sync/students/
    - GET /api-v1/sync/topics/
    - GET /api-v1/sync/periods/
    - GET /api-v1/sync/lessons/
    - GET /api-v1/sync/games/

    Query params (all optional):
    - since: ISO datetime; only items updated after this time are returned.
    - last_sync: alias for since (backwards compatibility with early clients).
    - cursor: opaque cursor for pagination within a since window.
    - limit: max number of items per page (default 500; max 2000).
    - status: for models that have a `status` field; defaults to APPROVED.

    Response:
    {
        "resource": "lessons",
        "items": [...],
        "next_cursor": "..." | null,
        "server_time": "2026-04-06T12:34:56.123456+00:00",
        "count": 123
    }
    """

    permission_classes = [permissions.IsAuthenticated]

    def _require_account_sync_role(self, request):
        user = getattr(request, "user", None)
        if not user or getattr(user, "role", None) not in _ALLOWED_ACCOUNT_SYNC_ROLES:
            return Response({"detail": "Not authorized for account sync."}, status=403)
        return None

    def _sync_list(
        self,
        *,
        request,
        resource: str,
        model: Type[Model],
        serializer_class,
        has_status: bool,
        base_queryset=None,
        since_filter=None,
    ):
        since_raw = request.query_params.get("since") or request.query_params.get("last_sync")
        since_dt = _parse_since(since_raw)

        cursor_raw = request.query_params.get("cursor")
        cursor = _decode_cursor(cursor_raw) if cursor_raw else None

        limit = _parse_int(request.query_params.get("limit"), default=500, min_value=1, max_value=2000)

        qs = base_queryset if base_queryset is not None else model.objects.all()

        if has_status:
            status_val = (request.query_params.get("status") or StatusEnum.APPROVED.value).strip()
            # Ignore invalid statuses by falling back to APPROVED
            allowed = {s.value for s in StatusEnum}
            if status_val not in allowed:
                status_val = StatusEnum.APPROVED.value
            qs = qs.filter(status=status_val)

        if since_dt is not None:
            # Inclusive cutoffs avoid edge cases where the client stores a
            # server-provided cutoff timestamp and an update happens at the
            # exact same timestamp.
            if since_filter is not None:
                qs = qs.filter(since_filter(since_dt))
            else:
                qs = qs.filter(updated_at__gte=since_dt)

        qs = qs.order_by("updated_at", "id")

        if cursor is not None:
            cursor_dt = _parse_since(cursor.updated_at)
            if cursor_dt is not None:
                qs = qs.filter(Q(updated_at__gt=cursor_dt) | Q(updated_at=cursor_dt, id__gt=cursor.id))

        # Grab one extra to know if there's another page
        rows = list(qs[: limit + 1])
        has_more = len(rows) > limit
        rows = rows[:limit]

        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = _encode_cursor(_Cursor(updated_at=last.updated_at.isoformat(), id=last.id))

        payload = {
            "resource": resource,
            "items": serializer_class(rows, many=True, context={"request": request}).data,
            "next_cursor": next_cursor,
            "server_time": timezone.now().isoformat(),
            "count": len(rows),
        }
        return Response(payload)

    @action(detail=False, methods=["get"], url_path="subjects")
    def subjects(self, request):
        return self._sync_list(
            request=request,
            resource="subjects",
            model=Subject,
            serializer_class=SyncSubjectSerializer,
            has_status=True,
            base_queryset=Subject.objects.all(),
        )

    @action(detail=False, methods=["get"], url_path="counties")
    def counties(self, request):
        return self._sync_list(
            request=request,
            resource="counties",
            model=County,
            serializer_class=SyncCountySerializer,
            has_status=True,
            base_queryset=County.objects.all(),
        )

    @action(detail=False, methods=["get"], url_path="districts")
    def districts(self, request):
        return self._sync_list(
            request=request,
            resource="districts",
            model=District,
            serializer_class=SyncDistrictSerializer,
            has_status=True,
            base_queryset=District.objects.select_related("county").all(),
        )

    @action(detail=False, methods=["get"], url_path="schools")
    def schools(self, request):
        return self._sync_list(
            request=request,
            resource="schools",
            model=School,
            serializer_class=SyncSchoolSerializer,
            has_status=True,
            base_queryset=School.objects.select_related("district").all(),
        )

    @action(detail=False, methods=["get"], url_path="student-users")
    def student_users(self, request):
        deny = self._require_account_sync_role(request)
        if deny:
            return deny

        return self._sync_list(
            request=request,
            resource="student_users",
            model=User,
            serializer_class=SyncStudentUserSerializer,
            has_status=False,
            base_queryset=User.objects.filter(role=UserRole.STUDENT.value),
        )

    @action(detail=False, methods=["get"], url_path="students")
    def students(self, request):
        deny = self._require_account_sync_role(request)
        if deny:
            return deny

        return self._sync_list(
            request=request,
            resource="students",
            model=Student,
            serializer_class=SyncStudentSerializer,
            has_status=False,
            base_queryset=Student.objects.select_related("profile", "school").filter(
                profile__role=UserRole.STUDENT.value
            ),
        )

    @action(detail=False, methods=["get"], url_path="topics")
    def topics(self, request):
        return self._sync_list(
            request=request,
            resource="topics",
            model=Topic,
            serializer_class=SyncTopicSerializer,
            has_status=False,
            base_queryset=Topic.objects.select_related("subject").filter(
                subject__status=StatusEnum.APPROVED.value
            ),
        )

    @action(detail=False, methods=["get"], url_path="periods")
    def periods(self, request):
        return self._sync_list(
            request=request,
            resource="periods",
            model=Period,
            serializer_class=SyncPeriodSerializer,
            has_status=False,
            base_queryset=Period.objects.all(),
        )

    @action(detail=False, methods=["get"], url_path="lessons")
    def lessons(self, request):
        return self._sync_list(
            request=request,
            resource="lessons",
            model=LessonResource,
            serializer_class=SyncLessonResourceSerializer,
            has_status=True,
            base_queryset=LessonResource.objects.select_related("subject", "topic", "period").filter(
                subject__status=StatusEnum.APPROVED.value
            ),
        )

    @action(detail=False, methods=["get"], url_path="games")
    def games(self, request):
        return self._sync_list(
            request=request,
            resource="games",
            model=GameModel,
            serializer_class=SyncGameSerializer,
            has_status=True,
            base_queryset=GameModel.objects.all(),
        )

    @action(detail=False, methods=["get"], url_path="general-assessments")
    def general_assessments(self, request):
        return self._sync_list(
            request=request,
            resource="general_assessments",
            model=GeneralAssessment,
            serializer_class=SyncGeneralAssessmentSerializer,
            has_status=True,
            base_queryset=GeneralAssessment.objects.all(),
        )

    @action(detail=False, methods=["get"], url_path="lesson-assessments")
    def lesson_assessments(self, request):
        return self._sync_list(
            request=request,
            resource="lesson_assessments",
            model=LessonAssessment,
            serializer_class=SyncLessonAssessmentSerializer,
            has_status=True,
            base_queryset=LessonAssessment.objects.select_related("lesson").filter(
                lesson__status=StatusEnum.APPROVED.value
            ),
        )

    @action(detail=False, methods=["get"], url_path="questions")
    def questions(self, request):
        approved = StatusEnum.APPROVED.value
        return self._sync_list(
            request=request,
            resource="questions",
            model=Question,
            serializer_class=SyncQuestionSerializer,
            has_status=False,
            base_queryset=Question.objects.select_related("general_assessment", "lesson_assessment").filter(
                Q(general_assessment__status=approved) | Q(lesson_assessment__status=approved)
            ),
            since_filter=lambda since_dt: (
                Q(updated_at__gte=since_dt)
                | Q(general_assessment__updated_at__gte=since_dt)
                | Q(lesson_assessment__updated_at__gte=since_dt)
            ),
        )

    @action(detail=False, methods=["get"], url_path="options")
    def options(self, request):
        approved = StatusEnum.APPROVED.value
        return self._sync_list(
            request=request,
            resource="options",
            model=Option,
            serializer_class=SyncOptionSerializer,
            has_status=False,
            base_queryset=Option.objects.select_related(
                "question",
                "question__general_assessment",
                "question__lesson_assessment",
            ).filter(
                Q(question__general_assessment__status=approved)
                | Q(question__lesson_assessment__status=approved)
            ),
            since_filter=lambda since_dt: (
                Q(updated_at__gte=since_dt)
                | Q(question__updated_at__gte=since_dt)
                | Q(question__general_assessment__updated_at__gte=since_dt)
                | Q(question__lesson_assessment__updated_at__gte=since_dt)
            ),
        )
