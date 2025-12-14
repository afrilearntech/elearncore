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
        # Adjusted for Daphne + Nginx deployment
        try:
            # Restart Daphne ASGI service
            subprocess.run(["systemctl", "restart", "elearncore-daphne"], check=True)

            # Restart and reload Nginx to pick up config changes
            subprocess.run(["systemctl", "restart", "nginx"], check=True)
            subprocess.run(["systemctl", "reload", "nginx"], check=True)
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"[DEPLOY] Could not restart one or more services: {e}"))

        self.stdout.write(self.style.SUCCESS("[DEPLOY] Deployment complete."))
