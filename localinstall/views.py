from __future__ import annotations

import httpx
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from audit.log import record
from core.api import api_view

from .package import build_package, make_download_token, package_name, user_for_token
from .version import build_info


def _cfg(key, default=None):
    return settings.CODE_STUDIO.get(key, default)


@require_GET
def version(request):
    """Public: lets a local copy compare its build with this server's. Reveals only the build id."""
    info = build_info()
    return JsonResponse({"version": info["version"], "build": info["build"],
                         "local_install": bool(_cfg("LOCAL_INSTALL"))})


def check_for_update() -> dict:
    """On a local copy: compare this build with the server it was installed from."""
    local = build_info()
    server_url = (_cfg("UPDATE_SERVER") or "").rstrip("/")
    result = {"enabled": bool(_cfg("LOCAL_INSTALL")), "local": local, "server": None, "server_url": server_url,
              "update_available": False, "error": ""}
    if not result["enabled"]:
        return result
    if not server_url:
        result["error"] = "No update server is configured for this copy."
        return result
    try:
        resp = httpx.get(f"{server_url}/api/version", timeout=4.0)
        resp.raise_for_status()
        data = resp.json()
        result["server"] = {"version": str(data.get("version", "")), "build": str(data.get("build", ""))}
        result["update_available"] = bool(result["server"]["build"]) and result["server"]["build"] != local["build"]
    except (httpx.HTTPError, ValueError) as exc:
        result["error"] = f"Could not reach {server_url} ({exc.__class__.__name__})."
    return result


@api_view(["GET"])
def update_status(request):
    return check_for_update()


@login_required
@require_GET
def local_page(request):
    is_local = bool(_cfg("LOCAL_INSTALL"))
    return render(request, "localinstall/local.html", {
        "build": build_info(),
        "is_local": is_local,
        "update": check_for_update() if is_local else None,
        "can_download": not is_local,
    })


@require_GET
def download(request):
    """The install/update package. Signed-in users, or a local copy's download token."""
    if _cfg("LOCAL_INSTALL"):
        return HttpResponse("Packages are only offered by the server copy of Code Studio.", status=404,
                            content_type="text/plain")
    user = request.user if request.user.is_authenticated else None
    via = "session"
    auth = request.headers.get("Authorization", "")
    if user is None and auth.lower().startswith("bearer "):
        user = user_for_token(auth[7:].strip())
        via = "token"
    if user is None:
        return JsonResponse({"error": "Sign in (or use a valid download token) to download Code Studio"},
                            status=401)

    server_url = request.build_absolute_uri("/").rstrip("/")
    package = build_package(server_url, make_download_token(user))
    record(actor=user, action="localinstall.download", target=package_name(), via=via)
    return FileResponse(package, as_attachment=True, filename=package_name(), content_type="application/zip")
