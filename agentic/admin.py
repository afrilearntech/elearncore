from django.contrib import admin

from .models import AIRecommendation, AIAbuseReport


@admin.register(AIRecommendation)
class AIRecommendationAdmin(admin.ModelAdmin):
	list_display = ("id", "student", "lesson", "created_at")
	list_filter = ("lesson",)


@admin.register(AIAbuseReport)
class AIAbuseReportAdmin(admin.ModelAdmin):
	list_display = ("id", "tag", "forum", "created_at")
	list_filter = ("forum",)
	search_fields = ("tag", "description")
