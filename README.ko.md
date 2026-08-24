# AgentBoard Collectors

[English](./README.md) | [简体中文](./README.zh-CN.md) | [繁體中文](./README.zh-TW.md) | [日本語](./README.ja.md) | **한국어** | [Português (BR)](./README.pt-BR.md)

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](./LICENSE) ![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg) ![dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)

AI 코딩 사용량을 위한 오픈소스 로컬 컬렉터.

[AgentBoard](https://agentboard.cc)(AI 코딩 사용량 리더보드)를 구동하는 오픈소스 컬렉터입니다 — [실시간으로 보기 →](https://agentboard.cc/leaderboard)

AgentBoard 컬렉터는 AI 코딩 도구가 이미 사용자의 컴퓨터에 보관하고 있는 로그를 스캔하고, 로컬에서 사용량 통계로 집계한 뒤 **메타데이터만** [AgentBoard](https://agentboard.cc)에 동기화합니다. 프롬프트, 모델 응답, 소스 코드, diff, 파일 내용, 파일 경로, 터미널 출력은 절대 업로드하지 않습니다.

로컬 로그에 접근하는 모든 코드는 이 저장소에 있으며, **외부 의존성 없는** 순수 Python으로 작성되어 있습니다. 무엇을 스캔하고 무엇을 전송하는지 한 줄씩 직접 확인할 수 있습니다.

## 지원 소스

| 소스 | 상태 | 스캔하는 로컬 데이터 |
| --- | --- | --- |
| Claude Code | ✅ 지원 | `~/.claude/projects`, `$CLAUDE_CONFIG_DIR/projects` |
| ↳ Claude Cowork | ✅ 지원 | `~/Library/Application Support/Claude/local-agent-mode-sessions` |
| Codex | ✅ 지원 | `~/.codex/sessions`, `~/.codex/archived_sessions`, `$CODEX_HOME`, `%APPDATA%/codex` |
| Gemini | ✅ 지원 | `~/.gemini/tmp`, `$GEMINI_CLI_HOME/tmp` |
| Kimi Code | ✅ 지원 | `~/.kimi-code/sessions`, `$KIMI_CODE_DIR`, `$KIMI_CODE_HOME` |
| OpenCode | ✅ 지원 | `~/.local/share/opencode`, `$OPENCODE_HOME`, `$OPENCODE_DB` |
| OpenClaw | ✅ 지원 | `~/.openclaw`, `$OPENCLAW_HOME`, `$OPENCLAW_DIR` |

**소스 매핑 방식.** 각 컬렉터는 특정 클라이언트가 아니라 도구의 로컬 세션 저장소(위 경로)를 읽습니다 — 따라서 CLI뿐 아니라 세션을 이 경로에 기록하는 클라이언트는 모두 수집됩니다. AgentBoard 리더보드에서는 6개의 배지로 집계됩니다: **Claude**(Claude Code + Cowork), **Codex**, **Gemini**, **Kimi Code**, **OpenCode**, **OpenClaw**.

소스별 상세: [docs/supported-sources.md](./docs/supported-sources.md)

## 업로드되는 것

AgentBoard는 **집계된 사용량 메타데이터만** 업로드합니다:

- 날짜, 접두사가 붙은 세션 id(예: `claude:<id>`), 소스 이름
- 디바이스 이름(정제된 호스트명, `$AGENTBOARD_DEVICE_NAME`으로 재정의 가능)과 플랫폼
- 활성 코딩 시간, 활성 시간 윈도우(타임스탬프만)
- 토큰 수: input, output, 캐시 읽기, 캐시 생성, 추론, 프로바이더 합계
- 메시지 수, 도구 호출 수, 상위 도구의 **이름**과 횟수
- 추가/삭제된 줄 수(편집 도구 기준 카운트)
- 작업한 프로젝트 수와 파일 수 — **개수만** 전송하며, 이름이나 경로는 절대 전송하지 않습니다
- 스킬 사용 현황(Claude Code): 공개 `SKILL.md` 메타데이터의 스킬 이름/키와 사용 횟수
- 컬렉터 버전

## 컴퓨터 밖으로 나가지 않는 것

- ❌ 프롬프트와 어시스턴트 응답
- ❌ 소스 코드, diff, 파일 내용
- ❌ 파일 경로와 프로젝트 경로(개수만 업로드)
- ❌ 터미널 출력과 환경 변수
- ❌ 사용자 이름, 이메일, 원본 머신 식별자

필드별 전체 레퍼런스: [docs/data-fields.md](./docs/data-fields.md) · 개인정보 정책: [PRIVACY.md](./PRIVACY.md)

## 설치

macOS / Linux:

```bash
curl -sL https://agentboard.cc/install | bash
```

Windows (PowerShell):

```powershell
irm https://agentboard.cc/install.ps1 | iex
```

설치 프로그램은 이 컬렉터들을 `~/.agentboard/`에 복사하고, 백그라운드 동기화(macOS는 launchd, Windows는 작업 스케줄러)를 등록하며, Claude Code의 세션 종료 이벤트에 훅을 연결합니다. 설치 후 원격으로 실행되는 것은 없습니다.

## 먼저 로컬에서 실행해 보기

모든 컬렉터에는 `--summary` 모드가 있어 로그를 스캔하고 집계 JSON을 출력하되 **아무것도 업로드하지 않습니다**. 설치 전에 실행해서 AgentBoard가 받게 될 내용을 직접 확인하세요:

```bash
python3 collectors/collect.py --summary           # Claude Code
python3 collectors/collect_codex.py --summary     # Codex
python3 collectors/collect_gemini.py --summary    # Gemini
python3 collectors/collect_kimi.py --summary      # Kimi Code
python3 collectors/collect_opencode.py --summary  # OpenCode
python3 collectors/collect_openclaw.py --summary  # OpenClaw
```

## 동작 방식

1. **훅** — Claude Code의 `Stop` 이벤트가 `hook.sh`를 트리거하여 종료된 세션에 대해 컬렉터를 실행합니다(동일 세션은 5분당 최대 1회로 제한).
2. **데몬** — 경량 백그라운드 프로세스가 180초마다 재스캔하여 Codex, Gemini 등 다른 도구의 사용량도 계속 동기화됩니다.
3. **로컬 집계** — 컬렉터는 JSONL/JSON/SQLite 세션 로그를 일별·세션별 통계로 파싱합니다. 파싱은 전적으로 사용자의 컴퓨터에서 이루어집니다.
4. **메타데이터 동기화** — 집계 결과는 `~/.agentboard/config.json`에 저장된 디바이스 토큰으로 AgentBoard API에 POST됩니다. 서버는 토큰의 **해시만** 저장하며 원본 토큰은 저장하지 않습니다.

## 토큰 집계 기준

도구마다 토큰을 보고하는 방식이 다릅니다. AgentBoard는 프로바이더 자체 합계와 전체 세부 내역을 모두 보관하므로, 리더보드 수치가 공식 사용량 대시보드와 일치합니다:

- **Claude Code / Cowork / Kimi Code / OpenCode / OpenClaw** — 캐시 토큰은 별도 필드. `provider_total = input + output + cache_read + cache_creation`.
- **Codex** — 보고되는 input에 캐시 토큰이 이미 포함됨. `provider_total = input + output`. 캐시 필드는 세부 내역 표시 전용이며 이중 계산하지 않습니다.
- **Gemini** — 프로바이더의 `totalTokenCount`를 그대로 사용. Gemini는 캐시 생성을 보고하지 않습니다.

모든 소스에서 `non_cache_total = provider_total − cache_read − cache_creation`을 제공하여 캐시를 제외한 비교가 가능합니다. 전체 명세: [docs/token-accounting.md](./docs/token-accounting.md)

## AgentBoard가 아닌 것

AgentBoard는 과금 미터가 아니며 암호학적 작업 증명도 아닙니다. 로컬 AI 도구 로그는 소유자가 편집할 수 있고, 도구마다 토큰 데이터를 노출하는 방식도 다릅니다. AgentBoard는 투명하고 소스를 구분하는 사용량 분석 — 개인 통계, 팀 가시성, 트렌드 — 에 집중합니다.

## 개발

컬렉터는 표준 라이브러리만 사용하는 독립 Python 3 스크립트로, `pip install`이 필요 없습니다. 각 스크립트는 한 번에 통독하며 감사할 수 있도록 의도적으로 자기 완결적으로 작성되어 있습니다.

```bash
python3 collectors/collect.py --summary            # 실제 로그 대상 드라이런
python3 collectors/collect_codex.py --diagnose-date 2026-06-01   # 토큰 집계 상세 진단
```

이슈와 PR을 환영합니다 — [CONTRIBUTING.md](./CONTRIBUTING.md)를 참고하세요. 완전 제거는 [UNINSTALL.md](./UNINSTALL.md)를 확인하세요.

## 라이선스

[MIT](./LICENSE)
