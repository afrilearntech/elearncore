from typing import Iterable, Dict, List, Set

from rest_framework import permissions, viewsets, status, filters, serializers
from drf_spectacular.utils import extend_schema, OpenApiResponse, OpenApiExample
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
	GameModel, Activity, AssessmentSolution, GamePlay,
)
from forum.models import Chat
from django.core.cache import cache
from content.serializers import (
	AssessmentSolutionSerializer, SubjectSerializer, TopicSerializer, PeriodSerializer, LessonResourceSerializer, TakeLessonSerializer,
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

		# Log login activity
		Activity.objects.create(
			user=user,
			type="login",
			description="User logged in",
			metadata={"role": user.role},
		)
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

