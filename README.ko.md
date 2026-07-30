# moim-scribe

**반복해서 만나는 모임을 위한 로컬 우선 음성 파이프라인 — 같은 사람들이 자주 만날수록 더 정확해집니다.**

독서모임, 글쓰기 모임, 매주 하는 팀 회의처럼 **같은 멤버가 반복해서 만나는 자리**를 위해 만들었습니다. 지정 폴더를 감시하다가 녹음 파일이 들어오면: 전사(whisper) → 화자분리(pyannote) → **회차마다 쌓이는 음성 지문 DB와 대조해 실명 자동 매칭** → 모임 유형별 양식으로 정리까지. 음성 지문은 항상 로컬입니다. 직접 고르는 단계가 둘 있습니다 — 전사는 기본이 내 컴퓨터이고 Soniox로 보낼 수도 있으며, 정리는 로컬(Ollama 또는 생략) 또는 클라우드 LLM(Claude)입니다.

> **동의가 먼저입니다.** moim-scribe는 참가자 전원의 동의 아래 스스로를 녹음하는 모임을 위해 만들어졌습니다. 음성 지문은 생체정보입니다 — 몰래 녹음하거나 동의 없이 사람을 식별하는 데 절대 쓰지 마세요. **사적 관계에서 특히 그렇습니다**: 연인·가족과의 대화는 한쪽이 말 없이 녹음할 유인이 가장 큰 경우이고, 그래서 가장 하지 말아야 하는 경우입니다. 많은 나라에서 이것은 무례가 아니라 불법입니다: [docs/privacy.md](docs/privacy.md)

[English README](README.md)

## 동작 구조

```mermaid
flowchart TD
    subgraph machine["내 컴퓨터 안"]
        A["inbox/<br>녹음 파일 도착"] --> B["전사 + 화자분리<br>whisper (mlx / faster) + pyannote"]
        FP[("화자 지문 DB<br>회차마다 축적")] <--> B
        B --> C["모임 양식으로 정리<br>렌즈 · 스타일 · 팩"]
        C --> D["results/<br>정리 md + 전사 txt"]
        V["enroll · verify 명령"] -.-> FP
    end
    B -.->|"stt: soniox 선택 시에만<br>녹음 파일 자체를 전송"| STT["Soniox<br>클라우드 STT"]
    C -.->|"클라우드 백엔드 선택 시에만<br>전사본 전송 (claude_cli · anthropic_api)"| LLM["클라우드 LLM"]
```

지문은 점선을 절대 넘지 않습니다. 넘을 수 있는 것은 둘이고 각각 직접 골라야만 넘습니다 — 녹음 파일(`stt: soniox`)과 전사본(클라우드 정리 백엔드). `soniox` + `none` 조합이면 녹음은 나가고 전사본은 안 나가며, `mlx` + `ollama`면 아무것도 안 나갑니다.

## 이미 많은 whisper + pyannote 스크립트와 뭐가 다른가

일회성 화자분리 스크립트는 모든 녹음을 "처음 만난 사람들의 방"으로 취급합니다. 반복 모임은 다릅니다 — 격주마다 같은 여섯 명이잖아요. moim-scribe는 그 사실을 이용합니다.

- **축적되는 음성 지문.** 누가 누군지 한 번만 확정해주면, 그 사람의 ECAPA 임베딩이 등장할 때마다 발화초 가중 평균으로 정교해집니다. 두 번째 회차부터 `SPEAKER_03`이 자동으로 실명이 됩니다.
- **병합 복구.** 목소리가 비슷한 두 사람을 pyannote가 한 화자로 뭉쳐버리는 고전적 실패를, 등록 지문과의 세그먼트 단위 재대조로 감지하고 전사 라벨을 복구합니다. 미등록 손님의 발화는 절대 건드리지 않습니다.
- **환각 억제 전사.** whisper가 같은 문장을 정확히 1.0초 간격으로 17번 반복하는 류의 디코딩 병리를 디코드 옵션과 결정론적 후처리로 이중 방어합니다.
- **모임별 양식(팩).** 회의는 회의록으로, 독서모임은 참가자별 감상 정리로. 모임의 참가자 명단·배경 맥락이 프롬프트에 함께 실립니다. [docs/packs.md](docs/packs.md)
- **정직한 라벨.** `moim-scribe verify`가 완성된 전사를 지문·참가자 명단과 교차 검증합니다. 실제 오귀속 사고에서 확립한 원칙 기반: [docs/speaker-verification.md](docs/speaker-verification.md)

## 플랫폼 지원

| | STT 백엔드 | 상태 |
|---|---|---|
| macOS (Apple Silicon) | `mlx` (mlx-whisper) | 1급 — 이 구성에서 만들어지고 실운영 검증됨 |
| Windows / Linux / Intel 맥 | `faster` (faster-whisper) | 설계상 지원, CI 유닛 검증 — 실사용 피드백 환영 |
| 모든 플랫폼 | `soniox` (클라우드) | 로컬 모델·허깅페이스 토큰 불필요 — 대신 녹음이 업로드됩니다. 아래 참고 |

pyannote·speechbrain은 PyTorch라 어디서든 돌고(CUDA·MPS·CPU), 폴더 감시는 `watchdog`(3개 OS 네이티브 이벤트)입니다.

## 전사 백엔드

| 백엔드 | 어디서 도나 | 화자분리 | 특징 |
|---|---|---|---|
| `mlx` (Apple Silicon 기본) | 내 컴퓨터 | pyannote, 로컬 | 파일명이나 녹음 앞부분 멘트에서 인원수를 받습니다 |
| `faster` (그 외 기본) | 내 컴퓨터 | pyannote, 로컬 | 동일 |
| `soniox` | Soniox 서버 — **녹음이 업로드됩니다** | Soniox가 수행 | pyannote·허깅페이스 토큰·모델 다운로드 전부 불필요. API 키 필요, 오디오 시간당 과금 |

`soniox`를 고르기 전에 알아둘 것이 둘입니다. **한 명씩 순서대로 말하는 대화**에서는 빠르고 정확한데, 여러 사람이 서로 말을 겹쳐 하는 시끄러운 녹음에서는 서로 다른 화자를 하나로 뭉치는 것이 실측으로 확인됐습니다. 그리고 **인원수를 받지 않습니다** — 화자분리가 켜고 끄는 스위치 하나이고 최대 15명까지 스스로 감지하므로, 파일명에 적은 인원수가 이 경로에서는 무시됩니다(무시할 때 로그에 남습니다). 파일 하나당 300분 상한이 있습니다.

설정 방법과 직접 관리해야 하는 용량 상한: [docs/setup-soniox.md](docs/setup-soniox.md)

## 설치

요구사항: Python 3.11+, [ffmpeg](https://ffmpeg.org) (`brew install ffmpeg` / `winget install ffmpeg` / `apt install ffmpeg`).

```bash
# macOS Apple Silicon
pip install "moim-scribe[mlx] @ git+https://github.com/kemy-ai/moim-scribe"

# Windows / Linux / Intel 맥
pip install "moim-scribe[cross] @ git+https://github.com/kemy-ai/moim-scribe"
```

최초 1회: pyannote 화자분리 모델은 gated라서 무료 Hugging Face 토큰 발급 + 모델 약관 동의가 필요합니다. 2분이면 됩니다: [docs/setup-pyannote.md](docs/setup-pyannote.md)

## 빠른 시작

```bash
moim-scribe init ~/MyMeetings          # 워크스페이스 생성 (기본 한국어)
# HF 토큰 저장:
echo "hf_..." > ~/MyMeetings/.hf_token

moim-scribe doctor ~/MyMeetings        # ffmpeg·백엔드·토큰 점검

# 유형 폴더에 녹음 넣기:
cp 녹음.m4a ~/MyMeetings/inbox/gathering/

moim-scribe watch ~/MyMeetings         # 1회만 돌리려면 scan-inbox
```

결과는 `results/`에 `YYMMDD_제목_유형.md` + 전체 전사본으로 저장됩니다. 추측을 없애는 팁 두 가지:

- 파일명에 인원수를 넣기 — `북클럽 5명.m4a`
- 또는 녹음 시작할 때 말하기 — "참석자 다섯 명입니다"

## 누가 누군지 가르치기

```bash
# 첫 회차 처리 후, 라벨→이름 매핑을 한 번만.
# enroll 이 읽는 것은 .jsonl 입니다(구간 start/end 가 거기에만 있음) — 그 파일은
# results/ 에 남습니다. 원본 오디오는 inbox/_done/ 으로 들어갑니다:
moim-scribe enroll inbox/_done/녹음.m4a results/YYMMDD_녹음_diarized.jsonl \
    SPEAKER_00=김하늘 SPEAKER_02=이바다 --workspace ~/MyMeetings

moim-scribe speakers ~/MyMeetings      # 축적된 지문 확인

# 이후 회차부터 자동 식별. 완성 전사는 언제든 검증(이쪽이 읽는 것은 .txt):
moim-scribe verify inbox/_done/녹음.m4a inbox/_done/녹음_transcript_diarized.txt \
    --expected "김하늘,이바다,박구름" --workspace ~/MyMeetings
```

지문 DB를 믿을 수 있게 유지하는 규칙(사람이 확정한 구간만 등록 등)은 [docs/speaker-verification.md](docs/speaker-verification.md)에 있습니다.

## 정리 백엔드

| 백엔드 | 필요한 것 | 전사본이 컴퓨터 밖으로 나가나? |
|---|---|---|
| `claude_cli` (기본) | Claude Code 설치 | 나감 — 기존 로그인으로 Anthropic에 전송 |
| `anthropic_api` | `ANTHROPIC_API_KEY` | 나감 — Messages API로 Anthropic에 전송 |
| `ollama` | 로컬 Ollama 실행 | 안 나감 — 이 단계에서 나가는 것 없음 |
| `none` | 없음 | 안 나감 — 화자분리 전사까지만 |

`config.yaml`에서 교체합니다. 렌즈(정리 양식)는 한국어·영어 둘 다 내장. 이 표는 **전사본**에 대한 것이고, **녹음 파일**이 나가는지는 위의 `stt` 선택이 결정합니다.

## 프라이버시와 동의 — 꼭 읽어주세요

음성 지문은 **생체정보(민감 개인정보)**이고, 녹음에는 사적 대화가 담깁니다.

- **녹음과 지문 등록은 모든 참가자의 동의를 받고 하세요.** 여러 나라에서 이것은 매너가 아니라 법적 요건입니다 — 한국 개인정보보호법상 개인을 식별하는 음성 특징 정보는 민감정보(생체인식정보)로 별도의 명시적 동의가 필요합니다.
- 지문화는 항상 로컬이고, 최초 모델 다운로드 후에는 그 단계가 강제 오프라인으로 동작합니다. 네트워크에 닿을 수 있는 단계는 둘이며 직접 그렇게 설정했을 때만 그렇습니다 — `stt: soniox`는 **녹음 파일**을, 클라우드 정리 백엔드는 **전사본**을 보냅니다. 기본 설정은 녹음을 내보내지 않습니다.
- 지문(`speakers/`)·녹음·명단은 리포 밖 워크스페이스에 있고, `init`이 방어용 `.gitignore`까지 깔아줍니다. 워크스페이스를 공개 저장소에 올리지 마세요.

상세: [docs/privacy.md](docs/privacy.md)

## 백그라운드 상주

launchd(macOS)·systemd(Linux)·작업 스케줄러(Windows) 템플릿: [docs/deployment.md](docs/deployment.md). 구글드라이브·Dropbox 동기화 폴더도 됩니다 — 파일이 완전히 내려와 디코딩 가능한지 확인한 뒤에만 처리합니다.

## 유지보수

best-effort로 운영합니다: 이슈·PR은 읽지만 응답 기한은 보장하지 않습니다. 포크 환영합니다.

## 로드맵 (v0.1 범위 밖)

- 발행 플러그인(노션 등)·완료 알림
- `faster` 백엔드의 1급 승격을 위한 실사용 검증

## 라이선스

[MIT](LICENSE). 의존 프로젝트는 전부 공개 라이선스(whisper 구현체 MIT, pyannote.audio MIT, speechbrain·watchdog Apache-2.0)이며, gated인 pyannote **모델 가중치**는 Hugging Face에서 각자 약관에 동의하고 본인 컴퓨터로 받는 것으로 이 리포에 재배포되지 않습니다.
