from django.db import models


class TimestampedModel(models.Model):
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		abstract = True


class Forum(TimestampedModel):
	name = models.CharField(max_length=200)
	members = models.ManyToManyField('accounts.Student', through='ForumMembership', related_name='forums', blank=True)

	def __str__(self) -> str:
		return self.name


class ForumMembership(TimestampedModel):
	forum = models.ForeignKey(Forum, on_delete=models.CASCADE, related_name='memberships')
	student = models.ForeignKey('accounts.Student', on_delete=models.CASCADE, related_name='forum_memberships')

	class Meta:
		unique_together = ("forum", "student")

	def __str__(self) -> str:
		return f"{self.student.profile.name} @ {self.forum.name}"


class Chat(TimestampedModel):
	sender = models.ForeignKey('accounts.User', on_delete=models.CASCADE, related_name='sent_messages')
	forum = models.ForeignKey(Forum, on_delete=models.CASCADE, related_name='chats')
	content = models.TextField()
	media_url = models.URLField(max_length=500, null=True, blank=True)

	def __str__(self) -> str:
		return f"{self.sender.name}: {self.content[:30]}"
