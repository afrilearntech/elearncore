from rest_framework import serializers

from .models import (
	Subject, Topic, Period, LessonResource, TakeLesson,
	GeneralAssessment, GeneralAssessmentGrade,
	LessonAssessment, LessonAssessmentGrade,
	Question, Option, GameModel, AssessmentSolution,
)


class SubjectSerializer(serializers.ModelSerializer):
	"""Read serializer: expose objectives as list of strings.

	The underlying model stores objectives as a single comma-separated
	string. This serializer converts that string into a list on output.
	"""

	objectives = serializers.ListField(
		child=serializers.CharField(),
		required=False,
	)

	teacher_count = serializers.IntegerField(read_only=True)

	class Meta:
		model = Subject
		fields = [
			'id', 'name', 'grade', 'status', 'description', 'thumbnail', 'teachers', 'moderation_comment',
			'objectives', 'created_at', 'updated_at', 'created_by', 'teacher_count',
		]
		read_only_fields = ['created_at', 'teachers', 'updated_at', 'created_by']

	def to_representation(self, instance):
		data = super().to_representation(instance)
		raw = instance.objectives or ""
		# Split on commas and strip whitespace; filter out empties
		items = [part.strip() for part in raw.split(',') if part.strip()]
		data['objectives'] = items
		return data


class SubjectWriteSerializer(serializers.ModelSerializer):
	"""Write serializer: accept objectives as a comma-separated string.

	Swagger/clients will see a simple text field for objectives when
	creating or updating subjects. Internally we normalize and store
	as a comma-separated string.
	"""

	objectives = serializers.CharField(
		required=False,
		allow_blank=True,
		help_text="Comma-separated list of objectives.",
	)

	class Meta:
		model = Subject
		fields = [
			'id', 'name', 'grade', 'status', 'description', 'thumbnail', 'teachers', 'moderation_comment',
			'objectives', 'created_at', 'updated_at', 'created_by',
		]
		read_only_fields = ['created_at', 'teachers', 'updated_at', 'created_by']

	def create(self, validated_data):
		raw = validated_data.get('objectives') or ""
		validated_data['objectives'] = self._normalize_objectives(raw)
		return super().create(validated_data)

	def update(self, instance, validated_data):
		if 'objectives' in validated_data:
			raw = validated_data.get('objectives') or ""
			validated_data['objectives'] = self._normalize_objectives(raw)
		return super().update(instance, validated_data)

	def _normalize_objectives(self, raw: str) -> str:
		parts = [part.strip() for part in str(raw).split(',') if part.strip()]
		return ", ".join(parts)


class TopicSerializer(serializers.ModelSerializer):
	subject_name = serializers.CharField(source='subject.name', read_only=True)
	class Meta:
		model = Topic
		fields = ['id', 'subject', 'subject_name', 'name', 'created_at', 'updated_at']
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