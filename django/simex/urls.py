from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("django-admin/", admin.site.urls),  # secondary tool; the custom tabs are the main UI
    path("", include("ops.urls")),
]
