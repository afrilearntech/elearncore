import uuid

from django.db import models


class NotificationDelivery(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        SENT = 'SENT', 'Sent'
        FAILED = 'FAILED', 'Failed'

    idempotency_key = models.UUIDField(default=uuid.uuid4, editable=False)
    channel = models.CharField(max_length=20)
    recipient_fingerprint = models.CharField(max_length=64)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    provider_status = models.CharField(max_length=100, blank=True, default='')
    last_error = models.CharField(max_length=100, blank=True, default='')
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['idempotency_key', 'channel'],
                name='unique_notification_delivery_channel',
            ),
        ]
        indexes = [models.Index(fields=['status', 'created_at'])]

    def __str__(self):
        return f'{self.idempotency_key}:{self.channel}:{self.status}'
