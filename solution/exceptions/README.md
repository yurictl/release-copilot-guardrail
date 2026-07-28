# Policy exceptions

This directory is normally **empty**. A file here waives one guardrail rule, for one
workload, for a bounded number of hours. It is the pressure-release valve that stops
people from disabling the guardrail entirely at 03:00 — which is the failure mode a
policy with no exception path always ends in.

Loaded by `validate_manifest.py --exceptions solution/exceptions`. A record that does
not hold is not ignored: it is reported as `[REJECTED EXCEPTION]` with the reason, in
the PR comment and in the change summary.

## What an exception does and does not do

- A valid exception downgrades a matching `BLOCK` to `CAVEAT`. The finding stays in
  the report, carrying the exception ID and the approver names. Nothing is hidden.
- It does **not** approve the deployment. `production-apply.yml` still requires the
  `production` environment reviewers.
- It does **not** silence the check for anyone else: the record names one rule and
  one workload.

## Who can use it

- **Requester:** any engineer on the owning team, during a declared incident or a
  release the on-call has agreed is urgent.
- **Approvers:** at least two people, and the set must include one **sre-oncall** and
  one **service-owner**. The requester may not be one of them — self-approval is
  rejected by the validator, not by convention.

## What evidence is required

Every field below is mandatory; a record missing any of them is rejected.

| Field | Why it is required |
|---|---|
| `id` | Stable handle for the audit trail and the post-incident review |
| `rule` | Exactly one rule. A blanket waiver is not expressible in this format |
| `workload` | `namespace/name`; must match the workload being validated |
| `incident` | A real ticket. "Urgent" without an incident is a scheduling problem |
| `reason` | Why the risk is acceptable *now*, in terms of the failure mode being waived |
| `requested_by` | Named individual |
| `approvers` | ≥2, with the required roles, none of them the requester |
| `created_at` / `expires_at` | Absolute UTC timestamps; ≤24h apart |

## How it expires

`expires_at` is absolute and evaluated at validation time, not at merge time. Once the
clock passes it, the next PR and the next apply block again — nobody has to remember to
remove the file, and a merged-and-forgotten waiver stops working on its own. A record
whose window exceeds `exceptions.max_duration_hours` (24) is rejected outright, so
"expires 2027" is not expressible either.

Deleting the file after the incident is housekeeping, not enforcement.

## What can never be waived

`exceptions.non_waivable_rules` in `solution/policy/guardrail.yaml`:

- `hardcoded-credential`
- `secret-downgrade`

No incident makes committing a credential acceptable. The fast path for a secret you
cannot reach is to fix secret delivery, not to inline the value — a "temporary" key in
git is permanent, and rotating it later is a task nobody schedules.

## How it is audited

1. The record is a committed file: it goes through PR review like any other change, and
   `git log` shows who added it and when.
2. Every apply attaches the change summary to the workflow run artifacts, including
   `exceptions.applied` (with approvers and incident) and `exceptions.rejected`.
3. `grep -rl "rule: " solution/exceptions/` at any time lists the live waivers.
4. Standing review: every exception used in the last period is read out at the weekly
   operational review, with one question — *did this become a permanent workaround?* If
   the same rule is waived twice for the same reason, the rule or the service is wrong,
   and that is a backlog item, not a third exception.

## Template

Copy `EXAMPLE-exception.yaml.template` to `EXC-<date>-<n>.yaml` and fill it in.
The template's `.template` extension keeps the loader from picking it up.
