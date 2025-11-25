from django.db import models
from django.db.models import Q
from django.conf import settings

from elearncore.sysutils.constants import (
	AssessmentType,
	ContentType as ContentTypeEnum,
	GameType,
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


class GameModel(TimestampedModel):
	name = models.CharField(max_length=150)
	instructions = models.TextField(blank=True, default="")
	description = models.TextField(blank=True, default="")
	grade = models.CharField(max_length=20, choices=[(lvl.value, lvl.value) for lvl in StudentLevel], default=StudentLevel.GRADE2.value)
	hint = models.CharField(max_length=250, blank=True, default="")
	correct_answer = models.CharField(max_length=150)
	type = models.CharField(max_length=50, choices=[(gt.value, gt.value) for gt in GameType])
	image = models.ImageField(upload_to='word_games/', null=True, blank=True)
	created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_games')
	status = models.CharField(max_length=30, choices=[(s.value, s.value) for s in StatusEnum], default=StatusEnum.DRAFT.value)

	def __str__(self) -> str:
		return self.name


class GamePlay(TimestampedModel):
	"""Track when a student plays a particular game.

	One row per student/game pair, updated with latest play timestamp.
	"""
	student = models.ForeignKey('accounts.Student', on_delete=models.CASCADE, related_name='played_games')
	game = models.ForeignKey(GameModel, on_delete=models.CASCADE, related_name='plays')
	last_played_at = models.DateTimeField(auto_now=True)

	class Meta:
		unique_together = ("student", "game")
		indexes = [
			models.Index(fields=["student", "game"]),
		]

	def __str__(self) -> str:
		return f"{getattr(self.student.profile, 'name', 'Student')} -> {self.game.name}"


class Objective(TimestampedModel):
	"""A reusable learning objective that can be linked to subjects."""
	text = models.TextField()

	class Meta:
		ordering = ["id"]

	def __str__(self) -> str:
		return (self.text[:47] + "...") if len(self.text) > 50 else self.text


class Subject(TimestampedModel):
	name = models.CharField(max_length=120)
	grade = models.CharField(max_length=20, choices=[(lvl.value, lvl.value) for lvl in StudentLevel])
	description = models.TextField(blank=True, default="")
	thumbnail = models.ImageField(upload_to='thumbnails/subjects/', null=True, blank=True)
	created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_subjects')
	# Many-to-many link to objectives; a subject may have many objectives,
	# and an objective string can be reused across subjects.
	objectives = models.ManyToManyField(Objective, related_name="subjects", blank=True)
	status = models.CharField(max_length=30, choices=[(s.value, s.value) for s in StatusEnum], default=StatusEnum.PENDING.value)

	# Allow teachers to be linked to one or more subjects
	teachers = models.ManyToManyField('accounts.Teacher', related_name='subjects', blank=True)
	moderation_comment = models.TextField(blank=True, default="")

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
	instructor_name = models.CharField(max_length=150, blank=True, default="")
	title = models.CharField(max_length=200)
	description = models.TextField(blank=True, default="")
	type = models.CharField(max_length=20, choices=[(t.value, t.value) for t in ContentTypeEnum])
	status = models.CharField(max_length=30, choices=[(s.value, s.value) for s in StatusEnum], default=StatusEnum.DRAFT.value)
	resource = models.FileField(upload_to='lesson_resources/')
	thumbnail = models.ImageField(upload_to='thumbnails/lessons/', null=True, blank=True)
	created_by = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='created_lessons')
	duration_minutes = models.PositiveIntegerField(null=True, blank=True, help_text="Estimated duration in minutes")
	moderation_comment = models.TextField(blank=True, default="")

	def __str__(self) -> str:
		return self.title

	class Meta:
		indexes = [
			models.Index(fields=["subject", "created_at"]),
		]


class TakeLesson(TimestampedModel):
	student = models.ForeignKey('accounts.Student', on_delete=models.CASCADE, related_name='taken_lessons')
	lesson = models.ForeignKey(LessonResource, on_delete=models.CASCADE, related_name='taken_by')

	class Meta:
		unique_together = ("student", "lesson")
		indexes = [
			models.Index(fields=["student", "created_at"]),
			models.Index(fields=["lesson", "student"]),
		]

	def __str__(self) -> str:
		return f"{getattr(self.student.profile, 'name', 'Student')} -> {self.lesson.title}"


# Assessments
class GeneralAssessment(TimestampedModel):
	title = models.CharField(max_length=200)
	given_by = models.ForeignKey('accounts.Teacher', on_delete=models.SET_NULL, null=True, related_name='general_assessments')
	instructions = models.TextField(blank=True, default="")
	type = models.CharField(max_length=30, choices=[(t.value, t.value) for t in AssessmentType], default=AssessmentType.ASSIGNMENT.value)
	marks = models.FloatField(default=0.0)
	due_at = models.DateTimeField(null=True, blank=True)
	# Optional grade scoping; when null, assessment is global
	grade = models.CharField(max_length=20, choices=[(lvl.value, lvl.value) for lvl in StudentLevel], null=True, blank=True)
	moderation_comment = models.TextField(blank=True, default="")
	status = models.CharField(max_length=30, choices=[(s.value, s.value) for s in StatusEnum], default=StatusEnum.DRAFT.value)

	def __str__(self) -> str:
		return self.title

	class Meta:
		indexes = [
			models.Index(fields=["due_at"]),
			models.Index(fields=["grade", "due_at"]),
		]

class AssessmentSolution(TimestampedModel):
	assessment = models.ForeignKey(GeneralAssessment, on_delete=models.CASCADE, related_name='solutions')
	student = models.ForeignKey('accounts.Student', on_delete=models.CASCADE, related_name='assessment_solutions')
	solution = models.TextField(blank=True, default="")
	attachment = models.FileField(upload_to='assessment_solutions/')
	submitted_at = models.DateTimeField(auto_now_add=True)

	def __str__(self) -> str:
		return f"Solution by {getattr(self.student.profile, 'name', 'Student')} for {self.assessment.title}"

class GeneralAssessmentGrade(TimestampedModel):
	assessment = models.ForeignKey(GeneralAssessment, on_delete=models.CASCADE, related_name='grades')
	student = models.ForeignKey('accounts.Student', on_delete=models.CASCADE, related_name='general_assessment_grades')
	solution = models.OneToOneField('AssessmentSolution', on_delete=models.SET_NULL, null=True, blank=True, related_name='grade')
	score = models.FloatField()

	class Meta:
		unique_together = ("assessment", "student")

	def __str__(self) -> str:
		return f"{getattr(self.student.profile, 'name', 'Student')}: {self.score} / {self.assessment.marks}"


class LessonAssessment(TimestampedModel):
	lesson = models.ForeignKey(LessonResource, on_delete=models.CASCADE, related_name='assessments')
	given_by = models.ForeignKey('accounts.Teacher', on_delete=models.SET_NULL, null=True, related_name='lesson_assessments')
	title = models.CharField(max_length=200)
	type = models.CharField(max_length=30, choices=[(t.value, t.value) for t in AssessmentType], default=AssessmentType.QUIZ.value)
	instructions = models.TextField(blank=True, default="")
	marks = models.FloatField(default=0.0)
	due_at = models.DateTimeField(null=True, blank=True)
	moderation_comment = models.TextField(blank=True, default="")
	status = models.CharField(max_length=30, choices=[(s.value, s.value) for s in StatusEnum], default=StatusEnum.DRAFT.value)

	def __str__(self) -> str:
		return self.title

	class Meta:
		indexes = [
			models.Index(fields=["due_at"]),
			models.Index(fields=["lesson", "due_at"]),
		]


class LessonAssessmentGrade(TimestampedModel):
	lesson_assessment = models.ForeignKey(LessonAssessment, on_delete=models.CASCADE, related_name='grades')
	student = models.ForeignKey('accounts.Student', on_delete=models.CASCADE, related_name='lesson_assessment_grades')
	score = models.FloatField()

	class Meta:
		unique_together = ("lesson_assessment", "student")

	def __str__(self) -> str:
		return f"{getattr(self.student.profile, 'name', 'Student')}: {self.score} / {self.lesson_assessment.marks}"


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


class Activity(TimestampedModel):
	"""A generic user activity for audit and feeds.

	Examples: login, take_lesson, play_game
	"""
	user = models.ForeignKey('accounts.User', on_delete=models.CASCADE, related_name='activities')
	type = models.CharField(max_length=50)
	description = models.CharField(max_length=255, blank=True, default="")
	metadata = models.JSONField(blank=True, null=True)

	class Meta:
		indexes = [
			models.Index(fields=["user", "created_at"]),
		]

	def __str__(self) -> str:
		return f"{self.user_id} - {self.type}"
