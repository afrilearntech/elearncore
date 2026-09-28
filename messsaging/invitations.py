import uuid

from django.conf import settings

from elearncore.sysutils.constants import UserRole


ROLE_DETAILS = {
    UserRole.ADMIN.value: ('Administrator', '/admin/sign-in'),
    UserRole.CONTENTCREATOR.value: ('Content Creator', '/content/sign-in'),
    UserRole.CONTENTVALIDATOR.value: ('Content Validator', '/content/sign-in'),
    UserRole.TEACHER.value: ('Teacher', '/parent-teacher/sign-in/teacher'),
    UserRole.HEADTEACHER.value: ('Head Teacher', '/parent-teacher/sign-in/teacher'),
    UserRole.PARENT.value: ('Parent', '/parent-teacher/sign-in/parent'),
    UserRole.STUDENT.value: ('Student', '/login'),
}


def build_invitation_payload(user, temporary_password: str) -> dict:
    role_label, login_path = ROLE_DETAILS.get(
        user.role,
        ('Platform User', '/sign-in'),
    )
    login_url = f"{settings.FRONTEND_BASE_URL.rstrip('/')}{login_path}"
    login_identifier = user.email or user.phone
    expires_in_hours = settings.TEMPORARY_PASSWORD_TTL_HOURS
    subject = f'Your LR Digital Learning Platform {role_label} account'
    message = (
        f'Hello {user.name},\n\n'
        f'Your {role_label} account has been created on the LR Digital Learning Platform.\n'
        f'Login: {login_identifier}\n'
        f'Temporary password: {temporary_password}\n'
        f'Sign in: {login_url}\n\n'
        f'This temporary password expires in {expires_in_hours} hours. '
        'For your security, change it immediately after signing in and do not share it.\n\n'
        'LR Digital Learning Platform\nMinistry of Education'
    )
    return {
        'subject': subject,
        'message': message,
        'email_context': {
            'is_invitation': True,
            'recipient_name': user.name,
            'role_label': role_label,
            'login_identifier': login_identifier,
            'email': user.email,
            'phone': user.phone,
            'temporary_password': temporary_password,
            'expires_in_hours': expires_in_hours,
            'login_url': login_url,
        },
    }


def queue_account_invitation(user, temporary_password: str) -> None:
    from .tasks import send_account_notifications_task

    payload = build_invitation_payload(user, temporary_password)
    send_account_notifications_task.apply_async(
        args=(
            str(uuid.uuid4()),
            payload['message'],
            user.phone,
            user.email,
            payload['subject'],
            payload['email_context'],
        ),
        retry=False,
        argsrepr='(<redacted account invitation>)',
    )
