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


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True, min_length=6)
    new_password = serializers.CharField(write_only=True, min_length=6)
    confirm_password = serializers.CharField(write_only=True, min_length=6)


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


class AdminDashboardSummaryCardSerializer(serializers.Serializer):
    """Summary card metrics for the admin dashboard.

    count: total items; change_pct: percentage change in items created this
    month versus last month.
    """

    count = serializers.IntegerField()
    change_pct = serializers.FloatField()


class AdminLessonsChartPointSerializer(serializers.Serializer):
    period = serializers.CharField()
    submitted = serializers.IntegerField()
    approved = serializers.IntegerField()
    rejected = serializers.IntegerField()


class AdminLessonsChartSerializer(serializers.Serializer):
    granularity = serializers.ChoiceField(choices=["day", "month", "year"])
    points = AdminLessonsChartPointSerializer(many=True)


class AdminHighLearnerSerializer(serializers.Serializer):
    student_id = serializers.IntegerField()
    name = serializers.CharField()
    subtitle = serializers.CharField()


class AdminDashboardSerializer(serializers.Serializer):
    summary_cards = serializers.DictField(child=AdminDashboardSummaryCardSerializer())
    lessons_chart = AdminLessonsChartSerializer()
    high_learners = AdminHighLearnerSerializer(many=True)


class TeacherCreateStudentSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    phone = serializers.CharField(max_length=25)
    email = serializers.EmailField(required=False, allow_blank=True)
    grade = serializers.CharField(required=False, allow_blank=True)
    gender = serializers.CharField(required=False, allow_blank=True, max_length=20)
    dob = serializers.DateField(required=False)
    school_id = serializers.IntegerField(required=False)

    def validate(self, attrs):
        from accounts.models import User
        phone = attrs.get("phone")
        email = attrs.get("email")
        if User.objects.filter(phone=phone).exists():
            raise serializers.ValidationError({"phone": "A user with this phone already exists."})
        if email and User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError({"email": "A user with this email already exists."})
        return attrs


class TeacherBulkStudentUploadSerializer(serializers.Serializer):
    """Serializer for teacher bulk student CSV uploads.

    Accepts a single file field. For now we support CSV files only.
    """
    file = serializers.FileField()

    def validate_file(self, value):
        name = getattr(value, "name", "") or ""
        if not name.lower().endswith(".csv"):
            raise serializers.ValidationError("Only CSV files with .csv extension are supported.")
        return value


class ContentCreateTeacherSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    phone = serializers.CharField(max_length=25)
    email = serializers.EmailField(required=False, allow_blank=True)
    gender = serializers.CharField(required=False, allow_blank=True, max_length=20)
    dob = serializers.DateField(required=False)
    school_id = serializers.IntegerField(required=True)

    def validate(self, attrs):
        from accounts.models import User
        phone = attrs.get("phone")
        email = attrs.get("email")
        if User.objects.filter(phone=phone).exists():
            raise serializers.ValidationError({"phone": "A user with this phone already exists."})
        if email and User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError({"email": "A user with this email already exists."})
        return attrs


class ContentBulkTeacherUploadSerializer(serializers.Serializer):
    """Serializer for bulk teacher CSV uploads used by content managers/admins."""
    file = serializers.FileField()

    def validate_file(self, value):
        name = getattr(value, "name", "") or ""
        if not name.lower().endswith(".csv"):
            raise serializers.ValidationError("Only CSV files with .csv extension are supported.")
        return value


class AssignSubjectsToTeacherSerializer(serializers.Serializer):
	teacher_id = serializers.IntegerField()
	subject_ids = serializers.ListField(
		child=serializers.IntegerField(),
		allow_empty=False,
		help_text="List of subject IDs to assign to this teacher (will replace existing assignments).",
	)


class AdminCreateContentManagerSerializer(serializers.Serializer):
    """Serializer for admin-created content managers (creators/validators)."""

    ROLE_CHOICES = [
        ("CONTENTCREATOR", "CONTENTCREATOR"),
        ("CONTENTVALIDATOR", "CONTENTVALIDATOR"),
    ]

    name = serializers.CharField(max_length=255)
    phone = serializers.CharField(max_length=25)
    email = serializers.EmailField(required=False, allow_blank=True)
    role = serializers.ChoiceField(choices=ROLE_CHOICES)
    gender = serializers.CharField(required=False, allow_blank=True, max_length=20)
    dob = serializers.DateField(required=False)

    def validate(self, attrs):
        from accounts.models import User

        phone = attrs.get("phone")
        email = attrs.get("email")
        if User.objects.filter(phone=phone).exists():
            raise serializers.ValidationError({"phone": "A user with this phone already exists."})
        if email and User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError({"email": "A user with this email already exists."})
        return attrs


class AdminBulkContentManagerUploadSerializer(serializers.Serializer):
    """Serializer for admin bulk content manager CSV uploads."""

    file = serializers.FileField()

    def validate_file(self, value):
        name = getattr(value, "name", "") or ""
        if not name.lower().endswith(".csv"):
            raise serializers.ValidationError("Only CSV files with .csv extension are supported.")
        return value


class AdminContentManagerListSerializer(serializers.Serializer):
    """Read-only representation of a content manager for admin listing.

    Includes basic identity fields plus a derived status.
    """

    name = serializers.CharField()
    email = serializers.EmailField(allow_null=True, required=False)
    role = serializers.CharField()
    status = serializers.CharField()

    def to_representation(self, instance):
        # instance is a User object
        status = "DELETED" if getattr(instance, "deleted", False) else (
            "ACTIVE" if getattr(instance, "is_active", False) else "INACTIVE"
        )
        return {
            "name": getattr(instance, "name", None),
            "email": getattr(instance, "email", None),
            "role": getattr(instance, "role", None),
            "status": status,
        }


class GradeAssessmentSerializer(serializers.Serializer):
    """Payload for grading an assessment for a student.

    Used for both general and lesson assessments (in the latter case,
    assessment_id refers to the lesson_assessment id).
    """

    assessment_id = serializers.IntegerField()
    student_id = serializers.IntegerField()
    score = serializers.FloatField(min_value=0.0)


class AdminStudentListSerializer(serializers.Serializer):
    """Read-only representation of a student for admin listing.

    Fields: name, school (name), email, linked_parents, grade, status.
    """

    name = serializers.CharField()
    school = serializers.CharField(allow_null=True, required=False)
    email = serializers.EmailField(allow_null=True, required=False)
    linked_parents = serializers.CharField(allow_blank=True, required=False)
    grade = serializers.CharField()
    status = serializers.CharField()

    def to_representation(self, instance):
        # instance is a Student object
        from accounts.models import Parent

        profile = getattr(instance, "profile", None)
        school = getattr(instance, "school", None)

        # Collect parent names from the guardians relationship
        parent_qs = getattr(instance, "guardians", None)
        parent_names = []
        if parent_qs is not None:
            for parent in parent_qs.select_related("profile").all():
                parent_profile = getattr(parent, "profile", None)
                if parent_profile and getattr(parent_profile, "name", None):
                    parent_names.append(parent_profile.name)

        return {
            "name": getattr(profile, "name", None) if profile else None,
            "school": getattr(school, "name", None) if school else None,
            "email": getattr(profile, "email", None) if profile else None,
            "linked_parents": ", ".join(parent_names) if parent_names else "",
            "grade": getattr(instance, "grade", None),
            "status": getattr(instance, "status", None),
        }


class AdminParentListSerializer(serializers.Serializer):
    """Read-only representation of a parent for admin listing.

    Fields: name, email, linked_students (count), status, date_joined.
    """

    name = serializers.CharField()
    email = serializers.EmailField(allow_null=True, required=False)
    linked_students = serializers.CharField()
    status = serializers.CharField()
    date_joined = serializers.DateTimeField()

    def to_representation(self, instance):
        # instance is a Parent object
        profile = getattr(instance, "profile", None)
        user_status = "DELETED" if getattr(profile, "deleted", False) else (
            "ACTIVE" if getattr(profile, "is_active", False) else "INACTIVE"
        )
        students_qs = getattr(instance, "wards", None)
        count = students_qs.count() if students_qs is not None else 0
        linked_label = f"{count} Student" if count == 1 else f"{count} Students"
        return {
            "name": getattr(profile, "name", None) if profile else None,
            "email": getattr(profile, "email", None) if profile else None,
            "linked_students": linked_label,
            "status": user_status,
            "date_joined": getattr(profile, "created_at", None) if profile else None,
        }
