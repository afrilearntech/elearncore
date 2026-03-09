from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User, Student, County, District, School
from content.models import Subject, Period, LessonResource, LessonAssessment, TakeLesson, LessonAssessmentSolution
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
