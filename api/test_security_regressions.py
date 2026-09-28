from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import serializers
from rest_framework.test import APIClient

from accounts.models import County, District, Parent, School, Student, Teacher, User
from accounts.security import assign_temporary_password
from content.models import (
    Activity,
    AssessmentSolution,
    GameModel,
    GeneralAssessment,
    LessonAssessment,
    LessonResource,
    Period,
    Question,
    Subject,
)
from elearncore.sysutils.constants import (
    AssessmentType,
    ContentType,
    QType,
    Status,
    StudentLevel,
    UserRole,
)

from .uploads import build_bulk_identity_index, claim_bulk_identity, validate_solution_upload


def create_school(name: str) -> School:
    county = County.objects.create(name=f'{name} County', status=Status.APPROVED.value)
    district = District.objects.create(county=county, name=f'{name} District', status=Status.APPROVED.value)
    return School.objects.create(district=district, name=name, status=Status.APPROVED.value)


class PublicContentSecurityTests(TestCase):
    def test_public_games_hide_drafts_and_answers(self):
        approved = GameModel.objects.create(
            name='Approved Game',
            type='WORD_PUZZLE',
            grade=StudentLevel.GRADE3.value,
            correct_answer='secret-answer',
            status=Status.APPROVED.value,
        )
        GameModel.objects.create(
            name='Draft Game',
            type='WORD_PUZZLE',
            grade=StudentLevel.GRADE3.value,
            correct_answer='draft-secret',
            status=Status.DRAFT.value,
        )

        response = APIClient().get('/api-v1/games/')

        self.assertEqual(response.status_code, 200)
        items = response.json()['results']
        self.assertEqual([item['id'] for item in items], [approved.id])
        self.assertNotIn('correct_answer', items[0])
        self.assertNotIn('status', items[0])


@override_settings(SELF_SERVICE_REGISTRATION_ENABLED=False)
class SelfServiceRegistrationSecurityTests(TestCase):
    def test_public_profile_setup_cannot_create_an_account(self):
        response = APIClient().post(
            '/api-v1/onboarding/profilesetup/',
            {
                'name': 'Unapproved Signup',
                'email': 'signup@example.com',
                'phone': '231770819999',
                'password': 'NotAllowed-42',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()['detail'],
            'Self-service registration is currently disabled.',
        )
        self.assertFalse(User.objects.filter(email='signup@example.com').exists())


class TeacherApprovalSecurityTests(TestCase):
    def test_pending_teacher_cannot_use_teacher_endpoints(self):
        school = create_school('Pending Teacher School')
        user = User.objects.create(
            phone='231770810001',
            name='Pending Teacher',
            role=UserRole.TEACHER.value,
        )
        Teacher.objects.create(profile=user, school=school, status=Status.PENDING.value)
        client = APIClient()
        client.force_authenticate(user=user)

        response = client.get('/api-v1/teacher/subjects/')

        self.assertEqual(response.status_code, 403)


class ParentLinkSecurityTests(TestCase):
    def test_linking_child_does_not_change_school_or_grade(self):
        original_school = create_school('Original School')
        other_school = create_school('Other School')
        child_user = User.objects.create(
            phone='231770810010',
            email='child.security@example.com',
            name='Child',
            role=UserRole.STUDENT.value,
        )
        child = Student.objects.create(
            profile=child_user,
            school=original_school,
            grade=StudentLevel.GRADE3.value,
            status=Status.APPROVED.value,
        )
        parent_user = User.objects.create(
            phone='231770810011',
            name='Parent',
            role=UserRole.PARENT.value,
        )
        Parent.objects.create(profile=parent_user)
        client = APIClient()
        client.force_authenticate(user=parent_user)

        response = client.post(
            '/api-v1/parent/linkchild/',
            {
                'student_id': child.id,
                'student_email': child_user.email,
                'school_id': other_school.id,
                'grade': StudentLevel.GRADE5.value,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        child.refresh_from_db()
        self.assertEqual(child.school_id, original_school.id)
        self.assertEqual(child.grade, StudentLevel.GRADE3.value)


class AssessmentEligibilitySecurityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(
            phone='231770810020',
            name='Eligible Student',
            role=UserRole.STUDENT.value,
        )
        self.student = Student.objects.create(
            profile=self.user,
            grade=StudentLevel.GRADE3.value,
            status=Status.APPROVED.value,
        )
        other_user = User.objects.create(
            phone='231770810021',
            name='Other Student',
            role=UserRole.STUDENT.value,
        )
        self.other_student = Student.objects.create(
            profile=other_user,
            grade=StudentLevel.GRADE3.value,
            status=Status.APPROVED.value,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        subject = Subject.objects.create(
            name='Eligibility Math',
            grade=StudentLevel.GRADE3.value,
            status=Status.APPROVED.value,
        )
        period = Period.objects.create(name='Eligibility Period', start_month=1, end_month=1)
        self.first_lesson = LessonResource.objects.create(
            subject=subject,
            period=period,
            title='A First Lesson',
            type=ContentType.VIDEO.value,
            status=Status.APPROVED.value,
            resource=SimpleUploadedFile('first.mp4', b'video', content_type='video/mp4'),
        )
        second_lesson = LessonResource.objects.create(
            subject=subject,
            period=period,
            title='B Locked Lesson',
            type=ContentType.VIDEO.value,
            status=Status.APPROVED.value,
            resource=SimpleUploadedFile('locked.mp4', b'video', content_type='video/mp4'),
        )
        self.locked_assessment = LessonAssessment.objects.create(
            lesson=second_lesson,
            title='Locked Assessment',
            type=AssessmentType.QUIZ.value,
            status=Status.APPROVED.value,
        )
        self.pending = GeneralAssessment.objects.create(
            title='Pending Assessment',
            status=Status.PENDING.value,
            grade=StudentLevel.GRADE3.value,
        )
        self.other_grade = GeneralAssessment.objects.create(
            title='Other Grade Assessment',
            status=Status.APPROVED.value,
            grade=StudentLevel.GRADE5.value,
        )
        self.targeted_other = GeneralAssessment.objects.create(
            title='Targeted Other Assessment',
            status=Status.APPROVED.value,
            grade=StudentLevel.GRADE3.value,
            is_targeted=True,
            target_student=self.other_student,
        )
        self.available = GeneralAssessment.objects.create(
            title='Available Assessment',
            status=Status.APPROVED.value,
            grade=StudentLevel.GRADE3.value,
        )
        Question.objects.create(
            general_assessment=self.available,
            type=QType.SHORT_ANSWER.value,
            question='What is two plus two?',
            answer='4',
        )

    def test_questions_reject_ineligible_assessments_and_hide_answers(self):
        for assessment in (self.pending, self.other_grade, self.targeted_other):
            with self.subTest(assessment=assessment.title):
                response = self.client.get(
                    '/api-v1/kids/assessment-questions/',
                    {'general_id': assessment.id},
                )
                self.assertEqual(response.status_code, 404)

        locked = self.client.get(
            '/api-v1/kids/assessment-questions/',
            {'lesson_id': self.locked_assessment.id},
        )
        self.assertEqual(locked.status_code, 404)

        available = self.client.get(
            '/api-v1/kids/assessment-questions/',
            {'general_id': self.available.id},
        )
        self.assertEqual(available.status_code, 200)
        self.assertNotIn('answer', available.json()['questions'][0])

    def test_submission_rejects_ineligible_assessments_without_awarding_points(self):
        for assessment in (self.pending, self.other_grade, self.targeted_other):
            with self.subTest(assessment=assessment.title):
                response = self.client.post(
                    '/api-v1/kids/submit-solution/',
                    {'general_id': assessment.id, 'solution': 'out of scope'},
                    format='json',
                )
                self.assertEqual(response.status_code, 404)

        locked = self.client.post(
            '/api-v1/kids/submit-solution/',
            {'lesson_id': self.locked_assessment.id, 'solution': 'out of scope'},
            format='json',
        )
        self.assertEqual(locked.status_code, 404)
        self.student.refresh_from_db()
        self.assertEqual(self.student.points, 0)
        self.assertFalse(AssessmentSolution.objects.filter(student=self.student).exists())


class SyncSecurityTests(TestCase):
    def test_sync_is_school_scoped_and_audited(self):
        school = create_school('Sync School')
        other_school = create_school('Outside Sync School')
        sync_user = User.objects.create(
            phone='231770810030',
            name='School Sync Service',
            role=UserRole.SYNC_SERVICE.value,
            sync_school=school,
        )
        in_scope_user = User.objects.create(
            phone='231770810031',
            name='In Scope',
            role=UserRole.STUDENT.value,
        )
        Student.objects.create(profile=in_scope_user, school=school, status=Status.APPROVED.value)
        out_scope_user = User.objects.create(
            phone='231770810032',
            name='Out of Scope',
            role=UserRole.STUDENT.value,
        )
        Student.objects.create(profile=out_scope_user, school=other_school, status=Status.APPROVED.value)
        client = APIClient()
        client.force_authenticate(user=sync_user)

        response = client.get('/api-v1/sync/student-users/')

        self.assertEqual(response.status_code, 200)
        returned = {item['sync_uuid'] for item in response.json()['items']}
        self.assertIn(str(in_scope_user.sync_uuid), returned)
        self.assertNotIn(str(out_scope_user.sync_uuid), returned)
        audit = Activity.objects.get(user=sync_user, type='sync_api_access')
        self.assertEqual(audit.metadata['school_id'], school.id)
        self.assertEqual(audit.metadata['path'], '/api-v1/sync/student-users/')

    def test_human_admin_account_cannot_use_sync(self):
        admin = User.objects.create(
            phone='231770810033',
            name='Human Admin',
            role=UserRole.ADMIN.value,
            is_staff=True,
        )
        client = APIClient()
        client.force_authenticate(user=admin)

        response = client.get('/api-v1/sync/subjects/')

        self.assertEqual(response.status_code, 403)


class CredentialAndUploadSecurityTests(TestCase):
    def test_bulk_identity_check_uses_one_query_and_detects_duplicates(self):
        User.objects.create(
            phone='231770810039',
            email='Existing@Example.com',
            name='Existing User',
        )
        rows = [
            {'phone': '231770810039', 'email': 'existing@example.com'},
            {'phone': '231770810042', 'email': 'new@example.com'},
            {'phone': '231770810042', 'email': 'other@example.com'},
        ]

        with self.assertNumQueries(1):
            index = build_bulk_identity_index(rows)

        self.assertIn('phone', claim_bulk_identity(index, rows[0]['phone'], rows[0]['email']))
        self.assertIsNone(claim_bulk_identity(index, rows[1]['phone'], rows[1]['email']))
        self.assertIn('phone', claim_bulk_identity(index, rows[2]['phone'], rows[2]['email']))

    def test_temporary_passwords_are_unique_and_expiring(self):
        first = User(phone='231770810040', name='First')
        second = User(phone='231770810041', name='Second')

        first_password = assign_temporary_password(first)
        second_password = assign_temporary_password(second)

        self.assertNotEqual(first_password, second_password)
        self.assertTrue(first.check_password(first_password))
        self.assertTrue(second.check_password(second_password))
        self.assertTrue(first.must_change_password)
        self.assertGreater(first.temporary_password_expires_at, timezone.now())
        self.assertLess(first.temporary_password_expires_at, timezone.now() + timedelta(hours=25))

    @override_settings(MAX_DOCUMENT_UPLOAD_BYTES=4)
    def test_solution_upload_size_limit_is_enforced(self):
        upload = SimpleUploadedFile('answer.txt', b'12345', content_type='text/plain')

        with self.assertRaises(serializers.ValidationError):
            validate_solution_upload(upload)
