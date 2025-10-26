from rest_framework import serializers

from .models import Forum, ForumMembership, Chat


class ForumSerializer(serializers.ModelSerializer):
    class Meta:
        model = Forum
        fields = ['id', 'name', 'members', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']


class ForumMembershipSerializer(serializers.ModelSerializer):
    class Meta:
        model = ForumMembership
        fields = ['id', 'forum', 'student', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']


class ChatSerializer(serializers.ModelSerializer):
    class Meta:
        model = Chat
        fields = ['id', 'sender', 'forum', 'content', 'media_url', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']
