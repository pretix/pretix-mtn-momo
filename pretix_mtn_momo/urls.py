from django.urls import re_path

from . import views

urlpatterns = [
    re_path(r"^_mtn_momo/webhook/$", views.webhook, name="webhook"),
]
