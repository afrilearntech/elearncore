from rest_framework import serializers

from .models import (
	Subject, Topic, Period, LessonResource, TakeLesson,
	GeneralAssessment, GeneralAssessmentGrade,
	LessonAssessment, LessonAssessmentGrade,
	Question, Option, GameModel, AssessmentSolution,
)


class SubjectSerializer(serializers.ModelSerializer):
	# Expose objectives as a list of strings while storing them
	# internally as a single comma-separated string on the model.
	objectives = serializers.ListField(
		child=serializers.CharField(),
		required=False,
	)

	class Meta:
		model = Subject
		fields = [
			'id', 'name', 'grade', 'status', 'description', 'thumbnail', 'teachers', 'moderation_comment',
			'objectives', 'created_at', 'updated_at', 'created_by',
		]
		read_only_fields = ['created_at', 'updated_at', 'created_by']

	def to_representation(self, instance):
		data = super().to_representation(instance)
		raw = instance.objectives or ""
		# Split on commas and strip whitespace; filter out empties
		items = [part.strip() for part in raw.split(',') if part.strip()]
		data['objectives'] = items
		return data

	def create(self, validated_data):
		objective_list = validated_data.pop('objectives', []) or []
		validated_data['objectives'] = self._join_objectives(objective_list)
		return super().create(validated_data)

	def update(self, instance, validated_data):
		objective_list = validated_data.pop('objectives', None)
		if objective_list is not None:
			validated_data['objectives'] = self._join_objectives(objective_list)
		return super().update(instance, validated_data)

	def _join_objectives(self, items):
		# Normalize and join into a comma-separated string for storage
		parts = [str(s).strip() for s in items if str(s).strip()]
		return ", ".join(parts)


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
			'id', 'subject', 'topic', 'period', 'title', 'description', 'type', 'status', 'resource', 'thumbnail', 'created_by', 'moderation_comment',
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
		fields = ['id', 'title', 'type', 'given_by', 'instructions', 'marks', 'due_at', 'grade', 'status', 'moderation_comment', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']


class AssessmentSolutionSerializer(serializers.ModelSerializer):
	class Meta:
		model = AssessmentSolution
		fields = ['id', 'assessment', 'student', 'solution', 'attachment', 'submitted_at', 'created_at', 'updated_at']
		read_only_fields = ['submitted_at', 'created_at', 'updated_at']


class GeneralAssessmentGradeSerializer(serializers.ModelSerializer):
	solution = AssessmentSolutionSerializer(read_only=True)

	class Meta:
		model = GeneralAssessmentGrade
		fields = ['id', 'assessment', 'student', 'solution', 'score', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at', 'solution']


class LessonAssessmentSerializer(serializers.ModelSerializer):
	class Meta:
		model = LessonAssessment
		fields = ['id', 'lesson', 'type', 'given_by', 'title', 'instructions', 'marks', 'due_at', 'status', 'moderation_comment', 'created_at', 'updated_at']
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


class GameSerializer(serializers.ModelSerializer):
	created_by = serializers.PrimaryKeyRelatedField(read_only=True)

	class Meta:
		model = GameModel
		fields = [
			'id', 'name', 'instructions', 'description', 'hint', 'correct_answer',
			'type', 'image', 'status', 'created_by', 'created_at', 'updated_at',
		]
		read_only_fields = ['created_at', 'updated_at', 'created_by']