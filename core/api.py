"""Small JSON API toolkit (no DRF): auth, method checks, JSON parsing, errors."""
from __future__ import annotations

import functools
import json
import logging

from django.http import Http404, JsonResponse
from django.views.decorators.http import require_http_methods

log = logging.getLogger(__name__)


class ApiError(Exception):
    status = 400

    def __init__(self, message: str, status: int | None = None, **extra):
        super().__init__(message)
        if status is not None:
            self.status = status
        self.extra = extra


class Conflict(ApiError):
    status = 409


def json_body(request) -> dict:
    if not request.body:
        return {}
    if len(request.body) > 25 * 1024 * 1024:
        raise ApiError("Request body too large", 413)
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ApiError("Invalid JSON body")
    if not isinstance(data, dict):
        raise ApiError("JSON body must be an object")
    return data


def client_ip(request) -> str:
    return request.META.get("REMOTE_ADDR", "")[:64]


def api_view(methods=("GET",), staff=False):
    """Decorator: JSON errors, 401 instead of login redirect, optional staff check.

    CSRF is enforced by Django's middleware for unsafe methods (views are not
    csrf_exempt); the browser sends X-CSRFToken from the page meta tag.
    """

    def decorator(fn):
        @require_http_methods(list(methods))
        @functools.wraps(fn)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return JsonResponse({"error": "Authentication required"}, status=401)
            if staff and not request.user.is_staff:
                return JsonResponse({"error": "Administrator permission required"}, status=403)
            from projects.paths import PathSecurityError

            try:
                result = fn(request, *args, **kwargs)
            except PathSecurityError as exc:
                from audit.log import record

                record(request=request, action="path.denied", target=request.path, outcome="denied", reason=str(exc))
                return JsonResponse({"error": str(exc), "code": "path_denied"}, status=403)
            except ApiError as exc:
                body = {"error": str(exc)}
                body.update(exc.extra)
                return JsonResponse(body, status=exc.status)
            except Http404 as exc:
                return JsonResponse({"error": str(exc) or "Not found"}, status=404)
            except PermissionError as exc:
                return JsonResponse({"error": f"Permission denied: {exc}"}, status=403)
            if isinstance(result, dict) or isinstance(result, list):
                return JsonResponse(result, safe=False)
            return result

        return wrapper

    return decorator
