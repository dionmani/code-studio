"""Antigravity shell-command allow-list and the prompt hand-off between steps."""
import json

import pytest

from agent_engine.adapters.antigravity import AntigravityAdapter
from agent_engine.tasks import build_prompt, describe_changes
from cs_common.agent_policy import PolicyError, clean_allowed_commands


def test_clean_allowed_commands_accepts_plain_prefixes():
    assert clean_allowed_commands(" npm   test \n\npython -m pytest\nnpm test\n") == ["npm test", "python -m pytest"]
    assert clean_allowed_commands(["ls", "wc -l", "./gradlew test"]) == ["ls", "wc -l", "./gradlew test"]
    assert clean_allowed_commands(None) == [] and clean_allowed_commands("") == []


@pytest.mark.parametrize("bad", [
    "npm test; id", "npm test && curl x", "ls | sh", "cat > x", "echo $HOME", "ls `id`", "rm -rf /", "bash",
    "sh -c id", "/bin/bash", "curl", "env FOO=1", "sudo ls", "python", "npm", "find .", "git status", "*",
    "command(*)", "'quoted'", "x" * 101,
])
def test_clean_allowed_commands_rejects_dangerous_entries(bad):
    with pytest.raises(PolicyError):
        clean_allowed_commands([bad])


def test_too_many_commands():
    with pytest.raises(PolicyError):
        clean_allowed_commands([f"tool{i} run" for i in range(21)])


def _argv(policy):
    a = AntigravityAdapter()
    a.find_executable = lambda: "/usr/local/bin/agy"
    argv, stdin = a.build_command("do it", None, policy)
    return argv


def test_agy_gets_session_allow_rules_only_when_configured():
    argv = _argv({"allowed_commands": ["npm test", "python -m pytest"], "timeout_seconds": 60})
    settings = [x for x in argv if x.startswith("--settings=")]
    assert len(settings) == 1
    assert json.loads(settings[0].split("=", 1)[1]) == {
        "permissions": {"allow": ["command(npm test)", "command(python -m pytest)"]}}
    assert "--dangerously-skip-permissions" not in argv and argv[-1] == "--print=do it"
    # none configured -> no settings flag; read-only -> never
    assert not any(x.startswith("--settings") for x in _argv({}))
    assert not any(x.startswith("--settings") for x in _argv({"read_only": True, "allowed_commands": ["npm test"]}))
    # an invalid list that slipped through is dropped, not passed to agy
    assert not any(x.startswith("--settings") for x in _argv({"allowed_commands": ["bash"]}))


def test_agy_prompt_note_lists_allowed_commands():
    a = AntigravityAdapter()
    assert "refused" in a.prompt_notes({}) and "may run" not in a.prompt_notes({})
    assert "npm test" in a.prompt_notes({"allowed_commands": ["npm test"]})


def test_later_steps_see_earlier_changes_and_no_git_hint():
    changes = [{"path": "a.py", "action": "modified", "diff": "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"},
               {"path": ".env", "action": "modified", "sensitive": True, "diff": "-SECRET=1\n+SECRET=2\n"},
               {"path": "logo.png", "action": "added", "binary": True, "diff": "Binary files differ\n"}]
    text = describe_changes(changes)
    assert "- modified: a.py" in text and "+y" in text and "- added: logo.png" in text
    assert "SECRET" not in text
    prompt = build_prompt("reviewer", "check it", [{"role": "implementer", "adapter": "codex", "status": "succeeded",
                                                    "summary": "done"}], True, text, "NOTE-X")
    assert ".git directory" in prompt and "NOTE-X" in prompt and "+y" in prompt
    assert prompt.index("+y") < prompt.index("Objective:")
    assert describe_changes([]).startswith("Earlier steps in this task have not changed")


def test_prompt_diff_is_bounded():
    big = [{"path": f"f{i}.txt", "action": "modified", "diff": "+" + "x" * 20_000 + "\n"} for i in range(5)]
    text = describe_changes(big)
    assert len(text) < 45_000 and "omitted: prompt size limit" in text


@pytest.mark.django_db
def test_profile_api_validates_allowed_commands(client_admin):
    from agents_client.models import AgentAdapter

    AgentAdapter.objects.create(identifier="antigravity", available=True)
    body = {"name": "Agy impl", "adapter": "antigravity", "role": "implementer",
            "execution_policy": {"allowed_commands": ["npm test; curl evil"]}}
    r = client_admin.post("/api/profiles", body, content_type="application/json")
    assert r.status_code == 400 and "plain command prefix" in r.json()["error"]
    body["execution_policy"] = {"allowed_commands": ["npm test", " npm  test"]}
    r = client_admin.post("/api/profiles", body, content_type="application/json")
    assert r.status_code == 200 and r.json()["execution_policy"]["allowed_commands"] == ["npm test"]
    # read-only profiles never keep a command list
    body.update(name="Agy rev", role="reviewer", execution_policy={"read_only": True, "allowed_commands": ["npm test"]})
    r = client_admin.post("/api/profiles", body, content_type="application/json")
    assert "allowed_commands" not in r.json()["execution_policy"]


def test_engine_rejects_bad_command_list(live_engine, tmp_path):
    from tests.test_engine import Signed

    eng = Signed(live_engine.url)
    src = tmp_path / "p"
    src.mkdir()
    r = eng.req("POST", "/tasks", {"objective": "x", "project": {"root": str(src)},
                                   "assignments": [{"adapter": "fixture", "policy": {"allowed_commands": ["bash"]}}]})
    assert r.status_code == 400 and "can't be allowed" in r.text
