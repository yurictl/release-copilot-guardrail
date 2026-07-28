# AI usage

**Tool:** Claude Code (Opus 5), one session, working from the exercise brief and the five
evidence blocks. Project scaffolding conventions came from a personal template repo
(`claude-3plus1-harness`) — specifically the three-band severity model (`BLOCK` /
`WARN` / `CAVEAT`), the fail-closed gate, and the practice of pairing every rule with both
a catch fixture and a precision fixture. The template's harness machinery itself is not
included here; only the ideas that earn their place in a 45-minute deliverable.

Everything below is what actually happened, including the parts where the assistant was
wrong. The exercise asks for AI use to be documented and verified, so a report claiming a
clean run would be the least useful possible answer.

---

## What I accepted

| Accepted | Why it survived review |
|---|---|
| The overall shape: policy YAML + a checker + a two-workflow split | Matches the brief's "one focused, runnable guardrail" and keeps thresholds out of code |
| The nine rules and their severities | Each traces to a specific line of Evidence C/D/E — I checked each mapping against the evidence text rather than accepting "this is a Kubernetes best practice" |
| The arithmetic in `rollout-capacity` | Verified by hand for the Evidence B numbers (2 − 2 = 0) and by test for percentage forms |
| The `CAVEAT` state for exceptions | Downgrading rather than hiding is the behaviour I wanted; the alternative (suppressing the finding) was never on the table |
| The risk-analysis structure (evidence / failure mode / blast radius per item) | Forces every claim to name what it rests on |

## What I changed

1. **Rejected the "grep the output" merge gate.** The first CI draft enforced the result
   with `grep -q '^\[BLOCK\]' report.txt`. That silently passes when the validator crashes
   or when the report path is wrong — a fail-open gate. Replaced with explicit exit-code
   capture (`manifest_rc` / `workflow_rc`) and an `Enforce` step that treats a missing
   code as failure.
2. **Made "cannot evaluate" its own exit code.** The initial design had 0/1 only. A
   guardrail that returns 0 for a manifest it failed to parse is worse than no guardrail,
   because it produces a green check. Added exit 2 and four tests that pin it.
3. **Fixed the credential leak in the guardrail's own output.** The first version printed
   the matched literal in the finding text. A scanner that echoes the secret it found has
   copied it into CI logs, the PR comment and the artifact store. Added redaction and
   `test_report_does_not_echo_the_credential`.
4. **Corrected `maxUnavailable`/`maxSurge` defaults and rounding.** The draft treated an
   absent value as 0 (which passes manifests that can lose a pod) and rounded percentages
   the same way in both directions. Kubernetes defaults both to 1, rounds `maxUnavailable`
   down and `maxSurge` up. Checked against the Kubernetes rolling-update semantics and
   pinned in `test_percentage_rollout_values_follow_kubernetes_rounding`.
5. **Handled `on:` parsing as boolean `True`.** The workflow check found nothing at first:
   PyYAML reads the bare key `on:` as YAML 1.1 boolean `True`, so `wf.get("on")` missed
   every workflow. Caught by running the check against the Evidence E fixture and getting
   a suspiciously clean result — which is the reason the "does it catch the known-bad
   input" test exists before the "does it pass good input" one.
6. **Kept `startup-grace` as a WARN and documented it as pre-existing.** The check fires
   against the *current* production manifest too. The tempting fix was to tune the rule
   until the baseline came out clean; instead the finding is reported, the test asserts it
   (`test_current_production_manifest_keeps_the_known_startup_gap`), and
   `risk_analysis.md` §O1 says plainly that this one is not the PR's fault. A rule
   weakened to make today's state pass is a rule that will never catch anything.
7. **Added the precision half of the test suite.** The assistant's first test file was all
   catch tests. That measures recall only, and a policy that flags everything scores 100 %
   on it. Added the current manifest, the remediated manifest and the proposed workflows
   as must-not-flag cases.
8. **Rewrote the risk analysis to lead with the interaction.** The draft was a list of
   eight independent defects. The actual danger is that they compound — replica cut ×
   surge 0 × cold start × reduced CPU × a probe path that may 404. That paragraph is the
   point of the document.
9. **Cut the assistant's hedging.** Draft prose said things like "consider possibly
   reducing risk by evaluating whether replicas should perhaps remain at 4". Production
   documents state the decision and the reason.

## What I rejected

- **Shipping a Rego/Conftest policy alongside the Python.** `conftest`, `opa` and
  `kyverno` are not installed in the environment this was built in, so the Rego would have
  shipped never having produced a verdict — the exact "AI output that looks reviewed"
  failure the exercise warns about. It is named as the migration path in the README
  instead, with the reason.
- **A `jsonschema` dependency** to validate the change summary. Not worth a dependency for
  one artifact; the test walks the schema's `required` and `enum` declarations directly,
  which also keeps the emitter and the schema from drifting.
- **An "AI auto-fix" mode** that rewrites the manifest and opens a follow-up PR. Directly
  contradicts the constraint against autonomous production writes, and the Evidence D
  "temporarily disable probes" suggestion is a working demonstration of why. Suggestions
  are diffs a human applies.
- **Letting the assistant's review comment be a required status check.** It would make a
  probabilistic system load-bearing for a safety property and create pressure to loosen it
  the first time it false-positives at 03:00.
- **A blanket "no plaintext env vars" rule.** `MODEL_PROVIDER=internal` is a legitimate
  plaintext value; a rule that flags it teaches people to ignore the tool. The policy
  targets a named list plus sensitive name patterns plus credential-shaped values.
- **A `--skip-checks` / `--force` flag.** Every operator eventually types it. The exception
  mechanism exists so the escape hatch is narrow, named, expiring and auditable instead.
- **Inventing metrics.** Draft prose contained specific SLO numbers ("99.95 % availability
  target") that appear nowhere in the evidence. Every quantitative claim in the final
  documents is either from Evidence A–E or explicitly labelled as an assumption.

---

## How I verified it

- **Executed, not eyeballed.** Every check was run against a fixture that must fail it and
  a fixture that must pass it: `./validate.sh` and 37 tests, all passing. The Evidence B
  numbers in `risk_analysis.md` (12 blocking findings, 0 available pods, −1800m CPU) are
  copied from actual output in `solution/examples/`, not written from expectation.
- **Fail-closed paths tested explicitly** — malformed YAML, a render missing the workload,
  a nonexistent baseline, a nonexistent workflow directory. All exit 2.
- **The exception mechanism tested against its own abuse cases** — expired, self-approved,
  under-approved, and an attempt to waive a credential finding.
- **Kubernetes semantics checked against documented behaviour** rather than recalled:
  rolling-update rounding, probe defaults (`failureThreshold: 3`, `periodSeconds: 10`),
  and the fact that `maxUnavailable` does not govern voluntary disruptions — which is why
  the missing PodDisruptionBudget is flagged as a gap in `risk_analysis.md` rather than
  assumed handled.
- **Every evidence citation traced back to Evidence A–E.** Where a conclusion needs
  something the evidence does not contain — whether `/health` exists in image `2.5.0` — it
  is listed as a thing to verify, not asserted.

### What is *not* verified, stated plainly

- **The GitHub Actions workflows have never run.** No CI environment was available. Their
  YAML parses and the guardrail invocations inside them were executed locally with the
  same arguments, but the `kustomize` download, the OIDC role assumption, the
  `kubectl diff`/`apply` steps and the rollback branch are unexercised. Treat them as a
  reviewed design, not as tested code.
- **No cluster behaviour was verified** — by the exercise's own terms, but it bears
  repeating: the guardrail reasons about manifests. It cannot tell you that `/health`
  exists, that 3 replicas is still the right floor at today's traffic, or that `2.5.0`
  starts. Those are in the human verification list for a reason.
- **The claim that these rules would have caught the staging incident** rests on Evidence D
  describing a "similar change". I verified the rules catch the change in Evidence B, which
  is the artifact I was given.
