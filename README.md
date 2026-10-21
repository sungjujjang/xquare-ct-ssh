# xquare-ct-ssh

PTY 기반 SSH 중계 서버와 내부 에이전트입니다.

일반적인 원격 실행 도구처럼 `command → execute → response` 방식으로 동작하지 않습니다.  
SSH 세션의 입력과 출력을 바이트 스트림 그대로 전달해, 중계 서버를 거쳐 내부 서버의 실제 PTY에 연결합니다.

덕분에 `bash`, `vim`, `top`, `htop`, `nano`, `python`, `ssh` 같은 대화형 프로그램도 일반 SSH처럼 사용할 수 있습니다.

## 어떻게 동작하나요?

```text
SSH Client
    │
    │ stdin / terminal bytes
    ▼
┌──────────────────┐
│   Relay Server   │
│ SSH + WebSocket  │
└────────┬─────────┘
         │ binary frames
         ▼
┌──────────────────┐
│  Internal Agent  │
│   PTY / ConPTY   │
└────────┬─────────┘
         │
         ▼
      Shell
```

에이전트는 내부 서버에서 릴레이 서버로 먼저 연결합니다. 따라서 내부 서버에 SSH 포트를 외부로 열 필요가 없습니다.

로그인 이후에는 릴레이가 터미널 내용을 해석하지 않고 그대로 전달합니다. ANSI escape sequence, 컬러 출력, 커서 이동, UTF-8 데이터도 동일한 방식으로 통과합니다.

---

## 주요 기능

- SSH를 통한 내부 서버 접속
- WebSocket 기반 양방향 터미널 스트리밍
- Linux PTY 및 Windows ConPTY 지원
- 터미널 창 크기 실시간 동기화
- 여러 서버와 여러 세션 관리
- 관리자용 C2 CLI
- 서버별 로그인 계정 및 비밀번호 관리
- 에이전트 토큰 발급 및 재발급
- 내부 서버 설치용 원라이너 제공
- 호스트 / 네트워크 / 프로세스 정보 조회
- 긴 출력에 대한 backpressure 처리
- systemd 기반 서비스 설치
- 프로토콜 / 보안 / E2E 테스트

### 요구사항과 구현 위치

| 기능 | 구현 |
| --- | --- |
| 키보드 입력 전달 | `relay/ssh_server.py` → `relay/registry.py` |
| stdout / stderr 전달 | `agent/pty_backend.py` → `agent/agent.py` |
| C2 CLI 입력 처리 | `relay/lineeditor.py` |
| 방향키 / Home / End / Delete | `relay/lineeditor.py` |
| Ctrl+C / Ctrl+D / Ctrl+L | `relay/lineeditor.py` 및 브리지 모드 |
| 터미널 resize | `asyncssh` → `RESIZE` → `TIOCSWINSZ` |
| ANSI / 컬러 출력 | raw byte stream + `TERM` / `COLORTERM` |
| 긴 출력 처리 | 비동기 큐 + `pause_writing` / `resume_writing` |
| Linux PTY | `pty.fork()` |
| Windows PTY | pywinpty / ConPTY |
| 관리자 C2 | `relay/ssh_server.py` |
| 세션 관리 | `relay/registry.py` |
| 로그 | `relay/logs.py` |
| 서버 정보 조회 | `relay/ssh_server.py` + `agent/sysinfo.py` |

---

## 프로젝트 구조

`relay/`와 `agent/`는 서로 독립적으로 배포할 수 있습니다.

두 폴더는 서로 import하지 않고, 공통 프로토콜은 각각 `protocol.py`로 가지고 있습니다. 두 파일의 동일성은 테스트로 확인합니다.

```text
relay/
├── __main__.py          # SSH / WebSocket / 설치 웹 서버 실행
├── ssh_server.py        # SSH 서버, C2 CLI, 서버 접속
├── lineeditor.py        # C2 CLI 라인 에디터
├── ws_server.py         # 에이전트 WebSocket 수신 및 인증
├── web.py               # 에이전트 설치 웹 서버
├── registry.py          # 에이전트 연결 및 세션 관리
├── logs.py              # 최근 로그 링버퍼
├── db.py                # SQLite 및 자격증명 관리
├── security.py          # PBKDF2 해시 / 토큰
├── config.py            # YAML + 환경변수 설정
├── manage.py            # DB 초기화 및 관리 CLI
├── protocol.py          # 공통 프로토콜
├── requirements.txt
└── setup.sh

agent/
├── __main__.py
├── agent.py             # WebSocket 클라이언트 / 세션 멀티플렉싱
├── sysinfo.py           # 호스트 / 네트워크 / 프로세스 정보
├── pty_backend.py       # Unix PTY / Windows ConPTY
├── protocol.py          # 공통 프로토콜
├── requirements.txt
└── setup.sh

tests/
└── 프로토콜 / 보안 / CLI / DB / 로그 / 웹 / E2E 테스트
```

---

## 프로토콜

릴레이와 에이전트는 하나의 WebSocket 연결을 유지하면서 여러 셸 세션을 멀티플렉싱합니다.

모든 프레임은 다음과 같은 5바이트 헤더로 시작합니다.

```text
┌──────┬──────────────┬───────────────┐
│ type │ session id   │ payload       │
│ 1 B  │ u32, big-end │ variable      │
└──────┴──────────────┴───────────────┘
```

| Type | 값 | 방향 | 용도 |
| --- | --- | --- | --- |
| `AUTH` | `0x01` | agent → relay | 에이전트 인증 |
| `AUTH_OK` / `AUTH_FAIL` | `0x02` / `0x03` | relay → agent | 인증 결과 |
| `OPEN` | `0x10` | relay → agent | 셸 세션 생성 |
| `OPENED` | `0x11` | agent → relay | 세션 생성 결과 |
| `DATA` | `0x20` | 양방향 | raw 터미널 데이터 |
| `RESIZE` | `0x21` | 양방향 | 터미널 크기 변경 |
| `CLOSE` | `0x22` | 양방향 | 세션 종료 |
| `PING` / `PONG` | `0x30` / `0x31` | 양방향 | keepalive |
| `EXIT` | `0x40` | 양방향 | 연결 종료 |
| `SYSINFO` | `0x50` | relay → agent | 시스템 정보 요청 |
| `SYSINFO_RES` | `0x51` | agent → relay | 시스템 정보 응답 |

`DATA` 프레임은 문자열 명령어로 해석하지 않습니다. `vim`의 제어 시퀀스나 `top`의 화면 출력처럼 터미널에서 발생하는 데이터를 그대로 전달합니다.

---

## 설치

### 기본 설치

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Windows에서 에이전트를 실행한다면:

```powershell
pip install pywinpty
```

---

## Ubuntu에서 systemd로 설치하기

### 1. Relay Server

공인 서버 또는 중계 서버에서 실행합니다.

```bash
git clone <repo>
cd xquare-ct-ssh

sudo ./relay/setup.sh \
    --ssh-port 2222 \
    --ws-port 8765 \
    --web-port 1234 \
    --advertise-host <relay-public-ip-or-domain> \
    --admin-user alice \
    --admin-password 'strong-password'
```

설치 스크립트가 처리하는 작업:

- Python 및 필요한 패키지 설치
- `/opt/xquare-ct-ssh-relay`에 애플리케이션 설치
- `/etc/xquare-ct-ssh/relay.yaml` 생성
- `/var/lib/xquare-ct-ssh/relay.db` 초기화
- `agent/`를 설치용 `agent-dist.tar.gz`로 패키징
- `xq-relay.service` 등록 및 실행

주요 옵션:

```text
--ssh-port
--ws-port
--web-host
--web-port
--advertise-host
--admin-user
--admin-password
--service-user
--install-dir
--config-dir
--data-dir
--no-service
--skip-deps
--allow-nonroot
```

### 2. 내부 서버 추가

Relay에 SSH로 접속한 뒤 C2 CLI에서 서버를 등록합니다.

```bash
ssh alice@<relay-host> -p 2222
```

```text
C2> add-server server-001
Login password for new server server-001: ********
Server 'server-001' created.

Run this one-liner on the internal server (as root):
  curl -fsSL 'http://<relay-host>:1234/install/server-001?token=xq_...' | sudo bash
```

내부 서버에서는 출력된 한 줄만 실행하면 됩니다.

```text
agent-dist.tar.gz 다운로드
        ↓
/opt/xquare-ct-ssh-agent 설치
        ↓
/etc/xquare-ct-ssh/agent.env 저장
        ↓
xq-agent.service 등록
        ↓
에이전트 시작
```

에이전트가 Relay로 먼저 연결하기 때문에 내부 서버의 인바운드 포트를 열 필요가 없습니다.

비밀번호에 `!!`, 공백, `$`, 백틱 등이 포함되어 있다면 명령어에 직접 넣지 않는 것을 권장합니다.

```text
C2> add-server server-001
Login password for new server server-001: ********
```

이 방식은 셸의 history expansion이나 quoting 때문에 비밀번호가 변형되는 문제를 피할 수 있습니다.

수동 설치가 필요한 경우에는 `agent/setup.sh`를 직접 실행하거나 다음 명령으로 토큰과 설치 링크를 확인할 수 있습니다.

```bash
python -m relay.manage add-server ...
```

---

## Relay 주소 설정

에이전트가 접속할 Relay 주소는 다음 순서로 결정됩니다.

1. `relay.advertise_host`
2. `relay/setup.sh --advertise-host`
3. SSH 접속에 사용한 주소
4. `XQ_RELAY_ADVERTISE_HOST`

예를 들어:

```yaml
relay:
  advertise_host: relay.example.com
  ws_port: 8765
  ws_path: /agent

web:
  web_port: 1234
```

NAT나 프록시 뒤에서 운영한다면 `advertise_host`를 직접 지정하는 편이 안전합니다.

포트를 변경했다면 `--ws-port`와 `--web-port`도 함께 맞춰야 합니다.

TLS를 사용할 경우에는 nginx나 Caddy 같은 reverse proxy 뒤에 두고 `wss://`, `https://`를 사용하는 구성을 권장합니다.

---

## 빠르게 실행하기

### 1. 설정과 DB 초기화

```bash
cp config.example.yaml config.yaml

python -m relay.manage -c config.yaml init
python -m relay.manage -c config.yaml add-user alice
python -m relay.manage -c config.yaml add-server server-001
```

`add-server`에서 생성된 에이전트 토큰은 한 번만 출력됩니다.

### 2. Relay 실행

```bash
python -m relay -c config.yaml
```

기본 포트:

```text
SSH : 0.0.0.0:2222
WS  : ws://0.0.0.0:8765/agent
WEB : http://0.0.0.0:1234
```

### 3. 내부 서버에 Agent 실행

Linux:

```bash
python -m agent \
  --relay ws://relay-host:8765/agent \
  --id server-001 \
  --token xq_xxxxxxxxxxxxxxxxxxxx \
  --shell /bin/bash
```

Windows:

```powershell
python -m agent `
  --relay ws://relay-host:8765/agent `
  --id win-001 `
  --token xq_... `
  --shell cmd.exe
```

셸의 시작 디렉터리는 기본적으로 에이전트를 실행한 사용자의 홈 디렉터리입니다.

변경하려면:

```bash
python -m agent --cwd /path
```

또는 `XQ_CWD`, 설정 파일의 `cwd`를 사용할 수 있습니다.

우선순위는 다음과 같습니다.

```text
Relay OPEN의 cwd
    ↓
--cwd / XQ_CWD / cwd
    ↓
사용자 홈 디렉터리
```

Linux에서는 기본 셸로 bash를 우선 사용합니다.

```text
/bin/bash
/usr/bin/bash
/usr/local/bin/bash
    ↓
$SHELL
    ↓
sh
```

다른 셸을 사용하려면 `--shell` 또는 `XQ_SHELL`을 지정하면 됩니다.

---

## 접속 방법

### 관리자

관리자는 Relay의 운영자 계정으로 로그인합니다.

```bash
ssh alice@relay-host -p 2222
```

접속 후 C2 CLI에서 서버를 선택합니다.

```text
C2> list

SERVER       STATUS     SESS
server-001   online       0

C2> login server-001

Connected to server-001

alice@server-001:~$ clear
alice@server-001:~$ top
alice@server-001:~$ vim test.py
alice@server-001:~$ exit

[disconnected from remote shell]

C2> exit
```

관리자는 서버별 로그인 비밀번호 없이 접속할 수 있습니다.

### 바로 서버에 연결하기

C2 메뉴를 거치지 않고 바로 연결할 수도 있습니다.

```bash
ssh -t alice@relay-host -p 2222 attach server-001
```

또는:

```bash
ssh -t alice@relay-host -p 2222 server-001
```

### 일반 사용자

일반 사용자는 서버 ID를 SSH 사용자명으로 사용합니다.

```bash
ssh server-001@relay-host -p 2222
```

비밀번호는 서버를 생성할 때 지정한 서버 로그인 비밀번호입니다.

인증되면 해당 서버의 셸로 바로 연결됩니다.

```text
server-001@relay-host's password:

server-001:~$
```

일반 사용자는 해당 서버만 접근할 수 있으며 C2 명령은 사용할 수 없습니다.

---

## C2 CLI

| 명령 | 설명 |
| --- | --- |
| `list` / `servers` / `ls` | 서버 목록과 상태, 활성 세션 확인 |
| `sessions` / `who` | 서버별 활성 세션 확인 |
| `add-server <id> [password]` | 서버 생성 및 설치 링크 생성 |
| `login <server> [password]` | 서버 셸에 연결 |
| `enable <server>` | 서버 접속 허용 |
| `disable <server>` | 서버 접속 차단 및 연결 해제 |
| `remove-server <server>` | 서버 및 자격증명 삭제 |
| `kick <server>` | 에이전트 연결 강제 종료 |
| `reset-token <server>` | 에이전트 토큰 재발급 |
| `overview` / `fleet` / `all` | 모든 서버의 리소스 정보 |
| `users` / `admins` | 운영자 계정 목록 |
| `add-user <name> [password]` | 운영자 계정 생성 또는 변경 |
| `remove-user <name>` | 운영자 계정 삭제 |
| `passwd [password]` | 현재 운영자 비밀번호 변경 |
| `reset-password <name> [password]` | 다른 운영자 비밀번호 변경 |
| `set-password <server> [password]` | 서버 로그인 비밀번호 변경 |
| `info <server>` | 호스트 / CPU / 메모리 / 디스크 정보 |
| `net <server>` | 네트워크 인터페이스 / 포트 / 연결 정보 |
| `procs <server> [n]` | CPU 사용률 상위 프로세스 |
| `logs [n]` | 최근 Relay 로그 |
| `whoami` | 현재 계정 확인 |
| `ping` | CLI 응답 확인 |
| `help` / `?` | 도움말 |
| `exit` / `quit` / `logout` | 접속 종료 |

`info`, `net`, `procs`는 `SYSINFO` 제어 프레임을 통해 에이전트에 요청합니다.

에이전트에 `psutil`이 있으면 이를 사용하고, 없는 경우 `/proc`과 표준 라이브러리를 이용해 정보를 수집합니다.

C2 CLI는 다음 입력도 지원합니다.

```text
방향키      히스토리 / 커서 이동
Home / End
Delete
Tab         자동 완성
Ctrl+A/E/U/K/W
Ctrl+C/L
Backspace
```

비밀번호를 생략한 명령에서는 화면에 입력 내용이 표시되지 않습니다.

---

## 터미널 resize

SSH 클라이언트의 창 크기가 변경되면 `asyncssh`의 `terminal_size_changed()`가 호출됩니다.

Relay는 변경된 크기를 `RESIZE` 프레임으로 전달하고, Agent는 PTY 크기를 갱신합니다.

```text
SSH Client
    │
    │ terminal_size_changed
    ▼
Relay
    │
    │ RESIZE
    ▼
Agent
    │
    │ TIOCSWINSZ / setwinsize
    ▼
PTY
```

따라서 `vim`, `top` 같은 프로그램의 화면도 터미널 크기에 맞춰 즉시 변경됩니다.

E2E 테스트에서는 다음과 같이 resize 전파를 확인합니다.

```text
stty size
30 100

resize

40 120
```

---

## Relay 중지 / 재시작

systemd로 설치했다면:

```bash
sudo systemctl stop xq-relay
sudo systemctl restart xq-relay
sudo systemctl status xq-relay
sudo journalctl -u xq-relay -f
```

직접 실행했다면 해당 프로세스를 종료하면 됩니다.

```bash
kill "$(cat /tmp/xq-test2/relay.pid)"
```

또는:

```bash
pkill -f "python -m relay"
```

정상 종료 시 SSH, WebSocket, 설치 웹 서버의 리스너를 함께 정리합니다.

에이전트는 `Restart=always`로 설정되어 있으므로 서비스를 완전히 중지하려면:

```bash
sudo systemctl stop xq-agent
sudo systemctl disable xq-agent
```

---

## 보안

이 프로젝트는 본인이 소유하거나 관리 권한을 가진 서버에서만 사용해야 합니다.

인증 방식은 두 가지입니다.

```text
관리자
Relay 운영자 계정
    │
    └── C2 CLI → 원하는 서버에 attach

일반 사용자
서버 ID + 서버 로그인 비밀번호
    │
    └── 해당 서버 셸로 직접 연결
```

추가로 다음 사항을 확인해야 합니다.

- `allow_anonymous: true`는 개발 환경에서만 사용
- 프로덕션에서는 `allow_anonymous: false` 권장
- 에이전트 토큰과 C2 비밀번호는 해시 형태로 저장
- 평문 자격증명은 생성 시 한 번만 출력
- TLS 환경에서는 `wss://`와 reverse proxy 사용 권장
- Relay host key는 최초 실행 시 Ed25519 키로 자동 생성

`allow_anonymous: true`는 C2 CLI 운영자 권한과 연결되므로 운영 환경에서 활성화하지 않는 것을 권장합니다.

---

## 테스트

전체 테스트:

```bash
python -m pytest -q
```

테스트 범위:

```text
tests/test_protocol.py
  프레이밍 / 바이너리 무손실 / resize / protocol 동일성

tests/test_security.py
  해시 / 토큰

tests/test_lineeditor.py
  편집 / 히스토리 / 제어문자 / UTF-8

tests/test_db.py
  운영자 계정 / 서버 관리 / 토큰 재발급 / 비밀번호 변경

tests/test_sysinfo.py
  info / net / procs 수집기

tests/test_logs.py
  로그 링버퍼

tests/test_web.py
  설치 웹 서버 / 토큰 검증 / 원라이너 / 패키지 배포

tests/test_e2e.py
  SSH → Relay → WebSocket → Agent PTY → bash 전체 경로
```

E2E 테스트에서는 실제 연결을 통해 다음 항목을 확인합니다.

- 로그인 후 실제 셸 프롬프트
- C2 관리 명령
- 비밀번호 관리
- 서버 리소스 조회
- 관리자 `login` 및 exec `attach`
- 비관리자 서버 직접 접속
- ANSI 컬러 출력
- 터미널 resize
- Ctrl+C를 이용한 프로세스 종료
- 긴 출력 스트리밍
- 셸 종료 후 C2 CLI 복귀

---

## Linux PTY 참고

Linux / POSIX 환경에서는 `pty.fork()`를 사용합니다.

Python 3.14부터 여러 스레드가 실행 중인 프로세스에서 `forkpty`를 사용할 경우 경고가 출력될 수 있습니다. 현재 에이전트는 단일 스레드 asyncio 프로세스로 실행하는 것을 권장합니다.
