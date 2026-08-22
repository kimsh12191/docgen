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

### 처음 실행할 때

이 순서대로 한다. 앞 단계가 실패하면 다음으로 넘어가지 말 것.

1. `python run.py doctor` — 두 서비스에 닿는지 확인한다. 실패하면 방화벽·VPN
   문제이지 코드 문제가 아니다.
2. `python run.py doctor --llm-image` — 이미지를 실제로 보내 멀티모달 호출이
   되는지 확인한다.
3. `python run.py build sample.png -o out/sample --max-rounds 2 -v` — 짧게
   먼저 돌려서 프롬프트가 먹히는지 본다. 여기서 `plan.json`과 `verify.json`이
   말이 되는 내용이면 라운드를 늘린다.
4. `python run.py build sample.png -o out/sample` — 기본 8라운드.

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

## 사람이 개입하기 (선택)

PLAN이 엉뚱한 것을 우선순위로 집거나, VERIFY가 자기가 한 수정에 관대할 때 쓴다.
둘 다 옵션이고 기본값은 꺼져 있다.

### 운영자 메모 — 배치 실행에서도 쓸 수 있다

```bash
python run.py build sample.png --note "표 정렬이 이 문서에서 가장 중요하다"
python run.py build sample.png --notes-file notes.txt
```

메모는 PLAN과 VERIFY 프롬프트에 함께 들어간다. "지금 무엇이 잘못됐다"는 서술이
아니라 **이 문서에서 무엇이 중요한지**를 적는 자리다. 프롬프트에는 "메모를 현재
문제의 설명으로 받아들이지 말고, 이미지를 먼저 판단하라"는 지시가 붙어 있어서
메모가 눈앞의 렌더 판단을 덮어쓰지 않는다.

### 대화형 — 라운드마다 개입

```bash
python run.py build sample.png --interactive
```

PLAN 직후와 VERIFY 직후에 멈춘다.

| 지점 | 입력 | 결과 |
| --- | --- | --- |
| PLAN | Enter | 계획 그대로 수락 |
| PLAN | 아무 텍스트 | `plan.json`의 `operator_instruction`으로 들어가고, ACTION이 그대로 받는다 |
| PLAN | `s` | 이 라운드를 건너뛴다 |
| VERIFY | Enter | 모델 판정 수락 |
| VERIFY | `keep` / `revert` / `done` | 판정을 강제한다 |

stdin이 터미널이 아니면(배치·cron·CI) `--interactive`는 경고를 남기고 자동으로
꺼진다. 입력을 기다리다 빌드가 멈추는 일은 없다.

### 개입은 전부 따로 기록된다

이게 중요하다. 사람이 구해준 것과 모델이 스스로 한 것을 구분하지 못하면
"Qwen이 이 작업을 할 수 있나"라는 판단이 오염된다.

* VERIFY 판정을 뒤집으면 모델의 원래 판정이 `verify.json`의 `model_decision`에
  그대로 남는다. `operator_override`에 사람이 고른 값이 들어간다.
* `summary.json`에 `operator_notes`(어떤 메모로 돌렸는지),
  `operator_interventions`(라운드별 개입 횟수), `operator_rounds`(개입이 있었던
  라운드 수), `skipped`(건너뛴 라운드 수)가 남는다.
* 라운드별로도 `operator: true/false`가 붙는다.

모델 단독 성능을 보려면 메모 없이 `--interactive` 없이 돌린 실행을 봐야 한다.
개입이 섞인 실행에서는 `operator: true`인 라운드를 제외하고 읽는다.

## 결과 읽는 법

먼저 `clone.png`와 입력 PNG를 나란히 놓고 눈으로 본다. 그게 이 도구의 목표다.
그 다음 `summary.json`으로 loop가 어떻게 굴러갔는지 확인한다.

```json
{
  "stop_reason": "done",
  "rounds_run": 5, "kept": 3, "reverted": 1, "rejected": 1, "errors": 0,
  "thinking_control": true,
  "rounds": [
    {"round": 1, "decision": "keep", "mode": "patch",
     "scope": "local", "target": "...", "goal": "...", "reason": "...", "error": ""}
  ]
}
```

| 필드 | 의미 |
| --- | --- |
| `stop_reason` | `done` = VERIFY가 충분히 닮았다고 판단하고 종료. `max_rounds` = 라운드를 다 쓰고 끝. |
| `kept` | VERIFY가 개선으로 인정해 채택한 라운드 수 |
| `reverted` | 렌더는 됐지만 더 나빠져서 되돌린 라운드 수 |
| `rejected` | HTML이 깨졌거나 patch가 적용되지 않았거나, 수정이 아무 변화도 만들지 못해 렌더까지 가지 못한 라운드 수 |
| `errors` | LLM 호출 자체가 실패한 라운드 수 |
| `mode` | 그 라운드가 `patch`였는지 `rewrite`였는지 |
| `thinking_control` | `false`면 서버가 `chat_template_kwargs`를 거부해 stage별 thinking 제어 없이 돌았다는 뜻 |
| `operator_interventions` | 사람이 PLAN에 지시를 넣거나 VERIFY 판정을 뒤집은 횟수. `0`이면 모델 단독 실행 |
| `skipped` | 사람이 건너뛴 라운드 수 |

건강한 실행은 `kept`가 대부분이고 `stop_reason`이 `done`이다.
`reverted`가 섞이는 것은 정상이다 — VERIFY가 제 역할을 했다는 신호다.

라운드별로 더 파고들려면 `rounds/rNN/` 안을 본다. `plan.json`(무엇을 고치려
했는지) → `patch.json` 또는 `action_raw.txt`(실제로 뭘 했는지) →
`before.png` / `candidate.png`(그래서 어떻게 변했는지) → `verify.json`(왜
채택·기각했는지) 순서로 읽으면 한 라운드의 판단 과정이 그대로 재구성된다.

## 문제가 생기면

| 증상 | 원인과 대응 |
| --- | --- |
| `doctor`의 `[LLM]` 또는 `[Renderer]`가 FAIL | 서비스에 못 닿는다. 사내망·VPN·방화벽을 먼저 확인한다. 코드를 고칠 일이 아니다. |
| `rejected`가 대부분이고 `mode`가 `rewrite` | 문서가 커서 ACTION이 `max_tokens`에 걸린다. 로그의 `finish_reason == 'length'` 경고로 확인된다. PLAN이 `global`만 내고 있다는 뜻이므로 PLAN 프롬프트를 국소 수정 쪽으로 유도해야 한다. |
| `rejected`가 대부분이고 `mode`가 `patch` | `find` 문자열이 문서에 없거나 여러 곳에 매칭된다. `error.json`에 어느 문자열이 문제였는지 그대로 찍힌다. patch 프롬프트에서 "유일하게 매칭되는 짧은 문자열" 지시를 강화할 지점이다. |
| `stop_reason`이 계속 `max_rounds` | 수렴이 느리다. `--max-rounds`를 늘리기 전에 `verify.json`의 `next_major_issue`를 보고 PLAN이 같은 문제를 반복해서 집는지 확인한다. |
| `thinking_control`이 `false` | 서버가 해당 파라미터를 안 받는다. 동작은 하지만 PLAN·VERIFY가 thinking 없이 판단하므로 품질이 떨어질 수 있다. |
| `errors`가 있다 | LLM 호출 실패다. `run.log`에 재시도 내역과 HTTP 응답이 남는다. |

## 현재 검증 상태

정직하게 적어 둔다.

* **검증됨** — loop 로직(keep / revert / reject / done, 산출물 구조, patch
  가드), renderer `/probe` 계약과 `probe_js`의 실제 브라우저 동작, ACTION의
  잘림 처리. `python3 tests/test_offline.py`로 재현 가능하다.
* **미검증** — 사내 Qwen이 이 프롬프트에 어떻게 반응하는지. 프롬프트 품질과
  수렴 속도는 실제 문서로 돌려봐야 안다. 위 "처음 실행할 때"의 3번을 짧게
  돌려서 `plan.json`·`verify.json`을 먼저 읽어보는 것을 권한다.

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
