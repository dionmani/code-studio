from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie


@login_required
@ensure_csrf_cookie
def ide(request):
    from localinstall.version import build_info
    from terminal.system import is_loopback, system_terminal_enabled

    cfg = settings.CODE_STUDIO
    return render(request, "ide.html", {
        "build": build_info(),
        "offer_local": cfg.get("OFFER_LOCAL_INSTALL", False),
        "local_install": cfg.get("LOCAL_INSTALL", False),
        "system_terminal": system_terminal_enabled() and is_loopback(request.META.get("REMOTE_ADDR")),
    })
