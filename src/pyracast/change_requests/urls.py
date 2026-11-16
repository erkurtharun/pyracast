from django.urls import path

from pyracast.change_requests.api import (
    ApproveView,
    DetailView,
    InboxView,
    RejectView,
    SeenView,
    WithdrawView,
)

urlpatterns = [
    path("", InboxView.as_view()),
    path("<int:request_id>/", DetailView.as_view()),
    path("<int:request_id>/approve/", ApproveView.as_view()),
    path("<int:request_id>/reject/", RejectView.as_view()),
    path("<int:request_id>/withdraw/", WithdrawView.as_view()),
    path("<int:request_id>/seen/", SeenView.as_view()),
]
