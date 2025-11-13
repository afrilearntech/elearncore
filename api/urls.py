from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .viewsets import (
	SubjectViewSet,
	TopicViewSet,
	PeriodViewSet,
	LessonResourceViewSet,
	TakeLessonViewSet,
	AIRecommendationViewSet,
	AIAbuseReportViewSet,
	OnboardingViewSet,
	DashboardViewSet,
	SchoolLookupViewSet,
	CountyLookupViewSet,
	DistrictLookupViewSet,
	LoginViewSet,
	AdminCountyViewSet,
	AdminDistrictViewSet,
	AdminSchoolViewSet,
)

router = DefaultRouter()
router.register(r'subjects', SubjectViewSet, basename='subject')
router.register(r'topics', TopicViewSet, basename='topic')
router.register(r'periods', PeriodViewSet, basename='period')
router.register(r'lessons', LessonResourceViewSet, basename='lesson')
router.register(r'taken-lessons', TakeLessonViewSet, basename='takelesson')
router.register(r'ai/recommendations', AIRecommendationViewSet, basename='ai-recommendation')
router.register(r'ai/abuse-reports', AIAbuseReportViewSet, basename='ai-abuse-report')
router.register(r'onboarding', OnboardingViewSet, basename='onboarding')
router.register(r'dashboard', DashboardViewSet, basename='dashboard')
router.register(r'lookup/schools', SchoolLookupViewSet, basename='school-lookup')
router.register(r'lookup/counties', CountyLookupViewSet, basename='county-lookup')
router.register(r'lookup/districts', DistrictLookupViewSet, basename='district-lookup')
router.register(r'auth', LoginViewSet, basename='auth')
router.register(r'admin/counties', AdminCountyViewSet, basename='admin-counties')
router.register(r'admin/districts', AdminDistrictViewSet, basename='admin-districts')
router.register(r'admin/schools', AdminSchoolViewSet, basename='admin-schools')

urlpatterns = [
	path('', include(router.urls)),
]