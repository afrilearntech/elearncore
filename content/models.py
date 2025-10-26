from django.db import models
from django.db.models import Q
from django.conf import settings

from elearncore.sysutils.constants import (
	ContentType as ContentTypeEnum,
	Status as StatusEnum,
	StudentLevel,
	Month,
	QType as QTypeEnum,
)


class TimestampedModel(models.Model):
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		abstract = True


class Subject(TimestampedModel):
	name = models.CharField(max_length=120)
	grade = models.CharField(max_length=20, choices=[(lvl.value, lvl.value) for lvl in StudentLevel])
	description = models.TextField(blank=True, default="")

	# Allow teachers to be linked to one or more subjects
	teachers = models.ManyToManyField('accounts.Teacher', related_name='subjects', blank=True)

	class Meta:
		unique_together = ("name", "grade")

	def __str__(self) -> str:
		return f"{self.name} ({self.grade})"


class Topic(TimestampedModel):
	subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="topics")
	name = models.CharField(max_length=150)

	class Meta:
		unique_together = ("subject", "name")

	def __str__(self) -> str:
		return f"{self.subject.name} - {self.name}"


class Period(TimestampedModel):
	name = models.CharField(max_length=80)
	start_month = models.PositiveSmallIntegerField(choices=[(m.value, m.name.title()) for m in Month])
	end_month = models.PositiveSmallIntegerField(choices=[(m.value, m.name.title()) for m in Month])

	def __str__(self) -> str:
		return self.name


class LessonResource(TimestampedModel):
	subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="lesson_resources")
	topic = models.ForeignKey(Topic, on_delete=models.SET_NULL, null=True, blank=True, related_name="lesson_resources")
	period = models.ForeignKey(Period, on_delete=models.SET_NULL, null=True, blank=True, related_name="lesson_resources")

	title = models.CharField(max_length=200)
	description = models.TextField(blank=True, default="")
	type = models.CharField(max_length=20, choices=[(t.value, t.value) for t in ContentTypeEnum])
	status = models.CharField(max_length=30, choices=[(s.value, s.value) for s in StatusEnum], default=StatusEnum.DRAFT.value)
	resource_url = models.URLField(max_length=500)

	def __str__(self) -> str:
		return self.title


class TakeLesson(TimestampedModel):
	student = models.ForeignKey('accounts.Student', on_delete=models.CASCADE, related_name='taken_lessons')
	lesson = models.ForeignKey(LessonResource, on_delete=models.CASCADE, related_name='taken_by')

	class Meta:
		unique_together = ("student", "lesson")

	def __str__(self) -> str:
		return f"{self.student.user.name} -> {self.lesson.title}"


# Assessments
class GeneralAssessment(TimestampedModel):
	title = models.CharField(max_length=200)
	given_by = models.ForeignKey('accounts.Teacher', on_delete=models.SET_NULL, null=True, related_name='general_assessments')
	instructions = models.TextField(blank=True, default="")
	marks = models.FloatField(default=0.0)

	def __str__(self) -> str:
		return self.title


class GeneralAssessmentGrade(TimestampedModel):
	assessment = models.ForeignKey(GeneralAssessment, on_delete=models.CASCADE, related_name='grades')
	student = models.ForeignKey('accounts.Student', on_delete=models.CASCADE, related_name='general_assessment_grades')
	score = models.FloatField()

	class Meta:
		unique_together = ("assessment", "student")

	def __str__(self) -> str:
		return f"{self.student.user.name}: {self.score} / {self.assessment.marks}"


class LessonAssessment(TimestampedModel):
	lesson = models.ForeignKey(LessonResource, on_delete=models.CASCADE, related_name='assessments')
	given_by = models.ForeignKey('accounts.Teacher', on_delete=models.SET_NULL, null=True, related_name='lesson_assessments')
	title = models.CharField(max_length=200)
	instructions = models.TextField(blank=True, default="")
	marks = models.FloatField(default=0.0)

	def __str__(self) -> str:
		return self.title


class LessonAssessmentGrade(TimestampedModel):
	lesson_assessment = models.ForeignKey(LessonAssessment, on_delete=models.CASCADE, related_name='grades')
	student = models.ForeignKey('accounts.Student', on_delete=models.CASCADE, related_name='lesson_assessment_grades')
	score = models.FloatField()

	class Meta:
		unique_together = ("lesson_assessment", "student")

	def __str__(self) -> str:
		return f"{self.student.user.name}: {self.score} / {self.lesson_assessment.marks}"


class Question(TimestampedModel):
	"""A question that belongs to either a GeneralAssessment or a LessonAssessment (exactly one)."""
	general_assessment = models.ForeignKey(GeneralAssessment, on_delete=models.CASCADE, related_name='questions', null=True, blank=True)
	lesson_assessment = models.ForeignKey(LessonAssessment, on_delete=models.CASCADE, related_name='questions', null=True, blank=True)

	type = models.CharField(max_length=40, choices=[(qt.value, qt.value) for qt in QTypeEnum])
	question = models.TextField()
	answer = models.CharField(max_length=500, blank=True, default="")

	class Meta:
		constraints = [
			models.CheckConstraint(
				check=(
					# XOR: one and only one FK is set
					(Q(general_assessment__isnull=False) & Q(lesson_assessment__isnull=True)) |
					(Q(general_assessment__isnull=True) & Q(lesson_assessment__isnull=False))
				),
				name="question_exactly_one_assessment_set",
			)
		]

	def __str__(self) -> str:
		return f"Question: {self.type}"


class Option(TimestampedModel):
	question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name='options')
	value = models.CharField(max_length=500)

	def __str__(self) -> str:
		return self.value
