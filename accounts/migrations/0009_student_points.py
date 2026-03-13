from django.db import migrations, models


class Migration(migrations.Migration):

	dependencies = [
		('accounts', '0008_county_created_by'),
	]

	operations = [
		migrations.AddField(
			model_name='student',
			name='points',
			field=models.PositiveIntegerField(default=0),
		),
	]