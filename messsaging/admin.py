from django.contrib import admin

from .models import NotificationDelivery


@admin.register(NotificationDelivery)
class NotificationDeliveryAdmin(admin.ModelAdmin):
    list_display = ('idempotency_key', 'channel', 'status', 'attempts', 'sent_at', 'updated_at')
    list_filter = ('channel', 'status')
    search_fields = ('idempotency_key', 'recipient_fingerprint')
    readonly_fields = (
        'idempotency_key', 'channel', 'recipient_fingerprint', 'status', 'attempts',
        'provider_status', 'last_error', 'sent_at', 'created_at', 'updated_at',
    )
