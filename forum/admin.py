from django.contrib import admin

from .models import Forum, ForumMembership, Chat


@admin.register(Forum)
class ForumAdmin(admin.ModelAdmin):
	list_display = ("id", "name", "created_at")
	search_fields = ("name",)


@admin.register(ForumMembership)
class ForumMembershipAdmin(admin.ModelAdmin):
	list_display = ("id", "forum", "student", "created_at")
	list_filter = ("forum",)


@admin.register(Chat)
class ChatAdmin(admin.ModelAdmin):
	list_display = ("id", "sender", "forum", "created_at")
	list_filter = ("forum",)
	search_fields = ("sender__name", "content")
