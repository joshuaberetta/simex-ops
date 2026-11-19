from django.contrib import admin
from django.urls import include, path, re_path
from django.views.generic import RedirectView

urlpatterns = [
    path("admin/", admin.site.urls),  # secondary tool; the custom tabs are the main UI
    re_path(r"^django-admin/(?P<rest>.*)$", RedirectView.as_view(url="/admin/%(rest)s", permanent=False)),
    path("", include("ops.urls")),
]
