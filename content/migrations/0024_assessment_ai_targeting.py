# Generated manually for AI targeting fields on assessments

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0007_parent_parent_id_student_student_id_and_more'),
        ('content', '0023_lessonassessmentsolution'),
    ]

    operations = [
        migrations.AddField(
            model_name='generalassessment',
            name='ai_recommended',
            field=models.BooleanField(default=False, help_text='Created or recommended by AI'),
        ),
        migrations.AddField(
            model_name='generalassessment',
            name='is_targeted',
            field=models.BooleanField(default=False, help_text='If true, this assessment targets a particular student'),
        ),
        migrations.AddField(
            model_name='generalassessment',
            name='target_student',
            field=models.ForeignKey(blank=True, null=True, on_delete=models.SET_NULL, related_name='targeted_general_assessments', to='accounts.student'),
        ),
        migrations.AddField(
            model_name='lessonassessment',
            name='ai_recommended',
            field=models.BooleanField(default=False, help_text='Created or recommended by AI'),
        ),
        migrations.AddField(
            model_name='lessonassessment',
            name='is_targeted',
            field=models.BooleanField(default=False, help_text='If true, this assessment targets a particular student'),
        ),
        migrations.AddField(
            model_name='lessonassessment',
            name='target_student',
            field=models.ForeignKey(blank=True, null=True, on_delete=models.SET_NULL, related_name='targeted_lesson_assessments', to='accounts.student'),
        ),
        migrations.AddIndex(
            model_name='generalassessment',
            index=models.Index(fields=['is_targeted', 'target_student'], name='content_gen_target_idx'),
        ),
        migrations.AddIndex(
            model_name='lessonassessment',
            index=models.Index(fields=['is_targeted', 'target_student'], name='content_les_target_idx'),
        ),
    ]
