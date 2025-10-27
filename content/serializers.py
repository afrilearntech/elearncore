from rest_framework import serializers

from .models import (
	Subject, Topic, Period, LessonResource, TakeLesson,
	GeneralAssessment, GeneralAssessmentGrade,
	LessonAssessment, LessonAssessmentGrade,
	Question, Option,
)


class SubjectSerializer(serializers.ModelSerializer):
	class Meta:
		model = Subject
		fields = ['id', 'name', 'grade', 'description', 'thumbnail', 'teachers', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']


class TopicSerializer(serializers.ModelSerializer):
	class Meta:
		model = Topic
		fields = ['id', 'subject', 'name', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']


class PeriodSerializer(serializers.ModelSerializer):
	class Meta:
		model = Period
		fields = ['id', 'name', 'start_month', 'end_month', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']


class LessonResourceSerializer(serializers.ModelSerializer):
	created_by = serializers.PrimaryKeyRelatedField(read_only=True)
	class Meta:
		model = LessonResource
		fields = [
			'id', 'subject', 'topic', 'period', 'title', 'description', 'type', 'status', 'resource_url', 'created_by',
			'duration_minutes', 'created_at', 'updated_at'
		]
		read_only_fields = ['created_at', 'updated_at']


class TakeLessonSerializer(serializers.ModelSerializer):
	class Meta:
		model = TakeLesson
		fields = ['id', 'student', 'lesson', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']


class GeneralAssessmentSerializer(serializers.ModelSerializer):
	class Meta:
		model = GeneralAssessment
		fields = ['id', 'title', 'given_by', 'instructions', 'marks', 'due_at', 'grade', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']


class GeneralAssessmentGradeSerializer(serializers.ModelSerializer):
	class Meta:
		model = GeneralAssessmentGrade
		fields = ['id', 'assessment', 'student', 'score', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']


class LessonAssessmentSerializer(serializers.ModelSerializer):
	class Meta:
		model = LessonAssessment
		fields = ['id', 'lesson', 'given_by', 'title', 'instructions', 'marks', 'due_at', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']


class LessonAssessmentGradeSerializer(serializers.ModelSerializer):
	class Meta:
		model = LessonAssessmentGrade
		fields = ['id', 'lesson_assessment', 'student', 'score', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']


class QuestionSerializer(serializers.ModelSerializer):
	class Meta:
		model = Question
		fields = [
			'id', 'general_assessment', 'lesson_assessment', 'type', 'question', 'answer', 'created_at', 'updated_at'
		]
		read_only_fields = ['created_at', 'updated_at']


class OptionSerializer(serializers.ModelSerializer):
	class Meta:
		model = Option
		fields = ['id', 'question', 'value', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']