#!/usr/bin/env python3
"""Pre-production guardrail for the inference-gateway Kubernetes manifests.

Reads a rendered manifest (and, optionally, the currently-deployed baseline) and
reports findings against solution/policy/guardrail.yaml. Designed to run in a
pull-request job with **no cluster credentials**: every check is computed from the
text being graded, never from a live API.

Severities
    BLOCK   must not reach production in this state; exit code 1
    WARN    should be fixed; does not block on its own
    CAVEAT  a BLOCK covered by a valid, time-boxed exception record — surfaced for
            human sign-off, never silently dropped

Exit codes
    0  no blocking findings
    1  at least one blocking finding
    2  the guardrail could not evaluate the input (fail closed — never "pass")

Usage
    validate_manifest.py --manifest k8s/rendered.yaml \
                         [--baseline baseline.yaml] \
                         [--policy solution/policy/guardrail.yaml] \
                         [--workflows .github/workflows] \
                         [--exceptions solution/exceptions] \
                         [--format text|json] [--out summary.json] [--now ISO8601]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - environment problem, not a policy result
    sys.stderr.write(
        "guardrail: PyYAML is required (pip install pyyaml). "
        "Refusing to pass a manifest it cannot parse.\n"
    )
    sys.exit(2)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_POLICY = os.path.join(REPO_ROOT, "solution", "policy", "guardrail.yaml")

BLOCK, WARN, CAVEAT = "BLOCK", "WARN", "CAVEAT"

# Categories used by the change summary and by the approval routing in runbook.md.
AVAILABILITY, SECURITY, OPERABILITY, SUPPLY_CHAIN = (
    "availability",
    "security",
    "operability",
    "supply-chain",
)


class GuardrailError(Exception):
    """Raised when the guardrail cannot evaluate its input. Always fails closed."""


@dataclass
class Finding:
    rule: str
    severity: str
    category: str
    title: str
    detail: str
    evidence: str
    remediation: str
    location: str = ""
    exception_id: str | None = None
    original_severity: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, "")}


@dataclass
class Capacity:
    replicas: int | None = None
    max_unavailable: int | None = None
    max_surge: int | None = None
    worst_case_available: int | None = None
    cpu_request_total_millicores: int | None = None
    memory_request_total_mib: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Result:
    findings: list[Finding] = field(default_factory=list)
    baseline_capacity: Capacity | None = None
    proposed_capacity: Capacity | None = None
    images: dict[str, str] = field(default_factory=dict)
    applied_exceptions: list[dict[str, Any]] = field(default_factory=list)
    rejected_exceptions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == BLOCK]


# --------------------------------------------------------------------------- #
# parsing helpers
# --------------------------------------------------------------------------- #

_CPU_RE = re.compile(r"^(\d+(?:\.\d+)?)(m)?$")
_MEM_RE = re.compile(r"^(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|K|M|G|T|k)?$")
_PERCENT_RE = re.compile(r"^(\d+)%$")


def parse_cpu(value: Any) -> float:
    """Kubernetes CPU quantity -> cores. '500m' -> 0.5, '1' -> 1.0."""
    m = _CPU_RE.match(str(value).strip())
    if not m:
        raise GuardrailError(f"unparseable CPU quantity: {value!r}")
    n = float(m.group(1))
    return n / 1000 if m.group(2) else n


def parse_memory(value: Any) -> int:
    """Kubernetes memory quantity -> bytes. '512Mi' -> 536870912."""
    m = _MEM_RE.match(str(value).strip())
    if not m:
        raise GuardrailError(f"unparseable memory quantity: {value!r}")
    n = float(m.group(1))
    factor = {
        None: 1,
        "Ki": 1024,
        "Mi": 1024**2,
        "Gi": 1024**3,
        "Ti": 1024**4,
        "k": 1000,
        "K": 1000,
        "M": 1000**2,
        "G": 1000**3,
        "T": 1000**4,
    }[m.group(2)]
    return int(n * factor)


def resolve_rollout_value(value: Any, replicas: int, round_up: bool) -> int:
    """Resolve maxUnavailable/maxSurge, which may be an int or a percentage.

    Kubernetes rounds maxUnavailable down and maxSurge up.
    """
    if value is None:
        return 1  # Kubernetes default for both fields
    s = str(value).strip()
    pm = _PERCENT_RE.match(s)
    if pm:
        raw = replicas * int(pm.group(1)) / 100
        return int(raw + 0.999999) if round_up else int(raw)
    try:
        return int(s)
    except ValueError as exc:
        raise GuardrailError(f"unparseable rollout value: {value!r}") from exc


def load_yaml_documents(path: str) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        raise GuardrailError(f"file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            docs = list(yaml.safe_load_all(fh))
    except yaml.YAMLError as exc:
        raise GuardrailError(f"{path}: invalid YAML ({exc.__class__.__name__})") from exc
    return [d for d in docs if isinstance(d, dict)]


def load_policy(path: str) -> dict[str, Any]:
    docs = load_yaml_documents(path)
    if not docs:
        raise GuardrailError(f"{path}: policy file is empty")
    return docs[0]


def select_workload(docs: list[dict[str, Any]], policy: dict[str, Any]) -> dict[str, Any] | None:
    want = policy["workload"]
    for doc in docs:
        meta = doc.get("metadata") or {}
        if (
            doc.get("kind") == want["kind"]
            and meta.get("name") == want["name"]
            and meta.get("namespace", "default") == want["namespace"]
        ):
            return doc
    return None


def containers_of(workload: dict[str, Any]) -> list[dict[str, Any]]:
    pod = ((workload.get("spec") or {}).get("template") or {}).get("spec") or {}
    return [c for c in (pod.get("containers") or []) if isinstance(c, dict)]


def target_containers(workload: dict[str, Any], policy: dict[str, Any]) -> list[dict[str, Any]]:
    """The container named in policy, or all of them if the name is absent."""
    wanted = policy["workload"].get("container")
    all_containers = containers_of(workload)
    named = [c for c in all_containers if c.get("name") == wanted]
    return named or all_containers


def env_map(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for entry in container.get("env") or []:
        if isinstance(entry, dict) and entry.get("name"):
            out[entry["name"]] = entry
    return out


def probe_path(container: dict[str, Any], probe: str) -> str | None:
    p = container.get(probe) or {}
    return ((p.get("httpGet") or {}).get("path")) if isinstance(p, dict) else None


def capacity_of(workload: dict[str, Any], policy: dict[str, Any]) -> Capacity:
    spec = workload.get("spec") or {}
    replicas = spec.get("replicas", 1)
    rolling = ((spec.get("strategy") or {}).get("rollingUpdate")) or {}
    mu = resolve_rollout_value(rolling.get("maxUnavailable"), replicas, round_up=False)
    ms = resolve_rollout_value(rolling.get("maxSurge"), replicas, round_up=True)
    cpu_total = 0.0
    mem_total = 0
    for c in target_containers(workload, policy):
        req = ((c.get("resources") or {}).get("requests")) or {}
        if req.get("cpu") is not None:
            cpu_total += parse_cpu(req["cpu"])
        if req.get("memory") is not None:
            mem_total += parse_memory(req["memory"])
    return Capacity(
        replicas=replicas,
        max_unavailable=mu,
        max_surge=ms,
        worst_case_available=max(0, replicas - mu),
        cpu_request_total_millicores=int(round(cpu_total * replicas * 1000)),
        memory_request_total_mib=int(mem_total * replicas / (1024**2)),
    )


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #


def check_replicas_floor(workload, policy, result) -> None:
    floor = policy["availability"]["min_healthy_replicas"]
    replicas = (workload.get("spec") or {}).get("replicas", 1)
    if replicas < floor:
        result.findings.append(
            Finding(
                rule="replicas-floor",
                severity=BLOCK,
                category=AVAILABILITY,
                title=f"replicas ({replicas}) is below the documented safe floor ({floor})",
                detail=(
                    f"Steady-state capacity would be {replicas} pods. The service is "
                    f"documented to need {floor} healthy replicas, so a single node "
                    "drain, eviction or crash takes the service below safe capacity "
                    "with no rollout involved at all."
                ),
                evidence="Evidence C: 'minimum safe production capacity is 3 healthy replicas during rollout'",
                remediation=f"Keep spec.replicas >= {floor} (current production runs 4).",
                location="spec.replicas",
            )
        )


def check_rollout_capacity(workload, policy, result) -> None:
    if not policy["availability"].get("enforce_rollout_capacity", True):
        return
    floor = policy["availability"]["min_healthy_replicas"]
    cap = capacity_of(workload, policy)
    if cap.worst_case_available is not None and cap.worst_case_available < floor:
        result.findings.append(
            Finding(
                rule="rollout-capacity",
                severity=BLOCK,
                category=AVAILABILITY,
                title=(
                    f"rollout can drop to {cap.worst_case_available} available pod(s); "
                    f"floor is {floor}"
                ),
                detail=(
                    f"replicas={cap.replicas}, maxUnavailable={cap.max_unavailable}, "
                    f"maxSurge={cap.max_surge}. Worst case during the rollout is "
                    f"{cap.replicas} - {cap.max_unavailable} = {cap.worst_case_available} "
                    "available pods before any new pod becomes ready"
                    + (
                        ", and with maxSurge=0 no replacement pod can even be created "
                        "until an old one is torn down"
                        if cap.max_surge == 0
                        else ""
                    )
                    + ". With a documented 20-40s warm-up, that window is minutes, not seconds."
                ),
                evidence="Evidence D: 'rollout took 6m and briefly ran with 2 available pods', p95 180ms -> 950ms, 5xx 3.8%",
                remediation=(
                    f"Require spec.replicas - maxUnavailable >= {floor}. Prefer a surge-only "
                    "rollout for this service: maxUnavailable: 0, maxSurge: 2."
                ),
                location="spec.strategy.rollingUpdate",
            )
        )


def check_probe_contract(workload, policy, result) -> None:
    want_ready = policy["probes"]["readiness_path"]
    want_live = policy["probes"]["liveness_path"]
    for c in target_containers(workload, policy):
        name = c.get("name", "<unnamed>")
        ready = probe_path(c, "readinessProbe")
        live = probe_path(c, "livenessProbe")
        for probe, actual, want in (
            ("readinessProbe", ready, want_ready),
            ("livenessProbe", live, want_live),
        ):
            if actual is None:
                result.findings.append(
                    Finding(
                        rule="probe-path-contract",
                        severity=BLOCK,
                        category=AVAILABILITY,
                        title=f"{probe} has no httpGet path on container '{name}'",
                        detail=(
                            "A workload without this probe cannot be gated safely during "
                            "rollout; Kubernetes would treat the pod as ready as soon as "
                            "the process starts."
                        ),
                        evidence="Evidence C: documented endpoints GET /live, GET /ready",
                        remediation=f"Set {probe}.httpGet.path to {want}.",
                        location=f"container[{name}].{probe}",
                    )
                )
            elif actual != want:
                result.findings.append(
                    Finding(
                        rule="probe-path-contract",
                        severity=BLOCK,
                        category=AVAILABILITY,
                        title=f"{probe} path '{actual}' is not the documented '{want}'",
                        detail=(
                            f"The application contract documents only /live, /ready and "
                            f"/metrics. '{actual}' is undocumented: it may 404 (every pod "
                            "fails its probe and the rollout stalls or crash-loops) or it "
                            "may exist with different semantics than this probe needs."
                        ),
                        evidence="Evidence C: documented endpoints GET /live, GET /ready, GET /metrics",
                        remediation=f"Set {probe}.httpGet.path back to {want}.",
                        location=f"container[{name}].{probe}.httpGet.path",
                    )
                )


def check_liveness_not_dependency_aware(workload, policy, result) -> None:
    dep_paths = set(policy["probes"].get("dependency_aware_paths") or [])
    want_live = policy["probes"]["liveness_path"]
    for c in target_containers(workload, policy):
        name = c.get("name", "<unnamed>")
        ready = probe_path(c, "readinessProbe")
        live = probe_path(c, "livenessProbe")
        if live is None or live == want_live:
            continue
        if live == ready or live in dep_paths:
            shared = " (identical to the readiness path)" if live == ready else ""
            result.findings.append(
                Finding(
                    rule="liveness-dependency-aware",
                    severity=BLOCK,
                    category=AVAILABILITY,
                    title=f"livenessProbe points at a dependency-aware path '{live}'{shared}",
                    detail=(
                        "Liveness answers 'is this process wedged'; readiness answers 'can "
                        "this pod serve traffic right now'. Wiring liveness to a "
                        "dependency-aware endpoint converts a downstream brownout into a "
                        "fleet-wide restart storm: every pod fails liveness at once, every "
                        "pod is killed, and the cold-start penalty is paid on all of them "
                        "simultaneously while the dependency is still degraded."
                    ),
                    evidence=(
                        "Evidence C: 'liveness should not depend on Redis or model backends'; "
                        "Evidence D: 'one pod restarted after failing liveness during Redis brownout'"
                    ),
                    remediation=f"Point livenessProbe at {want_live} and leave readiness on the dependency-aware path.",
                    location=f"container[{name}].livenessProbe.httpGet.path",
                )
            )


def check_startup_grace(workload, policy, result) -> None:
    warmup = policy["probes"].get("startup_warmup_seconds")
    if not warmup:
        return
    for c in target_containers(workload, policy):
        name = c.get("name", "<unnamed>")
        live = c.get("livenessProbe") or {}
        if not live:
            continue
        if c.get("startupProbe"):
            continue
        initial = live.get("initialDelaySeconds", 0)
        period = live.get("periodSeconds", 10)
        threshold = live.get("failureThreshold", 3)
        budget = initial + period * threshold
        if budget < warmup:
            result.findings.append(
                Finding(
                    rule="startup-grace",
                    severity=WARN,
                    category=OPERABILITY,
                    title=(
                        f"liveness can kill container '{name}' after ~{budget}s, inside the "
                        f"documented {warmup}s warm-up window"
                    ),
                    detail=(
                        f"initialDelaySeconds={initial} + periodSeconds={period} x "
                        f"failureThreshold={threshold} = {budget}s. A pod that takes the "
                        f"documented {warmup}s to warm its routing tables is restarted "
                        "before it ever becomes ready, producing a crash-loop that looks "
                        "like an application bug. This is a pre-existing gap in the current "
                        "manifest, not something this PR introduced."
                    ),
                    evidence="Evidence C: 'startup can take 20-40 seconds while model routing tables warm up'",
                    remediation=(
                        "Add a startupProbe (e.g. periodSeconds: 5, failureThreshold: 18) so "
                        "liveness only starts counting once the process is up."
                    ),
                    location=f"container[{name}].livenessProbe",
                )
            )


def check_resource_floor(workload, policy, result) -> None:
    floor = policy["resources"]["floor"]
    checks = (
        ("requests", "cpu", parse_cpu, "cores"),
        ("requests", "memory", parse_memory, "bytes"),
        ("limits", "cpu", parse_cpu, "cores"),
        ("limits", "memory", parse_memory, "bytes"),
    )
    for c in target_containers(workload, policy):
        name = c.get("name", "<unnamed>")
        resources = c.get("resources") or {}
        for section, key, parse, _unit in checks:
            want_raw = (floor.get(section) or {}).get(key)
            if want_raw is None:
                continue
            got_raw = (resources.get(section) or {}).get(key)
            if got_raw is None:
                result.findings.append(
                    Finding(
                        rule="resource-floor",
                        severity=BLOCK,
                        category=AVAILABILITY,
                        title=f"resources.{section}.{key} is unset on container '{name}'",
                        detail=(
                            "An unset request means the scheduler can pack this pod anywhere; "
                            "an unset limit means it can starve its neighbours. Either way the "
                            "capacity floor is unenforceable."
                        ),
                        evidence="Evidence D: reduced resources coincided with p95 180ms -> 950ms and a 3.8% 5xx rate",
                        remediation=f"Set resources.{section}.{key} to at least {want_raw}.",
                        location=f"container[{name}].resources.{section}.{key}",
                    )
                )
                continue
            if parse(got_raw) < parse(want_raw):
                result.findings.append(
                    Finding(
                        rule="resource-floor",
                        severity=BLOCK,
                        category=AVAILABILITY,
                        title=(
                            f"resources.{section}.{key} '{got_raw}' is below the floor "
                            f"'{want_raw}' on container '{name}'"
                        ),
                        detail=(
                            f"Cutting {section}.{key} from {want_raw} to {got_raw} shrinks per-pod "
                            "headroom at the same time as the replica count drops. For a memory "
                            "limit this is an OOMKill risk under load; for CPU it is throttling "
                            "that shows up as tail latency, not as an error."
                        ),
                        evidence="Evidence D: p95 latency 180ms -> 950ms, 5xx rate 3.8% after a similar change in staging",
                        remediation=(
                            f"Keep resources.{section}.{key} >= {want_raw}, or attach a load test "
                            "and change the floor in solution/policy/guardrail.yaml deliberately."
                        ),
                        location=f"container[{name}].resources.{section}.{key}",
                    )
                )


def check_secret_downgrade(workload, baseline, policy, result) -> None:
    """Plaintext where a secret reference belongs.

    Diff-aware when a baseline is supplied (was secretKeyRef, now a literal), and
    absolute otherwise (a sensitive env name with a literal value).
    """
    must = set(policy["secrets"].get("must_come_from_secret") or [])
    name_patterns = [re.compile(p) for p in policy["secrets"].get("sensitive_name_patterns") or []]
    baseline_envs: dict[str, dict[str, dict[str, Any]]] = {}
    if baseline is not None:
        for c in target_containers(baseline, policy):
            baseline_envs[c.get("name", "")] = env_map(c)

    for c in target_containers(workload, policy):
        name = c.get("name", "<unnamed>")
        for env_name, entry in env_map(c).items():
            if "value" not in entry:
                continue  # valueFrom / envFrom — the compliant shape
            was_secret = "valueFrom" in (baseline_envs.get(name, {}).get(env_name) or {})
            sensitive = env_name in must or any(p.search(env_name) for p in name_patterns)
            if not (was_secret or sensitive):
                continue
            if was_secret:
                title = f"env {env_name} was a secretKeyRef and is now a plaintext value"
                detail = (
                    "The baseline sources this value from the "
                    f"'{(baseline_envs[name][env_name].get('valueFrom') or {}).get('secretKeyRef', {}).get('name', 'secret')}' "
                    "Secret. Inlining it moves the value into git history, into every "
                    "rendered artifact, into CI logs and into `kubectl describe` output for "
                    "anyone with pod-read access in the namespace. Rotation stops being a "
                    "secret-store operation and becomes a code change, and the old value "
                    "stays in git forever."
                )
            else:
                title = f"env {env_name} holds a literal value but must come from a secret"
                detail = (
                    f"'{env_name}' is classified as sensitive by policy. A literal value here "
                    "is readable by anyone with repo access or pod-read access in the namespace."
                )
            result.findings.append(
                Finding(
                    rule="secret-downgrade",
                    severity=BLOCK,
                    category=SECURITY,
                    title=title,
                    detail=detail,
                    evidence="Evidence B: REDIS_URL moved from secretKeyRef to a literal; Evidence D: 'security scanner flagged a hard-coded API key pattern'",
                    remediation=(
                        f"Restore valueFrom.secretKeyRef for {env_name}. If the value genuinely "
                        "changed, rotate it in the secret store and reference the new key."
                    ),
                    location=f"container[{name}].env[{env_name}]",
                )
            )


def check_hardcoded_credentials(raw_text: str, policy, result) -> None:
    """Scan the rendered text for credential-shaped literals.

    Runs over the whole document, not only env blocks: annotations, args and
    ConfigMap payloads are just as capable of carrying a key.
    """
    for spec in policy["secrets"].get("credential_value_patterns") or []:
        pattern = re.compile(spec["pattern"])
        for lineno, line in enumerate(raw_text.splitlines(), start=1):
            m = pattern.search(line)
            if not m:
                continue
            matched = m.group(0)
            redacted = matched[:6] + "***" if len(matched) > 9 else "***"
            result.findings.append(
                Finding(
                    rule="hardcoded-credential",
                    severity=BLOCK,
                    category=SECURITY,
                    title=f"credential-shaped literal ({spec['name']}) in the manifest",
                    detail=(
                        f"Line {lineno} contains a value matching the {spec['name']} pattern "
                        f"('{redacted}'). Treat it as compromised the moment it is pushed: it is "
                        "in git history, in CI logs, and in every artifact rendered from this "
                        "commit. A placeholder is not an exemption — placeholders get replaced "
                        "with real values by the next person in a hurry, and the shape of the "
                        "manifest is what teaches them where to put it."
                    ),
                    evidence="Evidence D: 'security scanner flagged a hard-coded API key pattern in the manifest'",
                    remediation=(
                        "Remove the literal, reference a Secret via valueFrom.secretKeyRef, and "
                        "rotate the credential if it was ever real."
                    ),
                    location=f"line {lineno}",
                )
            )


def check_ci_workflows(workflow_dir: str, policy, result) -> None:
    """No cluster-write verb may run in a job triggered by a pull request."""
    triggers = set(policy["ci"].get("forbid_write_verbs_on_triggers") or [])
    verb_patterns = [re.compile(p) for p in policy["ci"].get("write_verb_patterns") or []]
    if not os.path.isdir(workflow_dir):
        raise GuardrailError(f"workflow directory not found: {workflow_dir}")

    for entry in sorted(os.listdir(workflow_dir)):
        if not entry.endswith((".yml", ".yaml")):
            continue
        path = os.path.join(workflow_dir, entry)
        docs = load_yaml_documents(path)
        if not docs:
            continue
        wf = docs[0]
        # PyYAML parses the bare key `on:` as the boolean True (YAML 1.1).
        on = wf.get("on", wf.get(True, {}))
        if isinstance(on, str):
            wf_triggers = {on}
        elif isinstance(on, list):
            wf_triggers = set(on)
        elif isinstance(on, dict):
            wf_triggers = set(on.keys())
        else:
            wf_triggers = set()
        offending_triggers = sorted(wf_triggers & triggers)
        if not offending_triggers:
            continue
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        for lineno, line in enumerate(lines, start=1):
            if line.lstrip().startswith("#"):
                continue
            for pattern in verb_patterns:
                if pattern.search(line):
                    result.findings.append(
                        Finding(
                            rule="ci-write-from-pr",
                            severity=BLOCK,
                            category=SUPPLY_CHAIN,
                            title=(
                                f"{entry} runs a cluster-write command in a "
                                f"{'/'.join(offending_triggers)}-triggered workflow"
                            ),
                            detail=(
                                f"Line {lineno}: `{line.strip()}`. A pull-request job holds "
                                "production credentials, so merge review is not the control "
                                "point any more — opening a PR is. The change applies before "
                                "anyone approves it, and on `pull_request_target` the code "
                                "deciding what to apply comes from the PR branch itself."
                            ),
                            evidence="Evidence E: the `deploy` workflow runs `kubectl apply -f rendered.yaml` on `pull_request`",
                            remediation=(
                                "Split the pipeline: the PR job renders, validates and posts a "
                                "plan with no cluster credentials; production apply moves to a "
                                "separate workflow_dispatch/push-to-main job bound to a "
                                "protected GitHub Environment with required reviewers."
                            ),
                            location=f"{entry}:{lineno}",
                        )
                    )
                    break


# --------------------------------------------------------------------------- #
# exceptions
# --------------------------------------------------------------------------- #


def _parse_ts(value: Any, label: str) -> dt.datetime:
    try:
        ts = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise GuardrailError(f"exception: unparseable {label}: {value!r}") from exc
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)


def load_exceptions(directory: str, policy, now: dt.datetime, result: Result) -> list[dict[str, Any]]:
    """Load exception records and keep only the ones that are structurally valid.

    Every rejection is recorded on the result: an exception that does not hold is
    itself an audit event, not a silent no-op.
    """
    valid: list[dict[str, Any]] = []
    if not os.path.isdir(directory):
        return valid
    cfg = policy.get("exceptions") or {}
    if not cfg.get("enabled", False):
        return valid
    max_hours = cfg.get("max_duration_hours", 24)
    min_approvers = cfg.get("min_approvers", 2)
    required_roles = set(cfg.get("required_approver_roles") or [])
    non_waivable = set(cfg.get("non_waivable_rules") or [])

    for entry in sorted(os.listdir(directory)):
        if not entry.endswith((".yml", ".yaml")):
            continue
        path = os.path.join(directory, entry)
        docs = load_yaml_documents(path)
        if not docs:
            continue
        exc = docs[0]
        exc_id = exc.get("id", entry)

        def reject(reason: str) -> None:
            result.rejected_exceptions.append({"id": exc_id, "file": entry, "reason": reason})

        missing = [
            k
            for k in ("id", "rule", "workload", "incident", "requested_by", "approvers", "expires_at", "created_at")
            if not exc.get(k)
        ]
        if missing:
            reject(f"missing required field(s): {', '.join(missing)}")
            continue
        if exc["rule"] in non_waivable:
            reject(f"rule '{exc['rule']}' is non-waivable by policy")
            continue
        approvers = exc.get("approvers") or []
        if len(approvers) < min_approvers:
            reject(f"{len(approvers)} approver(s), policy requires {min_approvers}")
            continue
        roles = {a.get("role") for a in approvers if isinstance(a, dict)}
        if not required_roles.issubset(roles):
            reject(f"missing approver role(s): {', '.join(sorted(required_roles - roles))}")
            continue
        names = {a.get("name") for a in approvers if isinstance(a, dict)}
        if exc["requested_by"] in names:
            reject("self-approval: requester also appears as an approver")
            continue
        created = _parse_ts(exc["created_at"], "created_at")
        expires = _parse_ts(exc["expires_at"], "expires_at")
        if expires <= now:
            reject(f"expired at {expires.isoformat()}")
            continue
        if (expires - created) > dt.timedelta(hours=max_hours):
            reject(f"duration exceeds the {max_hours}h maximum")
            continue
        valid.append(exc)
    return valid


def apply_exceptions(result: Result, exceptions: list[dict[str, Any]], policy) -> None:
    wl = policy["workload"]
    workload_key = f"{wl['namespace']}/{wl['name']}"
    for exc in exceptions:
        if exc["workload"] != workload_key:
            result.rejected_exceptions.append(
                {"id": exc["id"], "reason": f"workload '{exc['workload']}' does not match {workload_key}"}
            )
            continue
        matched = 0
        for f in result.findings:
            if f.severity == BLOCK and f.rule == exc["rule"]:
                f.original_severity = BLOCK
                f.severity = CAVEAT
                f.exception_id = exc["id"]
                matched += 1
        result.applied_exceptions.append(
            {
                "id": exc["id"],
                "rule": exc["rule"],
                "incident": exc["incident"],
                "expires_at": str(exc["expires_at"]),
                "approvers": [f"{a.get('name')} ({a.get('role')})" for a in exc.get("approvers", [])],
                "findings_downgraded": matched,
            }
        )


# --------------------------------------------------------------------------- #
# evaluation + summary
# --------------------------------------------------------------------------- #


def evaluate(
    manifest_path: str | None,
    baseline_path: str | None,
    policy: dict[str, Any],
    workflow_dir: str | None = None,
    exceptions_dir: str | None = None,
    now: dt.datetime | None = None,
) -> Result:
    result = Result()
    now = now or dt.datetime.now(dt.timezone.utc)

    if manifest_path:
        docs = load_yaml_documents(manifest_path)
        with open(manifest_path, "r", encoding="utf-8") as fh:
            raw_text = fh.read()
        workload = select_workload(docs, policy)
        if workload is None:
            wl = policy["workload"]
            raise GuardrailError(
                f"{manifest_path}: no {wl['kind']} {wl['namespace']}/{wl['name']} found. "
                "The guardrail will not pass a manifest it cannot locate the workload in."
            )
        baseline = None
        if baseline_path:
            baseline = select_workload(load_yaml_documents(baseline_path), policy)
            if baseline is None:
                raise GuardrailError(f"{baseline_path}: baseline does not contain the target workload")
            result.baseline_capacity = capacity_of(baseline, policy)

        result.proposed_capacity = capacity_of(workload, policy)
        for c in target_containers(workload, policy):
            result.images[c.get("name", "<unnamed>")] = c.get("image", "<unset>")

        check_replicas_floor(workload, policy, result)
        check_rollout_capacity(workload, policy, result)
        check_probe_contract(workload, policy, result)
        check_liveness_not_dependency_aware(workload, policy, result)
        check_startup_grace(workload, policy, result)
        check_resource_floor(workload, policy, result)
        check_secret_downgrade(workload, baseline, policy, result)
        check_hardcoded_credentials(raw_text, policy, result)

    if workflow_dir:
        check_ci_workflows(workflow_dir, policy, result)

    if exceptions_dir:
        valid = load_exceptions(exceptions_dir, policy, now, result)
        apply_exceptions(result, valid, policy)

    order = {BLOCK: 0, CAVEAT: 1, WARN: 2}
    result.findings.sort(key=lambda f: (order.get(f.severity, 9), f.rule, f.location))
    return result


def risk_score(result: Result, policy) -> tuple[int, str]:
    s = policy.get("scoring") or {}
    score = 0
    for f in result.findings:
        score += {
            BLOCK: s.get("block_weight", 20),
            CAVEAT: s.get("caveat_weight", 10),
            WARN: s.get("warn_weight", 5),
        }.get(f.severity, 0)
    score = min(100, score)
    bands = s.get("bands") or {"low": 0, "medium": 20, "high": 40, "critical": 60}
    band = "low"
    for name in ("low", "medium", "high", "critical"):
        if name in bands and score >= bands[name]:
            band = name
    return score, band


def required_approvals(result: Result, policy) -> list[str]:
    cats = {f.category for f in result.findings if f.severity in (BLOCK, CAVEAT)}
    approvals: list[str] = []
    if cats & {AVAILABILITY, OPERABILITY}:
        approvals += ["service-owner", "sre-oncall"]
    if cats & {SECURITY, SUPPLY_CHAIN}:
        approvals.append("security-reviewer")
    if result.applied_exceptions:
        approvals.append("incident-commander")
    if not approvals:
        approvals = ["service-owner"]
    seen: set[str] = set()
    return [a for a in approvals if not (a in seen or seen.add(a))]


def recommended_action(result: Result) -> str:
    if result.blocking:
        return "reject"
    if any(f.severity == CAVEAT for f in result.findings):
        return "approve-with-time-boxed-exception"
    if result.findings:
        return "approve-with-conditions"
    return "approve"


def build_summary(result: Result, policy, manifest_path: str | None, generated_at: str) -> dict[str, Any]:
    score, band = risk_score(result, policy)
    wl = policy["workload"]
    counts = {
        "block": sum(1 for f in result.findings if f.severity == BLOCK),
        "caveat": sum(1 for f in result.findings if f.severity == CAVEAT),
        "warn": sum(1 for f in result.findings if f.severity == WARN),
    }
    before = result.baseline_capacity
    after = result.proposed_capacity
    capacity_change: dict[str, Any] = {}
    if after:
        capacity_change["after"] = after.as_dict()
        if before:
            capacity_change["before"] = before.as_dict()
            capacity_change["delta"] = {
                "replicas": (after.replicas or 0) - (before.replicas or 0),
                "worst_case_available_during_rollout": (after.worst_case_available or 0)
                - (before.worst_case_available or 0),
                "cpu_request_total_millicores": (after.cpu_request_total_millicores or 0)
                - (before.cpu_request_total_millicores or 0),
                "memory_request_total_mib": (after.memory_request_total_mib or 0)
                - (before.memory_request_total_mib or 0),
            }
    return {
        "schema_version": "1.0.0",
        "generated_at": generated_at,
        "generated_by": "solution/scripts/validate_manifest.py",
        "source": {"manifest": manifest_path, "policy_version": policy.get("version")},
        "workload": {
            "kind": wl["kind"],
            "name": wl["name"],
            "namespace": wl["namespace"],
            "images": result.images,
        },
        "risk": {"score": score, "band": band, "counts": counts},
        "capacity_change": capacity_change,
        "findings": [f.as_dict() for f in result.findings],
        "security_findings": [
            f.as_dict() for f in result.findings if f.category in (SECURITY, SUPPLY_CHAIN)
        ],
        "exceptions": {
            "applied": result.applied_exceptions,
            "rejected": result.rejected_exceptions,
        },
        "required_approvals": required_approvals(result, policy),
        "recommended_action": recommended_action(result),
        "human_decision_required": True,
    }


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

_ICON = {BLOCK: "[BLOCK] ", CAVEAT: "[CAVEAT]", WARN: "[WARN]  "}


def render_text(result: Result, policy, summary: dict[str, Any]) -> str:
    lines: list[str] = []
    wl = policy["workload"]
    lines.append(f"guardrail: {wl['kind']} {wl['namespace']}/{wl['name']}  (policy v{policy.get('version')})")
    lines.append("")
    if not result.findings:
        lines.append("No findings. All checks passed.")
    for f in result.findings:
        head = f"{_ICON.get(f.severity, f.severity)} {f.rule}: {f.title}"
        lines.append(head)
        if f.location:
            lines.append(f"          at: {f.location}")
        lines.append(f"          why: {f.detail}")
        lines.append(f"     evidence: {f.evidence}")
        lines.append(f"          fix: {f.remediation}")
        if f.exception_id:
            lines.append(f"    exception: {f.exception_id} (was {f.original_severity}) — needs human sign-off")
        lines.append("")
    for rej in result.rejected_exceptions:
        lines.append(f"[REJECTED EXCEPTION] {rej['id']}: {rej['reason']}")
    if result.rejected_exceptions:
        lines.append("")
    counts = summary["risk"]["counts"]
    lines.append(
        f"risk score {summary['risk']['score']}/100 ({summary['risk']['band']})  "
        f"blocking={counts['block']} caveat={counts['caveat']} warn={counts['warn']}"
    )
    cap = summary.get("capacity_change") or {}
    if cap.get("before") and cap.get("after"):
        b, a = cap["before"], cap["after"]
        lines.append(
            f"capacity: replicas {b['replicas']} -> {a['replicas']}, "
            f"worst-case available during rollout {b['worst_case_available']} -> {a['worst_case_available']}, "
            f"cpu requests {b['cpu_request_total_millicores']}m -> {a['cpu_request_total_millicores']}m"
        )
    lines.append(f"required approvals: {', '.join(summary['required_approvals'])}")
    lines.append(f"recommended action: {summary['recommended_action']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", help="rendered manifest to validate")
    ap.add_argument("--baseline", help="currently-deployed manifest, enables diff-aware checks")
    ap.add_argument("--policy", default=DEFAULT_POLICY)
    ap.add_argument("--workflows", help="directory of GitHub Actions workflows to check")
    ap.add_argument("--exceptions", help="directory of exception records")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    ap.add_argument("--out", help="write the JSON change summary to this path as well")
    ap.add_argument("--now", help="ISO-8601 override for exception expiry evaluation (testing)")
    args = ap.parse_args(argv)

    if not args.manifest and not args.workflows:
        ap.error("at least one of --manifest or --workflows is required")

    try:
        policy = load_policy(args.policy)
        now = _parse_ts(args.now, "--now") if args.now else dt.datetime.now(dt.timezone.utc)
        result = evaluate(
            manifest_path=args.manifest,
            baseline_path=args.baseline,
            policy=policy,
            workflow_dir=args.workflows,
            exceptions_dir=args.exceptions,
            now=now,
        )
        summary = build_summary(result, policy, args.manifest, generated_at=now.isoformat())
    except GuardrailError as exc:
        sys.stderr.write(f"guardrail: FAILED CLOSED — {exc}\n")
        return 2

    output = json.dumps(summary, indent=2) if args.format == "json" else render_text(result, policy, summary)
    print(output)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
            fh.write("\n")
    return 1 if result.blocking else 0


if __name__ == "__main__":
    sys.exit(main())
