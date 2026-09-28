from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string


def send_branded_email(
    subject: str,
    plain_body: str,
    recipient: str,
    template_context: dict | None = None,
) -> dict:
    """Send an email with a consistent HTML brand and plain-text fallback."""
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', '').strip()
    if not from_email:
        raise RuntimeError('DEFAULT_FROM_EMAIL is not configured')

    context = {
        'subject': subject,
        'message': plain_body,
        **(template_context or {}),
    }
    html_body = render_to_string('emails/notification.html', context)
    email = EmailMultiAlternatives(
        subject=subject,
        body=plain_body,
        from_email=from_email,
        to=[recipient],
    )
    email.attach_alternative(html_body, 'text/html')
    sent_count = email.send(fail_silently=False)
    if sent_count != 1:
        raise RuntimeError('Email provider did not accept the notification')
    return {'status': 'accepted'}
