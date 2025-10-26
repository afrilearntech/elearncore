from django.core.management.base import BaseCommand

from agentic.services import scan_chats_for_abuse


class Command(BaseCommand):
    help = "Scan recent chats using OpenAI moderation API and create abuse reports"

    def add_arguments(self, parser):
        parser.add_argument('--hours', type=int, default=12, help='Look back window in hours')

    def handle(self, *args, **options):
        hours = options.get('hours')
        reports = scan_chats_for_abuse(hours=hours)
        self.stdout.write(self.style.SUCCESS(f"Abuse reports created: {len(reports)}"))
