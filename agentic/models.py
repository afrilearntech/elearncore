from django.db import models


class TimestampedModel(models.Model):
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		abstract = True


class AIRecommendation(TimestampedModel):
	student = models.ForeignKey('accounts.Student', on_delete=models.CASCADE, related_name='ai_recommendations')
	lesson = models.ForeignKey('content.LessonResource', on_delete=models.CASCADE, related_name='ai_recommendations')
	message = models.CharField(max_length=500)

	def __str__(self) -> str:
		return f"Rec for {self.student.user.name} -> {self.lesson.title}"


class AIAbuseReport(TimestampedModel):
	tag = models.CharField(max_length=120)
	description = models.TextField(blank=True, default="")
	mark_review = models.CharField(max_length=120, blank=True, default="")
	forum = models.ForeignKey('forum.Forum', on_delete=models.CASCADE, related_name='abuse_reports')
	sample_msg = models.TextField(blank=True, default="")

	def __str__(self) -> str:
		return self.tag
