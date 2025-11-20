from django.contrib import admin

from .models import (
	Objective, Subject, Topic, Period, LessonResource, TakeLesson,
	GeneralAssessment, GeneralAssessmentGrade, AssessmentSolution,
	LessonAssessment, LessonAssessmentGrade,
	Question, Option, GameModel,
)


@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
	list_display = ("id", "name", "grade", "created_at")
	list_filter = ("grade",)
	search_fields = ("name",)

@admin.register(Objective)
class ObjectiveAdmin(admin.ModelAdmin):
	list_display = ("id", "text", "created_at")
	search_fields = ("text",)

@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
	list_display = ("id", "name", "subject", "created_at")
	list_filter = ("subject",)
	search_fields = ("name",)


@admin.register(Period)
class PeriodAdmin(admin.ModelAdmin):
	list_display = ("id", "name", "start_month", "end_month", "created_at")


@admin.register(LessonResource)
class LessonResourceAdmin(admin.ModelAdmin):
	list_display = ("id", "title", "subject", "topic", "type", "status", "created_at")
	list_filter = ("subject", "type", "status")
	search_fields = ("title",)


@admin.register(TakeLesson)
class TakeLessonAdmin(admin.ModelAdmin):
	list_display = ("id", "student", "lesson", "created_at")
	list_filter = ("lesson",)


@admin.register(GeneralAssessment)
class GeneralAssessmentAdmin(admin.ModelAdmin):
	list_display = ("id", "title", 'type', "given_by", "marks", "created_at")
	list_filter = ("given_by",)
	search_fields = ("title",)


@admin.register(GeneralAssessmentGrade)
class GeneralAssessmentGradeAdmin(admin.ModelAdmin):
	list_display = ("id", "assessment", "student", "solution", "score", "created_at")
	list_filter = ("assessment",)


@admin.register(AssessmentSolution)
class AssessmentSolutionAdmin(admin.ModelAdmin):
	list_display = ("id", "assessment", "student", "submitted_at", "created_at")
	list_filter = ("assessment", "student")


@admin.register(LessonAssessment)
class LessonAssessmentAdmin(admin.ModelAdmin):
	list_display = ("id", "title", 'type', "lesson", "given_by", "marks", "created_at")
	list_filter = ("lesson", "given_by")
	search_fields = ("title",)


@admin.register(LessonAssessmentGrade)
class LessonAssessmentGradeAdmin(admin.ModelAdmin):
	list_display = ("id", "lesson_assessment", "student", "score", "created_at")
	list_filter = ("lesson_assessment",)


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
	list_display = ("id", "type", "general_assessment", "lesson_assessment", "created_at")
	list_filter = ("type",)
	search_fields = ("question",)


@admin.register(Option)
class OptionAdmin(admin.ModelAdmin):
	list_display = ("id", "question", "value", "created_at")


@admin.register(GameModel)
class GameModelAdmin(admin.ModelAdmin):
	list_display = ("id", "name", "type", 'grade', "created_by", "created_at")
	list_filter = ("type", "grade")
	search_fields = ("name", "description", "instructions")
