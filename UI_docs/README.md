# docgen 검토 UI

문서 PNG를 HTML로 복원하는 루프에서 **사람이 끼어드는 화면**이다.

```bash
python run.py build sample.png --ui
```

출력되는 주소를 브라우저로 열어둔다. 표준 라이브러리만 쓴다 — 서버 프레임워크를
새로 깔지 않는다. UI를 안 켜면 루프는 완전 자동으로 돈다.

구조와 확장 방법은 [ARCHITECTURE.md](ARCHITECTURE.md).

### 처음 몇 분은 화면이 비어 있다

주소를 열어도 바로 물어보지 않는다. 첫 HTML을 만드는 **bootstrap 3단계**
(구조 → 육안 대조 → 블록별 채우기) 에는 **개입 지점이 없다.** 그 단계가 끝나고
첫 라운드의 PLAN이 나올 때 첫 질문이 뜬다.

bootstrap은 LLM 호출을 `1 + 1~2 + 블록 수` 만큼 한다 — 블록 6개면 8~9회다.
그동안 뭘 하고 있는지는 터미널 로그(`BOOTSTRAP 1/3`, `2/3`, `3/3`)와
`out/<이름>/rounds/bootstrap_skeleton.png` 로 확인한다.

---

## 전부 이 여섯 개다

루프는 라운드마다 두 번 멈춘다 — **PLAN**(무엇을 고칠지)과 **VERIFY**(고쳐졌는지).
각 단계에서 할 수 있는 일은 세 개뿐이다.

| | 하고 싶은 것 | PLAN 버튼 | VERIFY 버튼 |
| --- | --- | --- | --- |
| ① | Qwen 결과 그대로 | `① Qwen 계획대로` | `① Qwen 판정대로` |
| ② | Qwen 결과 + 내 의견 | `② 참고로 첨부 (계획 유지)` | `② 참고로 첨부 (판정 유지)` |
| ③ | Qwen 결과 버리고 내 결정 | `③ Qwen 계획 버리고 내 지시만` | `③ 내 판정: keep/revert/done` |

**중간 단계는 없다.** ②면 Qwen 결과가 남고, ③이면 안 남는다. 그 사이는 없다.

어느 쪽이든 **Qwen이 뭐라고 했는지 화면에서 먼저 읽은 다음** 고른다. ③을 쓰려고
별도 옵션을 켤 필요가 없다 — `--ui` 하나면 된다.

---

## 목차

* [PLAN — 무엇을 고칠지](#plan--무엇을-고칠지)
* [VERIFY — 고쳐졌는지](#verify--고쳐졌는지)
* [함께 쓰는 것](#함께-쓰는-것) — 영역 지정 · 실행 중 전환 · 운영자 메모 · 다른 PC 접속
* [기록되는 것](#기록되는-것)
* [알아둘 제약](#알아둘-제약)

---

# PLAN — 무엇을 고칠지

화면에 **원본 · 현재 렌더** 두 장과 Qwen이 낸 PLAN JSON 원문이 뜬다.

![PLAN 화면](images/01-plan.png)

## ① Qwen 계획대로

`① Qwen 계획대로` 를 누른다(Enter도 같다). 그대로 ACTION으로 넘어간다.

* `planned_by: model`
* 이것만 누르며 8라운드를 돌리면 `--ui` 없이 돌린 것과 결과가 같다. **화면을 보는
  값은 여기 있다** — 매 라운드 Qwen이 무엇을 보고 무엇을 고치려 하는지 보인다.

## ② 참고로 첨부 (계획 유지)

입력창에 적고 `② 참고로 첨부 (계획 유지)` 를 누른다.

![개입 버튼](images/02-plan-intervene.png)

* Qwen 계획은 **그대로 남는다.** ACTION이 계획과 의견을 함께 본다.
* `planned_by: model+operator`, `operator_note: "..."`

## ③ Qwen 계획 버리고 내 지시만

지시를 적고 `③ Qwen 계획 버리고 내 지시만` 을 누른다.

* Qwen 계획을 **버린다.** 적은 지시가 그 라운드의 목표가 된다.
* `planned_by: operator`, `goal` 이 사람 지시로 바뀜, `operator_instruction`
* Qwen의 원래 계획은 `model_plan` 에 기록만 남고, 프롬프트에 "`model_plan` 은
  참고용이니 실행하지 말라"가 들어간다.

## 그 밖에

`라운드 건너뛰기` 는 이 라운드를 통째로 넘긴다. 세 가지 중 하나가 아니라
취소이고, 그래서 "이미 시도해서 안 된 것" 목록에도 들어가지 않는다.

---

# VERIFY — 고쳐졌는지

화면에 **전체 페이지(원본 · 수정 전 · 수정 후)** 와 Qwen이 낸 VERIFY JSON이 뜬다.
영역을 지정한 라운드면 **그 영역 확대 비교**가 위에 하나 더 붙는다.

![VERIFY 화면](images/04-verify-with-region.png)

## ① Qwen 판정대로

`① Qwen 판정대로`(Enter). Qwen의 keep / revert / done이 그대로 적용된다.
`verified_by: model`.

## ② 참고로 첨부 (판정 유지)

**판정은 바뀌지 않는다.** 의견만 붙는다.

* `verified_by: model+operator`, `operator_note: "..."`
* 그 의견은 **다음 라운드 PLAN의 history로 실려 간다.** 판정을 뒤집지 않고 방향만
  잡아주고 싶을 때 쓰는 경로다.

## ③ 내 판정: keep / revert / done

Qwen 판정을 **버리고** 사람 판정으로 간다. 판정 값을 골라야 하므로 버튼이 세 개지만
**세 가지 중 하나**다.

* `verified_by: operator`, `operator_override: <고른 값>`
* Qwen의 원래 판정은 `model_decision` 에 기록만 남는다.
* 입력창에 이유를 적고 누르면 그 이유가 다음 PLAN으로 실려 간다.

## 판정이 다음 라운드로 전달되는 것

**사람이 적은 이유도 들어간다.** keep이든 revert든 마찬가지다.

| 상황 | 다음 PLAN이 보는 줄 |
| --- | --- |
| ③ 사람 판정 + 이유 | `-> keep [operator] why: 표 폭은 맞았지만 여백이 남았다; next: 제목 자간` |
| ③ 사람 판정, 이유 생략 | `-> keep [operator] next: 제목 자간` |
| ② 판정 유지 + 첨언 | `-> keep [model+operator] operator: 우측 정렬 남음` |
| ① Qwen 단독 (revert) | `-> revert [model] why: table became too wide; next: header rule` |
| ① Qwen 단독 (keep) | `-> keep [model] next: notes font` |

읽는 규칙 세 가지:

* **`why:` 는 항상 판정한 쪽의 이유다.** 대괄호가 `[operator]` 면 `why:` 도 사람이
  적은 것이다. ③으로 버려진 Qwen의 근거는 여기 오지 않는다
  (`verify.json` 의 `model_decision` 에 기록으로만 남는다).
* **`why:` 는 revert 일 때 항상, keep 일 때는 사람이 이유를 적었을 때만** 붙는다.
  되돌린 이유는 같은 시도 반복을 막으니 항상 필요하고, 성공 사유는 사람이 일부러
  적었을 때만 신호가 된다.
* **`operator:` 는 ②일 때만** 나온다. ③이면 그 사람 말이 이미 `why:` 에 있다.

되돌려지거나 적용 실패한 접근은 최근 3라운드 창을 넘어서도 **실행 내내 별도 목록**
으로 유지되어 PLAN에 전달된다. `summary.json` 의 `failed_attempts` 에서 볼 수 있다.

---

# 함께 쓰는 것

## 영역만 지정해서 고치기

이미지 위를 **드래그**하면 그 영역만 고치라고 지정할 수 있다. 세 버튼 중 어느 것과도
함께 쓸 수 있다.

![영역 지정](images/03-plan-region-select.png)

```
선택 영역: 1. SOURCE  x 9%, y 18%, 폭 82%, 높이 32%  [선택 해제]
```

지정하면 그 라운드에서:

1. `plan.json` 에 `operator_region` (비율 좌표 0~1)이 저장된다.
2. **ACTION이 받는 이미지가 4장이 된다** — 원본 전체, 현재 렌더 전체, 그리고 그
   영역을 확대한 crop 2장.
3. 프롬프트에 좌표와 함께 "이 crop이 보여주는 것만 고치고 나머지는 건드리지 말라"가
   들어간다.
4. VERIFY 화면에 **그 영역 확대 비교**가 추가로 뜬다 — 전체만 보면 작은 영역이
   고쳐졌는지 알 수 없다.

좌표를 비율로 저장하는 이유는 **원본 스캔이 2480px이고 렌더가 800px이어도 같은 영역
을 가리켜야** 하기 때문이다.

## 실행 중에 개입 방식 바꾸기

화면 오른쪽 위 스위치 두 개. **다시 실행할 필요 없다.**

![실행 중 전환](images/05-switch-verify-mode.png)

| 스위치 | 선택 | 뜻 |
| --- | --- | --- |
| VERIFY 개입 | `멈추고 묻기` | 기본. Qwen 판정을 보여주고 멈춘다 |
| | `묻지 않기` | Qwen 판정대로 진행, 멈추지 않는다 |
| PLAN 개입 | `멈추고 묻기` / `묻지 않기` | PLAN에서 멈출지 |

Qwen은 어느 쪽이든 두 단계 모두 판정한다. 이 스위치는 **루프가 나를 위해 멈추는지**
만 정한다. 바꾸면 다음 라운드부터 적용되고, 진행 중인 질문에는 영향이 없다.

둘 다 끄면 그 시점부터 완전 자동이다. 브라우저는 진행 상황을 보는 창이 된다.

## 운영자 메모 (UI 없이도)

```bash
python run.py build sample.png --note "표 정렬이 이 문서에서 가장 중요하다"
python run.py build sample.png --notes-file notes.txt
```

메모는 PLAN과 VERIFY 프롬프트에 함께 들어간다. "지금 무엇이 잘못됐다"는 서술이 아니라
**이 문서에서 무엇이 중요한지**를 적는 자리다. 프롬프트에 "메모를 현재 문제의 설명으로
받아들이지 말고 이미지를 먼저 판단하라"가 붙어 있어서, 메모가 눈앞의 렌더 판단을
덮어쓰지 않는다.

## 다른 PC에서 접속 (GPU 서버에서 돌리고 윈도우에서 보기)

```bash
python run.py build sample.png --ui --ui-host 0.0.0.0 --ui-port 8900
```

출력되는 주소는 `0.0.0.0` 이 아니라 이 머신이 스스로 짐작한 IP로 찍힌다. 그런데
**도커 컨테이너 안에서 돌리면 그 짐작이 틀린다** — `172.17.0.2` 같은 컨테이너
내부 주소가 나오고, 윈도우에서는 그 주소로 열리지 않는다. 두 가지가 필요하다:

1. 컨테이너가 그 포트를 host로 내보내고 있어야 한다 (`docker run -p 8900:8900`
   또는 `--network host`). 이게 없으면 어떤 주소를 넣어도 안 열린다.
2. 브라우저에는 컨테이너가 아니라 **서버 주소**를 넣는다
   (예: `http://10.167.129.230:8900/`).

`--ui-public-host` 를 주면 그 주소가 출력 줄에 바로 찍혀서 복사해 쓸 수 있다.
바인딩은 `--ui-host` 그대로고, 찍히는 주소만 바뀐다:

```bash
python run.py build sample.png --ui \
  --ui-host 0.0.0.0 --ui-port 8900 --ui-public-host 10.167.129.230
```

환경변수 `DOCGEN_UI_PUBLIC_HOST` 도 같은 스위치다. 컨테이너 내부 주소가 찍힐
때는 실행 시점에 위 내용이 경고로 함께 출력된다.

인증이 없으니 사내망에서만 쓴다. 그래서 기본값을 localhost로 두고 `--ui-host` 를
명시해야 열리게 했다. 이미지는 그 실행의 출력 디렉터리 안에 있는 PNG만 서빙한다.

## 지난 라운드 기록

![지난 라운드 기록](images/06-history.png)

아래 카드에 무엇을 보냈는지 쌓인다. 영역까지 지정한 답에는 `[영역 지정]` 이 붙는다.

---

# 기록되는 것

사람이 개입한 것과 Qwen이 스스로 한 것은 **반드시 따로 남는다.** 안 그러면
"Qwen이 이 작업을 할 수 있나"라는 판단이 오염된다.

| 세 가지 | `planned_by` / `verified_by` | 함께 남는 것 |
| --- | --- | --- |
| ① Qwen 결과 그대로 | `model` | — |
| ② Qwen 결과 + 내 의견 | `model+operator` | `operator_note` |
| ③ Qwen 결과 버리고 내 결정 | `operator` | `operator_instruction` / `operator_override`, `model_plan` / `model_decision` |

| 파일 | 필드 |
| --- | --- |
| `rounds/rNN/plan.json` | `planned_by`, `operator_note`, `operator_instruction`, `operator_region`, `model_plan` |
| `rounds/rNN/verify.json` | `verified_by`, `model_decision`, `operator_override`, `operator_note` |
| `summary.json` | `verify_mode`, `verify_mode_final`, `operator_interventions`, `operator_rounds`, `skipped`, `failed_attempts`, `kept_line_changes`, 라운드별 `operator` · `mode` · `changed_lines` |
| `rounds/bootstrap_stages.json` | bootstrap 3단계 결과 (개입 없음, 참고용) |

모델 단독 성능을 보려면 `--ui` 없이 돌린 실행을 보고, 개입이 섞인 실행에서는
`operator: true` 라운드를 빼고 읽는다.

라운드 폴더에 함께 남는 이미지:

| 파일 | 언제 |
| --- | --- |
| `plan_view.png` | 사람이 개입하는 실행 (원본 · 현재 렌더) |
| `compare.png` | 항상 (원본 · 수정 전 · 수정 후) |
| `compare_region.png` | 영역을 지정한 라운드만 |

bootstrap 산출물은 라운드 폴더가 아니라 `rounds/` 바로 아래에 있다 —
`bootstrap_skeleton.png`(구조만), `bootstrap_fill_01.png`~(블록을 채울 때마다),
`bootstrap.png`(첫 초안 완성). **첫 질문이 이상하게 느껴지면 여기를 먼저 본다.**

세 상태는 코드에서 한 함수(`judged_by`)로만 읽는다. 여섯 가지를 전부 열거해 태그 ·
플래그 · 실려 가는 근거가 서로 맞는지 확인하는 테스트가 있다
([ARCHITECTURE.md](ARCHITECTURE.md#세-가지-상태는-한-곳에만-있다)).

---

# 알아둘 제약

* **bootstrap에는 개입할 수 없다.** 사람이 끼어드는 곳은 라운드의 PLAN과 VERIFY
  두 곳뿐이다. 첫 초안이 마음에 안 들면 뜨는 첫 PLAN에서 ③으로 직접 지시한다.
  초안 만드는 방식 자체를 바꾸려면 실행을 멈추고 `--bootstrap single` 이나
  `config.toml` 의 `[bootstrap]` 값을 조정해야 한다.
* **영역 지정은 요청이지 강제가 아니다.** APPLY가 patch가 그 영역 안을 건드렸는지
  검증하지 않는다. Qwen이 영역 밖을 고쳐도 통과한다.
* **영역 좌표는 "같은 상대 위치"지 "같은 내용"이 아니다.** 렌더의 전체 높이가 원본과
  많이 다른 초기 라운드에서는 같은 비율이 다른 내용을 가리킬 수 있다. Qwen은 전체
  이미지도 함께 받으므로 오해로 이어지진 않지만, 확대 crop이 엉뚱해 보이면 이 이유다.
* **영역은 한 번에 하나.** 그 라운드에만 유효하고 다음 라운드는 다시 지정한다.
* **지난 라운드로 돌아가 재검토할 수 없다.** 아래 카드는 무엇을 보냈는지만 보여준다.
* **동시 접속에 잠금이 없다.** 두 명이 열면 먼저 누른 답이 먹는다.
* **터치는 안 된다.** 드래그가 마우스 이벤트 기반이라 모바일에서는 영역 지정이
  동작하지 않는다.
* **실행 시작·중단은 CLI에서** 한다. 개입 방식은 실행 중에 바꿀 수 있지만 라운드
  수나 대상 문서는 바꿀 수 없다.
* **인증이 없다.** `--ui-host` 로 열 때는 사내망 안이라는 전제가 필요하다.
* 답이 `--ui-timeout`(기본 1800초) 안에 오지 않으면 입력 없음으로 처리한다 —
  Qwen 판정이 그대로 선다.
