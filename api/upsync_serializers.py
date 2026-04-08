from __future__ import annotations

from rest_framework import serializers


class UpSyncStudentItemSerializer(serializers.Serializer):
    sync_uuid = serializers.UUIDField()
    phone = serializers.CharField(max_length=25)
    name = serializers.CharField(max_length=255)

    email = serializers.EmailField(required=False, allow_null=True, allow_blank=True)
    dob = serializers.DateField(required=False, allow_null=True)
    gender = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=20)

    # Student profile
    grade = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    school_id = serializers.IntegerField(required=False, allow_null=True)


class UpSyncStudentsPayloadSerializer(serializers.Serializer):
    items = UpSyncStudentItemSerializer(many=True)


class UpSyncTakeLessonItemSerializer(serializers.Serializer):
    student_sync_uuid = serializers.UUIDField()
    lesson_id = serializers.IntegerField()
    occurred_at = serializers.DateTimeField(required=False, allow_null=True)


class UpSyncTakeLessonsPayloadSerializer(serializers.Serializer):
    items = UpSyncTakeLessonItemSerializer(many=True)


class UpSyncGamePlayItemSerializer(serializers.Serializer):
    student_sync_uuid = serializers.UUIDField()
    game_id = serializers.IntegerField()
    last_played_at = serializers.DateTimeField(required=False, allow_null=True)


class UpSyncGamePlaysPayloadSerializer(serializers.Serializer):
    items = UpSyncGamePlayItemSerializer(many=True)


class UpSyncLoginStreakItemSerializer(serializers.Serializer):
    student_sync_uuid = serializers.UUIDField()
    last_login_activity_date = serializers.DateField()
    current_login_streak = serializers.IntegerField(required=False, allow_null=True)
    max_login_streak = serializers.IntegerField(required=False, allow_null=True)


class UpSyncLoginStreaksPayloadSerializer(serializers.Serializer):
    items = UpSyncLoginStreakItemSerializer(many=True)


class UpSyncGeneralAssessmentGradeItemSerializer(serializers.Serializer):
    student_sync_uuid = serializers.UUIDField()
    assessment_id = serializers.IntegerField()
    score = serializers.FloatField()
    created_at = serializers.DateTimeField(required=False, allow_null=True)


class UpSyncGeneralAssessmentGradesPayloadSerializer(serializers.Serializer):
    items = UpSyncGeneralAssessmentGradeItemSerializer(many=True)


class UpSyncLessonAssessmentGradeItemSerializer(serializers.Serializer):
    student_sync_uuid = serializers.UUIDField()
    lesson_assessment_id = serializers.IntegerField()
    score = serializers.FloatField()
    created_at = serializers.DateTimeField(required=False, allow_null=True)


class UpSyncLessonAssessmentGradesPayloadSerializer(serializers.Serializer):
    items = UpSyncLessonAssessmentGradeItemSerializer(many=True)


class UpSyncGeneralAssessmentSolutionItemSerializer(serializers.Serializer):
    student_sync_uuid = serializers.UUIDField()
    assessment_id = serializers.IntegerField()

    solution = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    submitted_at = serializers.DateTimeField(required=False, allow_null=True)


class UpSyncGeneralAssessmentSolutionsPayloadSerializer(serializers.Serializer):
    items = UpSyncGeneralAssessmentSolutionItemSerializer(many=True)


class UpSyncLessonAssessmentSolutionItemSerializer(serializers.Serializer):
    student_sync_uuid = serializers.UUIDField()
    lesson_assessment_id = serializers.IntegerField()

    solution = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    submitted_at = serializers.DateTimeField(required=False, allow_null=True)


class UpSyncLessonAssessmentSolutionsPayloadSerializer(serializers.Serializer):
    items = UpSyncLessonAssessmentSolutionItemSerializer(many=True)


class UpSyncSolutionAttachmentSerializer(serializers.Serializer):
    student_sync_uuid = serializers.UUIDField()
    # Exactly one of these must be set by the caller.
    assessment_id = serializers.IntegerField(required=False)
    lesson_assessment_id = serializers.IntegerField(required=False)

    def validate(self, attrs):
        ga = attrs.get("assessment_id")
        la = attrs.get("lesson_assessment_id")
        if bool(ga) == bool(la):
            raise serializers.ValidationError("Provide exactly one of assessment_id or lesson_assessment_id.")
        return attrs
