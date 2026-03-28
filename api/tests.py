from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from rest_framework.test import APIClient

from accounts.models import User, Student, Teacher, Parent, County, District, School
from content.models import Subject, Period, LessonResource, LessonAssessment, LessonAssessmentGrade, TakeLesson, LessonAssessmentSolution, GeneralAssessment, AssessmentSolution, GameModel, Activity, LessonTemporaryUnlock, Story
from elearncore.sysutils.constants import UserRole, StudentLevel, ContentType, AssessmentType, Status as StatusEnum


class AdminGeographyBulkUploadTests(TestCase):
	def setUp(self):
		cache.clear()
		self.client = APIClient()
		admin = User(
			name="Admin",
			phone="231770999999",
			role=UserRole.ADMIN.value,
			is_staff=True,
			is_superuser=True,
		)
		admin.set_password("pass")
		admin.save()
		self.client.force_authenticate(user=admin)

	def test_counties_bulk_template_download(self):
		resp = self.client.get('/api-v1/admin/counties/bulk-template/')
		self.assertEqual(resp.status_code, 200)
		self.assertIn('text/csv', resp.get('Content-Type', ''))
		self.assertIn('counties_bulk_template.csv', resp.get('Content-Disposition', ''))

	def test_counties_bulk_create(self):
		csv_body = "name,status,moderation_comment\nMontserrado,APPROVED,Initial import\n"
		upload = SimpleUploadedFile('counties.csv', csv_body.encode('utf-8'), content_type='text/csv')
		resp = self.client.post('/api-v1/admin/counties/bulk-create/', data={'file': upload}, format='multipart')
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(County.objects.filter(name='Montserrado').count(), 1)

	def test_districts_bulk_create_with_county_name(self):
		County.objects.create(name='Montserrado')
		csv_body = "name,county_name,status\nCareysburg,Montserrado,APPROVED\n"
		upload = SimpleUploadedFile('districts.csv', csv_body.encode('utf-8'), content_type='text/csv')
		resp = self.client.post('/api-v1/admin/districts/bulk-create/', data={'file': upload}, format='multipart')
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(District.objects.filter(name='Careysburg').count(), 1)

	def test_schools_bulk_create_with_district_name_and_county_name(self):
		county = County.objects.create(name='Montserrado')
		district = District.objects.create(county=county, name='Careysburg')
		csv_body = "name,district_name,county_name,status\nAfrilearn Academy,Careysburg,Montserrado,APPROVED\n"
		upload = SimpleUploadedFile('schools.csv', csv_body.encode('utf-8'), content_type='text/csv')
		resp = self.client.post('/api-v1/admin/schools/bulk-create/', data={'file': upload}, format='multipart')
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(School.objects.filter(name='Afrilearn Academy', district=district).count(), 1)


class KidsSubjectsAndLessonsProgressionTests(TestCase):
	def setUp(self):
		cache.clear()
		self.client = APIClient()
		self.user = User.objects.create_user(
			phone='231770000111',
			name='Student One',
			email='student1@example.com',
			password='pass',
			role=UserRole.STUDENT.value,
		)
		self.student = Student.objects.create(
			profile=self.user,
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)
		self.client.force_authenticate(user=self.user)

		self.subject_math = Subject.objects.create(
			name='Mathematics',
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)
		self.subject_science = Subject.objects.create(
			name='Science',
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)
		self.period_jan = Period.objects.create(name='January', start_month=1, end_month=1)
		self.period_feb = Period.objects.create(name='February', start_month=2, end_month=2)

		resource_file = lambda name: SimpleUploadedFile(name, b'lesson-bytes', content_type='video/mp4')

		self.lesson_1 = LessonResource.objects.create(
			subject=self.subject_math,
			period=self.period_jan,
			title='Counting Numbers',
			type=ContentType.VIDEO.value,
			status=StatusEnum.APPROVED.value,
			resource=resource_file('counting.mp4'),
		)
		self.lesson_2 = LessonResource.objects.create(
			subject=self.subject_math,
			period=self.period_feb,
			title='Adding Numbers',
			type=ContentType.VIDEO.value,
			status=StatusEnum.APPROVED.value,
			resource=resource_file('adding.mp4'),
		)
		self.lesson_3 = LessonResource.objects.create(
			subject=self.subject_science,
			period=self.period_jan,
			title='Plants Around Us',
			type=ContentType.VIDEO.value,
			status=StatusEnum.APPROVED.value,
			resource=resource_file('plants.mp4'),
		)

		self.lesson_1_assessment = LessonAssessment.objects.create(
			lesson=self.lesson_1,
			title='Counting Quiz',
			type=AssessmentType.QUIZ.value,
			status=StatusEnum.APPROVED.value,
		)
		self.lesson_2_assessment = LessonAssessment.objects.create(
			lesson=self.lesson_2,
			title='Adding Quiz',
			type=AssessmentType.QUIZ.value,
			status=StatusEnum.APPROVED.value,
		)

		TakeLesson.objects.create(student=self.student, lesson=self.lesson_1)
		TakeLesson.objects.create(student=self.student, lesson=self.lesson_2)
		LessonAssessmentSolution.objects.create(
			lesson_assessment=self.lesson_1_assessment,
			student=self.student,
			solution='Done',
			attachment=SimpleUploadedFile('counting-answer.txt', b'done', content_type='text/plain'),
		)

	def test_subjects_and_lessons_returns_progression_lock_state(self):
		resp = self.client.get('/api-v1/kids/subjectsandlessons/?test=progression')
		self.assertEqual(resp.status_code, 200)

		payload = resp.json()
		lessons = payload['lessons']
		self.assertEqual([lesson['id'] for lesson in lessons], [self.lesson_1.id, self.lesson_2.id, self.lesson_3.id])

		first, second, third = lessons
		self.assertFalse(first['is_locked'])
		self.assertTrue(first['is_completed'])
		self.assertEqual(first['progression_status'], 'completed')
		self.assertEqual(first['next_video_id'], self.lesson_2.id)

		self.assertFalse(second['is_locked'])
		self.assertFalse(second['is_completed'])
		self.assertEqual(second['progression_status'], 'in_progress')
		self.assertEqual(second['assessments_total'], 1)
		self.assertEqual(second['assessments_completed'], 0)
		self.assertEqual(second['next_video_id'], self.lesson_3.id)

		self.assertTrue(third['is_locked'])
		self.assertFalse(third['is_completed'])
		self.assertEqual(third['progression_status'], 'locked')
		self.assertIsNone(third['next_video_id'])

	def test_lessons_list_hides_locked_lessons_for_students(self):
		resp = self.client.get('/api-v1/lessons/')
		self.assertEqual(resp.status_code, 200)
		payload = resp.json()
		self.assertEqual(payload['count'], 2)
		returned_ids = [item['id'] for item in payload['results']]
		self.assertEqual(returned_ids, [self.lesson_1.id, self.lesson_2.id])

	def test_locked_lesson_detail_is_forbidden(self):
		resp = self.client.get(f'/api-v1/lessons/{self.lesson_3.id}/')
		self.assertEqual(resp.status_code, 403)
		self.assertIn('Complete the previous lesson', resp.json()['detail'])

	def test_locked_lesson_cannot_be_started_directly(self):
		resp = self.client.post('/api-v1/taken-lessons/', {'lesson': self.lesson_3.id}, format='json')
		self.assertEqual(resp.status_code, 403)
		self.assertIn('Complete the previous lesson', resp.json()['detail'])
		self.assertFalse(TakeLesson.objects.filter(student=self.student, lesson=self.lesson_3).exists())

	def test_subjects_and_lessons_cache_is_invalidated_after_submission(self):
		first_resp = self.client.get('/api-v1/kids/subjectsandlessons/?test=cache')
		self.assertEqual(first_resp.status_code, 200)
		first_lessons = first_resp.json()['lessons']
		self.assertTrue(first_lessons[-1]['is_locked'])

		submit_resp = self.client.post(
			'/api-v1/kids/submit-solution/',
			{'lesson_id': self.lesson_2_assessment.id, 'solution': 'Submitted'},
			format='multipart',
		)
		self.assertEqual(submit_resp.status_code, 200)

		second_resp = self.client.get('/api-v1/kids/subjectsandlessons/?test=cache')
		self.assertEqual(second_resp.status_code, 200)
		second_lessons = second_resp.json()['lessons']
		self.assertFalse(second_lessons[1]['is_locked'])
		self.assertTrue(second_lessons[1]['is_completed'])
		self.assertFalse(second_lessons[2]['is_locked'])


class KidsStoriesEndpointTests(TestCase):
	def setUp(self):
		cache.clear()
		self.client = APIClient()
		self.student_user = User.objects.create_user(
			phone='231770010111',
			name='Story Student',
			email='storystudent@example.com',
			password='pass',
			role=UserRole.STUDENT.value,
		)
		self.student = Student.objects.create(
			profile=self.student_user,
			grade=StudentLevel.GRADE2.value,
			status=StatusEnum.APPROVED.value,
		)

		self.teacher_user = User.objects.create_user(
			phone='231770010112',
			name='Story Teacher',
			email='storyteacher@example.com',
			password='pass',
			role=UserRole.TEACHER.value,
		)

		self.story_grade2_friendship = Story.objects.create(
			title='Kona and the Lost Lunch Box',
			grade=StudentLevel.GRADE2.value,
			tag='Friendship',
			estimated_minutes=4,
			body='Once upon a time...',
			characters=[{'name': 'Kona', 'description': 'A kind student.'}],
			vocabulary=[{'word': 'honesty', 'definition': 'telling the truth'}],
			moral='Tell the truth and help others.',
			cover_image={'image_url': 'https://example.com/cover1.png', 'alt_text': 'Kids at school'},
			is_published=True,
		)

		self.story_grade2_honesty = Story.objects.create(
			title='The Broken Pencil',
			grade=StudentLevel.GRADE2.value,
			tag='Honesty',
			estimated_minutes=3,
			body='A short story body.',
			characters=[{'name': 'Momo', 'description': 'A curious learner.'}],
			vocabulary=[{'word': 'careful', 'definition': 'doing things slowly and safely'}],
			moral='Be honest when mistakes happen.',
			cover_image={'image_url': 'https://example.com/cover2.png', 'alt_text': 'A child with a pencil'},
			is_published=True,
		)

		Story.objects.create(
			title='Not Published Story',
			grade=StudentLevel.GRADE2.value,
			tag='Friendship',
			estimated_minutes=3,
			body='Hidden story.',
			characters=[],
			vocabulary=[],
			moral='',
			cover_image={},
			is_published=False,
		)

		Story.objects.create(
			title='Older Grade Story',
			grade=StudentLevel.GRADE5.value,
			tag='Friendship',
			estimated_minutes=6,
			body='Older grade story.',
			characters=[],
			vocabulary=[],
			moral='',
			cover_image={},
			is_published=True,
		)

	def test_student_can_list_stories_defaulting_to_own_grade(self):
		self.client.force_authenticate(user=self.student_user)
		resp = self.client.get('/api-v1/kids/stories/')
		self.assertEqual(resp.status_code, 200)
		titles = {item['title'] for item in resp.json()}
		self.assertIn('Kona and the Lost Lunch Box', titles)
		self.assertIn('The Broken Pencil', titles)
		self.assertNotIn('Older Grade Story', titles)
		self.assertNotIn('Not Published Story', titles)

	def test_student_can_filter_stories_by_grade_and_tag(self):
		self.client.force_authenticate(user=self.student_user)
		resp = self.client.get('/api-v1/kids/stories/?grade=GRADE%202&tag=Honesty')
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(len(resp.json()), 1)
		self.assertEqual(resp.json()[0]['title'], 'The Broken Pencil')

	def test_student_can_retrieve_story_detail(self):
		self.client.force_authenticate(user=self.student_user)
		resp = self.client.get(f'/api-v1/kids/stories/{self.story_grade2_friendship.id}/')
		self.assertEqual(resp.status_code, 200)
		data = resp.json()
		self.assertEqual(data['title'], 'Kona and the Lost Lunch Box')
		self.assertIn('characters', data)
		self.assertIn('vocabulary', data)
		self.assertIn('moral', data)
		self.assertIn('cover_image', data)

	def test_non_student_cannot_access_stories_endpoints(self):
		self.client.force_authenticate(user=self.teacher_user)
		list_resp = self.client.get('/api-v1/kids/stories/')
		detail_resp = self.client.get(f'/api-v1/kids/stories/{self.story_grade2_friendship.id}/')
		self.assertEqual(list_resp.status_code, 403)
		self.assertEqual(detail_resp.status_code, 403)


class StoryWorkflowVisibilityTests(TestCase):
	def setUp(self):
		cache.clear()
		self.client = APIClient()

		county = County.objects.create(name='Montserrado', status=StatusEnum.APPROVED.value)
		district = District.objects.create(county=county, name='Careysburg', status=StatusEnum.APPROVED.value)
		self.school_one = School.objects.create(district=district, name='School One', status=StatusEnum.APPROVED.value)
		self.school_two = School.objects.create(district=district, name='School Two', status=StatusEnum.APPROVED.value)

		self.student_user = User.objects.create_user(
			phone='231770710001',
			name='Story Student Scoped',
			email='student.scoped@example.com',
			password='pass',
			role=UserRole.STUDENT.value,
		)
		self.student = Student.objects.create(
			profile=self.student_user,
			school=self.school_one,
			grade=StudentLevel.GRADE2.value,
			status=StatusEnum.APPROVED.value,
		)

		self.teacher_user = User.objects.create_user(
			phone='231770710002',
			name='Story Teacher Scoped',
			email='teacher.scoped@example.com',
			password='pass',
			role=UserRole.TEACHER.value,
		)
		self.teacher = Teacher.objects.create(
			profile=self.teacher_user,
			school=self.school_one,
			status=StatusEnum.APPROVED.value,
		)

		self.headteacher_user = User.objects.create_user(
			phone='231770710003',
			name='Story Headteacher Scoped',
			email='head.scoped@example.com',
			password='pass',
			role=UserRole.HEADTEACHER.value,
		)
		self.headteacher = Teacher.objects.create(
			profile=self.headteacher_user,
			school=self.school_one,
			status=StatusEnum.APPROVED.value,
		)

		self.validator_user = User.objects.create_user(
			phone='231770710004',
			name='Story Validator Scoped',
			email='validator.scoped@example.com',
			password='pass',
			role=UserRole.CONTENTVALIDATOR.value,
		)

		self.creator_user = User.objects.create_user(
			phone='231770710005',
			name='Story Creator Scoped',
			email='creator.scoped@example.com',
			password='pass',
			role=UserRole.CONTENTCREATOR.value,
		)

		subject = Subject.objects.create(
			name='Mathematics Scoped Stories',
			grade=StudentLevel.GRADE2.value,
			status=StatusEnum.APPROVED.value,
		)
		subject.teachers.add(self.teacher)

		self.global_story = Story.objects.create(
			title='Global Published Story',
			grade=StudentLevel.GRADE2.value,
			tag='Friendship',
			estimated_minutes=4,
			body='Global body',
			characters=[],
			vocabulary=[],
			moral='Global moral',
			cover_image={},
			is_published=True,
			school=None,
		)
		self.school_one_published = Story.objects.create(
			title='School One Published Story',
			grade=StudentLevel.GRADE2.value,
			tag='Honesty',
			estimated_minutes=4,
			body='School one body',
			characters=[],
			vocabulary=[],
			moral='School one moral',
			cover_image={},
			is_published=True,
			school=self.school_one,
		)
		self.school_one_unpublished = Story.objects.create(
			title='School One Draft Story',
			grade=StudentLevel.GRADE2.value,
			tag='Kindness',
			estimated_minutes=4,
			body='School one draft body',
			characters=[],
			vocabulary=[],
			moral='Draft moral',
			cover_image={},
			is_published=False,
			school=self.school_one,
		)
		Story.objects.create(
			title='School Two Published Story',
			grade=StudentLevel.GRADE2.value,
			tag='Respect',
			estimated_minutes=4,
			body='School two body',
			characters=[],
			vocabulary=[],
			moral='School two moral',
			cover_image={},
			is_published=True,
			school=self.school_two,
		)
		Story.objects.create(
			title='Global Published Grade 5 Story',
			grade=StudentLevel.GRADE5.value,
			tag='Friendship',
			estimated_minutes=4,
			body='Global grade 5 body',
			characters=[],
			vocabulary=[],
			moral='Grade 5 moral',
			cover_image={},
			is_published=True,
			school=None,
		)

	def test_kids_only_see_published_global_plus_own_school(self):
		self.client.force_authenticate(user=self.student_user)
		resp = self.client.get('/api-v1/kids/stories/')
		self.assertEqual(resp.status_code, 200)
		titles = {item['title'] for item in resp.json()}
		self.assertIn('Global Published Story', titles)
		self.assertIn('School One Published Story', titles)
		self.assertNotIn('School One Draft Story', titles)
		self.assertNotIn('School Two Published Story', titles)

	def test_teacher_sees_published_scope_for_taught_grades(self):
		self.client.force_authenticate(user=self.teacher_user)
		resp = self.client.get('/api-v1/teacher/stories/')
		self.assertEqual(resp.status_code, 200)
		titles = {item['title'] for item in resp.json()}
		self.assertIn('Global Published Story', titles)
		self.assertIn('School One Published Story', titles)
		self.assertNotIn('School One Draft Story', titles)
		self.assertNotIn('School Two Published Story', titles)
		self.assertNotIn('Global Published Grade 5 Story', titles)

	def test_headteacher_sees_all_school_stories_including_unpublished(self):
		self.client.force_authenticate(user=self.headteacher_user)
		resp = self.client.get('/api-v1/headteacher/stories/')
		self.assertEqual(resp.status_code, 200)
		titles = {item['title'] for item in resp.json()}
		self.assertIn('School One Published Story', titles)
		self.assertIn('School One Draft Story', titles)
		self.assertNotIn('Global Published Story', titles)

	def test_headteacher_publishes_unpublished_story_in_school(self):
		self.client.force_authenticate(user=self.headteacher_user)
		resp = self.client.post(
			'/api-v1/headteacher/stories/publish/',
			{'story_ids': [self.school_one_unpublished.id]},
			format='json',
		)
		self.assertEqual(resp.status_code, 200)
		self.school_one_unpublished.refresh_from_db()
		self.assertTrue(self.school_one_unpublished.is_published)

	def test_validator_can_publish_story(self):
		self.client.force_authenticate(user=self.validator_user)
		resp = self.client.post(
			'/api-v1/content/stories/publish/',
			{'story_ids': [self.school_one_unpublished.id]},
			format='json',
		)
		self.assertEqual(resp.status_code, 200)
		self.school_one_unpublished.refresh_from_db()
		self.assertTrue(self.school_one_unpublished.is_published)

	@patch('api.viewsets._enqueue_story_generation')
	def test_generation_endpoints_validate_count_and_queue(self, mocked_enqueue):
		mocked_enqueue.return_value = type('TaskResult', (), {'id': 'task-123'})()

		self.client.force_authenticate(user=self.creator_user)
		bad_resp = self.client.post(
			'/api-v1/content/stories/generate/',
			{'grade': StudentLevel.GRADE2.value, 'tag': 'Friendship', 'count': 11},
			format='json',
		)
		self.assertEqual(bad_resp.status_code, 400)

		ok_resp = self.client.post(
			'/api-v1/content/stories/generate/',
			{'grade': StudentLevel.GRADE2.value, 'tag': 'Friendship', 'count': 2},
			format='json',
		)
		self.assertEqual(ok_resp.status_code, 202)

		self.client.force_authenticate(user=self.teacher_user)
		teacher_ok = self.client.post(
			'/api-v1/teacher/stories/generate/',
			{'grade': StudentLevel.GRADE2.value, 'tag': 'Friendship', 'count': 1},
			format='json',
		)
		self.assertEqual(teacher_ok.status_code, 202)

		teacher_forbidden = self.client.post(
			'/api-v1/teacher/stories/generate/',
			{'grade': StudentLevel.GRADE5.value, 'tag': 'Friendship', 'count': 1},
			format='json',
		)
		self.assertEqual(teacher_forbidden.status_code, 403)


class KidsProgressGardenRankingTests(TestCase):
	def setUp(self):
		cache.clear()
		self.client = APIClient()

		county = County.objects.create(name='Montserrado', status=StatusEnum.APPROVED.value)
		district = District.objects.create(county=county, name='Careysburg', status=StatusEnum.APPROVED.value)
		school = School.objects.create(district=district, name='Unity Academy', status=StatusEnum.APPROVED.value)

		self.user = User.objects.create_user(
			phone='231770004111',
			name='Ranked Student',
			email='ranked.student@example.com',
			password='pass',
			role=UserRole.STUDENT.value,
		)
		self.student = Student.objects.create(
			profile=self.user,
			school=school,
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)

		other_user = User.objects.create_user(
			phone='231770004112',
			name='Peer Student',
			email='peer.student@example.com',
			password='pass',
			role=UserRole.STUDENT.value,
		)
		self.peer_student = Student.objects.create(
			profile=other_user,
			school=school,
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)

		subject = Subject.objects.create(
			name='Mathematics',
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)
		period = Period.objects.create(name='April', start_month=4, end_month=4)
		lesson_one = LessonResource.objects.create(
			subject=subject,
			period=period,
			title='Place Values',
			type=ContentType.VIDEO.value,
			status=StatusEnum.APPROVED.value,
			resource=SimpleUploadedFile('place-values.mp4', b'video', content_type='video/mp4'),
		)
		lesson_two = LessonResource.objects.create(
			subject=subject,
			period=period,
			title='Addition Basics',
			type=ContentType.VIDEO.value,
			status=StatusEnum.APPROVED.value,
			resource=SimpleUploadedFile('addition-basics.mp4', b'video', content_type='video/mp4'),
		)

		TakeLesson.objects.create(student=self.student, lesson=lesson_one)
		TakeLesson.objects.create(student=self.student, lesson=lesson_two)
		TakeLesson.objects.create(student=self.peer_student, lesson=lesson_one)

		self.client.force_authenticate(user=self.user)

	def test_progress_garden_returns_scoped_rank_data(self):
		response = self.client.get('/api-v1/kids/progressgarden/?test=rank-cache')
		self.assertEqual(response.status_code, 200)

		payload = response.json()
		self.assertIsNotNone(payload['rank_in_school'])
		self.assertEqual(payload['rank_in_school']['out_of'], 2)
		self.assertEqual(payload['rank_in_school']['rank'], 1)
		self.assertIsNotNone(payload['rank_in_district'])
		self.assertEqual(payload['rank_in_district']['out_of'], 2)
		self.assertIsNotNone(payload['rank_in_county'])
		self.assertEqual(payload['rank_in_county']['out_of'], 2)


class KidsAssessmentListingEndpointsTests(TestCase):
	def setUp(self):
		cache.clear()
		self.client = APIClient()
		self.user = User.objects.create_user(
			phone='231770004211',
			name='Listing Student',
			email='listing.student@example.com',
			password='pass',
			role=UserRole.STUDENT.value,
		)
		self.student = Student.objects.create(
			profile=self.user,
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)
		self.client.force_authenticate(user=self.user)

		subject = Subject.objects.create(
			name='Science',
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)
		period = Period.objects.create(name='May', start_month=5, end_month=5)
		lesson = LessonResource.objects.create(
			subject=subject,
			period=period,
			title='Living Things',
			type=ContentType.VIDEO.value,
			status=StatusEnum.APPROVED.value,
			resource=SimpleUploadedFile('living-things.mp4', b'video', content_type='video/mp4'),
		)

		self.general_assessment = GeneralAssessment.objects.create(
			title='Weekly General Quiz',
			type=AssessmentType.QUIZ.value,
			status=StatusEnum.APPROVED.value,
			grade=StudentLevel.GRADE3.value,
			due_at=timezone.now() + timedelta(days=3),
		)
		self.lesson_assessment = LessonAssessment.objects.create(
			lesson=lesson,
			title='Living Things Lesson Quiz',
			type=AssessmentType.QUIZ.value,
			status=StatusEnum.APPROVED.value,
			due_at=timezone.now() + timedelta(days=2),
		)

		AssessmentSolution.objects.create(
			assessment=self.general_assessment,
			student=self.student,
			solution='Submitted answer',
			attachment=SimpleUploadedFile('general-solution.txt', b'ans', content_type='text/plain'),
		)

	def test_quizzes_endpoint_returns_paginated_items(self):
		resp = self.client.get('/api-v1/kids/quizzes/?test=listing')
		self.assertEqual(resp.status_code, 200)
		payload = resp.json()
		self.assertIn('quizzes', payload)
		self.assertIn('pagination', payload)
		self.assertEqual(payload['pagination']['count'], 2)

	def test_assessments_endpoint_returns_paginated_items(self):
		resp = self.client.get('/api-v1/kids/assessments/?test=listing')
		self.assertEqual(resp.status_code, 200)
		payload = resp.json()
		self.assertIn('assessments', payload)
		self.assertIn('pagination', payload)
		self.assertEqual(payload['pagination']['count'], 2)

	def test_assignments_endpoint_includes_stats(self):
		resp = self.client.get('/api-v1/kids/assignments/?test=listing')
		self.assertEqual(resp.status_code, 200)
		payload = resp.json()
		self.assertIn('stats', payload)
		self.assertEqual(payload['stats']['total'], 2)
		self.assertEqual(payload['stats']['submitted'], 1)


class KidsPeerSolutionsEndpointTests(TestCase):
	def setUp(self):
		cache.clear()
		self.client = APIClient()

		self.user = User.objects.create_user(
			phone='231770004311',
			name='Peer View Student',
			email='peer.view.student@example.com',
			password='pass',
			role=UserRole.STUDENT.value,
		)
		self.student = Student.objects.create(
			profile=self.user,
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)
		self.client.force_authenticate(user=self.user)

		subject = Subject.objects.create(
			name='Science Peer Visibility',
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)
		period = Period.objects.create(name='July', start_month=7, end_month=7)
		lesson = LessonResource.objects.create(
			subject=subject,
			period=period,
			title='Peer Lesson',
			type=ContentType.VIDEO.value,
			status=StatusEnum.APPROVED.value,
			resource=SimpleUploadedFile('peer-lesson.mp4', b'video', content_type='video/mp4'),
		)

		self.general_assessment = GeneralAssessment.objects.create(
			title='Peer General Assessment',
			type=AssessmentType.QUIZ.value,
			status=StatusEnum.APPROVED.value,
			grade=StudentLevel.GRADE3.value,
		)
		self.lesson_assessment = LessonAssessment.objects.create(
			lesson=lesson,
			title='Peer Lesson Assessment',
			type=AssessmentType.QUIZ.value,
			status=StatusEnum.APPROVED.value,
		)

	def _create_peer_general_solutions(self, count=12):
		for i in range(count):
			peer_user = User.objects.create_user(
				phone=f'23188000{i:04d}',
				name=f'Peer User {i}',
				email=f'peer.general.{i}@example.com',
				password='pass',
				role=UserRole.STUDENT.value,
			)
			peer_student = Student.objects.create(
				profile=peer_user,
				grade=StudentLevel.GRADE3.value,
				status=StatusEnum.APPROVED.value,
			)
			AssessmentSolution.objects.create(
				assessment=self.general_assessment,
				student=peer_student,
				solution=f'Peer general solution {i}',
				attachment=SimpleUploadedFile(f'peer-general-{i}.txt', b'peer', content_type='text/plain'),
			)

	def test_requires_own_solution_before_viewing_general_peer_solutions(self):
		resp = self.client.get(f'/api-v1/kids/peer-solutions/?general_id={self.general_assessment.id}')
		self.assertEqual(resp.status_code, 403)
		self.assertIn('Submit your own solution first', resp.json()['detail'])

	def test_returns_random_max_10_anonymized_general_peer_solutions(self):
		AssessmentSolution.objects.create(
			assessment=self.general_assessment,
			student=self.student,
			solution='My own general solution',
			attachment=SimpleUploadedFile('my-general-solution.txt', b'mine', content_type='text/plain'),
		)
		self._create_peer_general_solutions(count=12)

		resp = self.client.get(f'/api-v1/kids/peer-solutions/?general_id={self.general_assessment.id}')
		self.assertEqual(resp.status_code, 200)
		payload = resp.json()
		solutions = payload['solutions']
		self.assertLessEqual(len(solutions), 10)
		self.assertGreater(len(solutions), 0)
		for item in solutions:
			self.assertIn('peer_label', item)
			self.assertRegex(item['peer_label'], r'^Peer Student [A-F0-9]{8}$')
			self.assertNotIn('Peer User', item['peer_label'])

	def test_rejects_when_student_not_qualified_for_general_assessment(self):
		other_grade_assessment = GeneralAssessment.objects.create(
			title='Grade 5 Assessment',
			type=AssessmentType.QUIZ.value,
			status=StatusEnum.APPROVED.value,
			grade=StudentLevel.GRADE5.value,
		)
		resp = self.client.get(f'/api-v1/kids/peer-solutions/?general_id={other_grade_assessment.id}')
		self.assertEqual(resp.status_code, 403)

	def test_works_for_lesson_assessment_with_own_submission_gate(self):
		resp_no_own = self.client.get(f'/api-v1/kids/peer-solutions/?lesson_id={self.lesson_assessment.id}')
		self.assertEqual(resp_no_own.status_code, 403)

		LessonAssessmentSolution.objects.create(
			lesson_assessment=self.lesson_assessment,
			student=self.student,
			solution='My lesson solution',
			attachment=SimpleUploadedFile('my-lesson-solution.txt', b'mine', content_type='text/plain'),
		)

		for i in range(3):
			peer_user = User.objects.create_user(
				phone=f'23199000{i:04d}',
				name=f'Lesson Peer {i}',
				email=f'peer.lesson.{i}@example.com',
				password='pass',
				role=UserRole.STUDENT.value,
			)
			peer_student = Student.objects.create(
				profile=peer_user,
				grade=StudentLevel.GRADE3.value,
				status=StatusEnum.APPROVED.value,
			)
			LessonAssessmentSolution.objects.create(
				lesson_assessment=self.lesson_assessment,
				student=peer_student,
				solution=f'Lesson peer solution {i}',
				attachment=SimpleUploadedFile(f'peer-lesson-{i}.txt', b'peer', content_type='text/plain'),
			)

		resp = self.client.get(f'/api-v1/kids/peer-solutions/?lesson_id={self.lesson_assessment.id}')
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(resp.json()['assessment']['type'], 'lesson')
		for item in resp.json()['solutions']:
			self.assertRegex(item['peer_label'], r'^Peer Student [A-F0-9]{8}$')


class TeacherTemporaryLessonUnlockTests(TestCase):
	def setUp(self):
		cache.clear()

		county = County.objects.create(name='Bong', status=StatusEnum.APPROVED.value)
		district = District.objects.create(county=county, name='Gbarnga', status=StatusEnum.APPROVED.value)
		school = School.objects.create(district=district, name='Central High', status=StatusEnum.APPROVED.value)

		self.student_user = User.objects.create_user(
			phone='231770006111',
			name='Unlocked Student',
			email='unlock.student@example.com',
			password='pass',
			role=UserRole.STUDENT.value,
		)
		self.student = Student.objects.create(
			profile=self.student_user,
			school=school,
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)

		self.student_user_2 = User.objects.create_user(
			phone='231770006113',
			name='Unlocked Student Two',
			email='unlock.student2@example.com',
			password='pass',
			role=UserRole.STUDENT.value,
		)
		self.student_2 = Student.objects.create(
			profile=self.student_user_2,
			school=school,
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)

		self.teacher_user = User.objects.create_user(
			phone='231770006112',
			name='Class Teacher',
			email='class.teacher@example.com',
			password='pass',
			role=UserRole.TEACHER.value,
		)
		self.teacher = Teacher.objects.create(
			profile=self.teacher_user,
			school=school,
			status=StatusEnum.APPROVED.value,
		)

		self.math = Subject.objects.create(
			name='Math Unlocking',
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)
		self.math.teachers.add(self.teacher)

		self.other_subject = Subject.objects.create(
			name='Other Subject',
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)

		period = Period.objects.create(name='June', start_month=6, end_month=6)
		self.lesson_1 = LessonResource.objects.create(
			subject=self.math,
			period=period,
			title='Lesson One',
			type=ContentType.VIDEO.value,
			status=StatusEnum.APPROVED.value,
			resource=SimpleUploadedFile('unlock-lesson-1.mp4', b'video', content_type='video/mp4'),
		)
		self.lesson_2 = LessonResource.objects.create(
			subject=self.math,
			period=period,
			title='Lesson Two',
			type=ContentType.VIDEO.value,
			status=StatusEnum.APPROVED.value,
			resource=SimpleUploadedFile('unlock-lesson-2.mp4', b'video', content_type='video/mp4'),
		)
		self.lesson_other = LessonResource.objects.create(
			subject=self.other_subject,
			period=period,
			title='Other Lesson',
			type=ContentType.VIDEO.value,
			status=StatusEnum.APPROVED.value,
			resource=SimpleUploadedFile('unlock-other.mp4', b'video', content_type='video/mp4'),
		)

		self.lesson_1_assessment = LessonAssessment.objects.create(
			lesson=self.lesson_1,
			title='Lesson One Quiz',
			type=AssessmentType.QUIZ.value,
			status=StatusEnum.APPROVED.value,
		)

		TakeLesson.objects.create(student=self.student, lesson=self.lesson_1)

		self.student_client = APIClient()
		self.student_client.force_authenticate(user=self.student_user)
		self.student_2_client = APIClient()
		self.student_2_client.force_authenticate(user=self.student_user_2)
		self.teacher_client = APIClient()
		self.teacher_client.force_authenticate(user=self.teacher_user)

	def test_teacher_unlock_allows_access_and_start_until_revoked(self):
		before = self.student_client.get(f'/api-v1/lessons/{self.lesson_2.id}/')
		self.assertEqual(before.status_code, 403)

		unlock_resp = self.teacher_client.post(
			'/api-v1/teacher/unlock-lesson/',
			{
				'student_id': self.student.id,
				'lesson_id': self.lesson_2.id,
				'duration_hours': 2,
				'reason': 'Support intervention',
			},
			format='json',
		)
		self.assertEqual(unlock_resp.status_code, 200)
		self.assertEqual(LessonTemporaryUnlock.objects.filter(student=self.student, lesson=self.lesson_2, revoked_at__isnull=True).count(), 1)

		after_unlock = self.student_client.get(f'/api-v1/lessons/{self.lesson_2.id}/')
		self.assertEqual(after_unlock.status_code, 200)

		start_resp = self.student_client.post('/api-v1/taken-lessons/', {'lesson': self.lesson_2.id}, format='json')
		self.assertEqual(start_resp.status_code, 201)

		kids_payload = self.student_client.get('/api-v1/kids/subjectsandlessons/?unlock=test').json()
		lesson_two_item = [item for item in kids_payload['lessons'] if item['id'] == self.lesson_2.id][0]
		self.assertTrue(lesson_two_item['is_temporarily_unlocked'])
		self.assertIsNotNone(lesson_two_item['temporary_unlock_expires_at'])

		revoke_resp = self.teacher_client.post(
			'/api-v1/teacher/revoke-lesson-unlock/',
			{'student_id': self.student.id, 'lesson_id': self.lesson_2.id},
			format='json',
		)
		self.assertEqual(revoke_resp.status_code, 200)

		after_revoke = self.student_client.get(f'/api-v1/lessons/{self.lesson_2.id}/')
		self.assertEqual(after_revoke.status_code, 403)

	def test_unlock_duration_cannot_exceed_72_hours(self):
		resp = self.teacher_client.post(
			'/api-v1/teacher/unlock-lesson/',
			{'student_id': self.student.id, 'lesson_id': self.lesson_2.id, 'duration_hours': 73},
			format='json',
		)
		self.assertEqual(resp.status_code, 400)
		self.assertIn('duration_hours', resp.json())

	def test_teacher_can_unlock_whole_class(self):
		before_1 = self.student_client.get(f'/api-v1/lessons/{self.lesson_2.id}/')
		before_2 = self.student_2_client.get(f'/api-v1/lessons/{self.lesson_2.id}/')
		self.assertEqual(before_1.status_code, 403)
		self.assertEqual(before_2.status_code, 403)

		unlock_resp = self.teacher_client.post(
			'/api-v1/teacher/unlock-lesson/',
			{
				'unlock_whole_class': True,
				'lesson_id': self.lesson_2.id,
				'duration_hours': 2,
				'reason': 'Whole class intervention',
			},
			format='json',
		)
		self.assertEqual(unlock_resp.status_code, 200)
		self.assertEqual(
			LessonTemporaryUnlock.objects.filter(
				lesson=self.lesson_2,
				revoked_at__isnull=True,
			).count(),
			2,
		)

		after_1 = self.student_client.get(f'/api-v1/lessons/{self.lesson_2.id}/')
		after_2 = self.student_2_client.get(f'/api-v1/lessons/{self.lesson_2.id}/')
		self.assertEqual(after_1.status_code, 200)
		self.assertEqual(after_2.status_code, 200)

	def test_teacher_can_revoke_whole_class_unlock(self):
		unlock_resp = self.teacher_client.post(
			'/api-v1/teacher/unlock-lesson/',
			{
				'unlock_whole_class': True,
				'lesson_id': self.lesson_2.id,
				'duration_hours': 2,
			},
			format='json',
		)
		self.assertEqual(unlock_resp.status_code, 200)
		self.assertEqual(
			LessonTemporaryUnlock.objects.filter(
				lesson=self.lesson_2,
				revoked_at__isnull=True,
			).count(),
			2,
		)

		after_unlock_1 = self.student_client.get(f'/api-v1/lessons/{self.lesson_2.id}/')
		after_unlock_2 = self.student_2_client.get(f'/api-v1/lessons/{self.lesson_2.id}/')
		self.assertEqual(after_unlock_1.status_code, 200)
		self.assertEqual(after_unlock_2.status_code, 200)

		revoke_resp = self.teacher_client.post(
			'/api-v1/teacher/revoke-lesson-unlock/',
			{
				'unlock_whole_class': True,
				'lesson_id': self.lesson_2.id,
			},
			format='json',
		)
		self.assertEqual(revoke_resp.status_code, 200)
		self.assertEqual(revoke_resp.json()['revoked_count'], 2)
		self.assertEqual(
			LessonTemporaryUnlock.objects.filter(
				lesson=self.lesson_2,
				revoked_at__isnull=True,
			).count(),
			0,
		)

		after_revoke_1 = self.student_client.get(f'/api-v1/lessons/{self.lesson_2.id}/')
		after_revoke_2 = self.student_2_client.get(f'/api-v1/lessons/{self.lesson_2.id}/')
		self.assertEqual(after_revoke_1.status_code, 403)
		self.assertEqual(after_revoke_2.status_code, 403)

	def test_teacher_unlock_whole_class_enforces_xor_student_id(self):
		missing_both = self.teacher_client.post(
			'/api-v1/teacher/unlock-lesson/',
			{'lesson_id': self.lesson_2.id, 'duration_hours': 2},
			format='json',
		)
		self.assertEqual(missing_both.status_code, 400)

		both_set = self.teacher_client.post(
			'/api-v1/teacher/unlock-lesson/',
			{
				'student_id': self.student.id,
				'unlock_whole_class': True,
				'lesson_id': self.lesson_2.id,
				'duration_hours': 2,
			},
			format='json',
		)
		self.assertEqual(both_set.status_code, 400)

	def test_teacher_revoke_whole_class_enforces_xor_student_id(self):
		missing_both = self.teacher_client.post(
			'/api-v1/teacher/revoke-lesson-unlock/',
			{'lesson_id': self.lesson_2.id},
			format='json',
		)
		self.assertEqual(missing_both.status_code, 400)

		both_set = self.teacher_client.post(
			'/api-v1/teacher/revoke-lesson-unlock/',
			{
				'student_id': self.student.id,
				'unlock_whole_class': True,
				'lesson_id': self.lesson_2.id,
			},
			format='json',
		)
		self.assertEqual(both_set.status_code, 400)

	def test_teacher_can_only_unlock_subjects_they_teach(self):
		resp = self.teacher_client.post(
			'/api-v1/teacher/unlock-lesson/',
			{'student_id': self.student.id, 'lesson_id': self.lesson_other.id, 'duration_hours': 2},
			format='json',
		)
		self.assertEqual(resp.status_code, 403)
		self.assertIn('subjects you teach', resp.json()['detail'])

		resp_class = self.teacher_client.post(
			'/api-v1/teacher/unlock-lesson/',
			{'unlock_whole_class': True, 'lesson_id': self.lesson_other.id, 'duration_hours': 2},
			format='json',
		)
		self.assertEqual(resp_class.status_code, 403)
		self.assertIn('subjects you teach', resp_class.json()['detail'])

	def test_teacher_can_list_only_active_unlocks(self):
		self.teacher_client.post(
			'/api-v1/teacher/unlock-lesson/',
			{'student_id': self.student.id, 'lesson_id': self.lesson_2.id, 'duration_hours': 2},
			format='json',
		)

		resp = self.teacher_client.get('/api-v1/teacher/lesson-unlocks/')
		self.assertEqual(resp.status_code, 200)
		payload = resp.json()
		self.assertEqual(len(payload), 1)
		self.assertEqual(payload[0]['student_id'], self.student.id)
		self.assertEqual(payload[0]['lesson_id'], self.lesson_2.id)
		self.assertEqual(payload[0]['subject_id'], self.math.id)


class LessonUnlockCleanupCommandTests(TestCase):
	def setUp(self):
		cache.clear()
		county = County.objects.create(name='Lofa', status=StatusEnum.APPROVED.value)
		district = District.objects.create(county=county, name='Voinjama', status=StatusEnum.APPROVED.value)
		school = School.objects.create(district=district, name='Cleanup High', status=StatusEnum.APPROVED.value)

		user = User.objects.create_user(
			phone='231770006311',
			name='Cleanup Student',
			email='cleanup.student@example.com',
			password='pass',
			role=UserRole.STUDENT.value,
		)
		self.student = Student.objects.create(
			profile=user,
			school=school,
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)

		subject = Subject.objects.create(
			name='Cleanup Subject',
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)
		period = Period.objects.create(name='July', start_month=7, end_month=7)
		self.lesson = LessonResource.objects.create(
			subject=subject,
			period=period,
			title='Cleanup Lesson',
			type=ContentType.VIDEO.value,
			status=StatusEnum.APPROVED.value,
			resource=SimpleUploadedFile('cleanup-lesson.mp4', b'video', content_type='video/mp4'),
		)

	def test_cleanup_command_deletes_expired_unlocks(self):
		expired = LessonTemporaryUnlock.objects.create(
			lesson=self.lesson,
			student=self.student,
			expires_at=timezone.now() - timedelta(hours=1),
		)
		active = LessonTemporaryUnlock.objects.create(
			lesson=self.lesson,
			student=self.student,
			expires_at=timezone.now() + timedelta(hours=1),
		)

		out = StringIO()
		call_command('cleanup_lesson_unlocks', stdout=out)

		self.assertFalse(LessonTemporaryUnlock.objects.filter(id=expired.id).exists())
		self.assertTrue(LessonTemporaryUnlock.objects.filter(id=active.id).exists())

	def test_cleanup_command_dry_run_keeps_rows(self):
		expired = LessonTemporaryUnlock.objects.create(
			lesson=self.lesson,
			student=self.student,
			expires_at=timezone.now() - timedelta(hours=1),
		)
		out = StringIO()
		call_command('cleanup_lesson_unlocks', '--dry-run', stdout=out)
		self.assertTrue(LessonTemporaryUnlock.objects.filter(id=expired.id).exists())


class StudentGamificationPointsTests(TestCase):
	def setUp(self):
		cache.clear()
		self.client = APIClient()
		self.user = User.objects.create_user(
			phone='231770000211',
			name='Gamified Student',
			email='gamified@example.com',
			password='pass',
			role=UserRole.STUDENT.value,
		)
		self.student = Student.objects.create(
			profile=self.user,
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)
		self.client.force_authenticate(user=self.user)

		self.subject = Subject.objects.create(
			name='English',
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)
		self.period = Period.objects.create(name='March', start_month=3, end_month=3)
		self.video_lesson = LessonResource.objects.create(
			subject=self.subject,
			period=self.period,
			title='Alphabet Song',
			type=ContentType.VIDEO.value,
			status=StatusEnum.APPROVED.value,
			resource=SimpleUploadedFile('alphabet.mp4', b'video-bytes', content_type='video/mp4'),
		)
		self.general_assessment = GeneralAssessment.objects.create(
			title='Alphabet Quiz',
			type=AssessmentType.QUIZ.value,
			status=StatusEnum.APPROVED.value,
		)
		self.lesson_assessment = LessonAssessment.objects.create(
			lesson=self.video_lesson,
			title='Alphabet Lesson Quiz',
			type=AssessmentType.QUIZ.value,
			status=StatusEnum.APPROVED.value,
		)
		self.game = GameModel.objects.create(
			name='Letter Match',
			type='WORD_PUZZLE',
			correct_answer='A',
			status=StatusEnum.APPROVED.value,
		)

	def test_take_video_lesson_awards_points_once(self):
		resp = self.client.post('/api-v1/taken-lessons/', {'lesson': self.video_lesson.id}, format='json')
		self.assertEqual(resp.status_code, 201)
		self.student.refresh_from_db()
		self.assertEqual(self.student.points, 10)

	def test_play_game_awards_points_only_first_time(self):
		first = self.client.post('/api-v1/kids/play-game/', {'game_id': self.game.id}, format='json')
		self.assertEqual(first.status_code, 200)
		self.assertEqual(first.json()['points_awarded'], 5)

		second = self.client.post('/api-v1/kids/play-game/', {'game_id': self.game.id}, format='json')
		self.assertEqual(second.status_code, 200)
		self.assertEqual(second.json()['points_awarded'], 0)

		self.student.refresh_from_db()
		self.assertEqual(self.student.points, 5)

	def test_general_assessment_submission_awards_points_only_first_time(self):
		first = self.client.post(
			'/api-v1/kids/submit-solution/',
			{'general_id': self.general_assessment.id, 'solution': 'First try'},
			format='multipart',
		)
		self.assertEqual(first.status_code, 200)
		self.assertEqual(first.json()['points_awarded'], 10)

		second = self.client.post(
			'/api-v1/kids/submit-solution/',
			{'general_id': self.general_assessment.id, 'solution': 'Updated try'},
			format='multipart',
		)
		self.assertEqual(second.status_code, 200)
		self.assertEqual(second.json()['points_awarded'], 0)

		self.student.refresh_from_db()
		self.assertEqual(self.student.points, 10)

	def test_lesson_assessment_submission_accumulates_with_other_actions(self):
		self.client.post('/api-v1/taken-lessons/', {'lesson': self.video_lesson.id}, format='json')
		self.client.post('/api-v1/kids/play-game/', {'game_id': self.game.id}, format='json')
		response = self.client.post(
			'/api-v1/kids/submit-solution/',
			{'lesson_id': self.lesson_assessment.id, 'solution': 'Done'},
			format='multipart',
		)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['points_awarded'], 10)

		self.student.refresh_from_db()
		self.assertEqual(self.student.points, 25)


class StudentLoginStreakTests(TestCase):
	def setUp(self):
		cache.clear()
		self.client = APIClient()
		self.user = User.objects.create_user(
			phone='231770000311',
			name='Streak Student',
			email='streak@example.com',
			password='pass',
			role=UserRole.STUDENT.value,
		)
		self.student = Student.objects.create(
			profile=self.user,
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)

	def _login_student(self):
		return self.client.post(
			'/api-v1/auth/student/',
			{'identifier': self.user.phone, 'password': 'pass'},
			format='json',
		)

	def test_first_login_starts_streak(self):
		base_day = timezone.localdate()
		with patch('api.viewsets.timezone.localdate', return_value=base_day):
			response = self._login_student()

		self.assertEqual(response.status_code, 200)
		self.student.refresh_from_db()
		self.assertEqual(self.student.current_login_streak, 1)
		self.assertEqual(self.student.max_login_streak, 1)
		self.assertEqual(self.student.last_login_activity_date, base_day)
		self.assertEqual(response.json()['student']['current_login_streak'], 1)

	def test_multiple_logins_same_day_count_once(self):
		base_day = timezone.localdate()
		with patch('api.viewsets.timezone.localdate', return_value=base_day):
			first = self._login_student()
			second = self._login_student()

		self.assertEqual(first.status_code, 200)
		self.assertEqual(second.status_code, 200)
		self.student.refresh_from_db()
		self.assertEqual(self.student.current_login_streak, 1)
		self.assertEqual(self.student.max_login_streak, 1)

	def test_consecutive_day_login_increments_streak(self):
		base_day = timezone.localdate()
		with patch('api.viewsets.timezone.localdate', return_value=base_day):
			self._login_student()
		with patch('api.viewsets.timezone.localdate', return_value=base_day + timedelta(days=1)):
			response = self._login_student()

		self.assertEqual(response.status_code, 200)
		self.student.refresh_from_db()
		self.assertEqual(self.student.current_login_streak, 2)
		self.assertEqual(self.student.max_login_streak, 2)
		self.assertEqual(response.json()['student']['current_login_streak'], 2)

	def test_gap_day_resets_current_streak_but_keeps_max(self):
		base_day = timezone.localdate()
		with patch('api.viewsets.timezone.localdate', return_value=base_day):
			self._login_student()
		with patch('api.viewsets.timezone.localdate', return_value=base_day + timedelta(days=1)):
			self._login_student()
		with patch('api.viewsets.timezone.localdate', return_value=base_day + timedelta(days=3)):
			response = self._login_student()

		self.assertEqual(response.status_code, 200)
		self.student.refresh_from_db()
		self.assertEqual(self.student.current_login_streak, 1)
		self.assertEqual(self.student.max_login_streak, 2)

	def test_non_student_login_does_not_affect_student_streak(self):
		teacher_user = User.objects.create_user(
			phone='231770000312',
			name='Teacher Login',
			email='teacherlogin@example.com',
			password='pass',
			role=UserRole.TEACHER.value,
		)
		Teacher.objects.create(profile=teacher_user, status=StatusEnum.APPROVED.value)
		response = self.client.post(
			'/api-v1/auth/content/',
			{'identifier': teacher_user.phone, 'password': 'pass'},
			format='json',
		)
		self.assertEqual(response.status_code, 200)
		self.student.refresh_from_db()
		self.assertEqual(self.student.current_login_streak, 0)
		self.assertEqual(self.student.max_login_streak, 0)


class AuthProfileSchoolInfoTests(TestCase):
	def setUp(self):
		cache.clear()
		self.client = APIClient()
		self.county = County.objects.create(name='Profile County')
		self.district = District.objects.create(county=self.county, name='Profile District')
		self.school = School.objects.create(district=self.district, name='Profile School')

		self.student_user = User.objects.create_user(
			phone='231770000321',
			name='Profile Student',
			email='profilestudent@example.com',
			password='pass',
			role=UserRole.STUDENT.value,
		)
		self.student = Student.objects.create(
			profile=self.student_user,
			school=self.school,
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)

		self.teacher_user = User.objects.create_user(
			phone='231770000322',
			name='Profile Teacher',
			email='profileteacher@example.com',
			password='pass',
			role=UserRole.TEACHER.value,
		)
		self.teacher = Teacher.objects.create(
			profile=self.teacher_user,
			school=self.school,
			status=StatusEnum.APPROVED.value,
		)

		self.head_user = User.objects.create_user(
			phone='231770000323',
			name='Profile Headteacher',
			email='profilehead@example.com',
			password='pass',
			role=UserRole.HEADTEACHER.value,
		)
		self.head_teacher = Teacher.objects.create(
			profile=self.head_user,
			school=self.school,
			status=StatusEnum.APPROVED.value,
		)

	def test_student_login_and_userprofile_include_school_info(self):
		login_resp = self.client.post(
			'/api-v1/auth/student/',
			{'identifier': self.student_user.phone, 'password': 'pass'},
			format='json',
		)
		self.assertEqual(login_resp.status_code, 200)
		student_payload = login_resp.json().get('student')
		self.assertIsNotNone(student_payload)
		self.assertEqual(student_payload['school']['id'], self.school.id)
		self.assertEqual(student_payload['school']['name'], self.school.name)
		self.assertEqual(student_payload['school']['district_id'], self.district.id)
		self.assertEqual(student_payload['school']['county_id'], self.county.id)

		self.client.force_authenticate(user=self.student_user)
		profile_resp = self.client.get('/api-v1/auth/userprofile/')
		self.assertEqual(profile_resp.status_code, 200)
		profile_student = profile_resp.json().get('student')
		self.assertIsNotNone(profile_student)
		self.assertEqual(profile_student['school']['id'], self.school.id)
		self.assertEqual(profile_student['school']['name'], self.school.name)

	def test_teacher_and_headteacher_profiles_include_school_info(self):
		teacher_login = self.client.post(
			'/api-v1/auth/content/',
			{'identifier': self.teacher_user.phone, 'password': 'pass'},
			format='json',
		)
		self.assertEqual(teacher_login.status_code, 200)
		teacher_payload = teacher_login.json().get('teacher')
		self.assertIsNotNone(teacher_payload)
		self.assertEqual(teacher_payload['school']['id'], self.school.id)
		self.assertEqual(teacher_payload['school']['name'], self.school.name)

		head_login = self.client.post(
			'/api-v1/auth/content/',
			{'identifier': self.head_user.phone, 'password': 'pass'},
			format='json',
		)
		self.assertEqual(head_login.status_code, 200)
		head_payload = head_login.json().get('teacher')
		self.assertIsNotNone(head_payload)
		self.assertEqual(head_payload['school']['id'], self.school.id)

		self.client.force_authenticate(user=self.teacher_user)
		teacher_profile = self.client.get('/api-v1/auth/userprofile/')
		self.assertEqual(teacher_profile.status_code, 200)
		self.assertEqual(teacher_profile.json()['teacher']['school']['id'], self.school.id)

		self.client.force_authenticate(user=self.head_user)
		head_profile = self.client.get('/api-v1/auth/userprofile/')
		self.assertEqual(head_profile.status_code, 200)
		self.assertEqual(head_profile.json()['teacher']['school']['id'], self.school.id)


class MakeHeadmasterEndpointTests(TestCase):
	def setUp(self):
		self.client = APIClient()
		self.county = County.objects.create(name='MakeHead County')
		self.district = District.objects.create(county=self.county, name='MakeHead District')
		self.school = School.objects.create(district=self.district, name='MakeHead School')

		self.teacher_user = User.objects.create_user(
			phone='231770009001',
			name='Promoted Teacher',
			email='promoted.teacher@example.com',
			password='pass',
			role=UserRole.TEACHER.value,
		)
		self.teacher = Teacher.objects.create(
			profile=self.teacher_user,
			school=self.school,
			status=StatusEnum.APPROVED.value,
		)

		self.admin_user = User.objects.create_user(
			phone='231770009002',
			name='Admin User',
			email='admin.user@example.com',
			password='pass',
			role=UserRole.ADMIN.value,
			is_staff=True,
			is_superuser=True,
		)

		self.validator_user = User.objects.create_user(
			phone='231770009003',
			name='Validator User',
			email='validator.user@example.com',
			password='pass',
			role=UserRole.CONTENTVALIDATOR.value,
			is_staff=True,
			is_superuser=False,
		)

		self.regular_teacher_user = User.objects.create_user(
			phone='231770009004',
			name='Regular Teacher User',
			email='regular.teacher@example.com',
			password='pass',
			role=UserRole.TEACHER.value,
		)
		Teacher.objects.create(
			profile=self.regular_teacher_user,
			school=self.school,
			status=StatusEnum.APPROVED.value,
		)

	def test_admin_can_promote_teacher_to_headteacher(self):
		self.client.force_authenticate(user=self.admin_user)
		resp = self.client.post(
			'/api-v1/admin/teachers/makeheadmaster/',
			{'teacher_id': self.teacher.id},
			format='json',
		)
		self.assertEqual(resp.status_code, 200)
		self.teacher_user.refresh_from_db()
		self.assertEqual(self.teacher_user.role, UserRole.HEADTEACHER.value)

	def test_validator_can_promote_teacher_to_headteacher(self):
		self.client.force_authenticate(user=self.validator_user)
		resp = self.client.post(
			'/api-v1/admin/teachers/makeheadmaster/',
			{'teacher_id': self.teacher.id},
			format='json',
		)
		self.assertEqual(resp.status_code, 200)
		self.teacher_user.refresh_from_db()
		self.assertEqual(self.teacher_user.role, UserRole.HEADTEACHER.value)

	def test_regular_teacher_cannot_promote_teacher(self):
		self.client.force_authenticate(user=self.regular_teacher_user)
		resp = self.client.post(
			'/api-v1/admin/teachers/makeheadmaster/',
			{'teacher_id': self.teacher.id},
			format='json',
		)
		self.assertIn(resp.status_code, (401, 403))
		self.teacher_user.refresh_from_db()
		self.assertEqual(self.teacher_user.role, UserRole.TEACHER.value)


class HeadTeacherViewSetIsolationTests(TestCase):
	def setUp(self):
		self.client = APIClient()
		self.headteacher_notify_patcher = patch('api.headteacher_viewset.fire_and_forget')
		self.viewset_notify_patcher = patch('api.viewsets.fire_and_forget')
		self.headteacher_notify_patcher.start()
		self.viewset_notify_patcher.start()

		county = County.objects.create(name='Montserrado')
		district = District.objects.create(county=county, name='Careysburg')
		self.school_one = School.objects.create(district=district, name='School One')
		self.school_two = School.objects.create(district=district, name='School Two')

		self.head_user = User.objects.create_user(
			phone='231770001001',
			name='Head Teacher One',
			email='head1@example.com',
			password='pass',
			role=UserRole.HEADTEACHER.value,
		)
		self.head_teacher = Teacher.objects.create(
			profile=self.head_user,
			school=self.school_one,
			status=StatusEnum.APPROVED.value,
		)

		self.school_one_teacher_user = User.objects.create_user(
			phone='231770001002',
			name='Teacher School One',
			email='teacher1@example.com',
			password='pass',
			role=UserRole.TEACHER.value,
		)
		self.school_one_teacher = Teacher.objects.create(
			profile=self.school_one_teacher_user,
			school=self.school_one,
			status=StatusEnum.APPROVED.value,
		)

		self.school_two_teacher_user = User.objects.create_user(
			phone='231770001003',
			name='Teacher School Two',
			email='teacher2@example.com',
			password='pass',
			role=UserRole.TEACHER.value,
		)
		self.school_two_teacher = Teacher.objects.create(
			profile=self.school_two_teacher_user,
			school=self.school_two,
			status=StatusEnum.APPROVED.value,
		)

		self.school_one_subject = Subject.objects.create(
			name='Mathematics',
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)
		self.school_one_subject.teachers.add(self.school_one_teacher)

		self.school_two_subject = Subject.objects.create(
			name='Science',
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)
		self.school_two_subject.teachers.add(self.school_two_teacher)

		self.school_one_student_user = User.objects.create_user(
			phone='231770001006',
			name='Student School One',
			email='student1@schoolone.com',
			password='pass',
			role=UserRole.STUDENT.value,
		)
		self.school_one_student = Student.objects.create(
			profile=self.school_one_student_user,
			school=self.school_one,
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)

		self.school_two_student_user = User.objects.create_user(
			phone='231770001007',
			name='Student School Two',
			email='student2@schooltwo.com',
			password='pass',
			role=UserRole.STUDENT.value,
		)
		self.school_two_student = Student.objects.create(
			profile=self.school_two_student_user,
			school=self.school_two,
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)

		self.school_one_assessment = GeneralAssessment.objects.create(
			title='School One Assessment',
			given_by=self.school_one_teacher,
			marks=20,
		)
		self.school_two_assessment = GeneralAssessment.objects.create(
			title='School Two Assessment',
			given_by=self.school_two_teacher,
			marks=20,
		)

		resource_file = lambda name: SimpleUploadedFile(name, b'lesson-bytes', content_type='video/mp4')
		self.period = Period.objects.create(name='March', start_month=3, end_month=3)
		self.school_one_lesson = LessonResource.objects.create(
			subject=self.school_one_subject,
			period=self.period,
			title='School One Lesson',
			type=ContentType.VIDEO.value,
			status=StatusEnum.APPROVED.value,
			resource=resource_file('school-one-lesson.mp4'),
		)
		self.school_two_lesson = LessonResource.objects.create(
			subject=self.school_two_subject,
			period=self.period,
			title='School Two Lesson',
			type=ContentType.VIDEO.value,
			status=StatusEnum.APPROVED.value,
			resource=resource_file('school-two-lesson.mp4'),
		)
		self.school_one_lesson_assessment = LessonAssessment.objects.create(
			lesson=self.school_one_lesson,
			title='School One Lesson Assessment',
			type=AssessmentType.QUIZ.value,
			marks=10,
			given_by=self.school_one_teacher,
		)
		self.school_two_lesson_assessment = LessonAssessment.objects.create(
			lesson=self.school_two_lesson,
			title='School Two Lesson Assessment',
			type=AssessmentType.QUIZ.value,
			marks=10,
			given_by=self.school_two_teacher,
		)

		self.school_one_solution = AssessmentSolution.objects.create(
			assessment=self.school_one_assessment,
			student=self.school_one_student,
			solution='School one solution',
			attachment=SimpleUploadedFile('school-one-solution.txt', b'solution one', content_type='text/plain'),
		)
		self.school_two_solution = AssessmentSolution.objects.create(
			assessment=self.school_two_assessment,
			student=self.school_two_student,
			solution='School two solution',
			attachment=SimpleUploadedFile('school-two-solution.txt', b'solution two', content_type='text/plain'),
		)

		LessonAssessmentGrade.objects.create(
			lesson_assessment=self.school_one_lesson_assessment,
			student=self.school_one_student,
			score=7,
		)
		LessonAssessmentGrade.objects.create(
			lesson_assessment=self.school_two_lesson_assessment,
			student=self.school_two_student,
			score=8,
		)

		self.client.force_authenticate(user=self.head_user)

	def tearDown(self):
		self.headteacher_notify_patcher.stop()
		self.viewset_notify_patcher.stop()

	def test_headteacher_lists_only_teachers_in_own_school(self):
		resp = self.client.get('/api-v1/headteacher/teachers/')
		self.assertEqual(resp.status_code, 200)
		payload = resp.json()
		returned_ids = {item['id'] for item in payload}
		self.assertIn(self.head_teacher.id, returned_ids)
		self.assertIn(self.school_one_teacher.id, returned_ids)
		self.assertNotIn(self.school_two_teacher.id, returned_ids)

	def test_headteacher_lists_only_school_subjects(self):
		resp = self.client.get('/api-v1/headteacher/subjects/')
		self.assertEqual(resp.status_code, 200)
		payload = resp.json()
		returned_ids = {item['id'] for item in payload}
		self.assertIn(self.school_one_subject.id, returned_ids)
		self.assertNotIn(self.school_two_subject.id, returned_ids)

	def test_headteacher_lists_only_school_general_assessments(self):
		resp = self.client.get('/api-v1/headteacher/general-assessments/')
		self.assertEqual(resp.status_code, 200)
		titles = {item['title'] for item in resp.json()}
		self.assertIn('School One Assessment', titles)
		self.assertNotIn('School Two Assessment', titles)

	def test_headteacher_cannot_grade_general_assessment_from_other_school(self):
		resp = self.client.post(
			'/api-v1/headteacher/grade/general/',
			{
				'assessment_id': self.school_two_assessment.id,
				'student_id': self.school_one_student.id,
				'score': 10,
			},
			format='json',
		)
		self.assertEqual(resp.status_code, 404)

	def test_headteacher_cannot_grade_student_from_other_school(self):
		resp = self.client.post(
			'/api-v1/headteacher/grade/general/',
			{
				'assessment_id': self.school_one_assessment.id,
				'student_id': self.school_two_student.id,
				'score': 10,
			},
			format='json',
		)
		self.assertEqual(resp.status_code, 404)

	def test_headteacher_cannot_grade_lesson_assessment_from_other_school(self):
		resp = self.client.post(
			'/api-v1/headteacher/grade/lesson/',
			{
				'assessment_id': self.school_two_lesson_assessment.id,
				'student_id': self.school_one_student.id,
				'score': 8,
			},
			format='json',
		)
		self.assertEqual(resp.status_code, 404)

	def test_headteacher_submissions_excludes_other_school(self):
		resp = self.client.get('/api-v1/headteacher/submissions/')
		self.assertEqual(resp.status_code, 200)
		submissions = resp.json()['submissions']
		titles = {item['assessment_title'] for item in submissions}
		self.assertIn('School One Assessment', titles)
		self.assertIn('School One Lesson Assessment', titles)
		self.assertNotIn('School Two Assessment', titles)
		self.assertNotIn('School Two Lesson Assessment', titles)

	def test_headteacher_create_teacher_forces_own_school(self):
		resp = self.client.post(
			'/api-v1/headteacher/teachers/create/',
			{
				'name': 'New Teacher',
				'phone': '231770001004',
				'email': 'newteacher@example.com',
				'school_id': self.school_two.id,
			},
			format='json',
		)
		self.assertEqual(resp.status_code, 201)
		created = Teacher.objects.get(id=resp.json()['id'])
		self.assertEqual(created.school_id, self.school_one.id)

	def test_headteacher_cannot_create_student_in_other_school(self):
		resp = self.client.post(
			'/api-v1/headteacher/students/create/',
			{
				'name': 'Student Cross School',
				'phone': '231770001005',
				'email': 'studentcross@example.com',
				'school_id': self.school_two.id,
			},
			format='json',
		)
		self.assertEqual(resp.status_code, 403)
		self.assertEqual(Student.objects.filter(profile__phone='231770001005').count(), 0)

	def test_regular_teacher_gets_403_on_headteacher_route(self):
		self.client.force_authenticate(user=self.school_one_teacher_user)
		resp = self.client.get('/api-v1/headteacher/teachers/')
		self.assertEqual(resp.status_code, 403)


class LeaderboardScopeTests(TestCase):
	def setUp(self):
		cache.clear()
		self.client = APIClient()

		self.admin_user = User.objects.create_user(
			phone='231770009900',
			name='Admin Leaderboard',
			email='adminleaderboard@example.com',
			password='pass',
			role=UserRole.ADMIN.value,
		)
		self.admin_user.is_staff = True
		self.admin_user.is_superuser = True
		self.admin_user.save(update_fields=['is_staff', 'is_superuser'])

		county_one = County.objects.create(name='Montserrado')
		county_two = County.objects.create(name='Bong')
		district_one = District.objects.create(county=county_one, name='Careysburg')
		district_two = District.objects.create(county=county_one, name='Todee')
		district_three = District.objects.create(county=county_two, name='Gbarnga')

		self.school_one = School.objects.create(district=district_one, name='Alpha School')
		self.school_two = School.objects.create(district=district_two, name='Beta School')
		self.school_three = School.objects.create(district=district_three, name='Gamma School')

		self.teacher_user = User.objects.create_user(
			phone='231770009901',
			name='Teacher Leaderboard',
			email='teacherleaderboard@example.com',
			password='pass',
			role=UserRole.TEACHER.value,
		)
		self.teacher = Teacher.objects.create(
			profile=self.teacher_user,
			school=self.school_one,
			status=StatusEnum.APPROVED.value,
		)

		self.head_user = User.objects.create_user(
			phone='231770009902',
			name='Head Leaderboard',
			email='headleaderboard@example.com',
			password='pass',
			role=UserRole.HEADTEACHER.value,
		)
		self.head_teacher = Teacher.objects.create(
			profile=self.head_user,
			school=self.school_one,
			status=StatusEnum.APPROVED.value,
		)

		self.subject_grade3 = Subject.objects.create(
			name='Math Grade 3',
			grade=StudentLevel.GRADE3.value,
			status=StatusEnum.APPROVED.value,
		)
		self.subject_grade3.teachers.add(self.teacher)

		def create_student(name, phone, school, grade, points):
			user = User.objects.create_user(
				phone=phone,
				name=name,
				email=f'{phone}@example.com',
				password='pass',
				role=UserRole.STUDENT.value,
			)
			return Student.objects.create(
				profile=user,
				school=school,
				grade=grade,
				points=points,
				status=StatusEnum.APPROVED.value,
			)

		self.class_top = create_student('Class Top', '231770009911', self.school_one, StudentLevel.GRADE3.value, 40)
		self.class_second = create_student('Class Second', '231770009912', self.school_one, StudentLevel.GRADE3.value, 20)
		self.same_school_other_grade = create_student('Other Grade', '231770009913', self.school_one, StudentLevel.GRADE4.value, 35)
		self.other_school_same_county = create_student('Other School County', '231770009914', self.school_two, StudentLevel.GRADE3.value, 50)
		self.other_county = create_student('Other County', '231770009915', self.school_three, StudentLevel.GRADE3.value, 60)
		self.class_top.current_login_streak = 4
		self.class_top.max_login_streak = 7
		self.class_top.save(update_fields=['current_login_streak', 'max_login_streak'])

		self.parent_user = User.objects.create_user(
			phone='231770009916',
			name='Parent Leaderboard',
			email='parentleaderboard@example.com',
			password='pass',
			role=UserRole.PARENT.value,
		)
		self.parent_profile = Parent.objects.create(profile=self.parent_user)
		self.parent_profile.wards.add(self.class_top, self.other_county)

		self.recent_points_activity = Activity.objects.create(
			user=self.class_second.profile,
			type='manual_points_recent',
			description='Recent points for timeframe test',
			metadata={'points_awarded': 30},
		)
		old_activity = Activity.objects.create(
			user=self.class_top.profile,
			type='manual_points_old',
			description='Old points for timeframe test',
			metadata={'points_awarded': 50},
		)
		Activity.objects.filter(pk=old_activity.pk).update(created_at=timezone.now() - timedelta(days=40))

	def test_teacher_leaderboard_is_limited_to_their_class(self):
		self.client.force_authenticate(user=self.teacher_user)
		resp = self.client.get('/api-v1/teacher/leaderboard/')
		self.assertEqual(resp.status_code, 200)
		payload = resp.json()
		self.assertEqual(payload['scope']['kind'], 'class')
		self.assertEqual(payload['scope']['grades'], [StudentLevel.GRADE3.value])
		returned_ids = [item['student_db_id'] for item in payload['leaderboard']]
		self.assertEqual(returned_ids, [self.class_top.id, self.class_second.id])
		self.assertEqual(payload['leaderboard'][0]['rank'], 1)
		self.assertEqual(payload['leaderboard'][1]['rank'], 2)
		self.assertIn('current_login_streak', payload['leaderboard'][0])

	def test_headteacher_leaderboard_is_school_scoped(self):
		self.client.force_authenticate(user=self.head_user)
		resp = self.client.get('/api-v1/headteacher/leaderboard/')
		self.assertEqual(resp.status_code, 200)
		payload = resp.json()
		self.assertEqual(payload['scope']['kind'], 'school')
		returned_ids = [item['student_db_id'] for item in payload['leaderboard']]
		self.assertEqual(returned_ids, [self.class_top.id, self.same_school_other_grade.id, self.class_second.id])

	def test_admin_leaderboard_is_national_by_default(self):
		self.client.force_authenticate(user=self.admin_user)
		resp = self.client.get('/api-v1/admin/dashboard/leaderboard/')
		self.assertEqual(resp.status_code, 200)
		payload = resp.json()
		returned_ids = [item['student_db_id'] for item in payload['leaderboard']]
		self.assertEqual(returned_ids[:5], [self.other_county.id, self.other_school_same_county.id, self.class_top.id, self.same_school_other_grade.id, self.class_second.id])
		self.assertIn('current_login_streak', payload['leaderboard'][0])

	def test_admin_leaderboard_filters_by_county(self):
		self.client.force_authenticate(user=self.admin_user)
		resp = self.client.get(f'/api-v1/admin/dashboard/leaderboard/?county_id={self.school_one.district.county_id}')
		self.assertEqual(resp.status_code, 200)
		returned_ids = [item['student_db_id'] for item in resp.json()['leaderboard']]
		self.assertEqual(returned_ids, [self.other_school_same_county.id, self.class_top.id, self.same_school_other_grade.id, self.class_second.id])

	def test_admin_leaderboard_filters_by_district(self):
		self.client.force_authenticate(user=self.admin_user)
		resp = self.client.get(f'/api-v1/admin/dashboard/leaderboard/?district_id={self.school_one.district_id}')
		self.assertEqual(resp.status_code, 200)
		returned_ids = [item['student_db_id'] for item in resp.json()['leaderboard']]
		self.assertEqual(returned_ids, [self.class_top.id, self.same_school_other_grade.id, self.class_second.id])

	def test_admin_leaderboard_filters_by_school(self):
		self.client.force_authenticate(user=self.admin_user)
		resp = self.client.get(f'/api-v1/admin/dashboard/leaderboard/?school_id={self.school_two.id}')
		self.assertEqual(resp.status_code, 200)
		returned_ids = [item['student_db_id'] for item in resp.json()['leaderboard']]
		self.assertEqual(returned_ids, [self.other_school_same_county.id])

	def test_teacher_leaderboard_supports_timeframe_filter(self):
		self.client.force_authenticate(user=self.teacher_user)
		resp = self.client.get('/api-v1/teacher/leaderboard/?timeframe=this_month')
		self.assertEqual(resp.status_code, 200)
		payload = resp.json()
		self.assertEqual(payload['scope']['timeframe'], 'this_month')
		returned_ids = [item['student_db_id'] for item in payload['leaderboard']]
		self.assertEqual(returned_ids, [self.class_second.id, self.class_top.id])

	def test_headteacher_leaderboard_supports_timeframe_filter(self):
		self.client.force_authenticate(user=self.head_user)
		resp = self.client.get('/api-v1/headteacher/leaderboard/?timeframe=this_week')
		self.assertEqual(resp.status_code, 200)
		payload = resp.json()
		self.assertEqual(payload['scope']['timeframe'], 'this_week')
		self.assertEqual(payload['leaderboard'][0]['student_db_id'], self.class_second.id)

	def test_admin_leaderboard_supports_timeframe_filter(self):
		self.client.force_authenticate(user=self.admin_user)
		resp = self.client.get('/api-v1/admin/dashboard/leaderboard/?timeframe=this_week')
		self.assertEqual(resp.status_code, 200)
		payload = resp.json()
		self.assertEqual(payload['scope']['timeframe'], 'this_week')
		self.assertEqual(payload['leaderboard'][0]['student_db_id'], self.class_second.id)
		self.assertEqual(payload['leaderboard'][0]['points'], 30)

	def test_parent_leaderboard_returns_children_ranking_context(self):
		self.client.force_authenticate(user=self.parent_user)
		resp = self.client.get('/api-v1/parent/leaderboard/?timeframe=all_time')
		self.assertEqual(resp.status_code, 200)
		payload = resp.json()
		self.assertEqual(payload['timeframe'], 'all_time')
		self.assertEqual(len(payload['children']), 2)

		first_context = payload['children'][0]
		self.assertIn('child', first_context)
		self.assertIn('rank', first_context)
		self.assertIn('leaderboard_context', first_context)
		self.assertEqual(first_context['scope']['kind'], 'school_grade')
		self.assertIn('current_login_streak', first_context)
		if first_context['leaderboard_context']:
			self.assertIn('current_login_streak', first_context['leaderboard_context'][0])

	def test_parent_leaderboard_supports_timeframe_filter(self):
		self.client.force_authenticate(user=self.parent_user)
		resp = self.client.get('/api-v1/parent/leaderboard/?timeframe=this_month')
		self.assertEqual(resp.status_code, 200)
		payload = resp.json()
		self.assertEqual(payload['timeframe'], 'this_month')
		for child_ctx in payload['children']:
			self.assertEqual(child_ctx['scope']['timeframe'], 'this_month')
