"""Agent profile add / edit / delete API (used by the Manage profiles modal)."""
import json

import pytest

from agents_client.models import AgentAdapter, AgentProfile, Task, TaskAssignment


def j(client, method, url, body=None):
    fn = getattr(client, method.lower())
    return fn(url, data=json.dumps(body or {}), content_type="application/json")


@pytest.fixture
def adapters(db):
    AgentAdapter.objects.get_or_create(identifier="claude-code", defaults={"display_name": "Claude Code", "available": True})
    AgentAdapter.objects.get_or_create(identifier="codex", defaults={"display_name": "OpenAI Codex CLI", "available": True})


@pytest.mark.django_db
def test_create_edit_delete_roundtrip(client_admin, adapters):
    r = j(client_admin, "POST", "/api/profiles", {
        "name": "Claude planner", "adapter": "claude-code", "role": "architect", "enabled": True,
        "execution_policy": {"read_only": True, "model": "opus", "timeout_seconds": 600, "max_turns": 20}})
    assert r.status_code == 200, r.content
    p = r.json()
    assert p["adapter_name"] == "Claude Code" and p["task_count"] == 0
    assert p["execution_policy"] == {"read_only": True, "model": "opus", "timeout_seconds": 600, "max_turns": 20}

    # rename, switch agent, change role/access, clear model and max turns
    r = j(client_admin, "PATCH", f"/api/profiles/{p['id']}", {
        "name": "Codex planner", "adapter": "codex", "role": "reviewer", "enabled": False,
        "execution_policy": {"read_only": False, "model": None, "timeout_seconds": 1200, "max_turns": None}})
    assert r.status_code == 200, r.content
    e = r.json()
    assert (e["name"], e["adapter"], e["role"], e["enabled"]) == ("Codex planner", "codex", "reviewer", False)
    assert e["execution_policy"] == {"read_only": False, "timeout_seconds": 1200}
    assert client_admin.get(f"/api/profiles/{p['id']}").json()["name"] == "Codex planner"

    r = client_admin.delete(f"/api/profiles/{p['id']}")
    assert r.status_code == 200 and not AgentProfile.objects.filter(pk=p["id"]).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("body, status", [
    ({"name": "", "adapter": "codex", "role": "implementer"}, 400),
    ({"name": "x", "adapter": "nope", "role": "implementer"}, 400),
    ({"name": "x", "adapter": "codex", "role": "   "}, 400),
    ({"name": "x", "adapter": "codex", "role": "r", "execution_policy": {"timeout_seconds": "abc"}}, 400),
    ({"name": "x", "adapter": "codex", "role": "r", "execution_policy": {"timeout_seconds": 99999}}, 400),
    ({"name": "x", "adapter": "codex", "role": "r", "execution_policy": {"max_turns": 0.5}}, 400),
])
def test_validation_errors_are_400_not_500(client_admin, adapters, body, status):
    assert j(client_admin, "POST", "/api/profiles", body).status_code == status


@pytest.mark.django_db
def test_duplicate_names_rejected_case_insensitively(client_admin, adapters):
    assert j(client_admin, "POST", "/api/profiles", {"name": "Reviewer", "adapter": "codex", "role": "reviewer"}).status_code == 200
    assert j(client_admin, "POST", "/api/profiles", {"name": "reviewer", "adapter": "codex", "role": "reviewer"}).status_code == 409
    other = j(client_admin, "POST", "/api/profiles", {"name": "Other", "adapter": "codex", "role": "reviewer"}).json()
    assert j(client_admin, "PATCH", f"/api/profiles/{other['id']}", {"name": "REVIEWER"}).status_code == 409
    # renaming a profile to its own name (different case) is fine
    assert j(client_admin, "PATCH", f"/api/profiles/{other['id']}", {"name": "other"}).status_code == 200


@pytest.mark.django_db
def test_non_staff_can_view_but_not_change(client, plain_user, fixture_profile):
    client.force_login(plain_user)
    assert client.get("/api/profiles").status_code == 200
    assert j(client, "POST", "/api/profiles", {"name": "x", "adapter": "fixture", "role": "r"}).status_code == 403
    assert j(client, "PATCH", f"/api/profiles/{fixture_profile.id}", {"enabled": False}).status_code == 403
    assert client.delete(f"/api/profiles/{fixture_profile.id}").status_code == 403
    assert AgentProfile.objects.get(pk=fixture_profile.id).enabled


@pytest.mark.django_db
def test_delete_keeps_task_history(client_admin, admin_user, plain_project, fixture_profile):
    task = Task.objects.create(project=plain_project, requester=admin_user, objective="x", state="applied")
    TaskAssignment.objects.create(task=task, profile=fixture_profile, adapter_identifier="fixture", role="implementer")
    assert client_admin.get(f"/api/profiles/{fixture_profile.id}").json()["task_count"] == 1
    assert client_admin.delete(f"/api/profiles/{fixture_profile.id}").status_code == 200
    a = TaskAssignment.objects.get(task=task)
    assert a.profile is None and a.adapter_identifier == "fixture" and a.role == "implementer"


def jr(client, method, url, body=None):
    resp = client.get(url) if method == "GET" else j(client, method, url, body)
    return resp.status_code, resp.json()


@pytest.mark.django_db
def test_default_lineup_roundtrip_and_validation(client_admin, adapters):
    imp = AgentProfile.objects.create(name="Imp", adapter=AgentAdapter.objects.get(identifier="claude-code"),
                                      role="implementer")
    rev = AgentProfile.objects.create(name="Rev", adapter=AgentAdapter.objects.get(identifier="codex"), role="reviewer")
    off = AgentProfile.objects.create(name="Off", adapter=AgentAdapter.objects.get(identifier="codex"), enabled=False)

    assert jr(client_admin, "GET", "/api/team")[1]["steps"] == []
    status, data = jr(client_admin, "PUT", "/api/team",
                     {"steps": [{"profile_id": rev.id, "role": "reviewer"}, {"profile_id": imp.id, "role": ""}]})
    assert status == 200
    assert data["steps"] == [{"profile_id": rev.id, "role": "reviewer"}, {"profile_id": imp.id, "role": "implementer"}]
    assert jr(client_admin, "GET", "/api/team")[1]["steps"] == data["steps"]  # order kept

    for bad in ({"steps": "x"}, {"steps": [{"profile_id": 999999}]}, {"steps": [{"profile_id": off.id}]},
                {"steps": [{"profile_id": imp.id}, {"profile_id": imp.id}]},
                {"steps": [{"profile_id": imp.id}] * 11}):
        assert jr(client_admin, "PUT", "/api/team", bad)[0] == 400, bad

    rev.delete()  # deleted profiles silently drop out of the saved lineup
    assert jr(client_admin, "GET", "/api/team")[1]["steps"] == [{"profile_id": imp.id, "role": "implementer"}]


@pytest.mark.django_db
def test_default_lineup_is_per_user(client, admin_user, plain_user, fixture_profile):
    client.force_login(admin_user)
    assert jr(client, "PUT", "/api/team", {"steps": [{"profile_id": fixture_profile.id}]})[0] == 200
    client.force_login(plain_user)  # non-staff users keep their own lineup
    assert jr(client, "GET", "/api/team")[1]["steps"] == []
    assert jr(client, "PUT", "/api/team", {"steps": [{"profile_id": fixture_profile.id, "role": "tester"}]})[0] == 200
    client.force_login(admin_user)
    assert jr(client, "GET", "/api/team")[1]["steps"][0]["role"] == fixture_profile.role
    client.logout()
    assert client.get("/api/team").status_code == 401
