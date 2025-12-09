'''
This management command will push local code to a GitHub repository.
Usage:
    python manage.py github <commit_message>

Processes:
1. python manage.py makemigrations
2. python manage.py migrate
3. git add .
4. git commit -m "<commit_message>"
5. git push origin main
'''

import subprocess
import sys
from django.core.management.base import BaseCommand

class Command(BaseCommand):
    help = "Push local code changes to GitHub repository."

    def add_arguments(self, parser):
        parser.add_argument('commit_message', type=str, help='Commit message for the changes.')

    def handle(self, *args, **options):
        commit_message = options['commit_message']

        self.stdout.write(self.style.NOTICE("[GITHUB PUSH] Making migrations..."))
        subprocess.run([sys.executable, "manage.py", "makemigrations"], check=True)

        self.stdout.write(self.style.NOTICE("[GITHUB PUSH] Applying migrations..."))
        subprocess.run([sys.executable, "manage.py", "migrate"], check=True)

        self.stdout.write(self.style.NOTICE("[GITHUB PUSH] Staging changes..."))
        subprocess.run(["git", "add", "."], check=True)

        self.stdout.write(self.style.NOTICE(f"[GITHUB PUSH] Committing changes with message: {commit_message}"))
        subprocess.run(["git", "commit", "-m", commit_message], check=True)

        self.stdout.write(self.style.NOTICE("[GITHUB PUSH] Pushing changes to origin/main..."))
        subprocess.run(["git", "push", "origin", "main"], check=True)

        self.stdout.write(self.style.SUCCESS("[GITHUB PUSH] Code pushed to GitHub successfully."))