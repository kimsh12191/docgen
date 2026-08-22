# docgen

문서 PNG 한 장을 입력하면, 사람이 원본과 나란히 놓고 봤을 때 최대한 같아 보이는
**편집 가능한 HTML**을 만든다.

핵심은 하나의 loop뿐이다. Qwen VLM이 실제 렌더 결과를 직접 보면서 판단한다.

```
SOURCE PNG -> INITIAL HTML -> RENDER
                                |
        +-----------------------+
        v
      PLAN   (VLM, thinking ON)   가장 중요한 불일치 하나를 고른다
      ACTION (VLM, thinking OFF)  그 문제를 고치도록 HTML을 수정한다
      APPLY  (Python)             fence 제거 + sanity check
      RENDER (외부 renderer)      HTML -> PNG
      VERIFY (VLM, thinking ON)   keep / revert / done
        |
        +-> keep   -> candidate 채택, 다음 PLAN
            revert -> 이전 HTML 복원, 다음 PLAN
            done   -> clone.html
```

CV 파이프라인, heuristic rule 모음, 구조물별 action 타입은 없다. VLM이 원본과
실제 렌더를 비교해서 HTML을 직접 고치고, 그 수정 결과를 새로 렌더해서 스스로
판정한다.

### ACTION의 두 가지 모드

PLAN이 이미 내놓는 `scope`가 그대로 모드 스위치다. 별도 taxonomy는 없다.

| PLAN scope | ACTION 모드 | 모델이 돌려주는 것 |
| --- | --- | --- |
| `local` | patch | `{"edits": [{"find": ..., "replace": ...}]}` 정확 문자열 치환 |
| `global` | rewrite | HTML 전문 |
| 누락·불명 | rewrite | 안전한 기본값 |

patch 모드는 응답 크기가 **문서 크기가 아니라 수정 크기**에 비례하므로, 빽빽한
문서에서 ACTION이 `max_tokens`에 걸려 매 라운드 거부되는 문제를 원인 단계에서
없앤다.

patch의 `find`는 적용 시점에 **정확히 1회** 매칭되어야 한다. 없거나 여러 곳에
매칭되거나 no-op이면 그 라운드를 거부하고 현재 HTML은 건드리지 않는다. 절반만
적용된 patch는 거부된 라운드보다 나쁘다 — VERIFY가 실제로 일어나지 않은 수정을
판정하게 되기 때문이다.

## 요구 사항

* Python 3.11 이상 (`tomllib` 사용). 외부 의존성은 Pillow 하나뿐이다
* **Qwen VLM** OpenAI 호환 endpoint
* **HTML renderer** 서비스 (이미 별도로 실행 중인 것을 사용한다)

```bash
pip install -r requirements.txt
```

두 서비스는 사내망에 있다. `10.167.129.250:30164` 와 `10.167.129.230:30900` 에
접근 가능한 호스트에서 실행해야 한다.

## 사용법

```bash
python run.py doctor                          # 두 서비스 점검
python run.py doctor --llm-image              # 멀티모달 호출까지 확인
python run.py render test.html -o test.png    # HTML -> PNG 단발 렌더
python run.py build sample.png -o out/sample  # 전체 loop 실행
python run.py build sample.png --max-rounds 4 -v
```

`doctor`는 하나라도 실패하면 non-zero로 종료한다. `build`는 renderer health
검사가 실패하면 시작하지 않는다 — renderer는 이 loop의 필수 구성요소다.

## 산출물

```
out/sample/
  clone.html          최종 편집 가능 HTML
  clone.png           그 렌더 결과
  final_verify.json   마지막 VERIFY 판정
  summary.json        라운드별 결정 요약
  run.log
  rounds/
    bootstrap.html  bootstrap.png
    r01/  plan.json  action_raw.txt  patch.json
          before.html  before.png
          candidate.html  candidate.png
          metrics.json  verify.json
    r02/  ...
```

`patch.json`은 patch 모드 라운드에만 생긴다. APPLY나 RENDER에서 실패한 라운드는
`candidate.png` 대신 `error.json`을 남기고, 현재 HTML은 건드리지 않는다.
`summary.json`은 라운드별로 어느 모드였는지(`mode`)를 함께 기록한다.

## 설정

`config.toml`에 endpoint와 loop 설정이 들어 있다. 서비스 위치만 바꿔서 실행할
때는 환경 변수 세 개로 덮어쓸 수 있다: `DOCGEN_LLM_BASE_URL`,
`DOCGEN_LLM_MODEL`, `DOCGEN_RENDERER_URL`.

## 파일 구성

| 파일 | 역할 |
| --- | --- |
| `run.py` | CLI: `doctor`, `render`, `build` |
| `config.py` | `config.toml` 로딩 |
| `llm.py` | Qwen client (표준 `urllib`), message/image helper |
| `renderer.py` | `/health` + `/probe` client와 probe script |
| `prompts.py` | 4개 stage prompt |
| `pipeline.py` | bootstrap과 PLAN/ACTION/APPLY/RENDER/VERIFY loop |
| `utils.py` | 로깅, 이미지 인코딩, fence/think 제거, JSON 추출 |

## Renderer 계약

`POST /probe`에는 아래 다섯 필드를 항상 함께 보낸다. `probe_js`는 생략하지
않는다.

```json
{"html": "...", "width": 800, "wait_ms": 400, "device_scale": 1.0, "probe_js": "() => {...}"}
```

응답은 `{"ok": true, "png_base64": "...", "metrics": {...}}` 형태를 기대한다.
그 밖의 경우는 모두 `RendererError`를 발생시킨다. 이 프로젝트는 자체 브라우저를
띄우지 않는다.

## Thinking 제어

thinking은 stage별로 `chat_template_kwargs.enable_thinking`으로 지정한다
(BOOTSTRAP off, PLAN on, ACTION off, VERIFY on). 서버가 이 key 때문에 HTTP 400을
반환하면 client가 key를 제거하고 한 번 재시도하며, 경고를 명확히 로그에 남긴
뒤 이후로는 서버 기본값을 따른다. 이 경우 `summary.json`에
`"thinking_control": false`로 기록된다.

## 테스트

`tests/` 아래는 전부 테스트 전용이며 pipeline에서 import하지 않는다. 제품
코드는 `config.toml`에 설정된 두 HTTP 계약만 알고 있어서, 그 계약을 채우는
쪽을 바꿔 끼우면 사내망 없이도 루프를 돌릴 수 있다.

### 1. 오프라인 로직 테스트 (아무 것도 필요 없음)

```bash
python3 tests/test_offline.py
```

mock renderer와 mock Qwen을 in-process로 띄워 전체 build를 돌리고, 산출물
구조와 keep / revert / reject / done 동작을 검증한다.

### 2. 실제 브라우저 렌더러 (Chromium 필요)

```bash
pip install playwright        # 브라우저 바이너리는 이미 있다고 가정
python3 tests/real_renderer.py 38900
DOCGEN_RENDERER_URL=http://127.0.0.1:38900 python run.py render page.html -o page.png
```

`/health` + `/probe` 계약을 실제 Chromium으로 구현한다. `probe_js`를 페이지
안에서 진짜로 평가하므로, probe script가 동작하는지 확인할 때 쓴다.

### 3. Qwen 대신 Claude로 루프 돌리기 (API 키 필요)

```bash
pip install anthropic
export ANTHROPIC_API_KEY=...
python3 tests/claude_llm_adapter.py 38902     # OpenAI 계약 -> Claude API
python3 tests/real_renderer.py 38900          # 별도 터미널

DOCGEN_LLM_BASE_URL=http://127.0.0.1:38902/v1 \
DOCGEN_LLM_MODEL=claude-opus-5 \
DOCGEN_RENDERER_URL=http://127.0.0.1:38900 \
python run.py build source.png -o out/source
```

어댑터가 흡수하는 계약 차이:

* `temperature` / `top_p` 는 Claude Opus 5 에서 제거된 파라미터라 전달하지
  않는다.
* `chat_template_kwargs.enable_thinking` 은 `true` -> effort high,
  `false` -> effort low 로 매핑한다. thinking을 완전히 끄지는 않는다.
* Claude는 thinking을 별도 블록으로 주므로 `<think>` 를 벗겨낼 필요가 없다.
