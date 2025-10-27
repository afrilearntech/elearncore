from typing import Iterable, Dict, List

from rest_framework import permissions, viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from django.views.decorators.cache import cache_page
from django.utils.decorators import method_decorator
from django.utils.dateparse import parse_date
from django.utils import timezone
from datetime import timedelta
from django.db import models
from django.db.models import Q
from django.db.models.functions import TruncDate

from elearncore.sysutils.constants import UserRole, Status as StatusEnum

from content.models import (
	Subject, Topic, Period, LessonResource, TakeLesson, LessonAssessment,
	GeneralAssessment, GeneralAssessmentGrade, LessonAssessmentGrade,
)
from forum.models import Chat
from django.core.cache import cache
from content.serializers import (
	SubjectSerializer, TopicSerializer, PeriodSerializer, LessonResourceSerializer, TakeLessonSerializer,
)
from agentic.models import AIRecommendation, AIAbuseReport
from agentic.serializers import AIRecommendationSerializer, AIAbuseReportSerializer
from knox.models import AuthToken
from accounts.models import User, Student, Teacher, Parent, School, County, District
from accounts.serializers import SchoolLookupSerializer, CountyLookupSerializer, DistrictLookupSerializer


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

	# Cache list & retrieve for short periods (safe/public reads)
	@method_decorator(cache_page(60 * 5), name='list')
	@method_decorator(cache_page(60 * 10), name='retrieve')
	def dispatch(self, *args, **kwargs):
		return super().dispatch(*args, **kwargs)


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


class PeriodViewSet(viewsets.ModelViewSet):
	queryset = Period.objects.all()
	serializer_class = PeriodSerializer
	filter_backends = [filters.SearchFilter, filters.OrderingFilter]
	search_fields = ['name']
	ordering_fields = ['start_month', 'end_month', 'created_at']

	@method_decorator(cache_page(60 * 15), name='list')
	@method_decorator(cache_page(60 * 15), name='retrieve')
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


class OnboardingViewSet(viewsets.ViewSet):
	"""Endpoints to onboard users step-by-step.
	- profilesetup: create user and return token
	- role: set role and create associated profile
	- aboutyou: set personal details and optional institution/grade
	- linkchild: link a student to a parent profile
	"""

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

	@action(detail=False, methods=['post'], permission_classes=[permissions.IsAuthenticated])
	def aboutuser(self, request):
		user: User = request.user
		dob_raw = request.data.get('dob')
		gender = request.data.get('gender')
		# Backward compat: accept either 'school_name' or legacy 'institution_name'; prefer explicit 'school_id' when available
		school_name = request.data.get('school_name') or request.data.get('institution_name')
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


class DashboardViewSet(viewsets.ViewSet):
	permission_classes = [permissions.IsAuthenticated]

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

		data = {
			'assignments_due_this_week': assignments_due_this_week,
			'quick_stats': {
				'total_courses': total_courses,
				'completed_courses': completed_courses,
				'in_progress_courses': in_progress_courses,
			},
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

