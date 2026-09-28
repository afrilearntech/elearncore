import hashlib
import logging
from collections.abc import Callable

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from .models import NotificationDelivery
from .services import send_sms

logger = logging.getLogger(__name__)


def _deliver_channel_once(
    idempotency_key: str,
    channel: str,
    recipient: str,
    deliver: Callable[[], object],
) -> bool:
    fingerprint = hashlib.sha256(recipient.strip().lower().encode('utf-8')).hexdigest()
    delivery, _ = NotificationDelivery.objects.get_or_create(
        idempotency_key=idempotency_key,
        channel=channel,
        defaults={'recipient_fingerprint': fingerprint},
    )
    if delivery.status == NotificationDelivery.Status.SENT:
        return False

    delivery.attempts += 1
    delivery.status = NotificationDelivery.Status.PENDING
    delivery.last_error = ''
    delivery.save(update_fields=['attempts', 'status', 'last_error', 'updated_at'])
    try:
        result = deliver()
    except Exception as exc:
        delivery.status = NotificationDelivery.Status.FAILED
        delivery.last_error = type(exc).__name__[:100]
        delivery.save(update_fields=['status', 'last_error', 'updated_at'])
        raise

    provider_status = 'accepted'
    if isinstance(result, dict):
        provider_status = str(result.get('status') or provider_status)
    delivery.status = NotificationDelivery.Status.SENT
    delivery.provider_status = provider_status[:100]
    delivery.sent_at = timezone.now()
    delivery.save(update_fields=['status', 'provider_status', 'sent_at', 'updated_at'])
    return True


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def send_account_notifications_task(
    self,
    idempotency_key: str,
    message: str,
    phone: str | None,
    email: str | None,
    email_subject: str,
) -> dict:
    """Deliver account credentials through retryable, observable channels."""
    delivered = []
    if phone:
        if _deliver_channel_once(
            idempotency_key,
            'sms',
            phone,
            lambda: send_sms(message, [phone]),
        ):
            delivered.append('sms')
    if email:
        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', None)

        def _send_email():
            if not from_email:
                raise RuntimeError('DEFAULT_FROM_EMAIL is not configured')
            return send_mail(
                subject=email_subject,
                message=message,
                from_email=from_email,
                recipient_list=[email],
                fail_silently=False,
            )

        if _deliver_channel_once(
            idempotency_key,
            'email',
            email,
            _send_email,
        ):
            delivered.append('email')
    logger.info("Account notification delivered through %s", ','.join(delivered) or 'no channels')
    return {'delivered': delivered}
