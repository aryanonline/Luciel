"""Arc 11 Step 6 — shape tests for cfn/knowledge-bucket.yaml.

Mirrors the conventions in ``test_c9_1_prod_alarms_shape.py``: load
the CFN template with a permissive YAML loader that treats CFN
intrinsic functions (``!Ref``, ``!Sub``, ``!GetAtt``, ...) as opaque
strings, then assert the resource shapes.

The CFN stack is NOT deployed by this PR. These tests run on every
PR (backend-free pytest step in CI) so a drift in the template body
surfaces at PR-review time, not at deploy time.

Contracts guarded:

  B1   AWSTemplateFormatVersion + Description present.
  B2   The bucket resource exists with the exact name in the brief.
  B3   ``BucketEncryption`` is SSE-S3 (AES256), KMS NOT used.
  B4   ``VersioningConfiguration`` Status = Suspended.
  B5   All four ``BlockPublicAccess`` flags = true.
  B6   No CORS configuration.
  B7   Lifecycle rule: abort incomplete multipart uploads after 7 days.
  B8   Lifecycle rule: expire noncurrent versions / delete markers
       after 30 days; objects themselves are NOT auto-expired.
  B9   DeletionPolicy Retain + UpdateReplacePolicy Retain on the bucket.
  B10  ``BucketPolicy`` denies non-TLS connections.
  B11  ``ManagedPolicy`` named ``luciel-knowledge-bucket-access``
       attaches to both backend (luciel-ecs-web-role) and worker
       (luciel-ecs-worker-role) task roles.
  B12  Managed policy grants exactly GetObject + PutObject +
       DeleteObject + AbortMultipartUpload. No ListBucket
       (per-object access only).
"""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml


CFN_PATH = (
    Path(__file__).resolve().parents[2] / "cfn" / "knowledge-bucket.yaml"
)


def _load_template() -> dict:
    """Load CFN YAML with intrinsic-function tags as opaque strings."""
    raw = CFN_PATH.read_text(encoding="utf-8")

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


class TestKnowledgeBucketCfnShape(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.template = _load_template()
        cls.resources = cls.template.get("Resources", {})

    def _bucket(self) -> dict:
        for name, spec in self.resources.items():
            if spec.get("Type") == "AWS::S3::Bucket":
                return spec
        self.fail("No AWS::S3::Bucket resource in template")

    def _bucket_props(self) -> dict:
        return self._bucket().get("Properties", {})

    def _managed_policy(self) -> dict:
        for name, spec in self.resources.items():
            if spec.get("Type") == "AWS::IAM::ManagedPolicy":
                return spec
        self.fail("No AWS::IAM::ManagedPolicy resource in template")

    # ----- B1 -----
    def test_b1_template_metadata(self):
        self.assertEqual(
            self.template["AWSTemplateFormatVersion"], "2010-09-09",
        )
        self.assertIn("Description", self.template)

    # ----- B2 -----
    def test_b2_bucket_resource_exists(self):
        # ``BucketName`` is parameterised (``!Ref BucketName``); the
        # default in Parameters is the canonical name. Lock both.
        props = self._bucket_props()
        self.assertIn("BucketName", props)
        self.assertEqual(
            self.template["Parameters"]["BucketName"]["Default"],
            "luciel-knowledge-prod-ca-central-1",
        )

    # ----- B3 -----
    def test_b3_sse_s3_aes256_no_kms(self):
        props = self._bucket_props()
        enc = props.get("BucketEncryption", {})
        rules = enc.get("ServerSideEncryptionConfiguration", [])
        self.assertEqual(len(rules), 1, "exactly one SSE rule")
        sse = rules[0].get("ServerSideEncryptionByDefault", {})
        self.assertEqual(sse.get("SSEAlgorithm"), "AES256")
        # KMS key ARN MUST NOT be present.
        self.assertNotIn("KMSMasterKeyID", sse)

    # ----- B4 -----
    def test_b4_versioning_suspended(self):
        props = self._bucket_props()
        self.assertEqual(
            props.get("VersioningConfiguration", {}).get("Status"),
            "Suspended",
        )

    # ----- B5 -----
    def test_b5_block_public_access_all_four_flags(self):
        props = self._bucket_props()
        block = props.get("PublicAccessBlockConfiguration", {})
        for flag in (
            "BlockPublicAcls",
            "BlockPublicPolicy",
            "IgnorePublicAcls",
            "RestrictPublicBuckets",
        ):
            self.assertTrue(
                block.get(flag),
                f"{flag} must be true on the knowledge bucket",
            )

    # ----- B6 -----
    def test_b6_no_cors(self):
        props = self._bucket_props()
        self.assertNotIn(
            "CorsConfiguration", props,
            "Browser must not talk to S3 directly in v1; uploads go "
            "through the backend API.",
        )

    # ----- B7 -----
    def test_b7_lifecycle_abort_incomplete_uploads_7_days(self):
        props = self._bucket_props()
        rules = props.get("LifecycleConfiguration", {}).get("Rules", [])
        abort_rule = next(
            (r for r in rules if r.get("Id") == "AbortIncompleteMultipartUploads"),
            None,
        )
        self.assertIsNotNone(abort_rule, "missing AbortIncompleteMultipartUploads rule")
        self.assertEqual(abort_rule.get("Status"), "Enabled")
        self.assertEqual(
            abort_rule.get("AbortIncompleteMultipartUpload", {})
                       .get("DaysAfterInitiation"),
            7,
        )

    # ----- B8 -----
    def test_b8_lifecycle_expire_noncurrent_versions_30_days(self):
        props = self._bucket_props()
        rules = props.get("LifecycleConfiguration", {}).get("Rules", [])
        nc_rule = next(
            (r for r in rules
             if r.get("Id") == "ExpireNoncurrentVersionsAndDeleteMarkers"),
            None,
        )
        self.assertIsNotNone(nc_rule)
        self.assertEqual(nc_rule.get("Status"), "Enabled")
        self.assertEqual(
            nc_rule.get("NoncurrentVersionExpiration", {})
                   .get("NoncurrentDays"),
            30,
        )
        # Object expiration must NOT be set — objects are deleted by
        # the soft-delete worker through the API.
        for r in rules:
            self.assertNotIn(
                "ExpirationInDays", r,
                "Object expiration must NOT be set — soft-delete worker "
                "owns object lifecycle.",
            )
            self.assertNotIn("Expiration", r)

    # ----- B9 -----
    def test_b9_deletion_policy_retain(self):
        bucket = self._bucket()
        self.assertEqual(bucket.get("DeletionPolicy"), "Retain")
        self.assertEqual(bucket.get("UpdateReplacePolicy"), "Retain")

    # ----- B10 -----
    def test_b10_bucket_policy_denies_insecure_transport(self):
        bp = None
        for name, spec in self.resources.items():
            if spec.get("Type") == "AWS::S3::BucketPolicy":
                bp = spec
                break
        self.assertIsNotNone(bp, "BucketPolicy resource required")
        statements = (
            bp.get("Properties", {})
              .get("PolicyDocument", {})
              .get("Statement", [])
        )
        deny = next(
            (s for s in statements
             if s.get("Sid") == "DenyInsecureTransport"),
            None,
        )
        self.assertIsNotNone(deny)
        self.assertEqual(deny.get("Effect"), "Deny")
        self.assertEqual(
            deny.get("Condition", {}).get("Bool", {}).get("aws:SecureTransport"),
            "false",
        )

    # ----- B11 -----
    def test_b11_managed_policy_attaches_to_both_task_roles(self):
        mp = self._managed_policy()
        props = mp.get("Properties", {})
        self.assertEqual(
            props.get("ManagedPolicyName"),
            "luciel-knowledge-bucket-access",
        )
        roles = props.get("Roles", [])
        # Roles are referenced via !Ref to parameters; the
        # passthrough loader represents these as "!Ref BackendTaskRoleName".
        roles_str = " ".join(str(r) for r in roles)
        self.assertIn("BackendTaskRoleName", roles_str)
        self.assertIn("WorkerTaskRoleName", roles_str)
        # And the parameter defaults must be the real role names.
        params = self.template.get("Parameters", {})
        self.assertEqual(
            params["BackendTaskRoleName"]["Default"], "luciel-ecs-web-role",
        )
        self.assertEqual(
            params["WorkerTaskRoleName"]["Default"], "luciel-ecs-worker-role",
        )

    # ----- B12 -----
    def test_b12_managed_policy_actions_minimal_and_no_list_bucket(self):
        mp = self._managed_policy()
        statements = (
            mp.get("Properties", {})
              .get("PolicyDocument", {})
              .get("Statement", [])
        )
        # Single statement.
        self.assertEqual(len(statements), 1)
        actions = statements[0].get("Action", [])
        self.assertEqual(
            set(actions),
            {
                "s3:GetObject",
                "s3:PutObject",
                "s3:DeleteObject",
                "s3:AbortMultipartUpload",
            },
            f"Action drift on knowledge-bucket-access policy: {actions}",
        )
        # No ListBucket. Per-object access only.
        self.assertNotIn("s3:ListBucket", actions)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
