#!/usr/bin/env bash
# 새 Ubuntu 서버(22.04/24.04) 준비 — 한 번 실행하면 끝나고, 여러 번 실행해도 안전하다.
#
#   sudo REPO_URL=git@github.com:계정/저장소.git DOMAIN=내도메인.com PORTFOLIO_MODE=1 bash server_setup.sh
#
# 하는 일 (이미 된 단계는 건너뜀) — x86·ARM(Oracle Cloud Ampere 등) 모두
#   1. 기본 패키지(git, curl, ufw 등), 자동 보안 업데이트, Docker Engine + Compose 플러그인
#   2. 방화벽: 22(SSH)·80·443만 허용 (Oracle Cloud 기본 iptables도 처리)
#   3. 스왑 2GB (/swapfile)
#   4. 영문 경로 /opt/flow 에 저장소 clone (이미 있으면 그대로 둠)
#   5. 설정 파일: 맨 위 .env(무작위 JWT_SECRET)와 backend/.env를 없을 때만 만듦 — 값은 출력하지 않음
#   6. 매일 새벽 백업 예약(cron)
#
# 환경변수
#   REPO_URL      clone할 주소 (필수, /opt/flow가 이미 있으면 생략 가능)
#   REPO_BRANCH   기본 main
#   FLOW_DIR      기본 /opt/flow  (한글이 들어간 경로는 Docker 빌드가 실패한다)
#   DEPLOY_USER   docker 그룹에 넣을 사용자 (기본: sudo를 부른 사용자)
#   DOMAIN        사이트 도메인(새 .env에 들어감). 비우면 나중에 .env에 직접
#   PORTFOLIO_MODE=1  사업자 없이 결제 없는 데모(.env에 PORTFOLIO_MODE=true, 하루 AI 비용 상한 2달러)
#   SWAP_SIZE_GB  기본 2
#   SKIP_FIREWALL=1, SKIP_SWAP=1, SKIP_ENV=1, SKIP_CRON=1  해당 단계 건너뛰기
set -euo pipefail

FLOW_DIR="${FLOW_DIR:-/opt/flow}"
REPO_BRANCH="${REPO_BRANCH:-main}"
SWAP_SIZE_GB="${SWAP_SIZE_GB:-2}"
DEPLOY_USER="${DEPLOY_USER:-${SUDO_USER:-}}"

say() { printf '\n== %s\n' "$*"; }
warn() { printf '!! %s\n' "$*" >&2; }

if [ "$(id -u)" -ne 0 ]; then
  echo "root 권한이 필요합니다: sudo bash $0" >&2
  exit 1
fi

. /etc/os-release
if [ "${ID:-}" != "ubuntu" ]; then
  warn "Ubuntu가 아닙니다(${PRETTY_NAME:-알 수 없음}). Ubuntu 22.04/24.04 기준 스크립트입니다."
fi

export DEBIAN_FRONTEND=noninteractive

say "1/6 기본 패키지"
need=()
for pkg in ca-certificates curl git ufw gnupg openssl cron unattended-upgrades; do
  dpkg -s "$pkg" >/dev/null 2>&1 || need+=("$pkg")
done
if [ "${#need[@]}" -gt 0 ]; then
  apt-get update -qq
  apt-get install -y -qq "${need[@]}" >/dev/null
  echo "설치: ${need[*]}"
else
  echo "이미 설치됨"
fi
# Security patches every day, unattended (the default Ubuntu policy: security updates only).
if [ ! -f /etc/apt/apt.conf.d/20auto-upgrades ] || ! grep -q 'Unattended-Upgrade "1"' /etc/apt/apt.conf.d/20auto-upgrades; then
  printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' > /etc/apt/apt.conf.d/20auto-upgrades
  echo "자동 보안 업데이트 켬"
else
  echo "자동 보안 업데이트 이미 켜짐"
fi

say "1/6 Docker Engine + Compose"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  echo "이미 설치됨: $(docker --version)"
else
  install -m 0755 -d /etc/apt/keyrings
  if [ ! -s /etc/apt/keyrings/docker.asc ]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
  fi
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
  echo "설치: $(docker --version), $(docker compose version)"
fi
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  systemctl enable --now docker >/dev/null 2>&1 || warn "docker 서비스 시작 실패 — 'systemctl status docker'로 확인하세요"
else
  warn "systemd가 없는 환경입니다(컨테이너 등). Docker 데몬 자동 시작은 건너뜁니다."
fi
if [ -n "$DEPLOY_USER" ] && id "$DEPLOY_USER" >/dev/null 2>&1; then
  if id -nG "$DEPLOY_USER" | tr ' ' '\n' | grep -qx docker; then
    echo "$DEPLOY_USER 는 이미 docker 그룹"
  else
    usermod -aG docker "$DEPLOY_USER"
    echo "$DEPLOY_USER 를 docker 그룹에 추가(다시 로그인하면 sudo 없이 docker 사용 가능)"
  fi
fi

say "2/6 방화벽 (22·80·443만 허용)"
ORACLE_RULES=/etc/iptables/rules.v4
if [ "${SKIP_FIREWALL:-0}" = "1" ]; then
  echo "SKIP_FIREWALL=1 — 건너뜀"
elif [ -f "$ORACLE_RULES" ] && grep -q -- '-A INPUT .*-j REJECT' "$ORACLE_RULES"; then
  # Oracle Cloud Ubuntu images ship iptables rules that reject everything but SSH, and ufw's
  # chains would sit behind that REJECT. Open 80/443 in those rules instead of using ufw.
  echo "Oracle Cloud 기본 방화벽(iptables) 감지 — ufw 대신 그 규칙에 80·443을 추가"
  for port in 80 443; do
    if ! iptables -C INPUT -p tcp --dport "$port" -m conntrack --ctstate NEW -j ACCEPT 2>/dev/null; then
      iptables -I INPUT 1 -p tcp --dport "$port" -m conntrack --ctstate NEW -j ACCEPT 2>/dev/null \
        || warn "iptables 규칙을 지금 넣지 못했습니다(권한). 재부팅 뒤에는 아래 파일 규칙이 적용됩니다."
    fi
    # Persist by editing the saved rules (not iptables-save: that would also save Docker's chains).
    line="-A INPUT -p tcp -m conntrack --ctstate NEW -m tcp --dport $port -j ACCEPT"
    if ! grep -qxF -- "$line" "$ORACLE_RULES"; then
      awk -v add="$line" '!done && /^-A INPUT .*-j REJECT/ { print add; done=1 } { print }' "$ORACLE_RULES" > "$ORACLE_RULES.flow" \
        && mv "$ORACLE_RULES.flow" "$ORACLE_RULES"
    fi
  done
  echo "열림: 22(기존)·80·443 — Oracle 콘솔의 Security List에도 80·443이 열려 있어야 합니다"
else
  ufw default deny incoming >/dev/null
  ufw default allow outgoing >/dev/null
  for port in 22/tcp 80/tcp 443/tcp; do ufw allow "$port" >/dev/null; done
  if ufw --force enable >/dev/null 2>&1; then
    ufw status | sed -n '1,12p'
  else
    warn "ufw를 켜지 못했습니다(커널 방화벽 권한 없음). 클라우드 보안 그룹에서 22·80·443만 열어 주세요."
  fi
  # Docker가 publish한 포트는 ufw를 우회한다. 이 저장소의 compose는 api·web을 127.0.0.1에만 열고
  # 외부에는 Caddy(80·443)만 열어서 문제가 없다. 다른 포트를 0.0.0.0으로 열지 마라.
fi

say "3/6 스왑 ${SWAP_SIZE_GB}GB"
if [ "${SKIP_SWAP:-0}" = "1" ]; then
  echo "SKIP_SWAP=1 — 건너뜀"
elif [ "$(awk '/^SwapTotal:/ {print int($2 / 1024 / 1024 + 0.5)}' /proc/meminfo)" -ge "$SWAP_SIZE_GB" ]; then
  echo "이미 충분함: $(swapon --show --noheadings | tr -s ' ' | head -2 | paste -sd ';')"
else
  if [ ! -f /swapfile ]; then
    fallocate -l "${SWAP_SIZE_GB}G" /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=$((SWAP_SIZE_GB * 1024)) status=none
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
  fi
  if swapon /swapfile 2>/dev/null; then
    echo "켜짐"
  else
    warn "스왑을 켜지 못했습니다(컨테이너 등 권한 없음). 실제 서버에서는 정상 동작합니다."
  fi
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  if [ -d /etc/sysctl.d ]; then
    echo 'vm.swappiness=10' > /etc/sysctl.d/99-flow-swap.conf
    sysctl -q -p /etc/sysctl.d/99-flow-swap.conf 2>/dev/null || true
  fi
fi

say "4/6 저장소 clone → $FLOW_DIR"
if printf '%s' "$FLOW_DIR" | LC_ALL=C grep -q '[^ -~]'; then
  echo "FLOW_DIR에 영문이 아닌 글자가 있습니다. Docker 빌드가 실패하니 영문 경로를 쓰세요." >&2
  exit 1
fi
if [ -d "$FLOW_DIR/.git" ]; then
  echo "이미 있음: $(git -C "$FLOW_DIR" log --oneline -1)"
else
  if [ -z "${REPO_URL:-}" ]; then
    echo "REPO_URL이 필요합니다. 예) sudo REPO_URL=git@github.com:계정/저장소.git bash $0" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$FLOW_DIR")"
  git clone --branch "$REPO_BRANCH" "$REPO_URL" "$FLOW_DIR"
fi
if [ -n "$DEPLOY_USER" ] && id "$DEPLOY_USER" >/dev/null 2>&1; then
  chown -R "$DEPLOY_USER":"$DEPLOY_USER" "$FLOW_DIR"
fi
mkdir -p /var/backups/flow
if [ -n "$DEPLOY_USER" ] && id "$DEPLOY_USER" >/dev/null 2>&1; then
  chown "$DEPLOY_USER":"$DEPLOY_USER" /var/backups/flow
fi

portfolio=0
case "$(printf '%s' "${PORTFOLIO_MODE:-}" | tr '[:upper:]' '[:lower:]')" in 1|true|yes|on) portfolio=1 ;; esac

# KEY=value for the first KEY= line (commented or not), or append. Values here never contain "|".
set_env() {
  local file="$1" key="$2" value="$3"
  if grep -qE "^#? *${key}=" "$file"; then
    sed -i -E "0,/^#? *${key}=/s|^#? *${key}=.*|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

say "5/6 설정 파일 (없을 때만 만듦, 값은 출력하지 않음)"
root_env="$FLOW_DIR/.env"
backend_env="$FLOW_DIR/backend/.env"
if [ "${SKIP_ENV:-0}" = "1" ]; then
  echo "SKIP_ENV=1 — 건너뜀"
else
  if [ -f "$root_env" ]; then
    echo "이미 있음: $root_env (그대로 둠)"
  else
    {
      echo "# server_setup.sh가 $(date +%F)에 만듦. JWT_SECRET은 바꾸지 마세요(바꾸면 모두 로그아웃)."
      echo "DOMAIN=${DOMAIN:-}"
      echo "JWT_SECRET=$(openssl rand -hex 32)"
      echo "VITE_SITE_URL=${DOMAIN:+https://$DOMAIN}"
      if [ "$portfolio" = "1" ]; then
        echo "# 사업자 없이 결제 없는 데모. 사업자를 내면 이 줄을 지우고 VITE_BIZ_*를 채운 뒤 deploy.sh"
        echo "PORTFOLIO_MODE=true"
      else
        for key in VITE_BIZ_NAME VITE_BIZ_OWNER VITE_BIZ_REG_NO VITE_BIZ_MAIL_ORDER_NO VITE_BIZ_ADDRESS VITE_BIZ_PHONE VITE_BIZ_EMAIL; do
          echo "$key="
        done
      fi
      echo "VITE_PRIVACY_OFFICER_NAME="
      echo "VITE_PRIVACY_OFFICER_CONTACT="
      echo "VITE_POLICY_EFFECTIVE_DATE="
    } > "$root_env"
    chmod 600 "$root_env"
    echo "만듦: $root_env (무작위 JWT_SECRET 포함)"
  fi
  if [ -f "$backend_env" ]; then
    echo "이미 있음: $backend_env (그대로 둠)"
  elif [ -f "$FLOW_DIR/backend/.env.example" ]; then
    cp "$FLOW_DIR/backend/.env.example" "$backend_env"
    set_env "$backend_env" ENVIRONMENT production
    set_env "$backend_env" APP_ENV production
    # Empty on purpose: docker-compose takes JWT_SECRET from the root .env above.
    set_env "$backend_env" JWT_SECRET ""
    if [ "$portfolio" = "1" ]; then
      set_env "$backend_env" DAILY_AI_BUDGET_USD 2
    fi
    chmod 600 "$backend_env"
    echo "만듦: $backend_env (backend/.env.example에서, 운영 모드)"
  else
    warn "backend/.env.example이 없어 backend/.env를 만들지 못했습니다."
  fi
  if [ -n "$DEPLOY_USER" ] && id "$DEPLOY_USER" >/dev/null 2>&1; then
    chown "$DEPLOY_USER":"$DEPLOY_USER" "$root_env" "$backend_env" 2>/dev/null || true
  fi
fi

say "6/6 매일 백업 예약 (새벽 4시 10분)"
cron_user="${DEPLOY_USER:-root}"
cron_line="10 4 * * * bash $FLOW_DIR/scripts/backup.sh >> /var/log/flow-backup.log 2>&1"
if [ "${SKIP_CRON:-0}" = "1" ]; then
  echo "SKIP_CRON=1 — 건너뜀"
elif ! command -v crontab >/dev/null 2>&1; then
  warn "crontab이 없습니다. docs/DEPLOY.md 7절대로 직접 등록하세요."
elif crontab -u "$cron_user" -l 2>/dev/null | grep -qF "scripts/backup.sh"; then
  echo "이미 예약됨($cron_user)"
else
  { crontab -u "$cron_user" -l 2>/dev/null || true; echo "$cron_line"; } | crontab -u "$cron_user" -
  touch /var/log/flow-backup.log
  chown "$cron_user" /var/log/flow-backup.log 2>/dev/null || true
  echo "예약함($cron_user): 최근 7일치 보관"
fi

say "완료 — 남은 일"
echo "1) 값 채우기 (nano로 열어서, 화면이나 채팅에 붙여 넣지 말고):"
echo "   $root_env : DOMAIN, VITE_PRIVACY_OFFICER_NAME·CONTACT, VITE_POLICY_EFFECTIVE_DATE$([ "$portfolio" = "1" ] || echo ', VITE_BIZ_*')"
echo "   $backend_env : ADMIN_EMAIL, ADMIN_PASSWORD(12자 이상), SMTP_*(인증 메일), 선택: OPENAI_API_KEY, TELEGRAM_*"
echo "2) 다시 로그인(docker 그룹 적용) → bash $FLOW_DIR/scripts/launch_check.sh → bash $FLOW_DIR/scripts/deploy.sh"
echo "(자세한 순서는 docs/DEPLOY.md, Oracle Cloud는 docs/ORACLE_CLOUD.md)"
