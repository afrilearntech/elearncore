from rest_framework import serializers

from .models import User, County, District, School, Student, Teacher, Parent


class UserSerializer(serializers.ModelSerializer):
	class Meta:
		model = User
		fields = [
			'id', 'email', 'phone', 'name', 'role', 'is_active', 'is_staff', 'is_superuser',
			'phone_verified', 'email_verified', 'created_at', 'updated_at'
		]
		read_only_fields = ['is_staff', 'is_superuser', 'created_at', 'updated_at']


class CountySerializer(serializers.ModelSerializer):
	class Meta:
		model = County
		fields = ['id', 'name', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']


class DistrictSerializer(serializers.ModelSerializer):
	class Meta:
		model = District
		fields = ['id', 'county', 'name', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']


class SchoolSerializer(serializers.ModelSerializer):
	class Meta:
		model = School
		fields = ['id', 'district', 'name', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']


class StudentSerializer(serializers.ModelSerializer):
	class Meta:
		model = Student
		fields = ['id', 'user', 'school', 'grade', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']


class TeacherSerializer(serializers.ModelSerializer):
	class Meta:
		model = Teacher
		fields = ['id', 'user', 'school', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']


class ParentSerializer(serializers.ModelSerializer):
	class Meta:
		model = Parent
		fields = ['id', 'user', 'wards', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']