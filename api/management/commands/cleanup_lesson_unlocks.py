from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from content.models import LessonTemporaryUnlock


class Command(BaseCommand):
    help = "Delete expired and old revoked temporary lesson unlock records."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print counts without deleting rows.",
        )
        parser.add_argument(
            "--keep-revoked-days",
            type=int,
            default=30,
            help="Keep revoked rows newer than this many days (default: 30).",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        keep_revoked_days = max(0, int(options["keep_revoked_days"]))
        dry_run = bool(options["dry_run"])

        expired_qs = LessonTemporaryUnlock.objects.filter(expires_at__lte=now)
        revoked_cutoff = now - timedelta(days=keep_revoked_days)
        old_revoked_qs = LessonTemporaryUnlock.objects.filter(
            revoked_at__isnull=False,
            revoked_at__lte=revoked_cutoff,
        )

        expired_count = expired_qs.count()
        old_revoked_count = old_revoked_qs.count()

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run: would delete {expired_count} expired and {old_revoked_count} old revoked unlock rows."
                )
            )
            return

        deleted_expired, _ = expired_qs.delete()
        deleted_revoked, _ = old_revoked_qs.delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted_expired} expired and {deleted_revoked} old revoked unlock rows."
            )
        )
