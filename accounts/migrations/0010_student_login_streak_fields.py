from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0009_student_points'),
    ]

    operations = [
        migrations.AddField(
            model_name='student',
            name='current_login_streak',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='student',
            name='last_login_activity_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='student',
            name='max_login_streak',
            field=models.PositiveIntegerField(default=0),
        ),
    ]
