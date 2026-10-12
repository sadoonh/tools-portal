"""Operator-only Fargate image probe; no human auth/session forgery or AWS access.

Run as a one-off command override of the Terraform-managed kernel task definition.
The agent uses an ephemeral local-only test token; it is never printed. The task
exits when done, so it is not a persistent sandbox or a replacement orchestrator.
This verifies execution/agent behavior, NOT hub authorization or S3 persistence.
"""
import base64
import json
import os
import secrets
import stat
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def request(path, payload=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request("http://127.0.0.1:2717" + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=90) as response:
        return json.load(response)


def main():
    assert os.getuid() == 10001
    assert stat.S_IMODE(os.stat("/tmp").st_mode) == 0o1777, "Non-root sessions require sticky writable /tmp"
    token = secrets.token_hex(32)
    environment = dict(os.environ, MARIMOHUB_AGENT_TOKEN=token)
    agent = subprocess.Popen(["/usr/local/bin/python3", "/opt/marimohub/fargate_agent.py"], env=environment)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                health = request("/health", token=token)
                assert health["protocolVersion"] == 2
                break
            except urllib.error.URLError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)
        try:
            request("/health")
        except urllib.error.HTTPError as error:
            assert error.code == 401
        else:
            raise AssertionError("Agent allowed an unauthenticated request")
        # Exercise the hub's actual /files/write credential/context/bridge paths,
        # not just /workspace. Dummy probe data is unrelated to real user sessions.
        files = {
            "/tmp/.marimohub-kernel-token": "mhub_kernel_" + secrets.token_urlsafe(32),
            "/tmp/marimohub-context/operator-smoke.json": '{"probe":true}',
            "/tmp/marimohub-config/marimo/marimo.toml": "# operator probe\n",
            "/tmp/marimohub-bridge/operator-smoke/probe.txt": "probe",
        }
        written = request("/files/write", {"files": [
            {"path": path, "contentBase64": base64.b64encode(content.encode()).decode()}
            for path, content in files.items()
        ]}, token=token)
        assert written["written"] == len(files)
        assert all(Path(path).read_text() == content for path, content in files.items())
        source = '''import marimo
app = marimo.App()
@app.cell
def _():
    import marimo as mo
    return (mo,)
@app.cell
def _(mo):
    answer = 6 * 7
    mo.md(f"Smoke test answer: {answer}")
    return (answer,)
if __name__ == "__main__":
    app.run()
'''
        Path("/workspace/smoke_notebook.py").write_text(source)
        result = request("/exec", {
            "command": "marimo export html /workspace/smoke_notebook.py -o /workspace/smoke.html --no-include-code",
            "cwd": "/workspace", "timeoutMs": 60000,
        }, token=token)
        assert result["success"], "Notebook execution failed: " + result.get("stderr", "")
        assert "Smoke test answer: 42" in Path("/workspace/smoke.html").read_text()
        clean = request("/exec", {"command": "test -z \"${MARIMOHUB_AGENT_TOKEN:-}\"", "cwd": "/workspace"}, token=token)
        assert clean["success"], "Agent token leaked to notebook child environment"
        print(json.dumps({"event": "kernel_operator_smoke_passed", "uid": os.getuid(),
                          "protocol": 2, "answer": 42, "unauthenticated_agent_denied": True,
                          "session_files_written_via_agent": True, "tmp_mode": "1777",
                          "agent_token_not_in_child_environment": True}), flush=True)
    finally:
        agent.terminate()
        try:
            agent.wait(timeout=5)
        except subprocess.TimeoutExpired:
            agent.kill()
            agent.wait(timeout=5)


if __name__ == "__main__":
    main()
