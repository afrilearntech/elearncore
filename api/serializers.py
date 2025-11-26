from rest_framework import serializers


class ProfileSetupSerializer(serializers.Serializer):
    email = serializers.EmailField(required=True)
    phone = serializers.CharField(required=True, max_length=25)
    name = serializers.CharField(required=True, max_length=255)
    password = serializers.CharField(write_only=True, min_length=6)
    confirm_password = serializers.CharField(write_only=True, min_length=6)


class UserRoleSerializer(serializers.Serializer):
    role = serializers.CharField(required=True, max_length=50)


class AboutUserSerializer(serializers.Serializer):
    dob = serializers.DateField(required=False)
    gender = serializers.CharField(required=False, allow_blank=True, max_length=20)
    grade = serializers.CharField(required=False, allow_blank=True)
    # Prefer school_id when available; school_name/institution_name kept for convenience
    school_id = serializers.IntegerField(required=False)
    school_name = serializers.CharField(required=False, allow_blank=True)
    district_id = serializers.IntegerField(required=False)


class LinkChildSerializer(serializers.Serializer):
    student_id = serializers.IntegerField(required=True)
    student_email = serializers.EmailField(required=False, allow_blank=True)
    student_phone = serializers.CharField(required=False, allow_blank=True, max_length=25)

    def validate(self, attrs):
        if not attrs.get('student_email') and not attrs.get('student_phone'):
            raise serializers.ValidationError('Provide either student_email or student_phone.')
        return attrs


class LoginSerializer(serializers.Serializer):
    identifier = serializers.CharField(help_text="Phone or Email")
    password = serializers.CharField(write_only=True, min_length=6)


class ContentModerationSerializer(serializers.Serializer):
    model = serializers.ChoiceField(
        choices=[
            "subject",
            "lesson",
            "general_assessment",
            "lesson_assessment",
            "game",
            "school",
            "county",
            "district",
            "student",
            "teacher",
        ],
    )
    id = serializers.IntegerField()
    action = serializers.ChoiceField(choices=["approve", "reject", "request_changes", "request_review"])
    moderation_comment = serializers.CharField(required=False, allow_blank=True)


class ContentModerationResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    model = serializers.CharField()
    status = serializers.CharField()
    moderation_comment = serializers.CharField(allow_null=True, required=False)


class ContentAssessmentItemSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=["general", "lesson"])
    id = serializers.IntegerField()
    title = serializers.CharField()
    type = serializers.CharField()
    marks = serializers.FloatField()
    status = serializers.CharField()
    due_at = serializers.CharField(allow_null=True)
    grade = serializers.CharField(allow_null=True, required=False)
    lesson_id = serializers.IntegerField(allow_null=True, required=False)
    lesson_title = serializers.CharField(allow_null=True, required=False)
    subject_id = serializers.IntegerField(allow_null=True, required=False)
    subject_name = serializers.CharField(allow_null=True, required=False)
    given_by_id = serializers.IntegerField(allow_null=True, required=False)


class ContentDashboardCountsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    approved = serializers.IntegerField()
    rejected = serializers.IntegerField()
    review_requested = serializers.IntegerField()


class ContentDashboardSerializer(serializers.Serializer):
    overall = ContentDashboardCountsSerializer()
    by_type = serializers.DictField(child=ContentDashboardCountsSerializer())
