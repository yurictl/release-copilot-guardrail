#!/usr/bin/env python3
"""Tests for the inference-gateway guardrail.

Run: python3 -m unittest discover -s tests -v      (or ./validate.sh)

Two halves, on purpose:
  * catch tests  — the bad change in Evidence B, and each rule in isolation
  * precision tests — the current production manifest and the remediated change
    must NOT produce blocking findings. A guardrail measured only on what it
    catches scores perfectly by flagging everything, and then gets switched off.
"""

import datetime as dt
import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "solution", "scripts", "validate_manifest.py")
POLICY = os.path.join(ROOT, "solution", "policy", "guardrail.yaml")
SCHEMA = os.path.join(ROOT, "solution", "schema", "change_summary.schema.json")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
BASELINE = os.path.join(ROOT, "k8s", "production", "deployment.yaml")
REMEDIATED = os.path.join(ROOT, "k8s", "production", "deployment.remediated.yaml")

_spec = importlib.util.spec_from_file_location("guardrail", SCRIPT)
guardrail = importlib.util.module_from_spec(_spec)
sys.modules["guardrail"] = guardrail  # dataclasses resolve annotations via sys.modules
_spec.loader.exec_module(guardrail)

NOW = dt.datetime(2026, 7, 28, 10, 0, 0, tzinfo=dt.timezone.utc)


def manifest(name: str) -> str:
    return os.path.join(FIXTURES, "manifests", name)


def run_cli(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, SCRIPT, "--policy", POLICY, *args],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def evaluate(manifest_path=None, baseline=None, workflows=None, exceptions=None, now=NOW):
    policy = guardrail.load_policy(POLICY)
    return guardrail.evaluate(
        manifest_path=manifest_path,
        baseline_path=baseline,
        policy=policy,
        workflow_dir=workflows,
        exceptions_dir=exceptions,
        now=now,
    ), policy


def rules(result, severity=None):
    return sorted(f.rule for f in result.findings if severity is None or f.severity == severity)


class EvidenceB(unittest.TestCase):
    """The change the release-copilot proposed must not reach production."""

    @classmethod
    def setUpClass(cls):
        cls.result, cls.policy = evaluate(manifest("proposed-evidence-b.yaml"), baseline=BASELINE)

    def test_every_documented_check_fires(self):
        self.assertEqual(
            set(rules(self.result, guardrail.BLOCK)),
            {
                "replicas-floor",
                "rollout-capacity",
                "probe-path-contract",
                "liveness-dependency-aware",
                "resource-floor",
                "secret-downgrade",
                "hardcoded-credential",
            },
        )

    def test_both_probe_paths_are_reported_separately(self):
        probe = [f for f in self.result.findings if f.rule == "probe-path-contract"]
        self.assertEqual(len(probe), 2, "readiness and liveness are two distinct defects")

    def test_all_four_resource_fields_are_reported(self):
        res = [f for f in self.result.findings if f.rule == "resource-floor"]
        self.assertEqual(len(res), 4, "requests.cpu/memory and limits.cpu/memory")

    def test_secret_downgrade_covers_both_env_vars(self):
        secrets = {f.location for f in self.result.findings if f.rule == "secret-downgrade"}
        self.assertEqual(
            secrets,
            {"container[gateway].env[REDIS_URL]", "container[gateway].env[OPENAI_API_KEY]"},
        )

    def test_capacity_delta_is_computed(self):
        self.assertEqual(self.result.baseline_capacity.worst_case_available, 3)
        self.assertEqual(self.result.proposed_capacity.worst_case_available, 0)
        self.assertEqual(self.result.proposed_capacity.replicas, 2)

    def test_startup_grace_is_a_warning_not_a_block(self):
        self.assertIn("startup-grace", rules(self.result, guardrail.WARN))

    def test_cli_exits_nonzero(self):
        rc, _, _ = run_cli("--manifest", manifest("proposed-evidence-b.yaml"), "--baseline", BASELINE)
        self.assertEqual(rc, 1)

    def test_report_does_not_echo_the_credential(self):
        """A guardrail that prints the secret it found has leaked it into CI logs."""
        rc, out, err = run_cli(
            "--manifest", manifest("proposed-evidence-b.yaml"), "--baseline", BASELINE, "--format", "json"
        )
        self.assertEqual(rc, 1)
        self.assertNotIn("sk-test-placeholder", out + err)
        self.assertIn("sk-tes***", out)


class Precision(unittest.TestCase):
    """Things that must NOT be flagged."""

    def test_current_production_manifest_has_no_blocking_findings(self):
        result, _ = evaluate(BASELINE)
        self.assertEqual(rules(result, guardrail.BLOCK), [])

    def test_current_production_manifest_keeps_the_known_startup_gap(self):
        # Documented in risk_analysis.md as pre-existing, not introduced by the PR.
        result, _ = evaluate(BASELINE)
        self.assertEqual(rules(result, guardrail.WARN), ["startup-grace"])

    def test_remediated_change_is_completely_clean(self):
        result, _ = evaluate(REMEDIATED, baseline=BASELINE)
        self.assertEqual(result.findings, [], "the safe version of the change must pass silently")

    def test_split_pipeline_workflows_pass(self):
        result, _ = evaluate(workflows=os.path.join(ROOT, "solution", "workflow"))
        self.assertEqual(result.findings, [])


class IndividualRules(unittest.TestCase):
    def test_secret_downgrade_without_a_baseline(self):
        """Diff-aware checks must degrade to an absolute form, not disappear."""
        result, _ = evaluate(manifest("proposed-evidence-b.yaml"))  # no baseline
        locations = {f.location for f in result.findings if f.rule == "secret-downgrade"}
        self.assertIn("container[gateway].env[OPENAI_API_KEY]", locations)

    def test_liveness_equal_to_readiness_is_its_own_finding(self):
        result, _ = evaluate(manifest("proposed-evidence-b.yaml"), baseline=BASELINE)
        finding = next(f for f in result.findings if f.rule == "liveness-dependency-aware")
        self.assertIn("identical to the readiness path", finding.title)

    def test_evidence_e_workflow_is_blocked(self):
        result, _ = evaluate(workflows=os.path.join(FIXTURES, "workflows"))
        self.assertEqual(rules(result, guardrail.BLOCK), ["ci-write-from-pr"])
        self.assertIn("evidence-e-deploy.yml:16", result.findings[0].location)

    def test_cpu_and_memory_quantities(self):
        self.assertEqual(guardrail.parse_cpu("500m"), 0.5)
        self.assertEqual(guardrail.parse_cpu("1"), 1.0)
        self.assertEqual(guardrail.parse_memory("512Mi"), 512 * 1024**2)
        self.assertEqual(guardrail.parse_memory("1Gi"), 1024**3)
        self.assertLess(guardrail.parse_memory("256Mi"), guardrail.parse_memory("1Gi"))

    def test_percentage_rollout_values_follow_kubernetes_rounding(self):
        # maxUnavailable rounds down, maxSurge rounds up.
        self.assertEqual(guardrail.resolve_rollout_value("25%", 4, round_up=False), 1)
        self.assertEqual(guardrail.resolve_rollout_value("25%", 3, round_up=False), 0)
        self.assertEqual(guardrail.resolve_rollout_value("25%", 3, round_up=True), 1)
        # Absent values take the Kubernetes default of 1, not 0 — assuming 0 here
        # would silently pass a manifest that can lose a pod.
        self.assertEqual(guardrail.resolve_rollout_value(None, 4, round_up=False), 1)


class FailClosed(unittest.TestCase):
    """The guardrail must never answer 'no findings' when it could not evaluate."""

    def test_missing_workload_exits_2(self):
        rc, _, err = run_cli("--manifest", manifest("workload-missing.yaml"))
        self.assertEqual(rc, 2)
        self.assertIn("FAILED CLOSED", err)

    def test_malformed_yaml_exits_2(self):
        rc, _, err = run_cli("--manifest", manifest("malformed.yaml"))
        self.assertEqual(rc, 2)
        self.assertIn("invalid YAML", err)

    def test_missing_baseline_file_exits_2(self):
        rc, _, err = run_cli(
            "--manifest", manifest("proposed-evidence-b.yaml"), "--baseline", "/nonexistent/baseline.yaml"
        )
        self.assertEqual(rc, 2)

    def test_missing_workflow_dir_exits_2(self):
        rc, _, err = run_cli("--workflows", "/nonexistent/workflows")
        self.assertEqual(rc, 2)

    def test_no_input_is_a_usage_error(self):
        rc, _, _ = run_cli()
        self.assertEqual(rc, 2)


class Exceptions(unittest.TestCase):
    HOTFIX = "hotfix-rollout-capacity.yaml"

    def _run(self, kind):
        return evaluate(
            manifest(self.HOTFIX),
            baseline=BASELINE,
            exceptions=os.path.join(FIXTURES, "exceptions", kind),
        )

    def test_the_hotfix_blocks_without_an_exception(self):
        result, _ = evaluate(manifest(self.HOTFIX), baseline=BASELINE)
        self.assertEqual(rules(result, guardrail.BLOCK), ["rollout-capacity"])

    def test_valid_exception_downgrades_to_caveat_and_stays_visible(self):
        result, _ = self._run("valid")
        self.assertEqual(result.blocking, [])
        caveats = [f for f in result.findings if f.severity == guardrail.CAVEAT]
        self.assertEqual(len(caveats), 1)
        self.assertEqual(caveats[0].exception_id, "EXC-2026-07-28-01")
        self.assertEqual(caveats[0].original_severity, guardrail.BLOCK)
        self.assertEqual(result.applied_exceptions[0]["incident"], "INC-4471")

    def test_expired_exception_does_not_waive(self):
        result, _ = self._run("expired")
        self.assertEqual(rules(result, guardrail.BLOCK), ["rollout-capacity"])
        self.assertIn("expired", result.rejected_exceptions[0]["reason"])

    def test_self_approval_is_rejected(self):
        result, _ = self._run("self-approved")
        self.assertEqual(rules(result, guardrail.BLOCK), ["rollout-capacity"])
        self.assertIn("self-approval", result.rejected_exceptions[0]["reason"])

    def test_insufficient_approvers_is_rejected(self):
        result, _ = self._run("under-approved")
        self.assertEqual(rules(result, guardrail.BLOCK), ["rollout-capacity"])
        self.assertIn("approver", result.rejected_exceptions[0]["reason"])

    def test_credential_findings_can_never_be_waived(self):
        result, _ = evaluate(
            manifest("proposed-evidence-b.yaml"),
            baseline=BASELINE,
            exceptions=os.path.join(FIXTURES, "exceptions", "non-waivable"),
        )
        self.assertIn("hardcoded-credential", rules(result, guardrail.BLOCK))
        self.assertIn("non-waivable", result.rejected_exceptions[0]["reason"])

    def test_exception_expiry_is_evaluated_at_validation_time(self):
        """The same record stops working when the clock moves past expires_at."""
        after_expiry = dt.datetime(2026, 7, 28, 14, 0, 0, tzinfo=dt.timezone.utc)
        result, _ = evaluate(
            manifest(self.HOTFIX),
            baseline=BASELINE,
            exceptions=os.path.join(FIXTURES, "exceptions", "valid"),
            now=after_expiry,
        )
        self.assertEqual(rules(result, guardrail.BLOCK), ["rollout-capacity"])


class ChangeSummary(unittest.TestCase):
    """The machine-readable summary must match its published schema."""

    @classmethod
    def setUpClass(cls):
        with open(SCHEMA, encoding="utf-8") as fh:
            cls.schema = json.load(fh)
        result, policy = evaluate(manifest("proposed-evidence-b.yaml"), baseline=BASELINE)
        cls.summary = guardrail.build_summary(
            result, policy, manifest("proposed-evidence-b.yaml"), generated_at=NOW.isoformat()
        )

    def test_required_top_level_keys_present(self):
        for key in self.schema["required"]:
            self.assertIn(key, self.summary)

    def test_no_undeclared_top_level_keys(self):
        # The schema sets additionalProperties: false; keep the emitter honest
        # without pulling in a jsonschema dependency.
        self.assertEqual(set(self.summary) - set(self.schema["properties"]), set())

    def test_finding_fields_match_the_declared_enums(self):
        defs = self.schema["$defs"]["finding"]["properties"]
        for f in self.summary["findings"]:
            self.assertIn(f["rule"], defs["rule"]["enum"])
            self.assertIn(f["severity"], defs["severity"]["enum"])
            self.assertIn(f["category"], defs["category"]["enum"])
            for required in self.schema["$defs"]["finding"]["required"]:
                self.assertIn(required, f)

    def test_verdict_fields(self):
        self.assertEqual(self.summary["recommended_action"], "reject")
        self.assertEqual(self.summary["risk"]["band"], "critical")
        self.assertTrue(self.summary["human_decision_required"])
        self.assertIn("security-reviewer", self.summary["required_approvals"])

    def test_security_findings_are_surfaced_separately(self):
        cats = {f["category"] for f in self.summary["security_findings"]}
        self.assertTrue(cats.issubset({"security", "supply-chain"}))
        self.assertTrue(self.summary["security_findings"])

    def test_capacity_change_reports_before_after_and_delta(self):
        cc = self.summary["capacity_change"]
        self.assertEqual(cc["before"]["replicas"], 4)
        self.assertEqual(cc["after"]["replicas"], 2)
        self.assertEqual(cc["delta"]["worst_case_available_during_rollout"], -3)
        self.assertEqual(cc["delta"]["cpu_request_total_millicores"], -1800)

    def test_clean_summary_recommends_approve_but_still_requires_a_human(self):
        result, policy = evaluate(REMEDIATED, baseline=BASELINE)
        summary = guardrail.build_summary(result, policy, REMEDIATED, generated_at=NOW.isoformat())
        self.assertEqual(summary["recommended_action"], "approve")
        self.assertTrue(summary["human_decision_required"])


class TextReport(unittest.TestCase):
    def test_text_output_names_rule_evidence_and_fix(self):
        result, policy = evaluate(manifest("proposed-evidence-b.yaml"), baseline=BASELINE)
        summary = guardrail.build_summary(result, policy, None, generated_at=NOW.isoformat())
        buf = io.StringIO()
        with redirect_stdout(buf):
            print(guardrail.render_text(result, policy, summary))
        text = buf.getvalue()
        for token in ("[BLOCK]", "evidence:", "fix:", "recommended action: reject"):
            self.assertIn(token, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
