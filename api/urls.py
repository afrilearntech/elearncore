from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .viewsets import (
	ContentViewSet,
	ParentViewSet,
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
	AdminContentManagerViewSet,
	AdminStudentViewSet,
	AdminParentViewSet,
	AdminUserViewSet,
	AdminDashboardViewSet,
	AdminSystemReportViewSet,
	GameViewSet,
	KidsViewSet,
	TeacherViewSet,
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
router.register(r'kids', KidsViewSet, basename='kids')
router.register(r'lookup/schools', SchoolLookupViewSet, basename='school-lookup')
router.register(r'lookup/counties', CountyLookupViewSet, basename='county-lookup')
router.register(r'lookup/districts', DistrictLookupViewSet, basename='district-lookup')
router.register(r'auth', LoginViewSet, basename='auth')
router.register(r'admin/counties', AdminCountyViewSet, basename='admin-counties')
router.register(r'admin/districts', AdminDistrictViewSet, basename='admin-districts')
router.register(r'admin/schools', AdminSchoolViewSet, basename='admin-schools')
router.register(r'admin/content-managers', AdminContentManagerViewSet, basename='admin-content-managers')
router.register(r'admin/students', AdminStudentViewSet, basename='admin-students')
router.register(r'admin/parents', AdminParentViewSet, basename='admin-parents')
router.register(r'admin/users', AdminUserViewSet, basename='admin-users')
router.register(r'admin/dashboard', AdminDashboardViewSet, basename='admin-dashboard')
router.register(r'admin/system-reports', AdminSystemReportViewSet, basename='admin-system-reports')
router.register(r'games', GameViewSet, basename='game')
router.register(r'content', ContentViewSet, basename='content')
router.register(r'teacher', TeacherViewSet, basename='teacher')
router.register(r'parent', ParentViewSet, basename='parent')

urlpatterns = [
	path('', include(router.urls)),
]