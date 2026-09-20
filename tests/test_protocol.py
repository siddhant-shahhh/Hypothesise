"""Wire protocol tests. Framing bugs here corrupt every conversation."""

from weaver_proxy import protocol as p


def test_request_vs_notification():
    req = p.request(1, "tools/list", {})
    note = p.notification("notifications/initialized")
    assert p.is_request(req) and not p.is_notification(req)
    assert p.is_notification(note) and not p.is_request(note)
    # Replying to a notification is a protocol violation; the distinction is
    # the whole reason this pair of predicates exists.
    assert "id" not in note


def test_response_detection():
    assert p.is_response(p.result(1, {"ok": True}))
    assert p.is_response(p.error(1, p.INTERNAL_ERROR, "boom"))
    assert not p.is_response(p.request(1, "ping"))


def test_encode_decode_roundtrip():
    msg = p.request("abc", "tools/call", {"name": "read_file", "arguments": {"path": "/x"}})
    assert p.encode(msg).endswith(b"\n")     # newline framing
    assert p.decode(p.encode(msg)) == msg


def test_decode_never_raises():
    """A malformed line from an upstream must not kill a live conversation."""
    for bad in [b"", b"\n", b"   ", b"not json", b'{"unclosed":', b"[1,2,3]", b"null", b"12"]:
        assert p.decode(bad) is None


def test_decode_handles_bad_utf8():
    assert p.decode(b"\xff\xfe\x00bad") is None


def test_block_code_outside_reserved_range():
    """Clients must be able to tell a Weaver decision from a transport fault."""
    assert not (-32768 <= p.BLOCKED_BY_POLICY <= -32000)


def test_tool_call_params():
    msg = p.request(1, "tools/call", {"name": "exec", "arguments": {"command": "ls"}})
    assert p.tool_call_params(msg) == ("exec", {"command": "ls"})
    assert p.tool_call_params(p.request(1, "tools/call", {})) == ("", {})
