#!/usr/bin/env bash
# 배포: git pull → 이미지 빌드 → 재시작 → /api/health 확인 → 실패하면 이전 이미지로 되돌리기.
#
#   bash /opt/flow/scripts/deploy.sh
#
# 무중단에 가깝게: 새 이미지를 다 만든 뒤에야 컨테이너를 바꾼다. 빌드하는 몇 분 동안은 기존
# 서버가 그대로 응답하고, 바뀐 컨테이너만 다시 만들어지므로 끊기는 시간은 몇 초다.
#
# 환경변수
#   FLOW_DIR        기본: 이 스크립트가 있는 저장소 (/opt/flow)
#   NO_PULL=1       git pull 없이 지금 코드로 배포
#   HEALTH_URL      기본 http://127.0.0.1:8800/api/health
#   HEALTH_TIMEOUT  기본 180(초)
set -euo pipefail

FLOW_DIR="${FLOW_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8800/api/health}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
SERVICES=(api worker web)
PROJECT=flow

say() { printf '\n== [%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
fail() { printf '!! %s\n' "$*" >&2; exit 1; }

cd "$FLOW_DIR"

# --- 설정 확인 -------------------------------------------------------------
[ -f backend/.env ] || fail "backend/.env가 없습니다. docs/DEPLOY.md의 '.env 작성'을 먼저 하세요."
[ -f .env ] || fail "저장소 맨 위 .env가 없습니다(JWT_SECRET·DOMAIN 등). docs/DEPLOY.md 참고."
env_value() { sed -n "s/^$1=//p" .env | tail -1 | tr -d '\r' | sed 's/^"\(.*\)"$/\1/'; }
[ -n "$(env_value JWT_SECRET)" ] || fail ".env에 JWT_SECRET이 비어 있습니다. 비어 있으면 운영 서버가 뜨지 않습니다."

COMPOSE=(docker compose -p "$PROJECT" -f docker-compose.yml)
if [ -n "$(env_value DOMAIN)" ]; then
  COMPOSE+=(-f docker-compose.prod.yml)
  echo "도메인 $(env_value DOMAIN) — HTTPS(Caddy) 포함 배포"
else
  echo "DOMAIN 없음 — 이 서버 안(127.0.0.1)에서만 열리는 시험 배포"
fi

health_ok() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
  done
  return 1
}

notify_failure() {
  # 장애 알림(텔레그램 설정 시). 알림이 실패해도 배포 결과에는 영향 없음.
  timeout 30 "${COMPOSE[@]}" exec -T api python -m app.services.alerts deploy_failed "$1" >/dev/null 2>&1 || true
}

# --- 1. 코드 받기 ----------------------------------------------------------
previous_rev="$(git rev-parse HEAD)"
if [ "${NO_PULL:-0}" != "1" ]; then
  say "1/4 git pull"
  git pull --ff-only
else
  say "1/4 git pull 건너뜀(NO_PULL=1)"
fi
new_rev="$(git rev-parse HEAD)"
echo "배포할 커밋: $(git log --oneline -1)"
# 이미지 안에는 .git이 없어서, 화면 아래 버전과 /api/health의 커밋 번호를 빌드 때 넣어 준다.
export FLOW_GIT_COMMIT="$(git rev-parse --short=7 HEAD)"

# --- 2. 지금 이미지 보관 후 새로 빌드 -----------------------------------------
say "2/4 이미지 빌드 (지금 서버는 그대로 동작 중)"
had_previous=0
for svc in "${SERVICES[@]}"; do
  if docker image inspect "$PROJECT-$svc:latest" >/dev/null 2>&1; then
    docker tag "$PROJECT-$svc:latest" "$PROJECT-$svc:rollback"
    had_previous=1
  fi
done
if ! "${COMPOSE[@]}" build; then
  [ "$new_rev" != "$previous_rev" ] && git reset -q --hard "$previous_rev"
  notify_failure "이미지 빌드 실패($(git rev-parse --short "$new_rev")). 서버는 이전 버전 그대로입니다."
  fail "빌드 실패 — 서버는 이전 버전 그대로입니다(아무것도 바꾸지 않음)."
fi

# 빠진 운영 설정값 알림(배포는 막지 않음 — 도메인 없이 시험 배포할 수도 있으므로).
if ! "${COMPOSE[@]}" run --rm --no-deps -T -v "$FLOW_DIR/.env:/tmp/root.env:ro" \
  api python -m app.launch_check --root-env /tmp/root.env; then
  echo "!! 위 '필수' 중 빠진 것이 있습니다. 실제 출시 전에 채우세요(docs/LAUNCH_CHECKLIST.md)." >&2
fi

# --- 3. 바뀐 컨테이너만 다시 만들기 -------------------------------------------
say "3/4 재시작"
# api가 뜨지 않으면 up이 오류로 끝난다. 판단은 아래 상태 확인에 맡긴다.
if ! "${COMPOSE[@]}" up -d --no-build --remove-orphans; then
  echo "!! 재시작 중 오류(아래 상태 확인에서 판단)" >&2
fi

# --- 4. 상태 확인, 실패하면 되돌리기 -----------------------------------------
say "4/4 상태 확인 ($HEALTH_URL, 최대 ${HEALTH_TIMEOUT}초)"
if health_ok; then
  echo "정상: $(curl -fsS --max-time 5 "$HEALTH_URL")"
  docker image prune -f >/dev/null 2>&1 || true
  say "배포 완료 — $(git log --oneline -1)"
  exit 0
fi

echo "!! 새 버전이 응답하지 않습니다. 최근 로그:" >&2
# 재시작을 반복하는 컨테이너에서는 compose logs가 오래 멈출 수 있어 20초로 자른다.
timeout 20 "${COMPOSE[@]}" logs --no-color --tail 40 api >&2 || true
if [ "$had_previous" != "1" ]; then
  fail "첫 배포라 되돌릴 이전 이미지가 없습니다. 위 로그를 확인하세요."
fi

say "되돌리기: 이전 이미지로 재시작"
for svc in "${SERVICES[@]}"; do
  if docker image inspect "$PROJECT-$svc:rollback" >/dev/null 2>&1; then
    docker tag "$PROJECT-$svc:rollback" "$PROJECT-$svc:latest"
  fi
done
[ "$new_rev" != "$previous_rev" ] && git reset -q --hard "$previous_rev"
"${COMPOSE[@]}" up -d --no-build --force-recreate || true
if health_ok; then
  notify_failure "새 버전($(git rev-parse --short "$new_rev"))이 응답하지 않아 이전 버전으로 되돌렸습니다."
  fail "배포 실패 → 이전 버전($(git rev-parse --short "$previous_rev"))으로 되돌렸고 정상 응답합니다."
fi
fail "되돌린 뒤에도 응답이 없습니다. '${COMPOSE[*]} logs api'로 확인하세요."
