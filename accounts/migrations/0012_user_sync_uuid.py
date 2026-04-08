from __future__ import annotations

import uuid

from django.db import migrations, models


def _populate_user_sync_uuid(apps, schema_editor):
    User = apps.get_model("accounts", "User")

    qs = User.objects.filter(sync_uuid__isnull=True)
    for user in qs.iterator():
        user.sync_uuid = uuid.uuid4()
        user.save(update_fields=["sync_uuid"])


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0011_student_accounts_st_grade_45a9e3_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="sync_uuid",
            field=models.UUIDField(blank=True, editable=False, null=True, unique=True),
        ),
        migrations.RunPython(_populate_user_sync_uuid, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="user",
            name="sync_uuid",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
