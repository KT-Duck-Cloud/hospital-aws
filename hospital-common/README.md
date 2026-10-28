# hospital-common

DuckCloud 병원 서비스의 **게이트웨이 · 운영 공통** 부분을 작은 서비스로 나눈 저장소입니다 (교육용).
병원 앱(auth · reservation · stats · frontend)과 DR 컨트롤 · VPN 가입은 다른 저장소에 있고, 여기는 그 앞과 옆에서 **입구를 이중화하고, VPN 제어 서버를 이중화하고, 호스트 VM 을 안전하게 다루는** 부분만 담습니다.

- Python 3.12+ 표준 라이브러리만 사용 (외부 패키지 없음)
- 서비스마다 프로세스 · 계정 · 설정 · 권한이 따로 — 서로는 **unix socket 한 줄 JSON** 또는 HTTP 로만 부른다
- 가짜 인프라(`sim/`) 위에서 진짜 서비스 코드가 그대로 돈다 — 서버 없이 전체 흐름 확인

---

## 1. 서비스 구성

```
                     Cloudflare 터널
                 ┌────────┴────────┐
        S1 게이트웨이            S2 게이트웨이                  ← entry: 입구 ON/OFF · 메쉬 참여 · 서로 감시
   [mesh-gate :8088] ⇄ VPN ⇄ [mesh-gate :8088]                ← 켜진 쪽 frontdoor(:8087) 로 번갈아
   frontdoor :8087            frontdoor :8087  → 병원 앱 (upstream hospital_active — DR 컨트롤이 바꿈)
   front (첫 화면)
   Headscale (대기) ◀─ 5분 사본 ─ Headscale (메인)              ← hsha: 사본 · 가드 · 감시 · 승격 · 복귀
        │ unix socket
   console (VPN 전용) ── unix socket ──▶ hostagent (root, 1조 호스트 VM)
```

| 서비스 | 실행 계정 | 받는 곳 | 하는 일 |
| :--- | :--- | :--- | :--- |
| `services/hostagent` | root | unix socket 0660 (그룹 console) | 등록된 VM 만 켜기 · 끄기 · 사양 · 디스크. 보호 VM 거부 · 한도 · 호스트 예비 자원 확인 · 한 번에 하나 |
| `services/console` | console | VPN 대역 HTTP | 로그인 · CSRF · 감사 기록 뒤에서 hostagent · entry 를 부른다. 작업은 한 번에 하나, 한 대씩 (끄기 → 변경 → 켜기 → 응답) |
| `services/entry` | root | unix socket + 상대 서버(ssh 강제 명령) | 공개 입구 S1 · S2 켜기 · 끄기 · 메쉬 참여 · 상대 입구 감시 (2번 연속 준비 안 됨 → 상대 재시작 → 내가 켬) |
| `services/hsha` | root (entry 와 함께) | 타이머 | Headscale 사본(실행 중 sqlite .backup) · 시작 전 가드 · 두 주 서버면 보조가 멈춤 · 2분 무응답 → 최신 사본으로 승격 |
| `services/front` | front | 127.0.0.1 HTTP (nginx 뒤) | 공개 첫 화면 — 지금 요청이 가는 곳 · 입구 상태. 공개에는 주소를 숨기고, 로그인하면 보인다 |
| `common/` | — | — | 공용 계정(PBKDF2 · 잠금) · 세션 · CSRF · 허용 대역 · 보안 머리글 · unix socket RPC · 감사 · 서버 조작 어댑터 |

### 핵심 규칙

- **두 주 서버 금지** — Headscale DB 가 갈라지면 되돌릴 수 없다. 시작 전 가드(상대가 돌면 거부) + 1분 확인(둘 다 돌면 보조 S1 이 멈춤, S2 메인이 이김).
- **멈추기 전에 넘긴다** — 주 서버를 멈출 때 마지막 사본 전송이 실패하면 멈추지 않는다 (`entry.hs_stop`).
- **상대 확인은 두 길** — WireGuard · 공인 주소 모두 실패해야 '죽음'. `X-Duck-Gate` 머리글로 물어 게이트가 넘기지 않고 자기 Headscale 만 답하게 한다.
- **설정을 바꾸고 nginx -t 가 실패하면 되돌린다** (`entry.mesh`).
- **보호 VM 은 상태만** — control-plane · DB VM 은 콘솔에서 끄거나 바꿀 수 없다 (에이전트가 거부).
- **포트를 열지 않는 root** — hostagent · entry 는 unix socket 만, 허용 동작 목록 밖은 거부.

---

## 2. 구조

```
common/        auth.py (계정 · 잠금 · Throttle) · web.py (세션 · CSRF · 허용 대역 · 응답) · rpc.py · audit.py · host.py
services/
  hostagent/   agent.py (허용 동작 · 한도) · vmm.py (vmrun · systemd 유닛 · .vmx · vmdk) · __main__.py
  console/     app.py (HTTP · 동작) · jobs.py (한 번에 하나 · 단계 기록 · resize) · index.html · __main__.py
  entry/       entry.py (입구 · 메쉬 · peer · 감시 · CLI) · __main__.py (serve · watch · hs-* 타이머 진입점)
  hsha/        node.py (사본 · 복원 · 받기) · ha.py (replicate · Guard · Watch · promote · demote)
  front/       app.py (첫 화면 · 연결 · 현황) · __main__.py
deploy/
  config/      hostagent · console · entry · front .json (예시)
  nginx/       mesh-gate.conf.tmpl · hs-gate-peer.conf.tmpl · frontdoor.conf.example
  systemd/     서비스 4개 · 타이머 4개 (entry-watch 1분 · hs-watch 30초 · hs-guard 1분 · hs-push 5분) · headscale 시작 전 가드
sim/           fake.py (Host · Vmm · Net · 시계) · world.py (S1 · S2 · 1조 호스트를 socket 으로 연결) · drill.py · stack.py
tests/         test_common.py · test_services.py
```

읽는 순서 추천: `sim/world.py` → `sim/drill.py` → `services/entry/entry.py` `watch` → `services/hsha/ha.py` `Watch.tick` · `Guard` → `services/hsha/node.py` `snapshot` · `restore` → `services/console/jobs.py` `resize` → `services/hostagent/agent.py` `vm_set` → `common/auth.py` `Accounts.verify` → `common/web.py` `Sessions`.

---

## 3. 실행

### 3.1 서버 없이

```bash
python3 -m sim.drill
```

```bash
python3 -m unittest discover -s tests -t . -v
```

```bash
python3 -m sim.stack
```

`sim.stack` 은 콘솔(http://127.0.0.1:8190/)과 첫 화면(http://127.0.0.1:8191/)을 띄우고 시연 계정 비밀번호를 출력합니다.

모의 훈련 단계:

| 단계 | 상황 | 확인 |
| :--- | :--- | :--- |
| [0] | 평시 — 두 입구 ON, Headscale S2 메인 | S1 에 사본 |
| [1] | S1 입구 OFF + S2 전원 꺼짐 | 감시 2번째에 S1 입구 자동 ON |
| [2] | S2 Headscale 2분 무응답 | S1 이 최신 사본으로 승격, 가입 기록 유지, 입구 ON |
| [3] | S2 가 돌아옴 | 시작 전 가드가 거부 · 손으로 둘 다 켜면 보조 S1 이 멈춤 |
| [4] | S2 메인 복귀 | S1 이 마지막 사본을 넘긴 뒤 멈춤 → S2 복원 · 시작, S1 승격 중 가입도 유지 |
| [5] | 콘솔 | 5회 실패 잠금 · 사양 변경(끄기 → 변경 → 켜기 → 응답) · 보호 VM · 메모리 예비 부족 거부 |
| [6] | 첫 화면 | 공개는 주소 없음 · 로그인은 주소 · DR 전환을 따라 '지금 가는 곳' 변경 |

### 3.2 실제 서버

1. `/opt/hospital-common` 에 복사하고, `deploy/config/*.json` 을 `/etc/hospital-common/` 에 맞게 고칩니다.
2. 계정 `console` · `front` 를 만들고 `deploy/systemd/` 유닛을 설치합니다. hostagent 는 1조 호스트, 나머지는 각 게이트웨이(entry.json 의 `role` = S1 | S2).
3. 상대 게이트웨이의 `entry` 계정 SSH 키는 강제 명령(`command="python3 -m services.entry ..."`)으로만 허용합니다.
4. nginx 는 `deploy/nginx/` 템플릿의 `@SELF@ · @PEER@ · @PEER_IP@` 를 채워 넣습니다.

---

## 4. 넣지 않은 것

- **다른 담당 파트** — 병원 앱(auth · reservation · stats · frontend), GitOps · Helm · Argo CD(CI/CD), Kubernetes 매니페스트와 콘솔의 K8s 조작(노드 비우기 · 롤링 재시작 · HPA), 모니터링(Prometheus · Grafana · 알림 · 관측 화면 로그인 프록시), 권한 관리, AWS CloudFormation · IAM.
- **다른 저장소와 겹치는 것** — DR 판정 · 승격 · 복귀 · 입구 upstream 전환, VPN 에이전트 · 가입 화면, 통합 관제 화면(서비스 상태 · DR 현황 · 관리 사이트). 여기서는 DR 이 바꾼 upstream 을 **읽기만** 합니다.
- **운영 값** — 실제 도메인 · 공인 주소 · 계정 · 키. 주소는 `example.org` · `203.0.113.x`(문서용) · `10.10.10.x` · `100.64.0.x` 로 바꿨습니다.

## 5. 보안 메모

- 비밀번호 PBKDF2-SHA256 310,000회 · 없는 계정도 같은 시간 · 실패 사유 비공개 · 계정 5회 실패 잠금(콘솔에서만 해제) · IP 당 15분 10회 실패 → 429
- 세션: HttpOnly · Secure · SameSite(콘솔 Strict · 첫 화면 Lax) · 비밀번호가 바뀌면 끊김 · POST 는 Origin 확인 · 콘솔 동작은 X-CSRF 필수
- 콘솔은 허용 대역(VPN) 밖 403 · 모든 동작은 결과와 상관없이 감사 기록(JSONL, 덧붙이기만)
- 사본 복원은 허용 경로(`etc/headscale` · `var/lib/headscale`)만 풀고, 풀기 전에 지금 상태를 백업
