import csv
import io
from datetime import timedelta
from typing import Dict, List

from django.utils import timezone
from django.db.models import Count
from drf_spectacular.utils import extend_schema, OpenApiResponse, OpenApiParameter
from rest_framework import permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response

from elearncore.sysutils.constants import UserRole, Status as StatusEnum
from elearncore.sysutils.tasks import fire_and_forget

from accounts.models import Student, Teacher, User
from accounts.serializers import StudentSerializer, TeacherSerializer
from content.models import (
	AssessmentSolution,
	GeneralAssessment,
	GeneralAssessmentGrade,
	LessonAssessment,
	LessonAssessmentGrade,
	LessonResource,
	Question,
	Story,
	Subject,
	Topic,
)
from content.serializers import (
	GeneralAssessmentSerializer,
	LessonAssessmentSerializer,
	LessonResourceSerializer,
	QuestionCreateSerializer,
	QuestionSerializer,
	StoryDetailSerializer,
	StoryListSerializer,
	StoryPublishRequestSerializer,
	SubjectSerializer,
	TopicSerializer,
)
from agentic.services import generate_targeted_assessments_for_student

from .serializers import (
	ContentBulkTeacherUploadSerializer,
	ContentCreateTeacherSerializer,
	GradeAssessmentSerializer,
)
from .viewsets import (
	LeaderboardResponseSerializer,
	ParentSubmissionsResponseSerializer,
	TeacherDashboardResponseSerializer,
	TeacherGradesResponseSerializer,
	TeacherViewSet,
	_build_student_leaderboard_response,
	_parse_leaderboard_limit,
	_parse_bulk_date,
	_send_account_notifications,
)


class HeadTeacherViewSet(TeacherViewSet):
	"""School-scoped teacher endpoints for HEADTEACHER users.

	This mirrors teacher endpoints but data visibility is expanded from
	"owned by me" to "owned by any teacher in my school".
	"""
	permission_classes = [permissions.IsAuthenticated]

	def _require_teacher(self, request):
		user: User = request.user
		if not user or getattr(user, 'role', None) not in {UserRole.HEADTEACHER.value, UserRole.ADMIN.value}:
			return Response({"detail": "Head teacher role required."}, status=403)
		if not hasattr(user, 'teacher'):
			return Response({"detail": "Teacher profile required."}, status=403)
		if not getattr(user.teacher, 'school_id', None):
			return Response({"detail": "Head teacher must be assigned to a school."}, status=403)
		return None

	def _school_teacher_ids(self, request) -> List[int]:
		teacher = request.user.teacher
		if not teacher.school_id:
			return []
		return list(Teacher.objects.filter(school_id=teacher.school_id).values_list('id', flat=True))

	def _leaderboard_student_queryset(self, request):
		school_id = request.user.teacher.school_id
		return Student.objects.filter(
			school_id=school_id,
			status=StatusEnum.APPROVED.value,
		)

	def _leaderboard_scope(self, request) -> dict:
		school = getattr(request.user.teacher, 'school', None)
		return {
			'kind': 'school',
			'school_id': getattr(school, 'id', None),
			'school_name': getattr(school, 'name', None),
		}

	@extend_schema(
		description="List all stories in the head teacher's school, including unpublished stories.",
		parameters=[
			OpenApiParameter(name='grade', required=False, location=OpenApiParameter.QUERY, type=str),
			OpenApiParameter(name='tag', required=False, location=OpenApiParameter.QUERY, type=str),
			OpenApiParameter(name='is_published', required=False, location=OpenApiParameter.QUERY, type=bool),
		],
		responses={200: StoryListSerializer(many=True)},
	)
	@action(detail=False, methods=['get'], url_path='stories')
	def stories(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny

		school_id = request.user.teacher.school_id
		qs = Story.objects.filter(school_id=school_id).select_related('school', 'created_by').order_by('-created_at')

		grade = (request.query_params.get('grade') or '').strip()
		if grade:
			qs = qs.filter(grade=grade)

		tag = (request.query_params.get('tag') or '').strip()
		if tag:
			qs = qs.filter(tag__iexact=tag)

		is_published = request.query_params.get('is_published')
		if is_published in {'1', 'true', 'True'}:
			qs = qs.filter(is_published=True)
		elif is_published in {'0', 'false', 'False'}:
			qs = qs.filter(is_published=False)

		return Response(StoryListSerializer(qs, many=True).data)

	@extend_schema(
		description="Read a single story in the head teacher's school scope.",
		parameters=[OpenApiParameter(name='pk', required=True, location=OpenApiParameter.PATH, type=int)],
		responses={200: StoryDetailSerializer},
	)
	@action(detail=False, methods=['get'], url_path='stories/(?P<pk>[^/.]+)')
	def story_detail(self, request, pk=None):
		deny = self._require_teacher(request)
		if deny:
			return deny

		try:
			story = Story.objects.select_related('school', 'created_by').get(pk=pk, school_id=request.user.teacher.school_id)
		except Story.DoesNotExist:
			return Response({"detail": "Story not found."}, status=404)
		return Response(StoryDetailSerializer(story).data)

	@extend_schema(
		description="Publish school stories. Headteacher publish is the final approval step.",
		request=StoryPublishRequestSerializer,
		responses={200: OpenApiResponse(description="Stories published.")},
	)
	@action(detail=False, methods=['post'], url_path='stories/publish')
	def publish_stories(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny

		ser = StoryPublishRequestSerializer(data=request.data)
		ser.is_valid(raise_exception=True)
		story_ids = ser.validated_data['story_ids']
		school_id = request.user.teacher.school_id

		stories = list(Story.objects.filter(id__in=story_ids, school_id=school_id))
		if len(stories) != len(set(story_ids)):
			return Response({"detail": "One or more stories are outside your school scope."}, status=403)

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
		description="List all teachers in the head teacher's school.",
		responses={200: TeacherSerializer(many=True)},
	)
	@action(detail=False, methods=['get'], url_path='teachers')
	def my_teachers(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		school_id = request.user.teacher.school_id
		qs = Teacher.objects.filter(school_id=school_id).select_related('profile', 'school').order_by('profile__name')
		return Response(TeacherSerializer(qs, many=True).data)

	@extend_schema(
		description="Create a teacher account in the head teacher's school.",
		request=ContentCreateTeacherSerializer,
		responses={201: TeacherSerializer},
	)
	@action(detail=False, methods=['post'], url_path='teachers/create')
	def create_teacher(self, request):
		from django.db import transaction

		deny = self._require_teacher(request)
		if deny:
			return deny

		head_teacher = request.user.teacher
		school = head_teacher.school
		if school is None:
			return Response({"detail": "No school context available."}, status=status.HTTP_400_BAD_REQUEST)

		payload = dict(request.data)
		payload['school_id'] = school.id
		ser = ContentCreateTeacherSerializer(data=payload)
		ser.is_valid(raise_exception=True)
		data = ser.validated_data

		name = data['name'].strip()
		phone = data['phone'].strip()
		email = (data.get('email') or '').strip() or None
		gender = (data.get('gender') or '').strip() or None
		dob = data.get('dob')
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
			teacher = Teacher.objects.create(
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
		description="Bulk create teacher accounts in the head teacher's school from CSV.",
		request=ContentBulkTeacherUploadSerializer,
		responses={200: OpenApiResponse(description="Bulk teacher creation summary with per-row statuses.")},
	)
	@action(detail=False, methods=['post'], url_path='teachers/bulk-create')
	def bulk_create_teachers(self, request):
		from django.db import transaction
		from rest_framework.exceptions import ValidationError

		deny = self._require_teacher(request)
		if deny:
			return deny
		head_teacher = request.user.teacher
		if not head_teacher.school_id:
			return Response({"detail": "No school context available."}, status=status.HTTP_400_BAD_REQUEST)

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

		required_columns = ['name', 'phone']
		missing = [c for c in required_columns if c not in reader.fieldnames]
		if missing:
			return Response({"detail": f"Missing required columns: {', '.join(missing)}."}, status=status.HTTP_400_BAD_REQUEST)

		results = []
		created_count = 0
		failed_count = 0

		for row_index, row in enumerate(reader, start=2):
			row_result = {'row': row_index}
			mapped = {
				'name': (row.get('name') or '').strip(),
				'phone': (row.get('phone') or '').strip(),
				'email': (row.get('email') or '').strip() or None,
				'gender': (row.get('gender') or '').strip() or None,
				'dob': _parse_bulk_date(row.get('dob')),
				'school_id': head_teacher.school_id,
			}

			ser = ContentCreateTeacherSerializer(data=mapped)
			try:
				ser.is_valid(raise_exception=True)
			except ValidationError as exc:
				results.append({**row_result, 'status': 'error', 'errors': exc.detail})
				failed_count += 1
				continue

			data = ser.validated_data
			name = data['name'].strip()
			phone = data['phone'].strip()
			email = (data.get('email') or '').strip() or None
			gender = (data.get('gender') or '').strip() or None
			dob = data.get('dob')
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
					teacher = Teacher.objects.create(
						profile=user,
						school_id=head_teacher.school_id,
						status=StatusEnum.APPROVED.value,
					)
			except Exception as exc:
				results.append({**row_result, 'status': 'error', 'errors': {'non_field_errors': [str(exc)]}})
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
				'status': 'created',
				'teacher_db_id': teacher.id,
				'teacher_id': teacher.teacher_id,
				'name': name,
				'phone': phone,
			})

		return Response({
			'summary': {'total_rows': len(results), 'created': created_count, 'failed': failed_count},
			'results': results,
		})

	@extend_schema(description="List subjects taught by any teacher in the head teacher's school.", responses={200: SubjectSerializer(many=True)})
	@action(detail=False, methods=['get'], url_path='subjects')
	def my_subjects(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		school_id = request.user.teacher.school_id
		qs = Subject.objects.filter(teachers__school_id=school_id).distinct().order_by('name')
		return Response(SubjectSerializer(qs, many=True, context={"request": request}).data)

	@extend_schema(description="List topics for subjects taught in the head teacher's school.", responses={200: TopicSerializer(many=True)})
	@action(detail=False, methods=['get'], url_path='topics')
	def my_topics(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		school_id = request.user.teacher.school_id
		qs = Topic.objects.filter(subject__teachers__school_id=school_id).select_related('subject').distinct().order_by('subject__name', 'name')
		return Response(TopicSerializer(qs, many=True).data)

	@extend_schema(description="List lessons for subjects taught in the head teacher's school.", responses={200: LessonResourceSerializer(many=True)})
	@action(detail=False, methods=['get'], url_path='lessons')
	def my_lessons(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		school_id = request.user.teacher.school_id
		qs = LessonResource.objects.filter(subject__teachers__school_id=school_id).select_related('subject').distinct().order_by('-created_at')
		return Response(LessonResourceSerializer(qs, many=True, context={"request": request}).data)

	@extend_schema(
		description="List general assessments created by teachers in the head teacher's school.",
		responses={200: GeneralAssessmentSerializer(many=True)},
		parameters=[
			OpenApiParameter(name='ai_only', required=False, location=OpenApiParameter.QUERY, type=bool),
			OpenApiParameter(name='targeted_only', required=False, location=OpenApiParameter.QUERY, type=bool),
			OpenApiParameter(name='student_id', required=False, location=OpenApiParameter.QUERY, type=int),
		],
	)
	@action(detail=False, methods=['get'], url_path='general-assessments')
	def my_general_assessments(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher_ids = self._school_teacher_ids(request)
		qs = GeneralAssessment.objects.filter(given_by_id__in=teacher_ids).order_by('-created_at')
		if request.query_params.get('ai_only') in {'1', 'true', 'True'}:
			qs = qs.filter(ai_recommended=True)
		if request.query_params.get('targeted_only') in {'1', 'true', 'True'}:
			qs = qs.filter(is_targeted=True)
		student_id = request.query_params.get('student_id')
		if student_id:
			try:
				qs = qs.filter(target_student_id=int(student_id))
			except ValueError:
				qs = qs.none()
		return Response(GeneralAssessmentSerializer(qs, many=True).data)

	@extend_schema(
		description="List lesson assessments created by teachers in the head teacher's school.",
		responses={200: LessonAssessmentSerializer(many=True)},
		parameters=[
			OpenApiParameter(name='ai_only', required=False, location=OpenApiParameter.QUERY, type=bool),
			OpenApiParameter(name='targeted_only', required=False, location=OpenApiParameter.QUERY, type=bool),
			OpenApiParameter(name='student_id', required=False, location=OpenApiParameter.QUERY, type=int),
		],
	)
	@action(detail=False, methods=['get'], url_path='lesson-assessments')
	def my_lesson_assessments(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher_ids = self._school_teacher_ids(request)
		qs = LessonAssessment.objects.filter(given_by_id__in=teacher_ids).select_related('lesson').order_by('-created_at')
		if request.query_params.get('ai_only') in {'1', 'true', 'True'}:
			qs = qs.filter(ai_recommended=True)
		if request.query_params.get('targeted_only') in {'1', 'true', 'True'}:
			qs = qs.filter(is_targeted=True)
		student_id = request.query_params.get('student_id')
		if student_id:
			try:
				qs = qs.filter(target_student_id=int(student_id))
			except ValueError:
				qs = qs.none()
		return Response(LessonAssessmentSerializer(qs, many=True).data)

	@extend_schema(description="Create a question for assessments owned by teachers in the head teacher's school.", request=QuestionCreateSerializer, responses={201: QuestionSerializer})
	@action(detail=False, methods=['post'], url_path='questions/create')
	def create_question(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher_ids = set(self._school_teacher_ids(request))
		ser = QuestionCreateSerializer(data=request.data, context={'request': request, 'restrict_to_teacher': False})
		ser.is_valid(raise_exception=True)
		ga = ser.validated_data.get('general_assessment')
		la = ser.validated_data.get('lesson_assessment')
		if ga is not None and ga.given_by_id not in teacher_ids:
			return Response({'detail': 'You can only add questions to assessments from your school.'}, status=status.HTTP_403_FORBIDDEN)
		if la is not None and la.given_by_id not in teacher_ids:
			return Response({'detail': 'You can only add questions to assessments from your school.'}, status=status.HTTP_403_FORBIDDEN)
		question = ser.save()
		return Response(QuestionSerializer(question).data, status=status.HTTP_201_CREATED)

	@extend_schema(
		description="List questions for an assessment created by teachers in the head teacher's school.",
		parameters=[
			OpenApiParameter(name='general_assessment_id', required=False, location=OpenApiParameter.QUERY, type=int),
			OpenApiParameter(name='lesson_assessment_id', required=False, location=OpenApiParameter.QUERY, type=int),
		],
		responses={200: QuestionSerializer(many=True)},
	)
	@action(detail=False, methods=['get'], url_path='questions')
	def list_questions(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher_ids = self._school_teacher_ids(request)
		ga_id = request.query_params.get('general_assessment_id')
		la_id = request.query_params.get('lesson_assessment_id')
		if bool(ga_id) == bool(la_id):
			return Response({'detail': 'Provide exactly one of general_assessment_id or lesson_assessment_id.'}, status=status.HTTP_400_BAD_REQUEST)
		qs = Question.objects.all().prefetch_related('options')
		if ga_id:
			try:
				ga_id_int = int(ga_id)
			except ValueError:
				return Response({'detail': 'general_assessment_id must be an integer.'}, status=status.HTTP_400_BAD_REQUEST)
			qs = qs.filter(general_assessment_id=ga_id_int, general_assessment__given_by_id__in=teacher_ids)
		else:
			try:
				la_id_int = int(la_id)
			except ValueError:
				return Response({'detail': 'lesson_assessment_id must be an integer.'}, status=status.HTTP_400_BAD_REQUEST)
			qs = qs.filter(lesson_assessment_id=la_id_int, lesson_assessment__given_by_id__in=teacher_ids)
		return Response(QuestionSerializer(qs.order_by('created_at'), many=True).data)

	@extend_schema(description="School dashboard for head teachers.", responses={200: TeacherDashboardResponseSerializer})
	@action(detail=False, methods=['get'], url_path='dashboard')
	def dashboard(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		school_id = request.user.teacher.school_id
		teacher_ids = self._school_teacher_ids(request)

		students_qs = Student.objects.filter(school_id=school_id, status=StatusEnum.APPROVED.value)
		student_ids = list(students_qs.values_list('id', flat=True))
		total_students = len(student_ids)

		per_student_scores: Dict[int, List[float]] = {}
		recent_scores: Dict[int, List[float]] = {}
		past_scores: Dict[int, List[float]] = {}
		all_scores: List[float] = []
		cutoff = timezone.now() - timedelta(days=30)

		lesson_grades = LessonAssessmentGrade.objects.select_related('lesson_assessment__lesson__subject', 'student__profile').filter(lesson_assessment__given_by_id__in=teacher_ids, student_id__in=student_ids)
		for g in lesson_grades:
			if g.score is None:
				continue
			score = float(g.score)
			sid = g.student_id
			per_student_scores.setdefault(sid, []).append(score)
			all_scores.append(score)
			ts = getattr(g, 'updated_at', getattr(g, 'created_at', None)) or timezone.now()
			bucket = recent_scores if ts >= cutoff else past_scores
			bucket.setdefault(sid, []).append(score)

		general_grades = GeneralAssessmentGrade.objects.select_related('assessment', 'student__profile').filter(assessment__given_by_id__in=teacher_ids, student_id__in=student_ids)
		for g in general_grades:
			if g.score is None:
				continue
			score = float(g.score)
			sid = g.student_id
			per_student_scores.setdefault(sid, []).append(score)
			all_scores.append(score)
			ts = getattr(g, 'updated_at', getattr(g, 'created_at', None)) or timezone.now()
			bucket = recent_scores if ts >= cutoff else past_scores
			bucket.setdefault(sid, []).append(score)

		class_average = sum(all_scores) / len(all_scores) if all_scores else 0.0
		student_by_id = {s.id: s for s in students_qs.select_related('profile')}
		top_performers = []
		for sid, scores in per_student_scores.items():
			if not scores:
				continue
			avg_score = sum(scores) / len(scores)
			recent_list = recent_scores.get(sid) or []
			past_list = past_scores.get(sid) or []
			improvement = ((sum(recent_list) / len(recent_list)) - (sum(past_list) / len(past_list))) if past_list and recent_list else 0.0
			student = student_by_id.get(sid)
			top_performers.append({
				'student_name': getattr(getattr(student, 'profile', None), 'name', None) if student else None,
				'student_id': getattr(student, 'student_id', None) if student else None,
				'percentage': round(avg_score, 2),
				'improvement': round(improvement, 2),
			})
		top_performers.sort(key=lambda x: x.get('percentage', 0.0), reverse=True)

		graded_count = pending_count = 0
		pending_submissions = []
		solutions = AssessmentSolution.objects.select_related('assessment', 'student__profile').filter(assessment__given_by_id__in=teacher_ids, student_id__in=student_ids).order_by('assessment__due_at', 'submitted_at')
		grade_by_solution_id = {}
		for g in GeneralAssessmentGrade.objects.filter(assessment__given_by_id__in=teacher_ids, student_id__in=student_ids).select_related('assessment', 'solution'):
			if g.solution_id:
				grade_by_solution_id[g.solution_id] = g

		for sol in solutions:
			if grade_by_solution_id.get(sol.id):
				graded_count += 1
				continue
			pending_count += 1
			pending_submissions.append({
				'student_name': getattr(getattr(sol.student, 'profile', None), 'name', None),
				'student_id': getattr(sol.student, 'student_id', None),
				'assessment_title': getattr(sol.assessment, 'title', None),
				'subject': None,
				'due_at': getattr(sol.assessment, 'due_at', None),
				'submitted_at': sol.submitted_at,
			})
			if len(pending_submissions) >= 5:
				break

		total_review = graded_count + pending_count
		completion_rate = (graded_count / total_review * 100.0) if total_review else 0.0
		now = timezone.now()
		upcoming_deadlines = []
		for ga in GeneralAssessment.objects.filter(given_by_id__in=teacher_ids, due_at__gte=now).annotate(submissions_done=Count('solutions')).order_by('due_at'):
			submissions_done = getattr(ga, 'submissions_done', 0) or 0
			completion = (submissions_done / total_students * 100.0) if total_students else 0.0
			days_left = (ga.due_at.date() - now.date()).days if ga.due_at else 0
			upcoming_deadlines.append({
				'assessment_title': ga.title,
				'subject': None,
				'submissions_done': submissions_done,
				'submissions_expected': total_students,
				'completion_percentage': round(completion, 2),
				'due_at': ga.due_at,
				'days_left': days_left,
			})
		for la in LessonAssessment.objects.filter(given_by_id__in=teacher_ids, due_at__gte=now).annotate(submissions_done=Count('grades')).select_related('lesson__subject').order_by('due_at'):
			submissions_done = getattr(la, 'submissions_done', 0) or 0
			completion = (submissions_done / total_students * 100.0) if total_students else 0.0
			days_left = (la.due_at.date() - now.date()).days if la.due_at else 0
			upcoming_deadlines.append({
				'assessment_title': la.title,
				'subject': getattr(getattr(la.lesson, 'subject', None), 'name', None),
				'submissions_done': submissions_done,
				'submissions_expected': total_students,
				'completion_percentage': round(completion, 2),
				'due_at': la.due_at,
				'days_left': days_left,
			})
		upcoming_deadlines.sort(key=lambda x: x.get('due_at') or now)
		return Response({
			'summarycards': {
				'total_students': total_students,
				'class_average': round(class_average, 2),
				'pending_review': pending_count,
				'completion_rate': round(completion_rate, 2),
			},
			'top_performers': top_performers[:3],
			'pending_submissions': pending_submissions,
			'upcoming_deadlines': upcoming_deadlines[:5],
		})

	@extend_schema(description="School grade overview for head teachers.", responses={200: TeacherGradesResponseSerializer})
	@action(detail=False, methods=['get'], url_path='grades')
	def grades(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher_ids = self._school_teacher_ids(request)
		items = []
		total = excellent = good = needs_improvement = 0

		lesson_grades = LessonAssessmentGrade.objects.select_related('lesson_assessment__lesson__subject', 'student__profile').filter(lesson_assessment__given_by_id__in=teacher_ids)
		for g in lesson_grades:
			if g.score is None:
				continue
			score = float(g.score)
			grade_letter, remark = self._grade_for_score(score)
			status_bucket = self._grade_status(grade_letter, remark)
			total += 1
			excellent += int(status_bucket == 'Excellent')
			good += int(status_bucket == 'Good')
			needs_improvement += int(status_bucket == 'Needs Improvement')
			items.append({
				'student_name': getattr(getattr(g.student, 'profile', None), 'name', None),
				'student_id': getattr(g.student, 'student_id', None),
				'subject': getattr(getattr(getattr(g.lesson_assessment, 'lesson', None), 'subject', None), 'name', None),
				'grade_letter': grade_letter,
				'percentage': round(score, 2),
				'status': status_bucket,
				'updated_at': getattr(g, 'updated_at', getattr(g, 'created_at', None)),
			})

		general_grades = GeneralAssessmentGrade.objects.select_related('assessment', 'student__profile').filter(assessment__given_by_id__in=teacher_ids)
		for g in general_grades:
			if g.score is None:
				continue
			score = float(g.score)
			grade_letter, remark = self._grade_for_score(score)
			status_bucket = self._grade_status(grade_letter, remark)
			total += 1
			excellent += int(status_bucket == 'Excellent')
			good += int(status_bucket == 'Good')
			needs_improvement += int(status_bucket == 'Needs Improvement')
			items.append({
				'student_name': getattr(getattr(g.student, 'profile', None), 'name', None),
				'student_id': getattr(g.student, 'student_id', None),
				'subject': None,
				'grade_letter': grade_letter,
				'percentage': round(score, 2),
				'status': status_bucket,
				'updated_at': getattr(g, 'updated_at', getattr(g, 'created_at', None)),
			})

		items.sort(key=lambda x: x.get('updated_at') or timezone.now(), reverse=True)
		return Response({'summary': {'total_grades': total, 'excellent': excellent, 'good': good, 'needs_improvement': needs_improvement}, 'grades': items})

	@extend_schema(request=GradeAssessmentSerializer, responses={200: OpenApiResponse(description='Grading result.')})
	@action(detail=False, methods=['post'], url_path='grade/general')
	def grade_general_assessment(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher_ids = self._school_teacher_ids(request)
		assessment_id = request.data.get('assessment_id')
		student_id = request.data.get('student_id')
		score = request.data.get('score')
		if assessment_id is None or student_id is None or score is None:
			return Response({'detail': 'assessment_id, student_id and score are required.'}, status=400)
		try:
			assessment = GeneralAssessment.objects.get(pk=assessment_id, given_by_id__in=teacher_ids)
		except GeneralAssessment.DoesNotExist:
			return Response({'detail': 'Assessment not found in your school.'}, status=404)
		try:
			student = Student.objects.get(pk=student_id, school_id=request.user.teacher.school_id)
		except Student.DoesNotExist:
			return Response({'detail': 'Student not found in your school.'}, status=404)
		try:
			score_value = float(score)
		except (TypeError, ValueError):
			return Response({'detail': 'score must be a number.'}, status=400)
		if score_value < 0:
			return Response({'detail': 'score cannot be negative.'}, status=400)
		if assessment.marks is not None and score_value > float(assessment.marks):
			return Response({'detail': 'score cannot exceed assessment total marks.'}, status=400)
		grade_obj, _ = GeneralAssessmentGrade.objects.update_or_create(assessment=assessment, student=student, defaults={'score': score_value})
		return Response({'assessment_id': grade_obj.assessment_id, 'student_id': grade_obj.student_id, 'score': grade_obj.score})

	@extend_schema(request=GradeAssessmentSerializer, responses={200: OpenApiResponse(description='Grading result.')})
	@action(detail=False, methods=['post'], url_path='grade/lesson')
	def grade_lesson_assessment(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher_ids = self._school_teacher_ids(request)
		assessment_id = request.data.get('assessment_id')
		student_id = request.data.get('student_id')
		score = request.data.get('score')
		if assessment_id is None or student_id is None or score is None:
			return Response({'detail': 'assessment_id, student_id and score are required.'}, status=400)
		try:
			assessment = LessonAssessment.objects.select_related('lesson__subject').get(pk=assessment_id, given_by_id__in=teacher_ids)
		except LessonAssessment.DoesNotExist:
			return Response({'detail': 'Assessment not found in your school.'}, status=404)
		try:
			student = Student.objects.get(pk=student_id, school_id=request.user.teacher.school_id)
		except Student.DoesNotExist:
			return Response({'detail': 'Student not found in your school.'}, status=404)
		try:
			score_value = float(score)
		except (TypeError, ValueError):
			return Response({'detail': 'score must be a number.'}, status=400)
		if score_value < 0:
			return Response({'detail': 'score cannot be negative.'}, status=400)
		if assessment.marks is not None and score_value > float(assessment.marks):
			return Response({'detail': 'score cannot exceed assessment total marks.'}, status=400)
		grade_obj, _ = LessonAssessmentGrade.objects.update_or_create(lesson_assessment=assessment, student=student, defaults={'score': score_value})
		return Response({'assessment_id': grade_obj.lesson_assessment_id, 'student_id': grade_obj.student_id, 'score': grade_obj.score})

	@extend_schema(description="List school submissions for all school teachers.", responses={200: OpenApiResponse(response=ParentSubmissionsResponseSerializer)})
	@action(detail=False, methods=['get'], url_path='submissions')
	def submissions(self, request):
		deny = self._require_teacher(request)
		if deny:
			return deny
		teacher_ids = self._school_teacher_ids(request)
		solutions = AssessmentSolution.objects.select_related('assessment', 'student__profile').filter(assessment__given_by_id__in=teacher_ids)
		grade_by_solution_id: Dict[int, GeneralAssessmentGrade] = {}
		for g in GeneralAssessmentGrade.objects.filter(assessment__given_by_id__in=teacher_ids).select_related('assessment', 'solution'):
			if g.solution_id:
				grade_by_solution_id[g.solution_id] = g

		items = []
		graded_count = pending_count = 0
		for sol in solutions:
			grade_obj = grade_by_solution_id.get(sol.id)
			if grade_obj:
				graded_count += 1
			else:
				pending_count += 1
			items.append({
				'child_name': getattr(getattr(sol.student, 'profile', None), 'name', None),
				'assessment_title': getattr(sol.assessment, 'title', None),
				'subject': None,
				'score': float(grade_obj.score) if grade_obj else None,
				'assessment_score': float(getattr(sol.assessment, 'marks', 0.0) or 0.0),
				'submission_status': 'Graded' if grade_obj else 'Pending Review',
				'solution': {
					'solution': sol.solution,
					'attachment': request.build_absolute_uri(sol.attachment.url) if sol.attachment else None,
				},
				'date_submitted': sol.submitted_at,
			})

		lesson_grades = LessonAssessmentGrade.objects.select_related('lesson_assessment__lesson__subject', 'student__profile').filter(lesson_assessment__given_by_id__in=teacher_ids)
		for lg in lesson_grades:
			graded_count += 1
			items.append({
				'child_name': getattr(getattr(lg.student, 'profile', None), 'name', None),
				'assessment_title': getattr(lg.lesson_assessment, 'title', None),
				'subject': getattr(getattr(lg.lesson_assessment.lesson, 'subject', None), 'name', None),
				'score': float(lg.score) if lg.score is not None else None,
				'assessment_score': float(getattr(lg.lesson_assessment, 'marks', 0.0) or 0.0),
				'submission_status': 'Graded',
				'solution': {'solution': None, 'attachment': None},
				'date_submitted': lg.created_at,
			})

		return Response({'submissions': items, 'summary': {'graded': graded_count, 'pending': pending_count}})
