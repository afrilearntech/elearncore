from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView
from django.views.decorators.cache import cache_page

urlpatterns = [
    path('admin/', admin.site.urls),
    # api path
    path('api-v1/', include('api.urls')),
    # schema & docs
    # path('api-v1/schema/', cache_page(60 * 60)(SpectacularAPIView.as_view()), name='schema'),
    path('api-v1/schema-new/', SpectacularAPIView.as_view(), name='schema'),
    path('api-v1/docs-new/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api-v1/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]


# Let django serve static files in development mode
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL,
                          document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL,
                          document_root=settings.STATIC_ROOT)