# release-copilot guardrail — `inference-gateway`

An approval-gated guardrail around AI-assisted Kubernetes deployment for the
`inference-gateway` service. The premise in one sentence: **an assistant may read and
suggest, a deterministic checker decides what is blocking, and a human presses the
button** — and none of those three can substitute for another.

Start here:

```bash
./validate.sh          # tests + the four demonstrations, ~1s, no cluster needed
```

---

## What is here

| File | What it is |
|---|---|
| [`risk_analysis.md`](risk_analysis.md) | The 11 risks in the proposed change, classified, with the evidence for each and what I would verify before approving |
| [`ai_guardrail.md`](ai_guardrail.md) | The safe AI-review design: read scope, hard prohibitions, output format, approval path, and the three threat models |
| [`runbook.md`](runbook.md) | Rollout flow, pre-merge / pre-deploy validation, rollout checks, rollback triggers and steps, the required cross-functional gate |
| [`solution/scripts/validate_manifest.py`](solution/scripts/validate_manifest.py) | The guardrail — 9 checks, fails closed, redacts what it finds |
| [`solution/policy/guardrail.yaml`](solution/policy/guardrail.yaml) | Every threshold, each carrying the evidence that produced it |
| [`.github/workflows/`](.github/workflows) | The pipeline split, **installed and running here**: `pr-validate.yml` (no credentials) and `production-apply.yml` (dispatch + protected environment). Rationale in [`solution/workflow/README.md`](solution/workflow/README.md) |
| [`k8s/`](k8s) | `base/` + `overlays/production/` — a real kustomize overlay, so the render step in CI is genuine |
| [`solution/schema/change_summary.schema.json`](solution/schema/change_summary.schema.json) | The machine-readable approver summary |
| [`solution/exceptions/`](solution/exceptions/README.md) | The time-boxed exception mechanism |
| [`solution/examples/`](solution/examples) | Real output from the commands below, checked in |
| [`tests/`](tests) | 39 tests: catch tests, precision tests, fail-closed tests, render-parity |
| [`AI_USAGE.md`](AI_USAGE.md) | What the AI produced, what I changed, what I rejected, how each claim was verified |

---

## Completed

**Must complete**

1. **Risk analysis** — `risk_analysis.md`. Eight blocking defects (five availability,
   three security/supply-chain), the compounding failure they produce together, one
   pre-existing operability warning and two process risks. Each with evidence, failure
   mode and blast radius, plus a 12-item pre-approval verification list.
2. **Guardrail** — `solution/`. Nine executable checks (the brief asked for four):

   | Rule | Severity | Catches |
   |---|---|---|
   | `replicas-floor` | BLOCK | steady-state capacity below 3 |
   | `rollout-capacity` | BLOCK | `replicas - maxUnavailable < 3` — the "0 available pods" case |
   | `probe-path-contract` | BLOCK | probe paths not in the documented contract |
   | `liveness-dependency-aware` | BLOCK | liveness pointing at a readiness/dependency-aware path |
   | `resource-floor` | BLOCK | requests/limits below the defensible floor (4 fields) |
   | `secret-downgrade` | BLOCK | `secretKeyRef` replaced by a literal (diff-aware, with an absolute fallback) |
   | `hardcoded-credential` | BLOCK | credential-shaped literals anywhere in the render |
   | `ci-write-from-pr` | BLOCK | `kubectl apply` (and friends) in a PR-triggered workflow |
   | `startup-grace` | WARN | liveness able to kill a pod inside the documented warm-up |

3. **AI review design** — `ai_guardrail.md`.
4. **Rollout control** — `runbook.md`.

**Should attempt**

5. **Tests / example failures** — `./validate.sh`, `tests/test_validate.py` (39 tests),
   `solution/examples/*`.
6. **Pipeline split** — `.github/workflows/pr-validate.yml` +
   `production-apply.yml`, replacing the Evidence E workflow. Installed and running in
   this repository, not merely proposed — see `solution/workflow/README.md`.

**Stretch**

7. **Structured change summary** — `--format json`, described by
   `solution/schema/change_summary.schema.json`, with risk score and band, capacity
   delta, security findings, derived required approvals and a recommended action.
   `human_decision_required` is a schema `const: true` — the format cannot express
   "approved".
8. **Exception mechanism** — `solution/exceptions/`: one rule, one workload, ≤24 h,
   two approvers with required roles, no self-approval, credential rules non-waivable,
   expiry evaluated at validation time so a forgotten waiver stops working by itself.

---

## How to run it

Requirements: Python 3.10+ and PyYAML (`pip install -r solution/scripts/requirements.txt`).
No cluster, no cloud account, no policy-engine binary.

```bash
# everything: unit tests + the four demonstrations
./validate.sh

# the bad change (Evidence B) -> 12 BLOCK, 1 WARN, exit 1
python3 solution/scripts/validate_manifest.py \
  --manifest tests/fixtures/manifests/proposed-evidence-b.yaml \
  --baseline tests/fixtures/manifests/baseline-evidence-a.yaml

# the existing CI workflow (Evidence E) -> ci-write-from-pr, exit 1
python3 solution/scripts/validate_manifest.py --workflows tests/fixtures/workflows

# the remediated change -> no findings, exit 0
python3 solution/scripts/validate_manifest.py \
  --manifest tests/fixtures/manifests/remediated.yaml \
  --baseline tests/fixtures/manifests/baseline-evidence-a.yaml

# the approver's summary
python3 solution/scripts/validate_manifest.py \
  --manifest tests/fixtures/manifests/proposed-evidence-b.yaml \
  --baseline tests/fixtures/manifests/baseline-evidence-a.yaml --format json | less

# unit tests only
python3 -m unittest discover -s tests -v
```

Exit codes: `0` clean · `1` blocking findings · `2` **could not evaluate — fails closed**.
Unparseable YAML, a missing baseline, or a render that no longer contains the target
Deployment all produce `2`. An undeterminable state is never reported as a pass.

### What a reviewer should look at, in order

1. `./validate.sh` output — the guardrail catching the Evidence B change.
2. `risk_analysis.md` §"The compounding failure" — why the change is worse than the sum
   of its hunks.
3. `solution/policy/guardrail.yaml` — every threshold with its evidence.
4. `ai_guardrail.md` §"Threat: prompt injection" — the capability argument.
5. `tests/test_validate.py` class `Precision` — the half that keeps this from being a
   rubber stamp that flags everything.

---

## Design decisions worth defending

**Python, not Rego/Kyverno/Conftest.** The exercise says a reviewer must be able to inspect
this quickly and that AI output must be verified. Neither `conftest` nor `opa` nor
`kyverno` was installed in the environment I built this in, so a Rego policy would have
shipped unexecuted — reviewed-looking code that had never produced a verdict. A stdlib +
PyYAML script runs everywhere and every check is exercised by a test. The rules are
declarative in `guardrail.yaml`; porting them to Rego is mechanical once the CI image can
actually run it, and `ci-write-from-pr` would stay a script either way (it checks workflow
files, not admission objects). Longer term this belongs in **both** places: CI for fast
feedback, and a Kyverno/Gatekeeper admission policy so the cluster refuses the change
regardless of how it arrives — CI checks the path you know about.

**Precision is tested, not assumed.** Five of the 39 tests assert that the *current*
production manifest, the *remediated* change and the repo's own workflows produce no
blocking findings. A guardrail measured only on what it catches scores perfectly by
flagging everything, and then gets bypassed within a month.

**Fail closed, everywhere.** Exit 2 on anything it cannot evaluate; `maxUnavailable`
absent is treated as the Kubernetes default of 1 rather than 0; an exception that does not
hold is reported rather than ignored.

**The report redacts what it finds.** A scanner that prints the key it found has copied it
into CI logs, PR comments and artifact storage. There is a test for that.

**`CAVEAT` is a real state.** A finding covered by a valid exception is downgraded, never
hidden: it keeps its text, gains the exception ID and approver names, and adds the incident
commander to `required_approvals`. Exceptions surface risk to humans; they do not delete it.

---

## Assumptions

1. `kustomize build k8s/overlays/production` yields the manifest in
   `tests/fixtures/manifests/baseline-evidence-a.yaml`. That overlay is not included here — the guardrail
   takes a rendered file, so the render step is orthogonal to it.
2. `main` is the deployed state, because `production-apply.yml` only applies from `main`.
   That is what makes `origin/main` a valid baseline for diff-aware checks. In a repo with
   real drift, the baseline should come from `kubectl get -o yaml` via a read-only token
   instead — a one-flag change.
3. The Evidence C contract is current. It is the source of the probe paths, the 3-replica
   floor and the 20–40 s warm-up, and each is a policy value someone must re-confirm as
   traffic grows.
4. The resource floor is the *current* production allocation. I have no load test, so I
   treated today's values as the defensible minimum and made "go lower" require evidence.
5. GitHub Actions with GitHub Environments, `CODEOWNERS` and OIDC to the cloud provider.
   The controls the design leans on — required reviewers, branch protection — are
   configured in GitHub, deliberately not in a file that a PR can edit.
6. One workload, one namespace. The policy is per-service by design; a platform-wide
   version needs a selector model, which is the wrong shape to guess at now.
7. The service has no PodDisruptionBudget and no HPA. Both would change the capacity
   analysis; §"What I would verify" in `risk_analysis.md` calls this out as a gap.

---

## Verification status — what I actually ran

| Artifact | Status |
|---|---|
| `validate_manifest.py` | Executed against 6 manifests and 2 workflow directories; 37 tests pass |
| Exception logic | Executed for valid / expired / self-approved / under-approved / non-waivable |
| Fail-closed paths | Executed — exit 2 confirmed for malformed YAML, missing workload, missing baseline, missing workflow dir |
| Change summary | Generated and checked field-by-field against the JSON schema by test |
| `solution/workflow/*.yml` | **Not executed** — no CI environment. YAML parses; the guardrail commands inside them were run locally with the same arguments; the `kustomize`/`kubectl`/OIDC steps are unverified |
| Cluster behaviour | **Not verified** — no cluster, per the exercise |

---

## Time spent

**≈25 minutes wall clock**, in one AI-assisted session — inside the 45-minute budget,
including the stretch items. That number is the honest one and it is also the least
interesting one: writing this is fast, and *verifying* it is not. Reading every check,
confirming the Kubernetes semantics behind them, and deciding the thresholds are the right
thresholds took the larger share of that time and would take a reviewer longer than it took
to produce. `AI_USAGE.md` records where the assistant was fast, where it was wrong, and
what was done about it.

---

## What I would improve with more time

1. **Kyverno/Gatekeeper admission policy** mirroring these rules, so the cluster enforces
   them regardless of how a change arrives. CI only guards the path you know about, and
   this is the single biggest gap in the current design.
2. **PodDisruptionBudget as a checked resource.** `maxUnavailable` says nothing about node
   drains; without `minAvailable: 3` the replica floor is not really enforced. The
   guardrail should require the PDB to exist and agree with the policy.
3. **HPA awareness.** If an HPA owns `replicas`, `replicas-floor` is checking a field that
   does not decide capacity. The check should read `minReplicas` instead.
4. **A golden-set replay for the assistant's own output**, the way `tests/` calibrates the
   deterministic checks: fixtures of past AI review comments, scored for precision and
   for the "confident but wrong for this service" failure that produced the staging
   incident. An assistant nobody measures becomes an assistant everyone scrolls past.
5. **Progressive delivery.** Argo Rollouts or a canary with automated analysis on p95 and
   5xx would make the rollback triggers in `runbook.md` automatic rather than
   human-watched, and would shrink the blast radius of a bad `2.5.0` from the fleet to
   one canary pod.
6. **Signed exception records** (commit signature or an approval recorded via the GitHub
   API rather than a name in a YAML file), so approver identity is authenticated and not
   merely asserted.
7. **Drift detection** — the baseline assumption in §Assumptions holds only while nobody
   applies out-of-band. A scheduled `kubectl diff` against `main` would prove it, or
   catch it early.
8. **Policy versioning and a deprecation path**, so a threshold change is a reviewable
   event with its evidence attached, and rules that never fire in a year get retired
   rather than accumulating.
