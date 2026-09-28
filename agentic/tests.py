from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from accounts.models import Student, User
from agentic.models import AIRecommendation
from agentic.services import _create_question_from_ai, _load_lesson_candidates, _match_lesson
from content.models import GeneralAssessment, LessonResource, Option, Subject, Topic
from elearncore.sysutils.constants import ContentType, QType, StudentLevel, UserRole

class AIRecommendationTests(TestCase):
    def test_string_uses_student_profile(self):
        user = User.objects.create(
            phone='231770820001',
            name='Recommendation Student',
            role=UserRole.STUDENT.value,
        )
        student = Student.objects.create(profile=user)
        subject = Subject.objects.create(name='Math', grade=StudentLevel.GRADE3.value)
        lesson = LessonResource.objects.create(
            subject=subject,
            title='Fractions',
            type=ContentType.VIDEO.value,
            resource=SimpleUploadedFile('fractions.mp4', b'video', content_type='video/mp4'),
        )
        recommendation = AIRecommendation.objects.create(
            student=student,
            lesson=lesson,
            message='Practice fractions',
        )

        self.assertIn('Recommendation Student', str(recommendation))


class AIQueryOptimizationTests(TestCase):
    def setUp(self):
        self.subject = Subject.objects.create(name='Science', grade=StudentLevel.GRADE3.value)
        self.topic = Topic.objects.create(subject=self.subject, name='Plants')
        self.lesson = LessonResource.objects.create(
            subject=self.subject,
            topic=self.topic,
            title='How Plants Grow',
            type=ContentType.VIDEO.value,
            resource='lesson_resources/plants.mp4',
        )

    def test_generated_lesson_matches_use_one_batched_query(self):
        items = [
            {'subject': 'Science', 'topic': 'Plants', 'lesson_title': f'Plants {index}'}
            for index in range(5)
        ]
        with CaptureQueriesContext(connection) as load_queries:
            candidates = _load_lesson_candidates(items)
        self.assertEqual(len(load_queries), 1)

        with CaptureQueriesContext(connection) as match_queries:
            matches = [
                _match_lesson('Science', None, None, candidates=candidates)
                for _ in items
            ]
        self.assertEqual(len(match_queries), 0)
        self.assertTrue(all(match == self.lesson for match in matches))

    def test_ai_question_options_are_inserted_in_bulk(self):
        assessment = GeneralAssessment.objects.create(title='Science Check', marks=5)
        question = _create_question_from_ai(
            {
                'type': QType.MULTIPLE_CHOICE.value,
                'prompt': 'Which part absorbs water?',
                'answer': 'Roots',
                'options': ['Roots', 'Stem', 'Leaf', 'Flower'],
            },
            lesson_assessment=None,
            general_assessment=assessment,
        )
        self.assertIsNotNone(question)
        self.assertEqual(Option.objects.filter(question=question).count(), 4)
