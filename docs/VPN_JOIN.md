# VPN 가입 절차 — Headscale 서버 · 가입 화면 · VM 클라이언트

> 대상 코드: `vpn/agent.py` · `vpn/headscale.py` · `vpn/join.py` · `vpn/install.sh` · `vpn/acl.hujson` · `examples/{vpn,join}.json`
> 주소 · 도메인은 예시(`vpn.example.org`)입니다. 실제 값은 설정 파일에만 둡니다.

## 0. 한눈에

```
[관리자] Headscale 준비 → 사용자 t<조> 생성 → 에이전트 · 가입 화면 기동 → 명단 등록
                                                                │
[팀원]   첫 등록(이메일 + 이름) → 비밀번호 설정 → 로그인 → VM 줄의 [가입 키 발급]
                                                                │ 키는 한 번만 보임 (1시간 · 1회용)
[VM]     curl -fsSL https://vpn.example.org/join/install.sh | sudo bash -s -- t5-vm2
         → tailscale 설치 → 키 입력 → 가입 → 서버가 주소를 100.64.0.52 로 자동 고정
                                                                │
[확인]   tailscale ip -4 = 100.64.0.52 · 컨트롤 서버 ping → (선택) --ufw · --dr-node
```

| 규칙 | 값 |
| :--- | :--- |
| 호스트 이름 | `t<조>-vm<n>` (조 1~9, VM 1~9) |
| VPN 주소 | `100.64.0.<조><n>` — `t5-vm2` = `100.64.0.52` |
| Headscale 사용자 | `t<조>` — 조마다 하나 |
| 컨트롤 서버 | `100.64.0.1` (Headscale · 에이전트 · 가입 화면 · DR 컨트롤) |

## 1. 역할과 권한

| 구성 요소 | 실행 계정 | 받는 곳 | 할 수 있는 것 |
| :--- | :--- | :--- | :--- |
| `vpn.agent` | root | unix socket 2개 (포트 없음) | 쓰기 소켓(그룹 `vpnjoin`): 상태 · 가입 키 · 주소 고정 · 옛 노드 삭제 / 조회 소켓(그룹 `dashboard`): 상태만 |
| `vpn.join` | 권한 없는 계정 (그룹 `vpnjoin`) | `127.0.0.1:8096`, nginx 가 `/join` 전달 | 명단 확인 · 로그인 · 에이전트 호출 |
| nginx | www-data | 443 (TLS) | `/join` → 8096, `X-Real-IP` 전달 |
| 팀원 | 브라우저 | — | 자기 조 번호의 VM 만. 대상은 VM 번호로만 고름 |

## 2. 관리자 — 서버 준비 (1회)

### 2-1. Headscale

`config.yaml` 핵심: `prefixes.v4: 100.64.0.0/24`, `policy.path: /etc/headscale/acl.hujson`.
조마다 사용자를 만듭니다 (없으면 에이전트가 `Headscale 사용자 t5 없음` 으로 거부).

```bash
for s in 3 5 8; do headscale users create t$s; done
cp vpn/acl.hujson /etc/headscale/acl.hujson && systemctl restart headscale
```

### 2-2. 설정 파일

```bash
install -d -m 755 /etc/pilotlight
cp examples/vpn.json  /etc/pilotlight/vpn.json     # slots · login_server · control_ip · protected · group · ro_group
cp examples/join.json /etc/pilotlight/join.json    # db · agent_sock · login_server · public_origin · members · dr_pub
groupadd -f vpnjoin; groupadd -f dashboard
useradd -r -s /usr/sbin/nologin -G vpnjoin vpnjoin
install -d -o vpnjoin -g vpnjoin -m 700 /var/lib/pilotlight-join
```

- `members` 는 `{"email", "name", "slot"}` 목록입니다. 이메일 · 이름은 개인정보이므로 **Git 에 두지 않고 서버 설정에만** 둡니다.
- `slots` 에 없는 조 번호는 에이전트가 모두 거부합니다.
- `protected` 에 넣은 `{"3": [1, 2]}` 같은 운영 노드는 화면에 보이기만 하고 고정 · 삭제하지 않습니다.
- `--dr-node` 를 쓸 경우 `dr_pub` 에 DR 컨트롤의 ed25519 공개키 경로를 둡니다.

### 2-3. 기동

```bash
VPN_AGENT_CONF=/etc/pilotlight/vpn.json python3 -m vpn.agent             # root
sudo -u vpnjoin VPN_JOIN_CONF=/etc/pilotlight/join.json python3 -m vpn.join
```

nginx (TLS 서버 블록 안):

```nginx
location /join {
    proxy_pass http://127.0.0.1:8096;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;   # 없으면 모든 요청이 127.0.0.1 로 묶여 IP 제한이 한꺼번에 걸림
}
```

확인: `curl -s https://vpn.example.org/join/install.sh | head -3` 에 `LOGIN=https://vpn.example.org` 가 보이면 정상.

## 3. 팀원 — 가입 화면

| 단계 | 화면 | 내용 |
| :--- | :--- | :--- |
| ① 첫 등록 | `/join/claim` | 관리자에게 알려 준 이메일 + 이름. 명단과 맞고 등록 전이면 다음 단계 |
| ② 비밀번호 | `/join/setpw` | 12자 이상, 두 칸 같게, **10분 안에**. 한 번만 할 수 있음 |
| ③ 로그인 | `/join` | 이메일 + 비밀번호. 세션 8시간 |
| ④ 가입 키 | VM 줄의 **[가입 키 발급]** | 1회용 · 1시간 만료 · 조당 시간당 10개. **이 화면을 벗어나면 다시 볼 수 없음** |

화면 VM 표의 버튼:

| 버튼 | 나타나는 때 | 하는 일 |
| :--- | :--- | :--- |
| 가입 키 발급 | 항상 (운영 노드 제외) | 키와 VM 에서 칠 한 줄 명령을 보여 줌 |
| 주소 고정 | 현재 주소 ≠ 규칙 주소 | 즉시 규칙 주소로 고침 (자동 고정이 멈췄을 때) |
| 옛 노드 삭제 | 노드가 오프라인 | 같은 이름의 오프라인 노드 삭제 → 새 키로 재가입 |

VM 은 1~2 가 기본이고, 표 맨 아래 줄로 3~9 를 하나씩 추가합니다.

## 4. 팀원 — VM 에서 (Ubuntu)

```bash
curl -fsSL https://vpn.example.org/join/install.sh | sudo bash -s -- t5-vm2
# 가입 키 (화면에 보이지 않음): ← 화면의 키를 붙여 넣고 Enter
```

스크립트가 하는 일 (다시 실행해도 안전 — 이미 맞는 주소면 가입을 건너뜀):

1. 이름 검사 `t<1~9>-vm<1~9>` · root · Ubuntu 확인 → `hostnamectl set-hostname`
2. tailscale 설치 · `tailscaled` 기동
3. 키를 0600 임시 파일로 넘겨 `tailscale up --login-server … --hostname t5-vm2 --accept-dns=false --accept-routes=false --reset` (키는 명령 기록 · 프로세스 목록에 남지 않음)
4. 서버 에이전트가 15초마다 이름을 보고 규칙 주소로 고침 → VM 은 최대 3분(12회) 기다리며 `tailscaled` 재시작
5. `/etc/hosts` 에 `100.64.0.52 t5-vm2` · 컨트롤 서버 `tailscale ping`

확인:

```bash
tailscale ip -4                  # 100.64.0.52
tailscale ping 100.64.0.1        # "via <ip>:<port>" = 직결, "via DERP" = 중계
```

### 4-1. 옵션

| 옵션 | 언제 | 내용 |
| :--- | :--- | :--- |
| `--ufw` | **VPN 주소로 SSH 되는 것을 확인한 뒤** | 들어오는 것은 `tailscale0` 와 WireGuard `41641/udp` 만. 먼저 켜면 기존 SSH 가 끊김 |
| `--dr-node` | DR 컨트롤이 다룰 DB 노드 (1조 vm2 · AWS vm2) | 운영 계정 `drops` 생성. 키는 `/join/dr.pub` 에서 받고 `from="100.64.0.1"` 로만 허용, sudoers 는 `visudo` 검사 통과 시만 |
| `PL_JOIN_KEY_FILE=<파일>` | 사람 없는 기동 (EC2 user-data 등) | 키를 묻지 않고 파일에서 읽은 뒤 바로 지움 |

```bash
curl -fsSL https://vpn.example.org/join/install.sh | sudo bash -s -- t5-vm2 --ufw --dr-node
```

## 5. 가입 뒤 — ACL

가입만으로는 통신이 열리지 않습니다 (ACL 기본 거부). `vpn/acl.hujson` 에 있는 주소(.1 · .32 · .51 · .52 · .81 · .82)만 정해진 포트가 열립니다.
VM 3~9 처럼 **새 주소는 ACL 에 규칙을 추가해야** 다른 노드와 통신합니다 — 관리 사이트 `/admin/acl` 에서 편집 · 경로 시험 · 적용(`base_sha` 일치 시만).

## 6. 문제 해결

| 증상 | 원인 | 조치 |
| :--- | :--- | :--- |
| `주소가 100.64.0.52 가 아님` 으로 끝남 | 자동 고정 실패 (같은 노드 3회 실패하면 자동 고정이 멈춤) | 화면 **[주소 고정]** → VM 에서 스크립트 다시 실행 |
| `노드가 2개 — 오프라인인 옛 노드를 먼저 삭제` | 같은 이름으로 재가입 | **[옛 노드 삭제]** (온라인 노드는 지울 수 없음 — `tailscale logout` 후) |
| `노드가 0개 — --hostname 으로 가입했는지 확인` | 이름이 규칙과 다름 | 스크립트로 다시 가입 (수동 `tailscale up` 금지) |
| `<주소> 를 다른 노드가 쓰는 중` | 주소 충돌 | 그 주소의 노드를 관리자가 확인 · 정리 |
| `headscale 이 이 서버에서 돌고 있지 않음` | 대기 Headscale 에 요청 | 주 서버로 (주/대기 전환은 `hospital-common/services/hsha`) |
| `키는 1시간에 10개까지` | 발급 한도 | 1시간 뒤 |
| 로그인 실패만 반복 | 5회 실패로 계정 잠금, 또는 IP 15분 10회 → 429 | 관리자 잠금 해제 (아래) |
| `DR 컨트롤 공개키를 받지 못함` | `join.json` 의 `dr_pub` 미설정 | 관리자가 설정 후 재시작 |

## 7. 관리자 — 명단 운영

명단은 가입 화면이 시작될 때 `members` 를 DB 에 **추가만** 합니다 (이미 있는 이메일은 그대로). 화면에는 초기화 기능이 없고, 서버에서 SQL 로 처리합니다.

```bash
DB=/var/lib/pilotlight-join/join.db
# 팀원 추가: join.json members 에 넣고 가입 화면 재시작
# 잠금 해제
sudo -u vpnjoin sqlite3 $DB "UPDATE members SET fails=0, locked=0 WHERE email='a@example.org';"
# 비밀번호 초기화 — 팀원이 첫 등록부터 다시. 비밀번호가 바뀌므로 기존 세션은 끊김
sudo -u vpnjoin sqlite3 $DB "UPDATE members SET pw_hash=NULL, claimed=NULL, fails=0, locked=0 WHERE email='a@example.org';"
# 시도 기록 (키 값은 기록하지 않음)
sudo -u vpnjoin sqlite3 $DB "SELECT datetime(at,'unixepoch','localtime'), ip, email, event FROM log ORDER BY at DESC LIMIT 30;"
```

## 8. 서버 없이 해 보기

```bash
python3 -m sim.stack --manual      # http://127.0.0.1:8096/join
```

명단 예시: `site1b@example.org` / `1조 대기` (조 5). 첫 등록 → 가입 키까지 실제 코드로 동작하고, VM 가입 · 주소 고정은 가짜 Headscale 이 대신합니다.
관련 테스트: `python3 -m unittest tests.test_stack -v` (가입 → 자동 고정 · 1회용 키 · 한도 · 주소 충돌 · 잠금 · 429 · 설치 스크립트에 비밀 없음).
