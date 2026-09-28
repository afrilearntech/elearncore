from django.utils import timezone
from knox.auth import TokenAuthentication
from rest_framework.exceptions import AuthenticationFailed


class PasswordChangeAwareTokenAuthentication(TokenAuthentication):
    """Limit one-time-password sessions to password rotation endpoints."""

    allowed_suffixes = (
        '/api-v1/auth/change-password/',
        '/api-v1/onboarding/logout_after_setup/',
        '/api-v1/onboarding/logout-after-setup/',
    )

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None
        user, token = result
        if not getattr(user, 'must_change_password', False):
            return result
        expires_at = getattr(user, 'temporary_password_expires_at', None)
        if expires_at and expires_at <= timezone.now():
            raise AuthenticationFailed('Temporary password has expired. Contact an administrator for a new credential.')
        if request.path not in self.allowed_suffixes:
            raise AuthenticationFailed('Password change required before using this endpoint.')
        return user, token
