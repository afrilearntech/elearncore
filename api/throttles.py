from django.conf import settings
from rest_framework.throttling import UserRateThrottle


class AIGenerationThrottle(UserRateThrottle):
    scope = 'ai_generation'
    rate = settings.AI_GENERATION_RATE
