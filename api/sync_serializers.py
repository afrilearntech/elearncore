from __future__ import annotations

from typing import Any

from rest_framework import serializers

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
    `size` is best-effort (may trigger a storage metadata call on remote).
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
