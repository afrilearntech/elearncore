from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.models import School, Student, User
from content.models import (
    AssessmentSolution,
    GameModel,
    GamePlay,
    GeneralAssessment,
    GeneralAssessmentGrade,
    LessonAssessment,
    LessonAssessmentGrade,
    LessonAssessmentSolution,
    LessonResource,
    TakeLesson,
)
from elearncore.sysutils.constants import (
    ASSESSMENT_SUBMISSION_POINTS,
    ContentType as ContentTypeEnum,
    GAME_PLAY_POINTS,
    Status as StatusEnum,
    UserRole,
    VIDEO_WATCH_POINTS,
)

from .upsync_serializers import (
    UpSyncGamePlaysPayloadSerializer,
    UpSyncGeneralAssessmentGradesPayloadSerializer,
    UpSyncGeneralAssessmentSolutionsPayloadSerializer,
    UpSyncLoginStreaksPayloadSerializer,
    UpSyncLessonAssessmentGradesPayloadSerializer,
    UpSyncLessonAssessmentSolutionsPayloadSerializer,
    UpSyncSolutionAttachmentSerializer,
    UpSyncStudentsPayloadSerializer,
    UpSyncTakeLessonsPayloadSerializer,
)


_ALLOWED_UPSYNC_ROLES = {
    UserRole.ADMIN.value,
    UserRole.CONTENTCREATOR.value,
    UserRole.CONTENTVALIDATOR.value,
    UserRole.TEACHER.value,
    UserRole.HEADTEACHER.value,
}


def _award_student_points(student: Student | None, points: int) -> int | None:
    if student is None or points <= 0:
        return None
    Student.objects.filter(pk=student.pk).update(points=F("points") + points)
    student.points = int(getattr(student, "points", 0) or 0) + points
    return student.points


@dataclass(frozen=True)
class _UpSyncResult:
    created: int = 0
    updated: int = 0
    mapped: int = 0
    errors: int = 0


class UpSyncViewSet(viewsets.ViewSet):
    """Offline upsync endpoints (box -> central).

    All endpoints are idempotent and accept batch payloads.

    Routes (actions):
    - POST /api-v1/upsync/students/
    - POST /api-v1/upsync/taken-lessons/
    - POST /api-v1/upsync/general-assessment-solutions/
    - POST /api-v1/upsync/lesson-assessment-solutions/
    - POST /api-v1/upsync/general-assessment-solutions/attachment/
    - POST /api-v1/upsync/lesson-assessment-solutions/attachment/
    - POST /api-v1/upsync/general-assessment-grades/
    - POST /api-v1/upsync/lesson-assessment-grades/
    """

    permission_classes = [permissions.IsAuthenticated]

    def _require_upsync_role(self, request):
        user = getattr(request, "user", None)
        if not user or getattr(user, "role", None) not in _ALLOWED_UPSYNC_ROLES:
            return Response({"detail": "Not authorized for upsync."}, status=403)
        return None

    @action(detail=False, methods=["post"], url_path="students")
    def students(self, request):
        deny = self._require_upsync_role(request)
        if deny:
            return deny

        ser = UpSyncStudentsPayloadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        items = ser.validated_data["items"]

        results: list[dict] = []
        created = updated = mapped = errors = 0

        for it in items:
            client_uuid = it.get("sync_uuid")
            phone = (it.get("phone") or "").strip()
            name = (it.get("name") or "").strip()
            email = (it.get("email") or "").strip() or None
            dob = it.get("dob")
            gender = (it.get("gender") or "").strip() or None
            grade = (it.get("grade") or "").strip() or None
            school_id = it.get("school_id")

            try:
                with transaction.atomic():
                    user = User.objects.filter(sync_uuid=client_uuid).first()
                    canonical_uuid = None

                    if user is None and phone:
                        by_phone = User.objects.filter(phone=phone).first()
                        if by_phone is not None:
                            # Central is canonical; map the client UUID to the server UUID.
                            user = by_phone
                            canonical_uuid = user.sync_uuid
                            mapped += 1

                    if user is None:
                        # Create brand-new student account.
                        user = User(
                            sync_uuid=client_uuid,
                            name=name,
                            phone=phone,
                            email=email,
                            role=UserRole.STUDENT.value,
                            dob=dob,
                            gender=gender,
                        )
                        user.set_password("password123")
                        user.save()
                        canonical_uuid = user.sync_uuid
                        created += 1
                    else:
                        canonical_uuid = user.sync_uuid

                        # Guard against attaching offline student payloads to a non-student account.
                        if getattr(user, "role", None) != UserRole.STUDENT.value:
                            raise ValueError("phone belongs to a non-student account")

                        # Best-effort updates (do not overwrite with blanks).
                        update_fields = []
                        if name and user.name != name:
                            user.name = name
                            update_fields.append("name")
                        if email and user.email != email:
                            user.email = email
                            update_fields.append("email")
                        if dob and getattr(user, "dob", None) != dob:
                            user.dob = dob
                            update_fields.append("dob")
                        if gender and getattr(user, "gender", None) != gender:
                            user.gender = gender
                            update_fields.append("gender")

                        if update_fields:
                            update_fields.append("updated_at")
                            user.save(update_fields=update_fields)
                            updated += 1

                    # Ensure student profile exists and is linked.
                    student = getattr(user, "student", None)
                    if student is None:
                        school = None
                        if school_id is not None:
                            school = School.objects.filter(pk=school_id).first()

                        student_kwargs = {
                            "profile": user,
                            "status": StatusEnum.APPROVED.value,
                        }
                        if school is not None:
                            student_kwargs["school"] = school
                        if grade:
                            student_kwargs["grade"] = grade

                        student = Student.objects.create(**student_kwargs)
                    else:
                        # Best-effort student updates.
                        student_update_fields = []
                        if grade and getattr(student, "grade", None) != grade:
                            student.grade = grade
                            student_update_fields.append("grade")
                        if school_id is not None and getattr(student, "school_id", None) != school_id:
                            school = School.objects.filter(pk=school_id).first()
                            if school is not None:
                                student.school = school
                                student_update_fields.append("school")
                        if student_update_fields:
                            student_update_fields.append("updated_at")
                            student.save(update_fields=student_update_fields)

                results.append(
                    {
                        "status": "ok",
                        "phone": phone,
                        "client_sync_uuid": str(client_uuid) if client_uuid else None,
                        "server_sync_uuid": str(canonical_uuid) if canonical_uuid else None,
                        "user_id": getattr(user, "id", None),
                        "student_id": getattr(getattr(user, "student", None), "id", None),
                    }
                )
            except Exception as exc:
                errors += 1
                results.append(
                    {
                        "status": "error",
                        "phone": phone,
                        "client_sync_uuid": str(client_uuid) if client_uuid else None,
                        "error": str(exc),
                    }
                )

        return Response(
            {
                "resource": "students",
                "results": results,
                "created": created,
                "updated": updated,
                "mapped": mapped,
                "errors": errors,
                "server_time": timezone.now().isoformat(),
            }
        )

    @action(detail=False, methods=["post"], url_path="taken-lessons")
    def taken_lessons(self, request):
        deny = self._require_upsync_role(request)
        if deny:
            return deny

        ser = UpSyncTakeLessonsPayloadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        items = ser.validated_data["items"]

        results: list[dict] = []
        created = updated = errors = 0

        for it in items:
            student_uuid = it.get("student_sync_uuid")
            lesson_id = it.get("lesson_id")
            occurred_at = it.get("occurred_at")

            try:
                with transaction.atomic():
                    user = User.objects.filter(sync_uuid=student_uuid).select_related("student").first()
                    student = getattr(user, "student", None) if user is not None else None
                    if student is None:
                        raise ValueError("student not found")

                    lesson = LessonResource.objects.filter(pk=lesson_id).first()
                    if lesson is None:
                        raise ValueError("lesson not found")

                    obj, was_created = TakeLesson.objects.get_or_create(student=student, lesson=lesson)
                    if was_created:
                        created += 1
                        if occurred_at is not None:
                            TakeLesson.objects.filter(pk=obj.pk).update(created_at=occurred_at)

                        # Mirror in-app points awarding for video lessons.
                        if getattr(lesson, "type", None) == ContentTypeEnum.VIDEO.value:
                            _award_student_points(student, VIDEO_WATCH_POINTS)

                    results.append(
                        {
                            "status": "ok",
                            "student_sync_uuid": str(student_uuid),
                            "lesson_id": lesson_id,
                            "created": bool(was_created),
                        }
                    )
            except Exception as exc:
                errors += 1
                results.append(
                    {
                        "status": "error",
                        "student_sync_uuid": str(student_uuid) if student_uuid else None,
                        "lesson_id": lesson_id,
                        "error": str(exc),
                    }
                )

        return Response(
            {
                "resource": "taken_lessons",
                "results": results,
                "created": created,
                "updated": updated,
                "errors": errors,
                "server_time": timezone.now().isoformat(),
            }
        )

    @action(detail=False, methods=["post"], url_path="gameplays")
    def gameplays(self, request):
        deny = self._require_upsync_role(request)
        if deny:
            return deny

        ser = UpSyncGamePlaysPayloadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        items = ser.validated_data["items"]

        results: list[dict] = []
        created = updated = errors = 0

        for it in items:
            student_uuid = it.get("student_sync_uuid")
            game_id = it.get("game_id")
            last_played_at = it.get("last_played_at")

            try:
                with transaction.atomic():
                    user = User.objects.filter(sync_uuid=student_uuid).select_related("student").first()
                    student = getattr(user, "student", None) if user is not None else None
                    if student is None:
                        raise ValueError("student not found")

                    game = GameModel.objects.filter(pk=game_id).first()
                    if game is None:
                        raise ValueError("game not found")

                    obj, was_created = GamePlay.objects.get_or_create(student=student, game=game)
                    if was_created:
                        created += 1
                        _award_student_points(student, GAME_PLAY_POINTS)

                    if last_played_at is not None:
                        # Preserve latest play timestamp (do not move backwards).
                        existing_last = getattr(obj, "last_played_at", None)
                        if was_created or existing_last is None or last_played_at > existing_last:
                            GamePlay.objects.filter(pk=obj.pk).update(last_played_at=last_played_at)
                            if not was_created:
                                updated += 1

                results.append(
                    {
                        "status": "ok",
                        "student_sync_uuid": str(student_uuid),
                        "game_id": game_id,
                        "created": bool(was_created),
                    }
                )
            except Exception as exc:
                errors += 1
                results.append(
                    {
                        "status": "error",
                        "student_sync_uuid": str(student_uuid) if student_uuid else None,
                        "game_id": game_id,
                        "error": str(exc),
                    }
                )

        return Response(
            {
                "resource": "gameplays",
                "results": results,
                "created": created,
                "updated": updated,
                "errors": errors,
                "server_time": timezone.now().isoformat(),
            }
        )

    @action(detail=False, methods=["post"], url_path="login-streaks")
    def login_streaks(self, request):
        deny = self._require_upsync_role(request)
        if deny:
            return deny

        ser = UpSyncLoginStreaksPayloadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        items = ser.validated_data["items"]

        results: list[dict] = []
        updated = errors = 0

        for it in items:
            student_uuid = it.get("student_sync_uuid")
            last_day = it.get("last_login_activity_date")
            reported_current = it.get("current_login_streak")
            reported_max = it.get("max_login_streak")

            try:
                with transaction.atomic():
                    user = User.objects.filter(sync_uuid=student_uuid).select_related("student").first()
                    student: Student | None = getattr(user, "student", None) if user is not None else None
                    if student is None:
                        raise ValueError("student not found")

                    existing_last = getattr(student, "last_login_activity_date", None)
                    if existing_last is not None and last_day <= existing_last:
                        results.append(
                            {
                                "status": "ok",
                                "student_sync_uuid": str(student_uuid),
                                "updated": False,
                                "last_login_activity_date": str(existing_last),
                                "current_login_streak": int(getattr(student, "current_login_streak", 0) or 0),
                                "max_login_streak": int(getattr(student, "max_login_streak", 0) or 0),
                            }
                        )
                        continue

                    existing_current = int(getattr(student, "current_login_streak", 0) or 0)
                    existing_max = int(getattr(student, "max_login_streak", 0) or 0)

                    # Merge strategy:
                    # - Central is canonical, but we only move forward in time.
                    # - If the new day is consecutive, we can extend the streak.
                    candidate_current = 1
                    if existing_last is not None and existing_last == (last_day - timedelta(days=1)):
                        candidate_current = max(1, existing_current + 1)

                    rep_current = int(reported_current or 0)
                    rep_max = int(reported_max or 0)
                    rep_current = max(1, rep_current) if rep_current else 1

                    new_current = max(candidate_current, rep_current)
                    new_max = max(existing_max, rep_max, new_current)

                    Student.objects.filter(pk=student.pk).update(
                        last_login_activity_date=last_day,
                        current_login_streak=new_current,
                        max_login_streak=new_max,
                        updated_at=timezone.now(),
                    )
                    updated += 1

                results.append(
                    {
                        "status": "ok",
                        "student_sync_uuid": str(student_uuid),
                        "updated": True,
                        "last_login_activity_date": str(last_day),
                        "current_login_streak": new_current,
                        "max_login_streak": new_max,
                    }
                )
            except Exception as exc:
                errors += 1
                results.append(
                    {
                        "status": "error",
                        "student_sync_uuid": str(student_uuid) if student_uuid else None,
                        "error": str(exc),
                    }
                )

        return Response(
            {
                "resource": "login_streaks",
                "results": results,
                "updated": updated,
                "errors": errors,
                "server_time": timezone.now().isoformat(),
            }
        )

    @action(detail=False, methods=["post"], url_path="general-assessment-solutions")
    def general_assessment_solutions(self, request):
        deny = self._require_upsync_role(request)
        if deny:
            return deny

        ser = UpSyncGeneralAssessmentSolutionsPayloadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        items = ser.validated_data["items"]

        results: list[dict] = []
        created = updated = errors = 0

        for it in items:
            student_uuid = it.get("student_sync_uuid")
            assessment_id = it.get("assessment_id")
            solution_text = it.get("solution") or ""
            submitted_at = it.get("submitted_at")

            try:
                with transaction.atomic():
                    user = User.objects.filter(sync_uuid=student_uuid).select_related("student").first()
                    student = getattr(user, "student", None) if user is not None else None
                    if student is None:
                        raise ValueError("student not found")

                    assessment = GeneralAssessment.objects.filter(pk=assessment_id).first()
                    if assessment is None:
                        raise ValueError("assessment not found")

                    existing = (
                        AssessmentSolution.objects
                        .filter(assessment_id=assessment_id, student=student)
                        .order_by("id")
                        .first()
                    )
                    was_created = False
                    if existing is None:
                        existing = AssessmentSolution.objects.create(
                            assessment=assessment,
                            student=student,
                            solution=solution_text or "",
                        )
                        was_created = True
                        created += 1
                        _award_student_points(student, ASSESSMENT_SUBMISSION_POINTS)
                    else:
                        update_fields = []
                        if solution_text is not None and existing.solution != solution_text:
                            existing.solution = solution_text
                            update_fields.append("solution")
                        if update_fields:
                            update_fields.append("updated_at")
                            existing.save(update_fields=update_fields)
                            updated += 1

                    if submitted_at is not None and was_created:
                        AssessmentSolution.objects.filter(pk=existing.pk).update(submitted_at=submitted_at)

                results.append(
                    {
                        "status": "ok",
                        "student_sync_uuid": str(student_uuid),
                        "assessment_id": assessment_id,
                        "solution_id": existing.id,
                        "created": bool(was_created),
                    }
                )
            except Exception as exc:
                errors += 1
                results.append(
                    {
                        "status": "error",
                        "student_sync_uuid": str(student_uuid) if student_uuid else None,
                        "assessment_id": assessment_id,
                        "error": str(exc),
                    }
                )

        return Response(
            {
                "resource": "general_assessment_solutions",
                "results": results,
                "created": created,
                "updated": updated,
                "errors": errors,
                "server_time": timezone.now().isoformat(),
            }
        )

    @action(detail=False, methods=["post"], url_path="lesson-assessment-solutions")
    def lesson_assessment_solutions(self, request):
        deny = self._require_upsync_role(request)
        if deny:
            return deny

        ser = UpSyncLessonAssessmentSolutionsPayloadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        items = ser.validated_data["items"]

        results: list[dict] = []
        created = updated = errors = 0

        for it in items:
            student_uuid = it.get("student_sync_uuid")
            lesson_assessment_id = it.get("lesson_assessment_id")
            solution_text = it.get("solution") or ""
            submitted_at = it.get("submitted_at")

            try:
                with transaction.atomic():
                    user = User.objects.filter(sync_uuid=student_uuid).select_related("student").first()
                    student = getattr(user, "student", None) if user is not None else None
                    if student is None:
                        raise ValueError("student not found")

                    assessment = LessonAssessment.objects.filter(pk=lesson_assessment_id).first()
                    if assessment is None:
                        raise ValueError("lesson assessment not found")

                    existing = (
                        LessonAssessmentSolution.objects
                        .filter(lesson_assessment_id=lesson_assessment_id, student=student)
                        .order_by("id")
                        .first()
                    )
                    was_created = False
                    if existing is None:
                        existing = LessonAssessmentSolution.objects.create(
                            lesson_assessment=assessment,
                            student=student,
                            solution=solution_text or "",
                        )
                        was_created = True
                        created += 1
                        _award_student_points(student, ASSESSMENT_SUBMISSION_POINTS)
                    else:
                        update_fields = []
                        if solution_text is not None and existing.solution != solution_text:
                            existing.solution = solution_text
                            update_fields.append("solution")
                        if update_fields:
                            update_fields.append("updated_at")
                            existing.save(update_fields=update_fields)
                            updated += 1

                    if submitted_at is not None and was_created:
                        LessonAssessmentSolution.objects.filter(pk=existing.pk).update(submitted_at=submitted_at)

                results.append(
                    {
                        "status": "ok",
                        "student_sync_uuid": str(student_uuid),
                        "lesson_assessment_id": lesson_assessment_id,
                        "solution_id": existing.id,
                        "created": bool(was_created),
                    }
                )
            except Exception as exc:
                errors += 1
                results.append(
                    {
                        "status": "error",
                        "student_sync_uuid": str(student_uuid) if student_uuid else None,
                        "lesson_assessment_id": lesson_assessment_id,
                        "error": str(exc),
                    }
                )

        return Response(
            {
                "resource": "lesson_assessment_solutions",
                "results": results,
                "created": created,
                "updated": updated,
                "errors": errors,
                "server_time": timezone.now().isoformat(),
            }
        )

    @action(detail=False, methods=["post"], url_path="general-assessment-solutions/attachment")
    def general_assessment_solution_attachment(self, request):
        deny = self._require_upsync_role(request)
        if deny:
            return deny

        meta = UpSyncSolutionAttachmentSerializer(data=request.data)
        meta.is_valid(raise_exception=True)
        student_uuid = meta.validated_data["student_sync_uuid"]
        assessment_id = meta.validated_data.get("assessment_id")

        attachment = request.FILES.get("attachment")
        if attachment is None:
            return Response({"detail": "attachment file is required"}, status=400)

        user = User.objects.filter(sync_uuid=student_uuid).select_related("student").first()
        student = getattr(user, "student", None) if user is not None else None
        if student is None:
            return Response({"detail": "student not found"}, status=400)

        assessment = GeneralAssessment.objects.filter(pk=assessment_id).first()
        if assessment is None:
            return Response({"detail": "assessment not found"}, status=400)

        sol = (
            AssessmentSolution.objects
            .filter(assessment_id=assessment_id, student=student)
            .order_by("id")
            .first()
        )
        created = False
        if sol is None:
            sol = AssessmentSolution.objects.create(assessment=assessment, student=student, solution="")
            created = True
            _award_student_points(student, ASSESSMENT_SUBMISSION_POINTS)

        sol.attachment = attachment
        sol.save(update_fields=["attachment", "updated_at"])

        return Response(
            {
                "status": "ok",
                "created": bool(created),
                "solution_id": sol.id,
            }
        )

    @action(detail=False, methods=["post"], url_path="lesson-assessment-solutions/attachment")
    def lesson_assessment_solution_attachment(self, request):
        deny = self._require_upsync_role(request)
        if deny:
            return deny

        meta = UpSyncSolutionAttachmentSerializer(data=request.data)
        meta.is_valid(raise_exception=True)
        student_uuid = meta.validated_data["student_sync_uuid"]
        lesson_assessment_id = meta.validated_data.get("lesson_assessment_id")

        attachment = request.FILES.get("attachment")
        if attachment is None:
            return Response({"detail": "attachment file is required"}, status=400)

        user = User.objects.filter(sync_uuid=student_uuid).select_related("student").first()
        student = getattr(user, "student", None) if user is not None else None
        if student is None:
            return Response({"detail": "student not found"}, status=400)

        assessment = LessonAssessment.objects.filter(pk=lesson_assessment_id).first()
        if assessment is None:
            return Response({"detail": "lesson assessment not found"}, status=400)

        sol = (
            LessonAssessmentSolution.objects
            .filter(lesson_assessment_id=lesson_assessment_id, student=student)
            .order_by("id")
            .first()
        )
        created = False
        if sol is None:
            sol = LessonAssessmentSolution.objects.create(lesson_assessment=assessment, student=student, solution="")
            created = True
            _award_student_points(student, ASSESSMENT_SUBMISSION_POINTS)

        sol.attachment = attachment
        sol.save(update_fields=["attachment", "updated_at"])

        return Response(
            {
                "status": "ok",
                "created": bool(created),
                "solution_id": sol.id,
            }
        )

    @action(detail=False, methods=["post"], url_path="general-assessment-grades")
    def general_assessment_grades(self, request):
        deny = self._require_upsync_role(request)
        if deny:
            return deny

        ser = UpSyncGeneralAssessmentGradesPayloadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        items = ser.validated_data["items"]

        results: list[dict] = []
        created = updated = errors = 0

        for it in items:
            student_uuid = it.get("student_sync_uuid")
            assessment_id = it.get("assessment_id")
            score = it.get("score")
            created_at = it.get("created_at")

            try:
                with transaction.atomic():
                    user = User.objects.filter(sync_uuid=student_uuid).select_related("student").first()
                    student = getattr(user, "student", None) if user is not None else None
                    if student is None:
                        raise ValueError("student not found")

                    assessment = GeneralAssessment.objects.filter(pk=assessment_id).first()
                    if assessment is None:
                        raise ValueError("assessment not found")

                    obj, was_created = GeneralAssessmentGrade.objects.update_or_create(
                        assessment=assessment,
                        student=student,
                        defaults={"score": score},
                    )

                    if was_created:
                        created += 1
                        if created_at is not None:
                            GeneralAssessmentGrade.objects.filter(pk=obj.pk).update(created_at=created_at)
                    else:
                        updated += 1

                results.append(
                    {
                        "status": "ok",
                        "student_sync_uuid": str(student_uuid),
                        "assessment_id": assessment_id,
                        "grade_id": obj.id,
                        "created": bool(was_created),
                    }
                )
            except Exception as exc:
                errors += 1
                results.append(
                    {
                        "status": "error",
                        "student_sync_uuid": str(student_uuid) if student_uuid else None,
                        "assessment_id": assessment_id,
                        "error": str(exc),
                    }
                )

        return Response(
            {
                "resource": "general_assessment_grades",
                "results": results,
                "created": created,
                "updated": updated,
                "errors": errors,
                "server_time": timezone.now().isoformat(),
            }
        )

    @action(detail=False, methods=["post"], url_path="lesson-assessment-grades")
    def lesson_assessment_grades(self, request):
        deny = self._require_upsync_role(request)
        if deny:
            return deny

        ser = UpSyncLessonAssessmentGradesPayloadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        items = ser.validated_data["items"]

        results: list[dict] = []
        created = updated = errors = 0

        for it in items:
            student_uuid = it.get("student_sync_uuid")
            lesson_assessment_id = it.get("lesson_assessment_id")
            score = it.get("score")
            created_at = it.get("created_at")

            try:
                with transaction.atomic():
                    user = User.objects.filter(sync_uuid=student_uuid).select_related("student").first()
                    student = getattr(user, "student", None) if user is not None else None
                    if student is None:
                        raise ValueError("student not found")

                    assessment = LessonAssessment.objects.filter(pk=lesson_assessment_id).first()
                    if assessment is None:
                        raise ValueError("lesson assessment not found")

                    obj, was_created = LessonAssessmentGrade.objects.update_or_create(
                        lesson_assessment=assessment,
                        student=student,
                        defaults={"score": score},
                    )

                    if was_created:
                        created += 1
                        if created_at is not None:
                            LessonAssessmentGrade.objects.filter(pk=obj.pk).update(created_at=created_at)
                    else:
                        updated += 1

                results.append(
                    {
                        "status": "ok",
                        "student_sync_uuid": str(student_uuid),
                        "lesson_assessment_id": lesson_assessment_id,
                        "grade_id": obj.id,
                        "created": bool(was_created),
                    }
                )
            except Exception as exc:
                errors += 1
                results.append(
                    {
                        "status": "error",
                        "student_sync_uuid": str(student_uuid) if student_uuid else None,
                        "lesson_assessment_id": lesson_assessment_id,
                        "error": str(exc),
                    }
                )

        return Response(
            {
                "resource": "lesson_assessment_grades",
                "results": results,
                "created": created,
                "updated": updated,
                "errors": errors,
                "server_time": timezone.now().isoformat(),
            }
        )
