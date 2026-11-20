"""Authentication, authorization, CSRF and WebSocket auth."""
import pytest
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser
from django.test import Client

from audit.models import AuditEvent
from terminal.routing import websocket_urlpatterns


@pytest.mark.django_db
@pytest.mark.parametrize("url", ["/api/projects", "/api/roots", "/api/engine/status", "/api/agents", "/api/audit"])
def test_api_requires_login(client, url):
    assert client.get(url).status_code == 401


@pytest.mark.django_db
def test_ide_page_redirects_to_login(client):
    r = client.get("/")
    assert r.status_code == 302 and r["Location"].startswith("/login/")


@pytest.mark.django_db
def test_login_flow_and_page_security_headers(admin_user):
    c = Client()
    r = c.post("/login/", {"username": "admin", "password": "pw-admin-123"})
    assert r.status_code == 302
    page = c.get("/")
    assert page.status_code == 200
    assert "Content-Security-Policy" in page.headers
    assert page.headers["X-Frame-Options"] == "DENY"
    assert b'name="csrf-token"' in page.content


@pytest.mark.django_db
def test_non_member_cannot_see_project(client, plain_user, plain_project):
    client.force_login(plain_user)
    assert client.get(f"/api/projects/{plain_project.id}").status_code == 404
    assert client.get(f"/api/projects/{plain_project.id}/tree").status_code == 404
    assert client.get(f"/api/projects/{plain_project.id}/file", {"path": "hello.txt"}).status_code == 404
    assert client.get("/api/projects").json()["projects"] == []


@pytest.mark.django_db
def test_member_can_access(client, plain_user, plain_project):
    plain_project.members.add(plain_user)
    client.force_login(plain_user)
    assert client.get(f"/api/projects/{plain_project.id}/file", {"path": "hello.txt"}).status_code == 200


@pytest.mark.django_db
def test_only_staff_manage_roots_and_projects(client, plain_user, workroot):
    client.force_login(plain_user)
    r = client.post("/api/roots", {"alias": "x", "path": str(workroot)}, content_type="application/json")
    assert r.status_code == 403
    assert AuditEvent.objects.filter(action="root.create", outcome="denied").exists()
    r = client.post("/api/projects", {"root_id": 1}, content_type="application/json")
    assert r.status_code == 403


@pytest.mark.django_db
def test_csrf_enforced(admin_user, plain_project):
    c = Client(enforce_csrf_checks=True)
    c.force_login(admin_user)
    url = f"/api/projects/{plain_project.id}/file"
    body = {"path": "hello.txt", "content": "pwned", "force": True}
    r = c.put(url, body, content_type="application/json")
    assert r.status_code == 403
    # With the token from the page, the request is accepted.
    page = c.get("/")
    token = page.content.decode().split('name="csrf-token" content="')[1].split('"')[0]
    v = c.get(url, {"path": "hello.txt"}).json()["version"]["token"]
    r = c.put(url, {"path": "hello.txt", "content": "ok", "base_version": v}, content_type="application/json",
              HTTP_X_CSRFTOKEN=token)
    assert r.status_code == 200


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_websocket_rejects_anonymous(plain_project):
    comm = WebsocketCommunicator(URLRouter(websocket_urlpatterns), f"/ws/terminal/{plain_project.id}/")
    comm.scope["user"] = AnonymousUser()
    connected, code = await comm.connect()
    assert not connected
    await comm.disconnect()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_websocket_rejects_non_member(plain_project, plain_user):
    comm = WebsocketCommunicator(URLRouter(websocket_urlpatterns), f"/ws/terminal/{plain_project.id}/")
    comm.scope["user"] = plain_user
    connected, _ = await comm.connect()
    assert not connected
    await comm.disconnect()


def test_websocket_origin_validator():
    """The ASGI app wraps websockets in AllowedHostsOriginValidator."""
    from codestudio.asgi import application

    ws_app = application.application_mapping["websocket"]
    assert type(ws_app).__name__ == "OriginValidator"
