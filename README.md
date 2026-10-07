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
| 운영자 C2 관리 UI (목록/생성/활성화/삭제/kick/토큰재발급/계정/비밀번호/로그) | `relay/ssh_server.py` (C2 명령) + `relay/registry.py` `disconnect` + `relay/logs.py` |
| 관리자: 비밀번호 없이 서버 직행 (`login`, exec `attach`) | `relay/ssh_server.py` `_cmd_login` / `_attach_admin` / `_parse_exec` |
| 비관리자: 서버 id+비밀번호로 해당 셸 직행 | `relay/ssh_server.py` `validate_password` → `RelaySession._attach_direct` |
| DevOps 조회 툴 (호스트/네트워크/프로세스/전체 overview) | `relay/ssh_server.py` `info`/`net`/`procs`/`overview` → `SYSINFO` 제어 프레임 → `agent/sysinfo.py` |

---

## 저장소 구조

`relay/` 와 `agent/` 는 **각각 독립적으로 배포 가능한 폴더**입니다. 두 폴더는
서로를 import 하지 않으며, 공통 프로토콜은 각 폴더에 vendoring 되어 있습니다
(`relay/protocol.py` ≡ `agent/protocol.py`, 동일성은 테스트로 강제). 따라서
에이전트만 따로 복사/패키징해서 내부 서버에 배포할 수 있습니다.

```
relay/                      # 중계(릴레이) 서버 - 독립 배포 가능
  __main__.py               엔트리포인트 (SSH + WebSocket + 설치 웹 동시 기동)
  ssh_server.py             SSH 서버, C2 세션/CLI, add-server, 비관리자 직행, 브리지 전환
  lineeditor.py             C2 CLI 라인 에디터 (히스토리/완성/제어문자)
  ws_server.py              에이전트 WebSocket 수신 + 인증
  web.py                    설치 웹 서버 (1234): 원라이너 설치 스크립트 제공
  registry.py               에이전트 연결/세션 브리지 레지스트리 (+ kick/disconnect)
  logs.py                   최근 로그 인메모리 링버퍼 (C2 'logs' 명령)
  db.py                     SQLite (서버/운영자 자격증명, 해시 저장)
  security.py               PBKDF2 해시 / 토큰
  config.py                 YAML + 환경변수 설정
  manage.py                 DB 초기화, 사용자/서버 등록 CLI (python -m relay.manage)
  protocol.py               공통 프레이밍 (vendored 복사본)
  requirements.txt          릴레이 의존성
  setup.sh                  Ubuntu 설치 스크립트 (systemd)
agent/                      # 내부 에이전트 - 독립 배포 가능
  __main__.py               엔트리포인트
  agent.py                  WebSocket 클라이언트, 세션 멀티플렉싱, 재접속
  sysinfo.py                호스트/네트워크/프로세스 수집 (info/net/procs)
  pty_backend.py            Unix PTY / Windows ConPTY 백엔드
  protocol.py               공통 프레이밍 (vendored 복사본)
  requirements.txt          에이전트 의존성
  setup.sh                  Ubuntu 설치 스크립트 (systemd, 원라이너가 호출)
tests/                      프로토콜/보안/에디터/DB/로그/웹 단위 테스트 + E2E 테스트
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
| `SYSINFO` | 0x50 | relay→agent | `{cmd:"info"\|"net"\|"procs", args}` 제어 조회 |
| `SYSINFO_RES` | 0x51 | agent→relay | `{cmd, ok, error, data}` |

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

## Ubuntu 설치 (systemd)

### A. 릴레이 서버 (relay) — 공인/중계 서버에서 실행

```bash
git clone <repo> && cd xquare-ct-ssh
sudo ./relay/setup.sh \
    --ssh-port 2222 \
    --ws-port 8765 \
    --web-port 1234 \
    --advertise-host <relay-public-ip-or-domain> \
    --admin-user alice \
    --admin-password 'strong-password'
```

- `python3`, `python3-venv`, `python3-pip` 자동 설치
- `/opt/xquare-ct-ssh-relay` 에 venv + 애플리케이션 설치
- `/etc/xquare-ct-ssh/relay.yaml` 설정 생성 (`advertise_host` / `web` 포함)
- `/var/lib/xquare-ct-ssh/relay.db` 레지스트리 초기화 + SSH 사용자 생성
- `agent/` 를 `agent-dist.tar.gz` 로 패키징 (설치 웹이 배포)
- `xq-relay.service` 등록/기동 (`Restart=on-failure`)

주요 옵션: `--ssh-port`, `--ws-port`, `--web-host`, `--web-port`,
`--advertise-host`, `--admin-user`, `--admin-password`, `--service-user`,
`--install-dir`, `--config-dir`, `--data-dir`, `--no-service`, `--skip-deps`,
`--allow-nonroot`.

### B. 서버 추가는 C2 CLI 에서 한 줄로 (에이전트 수동 설치 불필요)

SSH 로 접속한 뒤 C2 CLI 에서 서버 id 와 로그인 비밀번호만 입력하면, 설치 웹
서버(기본 1234)가 **원라이너 curl 링크**를 만들어 줍니다.

```bash
ssh alice@<relay-host> -p 2222
```

```
C2> add-server server-001
Login password for new server server-001: ********
Server 'server-001' created.

Run this one-liner on the internal server (as root):
  curl -fsSL 'http://<relay-host>:1234/install/server-001?token=xq_...' | sudo bash
```

> **특수문자 비밀번호**: `!!`, 공백, `$`, 백틱 등이 들어간 비밀번호는 비밀번호를
> 명령에 붙여 넣지 말고 `add-server <id>` 처럼 생략한 뒤 **에코 없는 입력
> 프롬프트**(위 예시)에서 입력하세요. 그래야 셸의 이력 확장(`!!`)이나 인용
> 처리에 값이 변형되지 않습니다. 부득이 한 줄로 줄 때는 따옴표로 감싸세요:
> `add-server server-001 "p@ss !! word"`.

내부 서버에서 그 한 줄만 실행하면:

- `agent-dist.tar.gz` 다운로드 → `/opt/xquare-ct-ssh-agent` 설치
- `/etc/xquare-ct-ssh/agent.env`(600) 자격증명 기록
- `xq-agent.service` 등록 + **즉시 시작 + 부팅 시 자동 시작** (`Restart=always`)
- 에이전트가 relay 로 접속하므로 내부 서버 인바운드 포트 개방 **불필요**

오프라인/수동 설치가 필요하면 `agent/setup.sh` 를 직접 실행하거나
`python -m relay.manage add-server ...` 로 토큰+링크를 출력할 수 있습니다.

---

## 중계 서버 주소(IP) 설정

`add-server` 가 만들어 주는 원라이너와 에이전트의 `relay_url` 에는 **에이전트가
접속할 중계 서버 주소**가 들어갑니다. 우선순위는 다음과 같습니다.

1. 설정 파일 `relay.advertise_host` (권장: 공인 IP 또는 도메인)
2. `relay/setup.sh --advertise-host <host>` 또는 `relay/setup.sh` 실행 시 자동
3. 비어 있으면 **운영자가 접속한 주소**(SSH 연결의 서버측 소켓 주소)를 자동 사용
4. 환경변수 `XQ_RELAY_ADVERTISE_HOST` 로도 덮어쓸 수 있음

즉, 별도 설정이 없어도 `ssh <public-ip> ...` 로 접속했다면 링크가 그 IP 로
생성됩니다. 다만 NAT/프록시 뒤라면 `advertise_host` 를 명시하는 편이 안전합니다.

```yaml
# /etc/xquare-ct-ssh/relay.yaml
relay:
  advertise_host: relay.example.com   # 링크/에이전트에 쓸 주소
  ws_port: 8765
  ws_path: /agent
web:
  web_port: 1234
```

- 포트가 다르면 ws 포트(`--ws-port`)와 web 포트(`--web-port`)도 함께 맞춰 주세요.
- CLI 로 즉석에서 바꿀 수도 있습니다: `python -m relay --advertise-host 1.2.3.4 --web-port 1234`
- TLS(`wss://`, `https://`)는 nginx/caddy 역프록시 뒤에 두고 `advertise_host` 에
  도메인을 넣는 구성을 권장합니다.

---

## 중계 서버 중지 / 재시작

systemd 로 설치했다면:

```bash
sudo systemctl stop xq-relay          # 중지
sudo systemctl restart xq-relay       # 재시작
sudo systemctl status xq-relay        # 상태
sudo journalctl -u xq-relay -f        # 로그
```

systemd 없이 직접 띄운 경우(예: `python -m relay ... &`): 프로세스를 종료합니다.

```bash
kill "$(cat /tmp/xq-test2/relay.pid)"   # PID 파일을 저장해 둔 경우
pkill -f "python -m relay"              # 이름으로 종료
```

정상 종료(SIGTERM/SIGINT) 시 SSH/WebSocket/설치 웹 리스너를 모두 닫고 종료합니다.
에이전트도 동일하게 `systemctl stop xq-agent` (`RestartSec=5`, `Restart=always`
이므로 중지하려면 `stop` + `disable`).


---

## 빠른 시작

### 1) 레지스트리 초기화

```bash
cp config.example.yaml config.yaml
python -m relay.manage -c config.yaml init
python -m relay.manage -c config.yaml add-user alice          # SSH 로그인 계정
python -m relay.manage -c config.yaml add-server server-001   # (선택) 서버 등록
```

`add-server`는 **에이전트 토큰을 한 번만** 출력합니다. 내부 서버를 추가하는
가장 쉬운 방법은 릴레이에 SSH 로 접속한 뒤 C2 CLI 에서 `add-server` 를 쓰는
것입니다(아래 참고). `relay.manage` 는 오프라인/스크립트용입니다.

### 2) 릴레이 서버 기동

```bash
python -m relay -c config.yaml
# SSH   : 0.0.0.0:2222
# WS    : ws://0.0.0.0:8765/agent
# WEB   : http://0.0.0.0:1234   (설치 웹 서버)
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

셸의 **시작 디렉터리**는 기본적으로 **에이전트 실행 사용자의 홈 디렉터리**입니다
(기본 서비스 사용자가 `root`이면 `/root`). 바꾸려면 `--cwd /path`(또는 `XQ_CWD`,
설정 파일의 `cwd:`)를 지정하세요. 우선순위: 릴레이 `OPEN`의 `cwd` → `--cwd`/`XQ_CWD`/`cwd:`
→ 사용자 홈.

접속 셸은 **`bash`가 기본**입니다. `$SHELL`(systemd 환경에서 비어 있거나 `/bin/sh`인
경우가 많음)에 의존하지 않고 `/bin/bash` → `/usr/bin/bash` → `/usr/local/bin/bash` 순으로
찾아 사용하며, bash 가 없을 때만 `$SHELL`/`sh` 로 폴백합니다. 다른 셸을 쓰려면
`--shell <path>`(또는 `XQ_SHELL`)로 명시하세요.

### 4) 접속

접속 방식은 세 가지입니다.

**(a) 운영자(관리자)** — `relay_users` 계정으로 로그인 → C2 CLI.
관리자는 **서버 로그인 비밀번호 없이** 원하는 서버에 바로 붙을 수 있습니다:

```bash
ssh alice@relay-host -p 2222
```

```
C2> list
  SERVER           STATUS     SESS  DESCRIPTION
  server-001       online        0

C2> login server-001        # 비밀번호 불필요 (관리자)

Connected to server-001

alice@server-001:~$ clear
alice@server-001:~$ top
alice@server-001:~$ vim test.py
alice@server-001:~$ exit

[disconnected from remote shell]
C2> exit
```

**exec 원샷 접속** — 메뉴를 거치지 않고 바로 서버 셸로 진입:

```bash
ssh -t alice@relay-host -p 2222 attach server-001
# 또는
ssh -t alice@relay-host -p 2222 server-001
```

**(b) 일반 사용자(비관리자)** — **서버 id 를 SSH 사용자명으로, 서버 로그인
비밀번호를 SSH 비밀번호로** 입력하면 메뉴 없이 그 서버 셸로 바로 들어갑니다:

```bash
ssh server-001@relay-host -p 2222
# password: (서버 생성 시 정한 로그인 비밀번호)
# -> 곧바로 server-001 의 셸
```

로그아웃(`exit`)하면 연결이 종료됩니다. 해당 서버만 접근할 수 있고 C2 명령은
사용할 수 없습니다.

> 관리자 경로((a))는 릴레이 계정으로 이미 인증된 상태이므로 서버별 로그인
> 비밀번호를 묻지 않습니다. `login <server> <pw>`처럼 비밀번호를 함께 주면 그
> 값도 검증합니다(스크립트 호환용).

---

## C2 CLI 명령 (운영자)

| 명령 | 설명 |
| --- | --- |
| `list` (`servers`, `ls`) | 서버 목록 + online/offline, 활성 세션 수 |
| `sessions` (`who`) | 서버별 활성 세션 수 |
| `add-server <id> [password]` | 서버 생성 + **원라이너 설치 링크** 출력 |
| `login <server> [password]` (`connect`, `attach`) | 셸에 attach (관리자는 비밀번호 불필요) |
| `enable <server>` / `disable <server>` | 에이전트 접속 허용/차단(+연결 해제) |
| `remove-server <server>` (`delete`) | 서버와 자격증명 삭제 |
| `kick <server>` | 살아있는 에이전트 연결 강제 해제 |
| `reset-token <server>` | 에이전트 토큰 재발급 + 설치 링크 재출력 |
| `overview` (`fleet`, `all`) | **모든 서버 리소스를 한 표로** (host/IP/uptime/load/CPU/mem/disk) |
| `users` (`admins`) | 운영자 계정 목록 |
| `add-user <name> [password]` | 운영자 계정 생성/비밀번호 변경 |
| `remove-user <name>` | 운영자 계정 삭제 (마지막/본인 계정은 보호) |
| `passwd [password]` | **내 운영자 비밀번호 변경** (생략 시 echo 없이 2회 확인 입력) |
| `reset-password <name> [password]` | 다른 운영자 비밀번호 재설정 |
| `set-password <server> [password]` | **서버 로그인 비밀번호 변경** (직접 SSH 로그인/`login`에 사용) |
| `info <server>` | 호스트/CPU/메모리/스왑/디스크 요약 (에이전트에서 실시간 조회) |
| `net <server>` | 네트워크 인터페이스, 리스닝 포트, 연결 수 |
| `procs <server> [n]` | CPU 사용률 상위 n개 프로세스 (기본 15) |
| `logs [n]` | 최근 릴레이 로그 n줄 |
| `whoami` | 현재 계정 표시 |
| `ping` | CLI 응답 확인 |
| `help` (`?`) | 도움말 |
| `exit` (`quit`, `logout`) | 접속 종료 |

> `info`/`net`/`procs`는 제어 채널(`SYSINFO`/`SYSINFO_RES`)로 에이전트에 요청하며,
> 에이전트에 `psutil`이 있으면 크로스플랫폼 데이터를, 없으면 `/proc`·표준 라이브러리
> 폴백을 사용합니다. 에이전트가 offline이면 조회할 수 없습니다.

CLI는 방향키(히스토리/커서), Home/End, Delete, Tab 완성, Ctrl+A/E/U/K/W/C/L,
Backspace를 지원합니다. `login`/`add-server`/`add-user`에서 비밀번호를 생략하면
echo 없이 입력받습니다.

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
- SSH 인증은 두 종류입니다. ① 운영자: DB의 `relay_users`(PBKDF2 해시) 또는
  `authorized_keys` 공개키 → C2 CLI. ② 일반 사용자: **서버 id + 서버 로그인
  비밀번호** → 해당 서버 셸로 직행. 특정 서버만 공유하려면 그 서버를 만들고
  로그인 비밀번호만 알려주면 됩니다.
- `allow_anonymous: true`는 개발용이며 프로덕션에서는 **반드시 false**로 두세요.
  (익명 로그인은 C2 CLI 운영자 권한을 부여합니다.)
- 에이전트 토큰과 C2 로그인 비밀번호는 모두 해시로 저장되며, 평문은 생성 시 한 번만 노출됩니다.
- TLS 사용 시 `wss://` + 역프록시(nginx) 구성과 토큰 보관에 유의하세요.
- relay host key는 최초 기동 시 `host_key` 경로에 자동 생성됩니다(ed25519).

---

## 테스트

```bash
python -m pytest -q
```

- `tests/test_protocol.py` — 프레이밍/바이너리 무손실/리사이즈 (+ vendored 복사본 동일성)
- `tests/test_security.py` — 해시/토큰
- `tests/test_lineeditor.py` — 편집·히스토리·제어문자·UTF-8
- `tests/test_db.py` — 운영자 계정, 서버 enable/disable/remove, 토큰 재발급, 비밀번호 변경
- `tests/test_sysinfo.py` — 에이전트 `info`/`net`/`procs` 수집기 + 포맷 헬퍼
- `tests/test_logs.py` — 로그 링버퍼 캡처/테일
- `tests/test_web.py` — 설치 웹 서버(토큰 검증, 원라이너 생성, 패키지 배포)
- `tests/test_e2e.py` — **SSH 클라이언트 → 릴레이 → WebSocket → 에이전트 PTY → bash**
  전체 경로를 실제로 연결하여 확인:
  - 로그인 후 실제 셸 프롬프트
  - C2 관리 명령(`users`/`disable`/`enable`/`remove-server`/`logs`) 및 `add-server`
  - 비밀번호 관리(`passwd`/`reset-password`/`set-password`)
  - DevOps 조회(`info`/`net`/`procs`/`overview`)
  - 관리자 비밀번호 없는 `login` + exec `attach` 직행
  - 비관리자 **서버 id + 비밀번호 직행 접속**
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
