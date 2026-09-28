from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.management.base import BaseCommand, CommandError

from accounts.models import School
from elearncore.sysutils.constants import Status, UserRole


class Command(BaseCommand):
    help = 'Create a machine-only sync service account scoped to one approved school.'

    def add_arguments(self, parser):
        parser.add_argument('--school-id', type=int, required=True)
        parser.add_argument('--name', required=True)
        parser.add_argument('--phone', required=True)
        parser.add_argument('--email')
        parser.add_argument('--password', required=True)

    def handle(self, *args, **options):
        school = School.objects.filter(
            pk=options['school_id'],
            status=Status.APPROVED.value,
        ).first()
        if school is None:
            raise CommandError('An approved school with that id was not found.')

        User = get_user_model()
        if User.objects.filter(phone=options['phone']).exists():
            raise CommandError('A user with that phone already exists.')
        validate_password(options['password'])
        user = User.objects.create_user(
            name=options['name'],
            phone=options['phone'],
            email=options.get('email') or None,
            password=options['password'],
            role=UserRole.SYNC_SERVICE.value,
            sync_school=school,
        )
        self.stdout.write(self.style.SUCCESS(f'Created sync service user {user.id} for school {school.id}.'))
