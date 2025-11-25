from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User, OTP, County, District, School, Student, Teacher, Parent


@admin.register(User)
class UserAdmin(BaseUserAdmin):
	model = User
	list_display = ("id", "name", "phone", "email", "role", "is_active", "is_staff", "created_at")
	list_filter = ("role", "is_active", "is_staff", "is_superuser")
	search_fields = ("name", "phone", "email")
	ordering = ("-created_at",)
	fieldsets = (
		(None, {"fields": ("phone", "password")} ),
		("Personal info", {"fields": ("name", "email", "role")} ),
		("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")} ),
		("Important dates", {"fields": ("last_login", "created_at", "updated_at")} ),
	)
	readonly_fields = ("created_at", "updated_at")
	add_fieldsets = (
		(None, {
			'classes': ('wide',),
			'fields': ('phone', 'name', 'email', 'role', 'password1', 'password2', 'is_staff', 'is_superuser', 'is_active')
		}),
	)


@admin.register(OTP)
class OTPAdmin(admin.ModelAdmin):
	list_display = ("phone", "otp", "created_at")
	search_fields = ("phone",)


@admin.register(County)
class CountyAdmin(admin.ModelAdmin):
	list_display = ("id", "name", "status", "created_at")
	list_filter = ("status",)
	search_fields = ("name",)


@admin.register(District)
class DistrictAdmin(admin.ModelAdmin):
	list_display = ("id", "name", "county", "status", "created_at")
	list_filter = ("county", "status")
	search_fields = ("name",)


@admin.register(School)
class SchoolAdmin(admin.ModelAdmin):
	list_display = ("id", "name", "district", "status", "created_at")
	list_filter = ("district", "status")
	search_fields = ("name",)


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
	list_display = ("id", "profile", "school", "grade", "status", "created_at")
	list_filter = ("grade", "school", "status")
	search_fields = ("profile__name", "profile__phone")


@admin.register(Teacher)
class TeacherAdmin(admin.ModelAdmin):
	list_display = ("id", "profile", "school", "created_at")
	list_filter = ("school",)
	search_fields = ("profile__name", "profile__phone")


@admin.register(Parent)
class ParentAdmin(admin.ModelAdmin):
	list_display = ("id", "profile", "created_at")
	search_fields = ("profile__name", "profile__phone")
