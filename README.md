# Hybrid DR — DR 컨트롤 · VPN 서버 · 클라이언트 가입 · 통합 관제 + 게이트웨이 · 운영 공통

온프레미스(1조)가 통째로 멈췄을 때만 AWS Pilot Light 가 진료를 넘겨받고, 1조가 돌아오면 데이터를 맞춘 뒤 되돌려 줍니다.
모든 노드는 자체 VPN(Headscale/WireGuard) 위에서 **이름 규칙 = 주소 규칙**으로 묶이고, 한 화면에서 판정 · 복제 · 노드 · 이력을 봅니다.

게이트웨이 이중화 · 운영 콘솔 · 호스트 VM 조작은 [`hospital-common/`](hospital-common/README.md) 에 작은 서비스로 나눠 담았습니다.

교육용으로 기능의 뼈대만 남겼습니다. 표준 라이브러리만 쓰며, 같은 코드를 실제 인프라에도, `sim/` 의 가짜 인프라에도 붙일 수 있습니다.

```
 팀원 브라우저 ──▶ [가입 화면 vpn/join.py] ──unix socket──▶ [VPN 에이전트 vpn/agent.py · root] ──▶ Headscale
                                                                    ▲ overview (조회 전용 socket)
 운영자 브라우저 ─▶ [통합 관제 dashboard/server.py] ──────────────────┤
                                 └── Bearer ──▶ [DR 컨트롤 pilotlight/control.py] ── judge.tick (30초)
                                                         │ tcp 3306 · SSH(sudo mysql) · aws ec2 · nginx upstream
     ┌──────── 1조 온프레미스 ────────┐   GTID 복제 (SSL)   ┌──────── AWS Pilot Light ────────┐
     │ t3-vm2 .32 쓰기 ─▶ t5-vm2 .52  ├───────────────────▶│ t8-vm2 .82 DB  평시 켜 둠 (읽기 전용) │
     │ t3-vm1 .31 · t5-vm1 .51 앱     │  대기 DB 를 우선 따라감 │ t8-vm1 .81 앱  평시 정지 (과금 0)   │
     └───────────────────────────────┘                     └─────────────────────────────────────┘
                     모든 주소는 VPN 100.64.0.<조><VM>  (컨트롤 서버 = 100.64.0.1)
```

## 시스템 설계 요약

목표 구성은 온프레미스 1조를 주 서비스로, AWS를 DB 복제본을 상시 유지하는 Pilot Light 대기로 둡니다. 아래 노드·주소는 설계 예시이며, 이 저장소의 AWS 화면과 `cloud/infra/` 는 실제 계정에 연결되지 않는 데모·참고 구성입니다.

| 구간 | 노드 | 역할 |
| :--- | :--- | :--- |
| 온프레미스 1조 | `t3-vm1` · `t5-vm1` | 병원 앱 (Kubernetes, NodePort 30080·30800) |
| | `t3-vm2` → `t5-vm2` | MySQL 쓰기 DB → 대기 DB (GTID 복제) |
| AWS Pilot Light (설계 예시: `ap-southeast-2`) | `t8-vm2` | MySQL 복제본. 평시 실행·읽기 전용 |
| | `t8-vm1` · ASG 워커 0~2대 | 앱과 워커. 평시 앱 정지·워커 0, 승격 시 기동·확장 |
| 컨트롤 서버 | VPN `100.64.0.1` | 판정·DR 제어·Headscale·통합 관제 |
| 게이트웨이 S1/S2 | Cloudflare Tunnel 뒤 | 공개 입구 이중화 및 Headscale 주/대기 |

- 노드 이름은 `t<조>-vm<n>` → `100.64.0.<조><n>` 주소 규칙으로 매핑합니다. AWS 리전은 실제 실행 설정의 `AWS_REGION`으로 정합니다.
- AWS 승격은 1조 DB 전체가 5분 동안 응답하지 않고, 대기 DB가 정상이며, 장애 직전 복제 지연이 60초 이하일 때만 진행합니다. 증인 확인과 기존 쓰기 DB 펜싱을 거친 뒤 DB 동기화·쓰기 허용, 앱 준비 확인, 워커 확장, 공개 입구 전환 순으로 진행합니다.
- 1조 DB가 3분 연속 응답하면 복귀 절차를 시작합니다. AWS의 쓰기를 멈추고 GTID를 맞춘 뒤 마지막 위치를 확인하고 트래픽을 넘깁니다.
- 복제 지연 60초는 승격 허용 기준이지 데이터 손실이 없다는 절대 보장은 아닙니다. RTO 실측값은 문서화되어 있지 않으며, 설계상 감지 대기 5분에 DB·앱 기동 및 헬스체크 시간이 더해집니다.
- `python3 -m pilotlight cost` 예시 단가 기준 월 비용은 Pilot Light `$26.76`, Warm Standby `$68.96`, 전체 정지 `$3.84`입니다. 이 계산은 EC2·EBS·공인 IPv4 예시 비용이며 실제 리전·시점의 견적과 다를 수 있습니다.

---

## 1. 네 부분과 핵심 개념

### 1-1. DR 컨트롤 (`pilotlight/`)

| 개념 | 문제 | 답 | 위치 |
| :--- | :--- | :--- | :--- |
| **판정** | 성급하면 쓰기 DB 가 둘(split-brain), 늦으면 진료 중단 | 1조 DB **전부** 5분 무응답 + 대기 정상 + 장애 직전 복제 지연 ≤ 60초 | `judge.py` |
| **증인** | 판정 서버만 1조를 못 보는 경우 | AWS 복제본 IO 스레드가 아직 1조를 받으면(IO Yes) 보류. 접속 오류(2003 등)로 끊긴 경우만 통과 | `judge.witness` |
| **펜싱** | 옛 쓰기 DB 가 살아나 새 기록을 받음 | **앱 계정만** `ACCOUNT LOCK` + 세션 `KILL` — 승격 때 닿는 곳, 승격 뒤 돌아오는 즉시 | `fence.py` |
| **GTID 승격 · 복귀** | 받아 둔 기록을 잃지 않고 역할 교대 | 승격: IO 정지 → `WAIT_FOR_EXECUTED_GTID_SET` → 쓰기 가능. 복귀: 백업 → AWS 덤프 적재 → 모자란 GTID 만 `gtid_purged` → `failback` 채널 → AWS 쓰기 멈춤 → 마지막 GTID 확인 → 전환 | `replica.py` |
| **Pilot Light** | 1년에 한 번 쓸 대기를 늘 켜 두면 낭비 | DB 만 켜서 RPO 유지, 승격 때 `ec2 start` → `/readyz` 200 → ASG 확장 → 공개 입구 전환 | `power.py` · `frontdoor.py` · `cost.py` |
| **컨트롤 API** | 사람이 승인 · 정지할 창구 | `GET /api/state` · `POST /api/action` (prepare · mode auto/off · tick), Bearer 토큰, 바꾼 요청은 이력에 | `control.py` |

```
          1조 DB 응답                     전부 무응답 ≥ 5분 · 대기 정상 · 증인 통과
 [정상] ◀──────────── [1조 장애 감지] ──────────────────────────────────▶ [AWS 승격]
   ▲                                                                       │ 돌아온 1조 DB 즉시 잠금
   │ 다시 채움 (AWS 를 대기로)                                               ▼
   └──────────────── [복귀] ◀──── 1조 DB 3분 연속 응답 · AWS DB 정상 ───── [복귀 대기]
```

### 1-2. VPN 서버 (`vpn/agent.py` · `vpn/headscale.py` · `vpn/acl.hujson`)

- **이름 = 주소 규칙**: `t<조>-vm<n>` → `100.64.0.<조><n>` (t5-vm2 = .52). 요청은 조 · VM 번호만 받고 이름 · 주소 · Headscale 사용자는 에이전트가 계산합니다. 화면이 임의 노드를 고르지 못합니다.
- **자동 주소 고정**: 가입하면 Headscale 이 아무 주소나 주므로, 감시가 이름을 보고 규칙 주소로 고칩니다(정지 → DB 백업 → 갱신 → 시작, 충돌 · 대기 서버 · 운영 노드는 거부, 3회 실패 시 그 노드는 멈춤).
- **권한 분리**: 에이전트만 root. 포트를 열지 않고 unix socket 두 개 — 쓰기(가입 화면용) · 조회 전용(관제용, `overview` 만).
- **1회용 가입 키**: 1시간 만료 · 재사용 불가 · 팀원당 시간당 10개. 키 값은 기록하지 않습니다.
- **ACL**: 주소 규칙 그대로 그룹을 만들고 필요한 포트(판정 3306 · DR SSH 22 · 복제 3306 · 헬스 30080)만 엽니다.

### 1-3. 클라이언트 가입 (`vpn/join.py` · `vpn/install.sh`)

```
명단(이메일 · 이름 · 조) ─▶ 첫 등록(이름 확인 → 비밀번호 1회 설정) ─▶ 로그인 ─▶ VM 별 [가입 키] [주소 고정] [옛 노드 삭제]
                                                                               │ 키는 한 번만 화면에
VM: curl -fsSL <서버>/join/install.sh | sudo bash -s -- t5-vm2 ◀──────────────────┘
    tailscale 설치 → 키 입력(화면 · 명령 기록에 안 남음, 0600 파일) → up --hostname t5-vm2 → 서버 자동 고정을 기다림 → .52
    [--dr-node] DR 컨트롤 전용 운영 계정 (컨트롤 서버 주소에서 온 키만)   [--ufw] VPN · WireGuard 만 받기
```

- 가입 화면은 권한 없는 계정 · 127.0.0.1 · TLS 는 앞단 nginx.
- PBKDF2-SHA256 310,000회 · 없는 계정도 같은 시간 · 실패 사유 비공개 · 계정 5회 실패 잠금 · IP 당 15분 10회 실패 → 429 · 세션 8시간(HttpOnly · Secure · SameSite=Strict, 비밀번호가 바뀌면 끊김) · POST Origin 확인 · CSP · 모든 시도 기록.

### 1-4. 통합 관제 (`dashboard/` + `sites/` + `cloud/`)

나의 파트와 이어진 운영 사이트 여섯 곳을 `sites/<사이트>/` 폴더로 나눴고, 한 서버(`dashboard/server.py`)가 모두 엽니다.

| 운영 사이트 | 폴더 | 여기 주소 | 동작 |
| :--- | :--- | :--- | :--- |
| duckcloud-stat (서비스 상태) | `sites/duckcloud-stat/` | `/` | 디자인 · 화면 동작(노드 상세 창 등)만 — 실시간 값 없음 |
| dr (DR 현황) | `sites/dr/` | `/dr` (Host 가 `dr.` 으로 시작하면 `/`) | **실제로 동작** — 4초마다 `/api/drboard` (DR 컨트롤 상태 → 화면 모양, `board.py`), 시연 버튼 ①~④ |
| duckcloud-admin (관리 사이트) | `sites/duckcloud-admin/` | `/admin` → `/admin/home` · `/admin/dr/live` · `/admin/aws` · `/admin/acl` | **DR 칸 동작**(`/dr/api` — 승격 조건 하나씩, `admin.py`) · **보안 동작**(`/acl/api` — ACL 원문 주석 보존 편집 · 차이 · 경로 시험 · 적용, `acl.py`) · 나머지는 디자인 |
| duckcloud-admin-master (과금 · 콘솔, 팀 VPN 전용) | `sites/duckcloud-admin-master/` | `/master` · `/master/otp` (2단계 인증) | 디자인만 — 메뉴는 aws 화면으로 이어짐 |
| aws (AWS 대시보드) | `sites/aws/` | `/aws/` · `/aws/aws-services` · `/aws/aws-connection` | **동작** (`cloud/api.py`) — t8 두 대 상태 · 운영 형태(DR 상태를 따름) · 기능별 과금 · 전원 · TCO 견적 · 서비스 카탈로그 · 연결 구조. 과금 · 전원 제어는 **OTP(TOTP) 15분 세션** 필수, AWS 가 1조로 운영 중이면 끄기 거부 |
| hs (VPN 가입) | `sites/hs/` | `/hs/join` · `/hs/join/{claim,setpw,vm,mesh}` | 화면 스냅샷 — **실제 가입 동작은 `vpn/join.py`** (`:8096/join`, 같은 디자인) |

- 메뉴는 그대로 두되, 다른 담당 화면(업무 안내 · 1조 온프레미스 · 모니터링 · 배포 · 시뮬레이션 · 권한 · DB 구조 · 데모 소스)과 운영 서버 전용 화면(VM 터미널 · 과금 명세 · 입구 제어)은 `/<사이트>/excluded/...` → "포함하지 않음" 안내로 열립니다.
- 단계 판정(DR 현황): AWS 승격 + 1조 응답 → 복구 진행, AWS 승격 → 2차 DR, t5 가 쓰기 → 1차 DR, 복귀 30분 안 → 복구 완료, 그 밖 → 평시.
- 시연 버튼(`POST /api/drill/*`, `X-Dr-Auth`)은 시뮬레이터에서만 동작합니다. 실제 서버에서는 404 — 실제 전환은 판정 엔진만 합니다.
- 화면 아이콘 · 스타일은 운영과 같이 cdnjs(Font Awesome) · Tailwind CDN 을 씁니다. 조회만 하며, 허용 대역(`DASH_ALLOW`) 밖 요청은 403.

### 1-5. AWS Pilot Light (`cloud/`) — 데모 전용

실제 AWS 계정에는 붙지 않습니다. 화면 API 는 `cloud/demo.py`(메모리 안 가짜 리전)만 부르고, `cloud/infra/` 의 CloudFormation · 스크립트는 **구성 참고용**(이 저장소에서 실행하지 않음)입니다.

| 무엇 | 어떻게 | 어디 |
| :--- | :--- | :--- |
| 데모 리전 | vm1 · vm2 + 화면에서 추가한 VM(최대 6) · 워커 ASG · EBS · 스냅숏 · 예산. 켜기 · 끄기 · 추가 · 삭제가 상태 · 과금에 바로 반영 | `cloud/demo.py` |
| Pilot Light 칸 | 대기(vm1 끄기 · 워커 0) · 2차 DR 발동(시연 ①②) · 복귀(③④) — DR 단계를 따라만 갈 수 있음 | `cloud/api.py pl_action` |
| 인프라 (참고) | CloudFormation `network.yaml`(VPC · 공인 서브넷 · S3 Gateway Endpoint, NAT 없음) · `compute.yaml`(vm1 · vm2 t3.small · 워커 ASG 0~2 · SSM · IMDSv2) | `cloud/infra/` |
| 켜기 · 설정 | `up.sh` — 켜기 → vm1 k3s 서버 → vm2 MySQL 복제본 + 워커 → 같은 차트로 앱 배치(0개 · 읽기 전용). SSH 없이 SSM Run Command | `cloud/infra/up.sh` · `node/*.sh` |
| 과금 | 자원 → 기능(ec2 · asg · rds · alb · nat) + 고정비(IPv4 · EBS · 스냅숏). 최근 조회한 key 만 제어 (임의 ID 주입 차단) | `cloud/billing.py` |
| 운영 형태 | DR 컨트롤 상태를 읽어 '운영(1조 승격) · 대기 · 꺼짐' — 승격 중이면 전원 · 전체 절전 · 워커 0 거부 | `cloud/site.py` |
| 2단계 인증 | TOTP(RFC 6238) — 비밀은 파일(`AWS_OTP_SECRET_FILE`)에서만, 5분 5회 실패 잠금, 통과 뒤 15분 세션 쿠키 | `cloud/otp.py` |
| 연결 구조 | 부르는 API · 시점 · IAM 권한 · 실제 호출 수/실패 · 최소 권한 정책(쓰기는 태그 조건) | `cloud/connection.py` |

### 1-6. 다른 담당 저장소 (`external/`)

병원 앱 · GitOps · 모니터링은 **git submodule 링크로만** 둡니다 (코드 복사 없음). 맞닿는 곳만 이 저장소에 있습니다 — 자세히는 [`external/README.md`](external/README.md).

| 저장소 | 연결 |
| :--- | :--- |
| hospital-frontend | 공개 입구 upstream :30080 (승격 · 복귀 때 주소만 바꿈) |
| hospital-backend-auth · reservation · stats | `/readyz` 로 앱 기동 확인 (`DR_APP_PORT`, 기본값 `30080`) · DB 계정 `app_runtime` · `app_readonly` 잠금(펜싱) |
| hospital-gitops | 같은 차트 + `values-t8.yaml` · `readOnlyMode` · 스키마/계정이 복제 · 잠금 기준 |
| hospital-monitoring | duck-scorer → `POST /api/gateways` (추천 기록만) · Prometheus ← `GET /join/targets.json` |

---

## 2. 구조

```
hospital-common/   게이트웨이 · 운영 공통 (따로 실행 — 자세히는 hospital-common/README.md)
  common/            공용 계정 · 세션 · CSRF · unix socket RPC · 감사 · 서버 어댑터
  services/          hostagent (root VM 조작) · console (VPN 전용 콘솔) · entry (공개 입구 S1/S2) · hsha (Headscale 주/대기) · front (공개 첫 화면)
  deploy/ sim/ tests/
cloud/        AWS Pilot Light (데모) — demo.py (가짜 리전) · api.py (화면 API · OTP 보호) · site.py · billing.py · otp.py · connection.py · services.py · catalog.json · estimate.json
  infra/        (참고용 · 실행 안 함) CloudFormation network · compute · up · down · create · destroy · deploy · session · node/{common,vm1,vm2,app,users}.sh · site-values/
external/     다른 담당 저장소 (submodule 링크) — frontend · backend-auth/reservation/stats · gitops · monitoring
pilotlight/   DR 컨트롤
  judge.py      판정 — 타이머 · 대기 조건 · 증인 · 재시도 · 복귀 대상 · 돌아온 DB 잠금
  flows.py      실행 — prepare · follow · failover · failback · app_retry · door (Ctx 하나로 모든 의존성)
  replica.py    MySQL 8.4 GTID — 상태 해석 · 복제 준비 · 승격 · 역동기화 · 절체 · 사슬 원본 선택
  fence.py · power.py · frontdoor.py · runner.py · store.py · execs.py · config.py · cost.py
  control.py    컨트롤 API (+ /api/gateways 점수 서버 추천)      mysql_replica.sh  대기 DB 설치(서명 키 지문 확인 · 멱등)
  __main__.py   python -m pilotlight run | once | prepare | cost
vpn/          VPN 서버 · 클라이언트 가입
  agent.py      root 에이전트 — 규칙 · 동작 · 자동 고정 · unix socket 서버/클라이언트
  headscale.py  Headscale CLI · SQLite 어댑터 · tailscale 경로(직결/중계)
  join.py       가입 화면      auth.py  PBKDF2      install.sh  VM 가입 스크립트      acl.hujson  ACL 예시
dashboard/    통합 관제 — server.py (모으기 · HTTP · 경로) · board.py (/api/drboard) · admin.py (/dr/api) · acl.py (/acl/api)
sites/        운영 사이트별 화면
  duckcloud-stat/          index.html
  dr/                      index.html                              ← /api/drboard
  duckcloud-admin/         index · home · dr-live · aws · acl .html
  duckcloud-admin-master/  index · otp .html
  aws/                     index · aws-services · aws-connection .html
  hs/                      login · claim · setpw · vm · mesh .html  ← 동작은 vpn/join.py
sim/          가짜 인프라
  cluster.py    SSH 명령 · SQL 문자열을 그대로 해석하는 MySQL (GTID 집합 · relay · 1236/1045/1290 · 분할)
  headscale.py  가짜 Headscale (1회용 키 · 임의 주소 배정)
  scenario.py   시연 시나리오 ①~④ · 리셋      drill.py  모의 훈련      stack.py  네 부분을 localhost 에 모두 띄워 연결 시연
  aws.py        가짜 AWS CLI — EC2 상태가 DR 승격 · 복귀를 따라간다
tests/        test_pilotlight.py · test_stack.py · test_cloud.py
demo.py       로컬 데모 — 모든 가상 서버를 한 번에 (python3 demo.py)
```

읽는 순서 추천: `sim/stack.py` (전체가 어떻게 이어지는지) → `sim/scenario.py` → `judge.tick` → `flows.failover` → `replica.promote` → `judge.witness` → `flows.failback` → `vpn/agent.py` `fix_ip` · `watch_once` → `vpn/join.py` `post` → `dashboard/server.py` `Collector.status` → `dashboard/board.py` `build` → `sim/cluster.py` `Exec.stmt`.

---

## 3. 실행

### 3.0 로컬 데모 — 모든 가상 서버 한 번에 (Python 3.12+)

실제 서버 · AWS 계정 없이 이 컴퓨터 안에서 모든 서버를 가상으로 띄웁니다. 종료는 Ctrl-C.

```bash
python3 demo.py
```

| 가상 서버 | 주소 | 해 볼 것 |
| :--- | :--- | :--- |
| 서비스 상태 (duckcloud-stat) | http://127.0.0.1:8095/ | 흐름도 · 노드 상세 창 |
| DR 현황 (dr) | http://127.0.0.1:8095/dr | 시연 버튼 ① → ② → ③ → ④ (비밀번호는 실행할 때 출력) |
| 관리 사이트 (duckcloud-admin) | http://127.0.0.1:8095/admin | DR 칸 · 보안(ACL 편집 · 경로 시험 · 적용) |
| AWS 대시보드 (aws, 데모) | http://127.0.0.1:8095/aws/ | 아래 '데모 — 서버 구성' 칸: **VM 추가 · 켜기 · 끄기 · 삭제** (OTP 코드가 같은 칸에 보임) · Pilot Light 칸: **대기 · 2차 DR 발동 · 복귀** |
| VPN 가입 (hs) | http://127.0.0.1:8096/join | 첫 등록 → 가입 키 → (가짜) VM 가입 · 주소 고정 |
| 운영 콘솔 (hospital-common) | http://127.0.0.1:8190/ | VM 사양 변경 · 공개 입구 S1/S2 켜기 · 끄기 · 공용 계정 (계정은 실행할 때 출력) |
| 공개 첫 화면 (hospital-common) | http://127.0.0.1:8191/ | 지금 요청이 가는 곳 · 입구 상태 |

AWS 는 **데모만** 합니다 — `cloud/demo.py` 가 메모리 안에서 EC2 · ASG · 볼륨 · 예산을 흉내 내고, 실제 계정 · CLI 는 부르지 않습니다.
vm1 · vm2 는 가짜 DR 과 한 몸이라, 2차 DR 발동(②)이 vm1 을 켜고 복귀(④)가 다시 끕니다. AWS 가 운영 중이면 전원 끄기 · 전체 절전 · 워커 0 은 거부됩니다.

### 3.1 인프라 없이 — 부분별 (Python 3.10+)

자동 훈련을 약 1분에 걸쳐 진행합니다. 실행한 뒤 브라우저로 http://127.0.0.1:8095/dr 를 열어 두고 봅니다.

```bash
python3 -m sim.stack
```

자동 훈련 없이, DR 현황 화면의 시연 버튼 ① → ② → ③ → ④ 를 직접 누르려면 (비밀번호는 실행할 때 출력, 종료는 Ctrl-C):

```bash
python3 -m sim.stack --manual
```

| 화면 | 주소 |
| :--- | :--- |
| 서비스 상태 | http://127.0.0.1:8095/ |
| DR 현황 | http://127.0.0.1:8095/dr |
| 관리 사이트 | http://127.0.0.1:8095/admin |
| AWS 대시보드 | http://127.0.0.1:8095/aws/ |
| 가입 화면 | http://127.0.0.1:8096/join |
| DR 컨트롤 API | http://127.0.0.1:8090/api/state (Bearer 토큰) |

AWS 대시보드의 전원 · 과금 · VM 추가 버튼은 OTP 를 묻습니다. 데모 코드는 AWS 화면 아래 '데모 — 서버 구성' 칸에 보입니다. 승격(②) 뒤에는 AWS 가 '운영(1조 승격)' 으로 바뀌고 끄기가 거부됩니다.

판정만 바로 끝까지 · 테스트 · 비용 비교:

```bash
python3 -m sim.drill
python3 -m unittest discover -s tests -t . -v
python3 -m pilotlight cost
```

훈련 순서: [0] AWS 대기 준비 (대기 DB t5 에서 사슬 복제) → [1] 판정 서버 ↔ 1조 분할 — 증인이 승격을 막음 → ① t3 다운 → t5 (AWS 는 원본을 t5 로) → ② t5 다운 → AWS 승격 · 앱 기동 · 입구 전환 → ③ t3 복구 — 즉시 잠금 → ④ t3 주 서버 복귀 — 역동기화 · 절체 · AWS 다시 대기 → 리셋.
① 의 t3 → t5 승격과 리셋의 t5 재동기화는 1조 담당 영역이라 가짜 인프라가 대신합니다.

### 3.2 실제 환경

| 구성 요소 | 어디서 | 설정 |
| :--- | :--- | :--- |
| `python -m vpn.agent` | 컨트롤 서버, root (Headscale 같은 호스트) | `/etc/pilotlight/vpn.json` (`examples/vpn.json`) — `slots` · `login_server` · `control_ip` · `protected` · `group` · `ro_group` |
| `python -m vpn.join` | 컨트롤 서버, 권한 없는 계정 · 127.0.0.1:8096 · nginx 가 `/join` 전달 | `/etc/pilotlight/join.json` (`examples/join.json`) — `db` · `agent_sock` · `login_server` · `public_origin` · `members` · `dr_pub` |
| `python -m pilotlight run` | 컨트롤 서버 | `.env` (`DR_*` · `AWS_*` · `FRONTDOOR_*` · `DR_CONTROL_LISTEN` · `DR_CONTROL_TOKEN`) |
| `python -m dashboard.server` | 컨트롤 서버, VPN 주소에 | `DASH_LISTEN` · `DASH_ALLOW` · `DR_CONTROL_URL` · `DR_CONTROL_TOKEN` · `VPN_AGENT_RO_SOCK` |

`.env` 를 채운 뒤 AWS 대기 DB 를 1조 복제본으로 준비하고, 판정 + 컨트롤 API 를 띄웁니다:

```bash
cp .env.example .env
set -a; . ./.env; set +a
python3 -m pilotlight prepare
python3 -m pilotlight run
```

### 3.3 hospital-common

```bash
cd hospital-common
```

```bash
python3 -m sim.drill
```

```bash
python3 -m unittest discover -s tests -t .
```

```bash
python3 -m sim.stack
```

콘솔 http://127.0.0.1:8190/ · 공개 첫 화면 http://127.0.0.1:8191/ (계정 비밀번호는 실행할 때 출력). Python 3.12+.

---

## 4. 포함하지 않은 것

- **다른 담당 코드** — 병원 앱 · GitOps(CI/CD · Helm 차트) · 모니터링은 `external/` 에 링크로만 (복사 없음).
- **권한** — 팀원 IAM 역할(`iam/team-roles*`) · 다른 조용 kubeconfig · 관리 화면 권한 · 팀 역할 화면.
- **관리 사이트의 다른 담당 화면** — 업무 안내 · 1조 온프레미스 · 모니터링 · 배포 · 시뮬레이션 (메뉴는 남기고 "포함하지 않음" 안내).
- **운영 서버 전용** — VM 웹 터미널 · 과금 명세(Cost Explorer 유료 호출) · 실제 계정 · 키 · 도메인 · 공인 주소.
- **AWS 계정 연동** — 대시보드는 데모 리전만 씁니다. `cloud/infra/` 는 실제 구성을 보여 주는 참고 파일입니다.

## 5. 보안 메모

- 비밀번호 · 키는 명령줄 · 로그 · 상태 파일에 쓰지 않습니다 — 복제 계정 비밀번호는 `DR_SECRETS_PATH`(0600), DB 루트는 `auth_socket`, 가입 키는 화면에 한 번.
- 복제는 `REQUIRE SSL` · `SOURCE_SSL=1` · `caching_sha2_password`, 복제 계정은 `sql_log_bin=0` 으로 하류에 번지지 않게.
- root 는 VPN 에이전트 하나 — 받는 동작이 정해져 있고 대상은 번호로만. 웹은 모두 권한 없는 계정.
- `.env` · 키 · 상태 · DB 파일은 `.gitignore` 로 제외합니다.
- AWS 제어(전원 · 과금 · 워커)는 OTP 세션 없이는 403, AWS 가 1조로 운영 중이면 끄기 거부. 실제 AWS 계정 · 자격 증명은 쓰지 않습니다 (데모 리전). OTP 비밀은 실행마다 새로 만들고, 실제 서버라면 `AWS_OTP_SECRET_FILE`(0600)에서만 읽습니다.
- 관리 화면 POST 는 같은 출처만, ACL 적용은 `base_sha` 가 같을 때만(동시 편집 방지) · 버전마다 이전 원문 보관 · 실제 서버는 적용을 끈 채로(`acl_apply=False`) 시작.
- 점수 서버 메시지는 형식이 틀리면 400 · 같은 장애 재전송은 무시 · 추천 기록만 — 승격 결정을 바깥에 맡기지 않습니다.
