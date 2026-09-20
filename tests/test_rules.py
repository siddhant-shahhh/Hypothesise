"""Policy rule tests.

The asymmetry is the whole design: block/ask raise a verdict, allow lowers it,
and allow can never override a CRITICAL detector.
"""


from weaver_core.events import Event, Resource, ResourceClass, Tool, Verdict
from weaver_core.policy.rules import Rule, RuleSet, describe


def _ev(server="shell-mcp", name="exec", uri="file:///app/.env",
        cls=ResourceClass.SECRET, agent="coding-agent"):
    return Event(agent_id=agent, session_id="s", step=0,
                 tool=Tool(server, name), resource=Resource(uri, cls))


def test_block_rule_raises_verdict():
    rs = RuleSet.from_config([{"name": "no shell to secrets", "action": "block",
                               "match": {"tool_server": "shell-mcp",
                                         "resource_class": "secret"}}])
    v, rule = rs.apply(_ev(), Verdict.ALLOW)
    assert v is Verdict.BLOCK
    assert rule.name == "no shell to secrets"


def test_ask_rule_raises_but_not_past_block():
    rs = RuleSet.from_config([{"name": "ask", "action": "ask",
                               "match": {"resource_class": "secret"}}])
    assert rs.apply(_ev(), Verdict.ALLOW)[0] is Verdict.ASK
    # already BLOCK — an ask rule must not lower it
    assert rs.apply(_ev(), Verdict.BLOCK)[0] is Verdict.BLOCK


def test_allow_rule_lowers_a_behavioural_verdict():
    rs = RuleSet.from_config([{"name": "trusted", "action": "allow",
                               "match": {"agent_id": "coding-agent"}}])
    assert rs.apply(_ev(), Verdict.ASK, has_critical=False)[0] is Verdict.ALLOW


def test_allow_rule_cannot_override_a_critical_detector():
    """The difference between an allowlist and a backdoor.

    Somebody silencing noise with "always allow shell-mcp" must not thereby
    switch off retry_after_denial and shell_exfil.
    """
    rs = RuleSet.from_config([{"name": "shut it up", "action": "allow",
                               "match": {"tool_server": "shell-mcp"}}])
    v, rule = rs.apply(_ev(), Verdict.BLOCK, has_critical=True)
    assert v is Verdict.BLOCK
    assert rule is None


def test_glob_matching():
    rs = RuleSet.from_config([{"name": "prod", "action": "ask",
                               "match": {"resource_uri": "postgres://prod-*"}}])
    prod = _ev(uri="postgres://prod-db/users", cls=ResourceClass.DATABASE)
    dev = _ev(uri="postgres://dev-db/users", cls=ResourceClass.DATABASE)
    assert rs.apply(prod, Verdict.ALLOW)[0] is Verdict.ASK
    assert rs.apply(dev, Verdict.ALLOW)[0] is Verdict.ALLOW


def test_all_conditions_must_hold():
    rs = RuleSet.from_config([{"name": "both", "action": "block",
                               "match": {"tool_server": "shell-mcp",
                                         "resource_class": "database"}}])
    assert rs.apply(_ev(), Verdict.ALLOW)[0] is Verdict.ALLOW   # class differs


def test_empty_match_never_fires():
    """A rule with no conditions would otherwise match everything — a footgun
    that turns a typo into a fleet-wide block."""
    rs = RuleSet.from_config([{"name": "oops", "action": "block", "match": {}}])
    assert rs.apply(_ev(), Verdict.ALLOW)[0] is Verdict.ALLOW


def test_malformed_rules_are_skipped_not_fatal():
    rs = RuleSet.from_config([
        {"name": "bad action", "action": "explode", "match": {"agent_id": "*"}},
        {"name": "good", "action": "block", "match": {"agent_id": "coding-agent"}},
    ])
    assert len(rs.rules) == 1
    assert rs.apply(_ev(), Verdict.ALLOW)[0] is Verdict.BLOCK


def test_matching_is_case_insensitive():
    rs = RuleSet.from_config([{"name": "c", "action": "block",
                               "match": {"tool_server": "SHELL-MCP"}}])
    assert rs.apply(_ev(), Verdict.ALLOW)[0] is Verdict.BLOCK


def test_describe_is_human_readable():
    r = Rule(name="no prod writes", action=Verdict.BLOCK,
             match={"resource_uri": "postgres://prod-*"}, reason="change freeze")
    text = describe(r)
    assert "no prod writes" in text and "change freeze" in text


def test_engine_applies_rules(tmp_path):
    from weaver_core.engine import Engine

    eng = Engine(rules=RuleSet.from_config([
        {"name": "freeze", "action": "block", "reason": "release freeze",
         "match": {"resource_class": "secret"}}]))
    decision, _ = eng.evaluate(_ev())
    assert decision.verdict is Verdict.BLOCK
    assert "freeze" in decision.sentence
    assert any(d["detector"] == "policy_rule" for d in decision.detectors)
