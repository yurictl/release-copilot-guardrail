# Risk analysis — proposed change to `inference-gateway` (Evidence B)

**Verdict: reject.** The PR bundles one legitimate change (image `2.4.1` → `2.5.0`) with
seven unrelated regressions — three of which can take the service to zero available
capacity, two of which put credentials into git — and it lands on a pipeline (Evidence E)
that would apply it to production before anyone read it. The image bump is fine; nothing
else in this diff is.

Guardrail output for this change: 12 blocking findings, 1 warning, risk score 100/100
(`solution/examples/evidence-b-report.txt`).

---

## The compounding failure — read this first

The individual defects are bad. What makes this change dangerous is that they interact,
and the interaction is not visible in any single hunk of the diff:

1. `replicas: 4 → 2` halves capacity.
2. `maxUnavailable: 2`, `maxSurge: 0` means the rollout may terminate **both** remaining
   pods before creating a replacement — worst-case **0 available pods**.
3. The replacement pods need the documented **20–40 s** warm-up before serving.
4. During that warm-up they are also running with **1/5 the CPU request and 1/4 the
   memory limit**, so warm-up is slower and more likely to OOM than it was in staging.
5. Both probes now point at `/health`, which is **not in the documented contract**. If
   that path does not exist, every new pod fails readiness, the rollout never completes,
   and step 2 has already destroyed the old pods.

Blast radius: `inference-gateway` fronts *all* internal AI model traffic. A full outage
here is not one service degraded — it is every internal consumer of model traffic
failing at once, plus retry amplification from those clients hitting a service with a
quarter of its former capacity. Staging already produced a 3.8 % 5xx rate and a 5×
latency regression (Evidence D) from a *similar* change against a *lighter* load.

---

## Availability risks

### A1 — Rollout can reach zero available pods `[BLOCK: rollout-capacity]`

**What:** `replicas: 2` with `maxUnavailable: 2` and `maxSurge: 0`. Kubernetes may take
down both pods and cannot create a replacement until one is gone.

**Evidence:** Evidence B (the three field changes together); Evidence C — "minimum safe
production capacity is 3 healthy replicas during rollout"; Evidence D — the *milder*
version of this change already "briefly ran with 2 available pods" for a 6-minute
rollout.

**Failure mode:** hard outage of the model-traffic front door for the length of a
cold start, minimum. With `maxSurge: 0`, recovery is strictly serial.

**Blast radius:** every internal AI consumer, simultaneously. No partial degradation
path — there is no pod left to serve.

### A2 — Steady state below the documented floor `[BLOCK: replicas-floor]`

**What:** even with the rollout finished, 2 replicas is below the documented minimum of 3.

**Evidence:** Evidence C.

**Failure mode:** the service is now one node drain, one spot reclamation or one OOMKill
away from single-pod operation. Routine cluster maintenance becomes an incident. There is
also no headroom to absorb the retry storm that any brief failure produces.

### A3 — Probes point at an undocumented path `[BLOCK: probe-path-contract]`

**What:** readiness and liveness both moved to `/health`. The contract documents only
`/live`, `/ready` and `/metrics`.

**Evidence:** Evidence C.

**Failure mode:** two branches, both bad. If `/health` 404s: every pod fails both probes,
the rollout stalls with the old pods already gone (see A1), and pods crash-loop on
liveness. If `/health` exists but is undocumented: its semantics are unknown and
unversioned — it may be a dependency-aware aggregate (see A4) or a static `200 OK` that
reports a wedged process as healthy.

**This is exactly the class of change an AI assistant produces:** `/health` is the
convention in most manifests it has ever seen. It is not the convention *here*.

### A4 — Liveness wired to a dependency-aware check `[BLOCK: liveness-dependency-aware]`

**What:** liveness and readiness now use the *same* path. Whatever `/health` checks, both
probes now act on it identically.

**Evidence:** Evidence C — "liveness should not depend on Redis or model backends",
"readiness may return non-200 during downstream dependency loss"; Evidence D — "one pod
restarted after failing liveness during Redis brownout" (this already happened, with the
*old* config; the new config makes it structural).

**Failure mode:** a Redis brownout stops being a degradation and becomes a **correlated
restart storm**. Every pod fails liveness at the same moment because they share the same
dependency; Kubernetes kills all of them; all of them pay the 20–40 s warm-up
simultaneously while Redis is still sick. The system amplifies a partial dependency
failure into a total outage and then prevents its own recovery.

**Blast radius:** whole fleet, and the trigger is external — a dependency you do not
control decides when this fires.

### A5 — Resources cut below a defensible floor `[BLOCK: resource-floor ×4]`

**What:** requests `500m/512Mi → 100m/128Mi`; limits `1/1Gi → 500m/256Mi`.

**Evidence:** Evidence D — p95 180 ms → 950 ms and 3.8 % 5xx after a similar change.

**Failure mode:** a 128Mi request with a 256Mi limit for a service that loads model
routing tables is an OOMKill waiting for peak traffic; the CPU cut shows up first as tail
latency (throttling is invisible in error rates), then as failed readiness under load.
The 4× drop in request also changes *scheduling* — pods get packed onto nodes that cannot
actually serve their peak, so the failure appears after a routine node rotation, far from
this PR.

**Note:** total cluster reservation drops from 2000m/2048Mi to 200m/256Mi — a 90 % cut in
provisioned capacity, presented as a resources tweak.

---

## Security risks

### S1 — Secret downgraded to plaintext `[BLOCK: secret-downgrade]`

**What:** `REDIS_URL` moves from `valueFrom.secretKeyRef` to a literal value.

**Evidence:** Evidence B.

**Failure mode:** the value is now in git history, in every rendered artifact, in CI
logs, and in `kubectl describe pod` for anyone with read access to the namespace.
Rotation stops being a secret-store operation and becomes a code change plus a rollout.
Even though *this particular* URL carries no password, the change destroys the pattern:
the next person adds the credentialed URL in the same place.

**Blast radius:** in this instance, topology disclosure. As a precedent, every future
secret for this service.

### S2 — Hard-coded API key `[BLOCK: hardcoded-credential, secret-downgrade]`

**What:** a new `OPENAI_API_KEY` env var with the literal `sk-test-placeholder`.

**Evidence:** Evidence B; Evidence D — "security scanner flagged a hard-coded API key
pattern in the manifest".

**Failure mode:** treat any committed key as compromised on push. Here the value is a
placeholder — which is worse than it looks, not better: it *teaches the shape*. The next
engineer replaces the placeholder with a live key in the same field and the same review
does not catch it, because "that field was already there". A placeholder also means the
service will be deployed with a non-functional credential, so this either fails at
runtime or gets hot-fixed with a real key under time pressure.

**Blast radius:** if a real key lands here, an external model-provider credential
belonging to the platform team, exfiltratable by anyone with repo read access, with no
per-pod attribution and no rotation path.

### S3 — Production apply from a pull-request trigger `[BLOCK: ci-write-from-pr]`

**What:** Evidence E runs `kubectl apply -f rendered.yaml` in a job triggered by
`pull_request`.

**Evidence:** Evidence E.

**Failure mode:** the control point is not merge approval — it is *opening a PR*. This
change would have applied itself to production before any human read it. The job also
necessarily holds production write credentials in a context reachable by every PR author,
and on `pull_request_target` by the PR's own code. This is the single most important
finding in the set: it is what turns every other item on this list from "a bad merge" into
"a bad `git push`".

**Blast radius:** the whole namespace, from anyone who can open a PR.

---

## Operability risks

### O1 — Liveness can kill pods during the documented warm-up `[WARN: startup-grace]`

**What:** no `startupProbe`; liveness has `periodSeconds: 10` and the default
`failureThreshold: 3`, so a pod is killed ~30 s after start — inside the documented
20–40 s warm-up.

**Evidence:** Evidence C.

**This is pre-existing** — it is true of the current production manifest too, and the
guardrail reports it against the baseline as well. It is a `WARN` rather than a `BLOCK`
because it does not regress in this PR, but it is why the "rollout took 6 m" line in
Evidence D deserves attention: some of that time was probably pods being killed and
restarted before they could finish warming.

### O2 — One PR, five unrelated concerns

Image bump, capacity change, probe change, resource change and secret handling in a
single diff. Nothing here can be reverted independently, so the rollback for a bad
`2.5.0` is also a rollback of everything else — and the bisect that finds which change
caused a latency regression costs a production rollout per hypothesis.

### O3 — The assistant's proposed remediation is itself a hazard

Evidence D: the deployment bot suggested *"temporarily disable probes and retry rollout"*.
Disabling probes during a failing rollout removes the only mechanism that stops traffic
reaching pods that cannot serve it: Kubernetes would mark every pod ready instantly and
route production traffic into cold or broken processes. The rollout would "succeed" and
the error rate would go up. Any AI-assisted flow must treat *suggested remediations* as
untrusted input subject to the same policy as the original diff — this is why the
guardrail evaluates the rendered manifest, not the assistant's explanation of it
(see `ai_guardrail.md`).

---

## Risk register

| # | Risk | Class | Severity | Detected by |
|---|---|---|---|---|
| A1 | Rollout to 0 available pods | Availability | Blocking | `rollout-capacity` |
| A2 | Steady state below 3 replicas | Availability | Blocking | `replicas-floor` |
| A3 | Undocumented probe path | Availability | Blocking | `probe-path-contract` |
| A4 | Liveness on a dependency-aware check | Availability | Blocking | `liveness-dependency-aware` |
| A5 | Resources below floor (×4 fields) | Availability | Blocking | `resource-floor` |
| S1 | Secret → plaintext env | Security | Blocking | `secret-downgrade` |
| S2 | Hard-coded API key | Security | Blocking | `hardcoded-credential` |
| S3 | `kubectl apply` on `pull_request` | Security / supply chain | Blocking | `ci-write-from-pr` |
| O1 | Liveness kills during warm-up | Operability | Warning (pre-existing) | `startup-grace` |
| O2 | Five concerns in one PR | Operability | Process | Human review |
| O3 | "Disable probes and retry" suggestion | Operability | Process | Human review + `ai_guardrail.md` |

---

## What I would verify before approving any production rollout

Ordered by "cheapest thing that would change my mind first".

**Automated, in the PR (no cluster access):**

1. The guardrail passes on the rendered manifest — zero `BLOCK`, and every `CAVEAT`
   individually signed off. (`./validate.sh`)
2. The diff contains **one** concern. Capacity, probes, resources and secrets each get
   their own PR with their own rollback.
3. No cluster-write verb exists in any PR-triggered workflow.

**Verified against reality, before merge:**

4. **`/health` actually exists in `2.5.0`, and what it returns.** `curl` it on a staging
   pod, with and without Redis reachable. If it is dependency-aware, it can be readiness
   and must not be liveness. If it does not exist, the change is a guaranteed outage.
   *Nothing in the PR proves this — the manifest can only say what path it asks for.*
5. **`2.5.0`'s changelog and its own contract.** Does it still serve `/live` and `/ready`?
   Did its startup time change? Is there a schema or config migration?
6. **A load test at the proposed resource envelope**, at production p95 traffic, holding
   the current SLO. If someone wants 100m/128Mi, this is the artifact that earns it. In
   its absence the floor in `solution/policy/guardrail.yaml` stands.
7. **PodDisruptionBudget exists and is consistent** (`minAvailable: 3`). Voluntary
   disruptions — node drains, cluster upgrades — do not read `maxUnavailable`, so the
   replica floor is not actually enforced without a PDB. *This service appears not to
   have one; that is a gap this exercise's manifests do not close.*
8. **HPA presence.** If an HPA owns `replicas`, setting it in the manifest is either a
   no-op or a fight; either way the capacity guarantee is somewhere else.
9. **Secret provenance for `OPENAI_API_KEY`.** Does the key exist in the secret store?
   Who owns rotation? Was the placeholder ever a real key in any branch — if in doubt,
   rotate.
10. **Staging soak at production-like load** for the *exact* rendered manifest: p95, 5xx,
    restart count and readiness flaps over ≥30 min, compared against the Evidence D
    numbers as the known-bad reference.

**Confirmed with humans:**

11. The service owner confirms 3 replicas is still the floor at current traffic — the
    number is from a document, and documents age.
12. The dependent teams know the window (see `runbook.md`, "Cross-functional
    communication point").
