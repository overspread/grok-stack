#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is not running. Please start Docker Desktop first, then run this file again."
  exit 1
fi

echo "==> Starting grok2api on http://127.0.0.1:8000"
cd grok2api
# Local tree uses config.toml; remote :latest is a binary image that expects
# /run/grok2api/config.yaml. Compose builds grok2api-local:toml from Dockerfile.
docker compose up -d --build
API_KEY=$(awk -F= '/GROK2API_API_KEY/{print $2}' data/.keys)
APP_KEY=$(awk -F= '/GROK2API_APP_KEY/{print $2}' data/.keys)

echo "==> Waiting for grok2api..."
for i in {1..30}; do
  if curl -fsS http://127.0.0.1:8000/v1/models -H "Authorization: Bearer $API_KEY" >/dev/null 2>&1; then
    break
  fi
  sleep 2
  if [ "$i" = 30 ]; then
    echo "grok2api did not become ready. Logs:"
    docker compose logs --tail=80
    exit 1
  fi
done

echo "grok2api ready."
echo "Admin: http://127.0.0.1:8000/admin"
echo "App key is in: $(pwd)/data/.keys"
echo "API key is in: $(pwd)/data/.keys"

echo
read -r -p "Start grok-reg-tool Web UI too? [y/N] " ans
if [[ "$ans" =~ ^[Yy]$ ]]; then
  cd ../grok-reg-tool/docker
  if [ ! -f .env ]; then
    cp .env.example .env
  fi
  docker compose up -d --build
  echo "grok-reg-tool ready at http://127.0.0.1:6657"
  echo "Initial web credentials: docker logs grok-reg-tool"
fi

echo
cat <<EOF
Next:
1. Import valid SSO tokens into grok2api admin panel, or via API:
   curl -X POST http://127.0.0.1:8000/v1/admin/tokens \\
     -H "Authorization: Bearer $APP_KEY" \\
     -H "Content-Type: application/json" \\
     -d '{"ssoBasic":[{"token":"YOUR_SSO_TOKEN"}]}'

2. Test chat:
   curl http://127.0.0.1:8000/v1/chat/completions \\
     -H "Authorization: Bearer $API_KEY" \\
     -H "Content-Type: application/json" \\
     -d '{"model":"grok-4","messages":[{"role":"user","content":"hi"}]}'
EOF
