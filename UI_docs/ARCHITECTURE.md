# 검토 UI 구조

사용법은 [README.md](README.md) 에 있다. 이 문서는 UI가 파이프라인에 어떻게
붙어 있고 무엇을 고치면 되는지를 다룬다.

---

## 한 줄 요약

파이프라인은 UI를 모른다. `prompter.ask(prompt, context) -> str` 하나만 안다.

```
run.py --ui
  └ ReviewServer 생성 → start() → Pipeline(prompter=server)

Pipeline
  └ self._input(prompt, context)
       · prompter 없음 → input()          (터미널)
       · prompter 있음 → prompter.ask(...) (브라우저)

ReviewServer
  └ 질문 하나를 게시하고 답을 기다린다. 루프는 돌리지 않는다.
```

UI를 빼면 같은 코드가 터미널로 돌아간다. 루프의 제어 흐름은 어느 쪽이든 같다.

---

## 한 라운드의 시간축

```
파이프라인 스레드                        브라우저
─────────────────                      ─────────
PLAN 모델 호출
review_plan()
  └ ask() ──── _pending 게시 ──────────▶
       ⏸ Event 대기                     GET /state (1초 폴링)
       ⏸                                화면 렌더
       ⏸                                사람이 버튼 클릭
       ⏸ ◀───── Event set ───────────── POST /answer
  답 문자열 파싱
ACTION → APPLY → RENDER
VERIFY 모델 호출
review_verify()
  └ ask() ──── (같은 방식) ────────────▶
```

WebSocket이 아니라 **1초 폴링**이다. 표준 라이브러리만 쓰기로 한 결과이고,
사람이 판단하는 속도에서 1초는 문제가 되지 않는다.

파이프라인 스레드는 `threading.Event` 에서 실제로 멈춘다. `--ui-timeout`
(기본 1800초) 을 넘기면 빈 문자열을 돌려주고 계속 진행한다.

---

## 답을 주고받는 규약

답은 항상 **짧은 문자열 하나**다.

| 문자열 | 단계 | 세 가지 중 |
| --- | --- | --- |
| `""` | PLAN·VERIFY | ① Qwen 결과 그대로 |
| `a <의견>` | PLAN·VERIFY | ② Qwen 결과 + 내 의견 |
| `x <지시>` | PLAN | ③ Qwen 계획 버리고 내 지시만 |
| `keep` / `revert` / `done` `[이유]` | VERIFY | ③ Qwen 판정 버리고 내 판정 |
| `s` | PLAN | 세 가지 밖 — 라운드 취소 |

**터미널에서 타이핑하는 문자열과 완전히 같다.** 그래서 답을 해석하는 코드가
`pipeline.py` 한 곳뿐이고, UI와 터미널의 동작이 갈릴 여지가 없다.

버튼은 값에 `@text` 플레이스홀더를 갖고 브라우저가 입력창 내용으로 치환한다.
`a @text` + "표 정렬 먼저" → `a 표 정렬 먼저`.

영역만 예외로 문자열이 아니라 별도 JSON 필드(`region`)로 함께 간다. 좌표를
문자열에 인코딩하는 것보다 낫다.

---

## HTTP 표면

| 라우트 | 용도 |
| --- | --- |
| `GET /` | 페이지 (HTML·CSS·JS 한 덩어리, 외부 리소스 없음) |
| `GET /state` | `{pending, history, finished}` — 브라우저가 1초마다 폴링 |
| `POST /answer` | `{answer, region}` |
| `GET /config` | 현재 개입 설정 |
| `POST /config` | `{verify_mode, plan_interactive}` — 실행 중 전환 |
| `GET /img?p=<상대경로>` | 실행 출력 디렉터리 안의 PNG |
| `GET /favicon.ico` | 204 (브라우저가 항상 요청하므로 콘솔 잡음 제거) |

---

## pending 페이로드 = 화면

UI는 아무 판단도 하지 않는다. **버튼 목록조차 파이프라인이 내려준다.**

| 필드 | 화면 |
| --- | --- |
| `id` | 같은 질문을 다시 그리지 않기 위한 키 (입력 중인 텍스트가 날아가지 않는다) |
| `stage` / `round` | 히스토리 표 분류 |
| `title` | 굵은 제목 |
| `prompt` | 입력 안내 문구 |
| `data` | 모델이 낸 JSON 원문 |
| `image` + `panels` | 주 이미지. **드래그로 영역 지정 가능** |
| `image2` + `image2_label` | 보조 이미지 (영역 확대). 보기 전용 |
| `text` | 입력창 표시 여부 |
| `choices` | 버튼 배열 `{label, value, style}` |
| `enter_value` | Enter 키가 눌렸을 때 보낼 값 |

`choices` 가 데이터라서 버튼을 추가·변경할 때 `ui.py` 를 고칠 필요가 없다.
"Qwen 계획 버리고 내 지시만" 버튼은 `pipeline.py` 의 `choices` 한 줄과 답
파싱 분기 한 곳으로 끝난다.

---

## 실행 중 설정 전환

`--verify` 와 `--interactive` 는 실행 시작값일 뿐이다. UI가 바꾸면 **다음
라운드부터** 적용된다.

```python
# pipeline.py — 생성 시점에 고정하지 않고 라운드마다 읽는다
@property
def verify_mode(self) -> str:
    return getattr(self.prompter, "verify_mode_override", None) or self._base_verify_mode

@property
def interactive(self) -> bool:
    override = getattr(self.prompter, "plan_interactive_override", None)
    return self._base_interactive if override is None else bool(override)
```

`getattr` 기본값을 쓰는 이유: 터미널로 돌 때는 `prompter` 가 `None` 이고, 그
경우 그냥 시작값을 따른다. UI가 없어도 이 코드가 그대로 동작한다.

서버는 `announce_defaults()` 로 시작값을 전달받아 UI에 현재 설정을 표시한다.
`verify_mode` 로 허용되지 않는 값이 오면 무시한다.

`summary.json` 에는 `verify_mode`(시작)와 `verify_mode_final`(종료 시점)이 모두
남아서, 중간에 방식이 바뀐 실행을 나중에 구분할 수 있다.

## 세 가지 상태는 한 곳에만 있다

사람 개입 모델은 **세 상태뿐**이다.

| 상태 | 뜻 | 남는 필드 |
| --- | --- | --- |
| `model` | Qwen 결과 그대로 | — |
| `model+operator` | Qwen 결과 유지 + 사람 참고 의견 | `operator_note` |
| `operator` | Qwen 결과 폐기, 사람 결정 | `operator_instruction`/`operator_override` + `model_plan`/`model_decision` |

화면의 버튼도 이 세 개에 ①②③ 번호가 붙어 있어서, 버튼 수(VERIFY는 판정 값을
골라야 해서 5개)와 무관하게 선택지가 세 개라는 것이 읽힌다.

이 상태는 결정하는 지점에서 `planned_by` / `verified_by` 에 **기록**되고, 읽을
때는 항상 한 함수를 거친다.

```python
judged_by(payload)                 # -> model | model+operator | operator
touched_by_operator(plan, verdict) # 사람이 관여했나
deciding_words(payload, reason)    # 판정한 쪽의 이유 (버려진 쪽 것이 아님)
```

**필드 조합으로 다시 추론하지 않는다.** 예전에는 이 판단이 8곳에서 각자
이루어졌고 — `plan.get("planned_by","model") != "model"` 이 5개 복사본, 그와
별개인 4항 OR 하나, 태그와 근거를 다른 필드에서 가져오는 곳 하나 — 실제로
발생한 잘못된 귀속 버그는 모두 그 복사본 중 하나가 다른 것과 어긋난 결과였다.

`tests/test_offline.py` 의 14번 그룹이 3상태 × 두 단계를 전부 열거해서
`judged_by` · `operator` 플래그 · history 줄의 태그 · 실려 가는 근거가 서로
일치하는지 확인한다. 상태를 늘리거나 소비자를 추가하면 이 표에 줄을 넣으면 된다.

## 영역 좌표 변환

`side_by_side()` 가 합성 이미지와 함께 패널 박스를 돌려준다.

```python
path, panels = side_by_side([("1. SOURCE", src), ("2. CURRENT RENDER", png)], out)
# panels == [{"label": "1. SOURCE", "x": 0, "y": 26, "width": 800, "height": 561}, ...]
```

브라우저에서 일어나는 일:

1. 표시 크기와 `naturalWidth` 비율로 마우스 좌표를 **합성 이미지 좌표**로 환산
2. 드래그 **시작점**이 어느 패널 안인지 판정
3. 그 패널 기준 **비율(0~1)** 로 정규화

파이썬 쪽에서는 `crop_normalized(image, rect)` 가 그 비율을 아무 크기의
이미지에나 적용한다. 그래서 원본 스캔 2480px과 렌더 800px에 같은 rect를 쓸 수
있다.

서버는 받은 좌표를 그대로 믿지 않는다 — `0~1` 범위와 `w/h > 0` 을 확인하고,
어긋나면 **저장하지 않고 버린다.**

---

## 안전장치

| 위험 | 처리 | 코드 |
| --- | --- | --- |
| 경로 탈출 | resolve 후 출력 디렉터리 내부 + `.png` 확장자만 | `ReviewServer.read_image` |
| 잘못된 좌표 | 범위·부호 검증, 실패 시 폐기 | `ReviewServer._clean_region` |
| 답이 안 옴 | 타임아웃 후 입력 없음으로 처리 → Qwen 판정이 그대로 선다 | `ReviewServer.ask` |
| 인증 없음 | 기본 localhost 바인딩, 열 때 경고 출력 | `run.py` |
| UI 없이 실행 | `prompter=None` → 완전히 동일 동작 | `Pipeline._input` |

---

## 파일

| 파일 | 역할 |
| --- | --- |
| `ui.py` | `ReviewServer`, HTTP 핸들러, 페이지 한 덩어리 |
| `pipeline.py` | `review_plan` / `review_verify` 가 `choices` 를 선언하고 답을 해석 |
| `utils.py` | `side_by_side` (패널 박스 포함), `crop_normalized` |
| `run.py` | `--ui`, `--ui-host`, `--ui-public-host`, `--ui-port`, `--ui-timeout` |
| `prompts.py` | 사람 개입이 있는 실행에 붙는 계약 블록, 영역 설명 블록, history·실패목록 블록 |

---

## 고치는 법

**버튼을 추가한다** → `pipeline.py` 의 해당 `ctx["choices"]` 에 한 줄 넣고, 그
값을 답 파싱 분기에 추가한다. `ui.py` 는 건드리지 않는다.

**실행 중 바꿀 설정을 추가한다** → `ReviewServer` 에 `*_override` 속성과
`config()` / `set_config()` 항목을 넣고, 파이프라인 쪽에서 `getattr` 로 읽는
property 를 만든다. UI 헤더의 컨트롤은 `VERIFY_MODES` 처럼 배열 하나로
정의된다.

**새 단계에 개입 지점을 만든다** → 그 단계에서 `self._ask(프롬프트, ctx)` 를
부르고 `ctx` 에 `title` / `data` / `image` / `choices` 를 채운다.

**이미지를 더 보여준다** → 지금은 주 이미지 + 보조 이미지 두 장까지다. 세 장
이상이 필요하면 페이로드를 배열로 바꾸고 렌더 루프를 돌려야 한다.

**폴링을 실시간으로 바꾼다** → 필요하면 `/state` 를 long-poll 로 바꾸는 것이
WebSocket을 들이는 것보다 변경이 작다. 표준 라이브러리 안에서 된다.

---

## 테스트

```bash
python3 tests/test_offline.py
```

UI 관련해 덮는 것:

* 패널 박스가 계산되는지
* 출력 디렉터리 밖 파일과 PNG 아닌 파일을 거부하는지
* 질문 게시 → HTTP 응답 → 파이프라인 수신까지 왕복
* 범위를 벗어난 좌표를 버리는지
* 비율 rect가 크기가 다른 이미지에 비례해 적용되는지
* 영역을 지정한 라운드에서 ACTION이 이미지 4장을 받고 `compare_region.png` 가
  생기는지
* `0.0.0.0` 바인딩이 접속 가능한 주소를 광고하는지, 컨테이너 내부 주소면
  안내가 붙는지, `--ui-public-host` 가 바인딩을 옮기지 않는지
* 실행 중 설정 전환이 파이프라인에 즉시 반영되는지, 잘못된 값을 무시하는지
* 영역 지정이 ACTION의 확대 crop 2장과 `compare_region.png` 로 이어지는지

전체는 117개 검사이고 UI·영역 관련이 그 중 두 그룹이다.

브라우저 자체 동작(버튼 클릭, 드래그)은 자동 테스트에 없다. Playwright로 직접
띄워 확인했고, 그 과정에서 실제 버그 두 개가 나왔다 — 인라인 `onclick` 의
따옴표가 HTML 속성을 끊어 **모든 버튼이 동작하지 않던 것**, 그리고 끊긴 연결이
`urllib.error.URLError` 가 아니라서 재시도를 뚫고 빌드를 죽이던 것. 유닛
테스트만으로는 잡히지 않는 종류다.
