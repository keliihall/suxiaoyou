"""Permission engine tests — including two-dimensional matching."""

from app.agent.permission import (
    GLOBAL_DEFAULTS,
    RejectedError,
    disabled_tools,
    evaluate,
    intersect_permission_rulesets,
    merge_rulesets,
    parse_permission_snapshot,
    parse_session_permissions,
    presets_to_ruleset,
    serialize_permission_snapshot,
)
from app.schemas.agent import PermissionRule, Ruleset
from app.session.prompt import _merge_prompt_permission_layers


class TestEvaluate:
    def test_wildcard_allow(self):
        rs = Ruleset(rules=[PermissionRule(action="allow", permission="*")])
        assert evaluate("read", "*", rs) == "allow"
        assert evaluate("bash", "*", rs) == "allow"

    def test_wildcard_deny(self):
        rs = Ruleset(rules=[PermissionRule(action="deny", permission="*")])
        assert evaluate("read", "*", rs) == "deny"

    def test_last_match_wins(self):
        rs = Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
            PermissionRule(action="deny", permission="bash"),
        ])
        assert evaluate("read", "*", rs) == "allow"
        assert evaluate("bash", "*", rs) == "deny"

    def test_ask_permission(self):
        rs = Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
            PermissionRule(action="ask", permission="write"),
        ])
        assert evaluate("read", "*", rs) == "allow"
        assert evaluate("write", "*", rs) == "ask"

    def test_glob_prefix(self):
        rs = Ruleset(rules=[
            PermissionRule(action="deny", permission="*"),
            PermissionRule(action="allow", permission="read*"),
        ])
        assert evaluate("read", "*", rs) == "allow"
        assert evaluate("write", "*", rs) == "deny"

    def test_no_match_defaults_deny(self):
        rs = Ruleset(rules=[])
        assert evaluate("anything", "*", rs) == "deny"

    def test_global_defaults_ask_for_all_execution_and_file_mutation(self):
        for permission in (
            "bash", "code_execute", "write", "edit", "apply_patch",
            "office", "restore_file_version",
        ):
            assert evaluate(permission, "*", GLOBAL_DEFAULTS) == "ask"


class TestPermissionPresets:
    def test_run_commands_allows_shell_and_python(self):
        rules = merge_rulesets(GLOBAL_DEFAULTS, presets_to_ruleset({"run_commands": True}))
        assert evaluate("bash", "*", rules) == "allow"
        assert evaluate("code_execute", "*", rules) == "allow"

    def test_file_changes_allows_every_file_mutator(self):
        rules = merge_rulesets(GLOBAL_DEFAULTS, presets_to_ruleset({"file_changes": True}))
        assert evaluate("write", "*", rules) == "allow"
        assert evaluate("edit", "*", rules) == "allow"
        assert evaluate("apply_patch", "*", rules) == "allow"
        assert evaluate("office", "*", rules) == "allow"
        assert evaluate("restore_file_version", "*", rules) == "allow"

    def test_exact_match(self):
        rs = Ruleset(rules=[
            PermissionRule(action="deny", permission="*"),
            PermissionRule(action="allow", permission="grep"),
        ])
        assert evaluate("grep", "*", rs) == "allow"
        assert evaluate("glob", "*", rs) == "deny"


class TestTwoDimensionalMatching:
    """Tests for the two-dimensional (tool + resource) permission matching."""

    def test_env_file_ask(self):
        """Reading .env files should require asking."""
        rs = Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
            PermissionRule(action="ask", permission="read", pattern="*.env"),
        ])
        assert evaluate("read", "/tmp/test.py", rs) == "allow"
        assert evaluate("read", "/project/.env", rs) == "ask"
        assert evaluate("read", "config.env", rs) == "ask"

    def test_env_example_allowed(self):
        """Reading .env.example should be allowed even when .env.* is ask."""
        rs = Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
            PermissionRule(action="ask", permission="read", pattern="*.env"),
            PermissionRule(action="ask", permission="read", pattern="*.env.*"),
            PermissionRule(action="allow", permission="read", pattern="*.env.example"),
        ])
        assert evaluate("read", ".env", rs) == "ask"
        assert evaluate("read", ".env.local", rs) == "ask"
        assert evaluate("read", ".env.example", rs) == "allow"

    def test_resource_pattern_only_affects_specified_tool(self):
        """Resource pattern on 'read' shouldn't affect 'write'."""
        rs = Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
            PermissionRule(action="ask", permission="read", pattern="*.env"),
        ])
        assert evaluate("write", ".env", rs) == "allow"  # Write not affected
        assert evaluate("read", ".env", rs) == "ask"

    def test_directory_pattern(self):
        """Deny write to specific directory."""
        rs = Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
            PermissionRule(action="deny", permission="write", pattern="/etc/*"),
        ])
        assert evaluate("write", "/etc/passwd", rs) == "deny"
        assert evaluate("write", "/home/user/test.txt", rs) == "allow"

    def test_wildcard_resource_matches_all(self):
        """Default pattern='*' matches all resources."""
        rs = Ruleset(rules=[
            PermissionRule(action="deny", permission="bash"),
        ])
        assert evaluate("bash", "*", rs) == "deny"
        assert evaluate("bash", "/any/path", rs) == "deny"
        assert evaluate("bash", "anything", rs) == "deny"


class TestMergeRulesets:
    def test_merge_order(self):
        base = Ruleset(rules=[PermissionRule(action="allow", permission="*")])
        override = Ruleset(rules=[PermissionRule(action="deny", permission="bash")])
        merged = merge_rulesets(base, override)
        assert evaluate("read", "*", merged) == "allow"
        assert evaluate("bash", "*", merged) == "deny"

    def test_later_layer_overrides(self):
        layer1 = Ruleset(rules=[PermissionRule(action="deny", permission="bash")])
        layer2 = Ruleset(rules=[PermissionRule(action="allow", permission="bash")])
        merged = merge_rulesets(layer1, layer2)
        assert evaluate("bash", "*", merged) == "allow"

    def test_three_layers(self):
        defaults = Ruleset(rules=[PermissionRule(action="allow", permission="*")])
        agent = Ruleset(rules=[PermissionRule(action="deny", permission="write")])
        session = Ruleset(rules=[PermissionRule(action="allow", permission="write")])
        merged = merge_rulesets(defaults, agent, session)
        assert evaluate("write", "*", merged) == "allow"

    def test_session_overrides_agent_for_resource(self):
        """Session can override agent-level env restriction."""
        agent = Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
            PermissionRule(action="ask", permission="read", pattern="*.env"),
        ])
        session = Ruleset(rules=[
            PermissionRule(action="allow", permission="read", pattern="*.env"),
        ])
        merged = merge_rulesets(agent, session)
        assert evaluate("read", ".env", merged) == "allow"

    def test_authoritative_parent_snapshot_blocks_stale_child_session_allow(self):
        parent_snapshot = Ruleset(rules=[
            PermissionRule(action="deny", permission="bash"),
        ])
        stale_child_session = Ruleset(rules=[
            PermissionRule(action="allow", permission="bash"),
        ])

        merged = _merge_prompt_permission_layers(
            Ruleset(),
            Ruleset(),
            parent_snapshot,
            stale_child_session,
            request_is_authoritative=True,
            enforce_current_ceiling=True,
        )

        assert evaluate("bash", "*", merged) == "deny"

    def test_current_agent_deny_blocks_stale_authoritative_allow(self):
        current_agent = Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
            PermissionRule(action="deny", permission="bash"),
        ])
        stale_snapshot = Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
            PermissionRule(action="allow", permission="bash"),
        ])

        merged = _merge_prompt_permission_layers(
            current_agent,
            Ruleset(),
            stale_snapshot,
            Ruleset(),
            request_is_authoritative=True,
            enforce_current_ceiling=True,
        )

        assert evaluate("bash", "*", merged) == "deny"

    def test_current_session_deny_blocks_stale_authoritative_allow(self):
        stale_snapshot = Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
            PermissionRule(action="allow", permission="web_search"),
        ])
        current_session = Ruleset(rules=[
            PermissionRule(action="deny", permission="web_search"),
        ])

        merged = _merge_prompt_permission_layers(
            Ruleset(rules=[
                PermissionRule(action="allow", permission="*"),
            ]),
            Ruleset(),
            stale_snapshot,
            current_session,
            request_is_authoritative=True,
            enforce_current_ceiling=True,
        )

        assert evaluate("web_search", "*", merged) == "deny"

    def test_authoritative_deny_blocks_current_agent_allow(self):
        current_agent = Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
            PermissionRule(action="allow", permission="bash"),
        ])
        parent_snapshot = Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
            PermissionRule(action="deny", permission="bash"),
        ])

        merged = _merge_prompt_permission_layers(
            current_agent,
            Ruleset(),
            parent_snapshot,
            Ruleset(),
            request_is_authoritative=True,
            enforce_current_ceiling=True,
        )

        assert evaluate("bash", "*", merged) == "deny"

    def test_changed_agent_allow_to_ask_tightens_goal_with_baseline(self):
        old_agent = Ruleset(rules=[
            PermissionRule(action="allow", permission="web_search"),
        ])
        current_agent = Ruleset(rules=[
            PermissionRule(action="ask", permission="web_search"),
        ])
        old_goal = Ruleset(rules=[
            PermissionRule(action="allow", permission="web_search"),
        ])

        merged = _merge_prompt_permission_layers(
            current_agent,
            Ruleset(),
            old_goal,
            Ruleset(),
            request_is_authoritative=True,
            enforce_current_ceiling=True,
            goal_policy_baseline=(GLOBAL_DEFAULTS, old_agent),
        )

        assert evaluate("web_search", "*", merged) == "ask"

    def test_unchanged_agent_ask_preserves_authorized_goal_allow(self):
        agent = Ruleset(rules=[
            PermissionRule(action="ask", permission="bash"),
        ])
        old_goal = Ruleset(rules=[
            PermissionRule(action="allow", permission="bash"),
        ])

        merged = _merge_prompt_permission_layers(
            agent,
            Ruleset(),
            old_goal,
            Ruleset(),
            request_is_authoritative=True,
            enforce_current_ceiling=True,
            goal_policy_baseline=(GLOBAL_DEFAULTS, agent),
        )

        assert evaluate("bash", "*", merged) == "allow"


class TestIntersectPermissionRulesets:
    def test_allow_and_deny_intersection_is_deny_in_both_orders(self):
        allowed = Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
        ])
        denied = Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
            PermissionRule(action="deny", permission="bash"),
        ])

        assert evaluate(
            "bash", "*", intersect_permission_rulesets(allowed, denied)
        ) == "deny"
        assert evaluate(
            "bash", "*", intersect_permission_rulesets(denied, allowed)
        ) == "deny"
        assert evaluate(
            "read", "*", intersect_permission_rulesets(allowed, denied)
        ) == "allow"

    def test_ask_is_stricter_than_allow_but_not_deny(self):
        allowed = Ruleset(rules=[
            PermissionRule(action="allow", permission="web_search"),
        ])
        asked = Ruleset(rules=[
            PermissionRule(action="ask", permission="web_search"),
        ])
        denied = Ruleset(rules=[
            PermissionRule(action="deny", permission="web_search"),
        ])

        assert evaluate(
            "web_search", "*", intersect_permission_rulesets(allowed, asked)
        ) == "ask"
        assert evaluate(
            "web_search", "*", intersect_permission_rulesets(asked, denied)
        ) == "deny"

    def test_intersection_preserves_two_dimensional_path_policy(self):
        broad_allow = Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
        ])
        current_path_policy = Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
            PermissionRule(
                action="deny",
                permission="write",
                pattern="/etc/*",
            ),
        ])

        for effective in (
            intersect_permission_rulesets(broad_allow, current_path_policy),
            intersect_permission_rulesets(current_path_policy, broad_allow),
        ):
            assert evaluate("write", "/etc/passwd", effective) == "deny"
            assert evaluate("write", "/workspace/result.txt", effective) == "allow"

    def test_intersection_survives_snapshot_roundtrip(self):
        original = intersect_permission_rulesets(
            Ruleset(rules=[
                PermissionRule(action="allow", permission="*"),
                PermissionRule(action="ask", permission="bash"),
            ]),
            Ruleset(rules=[
                PermissionRule(action="allow", permission="*"),
                PermissionRule(
                    action="deny",
                    permission="read",
                    pattern="*.env",
                ),
            ]),
        )

        restored = parse_permission_snapshot(
            serialize_permission_snapshot(original)
        )

        assert restored is not None
        for permission, pattern, expected in (
            ("read", "README.md", "allow"),
            ("read", ".env", "deny"),
            ("bash", "*", "ask"),
        ):
            assert evaluate(permission, pattern, restored) == expected

    def test_malformed_authority_snapshot_is_rejected_as_a_whole(self):
        snapshot = serialize_permission_snapshot(Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
        ]))
        snapshot["rules"].append({
            "action": "not-a-real-action",
            "permission": "bash",
            "pattern": "*",
        })

        assert parse_permission_snapshot(snapshot) is None


class TestDisabledTools:
    def test_finds_denied(self):
        rs = Ruleset(rules=[
            PermissionRule(action="allow", permission="*"),
            PermissionRule(action="deny", permission="bash"),
            PermissionRule(action="deny", permission="write"),
        ])
        denied = disabled_tools(["read", "write", "bash", "grep"], rs)
        assert denied == {"bash", "write"}

    def test_all_allowed(self):
        rs = Ruleset(rules=[PermissionRule(action="allow", permission="*")])
        denied = disabled_tools(["read", "write", "bash"], rs)
        assert denied == set()


class TestGlobalDefaults:
    def test_defaults_allow_read(self):
        assert evaluate("read", "*", GLOBAL_DEFAULTS) == "allow"
        assert evaluate("glob", "*", GLOBAL_DEFAULTS) == "allow"
        assert evaluate("grep", "*", GLOBAL_DEFAULTS) == "allow"

    def test_defaults_ask_bash(self):
        assert evaluate("bash", "*", GLOBAL_DEFAULTS) == "ask"

    def test_defaults_ask_write(self):
        assert evaluate("write", "*", GLOBAL_DEFAULTS) == "ask"

    def test_defaults_deny_question(self):
        assert evaluate("question", "*", GLOBAL_DEFAULTS) == "deny"

    def test_defaults_deny_plan_tool(self):
        assert evaluate("plan", "*", GLOBAL_DEFAULTS) == "deny"

    def test_defaults_ask_env_files(self):
        assert evaluate("read", "/project/.env", GLOBAL_DEFAULTS) == "ask"
        assert evaluate("read", ".env.local", GLOBAL_DEFAULTS) == "ask"
        assert evaluate("read", ".env.example", GLOBAL_DEFAULTS) == "allow"


class TestParseSessionPermissions:
    def test_empty_returns_empty(self):
        rs = parse_session_permissions(None)
        assert rs.rules == []

    def test_empty_list_returns_empty(self):
        rs = parse_session_permissions([])
        assert rs.rules == []

    def test_valid_rules(self):
        data = [
            {"action": "allow", "permission": "bash"},
            {"action": "deny", "permission": "write", "pattern": "*.env"},
        ]
        rs = parse_session_permissions(data)
        assert len(rs.rules) == 2
        assert rs.rules[0].action == "allow"
        assert rs.rules[0].permission == "bash"
        assert rs.rules[1].pattern == "*.env"

    def test_malformed_skipped(self):
        data = [
            {"action": "invalid_action"},  # Invalid action
            {"action": "allow", "permission": "read"},
        ]
        rs = parse_session_permissions(data)
        # The invalid one should be skipped
        assert len(rs.rules) == 1
        assert rs.rules[0].permission == "read"
