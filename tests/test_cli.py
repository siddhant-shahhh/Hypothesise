"""CLI tests.

ADR-009 makes the first five minutes the thing this product is judged on, so
`hypothesise --help` and every subcommand's argument surface are part of the
deliverable rather than an afterthought.
"""

from pathlib import Path

import pytest
from hypothesise.__main__ import build_parser, main


def test_help_lists_every_subcommand(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    for cmd in ("api", "proxy", "doctor", "migrate", "version"):
        assert cmd in out


def test_help_tells_a_stranger_where_to_start(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "hypothesise api --demo" in capsys.readouterr().out


def test_version(capsys):
    assert main(["version"]) == 0
    assert "hypothesise 0.1.0a3" in capsys.readouterr().out


def test_no_subcommand_is_an_error():
    with pytest.raises(SystemExit) as e:
        main([])
    assert e.value.code != 0


def test_migrate_runs_and_reports(capsys, tmp_path):
    url = f"sqlite:///{tmp_path / 'w.db'}"
    assert main(["migrate", "--db", url]) == 0
    assert "applied" in capsys.readouterr().out

    assert main(["migrate", "--db", url]) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_migrate_check_exit_code_signals_pending(capsys, tmp_path):
    """Non-zero when migrations are pending, so it works as a deploy gate."""
    url = f"sqlite:///{tmp_path / 'w.db'}"
    assert main(["migrate", "--db", url, "--check"]) == 1
    main(["migrate", "--db", url])
    capsys.readouterr()
    assert main(["migrate", "--db", url, "--check"]) == 0
    assert "up to date" in capsys.readouterr().out


def test_proxy_refuses_enforce_without_a_control_plane(tmp_path, capsys):
    """Nobody to ask means ASK has no honest meaning (ADR-017)."""
    import json

    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"mcpServers": {}, "hypothesise": {"mode": "enforce"}}))
    assert main(["proxy", "--config", str(cfg)]) == 2
    assert "enforce mode needs a control plane" in capsys.readouterr().err


def test_proxy_reports_a_missing_config(tmp_path, capsys):
    assert main(["proxy", "--config", str(tmp_path / "nope.json")]) == 1
    assert "cannot load config" in capsys.readouterr().err


def test_parser_defaults():
    args = build_parser().parse_args(["api"])
    assert args.host == "127.0.0.1"      # localhost only — no auth yet (ADR-009)
    assert args.port == 8080
    assert args.demo is False

    args = build_parser().parse_args(["proxy"])
    assert args.detect is False          # record-only unless asked


def test_api_banner_names_the_dashboard_file_it_serves():
    """"I changed the dashboard and nothing changed" has exactly one cause
    worth checking first: a non-editable install leaves a real copy of the
    static folder in site-packages, and that copy shadows the repo. The banner
    prints the absolute path so the answer is on screen, not in a bug report."""
    src = (Path(__file__).resolve().parents[1]
           / "packages" / "cli" / "hypothesise" / "__main__.py").read_text()
    body = src.split("def _cmd_api", 1)[1].split("def _cmd_proxy", 1)[0]
    assert "STATIC" in body
    assert "index.html" in body
    assert "st_mtime" in body


def test_free_port_skips_a_port_that_is_taken():
    import socket

    from hypothesise.__main__ import _free_port

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        got = _free_port("127.0.0.1", taken)
        assert got is not None and got != taken


def test_demo_never_touches_the_real_database(tmp_path, monkeypatch):
    """ADR-040: sample sessions went into the real database and stayed there,
    indistinguishable from real activity, on the first Windows install."""
    import hypothesise.__main__ as m

    monkeypatch.setenv("HYPOTHESISE_HOME", str(tmp_path))
    seen = {}

    def fake_create_app(db_url=None, webhook_url=None):
        seen["db"] = db_url
        raise SystemExit(0)

    monkeypatch.setattr("hypothesise_api.app.create_app", fake_create_app)
    with pytest.raises(SystemExit):
        m.main(["api", "--demo", "--port", "0"])
    assert seen["db"] == f"sqlite:///{tmp_path / 'demo.db'}"
