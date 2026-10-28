#!/usr/bin/env bash
# AWS 조 VPN 가입 키 → SSM Parameter Store /duckcloud/aws/join-key (SecureString). 처음 한 번, 또는 노드를 다시 만들 때
#   1) ctl:  sudo headscale users list                      ← t8 의 ID 확인
#              sudo headscale preauthkeys create --user <t8 ID> --reusable --expiration 2h
#   2) Mac:    bash cloud/infra/put-join-key.sh                 ← 키를 붙여 넣음 (화면 · 명령 기록에 남지 않음, 일반 터미널에서)
#         또는 bash cloud/infra/put-join-key.sh --file <파일>   ← 키 한 줄을 적은 파일 (읽은 뒤 지움 — 입력 칸이 없는 곳에서)
#   up.sh 가 vm1 · vm2 가입이 끝나면 이 값을 지운다. 키는 2시간 뒤 만료
source "$(dirname "$0")/lib.sh"
if [[ ${1:-} == --file ]]; then
  KF=${2:?--file <파일>}; [[ -f $KF ]] || die "파일 없음: $KF"
  KEY=$(tr -d ' \r\n' < "$KF"); rm -f "$KF"
else
  [[ -t 0 ]] || die "입력 칸이 없음 — 일반 터미널에서 실행하거나 --file <파일>"
  read -rsp "t8 가입 키: " KEY; echo
fi
[[ -n $KEY ]] || die "키가 비었습니다"
(( ${#KEY} >= 20 )) || die "키가 너무 짧음 (${#KEY}자) — headscale preauthkeys create 출력의 긴 키 문자열 전체여야 함 (ID · 표의 일부가 아님)"
F=$(mktemp); chmod 600 "$F"; trap 'rm -f "$F"' EXIT
printf '%s' "$KEY" > "$F"; unset KEY
aws ssm put-parameter --name "$PARAM/join-key" --type SecureString --overwrite --value "file://$F" >/dev/null
log "저장함 — $PARAM/join-key (값은 출력하지 않음). 다음: bash cloud/infra/up.sh"
