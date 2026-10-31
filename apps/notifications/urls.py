from django.urls import path

from .views import (
    NotificationDetailView,
    NotificationListView,
    NotificationReadAllView,
    NotificationReadView,
    NotificationUnreadCountView,
)

# notifications/preferences is apps.accounts', which config/urls.py includes
# first. The two fixed words below come before <str:notification_id> for the
# same reason: the first match wins.
urlpatterns = [
    path("notifications", NotificationListView.as_view(), name="notification-list"),
    path(
        "notifications/unread-count",
        NotificationUnreadCountView.as_view(),
        name="notification-unread-count",
    ),
    path("notifications/read-all", NotificationReadAllView.as_view(), name="notification-read-all"),
    path(
        "notifications/<str:notification_id>",
        NotificationDetailView.as_view(),
        name="notification-detail",
    ),
    path(
        "notifications/<str:notification_id>/read",
        NotificationReadView.as_view(),
        name="notification-read",
    ),
]
