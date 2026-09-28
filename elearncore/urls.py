from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView
from rest_framework.permissions import IsAdminUser

urlpatterns = [
    path('admin/', admin.site.urls),
    # api path
    path('api-v1/', include('api.urls')),
]

if settings.API_DOCS_ENABLED:
    docs_permissions = [] if settings.DEBUG else [IsAdminUser]
    urlpatterns += [
        path('api-v1/schema/', SpectacularAPIView.as_view(permission_classes=docs_permissions), name='schema'),
        path('api-v1/docs/', SpectacularSwaggerView.as_view(url_name='schema', permission_classes=docs_permissions), name='swagger-ui'),
        path('api-v1/redoc/', SpectacularRedocView.as_view(url_name='schema', permission_classes=docs_permissions), name='redoc'),
    ]


# Let django serve static files in development mode
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL,
                          document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL,
                          document_root=settings.STATIC_ROOT)
