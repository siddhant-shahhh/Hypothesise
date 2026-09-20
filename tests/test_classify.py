"""Classifier tests.

`resource.class` drives every risk floor and every sensitive-class detector.
A misclassification here becomes a false positive or a missed attack later, so
these cases are the ones most likely to bite: .env beating CONFIG, internal
hosts not counting as egress.
"""

import pytest
from weaver_core.classify import classify_action, classify_resource
from weaver_core.events import Action, ResourceClass


@pytest.mark.parametrize(
    "uri,expected",
    [
        ("file:///app/.env", ResourceClass.SECRET),
        ("file:///root/.aws/credentials", ResourceClass.SECRET),
        ("file:///home/u/.ssh/id_rsa", ResourceClass.SECRET),
        ("file:///app/config/secrets.yaml", ResourceClass.SECRET),
        ("secret://aws/prod-keys", ResourceClass.SECRET),
        ("file:///certs/server.pem", ResourceClass.SECRET),
        ("postgres://prod-db/users", ResourceClass.DATABASE),
        ("mongodb+srv://cluster/app", ResourceClass.DATABASE),
        ("github://acme/api/src/app.py", ResourceClass.SOURCE_CODE),
        ("file:///app/main.py", ResourceClass.SOURCE_CODE),
        ("file:///app/config/logging.yaml", ResourceClass.CONFIG),
        ("file:///app/docker-compose.yml", ResourceClass.CONFIG),
        ("shell://cat /etc/passwd", ResourceClass.SHELL),
        ("slack://C012345", ResourceClass.COMMUNICATION),
    ],
)
def test_resource_classification(uri, expected):
    assert classify_resource(uri) is expected


def test_secret_beats_config():
    """.env and secrets.yaml must not fall through to CONFIG — the floor differs 3x."""
    assert classify_resource("file:///app/.env") is ResourceClass.SECRET
    assert classify_resource("file:///app/secrets.yaml") is ResourceClass.SECRET
    assert classify_resource("file:///app/settings.yaml") is ResourceClass.CONFIG


@pytest.mark.parametrize(
    "uri",
    [
        "http://localhost:8080/health",
        "http://127.0.0.1/status",
        "http://10.0.3.14:8080/status",
        "http://192.168.1.5/api",
        "http://172.16.0.9/api",
        "http://api.internal/v1/health",
        "http://svc.cluster.local/ping",
    ],
)
def test_internal_hosts_are_not_egress(uri):
    """The single most likely false positive in the product. Internal != egress."""
    assert classify_resource(uri) is not ResourceClass.NETWORK_EGRESS


@pytest.mark.parametrize(
    "uri",
    [
        "https://collector.unknown-host.net/ingest",
        "https://pastebin.com/raw/abc",
        "http://198.51.100.7/collect",
    ],
)
def test_external_hosts_are_egress(uri):
    assert classify_resource(uri) is ResourceClass.NETWORK_EGRESS


def test_action_inference():
    assert classify_action("get_file_contents") is Action.READ
    assert classify_action("list_issues") is Action.READ
    assert classify_action("create_pull_request") is Action.WRITE
    assert classify_action("delete_branch") is Action.WRITE
    assert classify_action("exec") is Action.EXECUTE
    assert classify_action("invoke_agent") is Action.INVOKE
    assert classify_action("fetch", ResourceClass.NETWORK_EGRESS) is Action.EGRESS


def test_tool_hints_when_no_uri():
    assert classify_resource("", "shell-mcp", "exec") is ResourceClass.SHELL
    assert classify_resource("", "postgres-mcp", "query") is ResourceClass.DATABASE
    assert classify_resource("", "unknown-mcp", "mystery") is ResourceClass.UNKNOWN
