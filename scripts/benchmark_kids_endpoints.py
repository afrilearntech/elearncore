import os
import sys
import time
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "elearncore.settings")

import django  # noqa: E402

django.setup()

from django.core.cache import cache  # noqa: E402
from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.db import connection  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from django.utils import timezone  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from accounts.models import Student, User  # noqa: E402
from content.models import (  # noqa: E402
    AssessmentSolution,
    GeneralAssessment,
    LessonAssessment,
    LessonResource,
    Period,
    Subject,
    TakeLesson,
)
from elearncore.sysutils.constants import (  # noqa: E402
    AssessmentType,
    ContentType,
    Status as StatusEnum,
    StudentLevel,
    UserRole,
)


def main() -> None:
    connection.force_debug_cursor = True

    suffix = str(int(time.time() * 1000))
    phone = "23177" + suffix[-7:]

    user = User.objects.create_user(
        phone=phone,
        name="Metrics Student",
        email=f"metrics_{suffix}@example.com",
        password="pass",
        role=UserRole.STUDENT.value,
    )
    student = Student.objects.create(
        profile=user,
        grade=StudentLevel.GRADE3.value,
        status=StatusEnum.APPROVED.value,
    )

    subject_math = Subject.objects.create(
        name=f"Math Metrics {suffix}",
        grade=StudentLevel.GRADE3.value,
        status=StatusEnum.APPROVED.value,
    )
    subject_science = Subject.objects.create(
        name=f"Science Metrics {suffix}",
        grade=StudentLevel.GRADE3.value,
        status=StatusEnum.APPROVED.value,
    )
    period = Period.objects.create(name=f"Metrics Period {suffix}", start_month=3, end_month=3)

    lessons = []
    for i in range(1, 9):
        subj = subject_math if i <= 4 else subject_science
        lesson = LessonResource.objects.create(
            subject=subj,
            period=period,
            title=f"Metrics Lesson {i} {suffix}",
            type=ContentType.VIDEO.value,
            status=StatusEnum.APPROVED.value,
            resource=SimpleUploadedFile(f"metrics-{i}.mp4", b"video-bytes", content_type="video/mp4"),
            duration_minutes=10 + i,
        )
        lessons.append(lesson)

    for i, lesson in enumerate(lessons, start=1):
        LessonAssessment.objects.create(
            lesson=lesson,
            title=f"Lesson Quiz {i} {suffix}",
            type=AssessmentType.QUIZ.value,
            status=StatusEnum.APPROVED.value,
            due_at=timezone.now() + timedelta(days=(i % 5) + 1),
        )

    for i in range(1, 6):
        GeneralAssessment.objects.create(
            title=f"General Quiz {i} {suffix}",
            type=AssessmentType.QUIZ.value,
            status=StatusEnum.APPROVED.value,
            grade=StudentLevel.GRADE3.value,
            due_at=timezone.now() + timedelta(days=i),
        )

    for lesson in lessons[:3]:
        TakeLesson.objects.create(student=student, lesson=lesson)

    first_general = GeneralAssessment.objects.filter(title__contains=suffix).first()
    if first_general:
        AssessmentSolution.objects.create(
            assessment=first_general,
            student=student,
            solution="done",
            attachment=SimpleUploadedFile("solution.txt", b"answer", content_type="text/plain"),
        )

    client = APIClient()
    client.force_authenticate(user=user)

    endpoints = [
        ("kids_dashboard", "/api-v1/kids/dashboard/"),
        ("kids_progressgarden", "/api-v1/kids/progressgarden/"),
        ("kids_subjectsandlessons", "/api-v1/kids/subjectsandlessons/"),
        ("kids_assignments", "/api-v1/kids/assignments/"),
        ("kids_quizzes", "/api-v1/kids/quizzes/"),
        ("kids_assessments", "/api-v1/kids/assessments/"),
    ]

    print("name,status_cold,ms_cold,queries_cold,status_warm,ms_warm,queries_warm")
    for name, url in endpoints:
        cache.clear()

        with CaptureQueriesContext(connection) as cold_ctx:
            t0 = time.perf_counter()
            cold_resp = client.get(url)
            cold_ms = (time.perf_counter() - t0) * 1000

        with CaptureQueriesContext(connection) as warm_ctx:
            t1 = time.perf_counter()
            warm_resp = client.get(url)
            warm_ms = (time.perf_counter() - t1) * 1000

        print(
            f"{name},{cold_resp.status_code},{cold_ms:.2f},{len(cold_ctx)},"
            f"{warm_resp.status_code},{warm_ms:.2f},{len(warm_ctx)}"
        )


if __name__ == "__main__":
    main()
