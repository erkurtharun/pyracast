from django.urls import path

from pyracast.notifications.views import ReminderPreviewView, RemindersView

urlpatterns = [
    path("tenants/<int:tenant_id>/regions/<int:region_id>/reminders/", RemindersView.as_view()),
    path(
        "tenants/<int:tenant_id>/regions/<int:region_id>/reminders/preview/",
        ReminderPreviewView.as_view(),
    ),
]
