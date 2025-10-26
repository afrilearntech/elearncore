from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView

urlpatterns = [
    path('admin/', admin.site.urls),
    # api path
    path('api-v1/', include('api.urls')),
    # schema & docs
    path('api-v1/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api-v1/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api-v1/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]
