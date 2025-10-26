from rest_framework import serializers

from .models import AIRecommendation, AIAbuseReport


class AIRecommendationSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIRecommendation
        fields = ['id', 'student', 'lesson', 'message', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']


class AIAbuseReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIAbuseReport
        fields = ['id', 'tag', 'description', 'mark_review', 'forum', 'sample_msg', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']
