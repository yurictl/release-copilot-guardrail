#!/usr/bin/env bash
# Render the production overlay. One implementation, used by CI and by humans, so
# "what the guardrail checked" and "what would be applied" cannot drift apart.
#
#   ./scripts/render.sh                  -> rendered manifest on stdout
#   ./scripts/render.sh <path-to-repo>   -> render another checkout (used for the baseline)
set -euo pipefail
root="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
overlay="$root/k8s/overlays/production"

if command -v kustomize >/dev/null 2>&1; then
  kustomize build "$overlay"
elif command -v kubectl >/dev/null 2>&1; then
  kubectl kustomize "$overlay"
else
  echo "render.sh: need kustomize or kubectl on PATH" >&2
  exit 2
fi
