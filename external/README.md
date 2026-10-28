# external — 다른 담당자 저장소 (git submodule)

이 폴더의 저장소들은 **다른 팀원이 만든 코드**입니다. 여기에는 복사하지 않고 원본 저장소의 한 시점(commit)을 가리키는 링크만 둡니다.
받을 때: `git clone --recurse-submodules <이 저장소>` 또는 이미 받았다면 `git submodule update --init`.

| 폴더 | 담당 | 이 저장소와 맞닿는 곳 (기능 연결) |
| :--- | :--- | :--- |
| `hospital-frontend` | 앱 개발 | 병원 화면 web — NodePort **30080**. 공개 입구 upstream(`pilotlight/frontdoor.py`)이 이 포트로 보내고, 승격 · 복귀 때 주소만 바꾼다. 첫 화면(`hospital-common/services/front`)의 '병원 사이트 열기' 대상 |
| `hospital-backend-auth` | 앱 개발 | DB 계정 `app_runtime` 사용. 장애 때 옛 쓰기 DB 에서 이 계정을 잠근다(`pilotlight/fence.py`, `DR_APP_USERS`). JWT · 진료기록 키 Secret 은 AWS 에 1조 값 그대로 복사(`cloud/infra/node/app.sh`) |
| `hospital-backend-reservation` | 앱 개발 | **`/readyz`** (DB `SELECT 1`, 1초 제한) — NodePort **30800**. DR 컨트롤이 앱 기동 확인에 쓴다(`DR_APP_PORT=30800` · `DR_READY_PATH=/readyz`, `pilotlight/power.py app_up`). 30080 의 `/readyz` 는 web 화면이 200 으로 답해 쓰지 않는다 |
| `hospital-backend-stats` | 앱 개발 | DB 계정 `app_readonly` — 잠금 대상 · AWS 복제본에 같은 이름으로 만든다 |
| `hospital-gitops` | IaC · CI/CD | 차트 `helm/reservation-app` — AWS 는 같은 차트에 `cloud/infra/site-values/values-t8.yaml`(0개 · `readOnlyMode: true`)을 얹고, 승격 때 `--set readOnlyMode=false`. DB 스키마 `db/schema.sql` · 계정 `db/grants.sql` 이 복제 대상(`REPLICATE_DO_DB=hospital`) · 잠금 계정의 기준 |
| `hospital-monitoring` | 모니터링 | `duck-scorer` 가 장애 때 target 메시지를 DR 컨트롤 **`POST /api/gateways`** 로 보낸다(`pilotlight/control.py gateways`) — 형식 검사 · 중복 무시 · `{"accepted": true}`. **추천으로 기록만** 하고 승격은 판정 엔진이 자기 확인으로만 한다. 반대로 감시 대상은 VPN 가입 서버 **`GET /join/targets.json?exporter=node\|mysqld\|app`**(`vpn/join.py`, Prometheus http_sd)에서 읽는다 |

```
 hospital-frontend (web :30080) ◀── 공개 입구 upstream ── pilotlight/frontdoor.py ◀── 승격 · 복귀
 hospital-backend-* (/readyz :30800) ◀── 앱 기동 확인 ── pilotlight/power.py
 hospital-backend-* (DB 계정 app_runtime · app_readonly) ◀── 잠금 ── pilotlight/fence.py
 hospital-gitops (차트 · 스키마) ◀── values-t8.yaml · readOnlyMode ── cloud/infra/node/app.sh
 hospital-monitoring duck-scorer ── POST /api/gateways ──▶ pilotlight/control.py (추천 기록)
                                 ◀── GET /join/targets.json ── vpn/join.py
```

여기 있는 코드는 이 저장소의 테스트 · 시뮬레이터가 부르지 않습니다 (각 저장소에서 따로 실행 · 시험).
