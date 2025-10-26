from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    # api path
    path('api-v1/', include('api.urls')),
]
