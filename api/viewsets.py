import csv
import io
import hashlib
from urllib.parse import quote
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
from django.db import models, transaction
from django.db.models import Q, Count, Window, F
from django.db.models.functions import TruncDate, DenseRank

from elearncore.sysutils.constants import (
	UserRole,
	Status as StatusEnum,
	ContentType,
	GAME_PLAY_POINTS,
	ASSESSMENT_SUBMISSION_POINTS,
	VIDEO_WATCH_POINTS,
)
from elearncore.sysutils.tasks import fire_and_forget

from content.models import (
	Subject, Topic, Period, LessonResource, TakeLesson, LessonAssessment,
	GeneralAssessment, GeneralAssessmentGrade, LessonAssessmentGrade,
	Question,
	GameModel, Activity, AssessmentSolution, GamePlay,
	LessonAssessmentSolution, LessonTemporaryUnlock, Story,
)
from forum.models import Chat
from django.core.cache import cache
from content.serializers import (
	AssessmentSolutionSerializer,
	LessonAssessmentSolutionSerializer,
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
	StoryListSerializer,
	StoryDetailSerializer,
	StoryGenerateRequestSerializer,
	StoryPublishRequestSerializer,
)
from agentic.models import AIRecommendation, AIAbuseReport
from agentic.services import ai_runtime_diagnostics
from agentic.serializers import AIRecommendationSerializer, AIAbuseReportSerializer
from agentic.services import generate_targeted_assessments_for_student
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
	AdminSystemReportSerializer,
	AdminBulkCountyUploadSerializer,
	AdminBulkDistrictUploadSerializer,
	AdminBulkSchoolUploadSerializer,
	KidsSubjectsAndLessonsResponseSerializer,
)
from .pagination import StandardResultsSetPagination
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


LESSON_LOCK_REASON = "Complete the previous lesson and submit all of its assessments to unlock this lesson."
TEACHER_UNLOCK_MAX_HOURS = 72
TEACHER_UNLOCK_REASON = "Temporarily unlocked by teacher."
STUDENT_LESSON_CACHE_TTL = 120


def _published_stories_for_school(school_id: int | None):
	qs = Story.objects.filter(is_published=True)
	if school_id:
		return qs.filter(Q(school__isnull=True) | Q(school_id=school_id))
	return qs.filter(school__isnull=True)


def _enqueue_story_generation(*, requested_by_id: int, grade: str, tag: str, count: int, school_id: int | None):
	# Local import keeps app startup resilient if Celery isn't installed yet.
	# If Celery isn't available (common in local/dev), run synchronously so the API still works.
	from agentic import tasks as agentic_tasks
	import uuid

	try:
		generate_stories_task = agentic_tasks.generate_stories_task
		return generate_stories_task.delay(
			requested_by_id=requested_by_id,
			grade=grade,
			tag=tag,
			count=count,
			school_id=school_id,
		)
	except Exception:
		result = agentic_tasks.generate_stories_task_sync(
			requested_by_id=requested_by_id,
			grade=grade,
			tag=tag,
			count=count,
			school_id=school_id,
		)

		class _ImmediateResult:
			def __init__(self, payload):
				self.id = uuid.uuid4()
				self.payload = payload

		return _ImmediateResult(result)


def _award_student_points(student: Student | None, points: int) -> int | None:
	if student is None or points <= 0:
		return None
	Student.objects.filter(pk=student.pk).update(points=F('points') + points)
	student.points = int(getattr(student, 'points', 0) or 0) + points
	return student.points


def _parse_leaderboard_limit(request, default: int = 10, max_limit: int = 100) -> int:
	raw_limit = request.query_params.get('limit') if request is not None else None
	if raw_limit in {None, ''}:
		return default
	try:
		limit = int(raw_limit)
	except (TypeError, ValueError):
		return default
	return max(1, min(limit, max_limit))


def _parse_leaderboard_timeframe(request, default: str = 'all_time') -> str:
	raw_timeframe = (request.query_params.get('timeframe') if request is not None else None) or default
	timeframe = str(raw_timeframe).strip().lower()
	allowed = {'this_week', 'this_month', 'all_time'}
	if timeframe not in allowed:
		raise ValueError("timeframe must be one of: this_week, this_month, all_time.")
	return timeframe


def _points_timeframe_start(timeframe: str):
	now = timezone.now()
	if timeframe == 'this_week':
		return now - timedelta(days=7)
	if timeframe == 'this_month':
		return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
	return None


def _student_points_for_timeframe(students: List[Student], timeframe: str) -> Dict[int, int]:
	if timeframe == 'all_time':
		return {student.id: int(getattr(student, 'points', 0) or 0) for student in students}

	start_at = _points_timeframe_start(timeframe)
	if start_at is None:
		return {student.id: 0 for student in students}

	points_by_student_id = {student.id: 0 for student in students}
	student_id_by_profile_id = {student.profile_id: student.id for student in students if getattr(student, 'profile_id', None)}
	if not student_id_by_profile_id:
		return points_by_student_id

	activities = Activity.objects.filter(user_id__in=list(student_id_by_profile_id.keys()), created_at__gte=start_at)
	for activity in activities:
		metadata = getattr(activity, 'metadata', None)
		if not isinstance(metadata, dict):
			continue
		raw_points = metadata.get('points_awarded', 0)
		try:
			awarded = int(raw_points)
		except (TypeError, ValueError):
			continue
		if awarded <= 0:
			continue
		student_id = student_id_by_profile_id.get(activity.user_id)
		if student_id is None:
			continue
		points_by_student_id[student_id] = points_by_student_id.get(student_id, 0) + awarded

	return points_by_student_id


def _build_student_leaderboard_response(queryset, *, scope: dict, limit: int, timeframe: str = 'all_time') -> dict:
	all_students = list(
		queryset
		.select_related('profile', 'school__district__county')
		.order_by('profile__name', 'id')
	)
	points_by_student_id = _student_points_for_timeframe(all_students, timeframe)

	all_students.sort(
		key=lambda student: (
			-int(points_by_student_id.get(student.id, 0)),
			str(getattr(getattr(student, 'profile', None), 'name', '') or '').lower(),
			student.id,
		)
	)

	ordered_students = all_students[:max(0, limit)]
	total_students = len(all_students)
	leaderboard = []
	last_points = None
	current_rank = 0

	for index, student in enumerate(ordered_students, start=1):
		points = int(points_by_student_id.get(student.id, 0) or 0)
		if last_points != points:
			current_rank = index
			last_points = points

		school = getattr(student, 'school', None)
		district = getattr(school, 'district', None) if school is not None else None
		county = getattr(district, 'county', None) if district is not None else None

		leaderboard.append(
			{
				'rank': current_rank,
				'student_db_id': student.id,
				'student_id': getattr(student, 'student_id', None),
				'student_name': getattr(getattr(student, 'profile', None), 'name', None),
				'grade': getattr(student, 'grade', None),
				'points': points,
				'current_login_streak': int(getattr(student, 'current_login_streak', 0) or 0),
				'school_id': getattr(school, 'id', None),
				'school_name': getattr(school, 'name', None),
				'district_id': getattr(district, 'id', None),
				'district_name': getattr(district, 'name', None),
				'county_id': getattr(county, 'id', None),
				'county_name': getattr(county, 'name', None),
			}
		)

	scope_payload = dict(scope or {})
	scope_payload['timeframe'] = timeframe

	return {
		'scope': scope_payload,
		'total_students': total_students,
		'leaderboard': leaderboard,
	}


def _student_lesson_progress_version_key(student_id: int) -> str:
	return f"student-lesson-progress-version:{student_id}"


def _grade_lesson_content_version_key(grade: str) -> str:
	return f"grade-lesson-content-version:{quote(str(grade), safe='')}"


def _get_cache_version(cache_key: str) -> int:
	return int(cache.get(cache_key, 1) or 1)


def _bump_cache_version(cache_key: str) -> int:
	new_version = _get_cache_version(cache_key) + 1
	cache.set(cache_key, new_version, timeout=None)
	return new_version


def _invalidate_student_lesson_cache(student: Student) -> None:
	_bump_cache_version(_student_lesson_progress_version_key(student.id))


def _invalidate_grade_lesson_cache(grade: str | None) -> None:
	if grade:
		_bump_cache_version(_grade_lesson_content_version_key(grade))


def _student_lesson_cache_key(student: Student, request, suffix: str) -> str:
	progress_version = _get_cache_version(_student_lesson_progress_version_key(student.id))
	grade_version = _get_cache_version(_grade_lesson_content_version_key(student.grade))
	query = quote(request.META.get('QUERY_STRING', ''), safe='') if request is not None else ''
	grade_token = quote(str(student.grade), safe='')
	return (
		f"student-lesson:{suffix}:student:{student.id}:grade:{grade_token}:"
		f"progress:{progress_version}:content:{grade_version}:query:{query}"
	)


def _paginate_payload(request, items, results_key: str, *, extra_payload: dict | None = None):
	paginator = StandardResultsSetPagination()
	page = paginator.paginate_queryset(list(items), request)
	payload = dict(extra_payload or {})
	payload[results_key] = page
	payload['pagination'] = {
		'count': paginator.page.paginator.count,
		'next': paginator.get_next_link(),
		'previous': paginator.get_previous_link(),
		'page_size': paginator.get_page_size(request),
	}
	return payload


def _active_lesson_unlocks_for_student(student: Student, *, lesson_ids: list[int] | None = None) -> dict[int, LessonTemporaryUnlock]:
	"""Return active lesson unlocks keyed by lesson_id for a student."""
	now = timezone.now()
	qs = (
		LessonTemporaryUnlock.objects
		.filter(
			student=student,
			revoked_at__isnull=True,
			expires_at__gt=now,
		)
		.order_by('expires_at')
	)
	if lesson_ids is not None:
		qs = qs.filter(lesson_id__in=lesson_ids)

	unlock_map: dict[int, LessonTemporaryUnlock] = {}
	for unlock in qs:
		unlock_map[unlock.lesson_id] = unlock
	return unlock_map


def _build_student_lesson_progression(student: Student) -> dict:
	"""Build ordered lesson progression state for a student in one batched pass."""
	lessons = list(
		LessonResource.objects
		.filter(
			subject__grade=student.grade,
			subject__status=StatusEnum.APPROVED.value,
			status=StatusEnum.APPROVED.value,
		)
		.select_related('subject', 'topic', 'period')
		.order_by(
			'subject__name',
			'period__start_month',
			'period__end_month',
			'topic__name',
			'title',
			'id',
		)
	)

	lesson_ids = [lesson.id for lesson in lessons]
	if not lesson_ids:
		return {
			"lessons": [],
			"states": {},
		}

	taken_lesson_ids = set(
		TakeLesson.objects
		.filter(student=student, lesson_id__in=lesson_ids)
		.values_list('lesson_id', flat=True)
	)

	visible_lesson_assessments = LessonAssessment.objects.filter(
		lesson_id__in=lesson_ids,
		status=StatusEnum.APPROVED.value,
	).filter(
		models.Q(is_targeted=False) | models.Q(target_student=student)
	)

	assessment_totals = {
		row['lesson_id']: row['total']
		for row in visible_lesson_assessments
		.values('lesson_id')
		.annotate(total=Count('id'))
	}
	completed_assessment_totals = {
		row['lesson_assessment__lesson_id']: row['total']
		for row in (
			LessonAssessmentSolution.objects
			.filter(student=student, lesson_assessment__in=visible_lesson_assessments)
			.values('lesson_assessment__lesson_id')
			.annotate(total=Count('lesson_assessment_id', distinct=True))
		)
	}
	active_unlocks = _active_lesson_unlocks_for_student(student, lesson_ids=lesson_ids)

	states = {}
	previous_lesson_completed = True
	for index, lesson in enumerate(lessons):
		assessments_total = int(assessment_totals.get(lesson.id, 0))
		assessments_completed = int(completed_assessment_totals.get(lesson.id, 0))
		is_taken = lesson.id in taken_lesson_ids
		is_completed = is_taken and assessments_completed >= assessments_total
		temporary_unlock = active_unlocks.get(lesson.id)
		is_temporarily_unlocked = temporary_unlock is not None and not previous_lesson_completed
		is_locked = (not previous_lesson_completed) and not is_temporarily_unlocked

		if is_locked:
			progression_status = "locked"
		elif is_completed:
			progression_status = "completed"
		elif is_taken:
			progression_status = "in_progress"
		else:
			progression_status = "available"

		next_lesson = lessons[index + 1] if index + 1 < len(lessons) else None
		states[lesson.id] = {
			"assessments_total": assessments_total,
			"assessments_completed": assessments_completed,
			"is_taken": is_taken,
			"is_completed": is_completed,
			"is_locked": is_locked,
			"is_temporarily_unlocked": is_temporarily_unlocked,
			"temporary_unlock_expires_at": temporary_unlock.expires_at if temporary_unlock else None,
			"progression_status": progression_status,
			"next_video_id": next_lesson.id if next_lesson else None,
			"lock_reason": LESSON_LOCK_REASON if is_locked and index > 0 else None,
			"sequence_position": index + 1,
		}

		previous_lesson_completed = is_completed

	return {
		"lessons": lessons,
		"states": states,
	}


class CanCreateContent(permissions.BasePermission):
	"""Allow writes if the user has a content-creation capable role."""
	allowed_roles = {
		UserRole.CONTENTCREATOR.value,
		UserRole.CONTENTVALIDATOR.value,
		UserRole.TEACHER.value,
		UserRole.HEADTEACHER.value,
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
		UserRole.HEADTEACHER.value,
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
	ordering = ['id']
	ordering_fields = ['created_at', 'updated_at', 'title']
	_student_progression_cache = None

	def get_permissions(self):
		if self.action in ['approve', 'reject', 'request_changes']:
			return [permissions.IsAuthenticated(), CanModerateContent()]
		elif self.request.method in permissions.SAFE_METHODS:
			return [permissions.IsAuthenticatedOrReadOnly()]
		else:
			return [permissions.IsAuthenticated(), CanCreateContent()]

	def _get_student_progression(self, student: Student) -> dict:
		if self._student_progression_cache is None:
			self._student_progression_cache = _build_student_lesson_progression(student)
		return self._student_progression_cache

	def get_queryset(self):
		qs = super().get_queryset()
		student = getattr(getattr(self, 'request', None), 'user', None)
		student = getattr(student, 'student', None)
		if not student:
			return qs

		progression = self._get_student_progression(student)
		allowed_lesson_ids = [lesson_id for lesson_id, state in progression['states'].items() if not state['is_locked']]
		if not allowed_lesson_ids:
			return qs.none()
		return qs.filter(id__in=allowed_lesson_ids)

	def retrieve(self, request, *args, **kwargs):
		student = getattr(request.user, 'student', None)
		if student:
			lesson = (
				LessonResource.objects
				.select_related('subject', 'topic', 'period', 'created_by')
				.filter(pk=kwargs.get('pk'))
				.first()
			)
			if lesson is None:
				return Response({"detail": "Not found."}, status=404)

			progression = self._get_student_progression(student)
			state = progression['states'].get(lesson.id)
			if state is None:
				return Response({"detail": "Not found."}, status=404)
			if state['is_locked']:
				return Response({"detail": LESSON_LOCK_REASON}, status=403)

			serializer = self.get_serializer(lesson)
			return Response(serializer.data)

		return super().retrieve(request, *args, **kwargs)

	def perform_create(self, serializer):
		lesson = serializer.save(created_by=self.request.user, status=StatusEnum.DRAFT.value)
		_invalidate_grade_lesson_cache(getattr(getattr(lesson, 'subject', None), 'grade', None))
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
		_invalidate_grade_lesson_cache(getattr(getattr(obj, 'subject', None), 'grade', None))
		return Response({'status': obj.status})

	@action(detail=True, methods=['post'])
	def reject(self, request, pk=None):
		obj = self.get_object()
		obj.status = StatusEnum.REJECTED.value
		obj.save(update_fields=['status', 'updated_at'])
		_invalidate_grade_lesson_cache(getattr(getattr(obj, 'subject', None), 'grade', None))
		return Response({'status': obj.status})

	@action(detail=True, methods=['post'], url_path='request-changes')
	def request_changes(self, request, pk=None):
		obj = self.get_object()
		obj.status = StatusEnum.REVIEW_REQUESTED.value
		obj.save(update_fields=['status', 'updated_at'])
		_invalidate_grade_lesson_cache(getattr(getattr(obj, 'subject', None), 'grade', None))
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
			UserRole.HEADTEACHER.value,
			UserRole.CONTENTVALIDATOR.value,
		})
		if elevated:
			return qs
		student = getattr(user, 'student', None)
		if student:
			return qs.filter(student=student)
		return TakeLesson.objects.none()

	def create(self, request, *args, **kwargs):
		student = getattr(request.user, 'student', None)
		if student is None:
			return super().create(request, *args, **kwargs)

		payload = request.data.copy()
		payload['student'] = student.id
		serializer = self.get_serializer(data=payload)
		serializer.is_valid(raise_exception=True)

		lesson = serializer.validated_data['lesson']
		progression = _build_student_lesson_progression(student)
		state = progression['states'].get(lesson.id)
		if state is None:
			return Response({"detail": "Lesson not available for this student."}, status=404)
		if state['is_locked']:
			return Response({"detail": LESSON_LOCK_REASON}, status=403)

		self.perform_create(serializer)
		headers = self.get_success_headers(serializer.data)
		return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

	def perform_create(self, serializer):
		"""Create a TakeLesson and log an activity for the student."""
		request_student = getattr(self.request.user, 'student', None)
		instance: TakeLesson = serializer.save(student=request_student) if request_student else serializer.save()
		awarded_points = 0
		if request_student is not None and getattr(instance.lesson, 'type', None) == ContentType.VIDEO.value:
			_award_student_points(request_student, VIDEO_WATCH_POINTS)
			awarded_points = VIDEO_WATCH_POINTS
		if request_student is not None:
			_invalidate_student_lesson_cache(request_student)
		user = getattr(getattr(instance, 'student', None), 'profile', None)
		if user is not None:
			Activity.objects.create(
				user=user,
				type="take_lesson",
				description=f"Took lesson '{instance.lesson.title}'",
				metadata={
					"lesson_id": instance.lesson_id,
					"subject_id": getattr(instance.lesson.subject, 'id', None),
					"points_awarded": awarded_points,
				},
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
		if user and getattr(user, 'role', None) in {UserRole.ADMIN.value, UserRole.CONTENTVALIDATOR.value, UserRole.TEACHER.value, UserRole.HEADTEACHER.value}:
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
	ai_recommended = serializers.BooleanField(required=False)
	is_targeted = serializers.BooleanField(required=False)
	target_student_id = serializers.IntegerField(allow_null=True, required=False)


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


class TeacherLessonUnlockRequestSerializer(serializers.Serializer):
	student_id = serializers.IntegerField(required=False, allow_null=True)
	unlock_whole_class = serializers.BooleanField(required=False, default=False)
	lesson_id = serializers.IntegerField()
	duration_hours = serializers.IntegerField(min_value=1, max_value=TEACHER_UNLOCK_MAX_HOURS)
	reason = serializers.CharField(required=False, allow_blank=True, max_length=255)

	def validate(self, attrs):
		unlock_whole_class = bool(attrs.get('unlock_whole_class'))
		has_student_id = attrs.get('student_id') is not None
		# XOR: exactly one of (student_id) or (unlock_whole_class=True) must be provided.
		if unlock_whole_class == has_student_id:
			raise serializers.ValidationError(
				"Provide exactly one of student_id or unlock_whole_class=true."
			)
		return attrs


class TeacherLessonUnlockRevokeSerializer(serializers.Serializer):
	student_id = serializers.IntegerField(required=False, allow_null=True)
	unlock_whole_class = serializers.BooleanField(required=False, default=False)
	lesson_id = serializers.IntegerField()

	def validate(self, attrs):
		unlock_whole_class = bool(attrs.get('unlock_whole_class'))
		has_student_id = attrs.get('student_id') is not None
		# XOR: exactly one of (student_id) or (unlock_whole_class=True) must be provided.
		if unlock_whole_class == has_student_id:
			raise serializers.ValidationError(
				"Provide exactly one of student_id or unlock_whole_class=true."
			)
		return attrs


class TeacherLessonUnlockResponseSerializer(serializers.Serializer):
	id = serializers.IntegerField()
	student_id = serializers.IntegerField()
	lesson_id = serializers.IntegerField()
	unlocked_by_id = serializers.IntegerField(allow_null=True)
	reason = serializers.CharField(allow_blank=True)
	expires_at = serializers.DateTimeField()
	revoked_at = serializers.DateTimeField(allow_null=True)


class TeacherActiveLessonUnlockSerializer(serializers.Serializer):
	id = serializers.IntegerField()
	student_id = serializers.IntegerField()
	student_name = serializers.CharField(allow_null=True)
	lesson_id = serializers.IntegerField()
	lesson_title = serializers.CharField()
	subject_id = serializers.IntegerField()
	subject_name = serializers.CharField(allow_null=True)
	reason = serializers.CharField(allow_blank=True)
	expires_at = serializers.DateTimeField()
	unlocked_by_id = serializers.IntegerField(allow_null=True)


class TeacherDashboardResponseSerializer(serializers.Serializer):
	summarycards = TeacherDashboardSummaryCardsSerializer()
	top_performers = TeacherDashboardTopPerformerSerializer(many=True)
	pending_submissions = TeacherDashboardPendingSubmissionSerializer(many=True)
	upcoming_deadlines = TeacherDashboardUpcomingDeadlineSerializer(many=True)


class LeaderboardEntrySerializer(serializers.Serializer):
	rank = serializers.IntegerField()
	student_db_id = serializers.IntegerField()
	student_id = serializers.CharField(allow_null=True)
	student_name = serializers.CharField(allow_null=True)
	grade = serializers.CharField(allow_null=True)
	points = serializers.IntegerField()
	current_login_streak = serializers.IntegerField()
	school_id = serializers.IntegerField(allow_null=True)
	school_name = serializers.CharField(allow_null=True)
	district_id = serializers.IntegerField(allow_null=True)
	district_name = serializers.CharField(allow_null=True)
	county_id = serializers.IntegerField(allow_null=True)
	county_name = serializers.CharField(allow_null=True)


class LeaderboardScopeSerializer(serializers.Serializer):
	kind = serializers.CharField()
	timeframe = serializers.CharField()
	school_id = serializers.IntegerField(required=False, allow_null=True)
	school_name = serializers.CharField(required=False, allow_null=True)
	grades = serializers.ListField(
		child=serializers.CharField(),
		required=False,
	)
	grade = serializers.CharField(required=False, allow_null=True)
	county_id = serializers.IntegerField(required=False, allow_null=True)
	district_id = serializers.IntegerField(required=False, allow_null=True)


class LeaderboardResponseSerializer(serializers.Serializer):
	scope = LeaderboardScopeSerializer()
	total_students = serializers.IntegerField()
	leaderboard = LeaderboardEntrySerializer(many=True)


class ParentLeaderboardChildSerializer(serializers.Serializer):
	student_db_id = serializers.IntegerField()
	student_id = serializers.CharField(allow_null=True)
	student_name = serializers.CharField(allow_null=True)
	grade = serializers.CharField(allow_null=True)


class ParentChildLeaderboardContextSerializer(serializers.Serializer):
	child = ParentLeaderboardChildSerializer()
	scope = LeaderboardScopeSerializer()
	rank = serializers.IntegerField(allow_null=True)
	total_students = serializers.IntegerField()
	points = serializers.IntegerField()
	current_login_streak = serializers.IntegerField()
	leaderboard_context = LeaderboardEntrySerializer(many=True)


class ParentLeaderboardResponseSerializer(serializers.Serializer):
	timeframe = serializers.CharField()
	children = ParentChildLeaderboardContextSerializer(many=True)


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

	def _child_ranking_context(self, child: Student, *, timeframe: str, window: int = 2) -> dict:
		child_profile = getattr(child, 'profile', None)
		child_payload = {
			'student_db_id': child.id,
			'student_id': getattr(child, 'student_id', None),
			'student_name': getattr(child_profile, 'name', None),
			'grade': getattr(child, 'grade', None),
		}

		if not getattr(child, 'school_id', None) or not getattr(child, 'grade', None):
			return {
				'child': child_payload,
				'scope': {'kind': 'school_grade', 'school_id': getattr(child, 'school_id', None), 'grade': getattr(child, 'grade', None), 'timeframe': timeframe},
				'rank': None,
				'total_students': 0,
				'points': 0,
				'current_login_streak': int(getattr(child, 'current_login_streak', 0) or 0),
				'leaderboard_context': [],
			}

		school = getattr(child, 'school', None)
		scope = {
			'kind': 'school_grade',
			'school_id': child.school_id,
			'school_name': getattr(school, 'name', None),
			'grade': child.grade,
		}
		qs = Student.objects.filter(
			school_id=child.school_id,
			grade=child.grade,
			status=StatusEnum.APPROVED.value,
		)
		total_students = qs.count()
		payload = _build_student_leaderboard_response(
			qs,
			scope=scope,
			limit=total_students,
			timeframe=timeframe,
		)
		entries = payload.get('leaderboard', [])

		child_index = None
		for idx, item in enumerate(entries):
			if item.get('student_db_id') == child.id:
				child_index = idx
				break

		if child_index is None:
			return {
				'child': child_payload,
				'scope': payload.get('scope', scope),
				'rank': None,
				'total_students': total_students,
				'points': 0,
				'current_login_streak': int(getattr(child, 'current_login_streak', 0) or 0),
				'leaderboard_context': entries[: (window * 2 + 1)],
			}

		start = max(0, child_index - window)
		end = child_index + window + 1
		child_entry = entries[child_index]
		return {
			'child': child_payload,
			'scope': payload.get('scope', scope),
			'rank': child_entry.get('rank'),
			'total_students': total_students,
			'points': child_entry.get('points', 0),
			'current_login_streak': child_entry.get('current_login_streak', 0),
			'leaderboard_context': entries[start:end],
		}

	@extend_schema(
		operation_id="parent_leaderboard",
		description="Leaderboard context for each of the parent's children within the child's school and grade cohort.",
		parameters=[
			OpenApiParameter(name='timeframe', required=False, location=OpenApiParameter.QUERY, description='Leaderboard window: this_week, this_month, or all_time (default).', type=str),
		],
		responses={200: ParentLeaderboardResponseSerializer},
	)
	@action(detail=False, methods=['get'], url_path='leaderboard')
	def leaderboard(self, request):
		user: User = request.user
		parent = getattr(user, 'parent', None)
		if not parent:
			return Response({"detail": "Parent profile required."}, status=status.HTTP_403_FORBIDDEN)

		try:
			timeframe = _parse_leaderboard_timeframe(request)
		except ValueError as exc:
			return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

		children = list(parent.wards.select_related('profile', 'school__district__county').all())
		contexts = [self._child_ranking_context(child, timeframe=timeframe) for child in children]
		return Response({'timeframe': timeframe, 'children': contexts})

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
				# Skip targeted lesson assessments that are meant for a different student
				if getattr(la, 'is_targeted', False) and getattr(la, 'target_student_id', None) != student.id:
					continue
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
					"ai_recommended": bool(getattr(la, 'ai_recommended', False)),
					"is_targeted": bool(getattr(la, 'is_targeted', False)),
					"target_student_id": getattr(la.target_student, 'id', None),
				})

		# General assessments: not subject-specific in the model; we keep subject as None
		general_assessments = GeneralAssessment.objects.all()
		gag_qs = GeneralAssessmentGrade.objects.filter(student_id__in=student_ids)
		gag_map: Dict[tuple, GeneralAssessmentGrade] = {}
		for g in gag_qs.select_related('assessment'):
			gag_map[(g.assessment_id, g.student_id)] = g

		for ga in general_assessments:
			for student in students:
				# Skip targeted assessments that are meant for a different student
				if getattr(ga, 'is_targeted', False) and getattr(ga, 'target_student_id', None) != student.id:
					continue
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
					"ai_recommended": bool(getattr(ga, 'ai_recommended', False)),
					"is_targeted": bool(getattr(ga, 'is_targeted', False)),
					"target_student_id": getattr(ga.target_student, 'id', None),
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
	- can list and update AI-generated assessments (ai_recommended=True) even without a teacher profile.

	Content Validators:
	- can view everything and perform approve/reject/request-review on content objects
	  that support a `status` field.
	- can list AI-generated assessments alongside all other content.
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
		operation_id="content_stories",
		responses={200: StoryListSerializer(many=True)},
		description="List stories for content operations. Supports grade, tag, school_id, and is_published filters.",
		parameters=[
			OpenApiParameter(name='grade', required=False, location=OpenApiParameter.QUERY, type=str),
			OpenApiParameter(name='tag', required=False, location=OpenApiParameter.QUERY, type=str),
			OpenApiParameter(name='school_id', required=False, location=OpenApiParameter.QUERY, type=int),
			OpenApiParameter(name='is_published', required=False, location=OpenApiParameter.QUERY, type=bool),
		],
	)
	@action(detail=False, methods=['get'], url_path='stories')
	def stories(self, request):
		deny = self._require_creator(request)
		if deny and not IsContentValidator().has_permission(request, self):
			return deny

		qs = Story.objects.select_related('school', 'created_by').all().order_by('-created_at')
		user = request.user
		is_creator_only = (
			user and user.is_authenticated
			and getattr(user, 'role', None) == UserRole.CONTENTCREATOR.value
		)
		if is_creator_only:
			qs = qs.filter(created_by=user)

		grade = (request.query_params.get('grade') or '').strip()
		if grade:
			qs = qs.filter(grade=grade)

		tag = (request.query_params.get('tag') or '').strip()
		if tag:
			qs = qs.filter(tag__iexact=tag)

		school_id = request.query_params.get('school_id')
		if school_id:
			try:
				qs = qs.filter(school_id=int(school_id))
			except (TypeError, ValueError):
				return Response({"detail": "school_id must be an integer."}, status=400)

		is_published = request.query_params.get('is_published')
		if is_published in {'1', 'true', 'True'}:
			qs = qs.filter(is_published=True)
		elif is_published in {'0', 'false', 'False'}:
			qs = qs.filter(is_published=False)

		return Response(StoryListSerializer(qs, many=True).data)

	@extend_schema(
		operation_id="content_generate_stories",
		description="Queue AI story generation for admins and content creators (global stories).",
		request=StoryGenerateRequestSerializer,
		responses={202: OpenApiResponse(description="Story generation task queued.")},
	)
	@action(detail=False, methods=['post'], url_path='stories/generate')
	def generate_stories(self, request):
		if not _user_role_in(request.user, {UserRole.ADMIN.value, UserRole.CONTENTCREATOR.value}):
			return Response({"detail": "Admin or content creator role required."}, status=403)

		ser = StoryGenerateRequestSerializer(data=request.data)
		ser.is_valid(raise_exception=True)
		data = ser.validated_data

		task = _enqueue_story_generation(
			requested_by_id=request.user.id,
			grade=data['grade'],
			tag=data['tag'],
			count=data['count'],
			school_id=None,
		)
		return Response(
			{
				"detail": "Story generation queued.",
				"task_id": str(task.id),
				"requested": data,
				"scope": "global",
			},
			status=202,
		)

	@extend_schema(
		operation_id="content_publish_stories",
		description="Publish one or more stories. Restricted to content validators and admins.",
		request=StoryPublishRequestSerializer,
		responses={200: OpenApiResponse(description="Stories published.")},
	)
	@action(detail=False, methods=['post'], url_path='stories/publish')
	def publish_stories(self, request):
		deny = self._require_validator(request)
		if deny:
			return deny

		ser = StoryPublishRequestSerializer(data=request.data)
		ser.is_valid(raise_exception=True)
		story_ids = ser.validated_data['story_ids']

		stories = list(Story.objects.filter(id__in=story_ids))
		if len(stories) != len(set(story_ids)):
			return Response({"detail": "One or more stories were not found."}, status=404)

		updated = 0
		for story in stories:
			if not story.is_published:
				story.is_published = True
				story.save(update_fields=['is_published', 'updated_at'])
				updated += 1

		return Response({
			"detail": "Stories published.",
			"published_count": updated,
			"story_ids": story_ids,
		})

	
	@extend_schema(
		operation_id="content_subjects",
		request=SubjectWriteSerializer,
		responses={200: SubjectSerializer(many=True)},
		description="List or create subjects for content management.",
	)
        
	@extend_schema(
		operation_id="content_ai_diagnostics",
		description=(
			"Admin diagnostic for AI runtime configuration (OpenAI/Celery). "
			"Does not return secrets; use to confirm why OpenAI usage is not occurring."
		),
		responses={200: OpenApiResponse(description="AI runtime diagnostics.")},
	)
	@action(detail=False, methods=['get'], url_path='ai/diagnostics')
	def ai_diagnostics(self, request):
		if not _user_role_in(request.user, {UserRole.ADMIN.value}):
			return Response({"detail": "Admin role required."}, status=403)
		return Response(ai_runtime_diagnostics(), status=200)

	@extend_schema(
		operation_id="content_celery_pending_tasks",
		description=(
			"Admin-only Celery broker inspection. Returns active/reserved/scheduled tasks as reported "
			"by running workers. This does not require storing tasks in the DB."
		),
		responses={200: OpenApiResponse(description="Celery inspect state.")},
	)
	@action(detail=False, methods=['get'], url_path='celery/pending')
	def celery_pending(self, request):
		if not _user_role_in(request.user, {UserRole.ADMIN.value}):
			return Response({"detail": "Admin role required."}, status=403)

		try:
			from celery import current_app  # type: ignore
		except Exception as e:
			return Response({"detail": "Celery not available.", "error": str(e)}, status=503)

		try:
			insp = current_app.control.inspect(timeout=1)
			active = insp.active() or {}
			reserved = insp.reserved() or {}
			scheduled = insp.scheduled() or {}
		except Exception as e:
			return Response({"detail": "Failed to inspect Celery workers.", "error": str(e)}, status=502)

		# Normalize into counts + payload for quick visibility
		def _count(d):
			return sum(len(v or []) for v in (d or {}).values())

		return Response(
			{
				"counts": {
					"active": _count(active),
					"reserved": _count(reserved),
					"scheduled": _count(scheduled),
				},
				"active": active,
				"reserved": reserved,
				"scheduled": scheduled,
			},
			status=200,
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
		parameters=[
			OpenApiParameter(
				name="ai_only",
				required=False,
				location=OpenApiParameter.QUERY,
				description="If set to 1/true, only AI-recommended assessments are returned.",
				type=bool,
			),
			OpenApiParameter(
				name="targeted_only",
				required=False,
				location=OpenApiParameter.QUERY,
				description="If set to 1/true, only targeted assessments are returned.",
				type=bool,
			),
			OpenApiParameter(
				name="student_id",
				required=False,
				location=OpenApiParameter.QUERY,
				description="Filter targeted assessments by target student id.",
				type=int,
			),
		],
	)
	@action(detail=False, methods=['get', 'post'], url_path='general-assessments')
	def general_assessments(self, request):
		"""List or create general assessments."""
		if request.method == 'GET':
			qs = GeneralAssessment.objects.select_related('given_by').all().order_by('-created_at')
			ai_only = request.query_params.get('ai_only')
			if ai_only in {'1', 'true', 'True'}:
				qs = qs.filter(ai_recommended=True)
			targeted_only = request.query_params.get('targeted_only')
			if targeted_only in {'1', 'true', 'True'}:
				qs = qs.filter(is_targeted=True)
			student_id = request.query_params.get('student_id')
			if student_id:
				try:
					qs = qs.filter(target_student_id=int(student_id))
				except ValueError:
					qs = qs.none()
			user = request.user
			if user and user.is_authenticated and IsContentCreator().has_permission(request, self) and not IsContentValidator().has_permission(request, self):
					# For creator role, restrict to their own assessments, but always expose
					# AI-generated assessments regardless of teacher-ownership.
					teacher = getattr(user, 'teacher', None)
					if teacher is not None:
						qs = qs.filter(Q(given_by=teacher) | Q(ai_recommended=True))
					else:
						qs = qs.filter(ai_recommended=True)
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
		parameters=[
			OpenApiParameter(
				name="ai_only",
				required=False,
				location=OpenApiParameter.QUERY,
				description="If set to 1/true, only AI-recommended assessments are returned.",
				type=bool,
			),
			OpenApiParameter(
				name="targeted_only",
				required=False,
				location=OpenApiParameter.QUERY,
				description="If set to 1/true, only targeted assessments are returned.",
				type=bool,
			),
			OpenApiParameter(
				name="student_id",
				required=False,
				location=OpenApiParameter.QUERY,
				description="Filter targeted assessments by target student id.",
				type=int,
			),
		],
	)
	@action(detail=False, methods=['get', 'post'], url_path='lesson-assessments')
	def lesson_assessments(self, request):
		"""List or create lesson assessments."""
		if request.method == 'GET':
			qs = LessonAssessment.objects.select_related('lesson').all().order_by('-created_at')
			ai_only = request.query_params.get('ai_only')
			if ai_only in {'1', 'true', 'True'}:
				qs = qs.filter(ai_recommended=True)
			targeted_only = request.query_params.get('targeted_only')
			if targeted_only in {'1', 'true', 'True'}:
				qs = qs.filter(is_targeted=True)
			student_id = request.query_params.get('student_id')
			if student_id:
				try:
					qs = qs.filter(target_student_id=int(student_id))
				except ValueError:
					qs = qs.none()
			user = request.user
			if user and user.is_authenticated and IsContentCreator().has_permission(request, self) and not IsContentValidator().has_permission(request, self):
					# Same as GeneralAssessment: expose AI-generated items to all creators.
					teacher = getattr(user, 'teacher', None)
					if teacher is not None:
						qs = qs.filter(Q(given_by=teacher) | Q(ai_recommended=True))
					else:
						qs = qs.filter(ai_recommended=True)
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
				"ai_recommended": bool(getattr(ga, 'ai_recommended', False)),
				"is_targeted": bool(getattr(ga, 'is_targeted', False)),
				"target_student_id": getattr(ga.target_student, 'id', None),
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
				"ai_recommended": bool(getattr(la, 'ai_recommended', False)),
				"is_targeted": bool(getattr(la, 'is_targeted', False)),
				"target_student_id": getattr(la.target_student, 'id', None),
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
		Teachers and head teachers only see colleagues in their own school.
		"""
		qs = Teacher.objects.select_related('profile', 'school').all().order_by('profile__name')
		user = request.user
		if user and user.is_authenticated and user.role in (UserRole.TEACHER.value, UserRole.HEADTEACHER.value):
			teacher = getattr(user, 'teacher', None)
			if teacher and teacher.school_id:
				qs = qs.filter(school_id=teacher.school_id)
			else:
				qs = qs.none()
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
		temp_password = "password123"

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
			temp_password = "password123"

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
		temp_password = "password123"

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
			temp_password = "password123"

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

	# ------------------------------------------------------------------ #
	# Update (PATCH) endpoints                                            #
	# ------------------------------------------------------------------ #

	@extend_schema(
		operation_id="content_update_subject",
		request=SubjectWriteSerializer,
		responses={200: SubjectSerializer},
		description=(
			"Partially update a subject. "
			"Creators may only update subjects they created; validators/admins can update any."
		),
	)
	@action(detail=False, methods=['patch'], url_path='subjects/(?P<pk>[^/.]+)')
	def update_subject(self, request, pk=None):
		deny = self._require_creator(request)
		if deny:
			return deny
		try:
			obj = Subject.objects.get(pk=pk)
		except Subject.DoesNotExist:
			return Response({"detail": "Subject not found."}, status=status.HTTP_404_NOT_FOUND)
		if not IsContentValidator().has_permission(request, self):
			if obj.created_by_id != request.user.pk:
				return Response({"detail": "You can only update subjects you created."}, status=status.HTTP_403_FORBIDDEN)
		ser = SubjectWriteSerializer(obj, data=request.data, partial=True)
		ser.is_valid(raise_exception=True)
		updated = ser.save()
		return Response(SubjectSerializer(updated).data)

	@extend_schema(
		operation_id="content_update_lesson",
		request=LessonResourceSerializer,
		responses={200: LessonResourceSerializer},
		description=(
			"Partially update a lesson. "
			"Creators may only update lessons they created; validators/admins can update any."
		),
	)
	@action(detail=False, methods=['patch'], url_path='lessons/(?P<pk>[^/.]+)')
	def update_lesson(self, request, pk=None):
		deny = self._require_creator(request)
		if deny:
			return deny
		try:
			obj = LessonResource.objects.get(pk=pk)
		except LessonResource.DoesNotExist:
			return Response({"detail": "Lesson not found."}, status=status.HTTP_404_NOT_FOUND)
		if not IsContentValidator().has_permission(request, self):
			if obj.created_by_id != request.user.pk:
				return Response({"detail": "You can only update lessons you created."}, status=status.HTTP_403_FORBIDDEN)
		ser = LessonResourceSerializer(obj, data=request.data, partial=True)
		ser.is_valid(raise_exception=True)
		updated = ser.save()
		return Response(LessonResourceSerializer(updated).data)

	@extend_schema(
		operation_id="content_update_general_assessment",
		request=GeneralAssessmentSerializer,
		responses={200: GeneralAssessmentSerializer},
		description=(
			"Partially update a general assessment. "
			"Creators may update assessments linked to their teacher profile or any AI-generated assessment; "
			"validators/admins can update any."
		),
	)
	@action(detail=False, methods=['patch'], url_path='general-assessments/(?P<pk>[^/.]+)')
	def update_general_assessment(self, request, pk=None):
		deny = self._require_creator(request)
		if deny:
			return deny
		try:
			obj = GeneralAssessment.objects.get(pk=pk)
		except GeneralAssessment.DoesNotExist:
			return Response({"detail": "General assessment not found."}, status=status.HTTP_404_NOT_FOUND)
		if not IsContentValidator().has_permission(request, self):
			teacher = getattr(request.user, 'teacher', None)
			if not obj.ai_recommended:
				if teacher is None or obj.given_by_id != teacher.id:
					return Response(
						{"detail": "You can only update your own assessments or AI-generated ones."},
						status=status.HTTP_403_FORBIDDEN,
					)
		ser = GeneralAssessmentSerializer(obj, data=request.data, partial=True)
		ser.is_valid(raise_exception=True)
		updated = ser.save()
		return Response(GeneralAssessmentSerializer(updated).data)

	@extend_schema(
		operation_id="content_update_lesson_assessment",
		request=LessonAssessmentSerializer,
		responses={200: LessonAssessmentSerializer},
		description=(
			"Partially update a lesson assessment. "
			"Creators may update assessments linked to their teacher profile or any AI-generated assessment; "
			"validators/admins can update any."
		),
	)
	@action(detail=False, methods=['patch'], url_path='lesson-assessments/(?P<pk>[^/.]+)')
	def update_lesson_assessment(self, request, pk=None):
		deny = self._require_creator(request)
		if deny:
			return deny
		try:
			obj = LessonAssessment.objects.get(pk=pk)
		except LessonAssessment.DoesNotExist:
			return Response({"detail": "Lesson assessment not found."}, status=status.HTTP_404_NOT_FOUND)
		if not IsContentValidator().has_permission(request, self):
			teacher = getattr(request.user, 'teacher', None)
			if not obj.ai_recommended:
				if teacher is None or obj.given_by_id != teacher.id:
					return Response(
						{"detail": "You can only update your own assessments or AI-generated ones."},
						status=status.HTTP_403_FORBIDDEN,
					)
		ser = LessonAssessmentSerializer(obj, data=request.data, partial=True)
		ser.is_valid(raise_exception=True)
		updated = ser.save()
		return Response(LessonAssessmentSerializer(updated).data)

	@extend_schema(
		operation_id="content_update_game",
		request=GameSerializer,
		responses={200: GameSerializer},
		description=(
			"Partially update a game. "
			"Creators may only update games they created; validators/admins can update any."
		),
	)
	@action(detail=False, methods=['patch'], url_path='games/(?P<pk>[^/.]+)')
	def update_game(self, request, pk=None):
		deny = self._require_creator(request)
		if deny:
			return deny
		try:
			obj = GameModel.objects.get(pk=pk)
		except GameModel.DoesNotExist:
			return Response({"detail": "Game not found."}, status=status.HTTP_404_NOT_FOUND)
		if not IsContentValidator().has_permission(request, self):
			if obj.created_by_id != request.user.pk:
				return Response({"detail": "You can only update games you created."}, status=status.HTTP_403_FORBIDDEN)
		ser = GameSerializer(obj, data=request.data, partial=True)
		ser.is_valid(raise_exception=True)
		updated = ser.save()
		return Response(GameSerializer(updated).data)

	@extend_schema(
		operation_id="content_update_school",
		request=SchoolSerializer,
		responses={200: SchoolSerializer},
		description="Partially update a school. Requires content creator or validator role.",
	)
	@action(detail=False, methods=['patch'], url_path='schools/(?P<pk>[^/.]+)')
	def update_school(self, request, pk=None):
		deny = self._require_creator(request)
		if deny:
			return deny
		try:
			obj = School.objects.get(pk=pk)
		except School.DoesNotExist:
			return Response({"detail": "School not found."}, status=status.HTTP_404_NOT_FOUND)
		ser = SchoolSerializer(obj, data=request.data, partial=True)
		ser.is_valid(raise_exception=True)
		updated = ser.save()
		return Response(SchoolSerializer(updated).data)

	@extend_schema(
		operation_id="content_update_county",
		request=CountySerializer,
		responses={200: CountySerializer},
		description=(
			"Partially update a county. "
			"Creators may only update counties they created; validators/admins can update any."
		),
	)
	@action(detail=False, methods=['patch'], url_path='counties/(?P<pk>[^/.]+)')
	def update_county(self, request, pk=None):
		deny = self._require_creator(request)
		if deny:
			return deny
		try:
			obj = County.objects.get(pk=pk)
		except County.DoesNotExist:
			return Response({"detail": "County not found."}, status=status.HTTP_404_NOT_FOUND)
		if not IsContentValidator().has_permission(request, self):
			if obj.created_by_id != request.user.pk:
				return Response({"detail": "You can only update counties you created."}, status=status.HTTP_403_FORBIDDEN)
		ser = CountySerializer(obj, data=request.data, partial=True)
		ser.is_valid(raise_exception=True)
		updated = ser.save()
		return Response(CountySerializer(updated).data)

	@extend_schema(
		operation_id="content_update_district",
		request=DistrictSerializer,
		responses={200: DistrictSerializer},
		description="Partially update a district. Requires content creator or validator role.",
	)
	@action(detail=False, methods=['patch'], url_path='districts/(?P<pk>[^/.]+)')
	def update_district(self, request, pk=None):
		deny = self._require_creator(request)
		if deny:
			return deny
		try:
			obj = District.objects.get(pk=pk)
		except District.DoesNotExist:
			return Response({"detail": "District not found."}, status=status.HTTP_404_NOT_FOUND)
		ser = DistrictSerializer(obj, data=request.data, partial=True)
		ser.is_valid(raise_exception=True)
		updated = ser.save()
		return Response(DistrictSerializer(updated).data)

	@extend_schema(
		operation_id="content_update_question",
		request=QuestionCreateSerializer,
		responses={200: QuestionSerializer},
		description=(
			"Partially update a question's type, text, or answer. "
			"The assessment association (general_assessment / lesson_assessment) cannot be changed here. "
			"Requires content creator or validator role."
		),
	)
	@action(detail=False, methods=['patch'], url_path='questions/(?P<pk>[^/.]+)')
	def update_question(self, request, pk=None):
		deny = self._require_creator(request)
		if deny:
			return deny
		try:
			obj = Question.objects.prefetch_related('options').get(pk=pk)
		except Question.DoesNotExist:
			return Response({"detail": "Question not found."}, status=status.HTTP_404_NOT_FOUND)
		allowed_fields = {'type', 'question', 'answer'}
		update_data = {k: v for k, v in request.data.items() if k in allowed_fields}
		if not update_data:
			return Response({"detail": "No updatable fields provided. Allowed: type, question, answer."}, status=status.HTTP_400_BAD_REQUEST)
		for field, value in update_data.items():
			setattr(obj, field, value)
		obj.save(update_fields=list(update_data.keys()))
		return Response(QuestionSerializer(obj).data)


class OnboardingViewSet(viewsets.ViewSet):
	"""Endpoints to onboard users step-by-step.
	- profilesetup: create user and return token
	- role: set role and create associated profile
	- aboutyou: set personal details and optional institution/grade
	- linkchild: link a student to a parent profile
	"""
	class OnboardingDummySerializer(serializers.Serializer):
		"""Placeholder for schema generation only."""
		id = serializers.IntegerField(read_only=True)

	serializer_class = OnboardingDummySerializer

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
		allowed = {UserRole.STUDENT.value, UserRole.TEACHER.value, UserRole.HEADTEACHER.value, UserRole.PARENT.value}
		
		if role not in allowed:
			return Response({"detail": f"Invalid role. Allowed: {', '.join(sorted(allowed))}"}, status=400)
		
		user: User = request.user
		user.role = role
		user.save(update_fields=['role', 'updated_at'])

		# ensure profile exists
		if role == UserRole.STUDENT.value and not hasattr(user, 'student'):
			Student.objects.create(profile=user)
		elif role in {UserRole.TEACHER.value, UserRole.HEADTEACHER.value} and not hasattr(user, 'teacher'):
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
		elif user.role in {UserRole.TEACHER.value, UserRole.HEADTEACHER.value} and hasattr(user, 'teacher'):
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

		qs = Student.objects.select_related('profile').filter(student_id=student_id)
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

	class LoginDummySerializer(serializers.Serializer):
		"""Placeholder for schema generation only."""
		id = serializers.IntegerField(read_only=True)

	serializer_class = LoginDummySerializer

	def _school_snapshot(self, school) -> dict | None:
		if school is None:
			return None
		district = getattr(school, 'district', None)
		county = getattr(district, 'county', None) if district is not None else None
		return {
			'id': getattr(school, 'id', None),
			'name': getattr(school, 'name', None),
			'district_id': getattr(school, 'district_id', None),
			'district_name': getattr(district, 'name', None),
			'county_id': getattr(district, 'county_id', None),
			'county_name': getattr(county, 'name', None),
		}

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
				"points": getattr(student, "points", 0),
				"current_login_streak": getattr(student, "current_login_streak", 0),
				"max_login_streak": getattr(student, "max_login_streak", 0),
				"last_login_activity_date": getattr(student, "last_login_activity_date", None),
				"school": self._school_snapshot(getattr(student, 'school', None)),
				"status": getattr(student, "status", None),
			}

		# Attach teacher profile snapshot, if present
		teacher = getattr(user, 'teacher', None)
		if teacher is not None:
			payload["teacher"] = {
				"id": teacher.id,
				"school_id": getattr(teacher, "school_id", None),
				"school": self._school_snapshot(getattr(teacher, 'school', None)),
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
		allowed = {UserRole.CONTENTCREATOR.value, UserRole.CONTENTVALIDATOR.value, UserRole.TEACHER.value, UserRole.HEADTEACHER.value}
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
		if user.role in {UserRole.TEACHER.value, UserRole.HEADTEACHER.value} and hasattr(user, 'teacher') and user.teacher:
			if getattr(user.teacher, 'status', StatusEnum.PENDING.value) != StatusEnum.APPROVED.value:
				return Response(
					{"detail": "Your teacher account is awaiting approval by a content validator or administrator."},
					status=403,
				)

		# For students, update streak once per calendar day (server timezone).
		self._update_student_login_streak(user)

		token = AuthToken.objects.create(user)[1]

		student_payload = None
		if user.role == UserRole.STUDENT.value and hasattr(user, 'student') and user.student:
			s = user.student
			school_payload = self._school_snapshot(getattr(s, 'school', None))

			student_payload = {
				'id': s.id,
				'grade': getattr(s, 'grade', None),
				'points': getattr(s, 'points', 0),
				'current_login_streak': getattr(s, 'current_login_streak', 0),
				'max_login_streak': getattr(s, 'max_login_streak', 0),
				'last_login_activity_date': getattr(s, 'last_login_activity_date', None),
				'school': school_payload,
			}

		teacher_payload = None
		if user.role in {UserRole.TEACHER.value, UserRole.HEADTEACHER.value} and hasattr(user, 'teacher') and user.teacher:
			t = user.teacher
			teacher_payload = {
				'id': t.id,
				'school_id': getattr(t, 'school_id', None),
				'school': self._school_snapshot(getattr(t, 'school', None)),
				'status': getattr(t, 'status', None),
			}

		return Response({
			"token": token,
			"user": UserSerializer(user).data,
			**({"student": student_payload} if student_payload else {}),
			**({"teacher": teacher_payload} if teacher_payload else {}),
		})

	def _update_student_login_streak(self, user: User) -> None:
		if user.role != UserRole.STUDENT.value:
			return
		student = getattr(user, 'student', None)
		if student is None:
			return

		today = timezone.localdate()
		last_day = getattr(student, 'last_login_activity_date', None)

		# Multiple logins on the same day should only count once.
		if last_day == today:
			return

		if last_day is not None and last_day == (today - timedelta(days=1)):
			student.current_login_streak = int(getattr(student, 'current_login_streak', 0) or 0) + 1
		else:
			student.current_login_streak = 1

		student.max_login_streak = max(
			int(getattr(student, 'max_login_streak', 0) or 0),
			student.current_login_streak,
		)
		student.last_login_activity_date = today
		student.save(update_fields=['current_login_streak', 'max_login_streak', 'last_login_activity_date'])


class DashboardViewSet(viewsets.ViewSet):
	permission_classes = [permissions.IsAuthenticated]

	class DashboardDummySerializer(serializers.Serializer):
		"""Placeholder for schema generation only."""
		id = serializers.IntegerField(read_only=True)

	serializer_class = DashboardDummySerializer

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
		# Make maps for quick lookups using one batched lesson query.
		in_progress_subject_ids = [subj.id for subj in in_progress_subjects]
		lessons_by_subject: Dict[int, List[dict]] = {sid: [] for sid in in_progress_subject_ids}
		if in_progress_subject_ids:
			all_in_progress_lessons = (
				LessonResource.objects
				.filter(subject_id__in=in_progress_subject_ids)
				.values('id', 'subject_id', 'title', 'duration_minutes')
			)
			for row in all_in_progress_lessons:
				lessons_by_subject[row['subject_id']].append(row)
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

		cache_key = _student_lesson_cache_key(student, request, 'kids-assignments-due')
		cached_payload = cache.get(cache_key)
		if cached_payload is not None:
			return Response(cached_payload)

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
		payload = _paginate_payload(request, items, 'assignments')
		cache.set(cache_key, payload, timeout=STUDENT_LESSON_CACHE_TTL)
		return Response(payload)

	@action(detail=False, methods=['get'], url_path='studystats')
	def studystats(self, request):
		"""Return aggregate study stats for the authenticated student."""
		user: User = request.user
		student = getattr(user, 'student', None)
		if not student:
			return Response({"detail": "Student profile required."}, status=403)

		cache_key = _student_lesson_cache_key(student, request, 'kids-study-stats')
		cached_payload = cache.get(cache_key)
		if cached_payload is not None:
			return Response(cached_payload)

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
		cache.set(cache_key, data, timeout=STUDENT_LESSON_CACHE_TTL)
		return Response(data)


class KidsAssessmentInfoSerializer(serializers.Serializer):
	id = serializers.IntegerField()
	title = serializers.CharField()
	type = serializers.CharField()


class KidsAssessmentOptionSerializer(serializers.Serializer):
	id = serializers.IntegerField()
	value = serializers.CharField()


class KidsAssessmentQuestionSerializer(serializers.Serializer):
	id = serializers.IntegerField()
	type = serializers.CharField()
	question = serializers.CharField()
	options = KidsAssessmentOptionSerializer(many=True)


class KidsAssessmentQuestionsResponseSerializer(serializers.Serializer):
	assessment = KidsAssessmentInfoSerializer()
	questions = KidsAssessmentQuestionSerializer(many=True)


class KidsPeerSolutionItemSerializer(serializers.Serializer):
	peer_label = serializers.CharField()
	solution = serializers.CharField(allow_blank=True)
	attachment = serializers.CharField(allow_null=True)
	submitted_at = serializers.DateTimeField(allow_null=True)


class KidsPeerSolutionsResponseSerializer(serializers.Serializer):
	assessment = KidsAssessmentInfoSerializer()
	solutions = KidsPeerSolutionItemSerializer(many=True)


class KidsViewSet(viewsets.ViewSet):
	"""Endpoints tailored for younger students (grades 1–3)."""
	permission_classes = [permissions.IsAuthenticated]

	@extend_schema(
		description="Read-only list of published stories for kids. Supports filtering by grade and tag.",
		parameters=[
			OpenApiParameter(name='grade', required=False, location=OpenApiParameter.QUERY, type=str),
			OpenApiParameter(name='tag', required=False, location=OpenApiParameter.QUERY, type=str),
		],
		responses={200: StoryListSerializer(many=True)},
	)
	@action(detail=False, methods=['get'], url_path='stories')
	def stories(self, request):
		user: User = request.user
		student = getattr(user, 'student', None)
		if not student:
			return Response({"detail": "Student profile required."}, status=403)

		qs = _published_stories_for_school(getattr(student, 'school_id', None)).order_by('-created_at')

		grade = request.query_params.get('grade')
		if grade:
			qs = qs.filter(grade=grade)
		else:
			# By default, return stories for the student's grade.
			qs = qs.filter(grade=student.grade)

		tag = request.query_params.get('tag')
		if tag:
			qs = qs.filter(tag__iexact=tag.strip())

		return Response(StoryListSerializer(qs, many=True).data)

	@extend_schema(
		description="Read-only detail for a single published story.",
		parameters=[
			OpenApiParameter(name='pk', required=True, location=OpenApiParameter.PATH, type=int),
		],
		responses={200: StoryDetailSerializer},
	)
	@action(detail=False, methods=['get'], url_path='stories/(?P<pk>[^/.]+)')
	def story_detail(self, request, pk=None):
		user: User = request.user
		student = getattr(user, 'student', None)
		if not student:
			return Response({"detail": "Student profile required."}, status=403)

		try:
			story = _published_stories_for_school(getattr(student, 'school_id', None)).get(pk=pk)
		except Story.DoesNotExist:
			return Response({"detail": "Story not found."}, status=404)

		return Response(StoryDetailSerializer(story).data)

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

		cache_key = _student_lesson_cache_key(student, request, 'kids-dashboard')
		cached_payload = cache.get(cache_key)
		if cached_payload is not None:
			return Response(cached_payload)

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
			.select_related('lesson__topic__subject')
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

		payload = {
			'lessons_completed': lessons_completed,
			'streaks_this_week': streak_days,
			'current_level': current_level.replace('_', ' '),
			'points_earned': points_earned,
			'todays_challenges': todays_challenges,
			'continue_learning': continue_learning,
			'recent_activities': recent_activities,
		}
		cache.set(cache_key, payload, timeout=STUDENT_LESSON_CACHE_TTL)
		return Response(payload)

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

		cache_key = _student_lesson_cache_key(student, request, 'kids-progress-garden')
		cached_payload = cache.get(cache_key)
		if cached_payload is not None:
			return Response(cached_payload)

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

		student_district_id = getattr(getattr(student.school, 'district', None), 'id', None) if getattr(student, 'school', None) else None
		student_county_id = None
		if getattr(student, 'school', None) and getattr(student.school, 'district', None) and getattr(student.school.district, 'county', None):
			student_county_id = student.school.district.county_id

		# Precompute performance by student once for the broadest available scope,
		# then reuse for school/district/county rank calculations.
		county_students_map = {}
		district_students_map = {}
		school_students_map = {}
		student_scope_qs = Student.objects.all()
		if student_county_id:
			student_scope_qs = student_scope_qs.filter(school__district__county_id=student_county_id)
		elif student_district_id:
			student_scope_qs = student_scope_qs.filter(school__district_id=student_district_id)
		elif getattr(student, 'school_id', None):
			student_scope_qs = student_scope_qs.filter(school_id=student.school_id)
		else:
			student_scope_qs = student_scope_qs.filter(id=student.id)

		for row in student_scope_qs.values('id', 'school_id', 'school__district_id', 'school__district__county_id'):
			sid = row['id']
			if row['school_id'] is not None:
				school_students_map.setdefault(row['school_id'], set()).add(sid)
			if row['school__district_id'] is not None:
				district_students_map.setdefault(row['school__district_id'], set()).add(sid)
			if row['school__district__county_id'] is not None:
				county_students_map.setdefault(row['school__district__county_id'], set()).add(sid)

		scope_student_ids = set()
		for ids in school_students_map.values():
			scope_student_ids.update(ids)
		for ids in district_students_map.values():
			scope_student_ids.update(ids)
		for ids in county_students_map.values():
			scope_student_ids.update(ids)

		perf_scores_by_student = {}
		if scope_student_ids:
			lessons_by_student = {
				row['student']: row['lessons']
				for row in (
					TakeLesson.objects
					.filter(student_id__in=scope_student_ids)
					.values('student')
					.annotate(lessons=DjangoCount('id', distinct=True))
				)
			}
			active_student_ids = list(lessons_by_student.keys())
			if active_student_ids:
				lesson_scores_by_student = {
					row['student']: row['avg']
					for row in (
						LessonAssessmentGrade.objects
						.filter(student_id__in=active_student_ids)
						.values('student')
						.annotate(avg=Avg('score'))
					)
				}
				general_scores_by_student = {
					row['student']: row['avg']
					for row in (
						GeneralAssessmentGrade.objects
						.filter(student_id__in=active_student_ids)
						.values('student')
						.annotate(avg=Avg('score'))
					)
				}
				for sid in active_student_ids:
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
					perf_scores_by_student[sid] = lessons + (avg * 2.0)

		def _compute_rank_for_student_ids(candidate_ids):
			perf_scores = [
				(sid, score)
				for sid, score in perf_scores_by_student.items()
				if sid in candidate_ids
			]
			if not perf_scores:
				return None, 0
			perf_scores.sort(key=lambda x: x[1], reverse=True)
			rank = None
			for idx, (sid, _) in enumerate(perf_scores, start=1):
				if sid == student.id:
					rank = idx
					break
			return rank, len(perf_scores)

		# School rank (if school attached)
		school_rank = None
		if getattr(student, 'school_id', None):
			school_ids = school_students_map.get(student.school_id, set())
			school_rank_val, school_total = _compute_rank_for_student_ids(school_ids)
			if school_rank_val is not None:
				school_rank = {
					'rank': school_rank_val,
					'out_of': school_total,
				}

		# District rank (if district attached)
		district_rank = None
		if student_district_id:
			district_ids = district_students_map.get(student_district_id, set())
			dist_rank_val, dist_total = _compute_rank_for_student_ids(district_ids)
			if dist_rank_val is not None:
				district_rank = {
					'rank': dist_rank_val,
					'out_of': dist_total,
				}

		# County rank (if county attached)
		county_rank = None
		if student_county_id:
			county_ids = county_students_map.get(student_county_id, set())
			county_rank_val, county_total = _compute_rank_for_student_ids(county_ids)
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

		payload = {
			"lessons_completed": lessons_completed,
			"longest_streak": longest_streak,
			"level": level.replace('_', ' '),
			"points": points,
			"subjects": subjects_payload,
			"rank_in_school": school_rank,
			"rank_in_district": district_rank,
			"rank_in_county": county_rank,
		}
		cache.set(cache_key, payload, timeout=STUDENT_LESSON_CACHE_TTL)
		return Response(payload)

	@extend_schema(
		description="Subjects and lessons for the student's grade (kids view).",
		responses={200: KidsSubjectsAndLessonsResponseSerializer},
		examples=[
			OpenApiExample(
				name="KidsSubjectsAndLessonsExample",
				value={
					"subjects": [
						{"id": 1, "name": "Mathematics", "grade": "GRADE 3", "thumbnail": None},
						{"id": 2, "name": "Science", "grade": "GRADE 3", "thumbnail": None},
					],
					"lessons": [
						{
							"id": 10,
							"title": "Addition Basics",
							"subject_id": 1,
							"subject_name": "Mathematics",
							"grade": "GRADE 3",
							"topic_id": 3,
							"topic_name": "Numbers",
							"period_id": 1,
							"period_name": "January",
							"resource_type": "VIDEO",
							"thumbnail": None,
							"resource": "/media/lesson_resources/addition-basics.mp4",
							"status": "taken",
							"progression_status": "completed",
							"is_locked": False,
							"is_completed": True,
							"assessments_total": 1,
							"assessments_completed": 1,
							"next_video_id": 11,
							"lock_reason": None,
							"sequence_position": 1,
						},
						{
							"id": 11,
							"title": "Animals Around Us",
							"subject_id": 2,
							"subject_name": "Science",
							"grade": "GRADE 3",
							"topic_id": None,
							"topic_name": None,
							"period_id": 2,
							"period_name": "February",
							"resource_type": "VIDEO",
							"thumbnail": None,
							"resource": "/media/lesson_resources/animals-around-us.mp4",
							"status": "new",
							"progression_status": "locked",
							"is_locked": True,
							"is_completed": False,
							"assessments_total": 2,
							"assessments_completed": 0,
							"next_video_id": None,
							"lock_reason": "Complete the previous lesson and submit all of its assessments to unlock this lesson.",
							"sequence_position": 2,
						},
					],
				},
			),
		],
	)
	@action(detail=False, methods=['get'], url_path='subjectsandlessons')
	def subjects_and_lessons(self, request):
		"""Return ordered lessons with progression lock state for the student."""
		user: User = request.user
		student = getattr(user, 'student', None)
		if not student:
			return Response({"detail": "Student profile required."}, status=403)

		cache_key = _student_lesson_cache_key(student, request, 'kids-subjects-and-lessons')
		cached_payload = cache.get(cache_key)
		if cached_payload is not None:
			return Response(cached_payload)

		# Subjects for the student's grade
		subjects_qs = Subject.objects.filter(
			grade=student.grade,
			status=StatusEnum.APPROVED.value,
		).order_by('name')
		subjects_payload = [
			{"id": s.id, "name": s.name, "grade": s.grade, "thumbnail": s.thumbnail.url if s.thumbnail else None}
			for s in subjects_qs
		]

		progression = _build_student_lesson_progression(student)
		lessons = progression['lessons']
		if not lessons:
			payload = {
				"subjects": subjects_payload,
				"lessons": [],
				"pagination": {
					"count": 0,
					"next": None,
					"previous": None,
					"page_size": StandardResultsSetPagination.page_size,
				},
			}
			cache.set(cache_key, payload, timeout=STUDENT_LESSON_CACHE_TTL)
			return Response(payload)

		lessons_payload = []
		for lesson in lessons:
			state = progression['states'][lesson.id]

			lessons_payload.append({
				"id": lesson.id,
				"title": lesson.title,
				"subject_id": lesson.subject_id,
				"subject_name": getattr(lesson.subject, 'name', None),
				"grade": getattr(lesson.subject, 'grade', None),
				"topic_id": lesson.topic_id,
				"topic_name": getattr(lesson.topic, 'name', None),
				"period_id": lesson.period_id,
				"period_name": getattr(lesson.period, 'name', None),
				"resource_type": lesson.type,
				"thumbnail": lesson.thumbnail.url if lesson.thumbnail else None,
				"resource": lesson.resource.url if lesson.resource else None,
				"status": "taken" if state['is_taken'] else "new",
				"progression_status": state['progression_status'],
				"is_locked": state['is_locked'],
				"is_temporarily_unlocked": state['is_temporarily_unlocked'],
				"temporary_unlock_expires_at": (
					state['temporary_unlock_expires_at'].isoformat()
					if state['temporary_unlock_expires_at'] else None
				),
				"is_completed": state['is_completed'],
				"assessments_total": state['assessments_total'],
				"assessments_completed": state['assessments_completed'],
				"next_video_id": state['next_video_id'],
				"lock_reason": state['lock_reason'],
				"sequence_position": state['sequence_position'],
			})

		payload = _paginate_payload(
			request,
			lessons_payload,
			'lessons',
			extra_payload={"subjects": subjects_payload},
		)
		cache.set(cache_key, payload, timeout=STUDENT_LESSON_CACHE_TTL)
		return Response(payload)

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

		cache_key = _student_lesson_cache_key(student, request, 'kids-assignments')
		cached_payload = cache.get(cache_key)
		if cached_payload is not None:
			return Response(cached_payload)

		# General assessments for grade or global
		general_qs = (
			GeneralAssessment.objects
			.filter(
				(models.Q(grade__isnull=True) | models.Q(grade=student.grade))
				& (models.Q(is_targeted=False) | models.Q(target_student=student))
			)
			.order_by('due_at', 'title')
		)

		# Lesson assessments for lessons in this grade
		lesson_qs = (
			LessonAssessment.objects
			.filter(
				lesson__subject__grade=student.grade,
			).filter(models.Q(is_targeted=False) | models.Q(target_student=student))
			.order_by('due_at', 'title')
		)

		now = timezone.now()
		in_5 = now + timedelta(days=5)

		# Prefetch existing solutions/grades to avoid per-row queries
		general_ids = list(general_qs.values_list('id', flat=True))
		lesson_ids = list(lesson_qs.values_list('id', flat=True))

		general_solutions = list(
			AssessmentSolution.objects
			.filter(assessment_id__in=general_ids, student=student)
		)
		general_solution_map = {sol.assessment_id: sol for sol in general_solutions}
		lesson_solutions = list(
			LessonAssessmentSolution.objects
			.filter(lesson_assessment_id__in=lesson_ids, student=student)
		)
		lesson_solution_map = {sol.lesson_assessment_id: sol for sol in lesson_solutions}
		lesson_grade_ids = set(
			LessonAssessmentGrade.objects.filter(
				lesson_assessment_id__in=lesson_ids,
				student=student,
			).values_list('lesson_assessment_id', flat=True)
		)

		items = []
		total = 0
		pending = 0
		due_soon = 0
		overdue = 0
		submitted = 0

		for ga in general_qs.only('id', 'title', 'instructions', 'due_at'):
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

		for la in lesson_qs.only('id', 'title', 'instructions', 'due_at'):
			lesson_solution_obj = lesson_solution_map.get(la.id)
			status = "submitted" if (la.id in lesson_grade_ids or lesson_solution_obj is not None) else "pending"
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
				"solution": LessonAssessmentSolutionSerializer(lesson_solution_obj).data if lesson_solution_obj else None,
				"due_at": la.due_at.isoformat() if la.due_at else None,
			})

		stats = {
			"total": total,
			"pending": pending,
			"due_soon": due_soon,
			"overdue": overdue,
			"submitted": submitted,
		}

		payload = _paginate_payload(request, items, 'assignments', extra_payload={"stats": stats})
		cache.set(cache_key, payload, timeout=STUDENT_LESSON_CACHE_TTL)
		return Response(payload)

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

		cache_key = _student_lesson_cache_key(student, request, 'kids-quizzes')
		cached_payload = cache.get(cache_key)
		if cached_payload is not None:
			return Response(cached_payload)

		# General assessments for grade or global
		general_qs = (
			GeneralAssessment.objects
			.filter(
				(models.Q(grade__isnull=True) | models.Q(grade=student.grade))
				& (models.Q(is_targeted=False) | models.Q(target_student=student))
			)
			.values('id', 'title', 'due_at')
			.order_by('due_at', 'title')
		)

		# Lesson assessments via lessons in student's grade
		lesson_qs = (
			LessonAssessment.objects
			.filter(
				lesson__subject__grade=student.grade,
			).filter(models.Q(is_targeted=False) | models.Q(target_student=student))
			.values('id', 'title', 'lesson_id', 'due_at')
			.order_by('due_at', 'title')
		)

		payload = []
		for ga in general_qs:
			payload.append({
				"id": ga['id'],
				"title": ga['title'],
				"type": "general",
				"due_at": ga['due_at'].isoformat() if ga['due_at'] else None,
			})
		for la in lesson_qs:
			payload.append({
				"id": la['id'],
				"title": la['title'],
				"type": "lesson",
				"lesson_id": la['lesson_id'],
				"due_at": la['due_at'].isoformat() if la['due_at'] else None,
			})

		response_payload = _paginate_payload(request, payload, 'quizzes')
		cache.set(cache_key, response_payload, timeout=STUDENT_LESSON_CACHE_TTL)
		return Response(response_payload)

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
		_, created = GamePlay.objects.update_or_create(
			student=student,
			game=game,
			defaults={},
		)
		points_awarded = GAME_PLAY_POINTS if created else 0
		total_points = _award_student_points(student, GAME_PLAY_POINTS) if created else getattr(student, 'points', 0)

		# Optionally log as an Activity for richer feeds/analytics
		Activity.objects.create(
			user=user,
			type="play_game",
			description=f"Played game '{game.name}'",
			metadata={"game_id": game.id, "game_type": game.type, "points_awarded": points_awarded},
		)

		return Response({
			"detail": "Game play recorded.",
			"points_awarded": points_awarded,
			"total_points": total_points,
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

		cache_key = _student_lesson_cache_key(student, request, 'kids-assessments')
		cached_payload = cache.get(cache_key)
		if cached_payload is not None:
			return Response(cached_payload)

		general_qs = (
			GeneralAssessment.objects
			.filter(
				(models.Q(grade__isnull=True) | models.Q(grade=student.grade))
				& (models.Q(is_targeted=False) | models.Q(target_student=student))
			)
			.values('id', 'title', 'marks')
			.order_by('title')
		)
		lesson_qs = (
			LessonAssessment.objects
			.filter(
				lesson__subject__grade=student.grade,
			).filter(models.Q(is_targeted=False) | models.Q(target_student=student))
			.values('id', 'title', 'lesson_id', 'marks')
			.order_by('title')
		)

		items = []
		for ga in general_qs:
			items.append({
				"id": ga['id'],
				"title": ga['title'],
				"type": "general",
				"marks": ga['marks'],
			})
		for la in lesson_qs:
			items.append({
				"id": la['id'],
				"title": la['title'],
				"type": "lesson",
				"lesson_id": la['lesson_id'],
				"marks": la['marks'],
			})

		payload = _paginate_payload(request, items, 'assessments')
		cache.set(cache_key, payload, timeout=STUDENT_LESSON_CACHE_TTL)
		return Response(payload)

	@extend_schema(
		description=(
			"Get questions and options for a specific assessment. "
			"Pass either ?general_id=<id> or ?lesson_id=<id>."
		),
		parameters=[
			OpenApiParameter(
				name="general_id",
				location=OpenApiParameter.QUERY,
				type=int,
				required=False,
				description=(
					"GeneralAssessment ID. Provide exactly one of general_id or lesson_id."
				),
			),
			OpenApiParameter(
				name="lesson_id",
				location=OpenApiParameter.QUERY,
				type=int,
				required=False,
				description=(
					"LessonAssessment ID. Provide exactly one of general_id or lesson_id."
				),
			),
		],
		responses={
			200: OpenApiResponse(
				response=KidsAssessmentQuestionsResponseSerializer,
				description="Assessment questions payload.",
			),
		},
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
				response_only=True,
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
		description=(
			"Return up to 10 random peer solutions for an assessment. "
			"Student must be qualified for the assessment and must have submitted their own solution first. "
			"Peer identities are anonymized."
		),
		parameters=[
			OpenApiParameter(
				name="general_id",
				location=OpenApiParameter.QUERY,
				type=int,
				required=False,
				description="GeneralAssessment ID. Provide exactly one of general_id or lesson_id.",
			),
			OpenApiParameter(
				name="lesson_id",
				location=OpenApiParameter.QUERY,
				type=int,
				required=False,
				description="LessonAssessment ID. Provide exactly one of general_id or lesson_id.",
			),
		],
		responses={200: KidsPeerSolutionsResponseSerializer},
	)
	@action(detail=False, methods=['get'], url_path='peer-solutions')
	def peer_solutions(self, request):
		user: User = request.user
		student = getattr(user, 'student', None)
		if not student:
			return Response({"detail": "Student profile required."}, status=403)

		def _anon_peer_label(*, scope: str, assessment_id: int, peer_student_id: int) -> str:
			raw = f"{scope}:{assessment_id}:{peer_student_id}"
			token = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:8].upper()
			return f"Peer Student {token}"

		general_id = request.query_params.get('general_id')
		lesson_id = request.query_params.get('lesson_id')
		if bool(general_id) == bool(lesson_id):
			return Response({"detail": "Provide exactly one of general_id or lesson_id."}, status=400)

		assessment_info = None
		solutions_payload = []

		if general_id:
			assessment = (
				GeneralAssessment.objects
				.filter(id=general_id)
				.filter(
					(models.Q(grade__isnull=True) | models.Q(grade=student.grade))
					& (models.Q(is_targeted=False) | models.Q(target_student=student))
				)
				.first()
			)
			if not assessment:
				return Response({"detail": "Assessment not found or not available for you."}, status=403)

			has_own_solution = AssessmentSolution.objects.filter(assessment=assessment, student=student).exists()
			if not has_own_solution:
				return Response({"detail": "Submit your own solution first to view peer solutions."}, status=403)

			peer_qs = (
				AssessmentSolution.objects
				.filter(assessment=assessment)
				.exclude(student=student)
				.order_by('?')[:10]
			)

			assessment_info = {"id": assessment.id, "title": assessment.title, "type": "general"}
			for peer_sol in peer_qs:
				attachment_url = None
				if getattr(peer_sol, 'attachment', None):
					try:
						attachment_url = request.build_absolute_uri(peer_sol.attachment.url)
					except Exception:
						attachment_url = None
				solutions_payload.append({
					"peer_label": _anon_peer_label(scope='general', assessment_id=assessment.id, peer_student_id=peer_sol.student_id),
					"solution": peer_sol.solution or "",
					"attachment": attachment_url,
					"submitted_at": getattr(peer_sol, 'submitted_at', None),
				})
		else:
			assessment = (
				LessonAssessment.objects
				.filter(id=lesson_id)
				.filter(lesson__subject__grade=student.grade)
				.filter(models.Q(is_targeted=False) | models.Q(target_student=student))
				.first()
			)
			if not assessment:
				return Response({"detail": "Assessment not found or not available for you."}, status=403)

			has_own_solution = LessonAssessmentSolution.objects.filter(lesson_assessment=assessment, student=student).exists()
			if not has_own_solution:
				return Response({"detail": "Submit your own solution first to view peer solutions."}, status=403)

			peer_qs = (
				LessonAssessmentSolution.objects
				.filter(lesson_assessment=assessment)
				.exclude(student=student)
				.order_by('?')[:10]
			)

			assessment_info = {"id": assessment.id, "title": assessment.title, "type": "lesson"}
			for peer_sol in peer_qs:
				attachment_url = None
				if getattr(peer_sol, 'attachment', None):
					try:
						attachment_url = request.build_absolute_uri(peer_sol.attachment.url)
					except Exception:
						attachment_url = None
				solutions_payload.append({
					"peer_label": _anon_peer_label(scope='lesson', assessment_id=assessment.id, peer_student_id=peer_sol.student_id),
					"solution": peer_sol.solution or "",
					"attachment": attachment_url,
					"submitted_at": getattr(peer_sol, 'submitted_at', None),
				})

		return Response({
			"assessment": assessment_info,
			"solutions": solutions_payload,
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

		Exactly one of general_id or lesson_id must be provided.
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

			points_awarded = ASSESSMENT_SUBMISSION_POINTS if created else 0
			total_points = _award_student_points(student, ASSESSMENT_SUBMISSION_POINTS) if created else getattr(student, 'points', 0)

			Activity.objects.create(
				user=user,
				type="submit_general_assessment",
				description=f"Submitted solution for '{assessment.title}'",
				metadata={
					'assessment_id': assessment.id,
					'points_awarded': points_awarded,
				},
			)

			_invalidate_student_lesson_cache(student)

			return Response({
				"detail": "Solution submitted.",
				"solution_id": solution_obj.id,
				"points_awarded": points_awarded,
				"total_points": total_points,
			})

		lesson_assessment = LessonAssessment.objects.filter(id=lesson_id).first()
		if not lesson_assessment:
			return Response({"detail": "Lesson assessment not found."}, status=404)

		solution_obj, created = LessonAssessmentSolution.objects.get_or_create(
			lesson_assessment=lesson_assessment,
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

		points_awarded = ASSESSMENT_SUBMISSION_POINTS if created else 0
		total_points = _award_student_points(student, ASSESSMENT_SUBMISSION_POINTS) if created else getattr(student, 'points', 0)

		Activity.objects.create(
			user=user,
			type="submit_lesson_assessment",
			description=f"Submitted solution for '{lesson_assessment.title}'",
			metadata={
				'lesson_assessment_id': lesson_assessment.id,
				'points_awarded': points_awarded,
			},
		)

		_invalidate_student_lesson_cache(student)

		return Response({
			"detail": "Solution submitted.",
			"solution_id": solution_obj.id,
			"points_awarded": points_awarded,
			"total_points": total_points,
		})


class TeacherViewSet(viewsets.ViewSet):
	"""Endpoints specifically for teachers to manage their classroom.

	Teachers can:
	- View subjects and lessons for their grade.
	- Create lessons and assessments.
	- View students in their school and approve them.
	"""
	permission_classes = [permissions.IsAuthenticated]

	class GenerateAIAssessmentsResponseSerializer(serializers.Serializer):
		general_assessments = GeneralAssessmentSerializer(many=True)
		lesson_assessments = LessonAssessmentSerializer(many=True)

	def _require_teacher(self, request):
		user: User = request.user
		if not user or getattr(user, 'role', None) not in {UserRole.TEACHER.value, UserRole.HEADTEACHER.value, UserRole.ADMIN.value}:
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

	def _teacher_leaderboard_grades(self, teacher: Teacher) -> List[str]:
		return list(
			Subject.objects
			.filter(teachers=teacher)
			.order_by()
			.values_list('grade', flat=True)
			.distinct()
		)

	def _leaderboard_student_queryset(self, request):
		teacher = request.user.teacher
		if not getattr(teacher, 'school_id', None):
			return Student.objects.none()

		grades = self._teacher_leaderboard_grades(teacher)
		if not grades:
			return Student.objects.none()

		return Student.objects.filter(
			school_id=teacher.school_id,
			grade__in=grades,
			status=StatusEnum.APPROVED.value,
		)

	def _leaderboard_scope(self, request) -> dict:
		teacher = request.user.teacher
		school = getattr(teacher, 'school', None)
		return {
			'kind': 'class',
			'school_id': getattr(school, 'id', None),
			'school_name': getattr(school, 'name', None),
			'grades': self._teacher_leaderboard_grades(teacher),
		}

	def _validate_unlock_scope(self, teacher: Teacher, student: Student, lesson: LessonResource) -> str | None:
		if not getattr(teacher, 'school_id', None) or student.school_id != teacher.school_id:
			return "Student not found in your school."
		if student.grade != getattr(getattr(lesson, 'subject', None), 'grade', None):
			return "Lesson grade does not match the student's grade."
		teaches_subject = Subject.objects.filter(id=lesson.subject_id, teachers=teacher).exists()
		if not teaches_subject:
			return "You can only unlock lessons for subjects you teach."
		return None

	def _serialize_unlock(self, unlock: LessonTemporaryUnlock) -> dict:
		return {
			"id": unlock.id,
			"student_id": unlock.student_id,
			"lesson_id": unlock.lesson_id,
			"unlocked_by_id": unlock.unlocked_by_id,
			"reason": unlock.reason,
			"expires_at": unlock.expires_at,
			"revoked_at": unlock.revoked_at,
		}

	@extend_schema(
		description=(
			"List published stories for a teacher's grade scope. Returns global published stories "
			"plus published stories tied to the teacher's school."
		),
		parameters=[
			OpenApiParameter(name='grade', required=False, location=OpenApiParameter.QUERY, type=str),
			OpenApiParameter(name='tag', required=False, location=OpenApiParameter.QUERY, type=str),
		],
		responses={200: StoryListSerializer(many=True)},
	)
	@action(detail=False, methods=['get'], url_path='stories')
	def stories(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny

		teacher = request.user.teacher
		teacher_grades = self._teacher_leaderboard_grades(teacher)
		if not teacher_grades:
			return Response([])

		qs = _published_stories_for_school(getattr(teacher, 'school_id', None)).filter(grade__in=teacher_grades)

		grade = (request.query_params.get('grade') or '').strip()
		if grade:
			if grade not in teacher_grades:
				return Response({"detail": "You can only access stories for grades you teach."}, status=403)
			qs = qs.filter(grade=grade)

		tag = (request.query_params.get('tag') or '').strip()
		if tag:
			qs = qs.filter(tag__iexact=tag)

		return Response(StoryListSerializer(qs.order_by('-created_at'), many=True).data)

	@extend_schema(
		description="Read story detail for a teacher within the teacher's published visibility scope.",
		parameters=[
			OpenApiParameter(name='pk', required=True, location=OpenApiParameter.PATH, type=int),
		],
		responses={200: StoryDetailSerializer},
	)
	@action(detail=False, methods=['get'], url_path='stories/(?P<pk>[^/.]+)')
	def story_detail(self, request, pk=None):
		deny = self._require_teacher(request)
		if deny:
			return deny

		teacher = request.user.teacher
		teacher_grades = self._teacher_leaderboard_grades(teacher)
		if not teacher_grades:
			return Response({"detail": "Story not found."}, status=404)

		try:
			story = (
				_published_stories_for_school(getattr(teacher, 'school_id', None))
				.filter(grade__in=teacher_grades)
				.get(pk=pk)
			)
		except Story.DoesNotExist:
			return Response({"detail": "Story not found."}, status=404)

		return Response(StoryDetailSerializer(story).data)

	@extend_schema(
		description=(
			"Queue AI story generation for teacher/headteacher users. Generated stories are tied "
			"to the teacher's school and start unpublished."
		),
		request=StoryGenerateRequestSerializer,
		responses={202: OpenApiResponse(description="Story generation task queued.")},
	)
	@action(detail=False, methods=['post'], url_path='stories/generate')
	def generate_stories(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny

		teacher = request.user.teacher
		if not getattr(teacher, 'school_id', None):
			return Response({"detail": "Teacher must be assigned to a school."}, status=403)

		ser = StoryGenerateRequestSerializer(data=request.data)
		ser.is_valid(raise_exception=True)
		data = ser.validated_data

		is_headteacher = getattr(request.user, 'role', None) == UserRole.HEADTEACHER.value
		if not is_headteacher:
			teacher_grades = self._teacher_leaderboard_grades(teacher)
			if data['grade'] not in teacher_grades:
				return Response({"detail": "You can only generate stories for grades you teach."}, status=403)

		task = _enqueue_story_generation(
			requested_by_id=request.user.id,
			grade=data['grade'],
			tag=data['tag'],
			count=data['count'],
			school_id=teacher.school_id,
		)
		return Response(
			{
				"detail": "Story generation queued.",
				"task_id": str(task.id),
				"requested": data,
				"school_id": teacher.school_id,
			},
			status=202,
		)

	@extend_schema(
		description=(
			"List active temporary lesson unlocks in your school for subjects you teach. "
			"Optionally filter by student_id."
		),
		parameters=[
			OpenApiParameter(
				name="student_id",
				required=False,
				location=OpenApiParameter.QUERY,
				description="Optional student id filter.",
				type=int,
			),
		],
		responses={200: TeacherActiveLessonUnlockSerializer(many=True)},
	)
	@action(detail=False, methods=['get'], url_path='lesson-unlocks')
	def list_active_lesson_unlocks(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher
		now = timezone.now()

		qs = (
			LessonTemporaryUnlock.objects
			.select_related('student__profile', 'lesson__subject')
			.filter(
				revoked_at__isnull=True,
				expires_at__gt=now,
				student__school_id=getattr(teacher, 'school_id', None),
				lesson__subject__teachers=teacher,
			)
			.order_by('expires_at', 'student__profile__name')
		)

		student_id = request.query_params.get('student_id')
		if student_id:
			try:
				qs = qs.filter(student_id=int(student_id))
			except (TypeError, ValueError):
				return Response({"detail": "student_id must be an integer."}, status=400)

		payload = [
			{
				"id": unlock.id,
				"student_id": unlock.student_id,
				"student_name": getattr(getattr(unlock.student, 'profile', None), 'name', None),
				"lesson_id": unlock.lesson_id,
				"lesson_title": getattr(unlock.lesson, 'title', ''),
				"subject_id": unlock.lesson.subject_id,
				"subject_name": getattr(getattr(unlock.lesson, 'subject', None), 'name', None),
				"reason": unlock.reason,
				"expires_at": unlock.expires_at,
				"unlocked_by_id": unlock.unlocked_by_id,
			}
			for unlock in qs
		]
		return Response(payload)

	@extend_schema(
		description=(
			"Temporarily unlock a lesson for either a single student or a whole class (school + grade + subject). "
			"Unlock duration must be between 1 and 72 hours and only applies to subjects you teach. "
			"Provide exactly one of student_id or unlock_whole_class=true."
		),
		request=TeacherLessonUnlockRequestSerializer,
		responses={200: TeacherLessonUnlockResponseSerializer},
	)
	@action(detail=False, methods=['post'], url_path='unlock-lesson')
	def unlock_lesson(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher
		ser = TeacherLessonUnlockRequestSerializer(data=request.data)
		ser.is_valid(raise_exception=True)
		data = ser.validated_data
		lesson = LessonResource.objects.filter(id=data['lesson_id']).select_related('subject').first()
		if not lesson:
			return Response({"detail": "Lesson not found."}, status=404)

		now = timezone.now()
		duration_hours = int(data['duration_hours'])
		expires_at = now + timedelta(hours=duration_hours)
		reason = data.get('reason') or TEACHER_UNLOCK_REASON
		unlock_whole_class = bool(data.get('unlock_whole_class'))

		if unlock_whole_class:
			if not getattr(teacher, 'school_id', None):
				return Response({"detail": "Teacher must be assigned to a school."}, status=403)

			teaches_subject = Subject.objects.filter(id=lesson.subject_id, teachers=teacher).exists()
			if not teaches_subject:
				return Response({"detail": "You can only unlock lessons for subjects you teach."}, status=403)

			lesson_grade = getattr(getattr(lesson, 'subject', None), 'grade', None)
			student_ids = list(
				Student.objects
				.filter(
					school_id=teacher.school_id,
					grade=lesson_grade,
					status=StatusEnum.APPROVED.value,
				)
				.order_by()
				.values_list('id', flat=True)
			)

			target_count = len(student_ids)
			if target_count == 0:
				Activity.objects.create(
					user=request.user,
					type="teacher_unlock_lesson_whole_class",
					description=f"Attempted class unlock for lesson '{lesson.title}' but no students matched.",
					metadata={
						"lesson_id": lesson.id,
						"subject_id": lesson.subject_id,
						"grade": lesson_grade,
						"duration_hours": duration_hours,
						"unlock_whole_class": True,
						"unlocked_count": 0,
					},
				)
				return Response(
					{
						"detail": "No students found for this class.",
						"lesson_id": lesson.id,
						"subject_id": lesson.subject_id,
						"grade": lesson_grade,
						"expires_at": expires_at,
						"unlocked_count": 0,
					},
				)

			existing_active_student_ids = set(
				LessonTemporaryUnlock.objects
				.filter(
					lesson=lesson,
					student_id__in=student_ids,
					revoked_at__isnull=True,
					expires_at__gt=now,
				)
				.order_by()
				.values_list('student_id', flat=True)
				.distinct()
			)
			missing_student_ids = [sid for sid in student_ids if sid not in existing_active_student_ids]

			with transaction.atomic():
				# Extend/update any existing active unlocks for this lesson + class.
				if existing_active_student_ids:
					(
						LessonTemporaryUnlock.objects
						.filter(
							lesson=lesson,
							student_id__in=existing_active_student_ids,
							revoked_at__isnull=True,
							expires_at__gt=now,
						)
						.update(
							unlocked_by=request.user,
							reason=reason,
							expires_at=expires_at,
							revoked_at=None,
						)
					)

				# Create missing unlocks.
				if missing_student_ids:
					LessonTemporaryUnlock.objects.bulk_create(
						[
							LessonTemporaryUnlock(
								student_id=sid,
								lesson=lesson,
								unlocked_by=request.user,
								reason=reason,
								expires_at=expires_at,
							)
							for sid in missing_student_ids
						],
						batch_size=500,
					)

				Activity.objects.create(
					user=request.user,
					type="teacher_unlock_lesson_whole_class",
					description=f"Temporarily unlocked lesson '{lesson.title}' for class.",
					metadata={
						"lesson_id": lesson.id,
						"subject_id": lesson.subject_id,
						"grade": lesson_grade,
						"duration_hours": duration_hours,
						"unlock_whole_class": True,
						"unlocked_count": target_count,
						"created_count": len(missing_student_ids),
						"updated_count": len(existing_active_student_ids),
					},
				)

			# Invalidate per-student progression caches.
			for student_id in student_ids:
				_bump_cache_version(_student_lesson_progress_version_key(student_id))

			return Response(
				{
					"detail": "Lesson unlocked for class.",
					"lesson_id": lesson.id,
					"subject_id": lesson.subject_id,
					"grade": lesson_grade,
					"expires_at": expires_at,
					"unlocked_count": target_count,
				},
			)

		student = Student.objects.filter(id=data['student_id']).select_related('school').first()
		if not student:
			return Response({"detail": "Student not found."}, status=404)

		scope_error = self._validate_unlock_scope(teacher, student, lesson)
		if scope_error:
			return Response({"detail": scope_error}, status=403)

		unlock = (
			LessonTemporaryUnlock.objects
			.filter(
				student=student,
				lesson=lesson,
				revoked_at__isnull=True,
				expires_at__gt=now,
			)
			.order_by('-expires_at')
			.first()
		)
		if unlock is None:
			unlock = LessonTemporaryUnlock.objects.create(
				student=student,
				lesson=lesson,
				unlocked_by=request.user,
				reason=reason,
				expires_at=expires_at,
			)
		else:
			unlock.unlocked_by = request.user
			unlock.reason = reason
			unlock.expires_at = expires_at
			unlock.revoked_at = None
			unlock.save(update_fields=['unlocked_by', 'reason', 'expires_at', 'revoked_at', 'updated_at'])

		_invalidate_student_lesson_cache(student)
		Activity.objects.create(
			user=request.user,
			type="teacher_unlock_lesson",
			description=f"Temporarily unlocked lesson '{lesson.title}' for {getattr(student.profile, 'name', 'student')}",
			metadata={
				"lesson_id": lesson.id,
				"student_id": student.id,
				"duration_hours": duration_hours,
			},
		)

		return Response(self._serialize_unlock(unlock))

	@extend_schema(
		description=(
			"Revoke an active temporary lesson unlock for either a single student or a whole class (school + grade + subject). "
			"Provide exactly one of student_id or unlock_whole_class=true."
		),
		request=TeacherLessonUnlockRevokeSerializer,
		responses={200: TeacherLessonUnlockResponseSerializer},
	)
	@action(detail=False, methods=['post'], url_path='revoke-lesson-unlock')
	def revoke_lesson_unlock(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher
		ser = TeacherLessonUnlockRevokeSerializer(data=request.data)
		ser.is_valid(raise_exception=True)
		data = ser.validated_data
		lesson = LessonResource.objects.filter(id=data['lesson_id']).select_related('subject').first()
		if not lesson:
			return Response({"detail": "Lesson not found."}, status=404)

		now = timezone.now()
		unlock_whole_class = bool(data.get('unlock_whole_class'))
		if unlock_whole_class:
			if not getattr(teacher, 'school_id', None):
				return Response({"detail": "Teacher must be assigned to a school."}, status=403)

			teaches_subject = Subject.objects.filter(id=lesson.subject_id, teachers=teacher).exists()
			if not teaches_subject:
				return Response({"detail": "You can only revoke unlocks for subjects you teach."}, status=403)

			lesson_grade = getattr(getattr(lesson, 'subject', None), 'grade', None)
			student_ids = list(
				Student.objects
				.filter(
					school_id=teacher.school_id,
					grade=lesson_grade,
					status=StatusEnum.APPROVED.value,
				)
				.order_by()
				.values_list('id', flat=True)
			)

			target_count = len(student_ids)
			if target_count == 0:
				Activity.objects.create(
					user=request.user,
					type="teacher_revoke_lesson_unlock_whole_class",
					description=f"Attempted class revoke for lesson '{lesson.title}' but no students matched.",
					metadata={
						"lesson_id": lesson.id,
						"subject_id": lesson.subject_id,
						"grade": lesson_grade,
						"unlock_whole_class": True,
						"revoked_count": 0,
					},
				)
				return Response(
					{
						"detail": "No students found for this class.",
						"lesson_id": lesson.id,
						"subject_id": lesson.subject_id,
						"grade": lesson_grade,
						"revoked_at": now,
						"revoked_count": 0,
					},
				)

			active_unlock_student_ids = list(
				LessonTemporaryUnlock.objects
				.filter(
					lesson=lesson,
					student_id__in=student_ids,
					revoked_at__isnull=True,
					expires_at__gt=now,
				)
				.order_by()
				.values_list('student_id', flat=True)
				.distinct()
			)
			if not active_unlock_student_ids:
				return Response(
					{"detail": "No active unlocks found for this class and lesson."},
					status=404,
				)

			with transaction.atomic():
				(
					LessonTemporaryUnlock.objects
					.filter(
						lesson=lesson,
						student_id__in=active_unlock_student_ids,
						revoked_at__isnull=True,
						expires_at__gt=now,
					)
					.update(revoked_at=now)
				)

				Activity.objects.create(
					user=request.user,
					type="teacher_revoke_lesson_unlock_whole_class",
					description=f"Revoked temporary unlock for lesson '{lesson.title}' for class.",
					metadata={
						"lesson_id": lesson.id,
						"subject_id": lesson.subject_id,
						"grade": lesson_grade,
						"unlock_whole_class": True,
						"revoked_count": len(active_unlock_student_ids),
					},
				)

			# Invalidate per-student progression caches.
			for student_id in active_unlock_student_ids:
				_bump_cache_version(_student_lesson_progress_version_key(student_id))

			return Response(
				{
					"detail": "Lesson unlock revoked for class.",
					"lesson_id": lesson.id,
					"subject_id": lesson.subject_id,
					"grade": lesson_grade,
					"revoked_at": now,
					"revoked_count": len(active_unlock_student_ids),
				},
			)

		student = Student.objects.filter(id=data['student_id']).select_related('school').first()
		if not student:
			return Response({"detail": "Student not found."}, status=404)

		scope_error = self._validate_unlock_scope(teacher, student, lesson)
		if scope_error:
			return Response({"detail": scope_error}, status=403)
		unlock = (
			LessonTemporaryUnlock.objects
			.filter(
				student=student,
				lesson=lesson,
				revoked_at__isnull=True,
				expires_at__gt=now,
			)
			.order_by('-expires_at')
			.first()
		)
		if unlock is None:
			return Response({"detail": "No active unlock found for this student and lesson."}, status=404)

		unlock.revoked_at = now
		unlock.save(update_fields=['revoked_at', 'updated_at'])
		_invalidate_student_lesson_cache(student)
		Activity.objects.create(
			user=request.user,
			type="teacher_revoke_lesson_unlock",
			description=f"Revoked temporary unlock for lesson '{lesson.title}'",
			metadata={"lesson_id": lesson.id, "student_id": student.id},
		)

		return Response(self._serialize_unlock(unlock))

	@extend_schema(
		description="Leaderboard for the teacher's class, derived from grades of subjects assigned to the teacher.",
		parameters=[
			OpenApiParameter(
				name='timeframe',
				required=False,
				location=OpenApiParameter.QUERY,
				description='Leaderboard window: this_week, this_month, or all_time (default).',
				type=str,
			),
			OpenApiParameter(
				name='limit',
				required=False,
				location=OpenApiParameter.QUERY,
				description='Maximum number of students to return. Defaults to 10, max 100.',
				type=int,
			),
		],
		responses={200: LeaderboardResponseSerializer},
	)
	@action(detail=False, methods=['get'], url_path='leaderboard')
	def leaderboard(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		try:
			timeframe = _parse_leaderboard_timeframe(request)
		except ValueError as exc:
			return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

		payload = _build_student_leaderboard_response(
			self._leaderboard_student_queryset(request),
			scope=self._leaderboard_scope(request),
			limit=_parse_leaderboard_limit(request),
			timeframe=timeframe,
		)
		return Response(payload)

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
		parameters=[
			OpenApiParameter(
				name="ai_only",
				required=False,
				location=OpenApiParameter.QUERY,
				description="If set to 1/true, only AI-recommended assessments are returned.",
				type=bool,
			),
			OpenApiParameter(
				name="targeted_only",
				required=False,
				location=OpenApiParameter.QUERY,
				description="If set to 1/true, only targeted assessments are returned.",
				type=bool,
			),
			OpenApiParameter(
				name="student_id",
				required=False,
				location=OpenApiParameter.QUERY,
				description="Filter targeted assessments by target student id.",
				type=int,
			),
		],
	)
	@action(detail=False, methods=['get'], url_path='general-assessments')
	def my_general_assessments(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher
		qs = GeneralAssessment.objects.filter(given_by=teacher).order_by('-created_at')
		ai_only = request.query_params.get('ai_only')
		if ai_only in {'1', 'true', 'True'}:
			qs = qs.filter(ai_recommended=True)
		targeted_only = request.query_params.get('targeted_only')
		if targeted_only in {'1', 'true', 'True'}:
			qs = qs.filter(is_targeted=True)
		student_id = request.query_params.get('student_id')
		if student_id:
			try:
				qs = qs.filter(target_student_id=int(student_id))
			except ValueError:
				qs = qs.none()
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
		parameters=[
			OpenApiParameter(
				name="ai_only",
				required=False,
				location=OpenApiParameter.QUERY,
				description="If set to 1/true, only AI-recommended assessments are returned.",
				type=bool,
			),
			OpenApiParameter(
				name="targeted_only",
				required=False,
				location=OpenApiParameter.QUERY,
				description="If set to 1/true, only targeted assessments are returned.",
				type=bool,
			),
			OpenApiParameter(
				name="student_id",
				required=False,
				location=OpenApiParameter.QUERY,
				description="Filter targeted assessments by target student id.",
				type=int,
			),
		],
	)
	@action(detail=False, methods=['get'], url_path='lesson-assessments')
	def my_lesson_assessments(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher
		qs = LessonAssessment.objects.filter(given_by=teacher).select_related('lesson').order_by('-created_at')
		ai_only = request.query_params.get('ai_only')
		if ai_only in {'1', 'true', 'True'}:
			qs = qs.filter(ai_recommended=True)
		targeted_only = request.query_params.get('targeted_only')
		if targeted_only in {'1', 'true', 'True'}:
			qs = qs.filter(is_targeted=True)
		student_id = request.query_params.get('student_id')
		if student_id:
			try:
				qs = qs.filter(target_student_id=int(student_id))
			except ValueError:
				qs = qs.none()
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
		description=(
			"Use AI to generate targeted quizzes/assignments for a specific student "
			"based on their recent activity. Returns the created assessments."
		),
		parameters=[
			OpenApiParameter(
				name='pk',
				required=True,
				location=OpenApiParameter.PATH,
				type=int,
				description="Student primary key.",
			),
		],
		request=None,
		responses={
			200: OpenApiResponse(
				description="AI-generated targeted assessments for the student.",
				response=GenerateAIAssessmentsResponseSerializer,
			),
		},
	)
	@action(detail=True, methods=['post'], url_path='generate-ai-assessments')
	def generate_ai_assessments(self, request, pk=None):
		"""Generate AI-targeted assessments (quizzes/assignments) for a student.

		The "pk" here refers to the Student's primary key.
		"""
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher = request.user.teacher
		try:
			student = Student.objects.get(pk=pk, school=teacher.school)
		except Student.DoesNotExist:
			return Response({"detail": "Student not found in your school."}, status=404)

		result = generate_targeted_assessments_for_student(student)
		general_ser = GeneralAssessmentSerializer(result.get("general", []), many=True)
		lesson_ser = LessonAssessmentSerializer(result.get("lesson", []), many=True)
		return Response({
			"general_assessments": general_ser.data,
			"lesson_assessments": lesson_ser.data,
		})

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
		temp_password = "password123"

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
			temp_password = "password123"

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


class LookupPagination(StandardResultsSetPagination):
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

	@extend_schema(
		operation_id="admin_bulk_create_counties",
		description=(
			"Bulk create counties from a CSV file. "
			"Required columns: name. Optional: status, moderation_comment."
		),
		request=AdminBulkCountyUploadSerializer,
		responses={
			200: OpenApiResponse(description="Bulk county creation summary with per-row statuses."),
		},
		examples=[
			OpenApiExample(
				name="AdminBulkCountiesResponse",
				summary="Example of bulk CSV upload result.",
				response_only=True,
				value={
					"summary": {"total_rows": 3, "created": 2, "failed": 1},
					"results": [
						{"row": 2, "status": "created", "county_id": 1, "name": "Montserrado"},
						{"row": 3, "status": "created", "county_id": 2, "name": "Bong"},
						{"row": 4, "status": "error", "errors": {"name": ["County with this name already exists."]}},
					],
				},
			),
		],
	)
	@action(detail=False, methods=['post'], url_path='bulk-create')
	def bulk_create(self, request):
		"""Bulk create counties from a CSV upload."""
		from django.db import transaction, IntegrityError

		upload_ser = AdminBulkCountyUploadSerializer(data=request.data)
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

		required_columns = ['name']
		missing = [c for c in required_columns if c not in reader.fieldnames]
		if missing:
			return Response({"detail": f"Missing required columns: {', '.join(missing)}."}, status=status.HTTP_400_BAD_REQUEST)

		valid_statuses: Set[str] = {s.value for s in StatusEnum}
		results = []
		created_count = 0
		failed_count = 0

		for row_index, row in enumerate(reader, start=2):
			row_result = {"row": row_index}
			name = (row.get('name') or '').strip()
			status_raw = (row.get('status') or '').strip()
			moderation_comment = (row.get('moderation_comment') or '').strip()

			if not name:
				results.append({**row_result, "status": "error", "errors": {"name": ["This field is required."]}})
				failed_count += 1
				continue

			county_kwargs = {
				"name": name,
				"moderation_comment": moderation_comment,
				"created_by": request.user,
			}
			if status_raw:
				if status_raw not in valid_statuses:
					results.append({**row_result, "status": "error", "errors": {"status": ["Invalid status value."]}})
					failed_count += 1
					continue
				county_kwargs["status"] = status_raw

			try:
				with transaction.atomic():
					county = County.objects.create(**county_kwargs)
			except IntegrityError:
				results.append({**row_result, "status": "error", "errors": {"name": ["County with this name already exists."]}})
				failed_count += 1
				continue
			except Exception as exc:
				results.append({**row_result, "status": "error", "errors": {"non_field_errors": [str(exc)]}})
				failed_count += 1
				continue

			created_count += 1
			results.append({**row_result, "status": "created", "county_id": county.id, "name": county.name})

		return Response({
			"summary": {"total_rows": len(results), "created": created_count, "failed": failed_count},
			"results": results,
		})

	@extend_schema(
		operation_id="admin_bulk_counties_template",
		description=(
			"Download a sample CSV template for bulk county creation. "
			"The file includes the correct header columns and example rows."
		),
		responses={200: OpenApiResponse(description="CSV file with header row and two sample counties.")},
	)
	@action(detail=False, methods=['get'], url_path='bulk-template')
	def bulk_template(self, request):
		"""Return a CSV template for bulk county creation."""
		header = ["name", "status", "moderation_comment"]
		example_rows = [
			{"name": "Montserrado", "status": "APPROVED", "moderation_comment": "Initial import"},
			{"name": "Bong", "status": "PENDING", "moderation_comment": ""},
		]

		buffer = io.StringIO()
		writer = csv.DictWriter(buffer, fieldnames=header)
		writer.writeheader()
		for row in example_rows:
			writer.writerow(row)

		csv_content = buffer.getvalue()
		response = HttpResponse(csv_content, content_type="text/csv")
		response["Content-Disposition"] = "attachment; filename=counties_bulk_template.csv"
		return response


class AdminDistrictViewSet(viewsets.ModelViewSet):
	queryset = District.objects.select_related('county').all().order_by('name')
	serializer_class = DistrictSerializer
	permission_classes = [permissions.IsAuthenticated, IsAdminRole, permissions.IsAdminUser]

	@extend_schema(
		operation_id="admin_bulk_create_districts",
		description=(
			"Bulk create districts from a CSV file. "
			"Required columns: name and either county_id or county_name. "
			"Optional: status, moderation_comment."
		),
		request=AdminBulkDistrictUploadSerializer,
		responses={200: OpenApiResponse(description="Bulk district creation summary with per-row statuses.")},
		examples=[
			OpenApiExample(
				name="AdminBulkDistrictsResponse",
				summary="Example of bulk CSV upload result.",
				response_only=True,
				value={
					"summary": {"total_rows": 2, "created": 2, "failed": 0},
					"results": [
						{"row": 2, "status": "created", "district_id": 10, "name": "Careysburg", "county": "Montserrado"},
						{"row": 3, "status": "created", "district_id": 11, "name": "Gbarnga", "county": "Bong"},
					],
				},
			),
		],
	)
	@action(detail=False, methods=['post'], url_path='bulk-create')
	def bulk_create(self, request):
		"""Bulk create districts from a CSV upload."""
		from django.db import transaction, IntegrityError

		upload_ser = AdminBulkDistrictUploadSerializer(data=request.data)
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

		required_columns = ['name']
		missing = [c for c in required_columns if c not in reader.fieldnames]
		if missing:
			return Response({"detail": f"Missing required columns: {', '.join(missing)}."}, status=status.HTTP_400_BAD_REQUEST)

		valid_statuses: Set[str] = {s.value for s in StatusEnum}
		results = []
		created_count = 0
		failed_count = 0

		for row_index, row in enumerate(reader, start=2):
			row_result = {"row": row_index}
			name = (row.get('name') or '').strip()
			county_id_raw = (row.get('county_id') or '').strip()
			county_name = (row.get('county_name') or '').strip()
			status_raw = (row.get('status') or '').strip()
			moderation_comment = (row.get('moderation_comment') or '').strip()

			if not name:
				results.append({**row_result, "status": "error", "errors": {"name": ["This field is required."]}})
				failed_count += 1
				continue

			county = None
			if county_id_raw:
				try:
					county_id = int(county_id_raw)
				except ValueError:
					results.append({**row_result, "status": "error", "errors": {"county_id": ["Invalid integer."]}})
					failed_count += 1
					continue
				county = County.objects.filter(id=county_id).first()
			elif county_name:
				county = County.objects.filter(name__iexact=county_name).first()
			else:
				results.append({**row_result, "status": "error", "errors": {"county": ["Provide county_id or county_name."]}})
				failed_count += 1
				continue

			if not county:
				results.append({**row_result, "status": "error", "errors": {"county": ["County not found."]}})
				failed_count += 1
				continue

			district_kwargs = {
				"county": county,
				"name": name,
				"moderation_comment": moderation_comment,
			}
			if status_raw:
				if status_raw not in valid_statuses:
					results.append({**row_result, "status": "error", "errors": {"status": ["Invalid status value."]}})
					failed_count += 1
					continue
				district_kwargs["status"] = status_raw

			try:
				with transaction.atomic():
					district = District.objects.create(**district_kwargs)
			except IntegrityError:
				results.append({**row_result, "status": "error", "errors": {"name": ["District with this name already exists for the county."]}})
				failed_count += 1
				continue
			except Exception as exc:
				results.append({**row_result, "status": "error", "errors": {"non_field_errors": [str(exc)]}})
				failed_count += 1
				continue

			created_count += 1
			results.append({
				**row_result,
				"status": "created",
				"district_id": district.id,
				"name": district.name,
				"county": county.name,
			})

		return Response({
			"summary": {"total_rows": len(results), "created": created_count, "failed": failed_count},
			"results": results,
		})

	@extend_schema(
		operation_id="admin_bulk_districts_template",
		description=(
			"Download a sample CSV template for bulk district creation. "
			"The file includes the correct header columns and example rows."
		),
		responses={200: OpenApiResponse(description="CSV file with header row and two sample districts.")},
	)
	@action(detail=False, methods=['get'], url_path='bulk-template')
	def bulk_template(self, request):
		"""Return a CSV template for bulk district creation."""
		header = ["name", "county_id", "county_name", "status", "moderation_comment"]
		example_rows = [
			{"name": "Careysburg", "county_id": "", "county_name": "Montserrado", "status": "APPROVED", "moderation_comment": "Bulk import"},
			{"name": "Gbarnga", "county_id": "", "county_name": "Bong", "status": "PENDING", "moderation_comment": ""},
		]

		buffer = io.StringIO()
		writer = csv.DictWriter(buffer, fieldnames=header)
		writer.writeheader()
		for row in example_rows:
			writer.writerow(row)

		csv_content = buffer.getvalue()
		response = HttpResponse(csv_content, content_type="text/csv")
		response["Content-Disposition"] = "attachment; filename=districts_bulk_template.csv"
		return response


class AdminSchoolViewSet(viewsets.ModelViewSet):
	queryset = School.objects.select_related('district__county').all().order_by('name')
	serializer_class = SchoolSerializer
	permission_classes = [permissions.IsAuthenticated, IsAdminRole, permissions.IsAdminUser]

	@extend_schema(
		operation_id="admin_bulk_create_schools",
		description=(
			"Bulk create schools from a CSV file. "
			"Required columns: name and either district_id, or district_name plus county_id/county_name. "
			"Optional: status, moderation_comment."
		),
		request=AdminBulkSchoolUploadSerializer,
		responses={200: OpenApiResponse(description="Bulk school creation summary with per-row statuses.")},
		examples=[
			OpenApiExample(
				name="AdminBulkSchoolsResponse",
				summary="Example of bulk CSV upload result.",
				response_only=True,
				value={
					"summary": {"total_rows": 2, "created": 1, "failed": 1},
					"results": [
						{"row": 2, "status": "created", "school_id": 55, "name": "Afrilearn Academy", "district": "Careysburg", "county": "Montserrado"},
						{"row": 3, "status": "error", "errors": {"district": ["District not found."]}},
					],
				},
			),
		],
	)
	@action(detail=False, methods=['post'], url_path='bulk-create')
	def bulk_create(self, request):
		"""Bulk create schools from a CSV upload."""
		from django.db import transaction, IntegrityError

		upload_ser = AdminBulkSchoolUploadSerializer(data=request.data)
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

		required_columns = ['name']
		missing = [c for c in required_columns if c not in reader.fieldnames]
		if missing:
			return Response({"detail": f"Missing required columns: {', '.join(missing)}."}, status=status.HTTP_400_BAD_REQUEST)

		valid_statuses: Set[str] = {s.value for s in StatusEnum}
		results = []
		created_count = 0
		failed_count = 0

		for row_index, row in enumerate(reader, start=2):
			row_result = {"row": row_index}
			name = (row.get('name') or '').strip()
			district_id_raw = (row.get('district_id') or '').strip()
			district_name = (row.get('district_name') or '').strip()
			county_id_raw = (row.get('county_id') or '').strip()
			county_name = (row.get('county_name') or '').strip()
			status_raw = (row.get('status') or '').strip()
			moderation_comment = (row.get('moderation_comment') or '').strip()

			if not name:
				results.append({**row_result, "status": "error", "errors": {"name": ["This field is required."]}})
				failed_count += 1
				continue

			district = None
			if district_id_raw:
				try:
					district_id = int(district_id_raw)
				except ValueError:
					results.append({**row_result, "status": "error", "errors": {"district_id": ["Invalid integer."]}})
					failed_count += 1
					continue
				district = District.objects.select_related('county').filter(id=district_id).first()
			else:
				if not district_name:
					results.append({**row_result, "status": "error", "errors": {"district": ["Provide district_id or district_name."]}})
					failed_count += 1
					continue

				county = None
				if county_id_raw:
					try:
						county_id = int(county_id_raw)
					except ValueError:
						results.append({**row_result, "status": "error", "errors": {"county_id": ["Invalid integer."]}})
						failed_count += 1
						continue
					county = County.objects.filter(id=county_id).first()
				elif county_name:
					county = County.objects.filter(name__iexact=county_name).first()
				else:
					results.append({**row_result, "status": "error", "errors": {"county": ["Provide county_id or county_name when using district_name."]}})
					failed_count += 1
					continue

				if not county:
					results.append({**row_result, "status": "error", "errors": {"county": ["County not found."]}})
					failed_count += 1
					continue

				district = District.objects.select_related('county').filter(county=county, name__iexact=district_name).first()

			if not district:
				results.append({**row_result, "status": "error", "errors": {"district": ["District not found."]}})
				failed_count += 1
				continue

			school_kwargs = {
				"district": district,
				"name": name,
				"moderation_comment": moderation_comment,
			}
			if status_raw:
				if status_raw not in valid_statuses:
					results.append({**row_result, "status": "error", "errors": {"status": ["Invalid status value."]}})
					failed_count += 1
					continue
				school_kwargs["status"] = status_raw

			try:
				with transaction.atomic():
					school = School.objects.create(**school_kwargs)
			except IntegrityError:
				results.append({**row_result, "status": "error", "errors": {"name": ["School with this name already exists for the district."]}})
				failed_count += 1
				continue
			except Exception as exc:
				results.append({**row_result, "status": "error", "errors": {"non_field_errors": [str(exc)]}})
				failed_count += 1
				continue

			created_count += 1
			results.append({
				**row_result,
				"status": "created",
				"school_id": school.id,
				"name": school.name,
				"district": district.name,
				"county": district.county.name,
			})

		return Response({
			"summary": {"total_rows": len(results), "created": created_count, "failed": failed_count},
			"results": results,
		})

	@extend_schema(
		operation_id="admin_bulk_schools_template",
		description=(
			"Download a sample CSV template for bulk school creation. "
			"The file includes the correct header columns and example rows."
		),
		responses={200: OpenApiResponse(description="CSV file with header row and two sample schools.")},
	)
	@action(detail=False, methods=['get'], url_path='bulk-template')
	def bulk_template(self, request):
		"""Return a CSV template for bulk school creation."""
		header = [
			"name",
			"district_id",
			"district_name",
			"county_id",
			"county_name",
			"status",
			"moderation_comment",
		]
		example_rows = [
			{
				"name": "Afrilearn Academy",
				"district_id": "",
				"district_name": "Careysburg",
				"county_id": "",
				"county_name": "Montserrado",
				"status": "APPROVED",
				"moderation_comment": "Bulk import",
			},
			{
				"name": "Gbarnga Public School",
				"district_id": "",
				"district_name": "Gbarnga",
				"county_id": "",
				"county_name": "Bong",
				"status": "PENDING",
				"moderation_comment": "",
			},
		]

		buffer = io.StringIO()
		writer = csv.DictWriter(buffer, fieldnames=header)
		writer.writeheader()
		for row in example_rows:
			writer.writerow(row)

		csv_content = buffer.getvalue()
		response = HttpResponse(csv_content, content_type="text/csv")
		response["Content-Disposition"] = "attachment; filename=schools_bulk_template.csv"
		return response


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

	@extend_schema(
		operation_id="admin_leaderboard",
		description="National student leaderboard with optional county, district, and school filters.",
		parameters=[
			OpenApiParameter(name='timeframe', required=False, location=OpenApiParameter.QUERY, description='Leaderboard window: this_week, this_month, or all_time (default).', type=str),
			OpenApiParameter(name='county_id', required=False, location=OpenApiParameter.QUERY, description='Filter by county id.', type=int),
			OpenApiParameter(name='district_id', required=False, location=OpenApiParameter.QUERY, description='Filter by district id.', type=int),
			OpenApiParameter(name='school_id', required=False, location=OpenApiParameter.QUERY, description='Filter by school id.', type=int),
			OpenApiParameter(name='limit', required=False, location=OpenApiParameter.QUERY, description='Maximum number of students to return. Defaults to 10, max 100.', type=int),
		],
		responses={200: LeaderboardResponseSerializer},
	)
	@action(detail=False, methods=['get'], url_path='leaderboard')
	def leaderboard(self, request):
		def _parse_optional_int(name: str):
			raw_value = request.query_params.get(name)
			if raw_value in {None, ''}:
				return None
			try:
				return int(raw_value)
			except (TypeError, ValueError):
				raise ValueError(f"{name} must be an integer.")

		try:
			county_id = _parse_optional_int('county_id')
			district_id = _parse_optional_int('district_id')
			school_id = _parse_optional_int('school_id')
			timeframe = _parse_leaderboard_timeframe(request)
		except ValueError as exc:
			return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

		qs = Student.objects.filter(status=StatusEnum.APPROVED.value)
		if county_id is not None:
			qs = qs.filter(school__district__county_id=county_id)
		if district_id is not None:
			qs = qs.filter(school__district_id=district_id)
		if school_id is not None:
			qs = qs.filter(school_id=school_id)

		payload = _build_student_leaderboard_response(
			qs,
			scope={
				'kind': 'national',
				'county_id': county_id,
				'district_id': district_id,
				'school_id': school_id,
			},
			limit=_parse_leaderboard_limit(request),
			timeframe=timeframe,
		)
		return Response(payload)


class AdminSystemReportViewSet(viewsets.ViewSet):
	"""Admin System Reports exposed under /admin/system-reports/.

	Currently supports monthly reports; year and month can be provided
	as query parameters (?year=2025&month=12). Defaults to the current
	year and month when not supplied.
	"""

	permission_classes = [permissions.IsAuthenticated, IsAdminRole, permissions.IsAdminUser]
	serializer_class = AdminSystemReportSerializer

	@extend_schema(
		operation_id="admin_system_reports",
		description=(
			"System reports for a given month, including summary cards, a "
			"detailed row, and content/activity statistics."
		),
		parameters=[
			OpenApiParameter(
				name="year",
				required=False,
				location=OpenApiParameter.QUERY,
				description="Report year (e.g. 2025). Defaults to current year.",
				type=int,
			),
			OpenApiParameter(
				name="month",
				required=False,
				location=OpenApiParameter.QUERY,
				description="Report month number (1-12). Defaults to current month.",
				type=int,
			),
		],
		responses={
			200: OpenApiResponse(
				response=AdminSystemReportSerializer,
				examples=[
					OpenApiExample(
						name="SystemReportExample",
						value={
							"period": "2025-12",
							"summary": {
								"total_users": 1250,
								"total_students": 850,
								"total_teachers": 120,
								"total_schools": 84,
							},
							"detailed": [
								{
									"period": "2025-12",
									"users": 1250,
									"students": 850,
									"teachers": 120,
									"parents": 280,
									"schools": 84,
									"subjects": 156,
									"lessons": 892,
									"submissions_total": 1845,
									"submissions_graded": 1520,
								},
							],
							"content_stats": {
								"content_creators": 45,
								"content_validators": 15,
								"approved_subjects": 142,
								"pending_subjects": 14,
								"approved_lessons": 756,
								"pending_lessons": 136,
								"total_games": 45,
							},
							"activity_stats": {
								"new_users": 25,
								"new_students": 18,
								"new_teachers": 3,
								"new_parents": 4,
								"active_users": 980,
								"total_assessments": 234,
								"pending_submissions": 325,
							},
						},
					),
				],
			),
		},
	)
	def list(self, request):
		"""Return monthly system reports for admins (single-period payload)."""
		now = timezone.now()
		try:
			year = int(request.query_params.get("year", now.year))
			month = int(request.query_params.get("month", now.month))
		except ValueError:
			return Response({"detail": "year and month must be integers."}, status=status.HTTP_400_BAD_REQUEST)
		if month < 1 or month > 12:
			return Response({"detail": "month must be between 1 and 12."}, status=status.HTTP_400_BAD_REQUEST)

		# Compute month boundaries (inclusive start, exclusive end)
		tz = timezone.get_current_timezone()
		period_start = timezone.make_aware(datetime(year, month, 1), tz)
		if month == 12:
			period_end = timezone.make_aware(datetime(year + 1, 1, 1), tz)
		else:
			period_end = timezone.make_aware(datetime(year, month + 1, 1), tz)

		period_label = f"{year:04d}-{month:02d}"

		date_filter = {"created_at__gte": period_start, "created_at__lt": period_end}

		# Top summary cards
		users_created = User.objects.filter(**date_filter).count()
		students_created = Student.objects.filter(**date_filter).count()
		teachers_created = Teacher.objects.filter(**date_filter).count()
		schools_created = School.objects.filter(**date_filter).count()

		summary = {
			"total_users": users_created,
			"total_students": students_created,
			"total_teachers": teachers_created,
			"total_schools": schools_created,
		}

		# Detailed row (one row for the selected period)
		parents_created = Parent.objects.filter(**date_filter).count()
		subjects_created = Subject.objects.filter(**date_filter).count()
		lessons_created = LessonResource.objects.filter(**date_filter).count()

		from content.models import AssessmentSolution

		submissions_total = AssessmentSolution.objects.filter(
			submitted_at__gte=period_start,
			submitted_at__lt=period_end,
		).count()
		submissions_graded = AssessmentSolution.objects.filter(
			submitted_at__gte=period_start,
			submitted_at__lt=period_end,
			grade__isnull=False,
		).count()

		detailed = [
			{
				"period": period_label,
				"users": users_created,
				"students": students_created,
				"teachers": teachers_created,
				"parents": parents_created,
				"schools": schools_created,
				"subjects": subjects_created,
				"lessons": lessons_created,
				"submissions_total": submissions_total,
				"submissions_graded": submissions_graded,
			},
		]

		# Content statistics for the period
		content_stats = {
			"content_creators": User.objects.filter(
				role=UserRole.CONTENTCREATOR.value,
				**date_filter,
			).count(),
			"content_validators": User.objects.filter(
				role=UserRole.CONTENTVALIDATOR.value,
				**date_filter,
			).count(),
			"approved_subjects": Subject.objects.filter(
				status=StatusEnum.APPROVED.value,
				**date_filter,
			).count(),
			"pending_subjects": Subject.objects.filter(
				status=StatusEnum.PENDING.value,
				**date_filter,
			).count(),
			"approved_lessons": LessonResource.objects.filter(
				status=StatusEnum.APPROVED.value,
				**date_filter,
			).count(),
			"pending_lessons": LessonResource.objects.filter(
				status=StatusEnum.PENDING.value,
				**date_filter,
			).count(),
			"total_games": GameModel.objects.filter(**date_filter).count(),
		}

		# Activity statistics for the period
		from content.models import GeneralAssessment, LessonAssessment
		from content.models import Activity as ContentActivity

		new_students = students_created
		new_teachers = teachers_created
		new_parents = parents_created

		active_users = User.objects.filter(
			activities__created_at__gte=period_start,
			activities__created_at__lt=period_end,
		).distinct().count()

		total_assessments = (
			GeneralAssessment.objects.filter(**date_filter).count()
			+ LessonAssessment.objects.filter(**date_filter).count()
		)

		pending_submissions = AssessmentSolution.objects.filter(
			submitted_at__gte=period_start,
			submitted_at__lt=period_end,
			grade__isnull=True,
		).count()

		activity_stats = {
			"new_users": users_created,
			"new_students": new_students,
			"new_teachers": new_teachers,
			"new_parents": new_parents,
			"active_users": active_users,
			"total_assessments": total_assessments,
			"pending_submissions": pending_submissions,
		}

		payload = {
			"period": period_label,
			"summary": summary,
			"detailed": detailed,
			"content_stats": content_stats,
			"activity_stats": activity_stats,
		}
		ser = AdminSystemReportSerializer(payload)
		return Response(ser.data)

class AdminStudentViewSet(viewsets.ReadOnlyModelViewSet):
	"""Admin-only read and moderation access to all students."""

	queryset = Student.objects.select_related('profile', 'school').prefetch_related('guardians__profile').all().order_by('profile__name')
	serializer_class = AdminStudentListSerializer
	permission_classes = [permissions.IsAuthenticated, IsAdminRole, permissions.IsAdminUser]
	filter_backends = [filters.SearchFilter, filters.OrderingFilter]
	search_fields = ['profile__name', 'profile__email', 'school__name', 'grade', 'status']
	ordering_fields = ['profile__name', 'school__name', 'grade', 'status', 'created_at']

	@extend_schema(
		operation_id="admin_approve_student",
		description="Approve a pending student account as an admin.",
		request=None,
		responses={200: StudentSerializer},
	)
	@action(detail=True, methods=['post'], url_path='approve')
	def approve(self, request, pk=None):
		"""Approve a student and notify them via SMS/email."""
		student = self.get_object()
		student.status = StatusEnum.APPROVED.value
		student.moderation_comment = "Approved by admin"
		student.save(update_fields=['status', 'moderation_comment', 'updated_at'])

		profile = student.profile
		message = (
			f"Hi {profile.name}, your Liberia eLearn student account has been approved by an administrator.\n"
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
		operation_id="admin_reject_student",
		description="Reject a pending student account as an admin.",
		request=None,
		responses={200: StudentSerializer},
	)
	@action(detail=True, methods=['post'], url_path='reject')
	def reject(self, request, pk=None):
		"""Reject a student and notify them via SMS/email."""
		student = self.get_object()
		moderation_comment = request.data.get('moderation_comment') or "Rejected by admin"
		student.status = StatusEnum.REJECTED.value
		student.moderation_comment = moderation_comment
		student.save(update_fields=['status', 'moderation_comment', 'updated_at'])

		profile = student.profile
		message = (
			f"Hi {profile.name}, your Liberia eLearn student account has been rejected by an administrator.\n"
			"Please contact your school or the Liberia eLearn support team for more information."
		)
		fire_and_forget(
			_send_account_notifications,
			message,
			getattr(profile, "phone", None),
			getattr(profile, "email", None),
			"Your Liberia eLearn student account status",
		)
		return Response(StudentSerializer(student).data)


class AdminTeacherViewSet(viewsets.ReadOnlyModelViewSet):
	"""Admin-only read and moderation access to all teachers."""

	queryset = Teacher.objects.select_related('profile', 'school').all().order_by('profile__name')
	serializer_class = TeacherSerializer
	permission_classes = [permissions.IsAuthenticated, IsAdminRole, permissions.IsAdminUser]
	filter_backends = [filters.SearchFilter, filters.OrderingFilter]
	search_fields = ['profile__name', 'profile__email', 'school__name', 'status']
	ordering_fields = ['profile__name', 'school__name', 'status', 'created_at']

	@extend_schema(
		operation_id="admin_approve_teacher",
		description="Approve a pending teacher account as an admin.",
		request=None,
		responses={200: TeacherSerializer},
	)
	@action(detail=True, methods=['post'], url_path='approve')
	def approve(self, request, pk=None):
		"""Approve a teacher and notify them via SMS/email."""
		teacher = self.get_object()
		teacher.status = StatusEnum.APPROVED.value
		teacher.moderation_comment = "Approved by admin"
		teacher.save(update_fields=['status', 'moderation_comment', 'updated_at'])

		profile = teacher.profile
		message = (
			f"Hi {profile.name}, your Liberia eLearn teacher account has been approved by an administrator.\n"
			"You can now log in and start teaching."
		)
		fire_and_forget(
			_send_account_notifications,
			message,
			getattr(profile, "phone", None),
			getattr(profile, "email", None),
			"Your Liberia eLearn teacher account has been approved",
		)
		return Response(TeacherSerializer(teacher).data)

	@extend_schema(
		operation_id="admin_reject_teacher",
		description="Reject a pending teacher account as an admin.",
		request=None,
		responses={200: TeacherSerializer},
	)
	@action(detail=True, methods=['post'], url_path='reject')
	def reject(self, request, pk=None):
		"""Reject a teacher and notify them via SMS/email."""
		teacher = self.get_object()
		moderation_comment = request.data.get('moderation_comment') or "Rejected by admin"
		teacher.status = StatusEnum.REJECTED.value
		teacher.moderation_comment = moderation_comment
		teacher.save(update_fields=['status', 'moderation_comment', 'updated_at'])

		profile = teacher.profile
		message = (
			f"Hi {profile.name}, your Liberia eLearn teacher account has been rejected by an administrator.\n"
			"Please contact your school or the Liberia eLearn support team for more information."
		)
		fire_and_forget(
			_send_account_notifications,
			message,
			getattr(profile, "phone", None),
			getattr(profile, "email", None),
			"Your Liberia eLearn teacher account status",
		)
		return Response(TeacherSerializer(teacher).data)

	class _MakeHeadmasterSerializer(serializers.Serializer):
		teacher_id = serializers.IntegerField(required=True)

	@extend_schema(
		operation_id="makeheadmaster",
		description=(
			"Promote a teacher to headteacher (HEADTEACHER role). "
			"Restricted to admins and content validators."
		),
		request=_MakeHeadmasterSerializer,
		responses={200: TeacherSerializer},
	)
	@action(
		detail=False,
		methods=['post'],
		url_path='makeheadmaster',
		permission_classes=[permissions.IsAuthenticated, CanModerateContent],
	)
	def makeheadmaster(self, request):
		"""Assign a teacher as a headteacher using the teacher's numeric id."""
		serializer = self._MakeHeadmasterSerializer(data=request.data)
		serializer.is_valid(raise_exception=True)
		teacher_id = serializer.validated_data['teacher_id']

		# Lock row for safe concurrent updates.
		try:
			teacher = (
				Teacher.objects.select_related('profile', 'school')
				.select_for_update()
				.get(pk=teacher_id)
			)
		except Teacher.DoesNotExist:
			raise serializers.ValidationError({"teacher_id": "Teacher not found."})

		# Promote linked user role. Keep Teacher record as-is.
		profile = teacher.profile
		if getattr(profile, 'role', None) != UserRole.HEADTEACHER.value:
			profile.role = UserRole.HEADTEACHER.value
			profile.save(update_fields=['role', 'updated_at'])

		return Response(TeacherSerializer(teacher).data, status=status.HTTP_200_OK)


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
		temp_password = "password123"

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
		examples=[
			OpenApiExample(
				name="AdminBulkContentManagersResponse",
				summary="Example of bulk CSV upload result.",
				response_only=True,
				value={
					"summary": {
						"total_rows": 3,
						"created": 2,
						"failed": 1,
					},
					"results": [
						{
							"row": 2,
							"status": "created",
							"user_id": 101,
							"name": "Jane Creator",
							"phone": "231770000010",
							"email": "jane.creator@example.com",
							"role": "creator",
						},
						{
							"row": 3,
							"status": "created",
							"user_id": 102,
							"name": "John Validator",
							"phone": "231770000011",
							"email": "john.validator@example.com",
							"role": "validator",
						},
						{
							"row": 4,
							"status": "error",
							"errors": {
								"phone": ["A user with this phone already exists."],
							},
						},
					],
				},
			),
		],
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
			temp_password = "password123"

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

