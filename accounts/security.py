import secrets
import string
from datetime import timedelta

from django.conf import settings
from django.utils import timezone


def generate_temporary_password(length: int = 18) -> str:
    """Return a high-entropy password containing each major character class."""
    length = max(16, int(length))
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice('!@#$%^&*_-+='),
    ]
    alphabet = string.ascii_letters + string.digits + '!@#$%^&*_-+='
    chars = required + [secrets.choice(alphabet) for _ in range(length - len(required))]
    secrets.SystemRandom().shuffle(chars)
    return ''.join(chars)


def assign_temporary_password(user) -> str:
    """Set a short-lived one-time password on an unsaved or saved user."""
    password = generate_temporary_password()
    user.set_password(password)
    user.must_change_password = True
    user.temporary_password_expires_at = timezone.now() + timedelta(
        hours=int(getattr(settings, 'TEMPORARY_PASSWORD_TTL_HOURS', 24))
    )
    return password
