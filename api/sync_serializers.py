from __future__ import annotations

from typing import Any

from rest_framework import serializers

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


def _absolute_url(request, maybe_relative_url: str | None) -> str | None:
    if not maybe_relative_url:
        return None
    if request is None:
        return maybe_relative_url
    try:
        return request.build_absolute_uri(maybe_relative_url)
    except Exception:
        return maybe_relative_url


def _file_info(*, request, field) -> dict[str, Any] | None:
    """Return a stable file descriptor for offline sync.

    We return both the storage path (`path`) and a downloadable URL (`url`).
    `size` is best-effort.

    NOTE: On remote storage backends (e.g., S3/Spaces), reading `field.size`
    may trigger a network metadata call per file and make list endpoints slow.
    By default we only include `size` for local filesystem storage, unless the
    client explicitly sets `?include_size=1`.
    """

    if not field:
        return None

    try:
        path = getattr(field, "name", None) or None
    except Exception:
        path = None

    try:
        url = getattr(field, "url", None) or None
    except Exception:
        url = None

    include_size: bool | None = None
    try:
        raw = request.query_params.get("include_size") if request is not None else None
        if raw not in (None, ""):
            include_size = str(raw).strip().lower() not in {"0", "false", "no"}
    except Exception:
        include_size = None

    if include_size is None:
        try:
            from django.core.files.storage import FileSystemStorage

            include_size = isinstance(getattr(field, "storage", None), FileSystemStorage)
        except Exception:
            include_size = False

    size = None
    if include_size:
        try:
            size = getattr(field, "size", None)
        except Exception:
            size = None

    if url:
        url = _absolute_url(request, url)

    return {
        "path": path,
        "url": url,
        "size": size,
    }


class SyncSubjectSerializer(serializers.ModelSerializer):
    thumbnail = serializers.SerializerMethodField()

    class Meta:
        model = Subject
        fields = [
            "id",
            "name",
            "grade",
            "description",
            "objectives",
            "status",
            "moderation_comment",
            "thumbnail",
            "created_at",
            "updated_at",
        ]

    def get_thumbnail(self, obj: Subject):
        return _file_info(request=self.context.get("request"), field=obj.thumbnail)


class SyncTopicSerializer(serializers.ModelSerializer):
    class Meta:
        model = Topic
        fields = [
            "id",
            "subject_id",
            "name",
            "created_at",
            "updated_at",
        ]


class SyncPeriodSerializer(serializers.ModelSerializer):
    class Meta:
        model = Period
        fields = [
            "id",
            "name",
            "start_month",
            "end_month",
            "created_at",
            "updated_at",
        ]


class SyncLessonResourceSerializer(serializers.ModelSerializer):
    resource = serializers.SerializerMethodField()
    thumbnail = serializers.SerializerMethodField()

    class Meta:
        model = LessonResource
        fields = [
            "id",
            "subject_id",
            "topic_id",
            "period_id",
            "instructor_name",
            "title",
            "description",
            "type",
            "status",
            "duration_minutes",
            "moderation_comment",
            "resource",
            "thumbnail",
            "created_at",
            "updated_at",
        ]

    def get_resource(self, obj: LessonResource):
        return _file_info(request=self.context.get("request"), field=obj.resource)

    def get_thumbnail(self, obj: LessonResource):
        return _file_info(request=self.context.get("request"), field=obj.thumbnail)


class SyncGameSerializer(serializers.ModelSerializer):
    image = serializers.SerializerMethodField()

    class Meta:
        model = GameModel
        fields = [
            "id",
            "name",
            "instructions",
            "description",
            "grade",
            "hint",
            "correct_answer",
            "type",
            "status",
            "image",
            "created_at",
            "updated_at",
        ]

    def get_image(self, obj: GameModel):
        return _file_info(request=self.context.get("request"), field=obj.image)


class SyncGeneralAssessmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = GeneralAssessment
        fields = [
            "id",
            "title",
            "instructions",
            "type",
            "marks",
            "due_at",
            "grade",
            "ai_recommended",
            "is_targeted",
            "status",
            "moderation_comment",
            "created_at",
            "updated_at",
        ]


class SyncLessonAssessmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = LessonAssessment
        fields = [
            "id",
            "lesson_id",
            "title",
            "instructions",
            "type",
            "marks",
            "due_at",
            "ai_recommended",
            "is_targeted",
            "status",
            "moderation_comment",
            "created_at",
            "updated_at",
        ]


class SyncQuestionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Question
        fields = [
            "id",
            "general_assessment_id",
            "lesson_assessment_id",
            "type",
            "question",
            "answer",
            "created_at",
            "updated_at",
        ]


class SyncOptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Option
        fields = [
            "id",
            "question_id",
            "value",
            "created_at",
            "updated_at",
        ]


class SyncCountySerializer(serializers.ModelSerializer):
    class Meta:
        model = County
        fields = [
            "id",
            "name",
            "status",
            "moderation_comment",
            "created_at",
            "updated_at",
        ]


class SyncDistrictSerializer(serializers.ModelSerializer):
    county_id = serializers.IntegerField(source="county.id", read_only=True)

    class Meta:
        model = District
        fields = [
            "id",
            "county_id",
            "name",
            "status",
            "moderation_comment",
            "created_at",
            "updated_at",
        ]


class SyncSchoolSerializer(serializers.ModelSerializer):
    district_id = serializers.IntegerField(source="district.id", read_only=True)

    class Meta:
        model = School
        fields = [
            "id",
            "district_id",
            "name",
            "status",
            "moderation_comment",
            "created_at",
            "updated_at",
        ]


class SyncStudentUserSerializer(serializers.ModelSerializer):
    """Student user downsync payload.

    WARNING: includes password hash for offline authentication.
    Endpoint access must be restricted.
    """

    password_hash = serializers.CharField(source="password")

    class Meta:
        model = User
        fields = [
            "sync_uuid",
            "phone",
            "email",
            "name",
            "role",
            "is_active",
            "deleted",
            "dob",
            "gender",
            "password_hash",
            "created_at",
            "updated_at",
        ]


class SyncStudentSerializer(serializers.ModelSerializer):
    profile_sync_uuid = serializers.UUIDField(source="profile.sync_uuid", read_only=True)

    class Meta:
        model = Student
        fields = [
            "profile_sync_uuid",
            "student_id",
            "school_id",
            "grade",
            "points",
            "current_login_streak",
            "max_login_streak",
            "last_login_activity_date",
            "status",
            "moderation_comment",
            "created_at",
            "updated_at",
        ]
