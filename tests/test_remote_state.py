import base64
import json

from app import remote_state
from app.config import Settings


def _response(status=200, payload=None):
    class Response:
        status_code = status

        def json(self):
            return payload or {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    return Response()


def test_remote_state_pull_writes_bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_BACKEND", "github")
    monkeypatch.setenv("STATE_GITHUB_TOKEN", "token")
    monkeypatch.setenv("STATE_GITHUB_REPO", "owner/repo")
    bundle = {"history": {"posts": [{"uri": "remote"}]}, "engagement": {"replies": []}}
    encoded = base64.b64encode(json.dumps(bundle).encode()).decode()
    monkeypatch.setattr(
        remote_state.httpx,
        "get",
        lambda *args, **kwargs: _response(200, {"content": encoded, "sha": "abc"}),
    )
    settings = Settings(state_dir=tmp_path)

    assert remote_state.pull_state(settings) is True
    assert "remote" in (tmp_path / "history.json").read_text()
    assert (tmp_path / "engagement.json").exists()


def test_remote_state_missing_file_is_safe_to_initialize(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_BACKEND", "github")
    monkeypatch.setenv("STATE_GITHUB_TOKEN", "token")
    monkeypatch.setenv("STATE_GITHUB_REPO", "owner/repo")
    monkeypatch.setattr(
        remote_state.httpx, "get", lambda *args, **kwargs: _response(404)
    )
    settings = Settings(state_dir=tmp_path)
    assert remote_state.pull_state(settings) is True


def test_remote_state_push_creates_bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_BACKEND", "github")
    monkeypatch.setenv("STATE_GITHUB_TOKEN", "token")
    monkeypatch.setenv("STATE_GITHUB_REPO", "owner/repo")
    (tmp_path / "history.json").write_text(json.dumps({"posts": [{"uri": "x"}]}))
    calls = []

    def fake_put(*args, **kwargs):
        calls.append(kwargs.get("json") or {})
        return _response(201, {})

    monkeypatch.setattr(remote_state.httpx, "get", lambda *args, **kwargs: _response(404))
    monkeypatch.setattr(remote_state.httpx, "put", fake_put)
    settings = Settings(state_dir=tmp_path)

    assert remote_state.push_state(settings, "test") is True
    assert calls
    assert calls[0]["message"] == "test"
    assert "content" in calls[0]
