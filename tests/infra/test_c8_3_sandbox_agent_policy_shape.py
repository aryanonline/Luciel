"""Arc 9 C8.3 - shape guard for the scoped sandbox-agent IAM policy.

This test is the structural counterpart to doctrine D8.1:
"luciel-sandbox-agent uses a scoped policy that names every action it
needs. *:* never returns to the scope."

If a future commit re-introduces `Action: "*"` with `Resource: "*"`,
or removes the explicit DenySelfPrivilegeEscalation stanza, this test
breaks at PR time before the change can land in prod.
"""

from __future__ import annotations

from pathlib import Path

import yaml

CFN_PATH = (
    Path(__file__).resolve().parents[2]
    / "cfn"
    / "luciel-sandbox-agent-policy.yaml"
)


def _load_policy_doc() -> dict:
    """Parse the YAML and return the IAM PolicyDocument dict."""
    # CloudFormation YAML uses !Sub / !Ref tags. We don't need to
    # resolve them for shape checks; tell yaml to ignore them.
    class _CfnLoader(yaml.SafeLoader):
        pass

    def _passthrough(loader, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    for tag in ("!Sub", "!Ref", "!GetAtt", "!Join", "!Equals", "!If"):
        _CfnLoader.add_constructor(tag, _passthrough)

    with CFN_PATH.open() as f:
        doc = yaml.load(f, Loader=_CfnLoader)

    return doc["Resources"]["LucielSandboxAgentPolicy"]["Properties"][
        "PolicyDocument"
    ]


def test_no_wildcard_action_with_wildcard_resource() -> None:
    """The policy must never combine Action='*' with Resource='*'.

    This is the structural guarantee that the policy is actually
    scoped. Wildcard reads on Resource='*' are allowed for services
    that don't support resource-arn scoping (e.g. cloudwatch:ListMetrics),
    but Action must always be enumerated.
    """
    doc = _load_policy_doc()
    for stmt in doc["Statement"]:
        actions = stmt.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        if "*" in actions:
            raise AssertionError(
                f"Statement {stmt.get('Sid')} uses Action='*'. "
                f"D8.1 forbids un-enumerated actions."
            )


def test_deny_self_privilege_escalation_present() -> None:
    """The explicit DenySelfPrivilegeEscalation stanza is the core
    safety net. If a future allow gets too permissive on iam:*, this
    deny still blocks the agent from creating a second access key or
    attaching AdministratorAccess to itself."""
    doc = _load_policy_doc()
    sids = {stmt.get("Sid") for stmt in doc["Statement"]}
    assert "DenySelfPrivilegeEscalation" in sids, (
        "DenySelfPrivilegeEscalation stanza is missing. The agent "
        "MUST be unable to self-escalate."
    )
    deny_stmt = next(
        s for s in doc["Statement"] if s.get("Sid") == "DenySelfPrivilegeEscalation"
    )
    assert deny_stmt["Effect"] == "Deny"
    blocked = set(deny_stmt["Action"])
    required_blocks = {
        "iam:CreateAccessKey",
        "iam:DeleteAccessKey",
        "iam:UpdateAccessKey",
        "iam:CreateUser",
        "iam:CreateLoginProfile",
        "iam:PutUserPolicy",
    }
    missing = required_blocks - blocked
    assert not missing, (
        f"DenySelfPrivilegeEscalation must block {required_blocks}, "
        f"missing {missing}."
    )


def test_deny_admin_policy_attachment_present() -> None:
    """A separate deny blocks any attempt to re-attach
    AdministratorAccess. Belt + braces guard against accidental
    rollback to *:*."""
    doc = _load_policy_doc()
    deny = next(
        (s for s in doc["Statement"] if s.get("Sid") == "DenyAdminPolicyAttachment"),
        None,
    )
    assert deny is not None, "DenyAdminPolicyAttachment stanza missing."
    assert deny["Effect"] == "Deny"
    condition = deny["Condition"]["ArnEquals"]["iam:PolicyARN"]
    assert "AdministratorAccess" in condition, (
        f"DenyAdminPolicyAttachment must condition on the "
        f"AdministratorAccess ARN, got {condition}"
    )


def test_writes_scoped_to_luciel_resources() -> None:
    """Every Allow-write stanza (Sid starts with 'AllowSnsWrite',
    'AllowCloudWatchAlarmWrites', 'AllowCfnWriteOnLucielProd',
    'AllowSsmWriteOnPagerDutyParams', 'AllowLogsWriteOnLucielGroups')
    must scope Resource to luciel-* or /luciel/* ARNs.

    Read stanzas may use Resource='*' for services that don't support
    resource scoping on list/describe (CloudFormation, CloudWatch list).
    """
    doc = _load_policy_doc()
    write_sids = {
        "AllowSnsWriteOnLucielProd",
        "AllowCloudWatchAlarmWrites",
        "AllowCfnWriteOnLucielProd",
        "AllowSsmWriteOnPagerDutyParams",
        "AllowLogsWriteOnLucielGroups",
    }
    for stmt in doc["Statement"]:
        if stmt.get("Sid") not in write_sids:
            continue
        resources = stmt["Resource"]
        if isinstance(resources, str):
            resources = [resources]
        for r in resources:
            assert "luciel" in r.lower(), (
                f"Write statement {stmt['Sid']} has unscoped "
                f"Resource {r!r}; must contain 'luciel'."
            )


def test_managed_policy_name_is_stable() -> None:
    """The ManagedPolicyName is part of the ARN the deny condition
    keys on -- it must be the literal we documented in the runbook."""
    with CFN_PATH.open() as f:
        text = f.read()
    assert "ManagedPolicyName: luciel-sandbox-agent-scoped" in text, (
        "The policy name 'luciel-sandbox-agent-scoped' is referenced by "
        "the AllowSelfIdentity stanza's Resource ARN. Renaming it "
        "without updating the ARN breaks the self-introspection path."
    )
