"""Arc 9 C9.1 — AST guards on luciel-prod-alarms.yaml.

Locks the shape of production CloudWatch alarms to prevent silent
miswires like AWS/ECS vs ECS/ContainerInsights, which produced
INSUFFICIENT_DATA -> Breaching alarm storms before C9.

Doctrine: AWS/ECS namespace exposes only CPUUtilization and
MemoryUtilization on the service dimension. Every other ECS metric
(RunningTaskCount, DesiredTaskCount, PendingTaskCount, task-level
CPU/Memory, etc.) is emitted by Container Insights under the
ECS/ContainerInsights namespace and requires Container Insights to
be enabled on the cluster.

These guards run on every PR (backend-free pytest step in CI).
"""

from __future__ import annotations

from pathlib import Path

import yaml


CFN_PATH = (
    Path(__file__).resolve().parents[2] / "cfn" / "luciel-prod-alarms.yaml"
)


# Metrics that AWS/ECS emits without Container Insights.
AWS_ECS_NATIVE_METRICS = frozenset({"CPUUtilization", "MemoryUtilization"})


def _load_template() -> dict:
    raw = CFN_PATH.read_text()
    # CFN intrinsic functions (!Ref, !Sub, !GetAtt, ...) are not native
    # YAML tags. Treat them as opaque strings for shape checking.
    class _CfnLoader(yaml.SafeLoader):
        pass

    def _passthrough(loader, tag_suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return f"!{tag_suffix} {node.value}"
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node, deep=True)
        return loader.construct_mapping(node, deep=True)

    _CfnLoader.add_multi_constructor("!", _passthrough)
    return yaml.load(raw, Loader=_CfnLoader)


def _alarm_resources() -> dict:
    """Return {LogicalId: Properties} for every CloudWatch::Alarm."""
    template = _load_template()
    resources = template.get("Resources", {})
    return {
        name: spec.get("Properties", {})
        for name, spec in resources.items()
        if spec.get("Type") == "AWS::CloudWatch::Alarm"
    }


def test_no_native_aws_ecs_namespace_misuse():
    """AWS/ECS may only be used with CPUUtilization or MemoryUtilization.

    Any other ECS metric under AWS/ECS will silently emit no datapoints
    and, with TreatMissingData=breaching, produce a permanent ALARM
    state on a healthy service. Catches the C9.1 regression class.
    """
    offenders = []
    for logical_id, props in _alarm_resources().items():
        ns = props.get("Namespace", "")
        metric = props.get("MetricName", "")
        # MetricName may be absent on metric-math alarms (Metrics: [...]);
        # those skip this check by design.
        if not metric:
            continue
        if ns == "AWS/ECS" and metric not in AWS_ECS_NATIVE_METRICS:
            offenders.append(
                f"{logical_id}: AWS/ECS does not emit {metric}; "
                f"use ECS/ContainerInsights instead"
            )
    assert not offenders, "\n".join(offenders)


def test_running_task_count_uses_container_insights():
    """The unhealthy-task alarm specifically reads from Container Insights.

    Locks the exact fix from C9.1: WorkerUnhealthyTaskCountAlarm must
    use ECS/ContainerInsights, not AWS/ECS.
    """
    alarms = _alarm_resources()
    alarm = alarms.get("WorkerUnhealthyTaskCountAlarm")
    assert alarm is not None, (
        "WorkerUnhealthyTaskCountAlarm missing from luciel-prod-alarms.yaml"
    )
    assert alarm.get("MetricName") == "RunningTaskCount", (
        f"WorkerUnhealthyTaskCountAlarm.MetricName must be RunningTaskCount, "
        f"got {alarm.get('MetricName')!r}"
    )
    assert alarm.get("Namespace") == "ECS/ContainerInsights", (
        f"WorkerUnhealthyTaskCountAlarm.Namespace must be "
        f"ECS/ContainerInsights (Container Insights emits RunningTaskCount), "
        f"got {alarm.get('Namespace')!r}"
    )


def test_alarms_have_actions_wired_to_alert_topic():
    """Every alarm must notify AlertTopic on alarm AND on recovery.

    Prevents the silent-alarm class (alarm exists but nobody knows
    when it fires or clears). Verifies alarm cadence is end-to-end.
    """
    missing = []
    for logical_id, props in _alarm_resources().items():
        alarm_actions = props.get("AlarmActions") or []
        ok_actions = props.get("OKActions") or []
        if not alarm_actions:
            missing.append(f"{logical_id}: no AlarmActions wired")
        if not ok_actions:
            missing.append(f"{logical_id}: no OKActions wired")
    assert not missing, "\n".join(missing)


def test_critical_alarms_present():
    """C7 + C9 critical-path alarms must exist in the template.

    These are the alarms that protect tenant isolation (GUC leak,
    audit integrity, ops-role connect velocity). Removing any of them
    without a doctrine update should fail CI.
    """
    required = {
        "luciel-guc-leak-guard-violation",
        "luciel-audit-log-integrity-breach",
        "luciel-admin-audit-write-velocity",
        "luciel-ops-role-connect-velocity",
        "luciel-worker-unhealthy-task-count",
        "luciel-rds-cpu",
        "luciel-rds-connection-count",
        # Arc 9.1 Phase B2 (G7): per-Admin observability.
        # Removing any of these without a doctrine update means a
        # tenant outage could be invisible to the on-call rotation.
        "luciel-per-admin-http-5xx",
        "luciel-per-admin-http-p99-latency",
        "luciel-per-admin-zero-row",
    }
    found = {
        props.get("AlarmName")
        for props in _alarm_resources().values()
        if props.get("AlarmName")
    }
    missing = required - found
    assert not missing, f"missing critical alarms: {sorted(missing)}"
