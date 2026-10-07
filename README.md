# xquare-ct-ssh — PTY 기반 SSH 중계 서버 + 내부 에이전트

`command → execute → response` 구조가 **아닙니다**. SSH 클라이언트의 stdin 바이트를
그대로 받아 WebSocket **binary** 스트림으로 내부 서버의 **실제 PTY**까지 흘려보내고,
반대 방향으로 셸의 stdout/stderr를 다시 SSH 클라이언트로 흘려보내는
**양방향 바이트 스트림**입니다.

```
SSH Client
    │  stdin bytes (raw, PTY)
    ▼
Relay Server  ── C2 CLI (login server-001 password) ──┐
    │  WebSocket binary frames                        │
    ▼                                                  │
Internal Agent  ── PTY (pty.fork / ConPTY) ──► 실제 Shell
    ▲                                                  │
    └────────── stdout/stderr ◄────────────────────────┘
```

로그인 이후에는 릴레이가 바이트를 **해석하지 않고 그대로 전달**하기 때문에
`clear`, `top`, `htop`, `vim`, `nano`, `python`, `bash`, `sh`, `ssh` 같은
대화형 프로그램이 실제 터미널과 동일하게 동작합니다.

---

## 요구사항 대응표

| 요구사항 | 구현 위치 |
| --- | --- |
| 키보드 입력 실시간 전달 | `relay/ssh_server.py` `data_received` → `relay/registry.py` `SessionBridge.send_input` |
| stdout/stderr 실시간 전달 | `agent/pty_backend.py` (PTY master 읽기) → `agent/agent.py` `_pump` |
| Enter / Backspace / Tab | 셸은 실제 PTY에서 처리, C2 CLI는 `relay/lineeditor.py` |
| 방향키 / Home / End / Delete | `relay/lineeditor.py` CSI/SS3 파서 |
| Ctrl+C / Ctrl+D / Ctrl+L | `relay/lineeditor.py` + 브리지 모드에서 그대로 전달 (`\x03` 등) |
| 터미널 resize | `asyncssh` `terminal_size_changed` → `RESIZE` 프레임 → `TIOCSWINSZ` |
| ANSI escape 그대로 | DATA 프레임은 raw bytes, 이스케이프 미해석 |
| 컬러 출력 | `TERM`/`COLORTERM` 환경변수 전달, 바이트 무손실 |
| 줄바꿈/커서 이동 | raw 스트림 |
| 긴 출력 실시간 스트리밍 | 비동기 큐 + `pause_writing`/`resume_writing` 기반 역압(backpressure) |
| interactive shell | `pty.fork()` (POSIX) / ConPTY (Windows) |
| Windows (`cls`, `powershell`, `cmd`) | `agent/pty_backend.py` `WindowsPty` (pywinpty/ConPTY) |

---

## 저장소 구조

```
common/protocol.py      WebSocket 바이너리 프레이밍 (type + session_id + payload)
relay/
  __main__.py           엔트리포인트 (SSH + WebSocket 동시 기동)
  ssh_server.py         SSH 서버, C2 세션, CLI→브리지 전환
  lineeditor.py         C2 CLI 라인 에디터 (히스토리/완성/제어문자)
  ws_server.py          에이전트 WebSocket 수신 + 인증
  registry.py           에이전트 연결/세션 브리지 레지스트리
  db.py                 SQLite (서버/운영자 자격증명, 해시 저장)
  security.py           PBKDF2 해시 / 토큰
  config.py             YAML + 환경변수 설정
agent/
  __main__.py           엔트리포인트
  agent.py              WebSocket 클라이언트, 세션 멀티플렉싱, 재접속
  pty_backend.py        Unix PTY / Windows ConPTY 백엔드
tools/manage.py         DB 초기화, 사용자/서버 등록 CLI
tests/                  프로토콜/보안/에디터 단위 테스트 + E2E 테스트
```

---

## 프로토콜

에이전트와 릴레이는 **하나의 WebSocket(binary)** 을 유지하고, 그 위에 여러 운영자의
셸 세션을 멀티플렉싱합니다. 모든 프레임은 5바이트 헤더로 시작합니다.

```
+--------+------------------+------------------+
| type   | session id (u32) | payload          |
| 1 byte | big-endian       | variable         |
+--------+------------------+------------------+
```

| type | 값 | 방향 | 의미 |
| --- | --- | --- | --- |
| `AUTH` | 0x01 | agent→relay | `{id, token, version, hostname, os}` |
| `AUTH_OK` / `AUTH_FAIL` | 0x02/0x03 | relay→agent | 인증 결과 |
| `OPEN` | 0x10 | relay→agent | `{shell, cols, rows, env}` 세션 생성 |
| `OPENED` | 0x11 | agent→relay | `{ok, error}` |
| `DATA` | 0x20 | 양방향 | raw 터미널 바이트 |
| `RESIZE` | 0x21 | 양방향 | `>HH` cols, rows |
| `CLOSE` | 0x22 | 양방향 | 세션 종료 (payload = 마지막 바이트) |
| `PING` / `PONG` | 0x30/0x31 | 양방향 | keepalive |
| `EXIT` | 0x40 | 양방향 | 연결 종료 |

`DATA`는 절대 문자열 명령어로 해석되지 않습니다. 즉 `vim`이 보내는 제어 시퀀스,
`top`이 그리는 화면, UTF-8 부분 바이트까지 그대로 전달됩니다.

---

## 설치

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Windows 내부 서버에서 에이전트를 돌릴 경우:

```powershell
pip install pywinpty
```

---

## 빠른 시작

### 1) 레지스트리 초기화

```bash
cp config.example.yaml config.yaml
python -m tools.manage -c config.yaml init
python -m tools.manage -c config.yaml add-user alice            # SSH 로그인 계정
python -m tools.manage -c config.yaml add-server server-001     # 내부 서버 등록
```

`add-server`는 **에이전트 토큰을 한 번만** 출력합니다. 내부 서버에 안전하게 보관하세요.

### 2) 릴레이 서버 기동

```bash
python -m relay -c config.yaml
# SSH   : 0.0.0.0:2222
# WS    : ws://0.0.0.0:8765/agent
```

### 3) 내부 서버에 에이전트 배치/기동

에이전트는 **밖으로(relay로) 접속**하므로 내부 서버에 인바운드 포트를 열 필요가 없습니다.

```bash
python -m agent \
  --relay ws://relay-host:8765/agent \
  --id server-001 \
  --token xq_xxxxxxxxxxxxxxxxxxxx \
  --shell /bin/bash
```

Windows:

```powershell
python -m agent --relay ws://relay-host:8765/agent --id win-001 --token xq_... --shell cmd.exe
```

### 4) 접속

```bash
ssh alice@relay-host -p 2222
```

```
C2> list
  SERVER           STATUS    DESCRIPTION
  server-001       online
server-001   online

C2> login server-001
Password for server-001: ********

Connected to server-001

alice@server-001:~$ clear
alice@server-001:~$ top
alice@server-001:~$ vim test.py
alice@server-001:~$ exit

[disconnected from remote shell]
C2> exit
```

---

## C2 CLI 명령

| 명령 | 설명 |
| --- | --- |
| `list` (`servers`, `ls`) | 등록된 내부 서버와 online/offline 상태 |
| `login <server> [password]` | 인증 후 해당 서버의 실제 셸에 attach |
| `ping` | CLI 응답 확인 |
| `help` (`?`) | 도움말 |
| `exit` (`quit`, `logout`) | 접속 종료 |

CLI는 방향키(히스토리/커서), Home/End, Delete, Tab 완성, Ctrl+A/E/U/K/W/C/L,
Backspace를 지원합니다. `login`만 입력하면 비밀번호를 echo 없이 입력받습니다.

---

## 터미널 resize

SSH 클라이언트의 창 크기가 바뀌면 `asyncssh`가 세션에
`terminal_size_changed(width, height, ...)`를 호출하고, 릴레이는 즉시 내부 서버로
`RESIZE` 프레임을 보냅니다. 에이전트는 `TIOCSWINSZ`(Windows는 `setwinsize`)로
셸의 PTY 크기를 갱신하므로 `vim`/`top`의 레이아웃도 즉시 따라갑니다.

E2E 테스트에서 `stty size`로 이를 검증합니다 (`30 100` → resize → `40 120`).

---

## 보안 / 운영 주의

- 이 도구는 **본인이 소유·관리 권한을 가진 서버**에만 사용하세요. 무단 접근은 불법입니다.
- 운영자 SSH 인증: DB의 `relay_users`(PBKDF2 해시) 또는 `authorized_keys` 공개키.
  `allow_anonymous: true`는 개발용이며 프로덕션에서는 **반드시 false**로 두세요.
- 에이전트 토큰과 C2 로그인 비밀번호는 모두 해시로 저장되며, 평문은 생성 시 한 번만 노출됩니다.
- TLS 사용 시 `wss://` + 역프록시(nginx) 구성과 토큰 보관에 유의하세요.
- relay host key는 최초 기동 시 `host_key` 경로에 자동 생성됩니다(ed25519).

---

## 테스트

```bash
python -m pytest -q
```

- `tests/test_protocol.py` — 프레이밍/바이너리 무손실/리사이즈
- `tests/test_security.py` — 해시/토큰
- `tests/test_lineeditor.py` — 편집·히스토리·제어문자·UTF-8
- `tests/test_e2e.py` — **SSH 클라이언트 → 릴레이 → WebSocket → 에이전트 PTY → bash**
  전체 경로를 실제로 연결하여 확인:
  - 로그인 후 실제 셸 프롬프트
  - ANSI 컬러 이스케이프 통과
  - `stty size` 로 resize 전파 검증
  - Ctrl+C 로 `sleep` 중단
  - 긴 출력(`seq 1 5000`) 실시간 스트리밍
  - `exit` 시 셸 종료 후 C2 CLI 복귀

---

## Linux PTY 관련 참고

POSIX에서는 `pty.fork()`(forkpty)를 사용합니다. Python 3.14부터
런타임에 스레드가 여러 개인 프로세스에서 forkpty 사용 시 경고가 출력될 수 있는데,
에이전트는 단일 스레드 asyncio 프로세스로 실행하는 것을 권장합니다.
