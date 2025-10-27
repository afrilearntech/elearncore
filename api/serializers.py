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
