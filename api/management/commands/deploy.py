import subprocess
from django.core.management.base import BaseCommand

class Command(BaseCommand):
    help = "Deploy: pull, migrate, collectstatic, restart server/services."

    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE("[DEPLOY] Pulling latest code from origin..."))
        subprocess.run(["git", "pull", "origin", "main"], check=True)

        self.stdout.write(self.style.NOTICE("[DEPLOY] Making migrations..."))
        subprocess.run(["python", "manage.py", "makemigrations"], check=True)

        self.stdout.write(self.style.NOTICE("[DEPLOY] Applying migrations..."))
        subprocess.run(["python", "manage.py", "migrate"], check=True)

        self.stdout.write(self.style.NOTICE("[DEPLOY] Collecting static files..."))
        subprocess.run(["python", "manage.py", "collectstatic", "--noinput"], check=True)

        self.stdout.write(self.style.NOTICE("[DEPLOY] Restarting server and essential services..."))
        # Example: systemctl restart gunicorn; systemctl restart celery; systemctl restart nginx
        # You may need to adjust these commands for your environment
        try:
            subprocess.run(["systemctl", "restart", "gunicorn"], check=True)
            # subprocess.run(["systemctl", "restart", "celery"], check=True)
            # sudo systemctl reload nginx
            subprocess.run(["sudo", "systemctl", "restart", "nginx"], check=True)
            subprocess.run(["sudo", "systemctl", "reload", "nginx"], check=True)
            # subprocess.run(["sudo", "systemctl", "restart", "nginx"], check=True)
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"[DEPLOY] Could not restart one or more services: {e}"))

        self.stdout.write(self.style.SUCCESS("[DEPLOY] Deployment complete."))
