#!/usr/bin/env bash
# Deploy all three Faultline apps to Modal, in dependency order, and print their URLs.
# Usage: scripts/deploy.sh [modal-environment]   (default: local)
set -euo pipefail
ENV="${1:-${MODAL_ENVIRONMENT:-local}}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
log() { printf '{"ts":"%s","svc":"deploy","lvl":"info","ev":"%s","msg":"%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2"; }

log deploy.start "environment=$ENV"
modal secret list -e "$ENV" | grep -q "${FAULTLINE_ANTHROPIC_SECRET:-anthropic-secret}" \
  || { log deploy.error "secret ${FAULTLINE_ANTHROPIC_SECRET:-anthropic-secret} missing in env $ENV"; exit 1; }

log deploy.sandbox_env "modal deploy services/sandbox-env/modal_app.py"
modal deploy -e "$ENV" "$ROOT/services/sandbox-env/modal_app.py"
SANDBOX_ENV_URL="${SANDBOX_ENV_URL:-$(python3 -c "import modal; print(modal.Function.from_name('faultline-sandbox-env','api',environment_name='$ENV').get_web_url())")}"
log deploy.sandbox_env.url "$SANDBOX_ENV_URL"

log deploy.harness "modal deploy services/agent-harness/modal_app.py"
SANDBOX_ENV_URL="$SANDBOX_ENV_URL" modal deploy -e "$ENV" "$ROOT/services/agent-harness/modal_app.py"
HARNESS_URL="${HARNESS_URL:-$(python3 -c "import modal; print(modal.Function.from_name('faultline-harness','api',environment_name='$ENV').get_web_url())")}"
log deploy.harness.url "$HARNESS_URL"

log deploy.web "modal deploy apps/web/modal_app.py"
HARNESS_URL="$HARNESS_URL" modal deploy -e "$ENV" "$ROOT/apps/web/modal_app.py"
WEB_URL="$(python3 -c "import modal; print(modal.Server.from_name('faultline-web','site',environment_name='$ENV').get_url())" 2>/dev/null || echo '(see modal deploy output above)')"
log deploy.web.url "$WEB_URL"

printf '\nSANDBOX_ENV_URL=%s\nHARNESS_URL=%s\nWEB_URL=%s\n' "$SANDBOX_ENV_URL" "$HARNESS_URL" "$WEB_URL"
