from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import Student
from agentic.services import generate_recommendations_for_student


class Command(BaseCommand):
    help = "Generate AI recommendations for students using LLM based on recent activity"

    def add_arguments(self, parser):
        parser.add_argument('--student-id', type=int, help='Limit to a single student id')
        parser.add_argument('--max', type=int, default=5, help='Max recommendations per student')

    @transaction.atomic
    def handle(self, *args, **options):
        student_id = options.get('student_id')
        max_recs = options.get('max')

        qs = Student.objects.all()
        if student_id:
            qs = qs.filter(id=student_id)

        total = 0
        for student in qs.iterator():
            created = generate_recommendations_for_student(student, max_recs=max_recs)
            total += len(created)
            self.stdout.write(self.style.SUCCESS(f"Student {student.id}: created {len(created)} recommendations"))
        self.stdout.write(self.style.SUCCESS(f"Total recommendations created: {total}"))
