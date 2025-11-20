from rest_framework import serializers

from .models import (
	Subject, Topic, Period, LessonResource, TakeLesson,
	GeneralAssessment, GeneralAssessmentGrade,
	LessonAssessment, LessonAssessmentGrade,
	Question, Option, GameModel, Objective,
)


class ObjectiveSerializer(serializers.ModelSerializer):
	class Meta:
		model = Objective
		fields = ['id', 'text']


class SubjectSerializer(serializers.ModelSerializer):
	# Accept objectives as a simple list of strings when creating/updating
	objectives = serializers.ListField(
		child=serializers.CharField(),
		write_only=True,
		required=False,
	)
	objective_items = ObjectiveSerializer(source='objectives', many=True, read_only=True)

	class Meta:
		model = Subject
		fields = [
			'id', 'name', 'grade', 'description', 'thumbnail', 'teachers',
			'objective_items', 'objectives', 'created_at', 'updated_at', 'created_by',
		]
		read_only_fields = ['created_at', 'updated_at', 'created_by', 'objective_items']

	def create(self, validated_data):
		objective_strings = validated_data.pop('objectives', [])
		subject = super().create(validated_data)
		self._set_objectives(subject, objective_strings)
		return subject

	def update(self, instance, validated_data):
		objective_strings = validated_data.pop('objectives', None)
		instance = super().update(instance, validated_data)
		if objective_strings is not None:
			self._set_objectives(instance, objective_strings)
		return instance

	def _set_objectives(self, subject: Subject, objective_strings):
		# Normalize strings and deduplicate
		texts = [s.strip() for s in objective_strings if str(s).strip()]
		if not texts:
			subject.objectives.clear()
			return
		# Get or create Objective rows
		objs = []
		for text in texts:
			obj, _ = Objective.objects.get_or_create(text=text)
			objs.append(obj)
		subject.objectives.set(objs)


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
		fields = ['id', 'title', 'type', 'given_by', 'instructions', 'marks', 'due_at', 'grade', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']


class GeneralAssessmentGradeSerializer(serializers.ModelSerializer):
	class Meta:
		model = GeneralAssessmentGrade
		fields = ['id', 'assessment', 'student', 'score', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']


class LessonAssessmentSerializer(serializers.ModelSerializer):
	class Meta:
		model = LessonAssessment
		fields = ['id', 'lesson', 'type', 'given_by', 'title', 'instructions', 'marks', 'due_at', 'created_at', 'updated_at']
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
			'type', 'image', 'created_by', 'created_at', 'updated_at',
		]
		read_only_fields = ['created_at', 'updated_at', 'created_by']