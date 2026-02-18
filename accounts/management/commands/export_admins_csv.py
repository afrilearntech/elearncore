import csv
from pathlib import Path

from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model

from elearncore.sysutils.constants import UserRole


class Command(BaseCommand):
    help = "Export all admin accounts (role=ADMIN) to a CSV file."

    def add_arguments(self, parser):
        parser.add_argument(
            "output",
            nargs="?",
            default="admins.csv",
            help=(
                "Output CSV file path. Defaults to 'admins.csv' in the "
                "current directory. Use '-' to write to stdout."
            ),
        )

    def handle(self, *args, **options):
        output = options["output"]
        User = get_user_model()

        admins_qs = User.objects.filter(role=UserRole.ADMIN.value).order_by("id")

        if not admins_qs.exists():
            self.stdout.write(self.style.WARNING("No admin accounts found (role=ADMIN)."))
            return

        fieldnames = [
            "id",
            "email",
            "phone",
            "name",
            "role",
            "is_active",
            "is_staff",
            "is_superuser",
            "created_at",
        ]

        if output == "-":
            writer = csv.writer(self.stdout)
            writer.writerow(fieldnames)
            for user in admins_qs:
                writer.writerow(
                    [
                        user.id,
                        user.email,
                        user.phone,
                        user.name,
                        user.role,
                        user.is_active,
                        user.is_staff,
                        user.is_superuser,
                        user.created_at.isoformat() if user.created_at else "",
                    ]
                )
            return

        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with output_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(fieldnames)
            for user in admins_qs:
                writer.writerow(
                    [
                        user.id,
                        user.email,
                        user.phone,
                        user.name,
                        user.role,
                        user.is_active,
                        user.is_staff,
                        user.is_superuser,
                        user.created_at.isoformat() if user.created_at else "",
                    ]
                )

        self.stdout.write(
            self.style.SUCCESS(f"Exported {admins_qs.count()} admin account(s) to '{output_path}'.")
        )
