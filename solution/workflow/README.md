# The pipeline split

The proposed workflows are not kept here as a copy to read — they are **installed and
running** in this repository:

| File | Trigger | Credentials | What it does |
|---|---|---|---|
| [`.github/workflows/pr-validate.yml`](../../.github/workflows/pr-validate.yml) | `pull_request` | none that can reach a cluster | renders the overlay, runs the guardrail against the rendered manifest *and* the workflows, publishes the change summary, comments on the PR, fails the required check |
| [`.github/workflows/production-apply.yml`](../../.github/workflows/production-apply.yml) | `workflow_dispatch` only | short-lived OIDC, `production` namespace | environment approval → ancestor check → re-render → re-run the guardrail → diff → apply → watch → health gate → rollback on failure |

A single copy on purpose: a proposal stored next to a diverging implementation is how a
pipeline ends up documented as safe and configured otherwise.

**Evidence that they work:** [PR #1](https://github.com/yurictl/release-copilot-guardrail/pull/1) carries the Evidence B change and is
blocked; [PR #2](https://github.com/yurictl/release-copilot-guardrail/pull/2) carries the same release as an image bump and passes. Both
runs are in [Actions](https://github.com/yurictl/release-copilot-guardrail/actions), with the rendered manifest and the JSON change summary
attached as artifacts.

## What Evidence E did, and what changed

```diff
- name: deploy
- on: pull_request          # opening a PR was the deploy trigger
- jobs.render-and-apply:
-   - kustomize build ... > rendered.yaml
-   - kubectl apply -f rendered.yaml       # production write, from a PR, unreviewed
+ pr-validate.yml       on: pull_request        no cluster credentials, verdict only
+ production-apply.yml  on: workflow_dispatch   protected environment, OIDC, re-validated
```

Four properties the split buys:

1. **The control point moves from "opening a PR" back to "a human approving."** In
   Evidence E the apply happened before review; nothing about the change was reviewable
   at the moment it took effect.
2. **No production credential is reachable from PR context.** There is no long-lived
   kubeconfig in the repository, so there is nothing for a PR — or a fork — to use.
3. **The guardrail runs twice, on the rendered artifact both times.** The PR verdict says
   the change was reviewable; the apply-time verdict says it is still true against the
   current `main`.
4. **Failure has a defined shape.** Exit 2 (cannot evaluate) blocks exactly like exit 1;
   unconfigured credentials stop the job before the apply instead of assuming success.

## Running here vs. running in a real service repo

This repo has no cluster, so the cluster-touching steps are gated on a repository
variable, `CLUSTER_CONFIGURED`. Unset (the state here) means: render, both guardrail
passes, and the environment approval all run for real, and the job stops before the apply
with a loud note in the run summary. To make it a real deploy pipeline, set
`CLUSTER_CONFIGURED=true`, `PRODUCTION_DEPLOY_ROLE_ARN`, `AWS_REGION`, and configure the
`production` environment's reviewers.

The `production` environment's protection rules are configured in GitHub, not in these
files — deliberately. A control that a pull request can edit is not a control.
