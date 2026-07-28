# Runbook — production rollout control for `inference-gateway`

Scope: any change to `k8s/**` affecting the `production` namespace. Assumes the pipeline
split in `solution/workflow/` is installed as `.github/workflows/`.

---

## The change in one line

**Before:** opening a PR applied to production (Evidence E).
**After:** merging is gated by a deterministic guardrail; applying is a separate,
human-dispatched job against a protected environment, with short-lived credentials scoped
to one namespace.

---

## Rollout flow

```
author -> PR -> [pr-validate: render + guardrail + plan]   no cluster credentials
                        |  required status check
                        v
              human review (CODEOWNERS: service owner; + security for S-findings)
                        |
                        v
                     merge to main                          still nothing applied
                        |
                        v
        [production-apply: workflow_dispatch, environment=production]
             required reviewers -> re-render -> re-run guardrail -> kubectl diff
                        |
                        v
                 apply -> watch rollout -> health gate -> record
                        |  fail
                        v
                 automatic rollback + page
```

Two properties worth being explicit about:

- **Rendering happens twice and the guardrail runs twice.** The PR verdict is evidence
  that the change was reviewable; it is not a permit. `main` may have moved between merge
  and apply.
- **The apply job is the only holder of write credentials**, obtained via OIDC for the
  duration of the run, scoped to the `production` namespace. No long-lived kubeconfig
  exists in the repository, so there is nothing for a PR to steal.

---

## Pre-merge validation (automated, no cluster access)

Runs in `pr-validate.yml`. All of it also runs locally via `./validate.sh`.

| # | Check | Fails the PR when |
|---|---|---|
| 1 | `kustomize build` succeeds | the overlay does not render |
| 2 | Guardrail on the rendered manifest, against the `main` baseline | any `BLOCK`; also on exit 2 — cannot evaluate never means pass |
| 3 | Guardrail on `.github/workflows/` | a cluster-write verb appears in a PR-triggered job |
| 4 | `kubectl diff` (read-only token, same-repo PRs only) | never fails the PR — it is context for the reviewer |
| 5 | Change summary artifact published | — |

### Pre-merge, human

6. **One concern per PR.** Image bumps, capacity changes, probe changes, resource changes
   and secret handling ship separately. If this PR is Evidence B, send it back and split
   it — that alone removes most of the review load.
7. **Contract claims verified against the image, not the manifest.** Any probe path
   change requires evidence the endpoint exists in the target image and behaves as the
   probe assumes (`curl` on a staging pod, with the dependency up and down).
8. **Resource reductions require a load test** at production p95, attached to the PR.
   Without it the floor in `solution/policy/guardrail.yaml` stands.
9. **Every `CAVEAT` individually signed off** in a review comment naming the person, not
   just an approving review.
10. **Staging soak for anything touching probes, capacity or resources**: ≥30 min at
    production-like load; p95, 5xx, restart count and readiness flaps compared against the
    Evidence D numbers as the known-bad reference.

---

## Pre-deploy validation (in `production-apply.yml`, before the apply step)

11. The dispatched SHA is an ancestor of `main` — no applying a branch.
12. Re-render and re-run the guardrail, including exception records (expiry is evaluated
    *now*, so an exception that lapsed since the PR blocks here).
13. `kubectl diff` against the live cluster, printed in the log. This is the last place a
    human sees what will actually change — including drift applied out-of-band.
14. Confirm the current state is healthy *before* touching it: `readyReplicas >= 3`, error
    rate at baseline. Never start a rollout into an ongoing incident unless the rollout
    *is* the mitigation.
15. Confirm no change freeze is active and the communication point below has happened.

---

## Rollout checks

Watch these for the duration of the rollout and for **15 minutes after** it reports
success — the failure modes in Evidence D (warm-up, dependency brownout) do not appear in
`rollout status`.

| Signal | Where | Healthy |
|---|---|---|
| Rollout progress | `kubectl -n production rollout status deploy/inference-gateway --timeout=10m` | completes; no `ProgressDeadlineExceeded` |
| Available replicas | `kubectl -n production get deploy inference-gateway -w` | **never below 3** at any sample |
| Pod restarts | `kubectl -n production get pods -l app=inference-gateway` | `RESTARTS` unchanged; no `CrashLoopBackOff` |
| Readiness flapping | pod events / readiness gauge | pods become ready once and stay ready |
| p95 latency | service dashboard | within 20 % of the pre-rollout baseline (pre-change: 180 ms) |
| 5xx rate | service dashboard | below the SLO error budget burn rate; **any sustained rise above 1 % is a rollback trigger** |
| Redis / model-backend errors | dependency dashboard | flat — a rise here plus pod restarts is the A4 restart-storm signature |

The apply job asserts `readyReplicas >= 3` from the cluster after the rollout: the
availability floor is verified against reality, not assumed from the manifest.

---

## Rollback

### Triggers — any one of these, no discussion required

- Available replicas drop below 3 at any point.
- 5xx rate above 1 % for 2 consecutive minutes, or any spike above 3 %.
- p95 latency above 2× the pre-rollout baseline for 5 minutes.
- Any pod restarting more than once, or entering `CrashLoopBackOff`.
- `rollout status` has not completed after 10 minutes.
- Readiness flapping (a pod ready → not ready → ready) on more than one pod.
- A dependent team reports errors that correlate with the rollout window.

Rolling back is cheap; a debate during a partial outage is not. Diagnose from the
artifacts afterwards.

### Steps

```bash
# 1. Roll back to the previous ReplicaSet (the apply job does this automatically on failure)
kubectl -n production rollout undo deploy/inference-gateway

# 2. Watch it land — this is a rollout too, and the same floor applies
kubectl -n production rollout status deploy/inference-gateway --timeout=10m
kubectl -n production get deploy inference-gateway -w   # availableReplicas must stay >= 3

# 3. Confirm recovery on the signals, not on the rollout status
#    p95 back to baseline, 5xx back to baseline, restarts stable for 10 min

# 4. If undo does not recover (e.g. the previous ReplicaSet is also unhealthy):
kubectl -n production scale deploy/inference-gateway --replicas=6   # buy headroom first
#    then re-apply the last known-good rendered manifest from that run's artifacts:
kubectl apply -f rendered.yaml
```

**Constraints, learned from Evidence D:**

- Do **not** "disable the probes and retry" — the suggestion in the staging incident.
  Removing readiness makes Kubernetes route production traffic into cold or broken pods
  and turns a stalled rollout into a silent one.
- Do **not** delete pods to force a reset. With `maxSurge: 0` this is how you reach zero.
- Do **not** roll forward with a fix under time pressure unless the rollback itself failed.
  Roll back, then fix in daylight.

**Secret-related rollback is different.** If the incident involved a committed credential,
rolling back the manifest does not un-leak it. Rotate the credential first, deploy the
manifest that reads the rotated secret, then clean history. Treat the key as compromised
from the moment of push.

### After any rollback

- Keep the artifacts: rendered manifest, change summary, `kubectl diff`, the dashboards
  for the window.
- File the incident, and check whether the guardrail *could* have caught it. If yes, it
  is a bug in the policy — add the rule and a fixture. If no, that is the interesting
  case: the rule that gets written from it is the one a generic policy pack would never
  have contained.

---

## Cross-functional communication point

**Required, before any change classified `high` or `critical` by the change summary, or
any change touching capacity, probes or secrets: a go/no-go in the shared
`#inference-platform-changes` channel, at least 2 hours before the apply window, with the
on-call of every dependent team acknowledging.**

The message is the change summary plus four lines:

> **What:** `inference-gateway` 2.4.1 → 2.5.0 (image only).
> **Window:** today 14:00–14:30 UTC. Expected: no capacity reduction, surge-only rollout.
> **Blast radius if it goes wrong:** all internal model traffic; worst case ~40 s of
> degraded capacity per pod replacement.
> **Rollback:** `rollout undo`, ~3 min; triggers are 5xx > 1 % or available < 3.
> **On-call for this change:** @name. Ack from: @model-serving, @agents-platform, @search.

Why this specific gate rather than a broader "notify stakeholders": `inference-gateway`
is a shared front door. Its consumers are the only people who can tell you that this
window overlaps their batch job, that their client has no retry budget, or that they are
mid-incident themselves. They also detect the failure before the service dashboard does —
their error rate moves first. An acknowledged window converts "the gateway is broken" into
"the gateway is rolling out, here is the rollback ETA", which is the difference between one
incident and four teams independently paging.

Two smaller communication rules that fall out of this:

- **Any use of the exception mechanism is announced in the same channel when it is
  approved**, not at the post-incident review. A waiver nobody saw is a waiver nobody
  can object to.
- **A rollback is announced in the channel as it starts**, not after it completes.

---

## Changing the guardrail itself

`solution/policy/guardrail.yaml`, `solution/scripts/`, and the workflows are
`CODEOWNERS`-protected: a change needs the service owner *and* someone from the SRE rota.
Loosening a threshold and shipping the change it unblocks in the same PR is the pattern to
refuse — land the policy change on its own, with the evidence (a load test, a contract
update) that justifies the new number.
