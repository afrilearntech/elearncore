from django.conf import settings
from rest_framework.throttling import UserRateThrottle


class AIGenerationThrottle(UserRateThrottle):
    scope = 'ai_generation'
    rate = settings.AI_GENERATION_RATE


class GameAnswerThrottle(UserRateThrottle):
    scope = 'game_answer'
    rate = settings.GAME_ANSWER_RATE
