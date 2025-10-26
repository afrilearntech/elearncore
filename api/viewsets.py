from typing import Iterable

from rest_framework import permissions, viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response

from elearncore.sysutils.constants import UserRole, Status as StatusEnum

from content.models import (
	Subject, Topic, Period, LessonResource, TakeLesson,
)
from content.serializers import (
	SubjectSerializer, TopicSerializer, PeriodSerializer, LessonResourceSerializer, TakeLessonSerializer,
)
from agentic.models import AIRecommendation, AIAbuseReport
from agentic.serializers import AIRecommendationSerializer, AIAbuseReportSerializer


# ----- Permissions -----
def _user_role_in(user, roles: Iterable[str]) -> bool:
	try:
		return (user and getattr(user, 'role', None) in roles)
	except Exception:
		return False


class CanCreateContent(permissions.BasePermission):
	"""Allow writes if the user has a content-creation capable role."""
	allowed_roles = {
		UserRole.CONTENTCREATOR.value,
		UserRole.CONTENTVALIDATOR.value,
		UserRole.TEACHER.value,
		UserRole.ADMIN.value,
	}

	def has_permission(self, request, view):
		if request.method in permissions.SAFE_METHODS:
			return True
		return _user_role_in(request.user, self.allowed_roles)


class CanModerateContent(permissions.BasePermission):
	"""Restrict moderation endpoints to validators & admins."""
	allowed_roles = {
		UserRole.CONTENTVALIDATOR.value,
		UserRole.ADMIN.value,
	}

	def has_permission(self, request, view):
		return _user_role_in(request.user, self.allowed_roles)


# ----- ViewSets -----
class SubjectViewSet(viewsets.ModelViewSet):
	queryset = Subject.objects.all().prefetch_related('teachers')
	serializer_class = SubjectSerializer
	permission_classes = [permissions.IsAuthenticatedOrReadOnly, CanCreateContent]
	filter_backends = [filters.SearchFilter, filters.OrderingFilter]
	search_fields = ['name', 'description']
	ordering_fields = ['name', 'created_at']


class TopicViewSet(viewsets.ModelViewSet):
	queryset = Topic.objects.select_related('subject').all()
	serializer_class = TopicSerializer
	permission_classes = [permissions.IsAuthenticatedOrReadOnly, CanCreateContent]
	filter_backends = [filters.SearchFilter, filters.OrderingFilter]
	search_fields = ['name']
	ordering_fields = ['name', 'created_at']


class PeriodViewSet(viewsets.ModelViewSet):
	queryset = Period.objects.all()
	serializer_class = PeriodSerializer
	filter_backends = [filters.SearchFilter, filters.OrderingFilter]
	search_fields = ['name']
	ordering_fields = ['start_month', 'end_month', 'created_at']

	def get_permissions(self):
		if self.request.method in permissions.SAFE_METHODS:
			return [permissions.IsAuthenticatedOrReadOnly()]
		return [permissions.IsAuthenticated(), CanModerateContent()]


class LessonResourceViewSet(viewsets.ModelViewSet):
	queryset = LessonResource.objects.select_related('subject', 'topic', 'period', 'created_by').prefetch_related('subject__teachers')
	serializer_class = LessonResourceSerializer
	filter_backends = [filters.SearchFilter, filters.OrderingFilter]
	search_fields = ['title', 'description']
	ordering_fields = ['created_at', 'updated_at', 'title']

	def get_permissions(self):
		if self.action in ['approve', 'reject', 'request_changes']:
			return [permissions.IsAuthenticated(), CanModerateContent()]
		elif self.request.method in permissions.SAFE_METHODS:
			return [permissions.IsAuthenticatedOrReadOnly()]
		else:
			return [permissions.IsAuthenticated(), CanCreateContent()]

	def perform_create(self, serializer):
		serializer.save(created_by=self.request.user, status=StatusEnum.DRAFT.value)

	@action(detail=True, methods=['post'], url_path='submit')
	def submit_for_review(self, request, pk=None):
		obj = self.get_object()
		obj.status = StatusEnum.PENDING.value
		obj.save(update_fields=['status', 'updated_at'])
		return Response({'status': obj.status})

	@action(detail=True, methods=['post'])
	def approve(self, request, pk=None):
		obj = self.get_object()
		obj.status = StatusEnum.APPROVED.value
		obj.save(update_fields=['status', 'updated_at'])
		return Response({'status': obj.status})

	@action(detail=True, methods=['post'])
	def reject(self, request, pk=None):
		obj = self.get_object()
		obj.status = StatusEnum.REJECTED.value
		obj.save(update_fields=['status', 'updated_at'])
		return Response({'status': obj.status})

	@action(detail=True, methods=['post'], url_path='request-changes')
	def request_changes(self, request, pk=None):
		obj = self.get_object()
		obj.status = StatusEnum.REVIEW_REQUESTED.value
		obj.save(update_fields=['status', 'updated_at'])
		return Response({'status': obj.status})


class TakeLessonViewSet(viewsets.ModelViewSet):
	queryset = TakeLesson.objects.select_related('student__user', 'lesson')
	serializer_class = TakeLessonSerializer
	permission_classes = [permissions.IsAuthenticated]
	filter_backends = [filters.OrderingFilter]
	ordering_fields = ['created_at']

	def get_queryset(self):
		qs = super().get_queryset()
		user = self.request.user
		elevated = _user_role_in(user, {
			UserRole.ADMIN.value,
			UserRole.TEACHER.value,
			UserRole.CONTENTVALIDATOR.value,
		})
		if elevated:
			return qs
		student = getattr(user, 'student', None)
		if student:
			return qs.filter(student=student)
		return TakeLesson.objects.none()


class AIRecommendationViewSet(viewsets.ReadOnlyModelViewSet):
	queryset = AIRecommendation.objects.select_related('student__user', 'lesson')
	serializer_class = AIRecommendationSerializer
	permission_classes = [permissions.IsAuthenticated]
	filter_backends = [filters.SearchFilter, filters.OrderingFilter]
	search_fields = ['message', 'lesson__title', 'student__user__name']
	ordering_fields = ['created_at']

	def get_queryset(self):
		qs = super().get_queryset()
		user = self.request.user
		student = getattr(user, 'student', None)
		if student:
			return qs.filter(student=student)
		if user and getattr(user, 'role', None) in {UserRole.ADMIN.value, UserRole.CONTENTVALIDATOR.value, UserRole.TEACHER.value}:
			return qs
		return AIRecommendation.objects.none()


class AIAbuseReportViewSet(viewsets.ReadOnlyModelViewSet):
	queryset = AIAbuseReport.objects.select_related('forum')
	serializer_class = AIAbuseReportSerializer
	permission_classes = [permissions.IsAuthenticated]
	filter_backends = [filters.SearchFilter, filters.OrderingFilter]
	search_fields = ['tag', 'description', 'sample_msg']
	ordering_fields = ['created_at']

	def get_queryset(self):
		qs = super().get_queryset()
		user = self.request.user
		if user and getattr(user, 'role', None) in {UserRole.ADMIN.value, UserRole.CONTENTVALIDATOR.value}:
			return qs
		return AIAbuseReport.objects.none()

