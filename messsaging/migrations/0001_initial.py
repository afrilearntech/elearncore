import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='NotificationDelivery',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('idempotency_key', models.UUIDField(default=uuid.uuid4, editable=False)),
                ('channel', models.CharField(max_length=20)),
                ('recipient_fingerprint', models.CharField(max_length=64)),
                ('status', models.CharField(choices=[('PENDING', 'Pending'), ('SENT', 'Sent'), ('FAILED', 'Failed')], default='PENDING', max_length=20)),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('provider_status', models.CharField(blank=True, default='', max_length=100)),
                ('last_error', models.CharField(blank=True, default='', max_length=100)),
                ('sent_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'indexes': [models.Index(fields=['status', 'created_at'], name='messsaging__status_040321_idx')],
                'constraints': [models.UniqueConstraint(fields=('idempotency_key', 'channel'), name='unique_notification_delivery_channel')],
            },
        ),
    ]
