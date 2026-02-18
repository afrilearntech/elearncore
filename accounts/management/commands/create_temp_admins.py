from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model

from elearncore.sysutils.constants import UserRole


class Command(BaseCommand):
    help = (
        "Create N temporary admin accounts with emails "
        "tempadmin<i>@afrilearntech.com and names 'Temp Admin <i>'."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "count",
            type=int,
            help="Number of temporary admin accounts to create.",
        )
        parser.add_argument(
            "--start-index",
            type=int,
            default=0,
            help=(
                "Starting index for the temp admin numbering. "
                "Default is 0, so the first user is tempadmin0@afrilearntech.com."
            ),
        )
        parser.add_argument(
            "--password",
            type=str,
            default="TempAdmin123!",
            help=(
                "Password to set for all created temp admin accounts. "
                "Defaults to 'TempAdmin123!'."
            ),
        )

    def handle(self, *args, **options):
        count = options["count"]
        start_index = options["start_index"]
        password = options["password"]

        if count <= 0:
            raise CommandError("Count must be a positive integer.")

        User = get_user_model()

        created = 0
        skipped = 0

        for i in range(start_index, start_index + count):
            email = f"tempadmin{i}@afrilearntech.com"
            name = f"Temp Admin {i}"

            if User.objects.filter(email=email).exists():
                skipped += 1
                self.stdout.write(self.style.WARNING(f"User with email {email} already exists; skipping."))
                continue

            # Generate a unique phone value respecting max_length=25
            base_phone = f"000111000{i}"
            phone = base_phone
            suffix = 0
            while User.objects.filter(phone=phone).exists():
                suffix += 1
                phone = f"{base_phone}{suffix}"

            user = User.objects.create_superuser(
                email=email,
                password=password,
                name=name,
                phone=phone,
                role=UserRole.ADMIN.value,
            )

            created += 1
            self.stdout.write(self.style.SUCCESS(f"Created temp admin: {user.email} ({user.name})"))

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Created {created} temp admin account(s); skipped {skipped} existing email(s)."
            )
        )
