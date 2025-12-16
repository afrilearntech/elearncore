from rest_framework import serializers

from elearncore.sysutils.constants import QType as QTypeEnum

from .models import (
	Subject, Topic, Period, LessonResource, TakeLesson,
	GeneralAssessment, GeneralAssessmentGrade,
	LessonAssessment, LessonAssessmentGrade,
	Question, Option, GameModel, AssessmentSolution,
	LessonAssessmentSolution,
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


class LessonAssessmentSolutionSerializer(serializers.ModelSerializer):
	class Meta:
		model = LessonAssessmentSolution
		fields = ['id', 'lesson_assessment', 'student', 'solution', 'attachment', 'submitted_at', 'created_at', 'updated_at']
		read_only_fields = ['submitted_at', 'created_at', 'updated_at']


class GeneralAssessmentGradeSerializer(serializers.ModelSerializer):
	solution = AssessmentSolutionSerializer(read_only=True)

	class Meta:
		model = GeneralAssessmentGrade
		fields = ['id', 'assessment', 'student', 'solution', 'score', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at', 'solution']


class LessonAssessmentSerializer(serializers.ModelSerializer):
	grade =  serializers.CharField(source='lesson.subject.grade', read_only=True)
	class Meta:
		model = LessonAssessment
		fields = ['id', 'lesson', 'type', 'given_by', 'title', 'grade', 'instructions', 'marks', 'due_at', 'status', 'moderation_comment', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']


class LessonAssessmentGradeSerializer(serializers.ModelSerializer):
	class Meta:
		model = LessonAssessmentGrade
		fields = ['id', 'lesson_assessment', 'student', 'score', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']


class OptionSerializer(serializers.ModelSerializer):
	class Meta:
		model = Option
		fields = ['id', 'question', 'value', 'created_at', 'updated_at']
		read_only_fields = ['created_at', 'updated_at']


class QuestionSerializer(serializers.ModelSerializer):
	options = OptionSerializer(many=True, read_only=True)

	class Meta:
		model = Question
		fields = [
			'id', 'general_assessment', 'lesson_assessment', 'type', 'question', 'answer', 'options', 'created_at', 'updated_at'
		]
		read_only_fields = ['created_at', 'updated_at']


class QuestionCreateSerializer(serializers.Serializer):
	"""Serializer to create a Question with optional options.

	Supports attaching the question to either a GeneralAssessment or a
	LessonAssessment (exactly one must be provided).
	"""

	general_assessment_id = serializers.IntegerField(required=False)
	lesson_assessment_id = serializers.IntegerField(required=False)
	type = serializers.ChoiceField(
		choices=[(qt.value, qt.value) for qt in QTypeEnum],
	)
	question = serializers.CharField()
	answer = serializers.CharField(required=False, allow_blank=True)
	options = serializers.ListField(
		child=serializers.CharField(),
		required=False,
		allow_empty=True,
		help_text="Optional list of option strings for multiple-choice/true-false questions.",
	)

	def validate(self, attrs):
		ga_id = attrs.get("general_assessment_id")
		la_id = attrs.get("lesson_assessment_id")
		# Exactly one of the two assessment IDs must be provided
		if bool(ga_id) == bool(la_id):
			raise serializers.ValidationError(
				{"non_field_errors": ["Provide exactly one of general_assessment_id or lesson_assessment_id."]}
			)

		general_assessment = None
		lesson_assessment = None
		if ga_id:
			try:
				general_assessment = GeneralAssessment.objects.get(pk=ga_id)
			except GeneralAssessment.DoesNotExist:
				raise serializers.ValidationError({"general_assessment_id": ["General assessment not found."]})
		elif la_id:
			try:
				lesson_assessment = LessonAssessment.objects.get(pk=la_id)
			except LessonAssessment.DoesNotExist:
				raise serializers.ValidationError({"lesson_assessment_id": ["Lesson assessment not found."]})

		# For teacher endpoints, enforce that the assessment belongs to the teacher
		request = self.context.get("request")
		restrict_to_teacher = bool(self.context.get("restrict_to_teacher"))
		if restrict_to_teacher and request is not None:
			teacher = getattr(getattr(request, "user", None), "teacher", None)
			if teacher is None:
				raise serializers.ValidationError({"non_field_errors": ["Teacher profile required."]})
			if general_assessment is not None and general_assessment.given_by_id != teacher.id:
				raise serializers.ValidationError({"general_assessment_id": ["You can only add questions to your own assessments."]})
			if lesson_assessment is not None and lesson_assessment.given_by_id != teacher.id:
				raise serializers.ValidationError({"lesson_assessment_id": ["You can only add questions to your own assessments."]})

		qtype = attrs.get("type")
		options = attrs.get("options") or []
		if qtype in {QTypeEnum.MULTIPLE_CHOICE.value, QTypeEnum.TRUE_FALSE.value} and not options:
			raise serializers.ValidationError({"options": ["Options are required for this question type."]})

		attrs["general_assessment"] = general_assessment
		attrs["lesson_assessment"] = lesson_assessment
		return attrs

	def create(self, validated_data):
		options = validated_data.pop("options", []) or []
		validated_data.pop("general_assessment_id", None)
		validated_data.pop("lesson_assessment_id", None)
		general_assessment = validated_data.pop("general_assessment", None)
		lesson_assessment = validated_data.pop("lesson_assessment", None)

		question = Question.objects.create(
			general_assessment=general_assessment,
			lesson_assessment=lesson_assessment,
			**validated_data,
		)
		for val in options:
			text = str(val).strip()
			if text:
				Option.objects.create(question=question, value=text)
		return question


class GameSerializer(serializers.ModelSerializer):
	created_by = serializers.PrimaryKeyRelatedField(read_only=True)

	class Meta:
		model = GameModel
		fields = [
			'id', 'name', 'instructions', 'description', 'hint', 'correct_answer',
			'type', 'image', 'status', 'created_by', 'created_at', 'updated_at',
		]
		read_only_fields = ['created_at', 'updated_at', 'created_by']