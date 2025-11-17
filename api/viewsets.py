from typing import Iterable, Dict, List, Set

from rest_framework import permissions, viewsets, status, filters, serializers
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework.decorators import action
from rest_framework.response import Response
from django.views.decorators.cache import cache_page
from django.utils.decorators import method_decorator
from django.utils.dateparse import parse_date
from django.utils import timezone
from datetime import timedelta
from django.db import models
from django.db.models import Q, Count, Window, F
from django.db.models.functions import TruncDate, DenseRank

from elearncore.sysutils.constants import UserRole, Status as StatusEnum

from content.models import (
	Subject, Topic, Period, LessonResource, TakeLesson, LessonAssessment,
	GeneralAssessment, GeneralAssessmentGrade, LessonAssessmentGrade,
	GameModel,
)
from forum.models import Chat
from django.core.cache import cache
from content.serializers import (
	SubjectSerializer, TopicSerializer, PeriodSerializer, LessonResourceSerializer, TakeLessonSerializer,
	GameSerializer,
)
from agentic.models import AIRecommendation, AIAbuseReport
from agentic.serializers import AIRecommendationSerializer, AIAbuseReportSerializer
from knox.models import AuthToken
from accounts.models import User, Student, Teacher, Parent, School, County, District
from accounts.serializers import (
	SchoolLookupSerializer, CountyLookupSerializer, DistrictLookupSerializer,
	CountySerializer, DistrictSerializer, SchoolSerializer,
)
from .serializers import ProfileSetupSerializer, UserRoleSerializer, AboutUserSerializer, LinkChildSerializer, LoginSerializer


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
		print(f'Instance: {instance}')
		data = self.get_serializer(instance).data

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
		serializer.save(created_by=self.request.user)


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
		token = AuthToken.objects.create(user)[1]
		return Response({
			"token": token,
			"user": {"id": user.id, "name": user.name, "phone": user.phone, "email": user.email, "role": user.role},
		}, status=201)

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
			"user": {"id": user.id, "name": user.name, "phone": user.phone, "email": user.email, "role": user.role},
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

		# Recent activities: combine last items from lessons taken, grades, and forum chats
		recent: List[dict] = []
		# Last taken lessons
		for tl in taken_qs.select_related('lesson__subject').order_by('-created_at')[:5]:
			recent.append({
				'type': 'lesson',
				'label': f"Completed {tl.lesson.title}",
				'course': tl.lesson.subject.name,
				'created_at': tl.created_at.isoformat(),
			})
		# Last grades
		for g in LessonAssessmentGrade.objects.select_related('lesson_assessment__lesson').filter(student=student).order_by('-created_at')[:5]:
			recent.append({
				'type': 'grade',
				'label': f"Scored {g.score} in {g.lesson_assessment.title}",
				'created_at': g.created_at.isoformat(),
			})
		for g in GeneralAssessmentGrade.objects.select_related('assessment').filter(student=student).order_by('-created_at')[:5]:
			recent.append({
				'type': 'grade',
				'label': f"Scored {g.score} in {g.assessment.title}",
				'created_at': g.created_at.isoformat(),
			})
		# Forum chats in forums where the student is a member
		forum_chats = Chat.objects.select_related('forum', 'sender').filter(forum__memberships__student=student).order_by('-created_at')[:5]
		for ch in forum_chats:
			recent.append({
				'type': 'chat',
				'label': f"{ch.sender.name}: {ch.content[:40]}",
				'forum': ch.forum.name,
				'created_at': ch.created_at.isoformat(),
			})
		recent.sort(key=lambda x: x.get('created_at', ''), reverse=True)
		recent = recent[:10]

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

