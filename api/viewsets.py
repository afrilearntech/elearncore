import csv
import io
from typing import Iterable, Dict, List, Set

from rest_framework import permissions, viewsets, status, filters, serializers
from drf_spectacular.utils import extend_schema, OpenApiResponse, OpenApiExample, OpenApiParameter
from django.conf import settings
from django.core.mail import send_mail
from django.http import HttpResponse
from rest_framework.decorators import action
from rest_framework.response import Response
from django.views.decorators.cache import cache_page
from django.utils.decorators import method_decorator
from django.utils.dateparse import parse_date
from django.utils import timezone
from datetime import timedelta, datetime
from django.db import models
from django.db.models import Q, Count, Window, F
from django.db.models.functions import TruncDate, DenseRank

from elearncore.sysutils.constants import UserRole, Status as StatusEnum
from elearncore.sysutils.tasks import fire_and_forget

from content.models import (
	Subject, Topic, Period, LessonResource, TakeLesson, LessonAssessment,
	GeneralAssessment, GeneralAssessmentGrade, LessonAssessmentGrade,
	Question,
	GameModel, Activity, AssessmentSolution, GamePlay,
)
from forum.models import Chat
from django.core.cache import cache
from content.serializers import (
	AssessmentSolutionSerializer,
	SubjectSerializer,
	SubjectWriteSerializer,
	TopicSerializer,
	PeriodSerializer,
	LessonResourceSerializer,
	TakeLessonSerializer,
	GeneralAssessmentSerializer,
	LessonAssessmentSerializer,
	QuestionSerializer,
	OptionSerializer,
	QuestionCreateSerializer,
	GameSerializer,
)
from agentic.models import AIRecommendation, AIAbuseReport
from agentic.serializers import AIRecommendationSerializer, AIAbuseReportSerializer
from knox.models import AuthToken
from accounts.models import User, Student, Teacher, Parent, School, County, District
from accounts.serializers import (
	SchoolLookupSerializer, CountyLookupSerializer, DistrictLookupSerializer,
	CountySerializer, DistrictSerializer, SchoolSerializer,
	StudentSerializer, TeacherSerializer, UserSerializer,
)
from .serializers import (
	ProfileSetupSerializer,
	UserRoleSerializer,
	AboutUserSerializer,
	LinkChildSerializer,
	LoginSerializer,
	ChangePasswordSerializer,
	ContentModerationSerializer,
	ContentModerationResponseSerializer,
	ContentAssessmentItemSerializer,
	ContentDashboardSerializer,
	TeacherCreateStudentSerializer,
	TeacherBulkStudentUploadSerializer,
	ContentCreateTeacherSerializer,
	ContentBulkTeacherUploadSerializer,
	AssignSubjectsToTeacherSerializer,
	AdminCreateContentManagerSerializer,
	AdminBulkContentManagerUploadSerializer,
	AdminContentManagerListSerializer,
	AdminStudentListSerializer,
	AdminParentListSerializer,
	GradeAssessmentSerializer,
	AdminDashboardSerializer,
)
from messsaging.services import send_sms


def _parse_bulk_date(value: str):
	"""Parse flexible date formats from CSV into a date object or ISO string.

	Accepted formats include:
	- YYYY-MM-DD (returned as-is)
	- DD/MM/YYYY, MM/DD/YYYY
	- DD-MM-YYYY, MM-DD-YYYY

	If parsing fails, the original value is returned so DRF validation
	can surface a clear error message.
	"""
	if not value:
		return None
	value = str(value).strip()
	if not value:
		return None
	# Already in ISO format
	if len(value) == 10 and value[4] == "-" and value[7] == "-":
		return value

	for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y"):
		try:
			parsed = datetime.strptime(value, fmt).date()
			return parsed
		except ValueError:
			continue
	return value


def _send_account_notifications(message: str, phone: str | None, email: str | None, email_subject: str) -> None:
	"""Send SMS and email notifications for new accounts.

	Designed to be called via ``fire_and_forget`` so that SMS/email I/O
	does not block API responses. All exceptions are swallowed inside the
	send functions to avoid impacting the caller.
	"""
	try:
		if phone:
			# send_sms expects an iterable of recipients
			send_sms(message, [phone])
	except Exception:
		pass

	try:
		if email:
			from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None) or None
			if from_email:
				send_mail(
					subject=email_subject,
					message=message,
					from_email=from_email,
					recipient_list=[email],
					fail_silently=True,
				)
	except Exception:
		pass


class ParentChildSerializer(serializers.Serializer):
	name = serializers.CharField()
	student_id = serializers.CharField()
	student_db_id = serializers.IntegerField()
	grade = serializers.CharField(allow_null=True)
	school = serializers.CharField(allow_null=True)


class ParentGradeOverviewSerializer(serializers.Serializer):
	child_name = serializers.CharField()
	student_id = serializers.CharField()
	subject = serializers.CharField()
	overall_score = serializers.FloatField()
	score_grade = serializers.CharField()
	score_remark = serializers.CharField()


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


class IsAdminRole(permissions.BasePermission):
	"""Allow only ADMIN role users."""

	def has_permission(self, request, view):
		return _user_role_in(request.user, {UserRole.ADMIN.value})


class IsContentCreator(permissions.BasePermission):
	"""Allow content creators, validators, teachers, and admins (for writes)."""
	allowed_roles = {
		UserRole.CONTENTCREATOR.value,
		UserRole.CONTENTVALIDATOR.value,
		UserRole.TEACHER.value,
		UserRole.ADMIN.value,
	}

	def has_permission(self, request, view):
		return _user_role_in(request.user, self.allowed_roles)


class IsContentValidator(permissions.BasePermission):
	"""Allow validators and admins for moderation actions."""
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

	# Cache list & retrieve for short periods (safe/public reads)
	@method_decorator(cache_page(60 * 5), name='list')
	@method_decorator(cache_page(60 * 10), name='retrieve')
	def dispatch(self, *args, **kwargs):
		return super().dispatch(*args, **kwargs)

	def retrieve(self, request, *args, **kwargs):
		"""Return subject detail plus basic aggregated stats."""
		instance: Subject = self.get_object()
		data = self.get_serializer(instance).data

		# Attach topics for this subject
		topics_qs = Topic.objects.filter(subject=instance).order_by('name')
		data['topics'] = TopicSerializer(topics_qs, many=True).data

		# Total instructors linked to this subject
		total_instructors = instance.teachers.count()

		# Total lessons under this subject
		total_lessons = LessonResource.objects.filter(subject=instance).count()

		# Total distinct students who have taken at least one lesson in this course
		total_students = (
			TakeLesson.objects
			.filter(lesson__subject=instance)
			.values('student_id')
			.distinct()
			.count()
		)

		# Estimated duration for the subject in hours, based on lesson durations
		total_minutes = (
			LessonResource.objects
			.filter(subject=instance)
			.aggregate(total=models.Sum('duration_minutes'))['total'] or 0
		)
		estimated_duration_hours = round(total_minutes / 60.0, 2)

		data['stats'] = {
			'total_instructors': total_instructors,
			'total_lessons': total_lessons,
			'total_students': total_students,
			'estimated_duration_hours': estimated_duration_hours,
		}
		return Response(data)

	def perform_create(self, serializer):
		# Set created_by to the authenticated user creating the subject
		serializer.save(created_by=self.request.user)

	@action(detail=False, methods=['get'], url_path='mysubjects', permission_classes=[permissions.IsAuthenticated])
	def mysubjects(self, request):
		"""Return subjects the student has touched, with progress percent.
		Progress = distinct lessons taken in subject / total lessons in subject.
		"""
		user: User = request.user
		student = getattr(user, 'student', None)
		if not student:
			return Response({"detail": "Student profile required."}, status=403)

		# Lessons taken by this student, grouped by subject
		taken_counts = (
			TakeLesson.objects
			.filter(student=student)
			.values('lesson__subject_id')
			.annotate(taken=Count('lesson_id', distinct=True))
		)
		subject_ids = [row['lesson__subject_id'] for row in taken_counts]
		if not subject_ids:
			return Response([])

		# Total lessons per subject (only for touched subjects)
		total_counts_qs = (
			LessonResource.objects
			.filter(subject_id__in=subject_ids)
			.values('subject_id')
			.annotate(total=Count('id'))
		)
		total_by_subject = {row['subject_id']: row['total'] for row in total_counts_qs}
		taken_by_subject = {row['lesson__subject_id']: row['taken'] for row in taken_counts}

		subjects = Subject.objects.filter(id__in=subject_ids).only('id', 'name', 'grade')
		payload = []
		for subj in subjects:
			total = int(total_by_subject.get(subj.id, 0))
			taken = int(taken_by_subject.get(subj.id, 0))
			percent = int(round((taken / total) * 100)) if total else 0
			payload.append({
				'id': subj.id,
				'name': subj.name,
				'grade': getattr(subj, 'grade', None),
				'creator': getattr(subj.created_by, 'name', None),
				'total_lessons': total,
				'taken_lessons': taken,
				'progress_percent': percent,
			})

		# Sort by name for stable output
		payload.sort(key=lambda x: (x['name'] or '').lower())
		return Response(payload)

	




class TopicViewSet(viewsets.ModelViewSet):
	queryset = Topic.objects.select_related('subject').all()
	serializer_class = TopicSerializer
	permission_classes = [permissions.IsAuthenticatedOrReadOnly, CanCreateContent]
	filter_backends = [filters.SearchFilter, filters.OrderingFilter]
	search_fields = ['name']
	ordering_fields = ['name', 'created_at']

	def get_queryset(self):
		"""Optionally filter topics by subject id via ?subject=<id>."""
		qs = super().get_queryset()
		subject_id = self.request.query_params.get('subject') if hasattr(self, 'request') else None
		if subject_id:
			try:
				qs = qs.filter(subject_id=int(subject_id))
			except (TypeError, ValueError):
				# Ignore invalid subject values and return the unfiltered queryset
				pass
		return qs

	@method_decorator(cache_page(60 * 5), name='list')
	@method_decorator(cache_page(60 * 10), name='retrieve')
	def dispatch(self, *args, **kwargs):
		return super().dispatch(*args, **kwargs)


@method_decorator(cache_page(60 * 15), name='list')
@method_decorator(cache_page(60 * 15), name='retrieve')
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

	@method_decorator(cache_page(60 * 2), name='list')
	@method_decorator(cache_page(60 * 5), name='retrieve')
	def dispatch(self, *args, **kwargs):
		return super().dispatch(*args, **kwargs)

	def get_permissions(self):
		if self.action in ['approve', 'reject', 'request_changes']:
			return [permissions.IsAuthenticated(), CanModerateContent()]
		elif self.request.method in permissions.SAFE_METHODS:
			return [permissions.IsAuthenticatedOrReadOnly()]
		else:
			return [permissions.IsAuthenticated(), CanCreateContent()]

	def perform_create(self, serializer):
		lesson = serializer.save(created_by=self.request.user, status=StatusEnum.DRAFT.value)
		# Optionally log content creation as an activity for the creator
		if self.request.user and self.request.user.is_authenticated:
			Activity.objects.create(
				user=self.request.user,
				type="create_lesson",
				description=f"Created lesson '{lesson.title}'",
				metadata={"lesson_id": lesson.id},
			)

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
	queryset = TakeLesson.objects.select_related('student__profile', 'lesson')
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

	def perform_create(self, serializer):
		"""Create a TakeLesson and log an activity for the student."""
		instance: TakeLesson = serializer.save()
		user = getattr(getattr(instance, 'student', None), 'profile', None)
		if user is not None:
			Activity.objects.create(
				user=user,
				type="take_lesson",
				description=f"Took lesson '{instance.lesson.title}'",
				metadata={"lesson_id": instance.lesson_id, "subject_id": getattr(instance.lesson.subject, 'id', None)},
			)


class AIRecommendationViewSet(viewsets.ReadOnlyModelViewSet):
	queryset = AIRecommendation.objects.select_related('student__profile', 'lesson')
	serializer_class = AIRecommendationSerializer
	permission_classes = [permissions.IsAuthenticated]
	filter_backends = [filters.SearchFilter, filters.OrderingFilter]
	search_fields = ['message', 'lesson__title', 'student__profile__name']
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


class ParentDashboardChildSerializer(serializers.Serializer):
	name = serializers.CharField(allow_null=True)
	student_id = serializers.CharField(allow_null=True)
	student_db_id = serializers.IntegerField()
	grade = serializers.CharField(allow_null=True)
	school = serializers.CharField(allow_null=True)


class ParentDashboardGradeOverviewSerializer(serializers.Serializer):
	child_name = serializers.CharField(allow_null=True)
	student_id = serializers.CharField(allow_null=True)
	subject = serializers.CharField()
	overall_score = serializers.FloatField()
	score_grade = serializers.CharField()
	score_remark = serializers.CharField()


class ParentDashboardSerializer(serializers.Serializer):
	children = ParentDashboardChildSerializer(many=True)
	grades_overview = ParentDashboardGradeOverviewSerializer(many=True)


class ParentAssessmentItemSerializer(serializers.Serializer):
	child_name = serializers.CharField(allow_null=True)
	assessment_title = serializers.CharField()
	subject = serializers.CharField(allow_null=True)
	assessment_type = serializers.CharField()
	assessment_score = serializers.FloatField()
	child_score = serializers.FloatField(allow_null=True)
	assessment_status = serializers.CharField()
	start_date = serializers.DateTimeField()
	due_date = serializers.DateTimeField(allow_null=True)


class ParentAssessmentsSummarySerializer(serializers.Serializer):
	completed = serializers.IntegerField()
	pending = serializers.IntegerField()
	in_progress = serializers.IntegerField()


class ParentAssessmentsResponseSerializer(serializers.Serializer):
	assessments = ParentAssessmentItemSerializer(many=True)
	summary = ParentAssessmentsSummarySerializer()


class ParentSubmissionSolutionSerializer(serializers.Serializer):
	solution = serializers.CharField(allow_null=True)
	attachment = serializers.CharField(allow_null=True)


class ParentSubmissionItemSerializer(serializers.Serializer):
	child_name = serializers.CharField(allow_null=True)
	assessment_title = serializers.CharField(allow_null=True)
	subject = serializers.CharField(allow_null=True)
	score = serializers.FloatField(allow_null=True)
	assessment_score = serializers.FloatField()
	submission_status = serializers.CharField()
	solution = ParentSubmissionSolutionSerializer()
	date_submitted = serializers.DateTimeField()


class ParentSubmissionsSummarySerializer(serializers.Serializer):
	graded = serializers.IntegerField()
	pending = serializers.IntegerField()


class ParentSubmissionsResponseSerializer(serializers.Serializer):
	submissions = ParentSubmissionItemSerializer(many=True)
	summary = ParentSubmissionsSummarySerializer()


class ParentAnalyticsSummaryCardsSerializer(serializers.Serializer):
	total_assessments = serializers.IntegerField()
	total_completed_assessments = serializers.IntegerField()
	overall_average_score = serializers.FloatField()
	total_subjects_touched = serializers.IntegerField()
	estimated_total_hours = serializers.FloatField()


class ParentAnalyticsTimeItemSerializer(serializers.Serializer):
	subject = serializers.CharField(allow_null=True)
	time = serializers.CharField()
	percentage = serializers.FloatField()


class ParentAnalyticsResponseSerializer(serializers.Serializer):
	summarycards = ParentAnalyticsSummaryCardsSerializer()
	estimated_time_spent = ParentAnalyticsTimeItemSerializer(many=True)


class TeacherGradeItemSerializer(serializers.Serializer):
	student_name = serializers.CharField(allow_null=True)
	student_id = serializers.CharField(allow_null=True)
	subject = serializers.CharField(allow_null=True)
	grade_letter = serializers.CharField()
	percentage = serializers.FloatField()
	status = serializers.CharField()
	updated_at = serializers.DateTimeField()


class TeacherGradesSummarySerializer(serializers.Serializer):
	total_grades = serializers.IntegerField()
	excellent = serializers.IntegerField()
	good = serializers.IntegerField()
	needs_improvement = serializers.IntegerField()


class TeacherGradesResponseSerializer(serializers.Serializer):
	summary = TeacherGradesSummarySerializer()
	grades = TeacherGradeItemSerializer(many=True)


class TeacherDashboardSummaryCardsSerializer(serializers.Serializer):
	total_students = serializers.IntegerField()
	class_average = serializers.FloatField()
	pending_review = serializers.IntegerField()
	completion_rate = serializers.FloatField()


class TeacherDashboardTopPerformerSerializer(serializers.Serializer):
	student_name = serializers.CharField(allow_null=True)
	student_id = serializers.CharField(allow_null=True)
	percentage = serializers.FloatField()
	improvement = serializers.FloatField()


class TeacherDashboardPendingSubmissionSerializer(serializers.Serializer):
	student_name = serializers.CharField(allow_null=True)
	student_id = serializers.CharField(allow_null=True)
	assessment_title = serializers.CharField()
	subject = serializers.CharField(allow_null=True)
	due_at = serializers.DateTimeField(allow_null=True)
	submitted_at = serializers.DateTimeField()


class TeacherDashboardUpcomingDeadlineSerializer(serializers.Serializer):
	assessment_title = serializers.CharField()
	subject = serializers.CharField(allow_null=True)
	submissions_done = serializers.IntegerField()
	submissions_expected = serializers.IntegerField()
	completion_percentage = serializers.FloatField()
	due_at = serializers.DateTimeField(allow_null=True)
	days_left = serializers.IntegerField()


class TeacherDashboardResponseSerializer(serializers.Serializer):
	summarycards = TeacherDashboardSummaryCardsSerializer()
	top_performers = TeacherDashboardTopPerformerSerializer(many=True)
	pending_submissions = TeacherDashboardPendingSubmissionSerializer(many=True)
	upcoming_deadlines = TeacherDashboardUpcomingDeadlineSerializer(many=True)


class ParentViewSet(viewsets.ViewSet):
	"""Endpoints for parents to see information about their wards."""
	permission_classes = [permissions.IsAuthenticated]

	def _grade_for_score(self, score: float):
		"""Map numeric score (0-100) to grade letter and remark."""
		if score is None:
			return "N/A", "No score"
		# 96-100 A+
		if score >= 96:
			return "A+", "Excellent"
		# 90-95 A-
		if score >= 90:
			return "A-", "Very good"
		# 86-89 B+
		if score >= 86:
			return "B+", "Good"
		# 80-85 B-
		if score >= 80:
			return "B-", "Good"
		# 76-79 C+
		if score >= 76:
			return "C+", "Fair"
		# 70-75 C-
		if score >= 70:
			return "C-", "Fair"
		# 65-69 D+
		if score >= 65:
			return "D+", "Poor"
		# 60-64 D-
		if score >= 60:
			return "D-", "Poor"
		# Below 60 F
		return "F", "Fail"

	@extend_schema(
		operation_id="parent_dashboard",
		request=None,
		responses={
			200: OpenApiResponse(
				description="Parent dashboard with children list and grades overview across all wards.",
				response=ParentDashboardSerializer,
				examples=[
					OpenApiExample(
						name="ParentDashboardExample",
						value={
							"children": [
								{
									"name": "Jane Doe",
									"student_id": "STU0000001",
									"student_db_id": 1,
									"grade": "PRIMARY_3",
									"school": "Afrilearn Academy",
								},
								{
									"name": "John Doe",
									"student_id": "STU0000002",
									"student_db_id": 2,
									"grade": "PRIMARY_4",
									"school": "Afrilearn Academy",
								},
							],
							"grades_overview": [
								{
									"child_name": "Jane Doe",
									"student_id": "STU0000001",
									"subject": "Mathematics",
									"overall_score": 92.5,
									"score_grade": "A-",
									"score_remark": "Very good",
								},
								{
									"child_name": "John Doe",
									"student_id": "STU0000002",
									"subject": "Science",
									"overall_score": 78.0,
									"score_grade": "C+",
									"score_remark": "Fair",
								},
							],
						},
					),
				],
			),
		},
		description="Parent dashboard with children list and grades overview across all wards.",
	)
	@action(detail=False, methods=['get'], url_path='dashboard')
	def dashboard(self, request):
		user: User = request.user
		parent = getattr(user, 'parent', None)
		if not parent:
			return Response({"detail": "Parent profile required."}, status=status.HTTP_403_FORBIDDEN)

		# Children list
		children_payload = []
		students = parent.wards.select_related('profile', 'school').all()
		for stu in students:
			children_payload.append({
				"name": getattr(stu.profile, 'name', None),
				"student_id": stu.student_id,
				"student_db_id": stu.id,
				"grade": stu.grade,
				"school": getattr(stu.school, 'name', None) if stu.school else None,
			})

		# Grades overview combined across all wards
		grades_by_key: Dict[tuple, List[float]] = {}
		student_map: Dict[int, tuple] = {}
		for stu in students:
			student_map[stu.id] = (getattr(stu.profile, 'name', None), stu.student_id)

			# General assessment grades with subject via assessment.title/grade scope is not subject-specific.
			# For now we won't include these in subject-based overview.

			# Lesson assessment grades -> subject via lesson.subject
			lesson_grades = (
				LessonAssessmentGrade.objects
				.select_related('lesson_assessment__lesson__subject')
				.filter(student=stu)
			)
			for g in lesson_grades:
				subject = getattr(getattr(getattr(g.lesson_assessment, 'lesson', None), 'subject', None), 'name', None)
				if not subject:
					continue
				key = (stu.id, subject)
				grades_by_key.setdefault(key, []).append(float(g.score))

		grades_overview = []
		for (student_id, subject_name), scores in grades_by_key.items():
			if not scores:
				continue
			avg_score = sum(scores) / len(scores)
			grade_letter, remark = self._grade_for_score(avg_score)
			child_name, child_student_id = student_map.get(student_id, (None, None))
			grades_overview.append({
				"child_name": child_name,
				"student_id": child_student_id,
				"subject": subject_name,
				"overall_score": round(avg_score, 2),
				"score_grade": grade_letter,
				"score_remark": remark,
			})

		response_data = {
			"children": children_payload,
			"grades_overview": grades_overview,
		}
		return Response(response_data)

	@extend_schema(
		operation_id="parent_grades",
		request=None,
		responses={200: ParentGradeOverviewSerializer(many=True)},
		description="Flat list of all individual lesson assessment grades for all wards of the parent.",
	)
	@action(detail=False, methods=['get'], url_path='grades')
	def grades(self, request):
		"""Return all individual grades for all wards of the parent.

		Each item includes child name, subject name, percentage score,
		letter grade and remark, and when it was last updated.
		"""
		user: User = request.user
		parent = getattr(user, 'parent', None)
		if not parent:
			return Response({"detail": "Parent profile required."}, status=status.HTTP_403_FORBIDDEN)

		students = parent.wards.select_related('profile').all()
		if not students:
			return Response([])

		student_ids = [s.id for s in students]
		name_by_id = {s.id: getattr(s.profile, 'name', None) for s in students}
		code_by_id = {s.id: s.student_id for s in students}

		items = []
		# Use lesson assessment grades because they map cleanly to a subject
		qs = (
			LessonAssessmentGrade.objects
			.select_related('student__profile', 'lesson_assessment__lesson__subject')
			.filter(student_id__in=student_ids)
		)
		for g in qs:
			student_id = g.student_id
			child_name = name_by_id.get(student_id)
			child_code = code_by_id.get(student_id)
			subject_name = getattr(getattr(getattr(g.lesson_assessment, 'lesson', None), 'subject', None), 'name', None)
			if not subject_name:
				continue
			# Assume score is already a percentage (0-100)
			percentage = float(g.score)
			grade_letter, remark = self._grade_for_score(percentage)
			items.append({
				"child_name": child_name,
				"student_id": child_code,
				"subject": subject_name,
				"overall_score": round(percentage, 2),
				"score_grade": grade_letter,
				"score_remark": remark,
				"updated_at": getattr(g, 'updated_at', None),
			})

		return Response(items)

	@extend_schema(
		operation_id="parent_assessments",
		request=None,
		responses={
			200: OpenApiResponse(
				description="Assessments list with summary.",
				response=ParentAssessmentsResponseSerializer,
				examples=[
					OpenApiExample(
						name="ParentAssessmentsExample",
						value={
							"assessments": [
								{
									"child_name": "Jane Doe",
									"assessment_title": "Midterm Math Test",
									"subject": "Mathematics",
									"assessment_type": "EXAM",
									"assessment_score": 100.0,
									"child_score": 92.5,
									"assessment_status": "Completed",
									"start_date": "2025-01-10T08:00:00Z",
									"due_date": "2025-01-15T08:00:00Z",
								},
								{
									"child_name": "John Doe",
									"assessment_title": "Science Quiz 1",
									"subject": "Science",
									"assessment_type": "QUIZ",
									"assessment_score": 20.0,
									"child_score": None,
									"assessment_status": "In Progress",
									"start_date": "2025-01-20T08:00:00Z",
									"due_date": "2025-01-22T08:00:00Z",
								},
							],
							"summary": {
								"completed": 1,
								"pending": 0,
								"in_progress": 1,
							},
						},
					),
				],
			),
		},
		description=(
			"List all assessments (general and lesson) relevant to the parent's children, "
			"including child scores when available, and a summary of completion status."
		),
	)
	@action(detail=False, methods=['get'], url_path='assessments')
	def assessments(self, request):
		"""Return all assessments relevant to the parent's children.

		Each item includes child, assessment, subject, type, allocated score,
		child score (if graded), status, start and due dates. A summary is
		also included with counts for Completed, Pending and In Progress.
		"""
		user: User = request.user
		parent = getattr(user, 'parent', None)
		if not parent:
			return Response({"detail": "Parent profile required."}, status=status.HTTP_403_FORBIDDEN)

		students = list(parent.wards.select_related('profile'))
		if not students:
			return Response({"assessments": [], "summary": {"completed": 0, "pending": 0, "in_progress": 0}})

		student_ids = [s.id for s in students]
		name_by_id = {s.id: getattr(s.profile, 'name', None) for s in students}

		items = []
		completed = pending = in_progress = 0

		# Lesson assessments: scoped via subject.grade to the student's grade
		lesson_assessments = (
			LessonAssessment.objects
			.select_related('lesson__subject')
			.filter(lesson__subject__grade__in=[s.grade for s in students])
		)

		# Preload grades for lesson assessments per (assessment_id, student_id)
		lag_qs = LessonAssessmentGrade.objects.filter(student_id__in=student_ids)
		lag_map: Dict[tuple, LessonAssessmentGrade] = {}
		for g in lag_qs.select_related('lesson_assessment'):
			lag_map[(g.lesson_assessment_id, g.student_id)] = g

		from elearncore.sysutils.constants import AssessmentType

		def _status_for(child_score, due_at, now):
			if child_score is not None:
				return "Completed"
			if due_at and due_at < now:
				return "Pending"
			return "In Progress"

		now = timezone.now()

		for la in lesson_assessments:
			subject = getattr(getattr(la.lesson, 'subject', None), 'name', None)
			for student in students:
				grade_rec = lag_map.get((la.id, student.id))
				child_score = float(grade_rec.score) if grade_rec else None
				status_label = _status_for(child_score, la.due_at, now)
				if status_label == "Completed":
					completed += 1
				elif status_label == "Pending":
					pending += 1
				else:
					in_progress += 1
				items.append({
					"child_name": name_by_id.get(student.id),
					"assessment_title": la.title,
					"subject": subject,
					"assessment_type": la.type,
					"assessment_score": float(la.marks),
					"child_score": float(child_score) if child_score is not None else None,
					"assessment_status": status_label,
					"start_date": la.created_at,
					"due_date": la.due_at,
				})

		# General assessments: not subject-specific in the model; we keep subject as None
		general_assessments = GeneralAssessment.objects.all()
		gag_qs = GeneralAssessmentGrade.objects.filter(student_id__in=student_ids)
		gag_map: Dict[tuple, GeneralAssessmentGrade] = {}
		for g in gag_qs.select_related('assessment'):
			gag_map[(g.assessment_id, g.student_id)] = g

		for ga in general_assessments:
			for student in students:
				grade_rec = gag_map.get((ga.id, student.id))
				child_score = float(grade_rec.score) if grade_rec else None
				status_label = _status_for(child_score, ga.due_at, now)
				if status_label == "Completed":
					completed += 1
				elif status_label == "Pending":
					pending += 1
				else:
					in_progress += 1
				items.append({
					"child_name": name_by_id.get(student.id),
					"assessment_title": ga.title,
					"subject": None,
					"assessment_type": ga.type,
					"assessment_score": float(ga.marks),
					"child_score": float(child_score) if child_score is not None else None,
					"assessment_status": status_label,
					"start_date": ga.created_at,
					"due_date": ga.due_at,
				})

		return Response({
			"assessments": items,
			"summary": {
				"completed": completed,
				"pending": pending,
				"in_progress": in_progress,
			},
		})

	@extend_schema(
		operation_id="parent_submissions",
		request=None,
		responses={
			200: OpenApiResponse(
				description="Submissions list with summary.",
				response=ParentSubmissionsResponseSerializer,
				examples=[
					OpenApiExample(
						name="ParentSubmissionsExample",
						value={
							"submissions": [
								{
									"child_name": "Jane Doe",
									"assessment_title": "Midterm Essay",
									"subject": None,
									"score": 18.0,
									"assessment_score": 20.0,
									"submission_status": "Graded",
									"solution": {
										"solution": "My essay answer...",
										"attachment": "https://example.com/uploads/essay.pdf",
									},
									"date_submitted": "2025-01-12T10:00:00Z",
								},
								{
									"child_name": "John Doe",
									"assessment_title": "Science Project",
									"subject": None,
									"score": None,
									"assessment_score": 30.0,
									"submission_status": "Pending Review",
									"solution": {
										"solution": "Project details...",
										"attachment": None,
									},
									"date_submitted": "2025-01-18T14:30:00Z",
								},
							],
							"summary": {
								"graded": 1,
								"pending": 1,
							},
						},
					),
				],
			),
		},
		description=(
			"List all assessment submissions made by the parent's children, "
			"including grading status and solution details."
		),
	)
	@action(detail=False, methods=['get'], url_path='submissions')
	def submissions(self, request):
		"""Return all submissions for the parent's children.

		Each item includes child name, assessment title, subject name,
		child score (if graded), allocated score, submission status,
		solution details, and submission date.
		"""
		user: User = request.user
		parent = getattr(user, 'parent', None)
		if not parent:
			return Response({"detail": "Parent profile required."}, status=status.HTTP_403_FORBIDDEN)

		students = list(parent.wards.select_related('profile'))
		if not students:
			return Response({"submissions": [], "summary": {"graded": 0, "pending": 0}})

		student_ids = [s.id for s in students]
		name_by_id = {s.id: getattr(s.profile, 'name', None) for s in students}

		graded_count = pending_count = 0
		items = []

		# General assessment submissions (use AssessmentSolution and GeneralAssessmentGrade)
		solutions = (
			AssessmentSolution.objects
			.select_related('assessment', 'student__profile')
			.filter(student_id__in=student_ids)
		)
		# Map grades by solution id
		grade_by_solution_id: Dict[int, GeneralAssessmentGrade] = {}
		for g in GeneralAssessmentGrade.objects.filter(student_id__in=student_ids).select_related('assessment', 'solution'):
			if g.solution_id:
				grade_by_solution_id[g.solution_id] = g

		for sol in solutions:
			student_id = sol.student_id
			child_name = name_by_id.get(student_id)
			assessment = sol.assessment
			grade_obj = grade_by_solution_id.get(sol.id)
			child_score = float(grade_obj.score) if grade_obj else None
			allocated = float(getattr(assessment, 'marks', 0.0) or 0.0)
			status_label = "Graded" if grade_obj else "Pending Review"
			if grade_obj:
				graded_count += 1
			else:
				pending_count += 1
			items.append({
				"child_name": child_name,
				"assessment_title": getattr(assessment, 'title', None),
				"subject": None,
				"score": child_score,
				"assessment_score": allocated,
				"submission_status": status_label,
				"solution": {
					"solution": sol.solution,
					"attachment": sol.attachment.url if getattr(sol, 'attachment', None) else None,
				},
				"date_submitted": sol.submitted_at,
			})

		# Note: Lesson assessments currently don't have a dedicated solution model;
		# if added later, similar logic can be applied for those.

		return Response({
			"submissions": items,
			"summary": {
				"graded": graded_count,
				"pending": pending_count,
			},
		})

	@extend_schema(
		operation_id="parent_linkchild",
		request=LinkChildSerializer,
		responses={200: OpenApiResponse(description="Child linked successfully.")},
		description=(
			"Link an existing student to the authenticated parent. "
			"Allows passing student id, email/phone for verification, and "
			"optionally school id and grade to update the student profile."
		),
	)
	@action(detail=False, methods=['post'], url_path='linkchild')
	def parent_linkchild(self, request):
		"""Link a student to the authenticated parent.

		Body fields:
		- student_id (required)
		- student_email (optional)
		- student_phone (optional)
		- school_id (optional)
		- grade (optional)
		"""
		user: User = request.user
		if user.role != UserRole.PARENT.value or not hasattr(user, 'parent'):
			return Response({"detail": "Only parents can link children."}, status=status.HTTP_403_FORBIDDEN)

		student_id = request.data.get('student_id')
		student_email = (request.data.get('student_email') or '').strip().lower()
		student_phone = (request.data.get('student_phone') or '').strip()
		school_id = request.data.get('school_id')
		grade = request.data.get('grade')

		if not student_id:
			return Response({"detail": "student_id is required."}, status=status.HTTP_400_BAD_REQUEST)
		if not (student_email or student_phone):
			return Response({"detail": "Provide student_email or student_phone (or both)."}, status=status.HTTP_400_BAD_REQUEST)

		qs = Student.objects.select_related('profile').filter(id=student_id)
		if student_email:
			qs = qs.filter(profile__email__iexact=student_email)
		if student_phone:
			qs = qs.filter(profile__phone=student_phone)
		student = qs.first()
		if not student:
			return Response({"detail": "Student not found with provided identifiers."}, status=status.HTTP_404_NOT_FOUND)

		# Optionally update school and grade
		update_fields = []
		if grade:
			student.grade = str(grade)
			update_fields.append('grade')
		if school_id:
			from accounts.models import School as AccountSchool
			school_obj = AccountSchool.objects.filter(id=school_id).first()
			if not school_obj:
				return Response({"detail": "School not found."}, status=status.HTTP_400_BAD_REQUEST)
			student.school = school_obj
			update_fields.append('school')
		if update_fields:
			update_fields.append('updated_at') if hasattr(student, 'updated_at') else None
			student.save(update_fields=[f for f in update_fields if f])

		user.parent.wards.add(student)
		return Response({"detail": "Child linked."})

	@extend_schema(
		operation_id="parent_mychildren",
		request=None,
		responses={200: ParentChildSerializer(many=True)},
		description="List all children (wards) linked to the authenticated parent.",
	)
	@action(detail=False, methods=['get'], url_path='mychildren')
	def mychildren(self, request):
		user: User = request.user
		parent = getattr(user, 'parent', None)
		if not parent:
			return Response({"detail": "Parent profile required."}, status=status.HTTP_403_FORBIDDEN)

		students = parent.wards.select_related('profile', 'school').all()
		payload = []
		for stu in students:
			payload.append({
				"id": stu.id,
				"name": getattr(stu.profile, 'name', None),
				"school": getattr(stu.school, 'name', None) if stu.school else None,
				"grade": stu.grade,
				"student_id": stu.student_id,
				"created_at": stu.created_at,
			})
		return Response(payload)

	@extend_schema(
		operation_id="parent_analytics",
		request=None,
		parameters=[
			OpenApiParameter(
				name="child",
				type=str,
				description="Optional student_id to filter analytics for a single child.",
				required=False,
			),
		],
		responses={
			200: OpenApiResponse(
				response=ParentAnalyticsResponseSerializer,
				description=(
					"Analytics summary for the parent's children, including summary cards "
					"and estimated time spent per subject."
				),
			),
		},
		description=(
			"Get analytics for the parent's children, including total assessments, "
			"completed assessments, overall average score, subjects touched, and "
			"estimated time spent per subject. Optionally filter by a single child via "
			"?child=<student_id>."
		),
	)
	@action(detail=False, methods=['get'], url_path='analytics')
	def analytics(self, request):
		"""Return analytics for the parent's children.

		By default aggregates across all wards. Pass ?child=<student_id>
		to restrict to a single child.
		"""
		user: User = request.user
		parent = getattr(user, 'parent', None)
		if not parent:
			return Response({"detail": "Parent profile required."}, status=status.HTTP_403_FORBIDDEN)

		students_qs = parent.wards.all()
		child_param = request.query_params.get('child')
		if child_param:
			child_id = child_param.strip()
			students_qs = students_qs.filter(student_id=child_id)
		if not students_qs.exists():
			return Response({
				"summarycards": {
					"total_assessments": 0,
					"total_completed_assessments": 0,
					"overall_average_score": 0.0,
					"total_subjects_touched": 0,
					"estimated_total_hours": 0.0,
				},
				"estimated_time_spent": [],
			})

		student_ids = list(students_qs.values_list('id', flat=True))

		# Assessments and completion stats (reuse LessonAssessmentGrade and GeneralAssessmentGrade)
		lag_qs = LessonAssessmentGrade.objects.filter(student_id__in=student_ids)
		gag_qs = GeneralAssessmentGrade.objects.filter(student_id__in=student_ids)

		all_scores = []
		completed_assessments = 0
		# For total assessments we count unique (assessment, student) pairs across both types
		total_assessments = lag_qs.count() + gag_qs.count()

		for g in lag_qs:
			if g.score is not None:
				completed_assessments += 1
				all_scores.append(float(g.score))
		for g in gag_qs:
			if g.score is not None:
				completed_assessments += 1
				all_scores.append(float(g.score))

		overall_average_score = sum(all_scores) / len(all_scores) if all_scores else 0.0

		# Subjects touched based on lessons taken
		lessons_taken = (
			TakeLesson.objects
			.filter(student_id__in=student_ids)
			.select_related('lesson__subject')
		)
		subject_ids = set()
		for tl in lessons_taken:
			if getattr(getattr(tl.lesson, 'subject', None), 'id', None):
				subject_ids.add(tl.lesson.subject.id)
		total_subjects_touched = len(subject_ids)

		# Estimate time spent: assume each TakeLesson has a duration field in minutes; if not, fall back to a constant
		DEFAULT_DURATION_MINUTES = 10
		from collections import defaultdict
		subject_time_minutes = defaultdict(float)

		for tl in lessons_taken:
			subject = getattr(getattr(tl.lesson, 'subject', None), 'name', None)
			if not subject:
				continue
			# Prefer an explicit duration field if present
			duration = getattr(tl, 'duration_minutes', None)
			try:
				duration_val = float(duration) if duration is not None else float(DEFAULT_DURATION_MINUTES)
			except (TypeError, ValueError):
				duration_val = float(DEFAULT_DURATION_MINUTES)
			subject_time_minutes[subject] += duration_val

		# Total time in hours
		total_minutes = sum(subject_time_minutes.values())
		estimated_total_hours = round(total_minutes / 60.0, 2) if total_minutes else 0.0

		estimated_time_spent = []
		for subject, minutes in subject_time_minutes.items():
			percentage = (minutes / total_minutes * 100.0) if total_minutes else 0.0
			hours = int(minutes // 60)
			remaining_minutes = int(minutes % 60)
			time_str = f"{hours}h {remaining_minutes}m"
			estimated_time_spent.append({
				"subject": subject,
				"time": time_str,
				"percentage": round(percentage, 2),
			})

		response_data = {
			"summarycards": {
				"total_assessments": total_assessments,
				"total_completed_assessments": completed_assessments,
				"overall_average_score": round(overall_average_score, 2),
				"total_subjects_touched": total_subjects_touched,
				"estimated_total_hours": estimated_total_hours,
			},
			"estimated_time_spent": estimated_time_spent,
		}
		return Response(response_data)


class GameViewSet(viewsets.ModelViewSet):
	"""Manage games; students can read, managers/admins can write.
	Game Types: MUSIC, WORD_PUZZLE, SHAPE, COLOR, NUMBER
	"""
	queryset = GameModel.objects.select_related('created_by').all()
	serializer_class = GameSerializer
	filter_backends = [filters.SearchFilter, filters.OrderingFilter]
	search_fields = ['name', 'description', 'instructions', 'hint']
	ordering_fields = ['created_at', 'updated_at', 'name']

	def get_permissions(self):
		# Students (and anonymous) can list/retrieve, but writes are restricted
		if self.request.method in permissions.SAFE_METHODS:
			return [permissions.IsAuthenticatedOrReadOnly()]
		# Only roles that can create content (incl. admin) may write
		return [permissions.IsAuthenticated(), CanCreateContent()]

	def perform_create(self, serializer):
		game = serializer.save(created_by=self.request.user)
		# Log game creation/update as an activity for the creator
		if self.request.user and self.request.user.is_authenticated:
			Activity.objects.create(
				user=self.request.user,
				type="create_game",
				description=f"Created game '{game.name}'",
				metadata={"game_id": game.id, "game_type": game.type},
			)

	def list(self, request, *args, **kwargs):
		"""List games and, for students, include a played/new status.

		If the authenticated user has a student profile, each game in the
		response will include a `status` field: "played" if a GamePlay record
		exists for that student/game, otherwise "new".
		"""
		response = super().list(request, *args, **kwargs)
		user: User = request.user
		student = getattr(user, 'student', None)
		if not student or not isinstance(response.data, list):
			return response

		# Collect game IDs from the paginated/filtered result set
		game_ids = [item.get('id') for item in response.data if isinstance(item, dict) and 'id' in item]
		if not game_ids:
			return response

		played_ids = set(
			GamePlay.objects
			.filter(student=student, game_id__in=game_ids)
			.values_list('game_id', flat=True)
		)

		for item in response.data:
			if isinstance(item, dict) and 'id' in item:
				item['status'] = 'played' if item['id'] in played_ids else 'new'

		return response


class ContentViewSet(viewsets.ViewSet):
	"""Aggregate content management operations for content teams.

	Content Creators:
	- can list/create/update subjects, lessons, assessments, games, schools, counties, districts.

	Content Validators:
	- can view everything and perform approve/reject/request-review on content objects
	  that support a `status` field.
	"""
	permission_classes = [permissions.IsAuthenticated]

	def _require_creator(self, request):
		if not IsContentCreator().has_permission(request, self):
			return Response({"detail": "Content creator role required."}, status=status.HTTP_403_FORBIDDEN)
		return None

	def _require_validator(self, request):
		if not IsContentValidator().has_permission(request, self):
			return Response({"detail": "Content validator role required."}, status=status.HTTP_403_FORBIDDEN)
		return None

	
	@extend_schema(
		operation_id="content_subjects",
		request=SubjectWriteSerializer,
		responses={200: SubjectSerializer(many=True)},
		description="List or create subjects for content management.",
	)
	@action(detail=False, methods=['get', 'post'], url_path='subjects')
	def subjects(self, request):
		"""List or create subjects.

		- Creators/validators can create/edit; others read-only.
		"""
		if request.method == 'GET':
			qs = Subject.objects.all().order_by('name')
			# Content creators should only see subjects they created;
			# validators/admins can still see all subjects.
			user = request.user
			if user and user.is_authenticated and IsContentCreator().has_permission(request, self) and not IsContentValidator().has_permission(request, self):
				qs = qs.filter(created_by=user)
			return Response(SubjectSerializer(qs, many=True).data)

		# POST - creation requires creator capability
		deny = self._require_creator(request)
		if deny:
			return deny
		ser = SubjectWriteSerializer(data=request.data)
		ser.is_valid(raise_exception=True)
		obj = ser.save(created_by=request.user)
		return Response(SubjectSerializer(obj).data, status=status.HTTP_201_CREATED)

	
	@extend_schema(
		operation_id="content_lessons",
		request=LessonResourceSerializer,
		responses={200: LessonResourceSerializer(many=True)},
		description="List or create lessons (LessonResource) for content management.",
	)
	@action(detail=False, methods=['get', 'post'], url_path='lessons')
	def lessons(self, request):
		"""List or create lessons (LessonResource)."""
		if request.method == 'GET':
			qs = LessonResource.objects.select_related('subject').all().order_by('-created_at')
			user = request.user
			if user and user.is_authenticated and IsContentCreator().has_permission(request, self) and not IsContentValidator().has_permission(request, self):
				qs = qs.filter(created_by=user)
			return Response(LessonResourceSerializer(qs, many=True).data)

		deny = self._require_creator(request)
		if deny:
			return deny
		ser = LessonResourceSerializer(data=request.data)
		ser.is_valid(raise_exception=True)
		obj = ser.save(created_by=request.user)
		return Response(LessonResourceSerializer(obj).data, status=status.HTTP_201_CREATED)

	@extend_schema(
		operation_id="content_general_assessments",
		request=GeneralAssessmentSerializer,
		responses={200: GeneralAssessmentSerializer(many=True)},
		description="List or create general assessments for content management.",
	)
	@action(detail=False, methods=['get', 'post'], url_path='general-assessments')
	def general_assessments(self, request):
		"""List or create general assessments."""
		if request.method == 'GET':
			qs = GeneralAssessment.objects.select_related('given_by').all().order_by('-created_at')
			user = request.user
			if user and user.is_authenticated and IsContentCreator().has_permission(request, self) and not IsContentValidator().has_permission(request, self):
				# For creator role, restrict to assessments created by them.
				# GeneralAssessment does not have created_by; it links to a teacher via given_by.
				teacher = getattr(user, 'teacher', None)
				if teacher is not None:
					qs = qs.filter(given_by=teacher)
				else:
					qs = qs.none()
			return Response(GeneralAssessmentSerializer(qs, many=True).data)

		deny = self._require_creator(request)
		if deny:
			return deny
		ser = GeneralAssessmentSerializer(data=request.data)
		ser.is_valid(raise_exception=True)
		obj = ser.save()
		return Response(GeneralAssessmentSerializer(obj).data, status=status.HTTP_201_CREATED)

	@extend_schema(
		operation_id="content_lesson_assessments",
		request=LessonAssessmentSerializer,
		responses={200: LessonAssessmentSerializer(many=True)},
		description="List or create lesson assessments for content management.",
	)
	@action(detail=False, methods=['get', 'post'], url_path='lesson-assessments')
	def lesson_assessments(self, request):
		"""List or create lesson assessments."""
		if request.method == 'GET':
			qs = LessonAssessment.objects.select_related('lesson').all().order_by('-created_at')
			user = request.user
			if user and user.is_authenticated and IsContentCreator().has_permission(request, self) and not IsContentValidator().has_permission(request, self):
				teacher = getattr(user, 'teacher', None)
				if teacher is not None:
					qs = qs.filter(given_by=teacher)
				else:
					qs = qs.none()
			return Response(LessonAssessmentSerializer(qs, many=True).data)

		deny = self._require_creator(request)
		if deny:
			return deny
		ser = LessonAssessmentSerializer(data=request.data)
		ser.is_valid(raise_exception=True)
		obj = ser.save()
		return Response(LessonAssessmentSerializer(obj).data, status=status.HTTP_201_CREATED)

	@extend_schema(
		operation_id="content_list_questions",
		parameters=[
			OpenApiParameter(
				name="general_assessment_id",
				required=False,
				location=OpenApiParameter.QUERY,
				description="ID of a GeneralAssessment to list its questions.",
				type=int,
			),
			OpenApiParameter(
				name="lesson_assessment_id",
				required=False,
				location=OpenApiParameter.QUERY,
				description="ID of a LessonAssessment to list its questions.",
				type=int,
			),
		],
		responses={200: QuestionSerializer(many=True)},
		description=(
			"List questions (with options) for a given assessment. "
			"Exactly one of general_assessment_id or lesson_assessment_id must be provided."
		),
	)
	@action(detail=False, methods=['get'], url_path='questions')
	def list_questions(self, request):
		"""List questions and their options for a specific assessment (content side)."""
		user = request.user
		if not (
			IsContentCreator().has_permission(request, self)
			or IsContentValidator().has_permission(request, self)
			or IsAdminRole().has_permission(request, self)
		):
			return Response({"detail": "Content creator, validator, or admin role required."}, status=status.HTTP_403_FORBIDDEN)

		ga_id = request.query_params.get('general_assessment_id')
		la_id = request.query_params.get('lesson_assessment_id')
		if bool(ga_id) == bool(la_id):
			return Response(
				{"detail": "Provide exactly one of general_assessment_id or lesson_assessment_id."},
				status=status.HTTP_400_BAD_REQUEST,
			)

		qs = Question.objects.all().prefetch_related('options')
		if ga_id:
			try:
				ga_id_int = int(ga_id)
			except ValueError:
				return Response({"detail": "general_assessment_id must be an integer."}, status=status.HTTP_400_BAD_REQUEST)
			qs = qs.filter(general_assessment_id=ga_id_int)
		else:
			try:
				la_id_int = int(la_id)
			except ValueError:
				return Response({"detail": "lesson_assessment_id must be an integer."}, status=status.HTTP_400_BAD_REQUEST)
			qs = qs.filter(lesson_assessment_id=la_id_int)

		qs = qs.order_by('created_at')
		return Response(QuestionSerializer(qs, many=True).data)

	@extend_schema(
		operation_id="content_create_question",
		request=QuestionCreateSerializer,
		responses={201: QuestionSerializer},
		description=(
			"Create a question (with optional options) for a general or lesson assessment. "
			"Exactly one of general_assessment_id or lesson_assessment_id must be provided. "
			"Content creators/validators/admins can attach questions to any assessment."
		),
		examples=[
			OpenApiExample(
				name="MultipleChoiceQuestionExample",
				value={
					"general_assessment_id": 12,
					"type": "MULTIPLE_CHOICE",
					"question": "What is 2 + 2?",
					"answer": "4",
					"options": ["3", "4", "5", "6"],
				},
			),
		],
	)
	@action(detail=False, methods=['post'], url_path='questions/create')
	def create_question(self, request):
		"""Create an assessment question and optional options (content side)."""
		deny = self._require_creator(request)
		if deny:
			return deny
		ser = QuestionCreateSerializer(data=request.data, context={"request": request, "restrict_to_teacher": False})
		ser.is_valid(raise_exception=True)
		question = ser.save()
		return Response(QuestionSerializer(question).data, status=status.HTTP_201_CREATED)

	@extend_schema(
		operation_id="content_all_assessments",
		responses={200: ContentAssessmentItemSerializer(many=True)},
		description=(
			"Return both general and lesson assessments in a single response. "
			"Each item includes a 'kind' field (general|lesson)."
		),
	)
	@action(detail=False, methods=['get'], url_path='all-assessments')
	def all_assessments(self, request):
		"""Return a combined list of general and lesson assessments."""
		general_qs = GeneralAssessment.objects.select_related('given_by').all().order_by('-created_at')
		lesson_qs = LessonAssessment.objects.select_related('lesson', 'given_by').all().order_by('-created_at')

		general_payload = [
			{
				"kind": "general",
				"id": ga.id,
				"title": ga.title,
				"type": ga.type,
				"marks": ga.marks,
				"status": ga.status,
				"due_at": ga.due_at.isoformat() if ga.due_at else None,
				"grade": ga.grade,
				"given_by_id": ga.given_by_id,
			}
			for ga in general_qs
		]

		lesson_payload = [
			{
				"kind": "lesson",
				"id": la.id,
				"title": la.title,
				"type": la.type,
				"marks": la.marks,
				"status": la.status,
				"due_at": la.due_at.isoformat() if la.due_at else None,
				"lesson_id": la.lesson_id,
				"lesson_title": getattr(la.lesson, 'title', None),
				"subject_id": getattr(getattr(la.lesson, 'subject', None), 'id', None),
				"subject_name": getattr(getattr(la.lesson, 'subject', None), 'name', None),
				"given_by_id": la.given_by_id,
			}
			for la in lesson_qs
		]

		# Sort combined by due_at (descending), then by created_at implicitly via original ordering
		def _sort_key(item):
			return item.get("due_at") or ""

		combined = general_payload + lesson_payload
		combined.sort(key=_sort_key, reverse=True)
		return Response(combined)

	@extend_schema(
		operation_id="content_games",
		request=GameSerializer,
		responses={200: GameSerializer(many=True)},
		description="List or create games for content management.",
	)
	@action(detail=False, methods=['get', 'post'], url_path='games')
	def games(self, request):
		"""List or create games for content management."""
		if request.method == 'GET':
			qs = GameModel.objects.all().order_by('-created_at')
			user = request.user
			if user and user.is_authenticated and IsContentCreator().has_permission(request, self) and not IsContentValidator().has_permission(request, self):
				qs = qs.filter(created_by=user)
			return Response(GameSerializer(qs, many=True).data)

		deny = self._require_creator(request)
		if deny:
			return deny
		ser = GameSerializer(data=request.data)
		ser.is_valid(raise_exception=True)
		obj = ser.save(created_by=request.user)
		return Response(GameSerializer(obj).data, status=status.HTTP_201_CREATED)

	@extend_schema(
		operation_id="content_schools",
		request=SchoolSerializer,
		responses={200: SchoolSerializer(many=True)},
		description="List or create schools for content management.",
	)
	@action(detail=False, methods=['get', 'post'], url_path='schools')
	def schools(self, request):
		"""List or create schools."""
		if request.method == 'GET':
			qs = School.objects.select_related('district__county').all().order_by('name')
			user = request.user
			if user and user.is_authenticated and IsContentCreator().has_permission(request, self) and not IsContentValidator().has_permission(request, self):
				qs = qs.filter(created_by=user)
			return Response(SchoolSerializer(qs, many=True).data)

		deny = self._require_creator(request)
		if deny:
			return deny
		ser = SchoolSerializer(data=request.data)
		ser.is_valid(raise_exception=True)
		obj = ser.save()
		return Response(SchoolSerializer(obj).data, status=status.HTTP_201_CREATED)

	@extend_schema(
		operation_id="content_counties",
		request=CountySerializer,
		responses={200: CountySerializer(many=True)},
		description="List or create counties for content management.",
		examples=[
			OpenApiExample(
				name="CreateCountyRequest",
				value={"name": "Montserrado", "status": "PENDING", "moderation_comment": "Initial import"},
			),
			OpenApiExample(
				name="CountyListResponseItem",
				value={
					"id": 1,
					"name": "Montserrado",
					"status": "APPROVED",
					"moderation_comment": "Validated by admin",
					"created_at": "2025-11-20T09:00:00Z",
					"updated_at": "2025-11-21T10:00:00Z",
				},
			),
		],
	)
	@action(detail=False, methods=['get', 'post'], url_path='counties')
	def counties(self, request):
		"""List or create counties."""
		if request.method == 'GET':
			qs = County.objects.all().order_by('name')
			user = request.user
			if user and user.is_authenticated and IsContentCreator().has_permission(request, self) and not IsContentValidator().has_permission(request, self):
				qs = qs.filter(created_by=user)
			return Response(CountySerializer(qs, many=True).data)

		deny = self._require_creator(request)
		if deny:
			return deny
		ser = CountySerializer(data=request.data)
		ser.is_valid(raise_exception=True)
		obj = ser.save()
		return Response(CountySerializer(obj).data, status=status.HTTP_201_CREATED)

	@extend_schema(
		operation_id="content_districts",
		request=DistrictSerializer,
		responses={200: DistrictSerializer(many=True)},
		description="List or create districts for content management.",
	)
	@action(detail=False, methods=['get', 'post'], url_path='districts')
	def districts(self, request):
		"""List or create districts."""
		if request.method == 'GET':
			qs = District.objects.select_related('county').all().order_by('name')
			user = request.user
			if user and user.is_authenticated and IsContentCreator().has_permission(request, self) and not IsContentValidator().has_permission(request, self):
				qs = qs.filter(created_by=user)
			return Response(DistrictSerializer(qs, many=True).data)

		deny = self._require_creator(request)
		if deny:
			return deny
		ser = DistrictSerializer(data=request.data)
		ser.is_valid(raise_exception=True)
		obj = ser.save()
		return Response(DistrictSerializer(obj).data, status=status.HTTP_201_CREATED)

	@extend_schema(
		operation_id="content_teachers",
		responses={200: TeacherSerializer(many=True)},
		description="List all teachers for content management (read-only).",
	)
	@action(detail=False, methods=['get'], url_path='teachers')
	def teachers(self, request):
		"""Return all teacher profiles.

		Intended for content managers/validators/admins to see teacher accounts
		and their moderation status.
		"""
		qs = Teacher.objects.select_related('profile', 'school').all().order_by('profile__name')
		return Response(TeacherSerializer(qs, many=True).data)

	@extend_schema(
		operation_id="content_create_teacher",
		description=(
			"Create a teacher account (User + Teacher profile). "
			"School is required because content managers do not have a school profile."
		),
		request=ContentCreateTeacherSerializer,
		responses={201: TeacherSerializer},
	)
	@action(detail=False, methods=['post'], url_path='teachers/create')
	def create_teacher(self, request):
		"""Content managers/admins create a single teacher (user + profile)."""
		from django.db import transaction
		from accounts.models import User, School, Teacher as TeacherModel

		deny = self._require_creator(request)
		if deny:
			return deny

		ser = ContentCreateTeacherSerializer(data=request.data)
		ser.is_valid(raise_exception=True)
		data = ser.validated_data

		name = data["name"].strip()
		phone = data["phone"].strip()
		email = (data.get("email") or "").strip() or None
		gender = (data.get("gender") or "").strip() or None
		dob = data.get("dob")
		school_id = data.get("school_id")

		try:
			school = School.objects.get(id=school_id)
		except School.DoesNotExist:
			return Response({"detail": "School not found."}, status=status.HTTP_400_BAD_REQUEST)

		import secrets
		import string
		alphabet = string.ascii_letters + string.digits
		temp_password = "".join(secrets.choice(alphabet) for _ in range(10))

		with transaction.atomic():
			user = User(
				name=name,
				phone=phone,
				email=email,
				role=UserRole.TEACHER.value,
				dob=dob,
				gender=gender,
			)
			user.set_password(temp_password)
			user.save()

			teacher = TeacherModel.objects.create(
				profile=user,
				school=school,
				status=StatusEnum.APPROVED.value,
			)

		message = (
			f"Hi {name}, your Liberia eLearn teacher account has been created.\n"
			f"Login with phone: {phone} and password: {temp_password}.\n"
			"Please change this password after your first login."
		)
		fire_and_forget(
			_send_account_notifications,
			message,
			phone,
			email,
			"Your Liberia eLearn teacher account",
		)

		return Response(TeacherSerializer(teacher).data, status=status.HTTP_201_CREATED)

	@extend_schema(
		operation_id="content_teacher_detail",
		responses={200: TeacherSerializer},
		description="Retrieve a single teacher by id for content management.",
	)
	@action(detail=False, methods=['get'], url_path='teachers/(?P<pk>[^/.]+)')
	def teacher_detail(self, request, pk=None):
		"""Return a single teacher profile by id."""
		try:
			teacher = Teacher.objects.select_related('profile', 'school').get(pk=pk)
		except Teacher.DoesNotExist:
			return Response({"detail": "Teacher not found."}, status=404)
		return Response(TeacherSerializer(teacher).data)

	@extend_schema(
		operation_id="content_bulk_create_teachers",
		description=(
			"Bulk create teacher accounts from a CSV file. "
			"Each row must include name, phone, and school_id; "
			"optional columns are email, gender, and dob (YYYY-MM-DD)."
		),
		request=ContentBulkTeacherUploadSerializer,
		responses={
			200: OpenApiResponse(
				description="Bulk teacher creation summary with per-row statuses.",
			),
		},
	)
	@action(detail=False, methods=['post'], url_path='teachers/bulk-create')
	def bulk_create_teachers(self, request):
		"""Bulk create teacher accounts (User + Teacher profile) from a CSV upload."""
		from django.db import transaction
		from rest_framework.exceptions import ValidationError
		from accounts.models import User, School, Teacher as TeacherModel

		deny = self._require_creator(request)
		if deny:
			return deny

		upload_ser = ContentBulkTeacherUploadSerializer(data=request.data)
		upload_ser.is_valid(raise_exception=True)
		file_obj = upload_ser.validated_data['file']

		try:
			decoded = file_obj.read().decode('utf-8-sig')
		except Exception:
			return Response({"detail": "Unable to read uploaded file as UTF-8 text."}, status=status.HTTP_400_BAD_REQUEST)

		if not decoded.strip():
			return Response({"detail": "Uploaded file is empty."}, status=status.HTTP_400_BAD_REQUEST)

		reader = csv.DictReader(io.StringIO(decoded))
		if not reader.fieldnames:
			return Response({"detail": "CSV file has no header row."}, status=status.HTTP_400_BAD_REQUEST)

		required_columns = ['name', 'phone', 'school_id']
		missing = [c for c in required_columns if c not in reader.fieldnames]
		if missing:
			return Response({"detail": f"Missing required columns: {', '.join(missing)}."}, status=status.HTTP_400_BAD_REQUEST)

		results = []
		created_count = 0
		failed_count = 0

		for row_index, row in enumerate(reader, start=2):
			row_result = {"row": row_index}

			mapped = {
				"name": (row.get("name") or "").strip(),
				"phone": (row.get("phone") or "").strip(),
				"email": (row.get("email") or "").strip() or None,
				"gender": (row.get("gender") or "").strip() or None,
				"dob": _parse_bulk_date(row.get("dob")),
			}

			school_id_raw = (row.get("school_id") or "").strip()
			if not school_id_raw:
				results.append({**row_result, "status": "error", "errors": {"school_id": ["This field is required."]}})
				failed_count += 1
				continue
			try:
				mapped["school_id"] = int(school_id_raw)
			except ValueError:
				results.append({**row_result, "status": "error", "errors": {"school_id": ["Invalid integer."]}})
				failed_count += 1
				continue

			ser = ContentCreateTeacherSerializer(data=mapped)
			try:
				ser.is_valid(raise_exception=True)
			except ValidationError as exc:
				results.append({**row_result, "status": "error", "errors": exc.detail})
				failed_count += 1
				continue

			data = ser.validated_data
			name = data["name"].strip()
			phone = data["phone"].strip()
			email = (data.get("email") or "").strip() or None
			gender = (data.get("gender") or "").strip() or None
			dob = data.get("dob")
			school_id = data.get("school_id")

			try:
				school = School.objects.get(id=school_id)
			except School.DoesNotExist:
				results.append({**row_result, "status": "error", "errors": {"school_id": ["School not found."]}})
				failed_count += 1
				continue

			import secrets
			import string
			alphabet = string.ascii_letters + string.digits
			temp_password = "".join(secrets.choice(alphabet) for _ in range(10))

			try:
				with transaction.atomic():
					user = User(
						name=name,
						phone=phone,
						email=email,
						role=UserRole.TEACHER.value,
						dob=dob,
						gender=gender,
					)
					user.set_password(temp_password)
					user.save()

					teacher = TeacherModel.objects.create(
						profile=user,
						school=school,
						status=StatusEnum.APPROVED.value,
					)
			except Exception as exc:
				results.append({**row_result, "status": "error", "errors": {"non_field_errors": [str(exc)]}})
				failed_count += 1
				continue

			message = (
				f"Hi {name}, your Liberia eLearn teacher account has been created.\n"
				f"Login with phone: {phone} and password: {temp_password}.\n"
				"Please change this password after your first login."
			)
			fire_and_forget(
				_send_account_notifications,
				message,
				phone,
				email,
				"Your Liberia eLearn teacher account",
			)

			created_count += 1
			results.append({
				**row_result,
				"status": "created",
				"teacher_db_id": teacher.id,
				"teacher_id": teacher.teacher_id,
				"name": name,
				"phone": phone,
			})

		return Response({
			"summary": {
				"total_rows": len(results),
				"created": created_count,
				"failed": failed_count,
			},
			"results": results,
		})

	@extend_schema(
		operation_id="content_dashboard",
		responses={200: ContentDashboardSerializer},
		description=(
			"Summary stats for content managers. Returns counts for four cards: "
			"Total content items, Approved, Rejected, and Reviews Requested (review_requested)."
		),
	)
	@action(detail=False, methods=['get'], url_path='dashboard')
	def dashboard(self, request):
		"""Return high-level status counts for all manageable content types.

		Includes subjects, lessons, general assessments, lesson assessments, games,
		and schools in the totals.
		"""
		# Require at least content creator/validator/admin access
		deny = self._require_creator(request)
		if deny and not IsContentValidator().has_permission(request, self):
			return deny

		from elearncore.sysutils.constants import Status as StatusEnum

		status_values = {
			"APPROVED": StatusEnum.APPROVED.value,
			"REJECTED": StatusEnum.REJECTED.value,
			"REVIEW_REQUESTED": StatusEnum.REVIEW_REQUESTED.value,
		}

		# Helper to count by status for a queryset with a 'status' field
		def _counts_for(qs):
			return {
				"total": qs.count(),
				"approved": qs.filter(status=status_values["APPROVED"]).count(),
				"rejected": qs.filter(status=status_values["REJECTED"]).count(),
				"review_requested": qs.filter(status=status_values["REVIEW_REQUESTED"]).count(),
			}

		# Collect counts for each model where status is available
		lesson_counts = _counts_for(LessonResource.objects.all())
		general_assessment_counts = _counts_for(GeneralAssessment.objects.all())
		lesson_assessment_counts = _counts_for(LessonAssessment.objects.all())
		game_counts = _counts_for(GameModel.objects.all())
		school_counts = _counts_for(School.objects.all())

		# Subjects have a status field as well
		subject_counts = _counts_for(Subject.objects.all())

		# Aggregate overall totals across all content types
		def _agg(*count_dicts):
			agg = {"total": 0, "approved": 0, "rejected": 0, "review_requested": 0}
			for c in count_dicts:
				for k in agg.keys():
					agg[k] += c.get(k, 0)
			return agg

		overall = _agg(
			lesson_counts,
			general_assessment_counts,
			lesson_assessment_counts,
			game_counts,
			school_counts,
			subject_counts,
		)

		return Response(
			{
				"overall": overall,
				"by_type": {
					"subjects": subject_counts,
					"lessons": lesson_counts,
					"general_assessments": general_assessment_counts,
					"lesson_assessments": lesson_assessment_counts,
					"games": game_counts,
					"schools": school_counts,
				},
			}
		)

	@extend_schema(
		operation_id="content_assign_subjects_to_teacher",
		description=(
			"Assign a set of subjects to a teacher. "
			"Existing subject assignments for this teacher will be replaced with the provided list."
		),
		request=AssignSubjectsToTeacherSerializer,
		responses={200: TeacherSerializer},
	)
	@action(detail=False, methods=['post'], url_path='teachers/assign-subjects')
	def assign_subjects_to_teacher(self, request):
		"""Assign subjects to a teacher (content creators/admins only).

		The payload must include a `teacher_id` and a non-empty list of
		`subject_ids`. The teacher's existing subject assignments are replaced
		with exactly this list.
		"""
		deny = self._require_creator(request)
		if deny:
			return deny

		ser = AssignSubjectsToTeacherSerializer(data=request.data)
		ser.is_valid(raise_exception=True)
		teacher_id = ser.validated_data["teacher_id"]
		subject_ids = ser.validated_data["subject_ids"]

		try:
			teacher = Teacher.objects.get(pk=teacher_id)
		except Teacher.DoesNotExist:
			return Response({"detail": "Teacher not found."}, status=status.HTTP_404_NOT_FOUND)

		subjects = list(Subject.objects.filter(id__in=subject_ids))
		found_ids = {s.id for s in subjects}
		missing_ids = [sid for sid in subject_ids if sid not in found_ids]
		if missing_ids:
			return Response(
				{"detail": "Some subjects were not found.", "missing_subject_ids": missing_ids},
				status=status.HTTP_400_BAD_REQUEST,
			)

		teacher.subjects.set(subjects)
		return Response(TeacherSerializer(teacher).data)

	@extend_schema(
		operation_id="content_create_student",
		description=(
			"Create a student account (User + Student profile). "
			"school_id is required because content managers do not have a school profile."
		),
		request=TeacherCreateStudentSerializer,
		responses={201: StudentSerializer},
	)
	@action(detail=False, methods=['post'], url_path='students/create')
	def create_student(self, request):
		"""Content managers/admins create a single student (user + profile)."""
		from django.db import transaction
		from accounts.models import User, School, Student as StudentModel

		deny = self._require_creator(request)
		if deny:
			return deny

		ser = TeacherCreateStudentSerializer(data=request.data)
		ser.is_valid(raise_exception=True)
		data = ser.validated_data

		name = data["name"].strip()
		phone = data["phone"].strip()
		email = (data.get("email") or "").strip() or None
		grade = (data.get("grade") or "").strip() or None
		gender = (data.get("gender") or "").strip() or None
		dob = data.get("dob")
		school_id = data.get("school_id")

		if school_id is None:
			return Response({"detail": "school_id is required for content-created students."}, status=status.HTTP_400_BAD_REQUEST)
		try:
			school = School.objects.get(id=school_id)
		except School.DoesNotExist:
			return Response({"detail": "School not found."}, status=status.HTTP_400_BAD_REQUEST)

		import secrets
		import string
		alphabet = string.ascii_letters + string.digits
		temp_password = "".join(secrets.choice(alphabet) for _ in range(10))

		with transaction.atomic():
			user = User(
				name=name,
				phone=phone,
				email=email,
				role=UserRole.STUDENT.value,
				dob=dob,
				gender=gender,
			)
			user.set_password(temp_password)
			user.save()

			student_kwargs = {
				"profile": user,
				"school": school,
				"status": StatusEnum.APPROVED.value,
			}
			if grade:
				student_kwargs["grade"] = grade
			student = StudentModel.objects.create(**student_kwargs)

		message = (
			f"Hi {name}, your Liberia eLearn student account has been created.\n"
			f"Login with phone: {phone} and password: {temp_password}.\n"
			"Please change this password after your first login."
		)
		fire_and_forget(
			_send_account_notifications,
			message,
			phone,
			email,
			"Your Liberia eLearn student account",
		)

		return Response(StudentSerializer(student).data, status=status.HTTP_201_CREATED)

	@extend_schema(
		operation_id="content_bulk_create_students",
		description=(
			"Bulk create student accounts from a CSV file. "
			"Each row must include name, phone, and school_id; "
			"optional columns are email, grade, gender, and dob (YYYY-MM-DD)."
		),
		request=TeacherBulkStudentUploadSerializer,
		responses={
			200: OpenApiResponse(
				description="Bulk student creation summary with per-row statuses.",
			),
		},
	)
	@action(detail=False, methods=['post'], url_path='students/bulk-create')
	def bulk_create_students(self, request):
		"""Bulk create student accounts (User + Student profile) from a CSV upload."""
		from django.db import transaction
		from rest_framework.exceptions import ValidationError
		from accounts.models import User, School, Student as StudentModel

		deny = self._require_creator(request)
		if deny:
			return deny

		upload_ser = TeacherBulkStudentUploadSerializer(data=request.data)
		upload_ser.is_valid(raise_exception=True)
		file_obj = upload_ser.validated_data['file']

		try:
			decoded = file_obj.read().decode('utf-8-sig')
		except Exception:
			return Response({"detail": "Unable to read uploaded file as UTF-8 text."}, status=status.HTTP_400_BAD_REQUEST)

		if not decoded.strip():
			return Response({"detail": "Uploaded file is empty."}, status=status.HTTP_400_BAD_REQUEST)

		reader = csv.DictReader(io.StringIO(decoded))
		if not reader.fieldnames:
			return Response({"detail": "CSV file has no header row."}, status=status.HTTP_400_BAD_REQUEST)

		required_columns = ['name', 'phone', 'school_id']
		missing = [c for c in required_columns if c not in reader.fieldnames]
		if missing:
			return Response({"detail": f"Missing required columns: {', '.join(missing)}."}, status=status.HTTP_400_BAD_REQUEST)

		results = []
		created_count = 0
		failed_count = 0

		for row_index, row in enumerate(reader, start=2):
			row_result = {"row": row_index}

			mapped = {
				"name": (row.get("name") or "").strip(),
				"phone": (row.get("phone") or "").strip(),
				"email": (row.get("email") or "").strip() or None,
				"grade": (row.get("grade") or "").strip() or None,
				"gender": (row.get("gender") or "").strip() or None,
				"dob": _parse_bulk_date(row.get("dob")),
			}

			school_id_raw = (row.get("school_id") or "").strip()
			if not school_id_raw:
				results.append({**row_result, "status": "error", "errors": {"school_id": ["This field is required."]}})
				failed_count += 1
				continue
			try:
				mapped["school_id"] = int(school_id_raw)
			except ValueError:
				results.append({**row_result, "status": "error", "errors": {"school_id": ["Invalid integer."]}})
				failed_count += 1
				continue

			ser = TeacherCreateStudentSerializer(data=mapped)
			try:
				ser.is_valid(raise_exception=True)
			except ValidationError as exc:
				results.append({**row_result, "status": "error", "errors": exc.detail})
				failed_count += 1
				continue

			data = ser.validated_data
			name = data["name"].strip()
			phone = data["phone"].strip()
			email = (data.get("email") or "").strip() or None
			grade = (data.get("grade") or "").strip() or None
			gender = (data.get("gender") or "").strip() or None
			dob = data.get("dob")
			school_id = data.get("school_id")

			try:
				school = School.objects.get(id=school_id)
			except School.DoesNotExist:
				results.append({**row_result, "status": "error", "errors": {"school_id": ["School not found."]}})
				failed_count += 1
				continue

			import secrets
			import string
			alphabet = string.ascii_letters + string.digits
			temp_password = "".join(secrets.choice(alphabet) for _ in range(10))

			try:
				with transaction.atomic():
					user = User(
						name=name,
						phone=phone,
						email=email,
						role=UserRole.STUDENT.value,
						dob=dob,
						gender=gender,
					)
					user.set_password(temp_password)
					user.save()

					student_kwargs = {
						"profile": user,
						"school": school,
						"status": StatusEnum.APPROVED.value,
					}
					if grade:
						student_kwargs["grade"] = grade
					student = StudentModel.objects.create(**student_kwargs)
			except Exception as exc:
				results.append({**row_result, "status": "error", "errors": {"non_field_errors": [str(exc)]}})
				failed_count += 1
				continue

			message = (
				f"Hi {name}, your Liberia eLearn student account has been created.\n"
				f"Login with phone: {phone} and password: {temp_password}.\n"
				"Please change this password after your first login."
			)
			fire_and_forget(
				_send_account_notifications,
				message,
				phone,
				email,
				"Your Liberia eLearn student account",
			)

			created_count += 1
			results.append({
				**row_result,
				"status": "created",
				"student_db_id": student.id,
				"student_id": student.student_id,
				"name": name,
				"phone": phone,
			})

		return Response({
			"summary": {
				"total_rows": len(results),
				"created": created_count,
				"failed": failed_count,
			},
			"results": results,
		})

	@extend_schema(
		description=(
			"Download a sample CSV template for bulk student creation via content endpoints. "
			"The file includes the correct header columns and example rows."
		),
		responses={
			200: OpenApiResponse(
				description="CSV file with header row and two sample student records.",
			),
		},
	)
	@action(detail=False, methods=['get'], url_path='students/bulk-template')
	def content_bulk_students_template(self, request):
		"""Return a CSV template for bulk student creation via content endpoints.

		The template contains a header row with all supported columns and
		two example rows to guide content managers/admins on the expected format.
		"""
		deny = self._require_creator(request)
		if deny:
			return deny

		header = [
			"name",
			"phone",
			"email",
			"grade",
			"gender",
			"dob",
			"school_id",
		]
		example_rows = [
			{
				"name": "Jane Doe",
				"phone": "231770000001",
				"email": "jane@example.com",
				"grade": "GRADE 3",
				"gender": "F",
				"dob": "2013-05-10",
				"school_id": "",
			},
			{
				"name": "John Doe",
				"phone": "231770000002",
				"email": "john@example.com",
				"grade": "GRADE 4",
				"gender": "M",
				"dob": "2012-09-02",
				"school_id": "5",
			},
		]

		buffer = io.StringIO()
		writer = csv.DictWriter(buffer, fieldnames=header)
		writer.writeheader()
		for row in example_rows:
			writer.writerow(row)

		csv_content = buffer.getvalue()
		response = HttpResponse(csv_content, content_type="text/csv")
		response["Content-Disposition"] = "attachment; filename=students_bulk_template.csv"
		return response

	@extend_schema(
		operation_id="content_moderate",
		request=ContentModerationSerializer,
		responses={200: ContentModerationResponseSerializer},
		description=(
			"Moderate a content object by approving, rejecting, or requesting changes. "
			"Requires content validator role."
		),
	)
	@action(detail=False, methods=['post'], url_path='moderate')
	def moderate(self, request):
		"""Generic moderation endpoint for validators.

		Body should include:
		- model: one of "subject", "lesson", "general_assessment", "lesson_assessment", "game",
		          "school", "county", "district", "student", "teacher".
		- id: object primary key
		- action: one of "approve", "reject", "request_changes".
		- moderation_comment: optional string, required if action is "request_changes" or "request_review".
		"""
		deny = self._require_validator(request)
		if deny:
			return deny

		model_name = str(request.data.get('model') or '').lower()
		obj_id = request.data.get('id')
		action_name = str(request.data.get('action') or '').lower()
		comment = request.data.get('moderation_comment')
		if not model_name or not obj_id or not action_name:
			return Response({"detail": "model, id and action are required."}, status=status.HTTP_400_BAD_REQUEST)
		if action_name in {'request_changes', 'request_review'} and not (comment and str(comment).strip()):
			return Response({"detail": "moderation_comment is required when requesting changes or review."}, status=status.HTTP_400_BAD_REQUEST)

		model_map = {
			"subject": Subject,
			"lesson": LessonResource,
			"general_assessment": GeneralAssessment,
			"lesson_assessment": LessonAssessment,
			"game": GameModel,
			"school": School,
			"county": County,
			"district": District,
			"student": Student,
			"teacher": Teacher,
		}
		ModelCls = model_map.get(model_name)
		if not ModelCls:
			return Response({"detail": "Unsupported model for moderation."}, status=status.HTTP_400_BAD_REQUEST)

		try:
			obj = ModelCls.objects.get(pk=obj_id)
		except ModelCls.DoesNotExist:
			return Response({"detail": "Object not found."}, status=status.HTTP_404_NOT_FOUND)

		if action_name == 'approve':
			obj.status = StatusEnum.APPROVED.value
		elif action_name == 'reject':
			obj.status = StatusEnum.REJECTED.value
		elif action_name in {'request_changes', 'request_review'}:
			obj.status = StatusEnum.REVIEW_REQUESTED.value
		else:
			return Response({"detail": "Unsupported action."}, status=status.HTTP_400_BAD_REQUEST)

		# Persist moderation comment on the object if the field exists
		if hasattr(obj, 'moderation_comment') and comment is not None:
			obj.moderation_comment = str(comment).strip()
			update_fields = ['status', 'moderation_comment']
		else:
			update_fields = ['status']
		if hasattr(obj, 'updated_at'):
			update_fields.append('updated_at')
		obj.save(update_fields=update_fields)

		# Also write an Activity log entry for audit trail
		Activity.objects.create(
			user=request.user,
			type="moderate_content",
			description=f"{action_name} {model_name} #{obj.pk}",
			metadata={
				"model": model_name,
				"object_id": obj.pk,
				"action": action_name,
				"status": obj.status,
				"moderation_comment": comment,
			},
		)
		return Response(
			{
				"id": obj.pk, 
				"model": model_name, 
				"status": obj.status, 
				"moderation_comment": getattr(obj, 'moderation_comment', None)
			})


class OnboardingViewSet(viewsets.ViewSet):
	"""Endpoints to onboard users step-by-step.
	- profilesetup: create user and return token
	- role: set role and create associated profile
	- aboutyou: set personal details and optional institution/grade
	- linkchild: link a student to a parent profile
	"""
	class DummySerializer(serializers.Serializer):
		"""Placeholder for schema generation only."""
		id = serializers.IntegerField(read_only=True)

	serializer_class = DummySerializer

	@extend_schema(request=ProfileSetupSerializer, responses={201: OpenApiResponse(description="Token and user payload")})
	@action(detail=False, methods=['post'], permission_classes=[permissions.AllowAny])
	def profilesetup(self, request):
		data = request.data
		email = (data.get('email') or '').strip().lower()
		phone = (data.get('phone') or '').strip()
		name = (data.get('name') or '').strip()
		password = data.get('password')
		confirm = data.get('confirm_password')

		if not all([email, phone, name, password, confirm]):
			return Response({"detail": "All fields (email, phone, name, password, confirm_password) are required."}, status=400)
		if password != confirm:
			return Response({"detail": "Passwords do not match."}, status=400)
		if len(password) < 6:
			return Response({"detail": "Password must be at least 6 characters."}, status=400)
		if User.objects.filter(phone=phone).exists():
			return Response({"detail": "Phone already in use."}, status=400)
		if email and User.objects.filter(email=email).exists():
			return Response({"detail": "Email already in use."}, status=400)

		user = User.objects.create_user(email=email, phone=phone, name=name, password=password)
		# Issue a token so the user can immediately call onboarding endpoints
		token = AuthToken.objects.create(user)[1]
		return Response({
			"detail": "Account created successfully. Please complete onboarding and wait for approval if required.",
			"token": token,
			"user": {"id": user.id, "name": user.name, "phone": user.phone, "email": user.email, "role": user.role},
		}, status=201)

	@extend_schema(
		description=(
			"Logout the current onboarding session by revoking the active token. "
			"Clients should call this after completing onboarding so the user "
			"can wait for account approval before logging in normally."
		),
		responses={200: OpenApiResponse(description="Token revoked; user logged out.")},
	)
	@action(detail=False, methods=['post'], permission_classes=[permissions.IsAuthenticated])
	def logout_after_setup(self, request):
		"""Revoke the current Knox token used during onboarding.

		This endpoint deletes the AuthToken associated with the current request,
		effectively logging the user out. Subsequent requests must authenticate
		again using a fresh login once the account is approved.
		"""
		# Knox attaches the token instance to the request when using TokenAuthentication
		token = getattr(request, "auth", None)
		if token is not None:
			try:
				token.delete()
			except Exception:
				pass
		return Response({"detail": "Logged out after onboarding."})

	@extend_schema(request=UserRoleSerializer)
	@action(detail=False, methods=['post'], permission_classes=[permissions.IsAuthenticated])
	def userrole(self, request):
		role = (request.data.get('role') or '').strip().upper()
		allowed = {UserRole.STUDENT.value, UserRole.TEACHER.value, UserRole.PARENT.value}
		if role not in allowed:
			return Response({"detail": f"Invalid role. Allowed: {', '.join(sorted(allowed))}"}, status=400)
		user: User = request.user
		user.role = role
		user.save(update_fields=['role', 'updated_at'])

		# ensure profile exists
		if role == UserRole.STUDENT.value and not hasattr(user, 'student'):
			Student.objects.create(profile=user)
		elif role == UserRole.TEACHER.value and not hasattr(user, 'teacher'):
			Teacher.objects.create(profile=user)
		elif role == UserRole.PARENT.value and not hasattr(user, 'parent'):
			Parent.objects.create(profile=user)

		return Response({"role": user.role})

	@extend_schema(request=AboutUserSerializer)
	@action(detail=False, methods=['post'], permission_classes=[permissions.IsAuthenticated])
	def aboutuser(self, request):
		user: User = request.user
		dob_raw = request.data.get('dob')
		gender = request.data.get('gender')
		# School resolution: prefer explicit 'school_id'; fallback to 'school_name'
		school_name = request.data.get('school_name')
		school_id = request.data.get('school_id')
		district_id = request.data.get('district_id')
		grade = request.data.get('grade')

		if dob_raw:
			dob = parse_date(str(dob_raw))
			if not dob:
				return Response({"detail": "Invalid dob. Expected YYYY-MM-DD."}, status=400)
			user.dob = dob
		if gender:
			user.gender = str(gender)[:20]
		user.save(update_fields=['dob', 'gender', 'updated_at'])

		if user.role == UserRole.STUDENT.value and hasattr(user, 'student'):
			s = user.student
			if grade:
				s.grade = str(grade)
			# Resolve school assignment
			school_obj = None
			if school_id:
				school_obj = School.objects.filter(id=school_id).first()
				if not school_obj:
					return Response({"detail": "Invalid school_id."}, status=400)
			elif school_name:
				qs = School.objects.all()
				if district_id:
					qs = qs.filter(district_id=district_id)
				qs = qs.filter(name__iexact=school_name)
				count = qs.count()
				if count == 1:
					school_obj = qs.first()
				elif count == 0:
					return Response({"detail": "School not found. Provide a valid school_id or also include district_id with school_name."}, status=404)
				else:
					return Response({"detail": "Multiple schools match this name. Provide a school_id or also include district_id."}, status=400)
			if school_obj:
				s.school = school_obj
			s.save(update_fields=['grade', 'school', 'updated_at'])
		elif user.role == UserRole.TEACHER.value and hasattr(user, 'teacher'):
			t = user.teacher
			# Resolve school assignment
			school_obj = None
			if school_id:
				school_obj = School.objects.filter(id=school_id).first()
				if not school_obj:
					return Response({"detail": "Invalid school_id."}, status=400)
			elif school_name:
				qs = School.objects.all()
				if district_id:
					qs = qs.filter(district_id=district_id)
				qs = qs.filter(name__iexact=school_name)
				count = qs.count()
				if count == 1:
					school_obj = qs.first()
				elif count == 0:
					return Response({"detail": "School not found. Provide a valid school_id or also include district_id with school_name."}, status=404)
				else:
					return Response({"detail": "Multiple schools match this name. Provide a school_id or also include district_id."}, status=400)
			if school_obj:
				t.school = school_obj
			t.save(update_fields=['school', 'updated_at'])

		return Response({"detail": "Saved"})

	@extend_schema(request=LinkChildSerializer)
	@action(detail=False, methods=['post'], permission_classes=[permissions.IsAuthenticated])
	def linkchild(self, request):
		user: User = request.user
		if user.role != UserRole.PARENT.value or not hasattr(user, 'parent'):
			return Response({"detail": "Only parents can link children."}, status=403)

		child_email = (request.data.get('student_email') or '').strip().lower()
		child_phone = (request.data.get('student_phone') or '').strip()
		student_id = request.data.get('student_id')
		if not (student_id and (child_email or child_phone)):
			return Response({"detail": "Provide student_id and either student_email or student_phone."}, status=400)

		qs = Student.objects.select_related('profile').filter(id=student_id)
		if child_email:
			qs = qs.filter(profile__email__iexact=child_email)
		if child_phone:
			qs = qs.filter(profile__phone=child_phone)
		student = qs.first()
		if not student:
			return Response({"detail": "Student not found with provided identifiers."}, status=404)

		request.user.parent.wards.add(student)
		return Response({"detail": "Child linked."})


class LoginViewSet(viewsets.ViewSet):
	permission_classes = [permissions.AllowAny]

	class DummySerializer(serializers.Serializer):
		"""Placeholder for schema generation only."""
		id = serializers.IntegerField(read_only=True)

	serializer_class = DummySerializer

	@extend_schema(
		description=(
			"Return the authenticated user's profile, including any attached "
			"role-specific profile (student, teacher, or parent)."
		),
		responses={200: OpenApiResponse(description="User profile payload")},
	)
	@action(detail=False, methods=['get'], url_path='userprofile', permission_classes=[permissions.IsAuthenticated])
	def userprofile(self, request):
		"""Return the current authenticated user's profile and attached role profile.

		Response structure:
		- user: basic fields (id, name, email, phone, role)
		- student / teacher / parent: included when available for that user.
		"""
		user: User = request.user
		payload = {
			"user": {
				"id": user.id,
				"name": getattr(user, "name", None),
				"phone": getattr(user, "phone", None),
				"email": getattr(user, "email", None),
				"dob": getattr(user, "dob", None),
				"gender": getattr(user, "gender", None),
				"role": getattr(user, "role", None),
			}
		}

		# Attach student profile snapshot, if present
		student = getattr(user, 'student', None)
		if student is not None:
			payload["student"] = {
				"id": student.id,
				"grade": getattr(student, "grade", None),
				"status": getattr(student, "status", None),
			}

		# Attach teacher profile snapshot, if present
		teacher = getattr(user, 'teacher', None)
		if teacher is not None:
			payload["teacher"] = {
				"id": teacher.id,
				"school_id": getattr(teacher, "school_id", None),
				"status": getattr(teacher, "status", None),
			}

		# Attach parent profile snapshot, if present
		parent = getattr(user, 'parent', None)
		if parent is not None:
			payload["parent"] = {
				"id": parent.id,
			}

		return Response(payload)

	@extend_schema(request=LoginSerializer, responses={200: OpenApiResponse(description="Token and user payload")})
	@action(detail=False, methods=['post'], url_path='student')
	def studentlogin(self, request):
		'''Student login endpoint.\n
		[identifier]: email or phone \n
		[password]: user's password
		'''
		return self._login_with_role(
			request,
			allowed_roles={UserRole.STUDENT.value},
			forbidden_msg="Only students can use this endpoint.",
		)

	@extend_schema(request=LoginSerializer, responses={200: OpenApiResponse(description="Token and user payload")})
	@action(detail=False, methods=['post'], url_path='content')
	def contentlogin(self, request):
		'''Content creator, validator, and teacher login endpoint.\n
		[identifier]: email or phone \n
		[password]: user's password'''
		allowed = {UserRole.CONTENTCREATOR.value, UserRole.CONTENTVALIDATOR.value, UserRole.TEACHER.value}
		return self._login_with_role(request, allowed_roles=allowed)

	@extend_schema(request=LoginSerializer, responses={200: OpenApiResponse(description="Token and user payload")})
	@action(detail=False, methods=['post'], url_path='admin')
	def adminlogin(self, request):
		'''Admin login endpoint.\n
		[identifier]: email or phone \n
		[password]: user's password
		'''
		return self._login_with_role(request, allowed_roles={UserRole.ADMIN.value})

	@extend_schema(request=LoginSerializer, responses={200: OpenApiResponse(description="Token and user payload")})
	@action(detail=False, methods=['post'], url_path='parent')
	def parentlogin(self, request):
		'''Parent login endpoint.\n
		[identifier]: email or phone \n
		[password]: user's password
		'''
		return self._login_with_role(
			request,
			allowed_roles={UserRole.PARENT.value},
			forbidden_msg="Only parents can use this endpoint.",
		)

	@extend_schema(
		description=(
			"Change password for the authenticated user. "
			"Requires current_password, new_password, and confirm_password."
		),
		request=ChangePasswordSerializer,
		responses={
			200: OpenApiResponse(
				description="Password changed successfully.",
			),
		},
		examples=[
			OpenApiExample(
				name="ChangePasswordRequest",
				value={
					"current_password": "oldpass123",
					"new_password": "newpass456",
					"confirm_password": "newpass456",
				},
			),
		],
	)
	@action(detail=False, methods=['post'], url_path='change-password', permission_classes=[permissions.IsAuthenticated])
	def change_password(self, request):
		"""Allow any authenticated user to change their password.

		Body:
		- current_password
		- new_password
		- confirm_password
		"""
		user: User = request.user
		current = request.data.get('current_password')
		new = request.data.get('new_password')
		confirm = request.data.get('confirm_password')
		if not all([current, new, confirm]):
			return Response({"detail": "current_password, new_password and confirm_password are required."}, status=400)
		if not user.check_password(current):
			return Response({"detail": "Current password is incorrect."}, status=400)
		if new != confirm:
			return Response({"detail": "New password and confirm password do not match."}, status=400)
		if len(new) < 6:
			return Response({"detail": "New password must be at least 6 characters."}, status=400)
		if new == current:
			return Response({"detail": "New password must be different from current password."}, status=400)
		user.set_password(new)
		user.save(update_fields=['password', 'updated_at'])
		return Response({"detail": "Password changed successfully."})

	def _login_with_role(self, request, allowed_roles: Set[str], stdprofile=False, forbidden_msg: str | None = None):
		identifier = str(request.data.get('identifier') or '').strip()
		password = request.data.get('password')
		if not identifier or not password:
			return Response({"detail": "identifier and password are required."}, status=400)

		# Find by phone or email
		user = None
		if '@' in identifier:
			user = User.objects.filter(email__iexact=identifier).first()
		else:
			user = User.objects.filter(phone=identifier).first()
		if not user:
			return Response({"detail": "Invalid credentials."}, status=400)
		if not user.is_active or getattr(user, 'deleted', False):
			return Response({"detail": "Account disabled."}, status=403)
		if not user.check_password(password):
			return Response({"detail": "Invalid credentials."}, status=400)
		if user.role not in allowed_roles:
			msg = forbidden_msg or "Insufficient role for this login."
			return Response({"detail": msg}, status=403)
		from elearncore.sysutils.constants import Status as StatusEnum
		# For student logins, require that the linked student profile is approved
		if user.role == UserRole.STUDENT.value and hasattr(user, 'student') and user.student:
			if getattr(user.student, 'status', StatusEnum.PENDING.value) != StatusEnum.APPROVED.value:
				return Response(
					{"detail": "Your account is awaiting approval by a teacher or administrator."},
					status=403,
				)
		# For teacher/content logins, require that the teacher profile is approved
		if user.role == UserRole.TEACHER.value and hasattr(user, 'teacher') and user.teacher:
			if getattr(user.teacher, 'status', StatusEnum.PENDING.value) != StatusEnum.APPROVED.value:
				return Response(
					{"detail": "Your teacher account is awaiting approval by a content validator or administrator."},
					status=403,
				)

		token = AuthToken.objects.create(user)[1]

		student_payload = None
		if user.role == UserRole.STUDENT.value and hasattr(user, 'student') and user.student:
			s = user.student
			school_payload = None
			if getattr(s, 'school_id', None):
				sch = s.school
				# Build a lightweight school snapshot to avoid a full serializer dependency
				district_name = getattr(getattr(sch, 'district', None), 'name', None)
				county_id = getattr(getattr(sch, 'district', None), 'county_id', None)
				county_name = None
				if getattr(sch, 'district', None) and getattr(sch.district, 'county', None):
					county_name = getattr(sch.district.county, 'name', None)
				school_payload = {
					'id': sch.id,
					'name': getattr(sch, 'name', None),
					'district_id': getattr(sch, 'district_id', None),
					'district_name': district_name,
					'county_id': county_id,
					'county_name': county_name,
				}

			student_payload = {
				'id': s.id,
				'grade': getattr(s, 'grade', None),
				'school': school_payload,
			}

		return Response({
			"token": token,
			"user": UserSerializer(user).data,
			**({"student": student_payload} if student_payload else {}),
		})


class DashboardViewSet(viewsets.ViewSet):
	permission_classes = [permissions.IsAuthenticated]

	class DummySerializer(serializers.Serializer):
		"""Placeholder for schema generation only."""
		id = serializers.IntegerField(read_only=True)

	serializer_class = DummySerializer

	def list(self, request):
		user: User = request.user
		student = getattr(user, 'student', None)
		if not student:
			return Response({"detail": "Student profile required."}, status=403)

		# Per-user cache (short TTL)
		cache_key = f"dashboard:{user.id}"
		cached = cache.get(cache_key)
		if cached:
			return Response(cached)

		now = timezone.now()
		in_7 = now + timedelta(days=7)

		# Total courses = subjects for student's grade
		subjects_qs = Subject.objects.filter(grade=student.grade)
		total_courses = subjects_qs.count()

		# Total lessons per subject
		lessons_qs = LessonResource.objects.filter(subject__grade=student.grade)
		total_by_subject: Dict[int, int] = {}
		for row in lessons_qs.values('subject_id').annotate(c=models.Count('id')):
			total_by_subject[row['subject_id']] = row['c']

		# Taken lessons per subject for this student
		taken_qs = TakeLesson.objects.filter(student=student, lesson__subject__grade=student.grade)
		taken_by_subject: Dict[int, int] = {}
		for row in taken_qs.values('lesson__subject_id').annotate(c=models.Count('id', distinct=True)):
			taken_by_subject[row['lesson__subject_id']] = row['c']

		completed_courses = 0
		in_progress_courses = 0
		in_progress_subjects: List[Subject] = []
		for subj in subjects_qs:
			tot = total_by_subject.get(subj.id, 0)
			taken = taken_by_subject.get(subj.id, 0)
			if tot > 0 and taken >= tot:
				completed_courses += 1
			elif taken > 0 and taken < tot:
				in_progress_courses += 1
				in_progress_subjects.append(subj)

		# Assignments due this week
		# Lesson assessments for matching grade, not yet graded by student
		upcoming_lessons_qs = (
			LessonAssessment.objects
			.filter(lesson__subject__grade=student.grade, due_at__gte=now, due_at__lte=in_7)
			.exclude(grades__student=student)
			.select_related('lesson')
			.order_by('due_at')
		)
		# General assessments (platform-wide) without grade association, not yet graded by student
		upcoming_general_qs = (
			GeneralAssessment.objects
			.filter(due_at__gte=now, due_at__lte=in_7)
			.filter(Q(grade__isnull=True) | Q(grade=student.grade))
			.exclude(grades__student=student)
			.order_by('due_at')
		)
		assignments_due_this_week = upcoming_lessons_qs.count() + upcoming_general_qs.count()
		upcoming_items = []
		for la in upcoming_lessons_qs[:10]:
			upcoming_items.append({
				'name': la.title or la.lesson.title,
				'due_in_days': max(0, (la.due_at.date() - now.date()).days) if la.due_at else None,
			})
		for ga in upcoming_general_qs[:10]:
			upcoming_items.append({
				'name': ga.title,
				'due_in_days': max(0, (ga.due_at.date() - now.date()).days) if ga.due_at else None,
			})
		# Sort and trim to 10
		upcoming = sorted(upcoming_items, key=lambda x: (x['due_in_days'] is None, x['due_in_days']))[:10]

		# Streaks
		dates = list(
			taken_qs.annotate(day=TruncDate('created_at')).values_list('day', flat=True).distinct()
		)
		date_set = {d for d in dates}
		# current streak across time
		cur = 0
		d = now.date()
		while d in date_set:
			cur += 1
			d = d - timedelta(days=1)

		# points this month: most recent streak length in current month * 15
		month_dates = sorted([d for d in date_set if d.month == now.date().month and d.year == now.date().year])
		recent_streak = 0
		if month_dates:
			streak = 1
			for i in range(len(month_dates)-1):
				if (month_dates[i+1] - month_dates[i]).days == 1:
					streak += 1
				else:
					streak = 1
			recent_streak = streak
		points_this_month = recent_streak * 15

		# Continue Learning: subjects in progress with progress & hours left
		continue_learning = []
		# Make maps for quick lookups
		lessons_by_subject = {}
		for subj in in_progress_subjects:
			subj_lessons = list(LessonResource.objects.filter(subject=subj).values('id', 'title', 'duration_minutes'))
			lessons_by_subject[subj.id] = subj_lessons
		taken_lesson_ids = set(taken_qs.values_list('lesson_id', flat=True))
		# latest lesson per subject
		latest_by_subject: Dict[int, LessonResource] = {}
		for tl in taken_qs.select_related('lesson__subject').order_by('-created_at'):
			sid = tl.lesson.subject_id
			if sid not in latest_by_subject:
				latest_by_subject[sid] = tl.lesson

		for subj in in_progress_subjects:
			total = total_by_subject.get(subj.id, 0)
			taken = taken_by_subject.get(subj.id, 0)
			percent = int(round((taken / total) * 100)) if total else 0

			lesson_list = lessons_by_subject.get(subj.id, [])
			remaining = [l for l in lesson_list if l['id'] not in taken_lesson_ids]
			# hours left: sum remaining durations; convert to hours
			minutes_left = sum([l['duration_minutes'] or 0 for l in remaining])
			hours_left = round(minutes_left / 60.0, 2)

			last_lesson = latest_by_subject.get(subj.id)
			continue_learning.append({
				'course': subj.name,
				'last_lesson': getattr(last_lesson, 'title', None),
				'percent_complete': percent,
				'hours_left': hours_left,
			})

		# Recent activities feed (3 most recent generic activities for this user)
		recent = [
			{
				'type': a.type,
				'description': a.description,
				'created_at': a.created_at.isoformat(),
				'metadata': a.metadata or {},
			}
			for a in Activity.objects.filter(user=user).order_by('-created_at')[:3]
		]

		# the number of courses completed by the student in their grade as a percentage of total courses in that grade
		overall_progress = (round((completed_courses / total_courses) * 100)) if total_courses > 0 else 0

		# Student ranking badge: prefer Top 20 in any assessment; otherwise engagement rank among grade peers
		student_ranking = {'show': False}

		# Try lesson assessments first (rank within each assessment by score desc)
		lesson_top = (
			LessonAssessmentGrade.objects
			.filter(lesson_assessment__lesson__subject__grade=student.grade)
			.annotate(rank=Window(expression=DenseRank(), partition_by=[F('lesson_assessment')], order_by=F('score').desc()))
			.filter(student=student, rank__lte=20)
			.select_related('lesson_assessment__lesson__subject')
			.order_by('rank', '-created_at')
			.first()
		)

		general_top = (
			GeneralAssessmentGrade.objects
			.filter(Q(assessment__grade__isnull=True) | Q(assessment__grade=student.grade))
			.annotate(rank=Window(expression=DenseRank(), partition_by=[F('assessment')], order_by=F('score').desc()))
			.filter(student=student, rank__lte=20)
			.select_related('assessment')
			.order_by('rank', '-created_at')
			.first()
		)

		best = None
		if lesson_top and general_top:
			best = lesson_top if lesson_top.rank <= general_top.rank else general_top
		elif lesson_top:
			best = lesson_top
		elif general_top:
			best = general_top

		if best is not None:
			if hasattr(best, 'lesson_assessment'):
				subj = getattr(best.lesson_assessment.lesson.subject, 'name', 'Subject')
				title = 'Top Performer'
				subtitle = f"{student.grade} {subj}"
				rank_val = int(best.rank)
				student_ranking = {
					'show': True,
					'type': 'assessment_top20',
					'title': title,
					'subtitle': subtitle,
					'rank': rank_val,
				}
			else:
				ass_title = getattr(best.assessment, 'title', 'Assessment')
				title = 'Top Performer'
				subtitle = f"{student.grade} - {ass_title}"
				rank_val = int(best.rank)
				student_ranking = {
					'show': True,
					'type': 'assessment_top20',
					'title': title,
					'subtitle': subtitle,
					'rank': rank_val,
				}
		else:
			# Engagement rank: compare total taken lessons within same grade
			my_taken = taken_qs.count()
			if my_taken > 0:
				better = (
					TakeLesson.objects
					.filter(student__grade=student.grade)
					.values('student')
					.annotate(c=Count('id'))
					.filter(c__gt=my_taken)
					.count()
				)
				rank_val = int(better) + 1
				if rank_val <= 20:
					student_ranking = {
						'show': True,
						'type': 'engagement_top20',
						'title': 'Top Performer',
						'subtitle': f"{student.grade} Learners",
						'rank': rank_val,
					}

		data = {
			'assignments_due_this_week': assignments_due_this_week,
			'quick_stats': {
				'total_courses': total_courses,
				'completed_courses': completed_courses,
				'in_progress_courses': in_progress_courses,
			},
			'overall_progress_percent': overall_progress,
			'student_ranking': student_ranking,
			'upcoming': upcoming,
			'streaks': {
				'current_study_streak_days': cur,
				'points_this_month': points_this_month,
			},
			'continue_learning': continue_learning,
			'recent_activities': recent,
		}
		cache.set(cache_key, data, timeout=120)  # 2 minutes
		return Response(data)

	@action(detail=False, methods=['get'], url_path='assignmentsdue')
	def assignmentsdue(self, request):
		"""Return all pending assignments for the student due in next 15 days."""
		user: User = request.user
		student = getattr(user, 'student', None)
		if not student:
			return Response({"detail": "Student profile required."}, status=403)

		now = timezone.now()
		in_15 = now + timedelta(days=15)

		# Lesson-based assignments for student's grade, not yet graded
		lesson_qs = (
			LessonAssessment.objects
			.filter(lesson__subject__grade=student.grade, due_at__gte=now, due_at__lte=in_15)
			.exclude(grades__student=student)
			.select_related('lesson__subject')
		)

		# General assessments (platform-wide) for grade or global, not yet graded
		general_qs = (
			GeneralAssessment.objects
			.filter(due_at__gte=now, due_at__lte=in_15)
			.filter(Q(grade__isnull=True) | Q(grade=student.grade))
			.exclude(grades__student=student)
		)

		items = []
		for la in lesson_qs.order_by('due_at'):
			items.append({
				'type': 'lesson',
				'id': la.id,
				'title': la.title or la.lesson.title,
				'course': getattr(getattr(la.lesson, 'subject', None), 'name', None),
				'due_at': la.due_at.isoformat() if la.due_at else None,
				'due_in_days': max(0, (la.due_at.date() - now.date()).days) if la.due_at else None,
			})

		for ga in general_qs.order_by('due_at'):
			items.append({
				'type': 'general',
				'id': ga.id,
				'title': ga.title,
				'course': None,
				'due_at': ga.due_at.isoformat() if ga.due_at else None,
				'due_in_days': max(0, (ga.due_at.date() - now.date()).days) if ga.due_at else None,
			})

		# Sort combined list by due date ascending
		items.sort(key=lambda x: (x['due_in_days'] is None, x['due_in_days']))
		return Response(items)

	@action(detail=False, methods=['get'], url_path='studystats')
	def studystats(self, request):
		"""Return aggregate study stats for the authenticated student."""
		user: User = request.user
		student = getattr(user, 'student', None)
		if not student:
			return Response({"detail": "Student profile required."}, status=403)

		# Active subjects: subjects where student has taken at least one lesson
		active_subjects = (
			TakeLesson.objects
			.filter(student=student)
			.values('lesson__subject_id')
			.distinct()
			.count()
		)

		# Average grade across all assessments (lesson + general)
		lesson_grades = (
			LessonAssessmentGrade.objects
			.filter(student=student)
			.values_list('score', flat=True)
		)
		general_grades = (
			GeneralAssessmentGrade.objects
			.filter(student=student)
			.values_list('score', flat=True)
		)
		all_scores = list(lesson_grades) + list(general_grades)
		avg_grade = float(sum(all_scores) / len(all_scores)) if all_scores else 0.0

		# Estimated study time: sum durations of distinct lessons taken
		lesson_ids = (
			TakeLesson.objects
			.filter(student=student)
			.values_list('lesson_id', flat=True)
			.distinct()
		)
		study_time_minutes = (
			LessonResource.objects
			.filter(id__in=lesson_ids)
			.aggregate(total=models.Sum('duration_minutes'))['total'] or 0
		)
		study_time_hours = round(study_time_minutes / 60.0, 2)

		data = {
			'active_subjects': active_subjects,
			'avg_grade': avg_grade,
			'study_time_hours': study_time_hours,
			'badges': 0,
		}
		return Response(data)


class KidsViewSet(viewsets.ViewSet):
	"""Endpoints tailored for younger students (grades 1–3)."""
	permission_classes = [permissions.IsAuthenticated]

	@extend_schema(
		description="Endpoints tailored for younger students (grades 1–3).",
		responses={200: None},
		examples=[
			OpenApiExample(
				name="KidsDashboardExample",
				value={
					"lessons_completed": 12,
					"streaks_this_week": 4,
					"current_level": "PRIMARY_3",
					"points_earned": 100,
					"todays_challenges": [
						{"name": "Complete 3 lessons", "icon": "lessons"},
						{"name": "Play 2 learning games", "icon": "games"},
						{"name": "Pass a quiz", "icon": "quiz"},
					],
					"continue_learning": [
						{"id": 1, "name": "Addition Basics", "subject": "Mathematics"},
						{"id": 5, "name": "Animals Around Us", "subject": "Science"},
					],
					"recent_activities": [
						{
							"type": "login",
							"description": "User logged in",
							"created_at": "2025-11-18T09:15:00Z",
							"metadata": {"role": "STUDENT"},
						},
						{
							"type": "take_lesson",
							"description": "Took lesson 'Shapes and Colors'",
							"created_at": "2025-11-18T09:30:00Z",
							"metadata": {"lesson_id": 10, "subject_id": 2},
						},
					],
				},
			),
		],
	)
	@action(detail=False, methods=['get'], url_path='dashboard')
	def dashboard(self, request):
		"""Return a simplified dashboard for lower-grade students.

		Cards:
		- lessons_completed: total distinct lessons taken
		- streaks_this_week: number of days in the current week with activity
		- current_level: the student's grade
		- points_earned: streaks_this_week multiplied by a constant (10)
		"""
		user: User = request.user
		student = getattr(user, 'student', None)
		if not student:
			return Response({"detail": "Student profile required."}, status=403)

		# Optionally restrict to lower grades if you encode them in grade string
		# For now, treat any student as eligible for this kids dashboard.

		now = timezone.now()
		# Lessons completed (distinct lessons)
		lessons_completed = (
			TakeLesson.objects
			.filter(student=student)
			.values('lesson_id')
			.distinct()
			.count()
		)

		# Streaks this week: count unique days in the current week with activity
		start_of_week = now.date() - timedelta(days=now.weekday())  # Monday
		end_of_week = start_of_week + timedelta(days=6)
		streak_days = (
			TakeLesson.objects
			.filter(student=student, created_at__date__gte=start_of_week, created_at__date__lte=end_of_week)
			.annotate(day=TruncDate('created_at'))
			.values('day')
			.distinct()
			.count()
		)

		current_level = student.grade
		POINT_MULTIPLIER = 25
		points_earned = streak_days * POINT_MULTIPLIER

		# Predefined daily challenges for kids
		todays_challenges = [
			{"name": "Complete 3 lessons", "icon": "lessons"},
			{"name": "Play 2 learning games", "icon": "games"},
			{"name": "Pass a quiz", "icon": "quiz"},
		]

		# Continue learning: 3 most recent topics the student has touched
		recent_topics_qs = (
			TakeLesson.objects
			.filter(student=student)
			.select_related('lesson__topic')
			.order_by('-created_at')
		)
		seen_topic_ids = set()
		continue_learning = []
		for tl in recent_topics_qs:
			topic = getattr(tl.lesson, 'topic', None)
			if not topic or topic.id in seen_topic_ids:
				continue
			seen_topic_ids.add(topic.id)
			continue_learning.append({
				'id': topic.id,
				'name': topic.name,
				'subject': getattr(getattr(topic, 'subject', None), 'name', None),
			})
			if len(continue_learning) >= 3:
				break

		recent_activities = [
			{
				'type': a.type,
				'description': a.description,
				'created_at': a.created_at.isoformat(),
				'metadata': a.metadata or {},
			}
			for a in Activity.objects.filter(user=user).order_by('-created_at')[:3]
		]

		return Response({
			'lessons_completed': lessons_completed,
			'streaks_this_week': streak_days,
			'current_level': current_level.replace('_', ' '),
			'points_earned': points_earned,
			'todays_challenges': todays_challenges,
			'continue_learning': continue_learning,
			'recent_activities': recent_activities,
		})

	@extend_schema(
		description="Progress garden view showing overall progress and per-subject completion.",
		responses={200: None},
	)
	@action(detail=False, methods=['get'], url_path='progressgarden')
	def progress_garden(self, request):
		"""Return progress metrics for the student in a garden-style view.

		Includes:
		- lessons_completed: total distinct lessons taken
		- longest_streak: longest consecutive days with at least one lesson taken
		- level: student's current grade
		- points: longest_streak multiplied by the same constant used in dashboard
		- subjects: list of subjects with completion percentage and thumbnail
		"""
		user: User = request.user
		student = getattr(user, 'student', None)
		if not student:
			return Response({"detail": "Student profile required."}, status=403)

		# All lessons the student has taken (distinct per lesson)
		taken_qs = (
			TakeLesson.objects
			.filter(student=student)
			.select_related('lesson__subject')
		)

		# Lessons completed count
		lessons_completed = (
			taken_qs
			.values('lesson_id')
			.distinct()
			.count()
		)

		# Longest streak: consecutive days with any taken lesson
		date_list = list(
			taken_qs
			.annotate(day=TruncDate('created_at'))
			.values_list('day', flat=True)
			.distinct()
		)
		date_list.sort()
		longest_streak = 0
		current_streak = 0
		prev_day = None
		for day in date_list:
			if prev_day is None or (day - prev_day).days == 1:
				current_streak += 1
			else:
				current_streak = 1
			prev_day = day
			if current_streak > longest_streak:
				longest_streak = current_streak

		# Level and points (use same POINT_MULTIPLIER as dashboard)
		level = student.grade
		POINT_MULTIPLIER = 25
		points = longest_streak * POINT_MULTIPLIER

		# --- Overall performance score for ranking ---
		# Use combination of distinct lessons taken and average assessment score.
		from django.db.models import Avg, Count as DjangoCount
		my_lessons_taken = lessons_completed
		lesson_scores = list(
			LessonAssessmentGrade.objects
			.filter(student=student)
			.values_list('score', flat=True)
		)
		general_scores = list(
			GeneralAssessmentGrade.objects
			.filter(student=student)
			.values_list('score', flat=True)
		)
		all_scores = lesson_scores + general_scores
		avg_score = float(sum(all_scores) / len(all_scores)) if all_scores else 0.0
		# Simple weighted score: lessons*1 + avg_score*2
		my_perf_score = my_lessons_taken + (avg_score * 2.0)

		def _compute_rank(base_qs, student_field='student'):
			"""Return (rank, total) within a queryset aggregated by student.

			base_qs should be a queryset of TakeLesson or Student-related rows
			within a given scope (school/district/county).
			"""
			# Aggregate lessons taken per student
			agg = (
				TakeLesson.objects
				.filter(**base_qs)
				.values(student_field)
				.annotate(lessons=DjangoCount('id', distinct=True))
			)
			if not agg.exists():
				return None, 0

			student_ids = [row[student_field] for row in agg]
			lessons_by_student = {row[student_field]: row['lessons'] for row in agg}

			# Average scores per student
			lesson_scores_by_student = {
				row['student']: row['avg']
				for row in (
					LessonAssessmentGrade.objects
					.filter(student_id__in=student_ids)
					.values('student')
					.annotate(avg=Avg('score'))
				)
			}
			general_scores_by_student = {
				row['student']: row['avg']
				for row in (
					GeneralAssessmentGrade.objects
					.filter(student_id__in=student_ids)
					.values('student')
					.annotate(avg=Avg('score'))
				)
			}

			perf_scores = []
			for sid in student_ids:
				lessons = lessons_by_student.get(sid, 0) or 0
				ls = lesson_scores_by_student.get(sid)
				gs = general_scores_by_student.get(sid)
				if ls is not None and gs is not None:
					avg = float(ls + gs) / 2.0
				elif ls is not None:
					avg = float(ls)
				elif gs is not None:
					avg = float(gs)
				else:
					avg = 0.0
				perf = lessons + (avg * 2.0)
				perf_scores.append((sid, perf))

			# Sort by performance descending; compute 1-based rank
			perf_scores.sort(key=lambda x: x[1], reverse=True)
			rank = None
			for idx, (sid, score) in enumerate(perf_scores, start=1):
				if sid == student.id:
					rank = idx
					break
			return rank, len(perf_scores)

		# School rank (if school attached)
		school_rank = None
		if getattr(student, 'school_id', None):
			# Filter TakeLesson by students in same school
			base = {'student__school_id': student.school_id}
			school_rank_val, school_total = _compute_rank(base)
			if school_rank_val is not None:
				school_rank = {
					'rank': school_rank_val,
					'out_of': school_total,
				}

		# District rank (if district attached)
		district_rank = None
		student_district_id = getattr(getattr(student.school, 'district', None), 'id', None) if getattr(student, 'school', None) else None
		if student_district_id:
			base = {'student__school__district_id': student_district_id}
			dist_rank_val, dist_total = _compute_rank(base)
			if dist_rank_val is not None:
				district_rank = {
					'rank': dist_rank_val,
					'out_of': dist_total,
				}

		# County rank (if county attached)
		county_rank = None
		student_county_id = None
		if getattr(student, 'school', None) and getattr(student.school, 'district', None) and getattr(student.school.district, 'county', None):
			student_county_id = student.school.district.county_id
		if student_county_id:
			base = {'student__school__district__county_id': student_county_id}
			county_rank_val, county_total = _compute_rank(base)
			if county_rank_val is not None:
				county_rank = {
					'rank': county_rank_val,
					'out_of': county_total,
				}

		# Subject completion: percentage of lessons taken per subject
		# Total lessons per subject (for student's grade)
		all_lessons_qs = (
			LessonResource.objects
			.filter(subject__grade=student.grade)
			.select_related('subject')
		)
		lessons_per_subject = {}
		for row in all_lessons_qs.values('subject_id').annotate(c=models.Count('id')):
			lessons_per_subject[row['subject_id']] = row['c']

		# Taken lessons per subject
		taken_per_subject = {}
		for row in taken_qs.values('lesson__subject_id').annotate(c=models.Count('lesson_id', distinct=True)):
			taken_per_subject[row['lesson__subject_id']] = row['c']

		subjects = Subject.objects.filter(grade=student.grade).order_by('name')
		subjects_payload = []
		for subj in subjects:
			total = int(lessons_per_subject.get(subj.id, 0))
			taken = int(taken_per_subject.get(subj.id, 0))
			percent = int(round((taken / total) * 100)) if total else 0
			subjects_payload.append({
				"id": subj.id,
				"name": subj.name,
				"thumbnail": subj.thumbnail.url if getattr(subj, 'thumbnail', None) else None,
				"percent_complete": percent,
			})

		return Response({
			"lessons_completed": lessons_completed,
			"longest_streak": longest_streak,
			"level": level.replace('_', ' '),
			"points": points,
			"subjects": subjects_payload,
			"rank_in_school": school_rank,
			"rank_in_district": district_rank,
			"rank_in_county": county_rank,
		})

	@extend_schema(
		description="Subjects and lessons for the student's grade (kids view).",
		responses={200: None},
		examples=[
			OpenApiExample(
				name="KidsSubjectsAndLessonsExample",
				value={
					"subjects": [
						{"id": 1, "name": "Mathematics", "grade": "PRIMARY_3"},
						{"id": 2, "name": "Science", "grade": "PRIMARY_3"},
					],
					"lessons": [
						{
							"id": 10,
							"title": "Addition Basics",
							"subject_id": 1,
							"subject_name": "Mathematics",
						},
						{
							"id": 11,
							"title": "Animals Around Us",
							"subject_id": 2,
							"subject_name": "Science",
						},
					],
				},
			),
		],
	)
	@action(detail=False, methods=['get'], url_path='subjectsandlessons')
	def subjects_and_lessons(self, request):
		"""Return subjects and lessons for the student's grade in a simple shape."""
		user: User = request.user
		student = getattr(user, 'student', None)
		if not student:
			return Response({"detail": "Student profile required."}, status=403)

		# Subjects for the student's grade
		subjects_qs = Subject.objects.filter(grade=student.grade).order_by('name')
		subjects_payload = [
			{"id": s.id, "name": s.name, "grade": s.grade, "thumbnail": s.thumbnail.url if s.thumbnail else None}
			for s in subjects_qs
		]

		# Lessons for the student's grade (via subject)
		lessons_qs = (
			LessonResource.objects
			.filter(subject__grade=student.grade)
			.select_related('subject')
			.order_by('subject__name', 'title')
		)
		# Determine which lessons the student has already taken
		taken_lesson_ids = set(
			TakeLesson.objects
			.filter(student=student)
			.values_list('lesson_id', flat=True)
		)
		lessons_payload = [
			{
				"id": l.id,
				"title": l.title,
				"subject_id": l.subject_id,
				"grade": getattr(l.subject, 'grade', None),
				"resource_type": l.type,
				"thumbnail": l.thumbnail.url if l.thumbnail else None,
				"resource": l.resource.url if l.resource else None,
				"subject_name": getattr(l.subject, 'name', None),
				"status": "taken" if l.id in taken_lesson_ids else "new",
			}
			for l in lessons_qs
		]

		return Response({
			"subjects": subjects_payload,
			"lessons": lessons_payload,
		})

	@extend_schema(
		description="All assignments (assessments) for the student's grade.",
		responses={200: None},
		examples=[
			OpenApiExample(
				name="KidsAssignmentsExample",
				value={
					"assignments": [
						{
							"id": 1,
							"title": "Term 1 Math Assessment",
							"type": "general",
							"due_at": "2025-11-20T09:00:00Z",
						},
						{
							"id": 2,
							"title": "Lesson 3 Quiz",
							"type": "lesson",
							"lesson_id": 10,
							"due_at": "2025-11-22T09:00:00Z",
						},
					],
				},
			),
		],
	)
	@action(detail=False, methods=['get'], url_path='assignments')
	def assignments(self, request):
		"""Return all assignments for the student's grade (no due-date filter)."""
		user: User = request.user
		student = getattr(user, 'student', None)
		if not student:
			return Response({"detail": "Student profile required."}, status=403)

		# General assessments for grade or global
		general_qs = (
			GeneralAssessment.objects
			.filter(models.Q(grade__isnull=True) | models.Q(grade=student.grade))
			.order_by('due_at', 'title')
		)

		# Lesson assessments for lessons in this grade
		lesson_qs = (
			LessonAssessment.objects
			.filter(lesson__subject__grade=student.grade)
			.select_related('lesson')
			.order_by('due_at', 'title')
		)

		now = timezone.now()
		in_5 = now + timedelta(days=5)

		# Prefetch existing solutions/grades to avoid per-row queries
		general_solutions = list(
			AssessmentSolution.objects
			.filter(assessment__in=general_qs, student=student)
		)
		general_solution_map = {sol.assessment_id: sol for sol in general_solutions}
		lesson_grade_ids = set(
			LessonAssessmentGrade.objects.filter(
				lesson_assessment__in=lesson_qs,
				student=student,
			).values_list('lesson_assessment_id', flat=True)
		)

		items = []
		total = 0
		pending = 0
		due_soon = 0
		overdue = 0
		submitted = 0

		for ga in general_qs:
			solution_obj = general_solution_map.get(ga.id)
			status = "submitted" if solution_obj is not None else "pending"
			total += 1
			if status == "submitted":
				submitted += 1
			else:
				pending += 1
				if ga.due_at:
					if ga.due_at < now:
						overdue += 1
					elif now <= ga.due_at <= in_5:
						due_soon += 1

			items.append({
				"id": ga.id,
				"title": ga.title,
				"instructions": ga.instructions,
				"type": "general",
				"due_at": ga.due_at.isoformat() if ga.due_at else None,
				"status": status,
				"solution": AssessmentSolutionSerializer(solution_obj).data if solution_obj else None,
			})

		for la in lesson_qs:
			status = "submitted" if la.id in lesson_grade_ids else "pending"
			total += 1
			if status == "submitted":
				submitted += 1
			else:
				pending += 1
				if la.due_at:
					if la.due_at < now:
						overdue += 1
					elif now <= la.due_at <= in_5:
						due_soon += 1

			items.append({
				"id": la.id,
				"title": la.title,
				"instructions": la.instructions,
				"type": "lesson",
				"status": status,
				"solution": AssessmentSolutionSerializer(solution_obj).data if solution_obj else None,
				"due_at": la.due_at.isoformat() if la.due_at else None,
				"status": status,
			})

		stats = {
			"total": total,
			"pending": pending,
			"due_soon": due_soon,
			"overdue": overdue,
			"submitted": submitted,
		}

		return Response({"assignments": items, "stats": stats})

	@extend_schema(
		description="List quizzes (assessments) available for the student's grade.",
		responses={200: None},
		examples=[
			OpenApiExample(
				name="KidsQuizzesExample",
				value={
					"quizzes": [
						{"id": 1, "title": "Math Quick Quiz", "type": "general", "due_at": "2025-11-20T09:00:00Z"},
						{"id": 2, "title": "Lesson 3 Checkup", "type": "lesson", "lesson_id": 10, "due_at": "2025-11-22T09:00:00Z"},
					],
				},
			),
		],
	)
	@action(detail=False, methods=['get'], url_path='quizzes')
	def quizzes(self, request):
		"""Return quizzes (general + lesson assessments) for the student's grade."""
		user: User = request.user
		student = getattr(user, 'student', None)
		if not student:
			return Response({"detail": "Student profile required."}, status=403)

		# General assessments for grade or global
		general_qs = (
			GeneralAssessment.objects
			.filter(models.Q(grade__isnull=True) | models.Q(grade=student.grade))
			.order_by('due_at', 'title')
		)

		# Lesson assessments via lessons in student's grade
		lesson_qs = (
			LessonAssessment.objects
			.filter(lesson__subject__grade=student.grade)
			.select_related('lesson')
			.order_by('due_at', 'title')
		)

		payload = []
		for ga in general_qs:
			payload.append({
				"id": ga.id,
				"title": ga.title,
				"type": "general",
				"due_at": ga.due_at.isoformat() if ga.due_at else None,
			})
		for la in lesson_qs:
			payload.append({
				"id": la.id,
				"title": la.title,
				"type": "lesson",
				"lesson_id": la.lesson_id,
				"due_at": la.due_at.isoformat() if la.due_at else None,
			})

		return Response({"quizzes": payload})

	@extend_schema(
		description="List games available for the student's grade.",
		responses={200: None},
		examples=[
			OpenApiExample(
				name="KidsGamesExample",
				value={
					"games": [
						{"id": 1, "name": "Color Match", "type": "COLOR"},
						{"id": 2, "name": "Number Hunt", "type": "NUMBER"},
					],
				},
			),
		],
	)
	@action(detail=False, methods=['get'], url_path='games')
	def games(self, request):
		"""Return games for the student's grade (currently all games)."""
		user: User = request.user
		student = getattr(user, 'student', None)
		if not student:
			return Response({"detail": "Student profile required."}, status=403)

		# GameModel currently has limited grade linkage, but we can still
		# compute a simple played/new status per game for this student.
		games_qs = GameModel.objects.all().order_by('name')
		played_ids = set(
			GamePlay.objects
			.filter(student=student, game__in=games_qs)
			.values_list('game_id', flat=True)
		)
		payload = [
			{
				"id": g.id,
				"name": g.name,
				"type": g.type,
				"status": "played" if g.id in played_ids else "new",
			}
			for g in games_qs
		]
		return Response({"games": payload})

	@extend_schema(
		description=(
			"Record that the student has played a game. "
			"Pass a game_id in the body; this will upsert a GamePlay row and "
			"log a play_game activity."
		),
		request=None,
		responses={200: OpenApiResponse(description="Game play recorded.")},
	)
	@action(detail=False, methods=['post'], url_path='play-game')
	def play_game(self, request):
		"""Mark a game as played for the current student.

		Body params:
		- game_id: ID of GameModel to mark as played.
		"""
		user: User = request.user
		student = getattr(user, 'student', None)
		if not student:
			return Response({"detail": "Student profile required."}, status=403)

		game_id = request.data.get('game_id')
		if not game_id:
			return Response({"detail": "game_id is required."}, status=400)
		try:
			game_id_int = int(game_id)
		except (TypeError, ValueError):
			return Response({"detail": "game_id must be an integer."}, status=400)

		game = GameModel.objects.filter(id=game_id_int).first()
		if not game:
			return Response({"detail": "Game not found."}, status=404)

		# Upsert GamePlay entry for this student/game
		GamePlay.objects.update_or_create(
			student=student,
			game=game,
			defaults={},
		)

		# Optionally log as an Activity for richer feeds/analytics
		Activity.objects.create(
			user=user,
			type="play_game",
			description=f"Played game '{game.name}'",
			metadata={"game_id": game.id, "game_type": game.type},
		)

		return Response({
			"detail": "Game play recorded.",
			"game": {
				"id": game.id,
				"name": game.name,
				"type": game.type,
			},
		})

	@extend_schema(
		description=(
			"Get the next game for the student. Returns the first unplayed game "
			"(ordered by name). If the student has played all available games, "
			"returns a congratulatory message instead."
		),
		responses={200: None},
	)
	@action(detail=False, methods=['get'], url_path='next-game')
	def next_game(self, request):
		"""Return the next unplayed game for the student.

		If all games have been played, return a congratulatory message.
		"""
		user: User = request.user
		student = getattr(user, 'student', None)
		if not student:
			return Response({"detail": "Student profile required."}, status=403)

		games_qs = GameModel.objects.all().order_by('name')
		if not games_qs.exists():
			return Response({
				"detail": "No games are available yet. Check back soon!",
			})

		played_ids = set(
			GamePlay.objects
			.filter(student=student, game__in=games_qs)
			.values_list('game_id', flat=True)
		)

		# Find first unplayed game in the ordered list
		next_game_obj = None
		for g in games_qs:
			if g.id not in played_ids:
				next_game_obj = g
				break

		if next_game_obj is None:
			# All games have been played by this student
			return Response({
				"detail": "Amazing! You've played all the games available. New challenges are coming soon!",
				"all_played": True,
			})

		return Response({
			"all_played": False,
			"game": {
				"id": next_game_obj.id,
				"name": next_game_obj.name,
				"type": next_game_obj.type,
				"status": "new",
			},
		})

	@extend_schema(
		description="List all assessments (general + lesson) available for the student's grade.",
		responses={200: None},
		examples=[
			OpenApiExample(
				name="KidsAssessmentsExample",
				value={
					"assessments": [
						{"id": 1, "title": "Math Quick Quiz", "type": "general", "marks": 100.0},
						{"id": 2, "title": "Lesson 3 Checkup", "type": "lesson", "lesson_id": 10, "marks": 20.0},
					],
				},
			),
		],
	)
	@action(detail=False, methods=['get'], url_path='assessments')
	def assessments(self, request):
		"""Return all assessments (general + lesson) for the student's grade."""
		user: User = request.user
		student = getattr(user, 'student', None)
		if not student:
			return Response({"detail": "Student profile required."}, status=403)

		general_qs = (
			GeneralAssessment.objects
			.filter(models.Q(grade__isnull=True) | models.Q(grade=student.grade))
			.order_by('title')
		)
		lesson_qs = (
			LessonAssessment.objects
			.filter(lesson__subject__grade=student.grade)
			.select_related('lesson')
			.order_by('title')
		)

		items = []
		for ga in general_qs:
			items.append({
				"id": ga.id,
				"title": ga.title,
				"type": "general",
				"marks": ga.marks,
			})
		for la in lesson_qs:
			items.append({
				"id": la.id,
				"title": la.title,
				"type": "lesson",
				"lesson_id": la.lesson_id,
				"marks": la.marks,
			})

		return Response({"assessments": items})

	@extend_schema(
		description=(
			"Get questions and options for a specific assessment. "
			"Pass either ?general_id=<id> or ?lesson_id=<id>."
		),
		responses={200: None},
		examples=[
			OpenApiExample(
				name="KidsAssessmentQuestionsExample",
				value={
					"assessment": {"id": 1, "title": "Math Quick Quiz", "type": "general"},
					"questions": [
						{
							"id": 10,
							"type": "MULTIPLE_CHOICE",
							"question": "What is 2 + 2?",
							"options": [
								{"id": 100, "value": "3"},
								{"id": 101, "value": "4"},
								{"id": 102, "value": "5"},
							],
						},
					],
				},
			),
		],
	)
	@action(detail=False, methods=['get'], url_path='assessment-questions')
	def assessment_questions(self, request):
		"""Return questions and options for a given assessment.

		Use query params:
		- general_id: for GeneralAssessment
		- lesson_id: for LessonAssessment
		Exactly one of these must be provided.
		"""
		user: User = request.user
		student = getattr(user, 'student', None)
		if not student:
			return Response({"detail": "Student profile required."}, status=403)

		general_id = request.query_params.get('general_id')
		lesson_id = request.query_params.get('lesson_id')
		if bool(general_id) == bool(lesson_id):
			return Response({"detail": "Provide exactly one of general_id or lesson_id."}, status=400)

		assessment_info = None
		questions_qs = None

		if general_id:
			ga = GeneralAssessment.objects.filter(id=general_id).first()
			if not ga:
				return Response({"detail": "General assessment not found."}, status=404)
			assessment_info = {"id": ga.id, "title": ga.title, "type": "general"}
			questions_qs = ga.questions.all().prefetch_related('options')
		else:
			la = LessonAssessment.objects.filter(id=lesson_id).first()
			if not la:
				return Response({"detail": "Lesson assessment not found."}, status=404)
			assessment_info = {"id": la.id, "title": la.title, "type": "lesson"}
			questions_qs = la.questions.all().prefetch_related('options')

		questions_payload = []
		for q in questions_qs:
			questions_payload.append({
				"id": q.id,
				"type": q.type,
				"question": q.question,
				"options": [
					{"id": opt.id, "value": opt.value}
					for opt in q.options.all()
				],
			})

		return Response({
			"assessment": assessment_info,
			"questions": questions_payload,
		})

	@extend_schema(
		description="All assessment grades (lesson + general) for the student.",
		responses={200: None},
		examples=[
			OpenApiExample(
				name="KidsGradesExample",
				value={
					"lesson_grades": [
						{
							"id": 1,
							"lesson_assessment_id": 5,
							"lesson_title": "Addition Basics Quiz",
							"score": 85.0,
							"marks": 100.0,
							"created_at": "2025-11-18T09:00:00Z",
						},
					],
					"general_grades": [
						{
							"id": 2,
							"assessment_id": 3,
							"assessment_title": "Term 1 Assessment",
							"score": 78.0,
							"marks": 100.0,
							"created_at": "2025-11-17T14:30:00Z",
						},
					],
				},
			),
		],
	)
	@action(detail=False, methods=['get'], url_path='grades')
	def grades(self, request):
		"""Return lesson assessment grades and general assessment grades for the student."""
		user: User = request.user
		student = getattr(user, 'student', None)
		if not student:
			return Response({"detail": "Student profile required."}, status=403)

		lesson_qs = (
			LessonAssessmentGrade.objects
			.filter(student=student)
			.select_related('lesson_assessment')
			.order_by('-created_at')
		)
		general_qs = (
			GeneralAssessmentGrade.objects
			.filter(student=student)
			.select_related('assessment')
			.order_by('-created_at')
		)

		lesson_payload = [
			{
				"id": g.id,
				"lesson_assessment_id": g.lesson_assessment_id,
				"lesson_title": getattr(g.lesson_assessment, 'title', None),
				"score": g.score,
				"marks": getattr(g.lesson_assessment, 'marks', None),
				"created_at": g.created_at.isoformat(),
			}
			for g in lesson_qs
		]
		general_payload = [
			{
				"id": g.id,
				"assessment_id": g.assessment_id,
				"assessment_title": getattr(g.assessment, 'title', None),
				"score": g.score,
				"marks": getattr(g.assessment, 'marks', None),
				"created_at": g.created_at.isoformat(),
			}
			for g in general_qs
		]

		return Response({
			"lesson_grades": lesson_payload,
			"general_grades": general_payload,
		})

	@extend_schema(
		description=(
			"Submit a solution for an assessment or assignment. "
			"Send either general_id (for GeneralAssessment) or lesson_id (for LessonAssessment), "
			"plus optional text solution and/or file attachment.\n"
			" \n\nBody params: \n- general_id: ID of GeneralAssessment (optional)\n- lesson_id: ID of LessonAssessment (optional)\n- solution: free-text answer (optional)\n- attachment: file upload (optional)"
		),
		request=None,
		responses={
			200: OpenApiResponse(description="Solution submitted or updated"),
		},
	)
	@action(detail=False, methods=['post'], url_path='submit-solution')
	def submit_solution(self, request):
		"""Allow a kid to submit a solution for an assessment.

		Body params:
		- general_id: ID of GeneralAssessment (optional)
		- lesson_id: ID of LessonAssessment (optional)
		- solution: free-text answer (optional)
		- attachment: file upload (optional)

		Exactly one of general_id or lesson_id must be provided. Currently,
		this implementation stores solutions only for GeneralAssessment.
		"""
		user: User = request.user
		student = getattr(user, 'student', None)
		if not student:
			return Response({"detail": "Student profile required."}, status=403)

		general_id = request.data.get('general_id')
		lesson_id = request.data.get('lesson_id')
		if bool(general_id) == bool(lesson_id):
			return Response({"detail": "Provide exactly one of general_id or lesson_id."}, status=400)

		text_solution = request.data.get('solution', '')
		attachment = request.FILES.get('attachment')

		if general_id:
			assessment = GeneralAssessment.objects.filter(id=general_id).first()
			if not assessment:
				return Response({"detail": "General assessment not found."}, status=404)

			# Create or update AssessmentSolution for this student/assessment
			solution_obj, created = AssessmentSolution.objects.get_or_create(
				assessment=assessment,
				student=student,
				defaults={
					'solution': text_solution or "",
				}
			)
			if not created:
				if text_solution:
					solution_obj.solution = text_solution
				if attachment is not None:
					solution_obj.attachment = attachment
				solution_obj.save()
			elif attachment is not None:
				solution_obj.attachment = attachment
				solution_obj.save()

			return Response({
				"detail": "Solution submitted.",
				"solution_id": solution_obj.id,
			})

		# Placeholder for future LessonAssessment solution handling
		return Response({"detail": "Lesson assessment solutions are not yet supported."}, status=400)




class TeacherViewSet(viewsets.ViewSet):
	"""Endpoints specifically for teachers to manage their classroom.

	Teachers can:
	- View subjects and lessons for their grade.
	- Create lessons and assessments.
	- View students in their school and approve them.
	"""
	permission_classes = [permissions.IsAuthenticated]

	def _require_teacher(self, request):
		user: User = request.user
		if not user or getattr(user, 'role', None) not in {UserRole.TEACHER.value, UserRole.ADMIN.value}:
			return Response({"detail": "Teacher role required."}, status=403)
		if not hasattr(user, 'teacher'):
			return Response({"detail": "Teacher profile required."}, status=403)
		return None

	def _grade_for_score(self, score: float):
		"""Map numeric score (0-100) to grade letter and remark."""
		if score is None:
			return "N/A", "No score"
		if score >= 96:
			return "A+", "Excellent"
		if score >= 90:
			return "A-", "Very good"
		if score >= 86:
			return "B+", "Good"
		if score >= 80:
			return "B-", "Good"
		if score >= 76:
			return "C+", "Fair"
		if score >= 70:
			return "C-", "Fair"
		if score >= 65:
			return "D+", "Poor"
		if score >= 60:
			return "D-", "Poor"
		return "F", "Fail"

	def _grade_status(self, grade_letter: str, remark: str) -> str:
		"""Collapse detailed remarks into UI-friendly status buckets.

		Excellent: top scores (Excellent/Very good, A-range).
		Good: middle scores (Good/Fair).
		Needs Improvement: Poor/Fail and anything else.
		"""
		if remark in {"Excellent", "Very good"} or grade_letter in {"A+", "A", "A-"}:
			return "Excellent"
		if remark in {"Good", "Fair"}:
			return "Good"
		return "Needs Improvement"

	@extend_schema(
		description="List subjects for the teacher's grade.",
		responses={200: SubjectSerializer(many=True)},
	)
	@action(detail=False, methods=['get'], url_path='subjects')
	def my_subjects(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher
		# For now, return all subjects linked to this teacher profile.
		qs = Subject.objects.filter(teachers=teacher).order_by('name')
		return Response(SubjectSerializer(qs, many=True).data)

	@extend_schema(
		description=(
			"List topics for all subjects assigned to this teacher. "
			"Each topic is returned with its subject information."
		),
		responses={200: TopicSerializer(many=True)},
	)
	@action(detail=False, methods=['get'], url_path='topics')
	def my_topics(self, request):
		"""Return all topics that belong to the teacher's subjects."""
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher
		qs = (
			Topic.objects
			.filter(subject__teachers=teacher)
			.select_related('subject')
			.order_by('subject__name', 'name')
		)
		return Response(TopicSerializer(qs, many=True).data)

	@extend_schema(
		description="List lessons created by this teacher or for their subjects.",
		responses={200: LessonResourceSerializer(many=True)},
	)
	@action(detail=False, methods=['get'], url_path='lessons')
	def my_lessons(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher
		qs = (
			LessonResource.objects
			.filter(subject__teachers=teacher)
			.select_related('subject')
			.order_by('-created_at')
		)
		return Response(LessonResourceSerializer(qs, many=True).data)

	@extend_schema(
		description="Create a new lesson resource for one of the teacher's subjects.",
		request=LessonResourceSerializer,
		responses={201: LessonResourceSerializer},
	)
	@action(detail=False, methods=['post'], url_path='lessons/create')
	def create_lesson(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		ser = LessonResourceSerializer(data=request.data)
		ser.is_valid(raise_exception=True)
		lesson = ser.save(created_by=request.user, status=StatusEnum.DRAFT.value)
		return Response(LessonResourceSerializer(lesson).data, status=201)

	@extend_schema(
		description="List general assessments created by this teacher.",
		responses={200: GeneralAssessmentSerializer(many=True)},
	)
	@action(detail=False, methods=['get'], url_path='general-assessments')
	def my_general_assessments(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher
		qs = GeneralAssessment.objects.filter(given_by=teacher).order_by('-created_at')
		return Response(GeneralAssessmentSerializer(qs, many=True).data)

	@extend_schema(
		description="Create a general assessment scoped to the teacher's grade (optional).",
		request=GeneralAssessmentSerializer,
		responses={201: GeneralAssessmentSerializer},
	)
	@action(detail=False, methods=['post'], url_path='general-assessments/create')
	def create_general_assessment(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher
		ser = GeneralAssessmentSerializer(data=request.data)
		ser.is_valid(raise_exception=True)
		ga = ser.save(given_by=teacher)
		return Response(GeneralAssessmentSerializer(ga).data, status=201)

	@extend_schema(
		description="List lesson assessments created by this teacher.",
		responses={200: LessonAssessmentSerializer(many=True)},
	)
	@action(detail=False, methods=['get'], url_path='lesson-assessments')
	def my_lesson_assessments(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher
		qs = LessonAssessment.objects.filter(given_by=teacher).select_related('lesson').order_by('-created_at')
		return Response(LessonAssessmentSerializer(qs, many=True).data)

	@extend_schema(
		description="Create a lesson assessment for one of the teacher's lessons.",
		request=LessonAssessmentSerializer,
		responses={201: LessonAssessmentSerializer},
	)
	@action(detail=False, methods=['post'], url_path='lesson-assessments/create')
	def create_lesson_assessment(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher
		ser = LessonAssessmentSerializer(data=request.data)
		ser.is_valid(raise_exception=True)
		la = ser.save(given_by=teacher)
		return Response(LessonAssessmentSerializer(la).data, status=201)

	@extend_schema(
		description=(
			"Create a question (with optional options) for one of this teacher's "
			"general or lesson assessments. Exactly one of general_assessment_id "
			"or lesson_assessment_id must be provided."
		),
		request=QuestionCreateSerializer,
		responses={201: QuestionSerializer},
		examples=[
			OpenApiExample(
				name="MultipleChoiceQuestionExample",
				value={
					"general_assessment_id": 12,
					"type": "MULTIPLE_CHOICE",
					"question": "What is 2 + 2?",
					"answer": "4",
					"options": ["3", "4", "5", "6"],
				},
			),
		],
	)
	@action(detail=False, methods=['post'], url_path='questions/create')
	def create_question(self, request):
		"""Create an assessment question and optional options (teacher side)."""
		deny = self._require_teacher(request)
		if deny:
			return deny
		ser = QuestionCreateSerializer(
			data=request.data,
			context={"request": request, "restrict_to_teacher": True},
		)
		ser.is_valid(raise_exception=True)
		question = ser.save()
		return Response(QuestionSerializer(question).data, status=201)

	@extend_schema(
		description=(
			"List questions (with options) for one of this teacher's general or "
			"lesson assessments. Exactly one of general_assessment_id or "
			"lesson_assessment_id must be provided."
		),
		parameters=[
			OpenApiParameter(
				name="general_assessment_id",
				required=False,
				location=OpenApiParameter.QUERY,
				description="ID of a GeneralAssessment to list its questions.",
				type=int,
			),
			OpenApiParameter(
				name="lesson_assessment_id",
				required=False,
				location=OpenApiParameter.QUERY,
				description="ID of a LessonAssessment to list its questions.",
				type=int,
			),
		],
		responses={200: QuestionSerializer(many=True)},
	)
	@action(detail=False, methods=['get'], url_path='questions')
	def list_questions(self, request):
		"""List questions and options for this teacher's specific assessment."""
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher

		ga_id = request.query_params.get('general_assessment_id')
		la_id = request.query_params.get('lesson_assessment_id')
		if bool(ga_id) == bool(la_id):
			return Response(
				{"detail": "Provide exactly one of general_assessment_id or lesson_assessment_id."},
				status=status.HTTP_400_BAD_REQUEST,
			)

		qs = Question.objects.all().prefetch_related('options')
		if ga_id:
			try:
				ga_id_int = int(ga_id)
			except ValueError:
				return Response({"detail": "general_assessment_id must be an integer."}, status=status.HTTP_400_BAD_REQUEST)
			qs = qs.filter(general_assessment_id=ga_id_int, general_assessment__given_by=teacher)
		else:
			try:
				la_id_int = int(la_id)
			except ValueError:
				return Response({"detail": "lesson_assessment_id must be an integer."}, status=status.HTTP_400_BAD_REQUEST)
			qs = qs.filter(lesson_assessment_id=la_id_int, lesson_assessment__given_by=teacher)

		qs = qs.order_by('created_at')
		return Response(QuestionSerializer(qs, many=True).data)

	@extend_schema(
		description="List students in the teacher's school, including their status.",
		responses={200: StudentSerializer(many=True)},
	)
	@action(detail=False, methods=['get'], url_path='students')
	def my_students(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher
		if not getattr(teacher, 'school_id', None):
			return Response([], status=200)
		qs = Student.objects.filter(school_id=teacher.school_id).select_related('profile', 'school').order_by('profile__name')
		return Response(StudentSerializer(qs, many=True).data)

	@extend_schema(
		description="Approve a pending student in the teacher's school.",
		request=None,
		responses={200: StudentSerializer},
	)
	@action(detail=True, methods=['post'], url_path='approve-student')
	def approve_student(self, request, pk=None):
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher
		try:
			student = Student.objects.get(pk=pk, school=teacher.school)
		except Student.DoesNotExist:
			return Response({"detail": "Student not found in your school."}, status=404)
		student.status = StatusEnum.APPROVED.value
		student.moderation_comment = "Approved by teacher"
		student.save(update_fields=['status', 'moderation_comment', 'updated_at'])

		# Notify the student that their account has been approved
		profile = student.profile
		message = (
			f"Hi {profile.name}, your Liberia eLearn student account has been approved.\n"
			"You can now log in and start learning."
		)
		fire_and_forget(
			_send_account_notifications,
			message,
			getattr(profile, "phone", None),
			getattr(profile, "email", None),
			"Your Liberia eLearn student account has been approved",
		)
		return Response(StudentSerializer(student).data)

	@extend_schema(
		description="Reject a pending student in the teacher's school.",
		request=None,
		responses={200: StudentSerializer},
	)
	@action(detail=True, methods=['post'], url_path='reject-student')
	def reject_student(self, request, pk=None):
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher
		try:
			student = Student.objects.get(pk=pk, school=teacher.school)
		except Student.DoesNotExist:
			return Response({"detail": "Student not found in your school."}, status=404)
		student.status = StatusEnum.REJECTED.value
		student.moderation_comment = "Rejected by teacher"
		student.save(update_fields=['status', 'moderation_comment', 'updated_at'])

		# Notify the student that their account has been rejected
		profile = student.profile
		message = (
			f"Hi {profile.name}, your Liberia eLearn student account has been rejected.\n"
			"Please contact your school or teacher for more information."
		)
		fire_and_forget(
			_send_account_notifications,
			message,
			getattr(profile, "phone", None),
			getattr(profile, "email", None),
			"Your Liberia eLearn student account status",
		)
		return Response(StudentSerializer(student).data)

	@extend_schema(
		description=(
			"Teacher dashboard summary: total students, class average, "
			"pending reviews, completion rate, top performers, pending "
			"submissions, and upcoming assessment deadlines."
		),
		responses={200: TeacherDashboardResponseSerializer},
	)
	@action(detail=False, methods=['get'], url_path='dashboard')
	def dashboard(self, request):
		"""Return dashboard metrics and lists for the teacher.

		Quick Actions are hard-coded on the frontend and are not returned here.
		"""
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher

		# If no school context, return an empty dashboard
		if not getattr(teacher, 'school_id', None):
			empty = {
				"summarycards": {
					"total_students": 0,
					"class_average": 0.0,
					"pending_review": 0,
					"completion_rate": 0.0,
				},
				"top_performers": [],
				"pending_submissions": [],
				"upcoming_deadlines": [],
			}
			return Response(empty)

		students_qs = Student.objects.filter(
			school_id=teacher.school_id,
			status=StatusEnum.APPROVED.value,
		)
		student_ids = list(students_qs.values_list('id', flat=True))
		total_students = len(student_ids)

		# ----- Grades for class average and top performers -----
		per_student_scores: Dict[int, List[float]] = {}
		recent_scores: Dict[int, List[float]] = {}
		past_scores: Dict[int, List[float]] = {}
		all_scores: List[float] = []
		cutoff = timezone.now() - timedelta(days=30)

		lesson_grades = (
			LessonAssessmentGrade.objects
			.select_related('lesson_assessment__lesson__subject', 'student__profile', 'lesson_assessment__given_by')
			.filter(lesson_assessment__given_by=teacher, student_id__in=student_ids)
		)

		for g in lesson_grades:
			if g.score is None:
				continue
			score = float(g.score)
			student_id = g.student_id
			per_student_scores.setdefault(student_id, []).append(score)
			all_scores.append(score)
			ts = getattr(g, 'updated_at', getattr(g, 'created_at', None)) or timezone.now()
			bucket = recent_scores if ts >= cutoff else past_scores
			bucket.setdefault(student_id, []).append(score)

		general_grades = (
			GeneralAssessmentGrade.objects
			.select_related('assessment__given_by', 'student__profile')
			.filter(assessment__given_by=teacher, student_id__in=student_ids)
		)

		for g in general_grades:
			if g.score is None:
				continue
			score = float(g.score)
			student_id = g.student_id
			per_student_scores.setdefault(student_id, []).append(score)
			all_scores.append(score)
			ts = getattr(g, 'updated_at', getattr(g, 'created_at', None)) or timezone.now()
			bucket = recent_scores if ts >= cutoff else past_scores
			bucket.setdefault(student_id, []).append(score)

		class_average = sum(all_scores) / len(all_scores) if all_scores else 0.0

		# Build top performers list (sorted by average score desc)
		student_by_id = {s.id: s for s in students_qs.select_related('profile')}
		top_performers = []
		for sid, scores in per_student_scores.items():
			if not scores:
				continue
			avg_score = sum(scores) / len(scores)
			recent_list = recent_scores.get(sid) or []
			past_list = past_scores.get(sid) or []
			if past_list:
				improvement = (sum(recent_list) / len(recent_list) - sum(past_list) / len(past_list)) if recent_list else 0.0
			else:
				improvement = 0.0
			student = student_by_id.get(sid)
			name = getattr(getattr(student, 'profile', None), 'name', None) if student else None
			code = getattr(student, 'student_id', None) if student else None
			top_performers.append({
				"student_name": name,
				"student_id": code,
				"percentage": round(avg_score, 2),
				"improvement": round(improvement, 2),
			})

		# Sort by percentage desc and take top 3
		top_performers.sort(key=lambda x: x.get("percentage", 0.0), reverse=True)
		top_performers = top_performers[:3]

		# ----- Pending submissions and grading completion -----
		graded_count = pending_count = 0
		pending_submissions = []

		solutions = (
			AssessmentSolution.objects
			.select_related('assessment', 'student__profile')
			.filter(assessment__given_by=teacher, student_id__in=student_ids)
			.order_by('assessment__due_at', 'submitted_at')
		)

		grade_by_solution_id: Dict[int, GeneralAssessmentGrade] = {}
		grade_qs = (
			GeneralAssessmentGrade.objects
			.filter(assessment__given_by=teacher, student_id__in=student_ids)
			.select_related('assessment', 'solution')
		)
		for g in grade_qs:
			if g.solution_id:
				grade_by_solution_id[g.solution_id] = g

		for sol in solutions:
			grade_obj = grade_by_solution_id.get(sol.id)
			if grade_obj:
				graded_count += 1
				continue
			pending_count += 1
			student = sol.student
			student_name = getattr(getattr(student, 'profile', None), 'name', None)
			student_code = getattr(student, 'student_id', None)
			assessment = sol.assessment
			pending_submissions.append({
				"student_name": student_name,
				"student_id": student_code,
				"assessment_title": getattr(assessment, 'title', None),
				"subject": None,
				"due_at": getattr(assessment, 'due_at', None),
				"submitted_at": getattr(sol, 'submitted_at', None),
			})
			if len(pending_submissions) >= 5:
				break

		total_review = graded_count + pending_count
		completion_rate = (graded_count / total_review * 100.0) if total_review else 0.0

		# ----- Upcoming deadlines (general and lesson assessments) -----
		now = timezone.now()
		from django.db.models import Count as DjangoCount

		general_upcoming = (
			GeneralAssessment.objects
			.filter(given_by=teacher, due_at__gte=now)
			.annotate(submissions_done=DjangoCount('solutions'))
			.order_by('due_at')
		)

		lesson_upcoming = (
			LessonAssessment.objects
			.filter(given_by=teacher, due_at__gte=now)
			.annotate(submissions_done=DjangoCount('grades'))
			.select_related('lesson__subject')
			.order_by('due_at')
		)

		upcoming_deadlines = []
		total_expected = total_students or 0

		for ga in general_upcoming:
			submissions_done = getattr(ga, 'submissions_done', 0) or 0
			completion = (submissions_done / total_expected * 100.0) if total_expected else 0.0
			days_left = (ga.due_at.date() - now.date()).days if ga.due_at else 0
			upcoming_deadlines.append({
				"assessment_title": ga.title,
				"subject": None,
				"submissions_done": submissions_done,
				"submissions_expected": total_expected,
				"completion_percentage": round(completion, 2),
				"due_at": ga.due_at,
				"days_left": days_left,
			})

		for la in lesson_upcoming:
			submissions_done = getattr(la, 'submissions_done', 0) or 0
			completion = (submissions_done / total_expected * 100.0) if total_expected else 0.0
			days_left = (la.due_at.date() - now.date()).days if la.due_at else 0
			subject_name = getattr(getattr(la.lesson, 'subject', None), 'name', None)
			upcoming_deadlines.append({
				"assessment_title": la.title,
				"subject": subject_name,
				"submissions_done": submissions_done,
				"submissions_expected": total_expected,
				"completion_percentage": round(completion, 2),
				"due_at": la.due_at,
				"days_left": days_left,
			})

		# Sort upcoming deadlines by due date ascending and limit
		upcoming_deadlines.sort(key=lambda x: x.get("due_at") or now)
		upcoming_deadlines = upcoming_deadlines[:5]

		response_data = {
			"summarycards": {
				"total_students": total_students,
				"class_average": round(class_average, 2),
				"pending_review": pending_count,
				"completion_rate": round(completion_rate, 2),
			},
			"top_performers": top_performers,
			"pending_submissions": pending_submissions,
			"upcoming_deadlines": upcoming_deadlines,
		}
		return Response(response_data)

	@extend_schema(
		description=(
			"List all grades recorded by this teacher for their students, "
			"along with summary cards for Excellent, Good, and Needs Improvement."
		),
		responses={200: TeacherGradesResponseSerializer},
	)
	@action(detail=False, methods=['get'], url_path='grades')
	def grades(self, request):
		"""Return all grades the teacher has recorded, plus summary cards.

		Rows combine lesson assessment grades and general assessment grades
		where this teacher is the `given_by` teacher.
		"""
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher

		items = []
		total = excellent = good = needs_improvement = 0

		# Lesson assessment grades
		lesson_grades = (
			LessonAssessmentGrade.objects
			.select_related('lesson_assessment__lesson__subject', 'student__profile', 'lesson_assessment__given_by')
			.filter(lesson_assessment__given_by=teacher)
		)
		for g in lesson_grades:
			if g.score is None:
				continue
			score = float(g.score)
			grade_letter, remark = self._grade_for_score(score)
			status_bucket = self._grade_status(grade_letter, remark)
			total += 1
			if status_bucket == "Excellent":
				excellent += 1
			elif status_bucket == "Good":
				good += 1
			else:
				needs_improvement += 1

			student = g.student
			student_name = getattr(getattr(student, 'profile', None), 'name', None)
			student_code = getattr(student, 'student_id', None)
			subject_name = getattr(getattr(getattr(g.lesson_assessment, 'lesson', None), 'subject', None), 'name', None)
			updated_at = getattr(g, 'updated_at', getattr(g, 'created_at', None))

			items.append({
				"student_name": student_name,
				"student_id": student_code,
				"subject": subject_name,
				"grade_letter": grade_letter,
				"percentage": round(score, 2),
				"status": status_bucket,
				"updated_at": updated_at,
			})

		# General assessment grades
		general_grades = (
			GeneralAssessmentGrade.objects
			.select_related('assessment__given_by', 'student__profile')
			.filter(assessment__given_by=teacher)
		)
		for g in general_grades:
			if g.score is None:
				continue
			score = float(g.score)
			grade_letter, remark = self._grade_for_score(score)
			status_bucket = self._grade_status(grade_letter, remark)
			total += 1
			if status_bucket == "Excellent":
				excellent += 1
			elif status_bucket == "Good":
				good += 1
			else:
				needs_improvement += 1

			student = g.student
			student_name = getattr(getattr(student, 'profile', None), 'name', None)
			student_code = getattr(student, 'student_id', None)
			updated_at = getattr(g, 'updated_at', getattr(g, 'created_at', None))

			items.append({
				"student_name": student_name,
				"student_id": student_code,
				"subject": None,
				"grade_letter": grade_letter,
				"percentage": round(score, 2),
				"status": status_bucket,
				"updated_at": updated_at,
			})

		# Sort by most recently updated first to match UI expectations
		try:
			items.sort(key=lambda x: x.get("updated_at") or timezone.now(), reverse=True)
		except Exception:
			pass

		return Response({
			"summary": {
				"total_grades": total,
				"excellent": excellent,
				"good": good,
				"needs_improvement": needs_improvement,
			},
			"grades": items,
		})

	@extend_schema(
		description=(
			"Create a new student user and profile. "
			"The student will be associated with the teacher's school by default, "
			"or with the provided school_id if allowed. A temporary password is "
			"generated and sent to the student's phone/email."
		),
		request=TeacherCreateStudentSerializer,
		responses={201: StudentSerializer},
	)
	@action(detail=False, methods=['post'], url_path='students/create')
	def create_student(self, request):
		"""Create a new student (User + Student profile) under this teacher.

		Teachers provide basic user details; the system generates a temp password
		and sends it via SMS/email.
		"""
		from django.db import transaction
		from accounts.models import User, School

		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher

		ser = TeacherCreateStudentSerializer(data=request.data)
		ser.is_valid(raise_exception=True)
		data = ser.validated_data

		name = data["name"].strip()
		phone = data["phone"].strip()
		email = (data.get("email") or "").strip() or None
		grade = (data.get("grade") or "").strip() or None
		school_id = data.get("school_id")

		# Resolve school: default to teacher's school if not explicitly provided
		school = None
		if school_id is not None:
			try:
				school = School.objects.get(id=school_id)
			except School.DoesNotExist:
				return Response({"detail": "School not found."}, status=status.HTTP_400_BAD_REQUEST)
			# Enforce same-school constraint for non-admin teachers
			if request.user.role != UserRole.ADMIN.value and teacher.school_id and school.id != teacher.school_id:
				return Response({"detail": "You can only create students in your own school."}, status=status.HTTP_403_FORBIDDEN)
		else:
			school = teacher.school

		if school is None:
			return Response({"detail": "No school context available to assign to the student."}, status=status.HTTP_400_BAD_REQUEST)

		import secrets
		import string
		alphabet = string.ascii_letters + string.digits
		temp_password = "".join(secrets.choice(alphabet) for _ in range(10))

		with transaction.atomic():
			user = User(
				name=name,
				phone=phone,
				email=email,
				role=UserRole.STUDENT.value,
			)
			user.set_password(temp_password)
			user.save()

			student_kwargs = {"profile": user, "school": school}
			if grade:
				student_kwargs["grade"] = grade
			student = Student.objects.create(**student_kwargs)

		# Notify student via SMS/email with temp password
		message = (
			f"Hi {name}, your Liberia eLearn student account has been created.\n"
			f"Login with phone: {phone} and password: {temp_password}.\n"
			"Please change this password after your first login."
		)
		fire_and_forget(
			_send_account_notifications,
			message,
			phone,
			email,
			"Your Liberia eLearn student account",
		)

		return Response(StudentSerializer(student).data, status=status.HTTP_201_CREATED)

	@extend_schema(
		description=(
			"Bulk create students from an uploaded CSV file. "
			"Each row should at least include 'name' and 'phone'. "
			"Optional columns: 'email', 'grade', 'gender', 'dob', 'school_id'."
		),
		request=TeacherBulkStudentUploadSerializer,
		responses={
			200: OpenApiResponse(
				description=(
					"Bulk create summary with per-row statuses. "
					"Each result item includes the CSV row number, status, and basic student info or errors."
				),
			),
		},
	)
	@action(detail=False, methods=['post'], url_path='students/bulk-create')
	def bulk_create_students(self, request):
		"""Bulk create students for this teacher from a CSV upload.

		The CSV file must have a header row. Required columns:
		- name
		- phone

		Optional columns:
		- email
		- grade
		- gender
		- dob (YYYY-MM-DD)
		- school_id (if omitted, defaults to the teacher's school)
		"""
		from django.db import transaction
		from rest_framework.exceptions import ValidationError

		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher

		upload_ser = TeacherBulkStudentUploadSerializer(data=request.data)
		upload_ser.is_valid(raise_exception=True)
		file_obj = upload_ser.validated_data['file']

		try:
			decoded = file_obj.read().decode('utf-8-sig')
		except Exception:
			return Response({"detail": "Unable to read uploaded file as UTF-8 text."}, status=status.HTTP_400_BAD_REQUEST)

		if not decoded.strip():
			return Response({"detail": "Uploaded file is empty."}, status=status.HTTP_400_BAD_REQUEST)

		reader = csv.DictReader(io.StringIO(decoded))
		if not reader.fieldnames:
			return Response({"detail": "CSV file has no header row."}, status=status.HTTP_400_BAD_REQUEST)

		required_columns = ['name', 'phone']
		missing = [c for c in required_columns if c not in reader.fieldnames]
		if missing:
			return Response({"detail": f"Missing required columns: {', '.join(missing)}."}, status=status.HTTP_400_BAD_REQUEST)

		results = []
		created_count = 0
		failed_count = 0

		for row_index, row in enumerate(reader, start=2):  # data rows start at line 2
			row_result = {"row": row_index}

			# Map CSV row to the single-create serializer fields
			mapped = {
				"name": (row.get("name") or "").strip(),
				"phone": (row.get("phone") or "").strip(),
				"email": (row.get("email") or "").strip() or None,
				"grade": (row.get("grade") or "").strip() or None,
				"gender": (row.get("gender") or "").strip() or None,
				"dob": _parse_bulk_date(row.get("dob")),
			}
			school_id_value = (row.get("school_id") or "").strip()
			if school_id_value:
				try:
					mapped["school_id"] = int(school_id_value)
				except ValueError:
					results.append({**row_result, "status": "error", "errors": {"school_id": ["Invalid integer."]}})
					failed_count += 1
					continue

			ser = TeacherCreateStudentSerializer(data=mapped)
			try:
				ser.is_valid(raise_exception=True)
			except ValidationError as exc:
				results.append({**row_result, "status": "error", "errors": exc.detail})
				failed_count += 1
				continue

			data = ser.validated_data
			name = data["name"].strip()
			phone = data["phone"].strip()
			email = (data.get("email") or "").strip() or None
			grade = (data.get("grade") or "").strip() or None
			gender = (data.get("gender") or "").strip() or None
			dob = data.get("dob")
			school_id = data.get("school_id")

			# Resolve school similar to the single create endpoint
			school = None
			if school_id is not None:
				try:
					school = School.objects.get(id=school_id)
				except School.DoesNotExist:
					results.append({**row_result, "status": "error", "errors": {"school_id": ["School not found."]}})
					failed_count += 1
					continue
				if request.user.role != UserRole.ADMIN.value and teacher.school_id and school.id != teacher.school_id:
					results.append({**row_result, "status": "error", "errors": {"school_id": ["You can only create students in your own school."]}})
					failed_count += 1
					continue
			else:
				school = teacher.school

			if school is None:
				results.append({**row_result, "status": "error", "errors": {"school": ["No school context available to assign to the student."]}})
				failed_count += 1
				continue

			import secrets
			import string
			alphabet = string.ascii_letters + string.digits
			temp_password = "".join(secrets.choice(alphabet) for _ in range(10))

			try:
				with transaction.atomic():
					user = User(
						name=name,
						phone=phone,
						email=email,
						role=UserRole.STUDENT.value,
						dob=dob,
						gender=gender,
					)
					user.set_password(temp_password)
					user.save()

					student_kwargs = {
						"profile": user,
						"school": school,
						"status": StatusEnum.APPROVED.value,
					}
					if grade:
						student_kwargs["grade"] = grade
					student = Student.objects.create(**student_kwargs)
			except Exception as exc:
				results.append({**row_result, "status": "error", "errors": {"non_field_errors": [str(exc)]}})
				failed_count += 1
				continue

			# Notify via SMS/email with temp password
			message = (
				f"Hi {name}, your Liberia eLearn student account has been created.\n"
				f"Login with phone: {phone} and password: {temp_password}.\n"
				"Please change this password after your first login."
			)
			fire_and_forget(
				_send_account_notifications,
				message,
				phone,
				email,
				"Your Liberia eLearn student account",
			)

			created_count += 1
			results.append({
				**row_result,
				"status": "created",
				"student_db_id": student.id,
				"student_id": student.student_id,
				"name": name,
				"phone": phone,
			})

		return Response({
			"summary": {
				"total_rows": len(results),
				"created": created_count,
				"failed": failed_count,
			},
			"results": results,
		})

	@extend_schema(
		description=(
			"Download a sample CSV template for bulk student creation. "
			"The file includes the correct header columns and example rows."
		),
		responses={
			200: OpenApiResponse(
				description="CSV file with header row and two sample student records.",
			),
		},
	)
	@action(detail=False, methods=['get'], url_path='students/bulk-template')
	def bulk_students_template(self, request):
		"""Return a CSV template for bulk student creation.

		The template contains a header row with all supported columns and
		two example rows to guide teachers on the expected format.
		"""
		deny = self._require_teacher(request)
		if deny:
			return deny

		header = [
			"name",
			"phone",
			"email",
			"grade",
			"gender",
			"dob",
			"school_id",
		]
		example_rows = [
			{
				"name": "Jane Doe",
				"phone": "231770000001",
				"email": "jane@example.com",
				"grade": "PRIMARY_3",
				"gender": "F",
				"dob": "2013-05-10",
				"school_id": "",
			},
			{
				"name": "John Doe",
				"phone": "231770000002",
				"email": "john@example.com",
				"grade": "PRIMARY_4",
				"gender": "M",
				"dob": "2012-09-02",
				"school_id": "5",
			},
		]

		buffer = io.StringIO()
		writer = csv.DictWriter(buffer, fieldnames=header)
		writer.writeheader()
		for row in example_rows:
			writer.writerow(row)

		csv_content = buffer.getvalue()
		response = HttpResponse(csv_content, content_type="text/csv")
		response["Content-Disposition"] = "attachment; filename=students_bulk_template.csv"
		return response

	@extend_schema(
		description=(
			"Grade a general assessment solution for a student. "
			"Only assessments where this teacher is given_by are allowed. "
			"Score must be numeric, non-negative, and not exceed the assessment's total marks."
		),
		request=GradeAssessmentSerializer,
		responses={
			200: OpenApiResponse(
				description="Grading result.",
				response=GradeAssessmentSerializer,
				examples=[
					OpenApiExample(
						name="GradeGeneralAssessmentResponse",
						value={"assessment_id": 1, "student_id": 10, "score": 18.5},
					),
				],
			),
		},
		examples=[
			OpenApiExample(
				name="GradeGeneralAssessmentRequest",
				value={"assessment_id": 1, "student_id": 10, "score": 18.5},
			),
		],
	)
	@action(detail=False, methods=['post'], url_path='grade/general')
	def grade_general_assessment(self, request):
		"""Grade a general assessment for a student.

		Body:
		- assessment_id
		- student_id
		- score
		"""
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher
		assessment_id = request.data.get('assessment_id')
		student_id = request.data.get('student_id')
		score = request.data.get('score')
		if assessment_id is None or student_id is None or score is None:
			return Response({"detail": "assessment_id, student_id and score are required."}, status=400)
		try:
			assessment = GeneralAssessment.objects.get(pk=assessment_id, given_by=teacher)
		except GeneralAssessment.DoesNotExist:
			return Response({"detail": "Assessment not found or not owned by you."}, status=404)
		try:
			student = Student.objects.get(pk=student_id)
		except Student.DoesNotExist:
			return Response({"detail": "Student not found."}, status=404)
		try:
			score_value = float(score)
		except (TypeError, ValueError):
			return Response({"detail": "score must be a number."}, status=400)
		if score_value < 0:
			return Response({"detail": "score cannot be negative."}, status=400)
		if assessment.marks is not None and score_value > float(assessment.marks):
			return Response({"detail": "score cannot exceed assessment total marks."}, status=400)
		grade_obj, _created = GeneralAssessmentGrade.objects.update_or_create(
			assessment=assessment,
			student=student,
			defaults={"score": score_value},
		)
		return Response({
			"assessment_id": grade_obj.assessment_id,
			"student_id": grade_obj.student_id,
			"score": grade_obj.score,
		})

	@extend_schema(
		description=(
			"Grade a lesson assessment for a student. "
			"Only assessments where this teacher is given_by are allowed. "
			"Score must be numeric, non-negative, and not exceed the assessment's total marks."
		),
		request=GradeAssessmentSerializer,
		responses={
			200: OpenApiResponse(
				description="Grading result.",
				response=GradeAssessmentSerializer,
				examples=[
					OpenApiExample(
						name="GradeLessonAssessmentResponse",
						value={"assessment_id": 5, "student_id": 10, "score": 9},
					),
				],
			),
		},
		examples=[
			OpenApiExample(
				name="GradeLessonAssessmentRequest",
				value={"assessment_id": 5, "student_id": 10, "score": 9},
			),
		],
	)
	@action(detail=False, methods=['post'], url_path='grade/lesson')
	def grade_lesson_assessment(self, request):
		"""Grade a lesson assessment for a student.

		Body:
		- assessment_id (lesson_assessment id)
		- student_id
		- score
		"""
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher
		assessment_id = request.data.get('assessment_id')
		student_id = request.data.get('student_id')
		score = request.data.get('score')
		if assessment_id is None or student_id is None or score is None:
			return Response({"detail": "assessment_id, student_id and score are required."}, status=400)
		try:
			assessment = LessonAssessment.objects.select_related('lesson__subject').get(pk=assessment_id, given_by=teacher)
		except LessonAssessment.DoesNotExist:
			return Response({"detail": "Assessment not found or not owned by you."}, status=404)
		try:
			student = Student.objects.get(pk=student_id)
		except Student.DoesNotExist:
			return Response({"detail": "Student not found."}, status=404)
		try:
			score_value = float(score)
		except (TypeError, ValueError):
			return Response({"detail": "score must be a number."}, status=400)
		if score_value < 0:
			return Response({"detail": "score cannot be negative."}, status=400)
		if assessment.marks is not None and score_value > float(assessment.marks):
			return Response({"detail": "score cannot exceed assessment total marks."}, status=400)
		grade_obj, _created = LessonAssessmentGrade.objects.update_or_create(
			lesson_assessment=assessment,
			student=student,
			defaults={"score": score_value},
		)
		return Response({
			"assessment_id": grade_obj.lesson_assessment_id,
			"student_id": grade_obj.student_id,
			"score": grade_obj.score,
		})

	@extend_schema(
		description=(
			"List all submissions for assessments created by this teacher. "
			"Includes grading status and solution details."
		),
		responses={
			200: OpenApiResponse(
				description="Submissions list with summary.",
				response=ParentSubmissionsResponseSerializer,
				examples=[
					OpenApiExample(
						name="TeacherSubmissionsExample",
						value={
							"submissions": [
								{
									"child_name": "Jane Doe",
									"assessment_title": "Midterm Essay",
									"subject": "Mathematics",
									"score": 18.0,
									"assessment_score": 20.0,
									"submission_status": "Graded",
									"solution": {
										"solution": "My essay answer...",
										"attachment": "https://example.com/uploads/essay.pdf",
									},
									"date_submitted": "2025-01-12T10:00:00Z",
								},
								{
									"child_name": "John Doe",
									"assessment_title": "Science Project",
									"subject": None,
									"score": None,
									"assessment_score": 30.0,
									"submission_status": "Pending Review",
									"solution": {
										"solution": "Project details...",
										"attachment": None,
									},
									"date_submitted": "2025-01-18T14:30:00Z",
								},
							],
							"summary": {
								"graded": 1,
								"pending": 1,
							},
						},
					),
				],
			),
		},
	)
	@action(detail=False, methods=['get'], url_path='submissions')
	def submissions(self, request):
		"""Return all submissions for assessments created by this teacher.

		Each item includes child name, assessment title, score (if graded),
		allocated score, submission status, solution details, and submission date.
		"""
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher

		# General assessment submissions for assessments given by this teacher
		solutions = (
			AssessmentSolution.objects
			.select_related('assessment', 'student__profile')
			.filter(assessment__given_by=teacher)
		)

		# Map grades by solution id
		grade_by_solution_id: Dict[int, GeneralAssessmentGrade] = {}
		grade_qs = GeneralAssessmentGrade.objects.filter(assessment__given_by=teacher).select_related('assessment', 'solution')
		for g in grade_qs:
			if g.solution_id:
				grade_by_solution_id[g.solution_id] = g

		items = []
		graded_count = pending_count = 0

		for sol in solutions:
			student = sol.student
			child_name = getattr(getattr(student, 'profile', None), 'name', None)
			assessment = sol.assessment
			grade_obj = grade_by_solution_id.get(sol.id)
			child_score = float(grade_obj.score) if grade_obj else None
			allocated = float(getattr(assessment, 'marks', 0.0) or 0.0)
			status_label = "Graded" if grade_obj else "Pending Review"
			if grade_obj:
				graded_count += 1
			else:
				pending_count += 1
			items.append({
				"child_name": child_name,
				"assessment_title": getattr(assessment, 'title', None),
				"subject": None,
				"score": child_score,
				"assessment_score": allocated,
				"submission_status": status_label,
				"solution": {
					"solution": sol.solution,
					"attachment": sol.attachment.url if getattr(sol, 'attachment', None) else None,
				},
				"date_submitted": sol.submitted_at,
			})

		# Lesson assessments: we treat graded records as submissions for assessments this teacher created
		lesson_grades = (
			LessonAssessmentGrade.objects
			.select_related('lesson_assessment__lesson__subject', 'student__profile', 'lesson_assessment__given_by')
			.filter(lesson_assessment__given_by=teacher)
		)
		for lg in lesson_grades:
			student = lg.student
			child_name = getattr(getattr(student, 'profile', None), 'name', None)
			la = lg.lesson_assessment
			subject_name = getattr(getattr(la.lesson, 'subject', None), 'name', None)
			child_score = float(lg.score) if lg.score is not None else None
			allocated = float(getattr(la, 'marks', 0.0) or 0.0)
			status_label = "Graded"
			graded_count += 1
			items.append({
				"child_name": child_name,
				"assessment_title": getattr(la, 'title', None),
				"subject": subject_name,
				"score": child_score,
				"assessment_score": allocated,
				"submission_status": status_label,
				"solution": {
					"solution": None,
					"attachment": None,
				},
				"date_submitted": lg.created_at,
			})

		return Response({
			"submissions": items,
			"summary": {
				"graded": graded_count,
				"pending": pending_count,
			},
		})


class LookupPagination(filters.BaseFilterBackend):
	pass

from rest_framework.pagination import PageNumberPagination

class LookupPagination(PageNumberPagination):
	page_size = 20
	page_size_query_param = 'page_size'
	max_page_size = 100


class SchoolLookupViewSet(viewsets.ReadOnlyModelViewSet):
	queryset = School.objects.select_related('district__county').all()
	serializer_class = SchoolLookupSerializer
	permission_classes = [permissions.AllowAny]
	filter_backends = [filters.SearchFilter, filters.OrderingFilter]
	search_fields = ['name', 'district__name', 'district__county__name']
	ordering_fields = ['name', 'created_at']
	pagination_class = LookupPagination

	@method_decorator(cache_page(60 * 10), name='list')
	def dispatch(self, *args, **kwargs):
		return super().dispatch(*args, **kwargs)

	def get_queryset(self):
		qs = super().get_queryset()
		q = (self.request.query_params.get('q') or '').strip()
		district_id = self.request.query_params.get('district_id')
		county_id = self.request.query_params.get('county_id')
		if district_id:
			qs = qs.filter(district_id=district_id)
		if county_id:
			qs = qs.filter(district__county_id=county_id)
		if q:
			qs = qs.filter(name__icontains=q)
		return qs


class CountyLookupViewSet(viewsets.ReadOnlyModelViewSet):
	queryset = County.objects.all()
	serializer_class = CountyLookupSerializer
	permission_classes = [permissions.AllowAny]
	filter_backends = [filters.SearchFilter, filters.OrderingFilter]
	search_fields = ['name']
	ordering_fields = ['name']
	pagination_class = LookupPagination

	@method_decorator(cache_page(60 * 10), name='list')
	def dispatch(self, *args, **kwargs):
		return super().dispatch(*args, **kwargs)

	def get_queryset(self):
		qs = super().get_queryset()
		q = (self.request.query_params.get('q') or '').strip()
		if q:
			qs = qs.filter(name__icontains=q)
		return qs


class DistrictLookupViewSet(viewsets.ReadOnlyModelViewSet):
	queryset = District.objects.select_related('county').all()
	serializer_class = DistrictLookupSerializer
	permission_classes = [permissions.AllowAny]
	filter_backends = [filters.SearchFilter, filters.OrderingFilter]
	search_fields = ['name', 'county__name']
	ordering_fields = ['name']
	pagination_class = LookupPagination

	@method_decorator(cache_page(60 * 10), name='list')
	def dispatch(self, *args, **kwargs):
		return super().dispatch(*args, **kwargs)

	def get_queryset(self):
		qs = super().get_queryset()
		q = (self.request.query_params.get('q') or '').strip()
		county_id = self.request.query_params.get('county_id')
		if county_id:
			qs = qs.filter(county_id=county_id)
		if q:
			qs = qs.filter(name__icontains=q)
		return qs


# ---------- Admin CRUD for Geography ----------
class AdminCountyViewSet(viewsets.ModelViewSet):
	queryset = County.objects.all().order_by('name')
	serializer_class = CountySerializer
	permission_classes = [permissions.IsAuthenticated, IsAdminRole, permissions.IsAdminUser]


class AdminDistrictViewSet(viewsets.ModelViewSet):
	queryset = District.objects.select_related('county').all().order_by('name')
	serializer_class = DistrictSerializer
	permission_classes = [permissions.IsAuthenticated, IsAdminRole, permissions.IsAdminUser]


class AdminSchoolViewSet(viewsets.ModelViewSet):
	queryset = School.objects.select_related('district__county').all().order_by('name')
	serializer_class = SchoolSerializer
	permission_classes = [permissions.IsAuthenticated, IsAdminRole, permissions.IsAdminUser]


class AdminDashboardViewSet(viewsets.ViewSet):
	"""Admin dashboard endpoints exposed under /admin/dashboard/."""

	permission_classes = [permissions.IsAuthenticated, IsAdminRole, permissions.IsAdminUser]
	serializer_class = AdminDashboardSerializer

	@extend_schema(
		operation_id="admin_dashboard",
		description=(
			"Admin dashboard metrics including key summary cards, a lessons "
			"status chart, and a list of high-performing learners."
		),
		responses={
			200: OpenApiResponse(
				response=AdminDashboardSerializer,
				examples=[
					OpenApiExample(
						name="AdminDashboardExample",
						value={
							"summary_cards": {
								"schools": {"count": 84, "change_pct": 12.0},
								"districts": {"count": 122, "change_pct": 5.0},
								"teachers": {"count": 18, "change_pct": -3.0},
								"parents": {"count": 473, "change_pct": 15.0},
								"content_creators": {"count": 84, "change_pct": 12.0},
								"content_validators": {"count": 122, "change_pct": 5.0},
								"approved_subjects": {"count": 18, "change_pct": -3.0},
								"lessons_pending_approval": {"count": 473, "change_pct": -3.0},
							},
							"lessons_chart": {
								"granularity": "month",
								"points": [
									{"period": "Jan", "submitted": 400000, "approved": 350000, "rejected": 30000},
									{"period": "Feb", "submitted": 450000, "approved": 380000, "rejected": 32000},
								],
							},
							"high_learners": [
								{"student_id": 1, "name": "Bertha Jones", "subtitle": "Completed 8 quizzes this week"},
								{"student_id": 2, "name": "Prince Samuel", "subtitle": "Achieved 95% in maths subject"},
							],
						},
					),
				],
			),
		},
	)
	def list(self, request):
		"""Return high-level metrics for the admin dashboard.

		This includes summary cards, a monthly lessons chart, and a
		"high learners" list based on recent assessment completions.
		"""
		now = timezone.now()
		current_year = now.year
		current_month = now.month
		prev_year = current_year if current_month > 1 else current_year - 1
		prev_month = current_month - 1 if current_month > 1 else 12

		def _month_counts(qs, year, month, date_field='created_at'):
			filter_kwargs = {
				f"{date_field}__year": year,
				f"{date_field}__month": month,
			}
			return qs.filter(**filter_kwargs).count()

		def _summary_card(qs, *, date_field='created_at', base_filter=None):
			base_qs = qs
			if base_filter is not None:
				base_qs = base_qs.filter(**base_filter)
			total = base_qs.count()
			current_created = _month_counts(base_qs, current_year, current_month, date_field)
			prev_created = _month_counts(base_qs, prev_year, prev_month, date_field)
			if prev_created > 0:
				change_pct = ((current_created - prev_created) / prev_created) * 100.0
			elif current_created > 0:
				change_pct = 100.0
			else:
				change_pct = 0.0
			return {
				"count": total,
				"change_pct": round(change_pct, 1),
			}

		from accounts.models import County, District, School

		summary_cards = {
			"schools": _summary_card(School.objects.all()),
			"districts": _summary_card(District.objects.all()),
			"teachers": _summary_card(Teacher.objects.all()),
			"parents": _summary_card(Parent.objects.all()),
			"content_creators": _summary_card(
				User.objects.filter(role=UserRole.CONTENTCREATOR.value)
			),
			"content_validators": _summary_card(
				User.objects.filter(role=UserRole.CONTENTVALIDATOR.value)
			),
			"approved_subjects": _summary_card(
				Subject.objects.all(),
				base_filter={"status": StatusEnum.APPROVED.value},
			),
			"lessons_pending_approval": _summary_card(
				LessonResource.objects.all(),
				base_filter={"status": StatusEnum.PENDING.value},
			),
		}

		# Lessons chart: monthly breakdown for current year
		lessons_chart_points = []
		for month in range(1, 13):
			month_qs = LessonResource.objects.filter(
				created_at__year=current_year,
				created_at__month=month,
			)
			lessons_chart_points.append(
				{
					"period": datetime(current_year, month, 1).strftime("%b"),
					"submitted": month_qs.count(),
					"approved": month_qs.filter(status=StatusEnum.APPROVED.value).count(),
					"rejected": month_qs.filter(status=StatusEnum.REJECTED.value).count(),
				}
			)

		lessons_chart = {
			"granularity": "month",
			"points": lessons_chart_points,
		}

		# High learners: top students by number of assessment grades in last 7 days
		week_ago = now - timedelta(days=7)
		ga_counts = (
			GeneralAssessmentGrade.objects
			.filter(created_at__gte=week_ago)
			.values("student_id")
			.annotate(c=Count("id"))
		)
		la_counts = (
			LessonAssessmentGrade.objects
			.filter(created_at__gte=week_ago)
			.values("student_id")
			.annotate(c=Count("id"))
		)

		combined: Dict[int, int] = {}
		for row in ga_counts:
			combined[row["student_id"]] = combined.get(row["student_id"], 0) + row["c"]
		for row in la_counts:
			combined[row["student_id"]] = combined.get(row["student_id"], 0) + row["c"]

		# Sort by total completions desc and take top 5
		best_ids = [sid for sid, _ in sorted(combined.items(), key=lambda kv: kv[1], reverse=True)[:5]]
		students_by_id = {
			stu.id: stu
			for stu in Student.objects.filter(id__in=best_ids).select_related("profile")
		}
		high_learners = []
		for sid in best_ids:
			student = students_by_id.get(sid)
			if not student or not getattr(student, "profile", None):
				continue
			name = student.profile.name
			count = combined.get(sid, 0)
			subtitle = f"Completed {count} quizzes this week" if count else "Active learner this week"
			high_learners.append(
				{
					"student_id": sid,
					"name": name,
					"subtitle": subtitle,
				}
			)

		payload = {
			"summary_cards": summary_cards,
			"lessons_chart": lessons_chart,
			"high_learners": high_learners,
		}
		ser = AdminDashboardSerializer(payload)
		return Response(ser.data)
class AdminStudentViewSet(viewsets.ReadOnlyModelViewSet):
	"""Admin-only read access to all students with summary fields."""

	queryset = Student.objects.select_related('profile', 'school').prefetch_related('guardians__profile').all().order_by('profile__name')
	serializer_class = AdminStudentListSerializer
	permission_classes = [permissions.IsAuthenticated, IsAdminRole, permissions.IsAdminUser]
	filter_backends = [filters.SearchFilter, filters.OrderingFilter]
	search_fields = ['profile__name', 'profile__email', 'school__name', 'grade', 'status']
	ordering_fields = ['profile__name', 'school__name', 'grade', 'status', 'created_at']


class AdminParentViewSet(viewsets.ReadOnlyModelViewSet):
	"""Admin-only read access to all parents with summary fields."""

	queryset = Parent.objects.select_related('profile').prefetch_related('wards').all().order_by('profile__name')
	serializer_class = AdminParentListSerializer
	permission_classes = [permissions.IsAuthenticated, IsAdminRole, permissions.IsAdminUser]
	filter_backends = [filters.SearchFilter, filters.OrderingFilter]
	search_fields = ['profile__name', 'profile__email']
	ordering_fields = ['profile__name', 'created_at']


class AdminUserViewSet(viewsets.ReadOnlyModelViewSet):
	"""Admin-only read access to all users.

	Provides list and retrieve endpoints for all `User` records.
	"""

	queryset = User.objects.all().order_by('-created_at')
	serializer_class = UserSerializer
	permission_classes = [permissions.IsAuthenticated, IsAdminRole, permissions.IsAdminUser]
	filter_backends = [filters.SearchFilter, filters.OrderingFilter]
	search_fields = ['name', 'phone', 'email', 'role']
	ordering_fields = ['created_at', 'name', 'phone', 'role']


class AdminContentManagerViewSet(viewsets.ViewSet):
	"""Admin-only endpoints to manage content managers (creators/validators).

	Provides:
	- create: create a single content manager account with a temp password.
	- bulk-create: bulk create from CSV.
	- bulk-template: download a CSV template.
	"""

	permission_classes = [permissions.IsAuthenticated, IsAdminRole, permissions.IsAdminUser]

	@extend_schema(
		operation_id="admin_list_content_managers",
		description=(
			"List all content managers (both content creators and validators). "
			"Each object includes name, email, role, and status."
		),
		responses={200: AdminContentManagerListSerializer(many=True)},
	)
	def list(self, request):
		"""Return all content managers (creators and validators) for admins.

		Status is derived from the underlying user record: ACTIVE/INACTIVE/DELETED.
		"""
		roles = {UserRole.CONTENTCREATOR.value, UserRole.CONTENTVALIDATOR.value}
		qs = User.objects.filter(role__in=roles).order_by('name')
		ser = AdminContentManagerListSerializer(qs, many=True)
		return Response(ser.data)

	@extend_schema(
		operation_id="admin_create_content_manager",
		description=(
			"Create a content manager account (User with role CONTENTCREATOR or CONTENTVALIDATOR). "
			"Admins provide name, phone, optional email, role (creator|validator), and optional gender/dob."
		),
		request=AdminCreateContentManagerSerializer,
		responses={201: UserSerializer},
	)
	@action(detail=False, methods=['post'], url_path='create')
	def create_content_manager(self, request):
		"""Admins create a single content manager (content creator or validator)."""
		from django.db import transaction

		ser = AdminCreateContentManagerSerializer(data=request.data)
		ser.is_valid(raise_exception=True)
		data = ser.validated_data

		name = data["name"].strip()
		phone = data["phone"].strip()
		email = (data.get("email") or "").strip() or None
		gender = (data.get("gender") or "").strip() or None
		dob = data.get("dob")
		role_label = str(data.get("role") or "").strip().lower()

		if role_label == "creator":
			user_role = UserRole.CONTENTCREATOR.value
		else:
			user_role = UserRole.CONTENTVALIDATOR.value

		import secrets
		import string
		alphabet = string.ascii_letters + string.digits
		temp_password = "".join(secrets.choice(alphabet) for _ in range(10))

		with transaction.atomic():
			user = User(
				name=name,
				phone=phone,
				email=email,
				role=user_role,
				dob=dob,
				gender=gender,
			)
			user.set_password(temp_password)
			user.save()

		message = (
			f"Hi {name}, your Liberia eLearn content manager account has been created.\n"
			f"Login with phone: {phone} and password: {temp_password}.\n"
			"Please change this password after your first login."
		)
		fire_and_forget(
			_send_account_notifications,
			message,
			phone,
			email,
			"Your Liberia eLearn content manager account",
		)

		return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)

	@extend_schema(
		operation_id="admin_bulk_create_content_managers",
		description=(
			"Bulk create content manager accounts from a CSV file. "
			"Each row must include name, phone, and role (creator|validator); "
			"optional columns are email, gender, and dob (YYYY-MM-DD or common Excel formats)."
		),
		request=AdminBulkContentManagerUploadSerializer,
		responses={
			200: OpenApiResponse(
				description="Bulk content manager creation summary with per-row statuses.",
			),
		},
	)
	@action(detail=False, methods=['post'], url_path='bulk-create')
	def bulk_create_content_managers(self, request):
		"""Bulk create content manager accounts from a CSV upload."""
		from django.db import transaction
		from rest_framework.exceptions import ValidationError

		upload_ser = AdminBulkContentManagerUploadSerializer(data=request.data)
		upload_ser.is_valid(raise_exception=True)
		file_obj = upload_ser.validated_data['file']

		try:
			decoded = file_obj.read().decode('utf-8-sig')
		except Exception:
			return Response({"detail": "Unable to read uploaded file as UTF-8 text."}, status=status.HTTP_400_BAD_REQUEST)

		if not decoded.strip():
			return Response({"detail": "Uploaded file is empty."}, status=status.HTTP_400_BAD_REQUEST)

		reader = csv.DictReader(io.StringIO(decoded))
		if not reader.fieldnames:
			return Response({"detail": "CSV file has no header row."}, status=status.HTTP_400_BAD_REQUEST)

		required_columns = ['name', 'phone', 'role']
		missing = [c for c in required_columns if c not in reader.fieldnames]
		if missing:
			return Response({"detail": f"Missing required columns: {', '.join(missing)}."}, status=status.HTTP_400_BAD_REQUEST)

		results = []
		created_count = 0
		failed_count = 0

		for row_index, row in enumerate(reader, start=2):
			row_result = {"row": row_index}

			mapped = {
				"name": (row.get("name") or "").strip(),
				"phone": (row.get("phone") or "").strip(),
				"email": (row.get("email") or "").strip() or None,
				"gender": (row.get("gender") or "").strip() or None,
				"dob": _parse_bulk_date(row.get("dob")),
				"role": (row.get("role") or "").strip().lower(),
			}

			ser = AdminCreateContentManagerSerializer(data=mapped)
			try:
				ser.is_valid(raise_exception=True)
			except ValidationError as exc:
				results.append({**row_result, "status": "error", "errors": exc.detail})
				failed_count += 1
				continue

			data = ser.validated_data
			name = data["name"].strip()
			phone = data["phone"].strip()
			email = (data.get("email") or "").strip() or None
			gender = (data.get("gender") or "").strip() or None
			dob = data.get("dob")
			role_label = str(data.get("role") or "").strip().lower()

			if role_label == "creator":
				user_role = UserRole.CONTENTCREATOR.value
			else:
				user_role = UserRole.CONTENTVALIDATOR.value

			import secrets
			import string
			alphabet = string.ascii_letters + string.digits
			temp_password = "".join(secrets.choice(alphabet) for _ in range(10))

			try:
				with transaction.atomic():
					user = User(
						name=name,
						phone=phone,
						email=email,
						role=user_role,
						dob=dob,
						gender=gender,
					)
					user.set_password(temp_password)
					user.save()
			except Exception as exc:
				results.append({**row_result, "status": "error", "errors": {"non_field_errors": [str(exc)]}})
				failed_count += 1
				continue

			message = (
				f"Hi {name}, your Liberia eLearn content manager account has been created.\n"
				f"Login with phone: {phone} and password: {temp_password}.\n"
				"Please change this password after your first login."
			)
			fire_and_forget(
				_send_account_notifications,
				message,
				phone,
				email,
				"Your Liberia eLearn content manager account",
			)

			created_count += 1
			results.append({
				**row_result,
				"status": "created",
				"user_id": user.id,
				"name": name,
				"phone": phone,
				"email": email,
				"role": role_label,
			})

		return Response({
			"summary": {
				"total_rows": len(results),
				"created": created_count,
				"failed": failed_count,
			},
			"results": results,
		})

	@extend_schema(
		description=(
			"Download a sample CSV template for bulk content manager creation via admin endpoints. "
			"The file includes the correct header columns and example rows."
		),
		responses={
			200: OpenApiResponse(
				description="CSV file with header row and two sample content manager records.",
			),
		},
	)
	@action(detail=False, methods=['get'], url_path='bulk-template')
	def bulk_content_managers_template(self, request):
		"""Return a CSV template for bulk content manager creation via admin endpoints."""
		header = [
			"name",
			"phone",
			"email",
			"role",
			"gender",
			"dob",
		]
		example_rows = [
			{
				"name": "Jane Creator",
				"phone": "231770000010",
				"email": "jane.creator@example.com",
				"role": "CONTENTCREATOR",
				"gender": "F",
				"dob": "1990-05-10",
			},
			{
				"name": "John Validator",
				"phone": "231770000011",
				"email": "john.validator@example.com",
				"role": "CONTENTVALIDATOR",
				"gender": "M",
				"dob": "1988-09-02",
			},
		]

		buffer = io.StringIO()
		writer = csv.DictWriter(buffer, fieldnames=header)
		writer.writeheader()
		for row in example_rows:
			writer.writerow(row)

		csv_content = buffer.getvalue()
		response = HttpResponse(csv_content, content_type="text/csv")
		response["Content-Disposition"] = "attachment; filename=content_managers_bulk_template.csv"
		return response

