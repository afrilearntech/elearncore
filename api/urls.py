from django.urls import path, include
from rest_framework.routers import DefaultRouter

# from .viewsets import *  # ViewSets can be registered to the router below when available

router = DefaultRouter()

urlpatterns = [
	path('', include(router.urls)),
]