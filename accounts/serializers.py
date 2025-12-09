from rest_framework import serializers

from .models import User, County, District, School, Student, Teacher, Parent


class UserSerializer(serializers.ModelSerializer):
	class Meta:
		model = User
		fields = [
			'id', 'email', 'phone', 'name', 'role', 'dob', 'gender', 'is_active', 'is_staff', 'is_superuser',
			'phone_verified', 'email_verified', 'created_at', 'updated_at'
		]
		read_only_fields = ['is_staff', 'is_superuser', 'created_at', 'updated_at']


class CountySerializer(serializers.ModelSerializer):
	class Meta:
		model = County
		fields = ['id', 'name', 'status', 'moderation_comment', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']


class DistrictSerializer(serializers.ModelSerializer):
	class Meta:
		model = District
		fields = ['id', 'county', 'name', 'status', 'moderation_comment', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']


class SchoolSerializer(serializers.ModelSerializer):
	class Meta:
		model = School
		fields = ['id', 'district', 'name', 'status', 'moderation_comment', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']


class CountyLookupSerializer(serializers.ModelSerializer):
	class Meta:
		model = County
		fields = ['id', 'name']
		read_only_fields = ['id', 'name']


class DistrictLookupSerializer(serializers.ModelSerializer):
	county_id = serializers.IntegerField(source='county.id', read_only=True)
	county_name = serializers.CharField(source='county.name', read_only=True)

	class Meta:
		model = District
		fields = ['id', 'name', 'county_id', 'county_name']
		read_only_fields = ['id', 'name', 'county_id', 'county_name']


class SchoolLookupSerializer(serializers.ModelSerializer):
	district_id = serializers.IntegerField(source='district.id', read_only=True)
	district_name = serializers.CharField(source='district.name', read_only=True)
	county_id = serializers.IntegerField(source='district.county.id', read_only=True)
	county_name = serializers.CharField(source='district.county.name', read_only=True)

	class Meta:
		model = School
		fields = ['id', 'name', 'district_id', 'district_name', 'county_id', 'county_name']
		read_only_fields = ['id', 'name', 'district_id', 'district_name', 'county_id', 'county_name']


class StudentSerializer(serializers.ModelSerializer):
	profile = UserSerializer(read_only=True)
	school = SchoolLookupSerializer(read_only=True)
	class Meta:
		model = Student
		fields = ['id', 'student_id', 'profile', 'school', 'grade', 'status', 'moderation_comment', 'created_at', 'updated_at']
		read_only_fields = ['id', 'student_id', 'created_at', 'updated_at']


class TeacherSerializer(serializers.ModelSerializer):
	profile = UserSerializer(read_only=True)
	class Meta:
		model = Teacher
		fields = ['id', 'teacher_id', 'profile', 'school', 'status', 'moderation_comment', 'created_at', 'updated_at']
		read_only_fields = ['id', 'teacher_id', 'created_at', 'updated_at']


class ParentSerializer(serializers.ModelSerializer):
	class Meta:
		model = Parent
		fields = ['id', 'parent_id', 'profile', 'wards', 'created_at', 'updated_at']
		read_only_fields = ['id', 'parent_id', 'created_at', 'updated_at']