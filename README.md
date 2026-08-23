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
      ACTION (VLM, thinking OFF)  patch 또는 HTML 전문으로 수정한다
      APPLY  (Python)             patch 적용 또는 fence 제거 + sanity check
      RENDER (외부 renderer)      HTML -> PNG
      VERIFY (VLM 또는 사람)      keep / revert / done
        |
        +-> keep   -> candidate 채택, 다음 PLAN
            revert -> 이전 HTML 복원, 다음 PLAN
            done   -> clone.html
```

CV 파이프라인, heuristic rule 모음, 구조물별 action 타입은 없다. VLM이 원본과
실제 렌더를 비교해서 HTML을 직접 고치고, 그 수정 결과를 새로 렌더해서 스스로
판정한다.

## ACTION의 두 가지 모드

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
python run.py render test.html --width 1000    # 렌더 폭을 바꿔서
python run.py build sample.png -o out/sample  # 전체 loop 실행
python run.py build sample.png --max-rounds 4 -v

# 사람이 개입하는 방식 (아래 "사람이 개입하기" 참고)
python run.py build sample.png --note "표 정렬이 가장 중요하다"
python run.py build sample.png --interactive
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
   먼저 돌려서 프롬프트가 먹히는지 본다. `rounds/r01/compare.png` 로 실제로
   나아졌는지 눈으로 보고, `plan.json`·`verify.json` 이 말이 되는 내용이면
   라운드를 늘린다.
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
    bootstrap_raw.txt  bootstrap_metrics.json
    r01/  plan.json  action_raw.txt  patch.json
          before.html  before.png
          candidate.html  candidate.png
          compare.png   compare_region.png
          plan_view.png metrics.json  verify.json
    r02/  ...
```

`compare.png`는 원본·수정 전·수정 후를 나란히 붙인 이미지로, 사람이 판정할 때
쓴다. `compare_region.png`는 영역을 지정한 라운드에만, `plan_view.png`는 사람이
개입하는 실행에만 생긴다.
`patch.json`은 patch 모드 라운드에만 생긴다. APPLY나 RENDER에서 실패한 라운드는
`candidate.png` 대신 `error.json`을 남기고, 현재 HTML은 건드리지 않는다.
`summary.json`은 라운드별로 ACTION 모드(`mode`)와 사람 개입 여부(`operator`)를
함께 기록한다.

## 사람이 개입하기 (선택)

가장 쉬운 방법은 웹 UI다. 터미널만 쓰는 방법은 그 아래에 있다.

> UI는 시나리오별 스크린샷과 함께 [`UI_docs/`](UI_docs/README.md) 에 따로
> 정리해 두었다. 구조와 확장 방법은 [`UI_docs/ARCHITECTURE.md`](UI_docs/ARCHITECTURE.md).

### 웹 UI

```bash
python run.py build sample.png --ui
```

로컬 주소가 출력되니 브라우저로 열어둔다. `--ui` 를 켜면 **기본적으로 사람 의견을
받는다** (PLAN·VERIFY 양쪽). 판단 조합은 이렇게 된다.

PLAN과 VERIFY 각각 **세 가지**뿐이다.

| | 하는 일 | PLAN | VERIFY |
| --- | --- | --- | --- |
| ① | Qwen 결과 그대로 | `① Qwen 계획대로` | `① Qwen 판정대로` |
| ② | Qwen 결과 + 내 의견 | `② 참고로 첨부 (계획 유지)` | `② 참고로 첨부 (판정 유지)` |
| ③ | Qwen 결과 버리고 내 결정 | `③ Qwen 계획 버리고 내 지시만` | `③ 내 판정: keep/revert/done` |

중간 단계는 없다. ②면 Qwen 결과가 남고 ③이면 안 남는다.

| 그 밖에 | 결과 |
| --- | --- |
| (PLAN) `Qwen 계획 버리고 내 지시만` | Qwen 계획을 **버린다**. `model_plan`에 기록만 남고 ACTION은 사람 지시만 본다 |
| 헤더의 `VERIFY 판정` / `PLAN 개입` | **실행 중에** 개입 방식을 바꾼다. 다음 라운드부터 적용 |
| (VERIFY) `내 판정: keep/revert/done` | Qwen 판정을 읽고 사람 판정으로 교체. `verified_by: operator` |

어느 쪽이든 **Qwen이 뭐라고 했는지 화면에서 먼저 본 다음** 고를 수 있다.
`--verify` 는 시작값일 뿐이고, 화면 오른쪽 위에서 실행 중에 바꿀 수 있다 —
"이제부터 내가 판정" 또는 "나머지는 알아서 돌려라" 둘 다 다시 실행하지 않고
된다.

화면에 나오는 것:

* **비교 이미지** — PLAN에서는 원본 · 현재 렌더, VERIFY에서는 원본 · 수정 전 ·
  수정 후
* 모델이 낸 PLAN 또는 VERIFY JSON 원문
* 의견 입력창과 버튼 (수락 / 의견 첨부 / 교체 / 건너뛰기)
* 지난 라운드에 무엇을 보냈는지

UI는 표준 라이브러리만 쓴다. 서버 프레임워크를 새로 깔지 않는다. 브라우저가
입력을 대신 주므로 터미널이 없어도 되고, `--ui-timeout` (기본 1800초) 안에 답이
없으면 입력 없음으로 처리한다.

#### 다른 PC에서 접속하기 (GPU 서버에서 돌리고 윈도우에서 보기)

기본값은 `127.0.0.1` 바인딩이라 그 서버에서만 열린다. 다른 PC에서 열려면:

```bash
python run.py build sample.png --ui --ui-host 0.0.0.0 --ui-port 8900
```

출력되는 주소는 `0.0.0.0` 이 아니라 **실제로 접속 가능한 IP**로 찍힌다
(예: `http://10.167.129.230:8900/`). 윈도우 브라우저에 그 주소를 넣으면 된다.

인증이 없으니 사내망에서만 쓴다. 그래서 기본값을 localhost로 두고 `--ui-host`를
명시적으로 켜야 하게 했다. 이미지는 그 실행의 출력 디렉터리 안에 있는 PNG만
서빙한다.

### 영역만 지정해서 고치기

UI의 이미지 위를 **드래그하면 그 영역만 고치라고 지정**할 수 있다.

```
선택 영역: 1. SOURCE  x 10%, y 14%, 폭 79%, 높이 28%
```

지정하면 그 라운드의 ACTION 호출에 **그 영역을 확대한 crop 두 장**(원본의 그
영역, 현재 렌더의 그 영역)이 이미지로 추가되고, 프롬프트에 "이 crop이 보여주는
것만 고치고 문서의 나머지는 건드리지 말라"가 함께 들어간다. 좌표는 패널 기준
비율(0~1)로 저장되므로 원본 스캔이 2480px이고 렌더가 800px여도 같은 영역을
가리킨다.

`plan.json` 에 `operator_region` 으로 남는다. 범위를 벗어나거나 형식이 잘못된
좌표는 저장하지 않고 버린다.

#### 영역 지정의 전체 흐름

**별도 파이프라인이 아니다.** 같은 루프의 한 라운드에 입력이 하나 더 붙는 것뿐이다.

```
PLAN 실행 (모델)
  └ UI에 원본 | 현재 렌더 표시
      └ 사람: 표 영역을 드래그 + "이 표만 원본에 맞춰라" + [Qwen 계획 버리고 내 지시만]
          └ plan.json  ← operator_instruction + operator_region(비율 좌표)
ACTION (모델)
  └ 받는 것: 원본 전체, 현재 렌더 전체, 그리고
             그 영역을 확대한 crop 2장 (원본 / 현재 렌더)
     프롬프트: "이 crop이 보여주는 것만 고치고 나머지는 건드리지 마라"
  └ patch 반환 (find / replace)
APPLY (Python) → RENDER (외부 renderer) → 새 PNG
VERIFY
  └ UI에 두 장 표시:
      · 전체 페이지 (원본 | 수정 전 | 수정 후)  ← 다른 곳이 망가졌는지
      · 지정 영역 확대 (원본 | 수정 전 | 수정 후) ← 그 부분이 고쳐졌는지
  └ 사람 또는 모델이 keep / revert / done
```

몇 가지 짚어둘 점:

* **모델이 "어디인지" 아는 방법은 좌표가 아니라 그림이다.** DOM 좌표를 HTML
  요소로 매핑하지 않는다. 확대 crop을 보여주고, 모델이 그것을 HTML 텍스트에서
  찾아 patch를 쓴다. 그래서 좌표계 변환 코드가 필요 없다.
* **좌표는 비율(0~1)로 저장한다.** 원본 스캔이 2480px, 렌더가 800px이어도 같은
  영역을 가리킨다.
* **반영 경로는 평소와 완전히 같다.** patch → APPLY → RENDER → VERIFY. 영역
  지정이 만드는 차이는 ACTION이 보는 이미지와 VERIFY가 보여주는 이미지뿐이다.
* **영역은 그 라운드에만 유효하다.** 다음 라운드는 다시 지정한다. 한 라운드에
  하나만 고치는 루프 원칙과 같은 이유다.
* 영역을 지정한 라운드는 `rounds/rNN/compare_region.png` 가 함께 남는다.

PLAN이 엉뚱한 것을 우선순위로 집거나, VERIFY가 자기가 한 수정에 관대할 때 쓴다.
둘 다 옵션이고 기본값은 꺼져 있다.

### 운영자 메모 — 배치 실행에서도 쓸 수 있다 (UI 없이)

```bash
python run.py build sample.png --note "표 정렬이 이 문서에서 가장 중요하다"
python run.py build sample.png --notes-file notes.txt
```

메모는 PLAN과 VERIFY 프롬프트에 함께 들어간다. "지금 무엇이 잘못됐다"는 서술이
아니라 **이 문서에서 무엇이 중요한지**를 적는 자리다. 프롬프트에는 "메모를 현재
문제의 설명으로 받아들이지 말고, 이미지를 먼저 판단하라"는 지시가 붙어 있어서
메모가 눈앞의 렌더 판단을 덮어쓰지 않는다.

### VERIFY를 누가 하는가

```bash
python run.py build sample.png --verify model   # 기본. 모델만 판정
python run.py build sample.png --verify both    # 모델이 판정하고 사람이 뒤집을 수 있다
   # 모델 호출 없이 사람이 판정
```

`compare.png` 는 **원본 · 수정 전 · 수정 후**를 한 장에 나란히 붙인 이미지다.
세 파일을 따로 열지 않아도 되고, 아래 여백은 세 패널에서 같은 양만큼 잘라내므로
수직 위치를 그대로 비교할 수 있다. 라벨이 영문인 이유는 한글 글리프가 없는
환경에서 라벨이 □로 깨지는 것보다 낫기 때문이다.

"다음에 고칠 것"에 적은 내용은 **다음 라운드 PLAN의 history에 실려 들어간다.**
사람의 판단이 다음 계획에 반영되는 경로다.

`--verify both` 인데 터미널도 UI도 없으면 `model` 로 내려간다.

### 대화형 — 뒤집기와 첨부

```bash
python run.py build sample.png --interactive
```

PLAN 직후와(`--verify both` 면) VERIFY 직후에 멈춘다. `--verify` 를 따로 주지
않으면 `both` 가 된다.

개입 방식은 **어느 단계에서든 두 가지**다.

* **뒤집기** — 모델 판단을 사람 것으로 갈아치운다
* **첨부** — 모델 판단을 그대로 두고 사람 의견을 붙인다

**PLAN**

| 입력 | 결과 |
| --- | --- |
| Enter | ① 계획 그대로 수락 |
| `a <의견>` | ② `operator_note`로 들어가고 Qwen 계획은 그대로 남는다 |
| `x <지시>` | ③ Qwen 계획을 버리고 그 지시가 목표가 된다. 원래 계획은 `model_plan`에 기록만 |
| `s` | 이 라운드를 건너뛴다 (세 가지 밖) |
| `keep`/`revert`/`done` | VERIFY 판정어라고 알려주고 다시 묻는다 |

**VERIFY** (`--verify both`)

| 입력 | 결과 |
| --- | --- |
| Enter | ① Qwen 판정 수락 |
| `a <의견>` | ② 판정은 그대로 두고 `operator_note`만 붙는다 |
| `keep`/`revert`/`done` | ③ Qwen 판정은 `model_decision`에 보존된다 |
| `revert <이유>` | ③ 뒤집으면서 같은 줄에 이유를 붙인다 |

첨부한 의견은 **다음 라운드 PLAN의 history로 실려 간다.** 판정을 바꾸지 않고
방향만 잡아주고 싶을 때 쓰는 경로다.

`a` 나 `o` 뒤에 내용을 안 적으면 빈 값으로 저장하지 않고 다시 묻는다. 세 번
알아듣지 못하면 모델 판단을 그대로 두고 넘어간다.

### 모델은 사람 개입이 온다는 걸 미리 안다

사람이 개입할 수 있는 실행에서는 PLAN·ACTION·VERIFY 프롬프트에 아래 계약이
함께 들어간다. 그래야 ACTION이 `operator_instruction` 을 "설명 없는 낯선 필드"가
아니라 우선해야 할 지시로 다룬다.

```
A human operator is taking part in this loop, so some of the input you get is
written by a person, not by you:
- "operator_instruction" in the plan: a human instruction that REPLACES the
  plan's own goal. Do what it says instead.
- "operator_note" in the plan: a human comment to take into account WITHOUT
  discarding the plan.
- a history line marked (operator: ...): a human comment on an earlier round.
The operator is looking at the same images you are. Prefer their input over your
own earlier reasoning, but never over what the current images plainly show. If
their input contradicts the images, say so rather than following it blindly.
```

모델 단독 실행(`--verify model`, `--interactive` 없음)에서는 이 블록이 **들어가지
않는다.** 오지 않을 입력을 설명해서 프롬프트를 흐리지 않는다.

멈춤은 해당 단계가 **이미 실행된 뒤**다. PLAN에 넣은 텍스트는 PLAN을 다시
돌리지 않고 ACTION으로 간다. 계획을 다시 짜게 하는 게 아니라 덧붙이는 것이다.

한 라운드에서 멈추는 횟수는 상황에 따라 다르다. APPLY에서 거부되거나 렌더가
실패하면 VERIFY까지 가지 않으므로 그 라운드는 PLAN에서만 멈춘다.

stdin이 터미널이 아니면(배치·cron·CI) `--interactive`는 경고를 남기고 자동으로
꺼진다. 입력을 기다리다 빌드가 멈추는 일은 없다.

### 개입은 전부 따로 기록된다

이게 중요하다. 사람이 구해준 것과 모델이 스스로 한 것을 구분하지 못하면
"Qwen이 이 작업을 할 수 있나"라는 판단이 오염된다.

* `plan.json`의 `planned_by`: `model` / `model+operator` / `operator`(Qwen 계획을
  버린 경우, 원래 계획은 `model_plan`에 남는다), 영역을 지정했다면
  `operator_region`.
* `verify.json`의 `verified_by`: `model` / `model+operator`(의견만 첨부) /
  `operator`(사람이 판정).
* 뒤집기와 첨부는 남는 필드로 구분된다. 뒤집기는 `operator_instruction`(PLAN)
  또는 `operator_override`(VERIFY), 첨부는 양쪽 다 `operator_note`.
* VERIFY 판정을 뒤집으면 모델의 원래 판정이 `verify.json`의 `model_decision`에
  그대로 남는다. `operator_override`에 사람이 고른 값이 들어간다.
* `summary.json`에 `operator_notes`(어떤 메모로 돌렸는지),
  `operator_interventions`(라운드별 개입 횟수), `operator_rounds`(개입이 있었던
  라운드 수), `skipped`(건너뛴 라운드 수)가 남는다.
* 라운드별로도 `operator: true/false`가 붙는다. 사람이 판정한 라운드, 사람이
  판정을 뒤집은 라운드, 사람이 PLAN에 지시를 넣은 라운드가 모두 여기 포함된다.
* `summary.json`의 `verify_mode`로 그 실행이 어떤 방식이었는지 남는다.

모델 단독 성능을 보려면 메모 없이 `--interactive` 없이 돌린 실행을 봐야 한다.
개입이 섞인 실행에서는 `operator: true`인 라운드를 제외하고 읽는다.

## 결과 읽는 법

먼저 `clone.png`와 입력 PNG를 나란히 놓고 눈으로 본다. 그게 이 도구의 목표다.
그 다음 `summary.json`으로 loop가 어떻게 굴러갔는지 확인한다.

```json
{
  "stop_reason": "done",
  "rounds_run": 5, "kept": 3, "reverted": 1, "rejected": 1, "errors": 0, "skipped": 0,
  "thinking_control": true,
  "verify_mode": "model",
  "operator_notes": "", "operator_interventions": 0, "operator_rounds": 0,
  "rounds": [
    {"round": 1, "decision": "keep", "mode": "patch", "operator": false,
     "scope": "local", "target": "...", "goal": "...", "reason": "...", "error": ""}
  ]
}
```

| 필드 | 의미 |
| --- | --- |
| `stop_reason` | `done` = VERIFY(모델 또는 사람)가 충분히 닮았다고 판단하고 종료. `max_rounds` = 라운드를 다 쓰고 끝. |
| `kept` | VERIFY가 개선으로 인정해 채택한 라운드 수 |
| `reverted` | 렌더는 됐지만 더 나빠져서 되돌린 라운드 수 |
| `rejected` | HTML이 깨졌거나 patch가 적용되지 않았거나, 수정이 아무 변화도 만들지 못해 렌더까지 가지 못한 라운드 수 |
| `errors` | LLM 호출 자체가 실패한 라운드 수 |
| `mode` | 그 라운드가 `patch`였는지 `rewrite`였는지 |
| `thinking_control` | `false`면 서버가 `chat_template_kwargs`를 거부해 stage별 thinking 제어 없이 돌았다는 뜻 |
| `verify_mode` | 시작할 때의 VERIFY 개입 여부: `model`(안 물음) / `both`(물음) |
| `verify_mode_final` | 끝날 때의 방식. 다르면 실행 중에 바꾼 것이다 |
| `operator_interventions` | 사람이 PLAN에 지시를 넣거나, VERIFY 판정을 뒤집거나, 직접 판정한 횟수. `0`이면 모델 단독 실행 |
| `operator_rounds` | 사람 개입이 있었던 라운드 수 |
| `skipped` | 사람이 건너뛴 라운드 수 |
| `operator_notes` | 그 실행에 쓰인 운영자 메모 원문 |
| `failed_attempts` | 시도했지만 안 된 접근 목록. 계속 쌓이면 같은 벽에 막혀 있다는 뜻 |

나머지 필드는 그대로 읽으면 된다 — `source`, `out_dir`, `clone_html`,
`clone_png`, `rounds_run`, `max_rounds`, 그리고 라운드별 상세가 담긴 `rounds`.

건강한 실행은 `kept`가 대부분이고 `stop_reason`이 `done`이다.
`reverted`가 섞이는 것은 정상이다 — VERIFY가 제 역할을 했다는 신호다.

VERIFY의 판단은 다음 라운드 PLAN으로 이어진다. 되돌린 이유(`why`)와 아직 남은
문제(`next`)가 짧은 history 한 줄로 실려 가고, 누가 판정했는지 대괄호로
표시된다.

```
Previous attempts:
- Round 3: reduce the title font -> revert [model] why: table became too wide; next: header rule
- Round 4: fix the header rule -> keep [operator] next: 제목 자간이 아직 다르다
```

되돌린 이유가 함께 가므로 같은 시도를 반복하지 않는다. `why:` 는 항상 **판정한
쪽의** 이유다 — `[operator]` 면 사람이 적은 것이고, 사람이 Qwen 판정을 교체한
경우 버려진 Qwen의 근거는 여기 오지 않는다. keep 일 때는 사람이 이유를 적었을
때만 붙는다. 자세한 표는 [`UI_docs/README.md`](UI_docs/README.md#판정이-다음-라운드로-전달되는-것) 에 있다.

history는 최근 3라운드만 간다(명세 17절). 그래서 그 창을 넘어간 실패는 **별도
목록으로 실행 내내 유지된다.**

```
Already tried without success:
- widen the main table -> revert: table became too wide
- tighten the title tracking -> rejected: could not be applied (edit 1 'find' is not in the document)
```

`revert`(렌더는 됐지만 더 나빠짐) · `rejected`(적용 실패) · `noop`(변화 없음)만
들어간다. LLM 오류는 계획 탓이 아니고, 사람이 건너뛴 라운드는 실패한 접근이
아니므로 제외한다. 최대 8개까지 유지되고 `summary.json` 의 `failed_attempts` 에
남는다.

프롬프트 문구는 "**같은 방식**을 반복하지 마라"다 — 목록에 오른 목표가 여전히
진짜 문제일 수 있으니, 이미지에 보이면 **다른 방법으로** 접근하라고 한다.
"그 문제를 건드리지 마라"가 아니다.

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
| `stop_reason`이 계속 `max_rounds` | 수렴이 느리다. `--max-rounds`를 늘리기 전에 `verify.json`의 `next_major_issue`를 보고 PLAN이 같은 문제를 반복해서 집는지 확인한다. 이 값은 다음 라운드 PLAN에 전달되므로, 계속 같은 값이면 PLAN이 그걸 못 고치고 있다는 뜻이다. |
| `thinking_control`이 `false` | 서버가 해당 파라미터를 안 받는다. 동작은 하지만 PLAN·VERIFY가 thinking 없이 판단하므로 품질이 떨어질 수 있다. |
| `errors`가 있다 | LLM 호출 실패다. `run.log`에 재시도 내역과 HTTP 응답이 남는다. |
| `kept`만 쌓이는데 `compare.png`는 나아지지 않는다 | VERIFY가 자기 수정에 관대한 경우다. `--ui` 로 Qwen 판정을 보면서 사람이 갈아치운다. |
| `done`이 너무 일찍 나온다 | VERIFY가 "거의 같다"를 느슨하게 본다. 위와 같은 대응. `final_verify.json`의 `reason`을 먼저 읽어 근거를 확인한다. |
| PLAN이 매 라운드 같은 것만 집는다 | `--note`로 이 문서에서 중요한 것을 알려주거나, `--interactive`로 그 라운드에 직접 지시한다. |

## 현재 검증 상태

정직하게 적어 둔다.

* **검증됨** — loop 로직(keep / revert / reject / done, 산출물 구조),
  patch 가드(없는·중복·no-op·잘못된 형식 edit 전부 거부), renderer `/probe`
  계약과 `probe_js`의 실제 브라우저 동작, ACTION 입출력 잘림 처리, 사람 개입
  전 경로와 그 기록, 검토 UI의 HTTP 왕복·경로 제한·실행 중 설정 전환, 영역
  지정이 확대 crop으로 ACTION까지 가는 경로, VERIFY 판단이 다음 PLAN으로
  전달되는 경로. `python3 tests/test_offline.py` 로 87개 검사가 재현된다.
* **부분 검증** — 실제 문서 한 장으로 2라운드를 돌려 원본 대비 불일치 픽셀이
  7.17% → 5.35% → 4.91% 로 줄어드는 것을 확인했다. 단 그때 VLM 역할은 Qwen이
  아니었으므로 수렴이 가능하다는 것까지만 말할 수 있다.
* **미검증(브라우저)** — 버튼 클릭과 영역 드래그는 자동 테스트에 없다.
  Playwright로 직접 띄워 확인했고, 그 과정에서 모든 버튼이 동작하지 않던 결함이
  나왔다. UI를 고치면 브라우저로 한 번 눌러보는 것이 필요하다.
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
| `ui.py` | 로컬 검토 UI (표준 라이브러리만). 질문을 띄우고 답을 기다린다. 문서는 `UI_docs/` |
| `config.py` | `config.toml` 로딩 |
| `llm.py` | Qwen client (표준 `urllib`), message/image helper |
| `renderer.py` | `/health` + `/probe` client와 probe script |
| `prompts.py` | stage prompt 5종(bootstrap / plan / action-patch / action-rewrite / verify)과 운영자 메모·계약·영역·history·실패목록 블록 |
| `pipeline.py` | bootstrap, PLAN/ACTION/APPLY/RENDER/VERIFY loop, 사람 개입 지점, 라운드 간 history·실패목록 |
| `utils.py` | 로깅, 이미지 인코딩, fence/think 제거, JSON 추출, patch edit 적용, 비교 이미지 합성, 영역 crop |

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

thinking은 stage별로 `chat_template_kwargs.enable_thinking`으로 지정한다.

| stage | thinking |
| --- | --- |
| BOOTSTRAP | off |
| PLAN | on |
| ACTION (patch·rewrite 모두) | off |
| VERIFY | on |

서버가 이 key 때문에 HTTP 400을 반환하면 client가 key를 제거하고 한 번
재시도하며, 경고를 명확히 로그에 남긴 뒤 이후로는 서버 기본값을 따른다. 이
재시도는 일반 retry 예산과 별개라서 `retries = 1`이어도 동작한다. 이 경우
`summary.json`에 `"thinking_control": false`로 기록된다.

## 테스트

`tests/` 아래는 전부 테스트 전용이며 pipeline에서 import하지 않는다. 제품
코드는 `config.toml`에 설정된 두 HTTP 계약만 알고 있어서, 그 계약을 채우는
쪽을 바꿔 끼우면 사내망 없이도 루프를 돌릴 수 있다.

### 1. 오프라인 로직 테스트 (아무 것도 필요 없음)

```bash
python3 tests/test_offline.py
```

mock renderer와 mock Qwen을 in-process로 띄워 전체 build를 돌린다. 검사 87개가
14개 그룹으로 나뉘어 다루는 범위:

* 산출물 구조와 keep / revert / reject / done 동작, revert가 이전 HTML을 실제로
  복원하는지
* patch 가드 — `find`가 없거나 여러 곳에 매칭되거나 no-op이거나 형식이 잘못된
  edit 전부 거부, 뒤쪽 edit이 실패하면 앞쪽도 적용되지 않음
* ACTION 입력이 잘리지 않고 전달되는지, `finish_reason=length`가 라운드를
  거부하는지
* renderer `/probe` 계약 위반 감지, `ok:false`가 예외를 던지는지
* `chat_template_kwargs` 400 fallback (`retries=1` 포함)
* 사람 개입 — 메모 주입, PLAN 첨부/교체/버리기, VERIFY 오버라이드/첨부,
  개입 기록의 정합성(개입 횟수와 라운드 플래그가 일치하는지)
* 검토 UI — 패널 좌표 계산, 출력 디렉터리 밖 파일·PNG 아닌 파일 거부, 질문
  게시부터 답 수신까지 HTTP 왕복, 범위를 벗어난 영역 좌표 폐기, 실행 중 설정
  전환이 다음 라운드에 반영되는지, `0.0.0.0` 바인딩이 접속 가능한 주소를
  광고하는지
* 영역 지정 — 비율 rect가 크기가 다른 이미지에 비례 적용되는지, ACTION이 확대
  crop 2장을 함께 받는지, `compare_region.png` 가 생기는지
* 라운드 간 전달 — VERIFY의 `reason`·`next_major_issue` 가 다음 PLAN 프롬프트에
  실제로 도달하는지, history 창(3)을 넘어간 실패가 별도 목록으로 남는지
* 개입 3상태 정합성 — `model` / `model+operator` / `operator` 를 두 단계에서
  전부 열거해, 누가 판정했는지 · 라운드 플래그 · history 태그 · 실려 가는 근거가
  서로 일치하는지. 이 판단을 필드 조합으로 다시 추론하는 코드가 없는지도 확인한다

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
