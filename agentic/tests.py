from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from accounts.models import Student, User
from agentic.models import AIRecommendation
from content.models import LessonResource, Subject
from elearncore.sysutils.constants import ContentType, StudentLevel, UserRole

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
