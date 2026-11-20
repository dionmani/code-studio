from django.urls import path

from .consumers import TerminalConsumer
from .system import SystemTerminalConsumer

websocket_urlpatterns = [
    path("ws/terminal/<int:project_id>/", TerminalConsumer.as_asgi()),
    path("ws/systerm/<int:project_id>/", SystemTerminalConsumer.as_asgi()),
]
