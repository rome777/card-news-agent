# API 명세

브라우저 화면(`cardnews/static/app.js`)과 웹 서버(`cardnews/server.py`) 사이의 약속입니다. PRD의 「웹 API 계약 초안」을 이 구현에 맞춰 확정했습니다.

- 주소: `http://127.0.0.1:8765` (127.0.0.1에만 열림)
- 모든 요청·응답 본문은 JSON(UTF-8)입니다. 파일 내려받기만 예외입니다.
- 시각은 서버 PC의 시간대를 붙인 ISO 8601 문자열입니다. 예: `2026-10-23T14:05:11+09:00`

## 연결 방식 한눈에 보기

```
브라우저                       웹 서버                         작업 스레드
  POST /api/projects ────────▶ 프로젝트 저장
  POST …/research ───────────▶ run 생성 ──────────────────────▶ 엔진: 넓은 조사
  GET …/{id} 1.5초마다 ──────▶ 저장된 상태 그대로               └ 후보 저장 → 후보 고르기 대기
  POST …/selection ──────────▶ 선택 저장 + run ───────────────▶ 엔진: 심층 조사
                                                                 ├ need_input → 질문 저장, 대기(호출 종료)
  POST …/answers ────────────▶ 답 저장, 같은 run 재개 ─────────▶ 엔진: 같은 세션으로 이어서
                                                                 └ 편집 방향 → 자동으로 스토리보드 run
  POST …/storyboard/revise ──▶ 변경 요청 저장 + run ──────────▶ 엔진: 스토리보드 다시 쓰기
  POST …/approve(storyboard) ▶ 카드 만들기 + run ─────────────▶ 그림 작업자(agy) → 렌더러(PNG)
  POST …/cards/{c}/image ────▶ run ───────────────────────────▶ 그 카드만 그림 다시 → PNG
  POST …/cards/{c}/edit ─────▶ 글·스타일 저장 + run ──────────▶ 그 카드만 PNG 다시(그림 유지)
  POST …/revisions ──────────▶ run ───────────────────────────▶ 엔진: 고른 카드 수정 → 필요하면 그림 → PNG
  POST …/approve(final) ─────▶ 내보내기 파일 만들기
  GET  …/exports, …/files/… ─▶ 내려받기
```

- 긴 작업은 HTTP 요청 안에서 기다리지 않습니다. 서버는 run ID를 바로 돌려주고, 화면은 `GET /api/projects/{id}`를 **1.5초마다 조회(폴링)** 해서 그립니다.
- 화면은 `project.version`이 바뀌면 전체를 다시 그리고, 진행 내역만 늘었을 때는 상태 줄과 내역만 바꿉니다. 고르는 중인 체크박스와 입력 중인 글은 지워지지 않고, 버튼이 눌리는 도중에 바뀌지도 않습니다.
- 모든 상태는 `data/app.db`(SQLite)에 있습니다. 새로고침이나 서버 재시작 뒤에도 후보·선택·질문·기획·카드가 그대로 돌아옵니다.

## 공통 규칙

### 중복 요청 (requestId)

상태를 바꾸는 요청(POST)은 `requestId`를 함께 보냅니다. 화면은 동작 하나마다 ID를 하나 만들고, 응답을 받을 때까지 같은 ID로 다시 보냅니다.

| 경우 | 서버 동작 |
|---|---|
| 처음 보는 ID | 처리하고 응답을 저장 |
| 같은 ID · 같은 입력 | 다시 처리하지 않고 저장해 둔 응답을 그대로 반환 |
| 같은 ID · 다른 입력 | `409 request_conflict` |

ID는 프로젝트 범위에서 구분합니다(프로젝트 만들기는 전체 범위). 서버가 오류로 거절한 요청은 저장하지 않습니다. 예외로 `POST /api/runs/{id}/cancel`과 `POST …/exports/verify`는 몇 번 보내도 결과가 같아서 requestId가 없습니다.

### 버전 (version)

선택·승인·카드 고치기는 화면이 보고 있던 `project.version`을 함께 보냅니다. 그사이 프로젝트가 바뀌었으면 `409 stale_version`으로 거절합니다. 오래된 화면에서 승인하거나 고치는 일을 막기 위함입니다.

### 오류 응답

```json
{ "error": { "code": "stale_question", "message": "지난 질문에 대한 답변이라 반영하지 않았습니다.", "nextAction": "새로고침해서 지금 질문에 답하세요." } }
```

| HTTP | code | 뜻 |
|---|---|---|
| 400 | `invalid_request` | 필수 값 누락·형식 오류 |
| 400 | `invalid_selection` | 후보가 0개·4개 이상, 없는 후보·카드 |
| 400 | `invalid_answer` | 선택지에 없는 답, 빈 직접 입력 |
| 404 | `not_found` | 없는 프로젝트·실행·카드·파일 |
| 409 | `request_conflict` | 같은 requestId로 다른 내용 |
| 409 | `invalid_stage` | 지금 단계에서 할 수 없는 동작 |
| 409 | `stale_version` | 화면이 본 프로젝트 버전이 오래됨 |
| 409 | `no_pending_question` | 기다리는 질문이 없음(이미 답함) |
| 409 | `stale_question` | 지난 질문(ID·버전 불일치)에 대한 답 |
| 409 | `not_ready` | 완성되지 않은 카드가 있어 최종 승인 불가 |
| 409 | `not_approved` | 최종 승인한 결과가 없어 내려받기 불가 |
| 429 | `daily_limit` | 공개 모드의 하루 새 카드뉴스 상한(전체 또는 접속 주소별)을 넘음 |

작업 자체의 실패는 HTTP 오류가 아니라 프로젝트 상태(`status`)와 `project.error`, 카드의 `image.error`로 전달합니다. 오류에는 `code`, `message`, `next_action`(다음 행동)이 있습니다.

| error.code | 뜻 | 어디에 |
|---|---|---|
| `search_failed` | 검색 실패, 또는 엔진이 실제 검색을 하지 않음 | 프로젝트 |
| `auth_required` | Claude CLI 또는 agy 로그인 필요 | 프로젝트·카드 |
| `rate_limited` | 사용 한도 초과(Gemini·OpenAI 429, Claude 사용 한도, agy 429 RESOURCE_EXHAUSTED, 공개 모드의 하루 그림 상한) | 프로젝트·카드 |
| `billing_limit` | Gemini·OpenAI 계정의 결제 미등록·크레딧 부족·지출 한도 도달. 공개 화면에 계정 정보가 나가지 않게 제공사 원문은 보여 주지 않음 | 프로젝트·카드 |
| `engine_unavailable` | claude·agy 실행 파일을 찾지 못함 | 프로젝트·카드 |
| `permission_denied` | agy가 이미지 도구 권한을 받지 못함 | 카드 |
| `image_missing` | agy가 성공이라고 했지만 새 그림 파일이 없음 | 카드 |
| `image_invalid` | 그림 파일이 이미지로 열리지 않거나 너무 작음 | 카드 |
| `image_blocked` | 그림 모델이 안전 기준 등으로 그림 대신 글만 돌려줌(OpenAI는 안전 기준 오류) | 카드 |
| `image_failed` | 그 밖의 agy 실패 | 카드 |
| `invalid_output` | 엔진 출력이 정해진 형식이 아님(카드 수·질문 형식 등) | 프로젝트 |
| `session_missing` | 저장한 엔진 세션을 찾지 못함. 저장된 내용으로 새 세션을 열어 한 번 더 시도함 | 진행 내역 |
| `engine_failed` | 그 밖의 Claude CLI 실패 | 프로젝트 |
| `timeout` | 시간 제한(Claude 15분, agy 7분) 초과 | 프로젝트·카드 |
| `cancelled` | 사용자가 취소 | 프로젝트 |
| `interrupted` | 서버 재시작으로 중단 | 프로젝트 |
| `internal` | 앱 내부 오류 | 프로젝트 |

카드 단위 작업(`card_image`·`render`·`revise`)이 실패하면 프로젝트는 검수 화면(`review`, `waiting_for_user`)에 그대로 머물고, `project.error.scope`가 `"card"`가 됩니다.

## 상태

### 프로젝트 단계 (`project.stage`)

`created` → `research`(넓은 조사) → `select`(후보 고르기) → `deep_research`(심층 조사와 편집 판단) → `editorial`(잠깐) → `storyboard`(스토리보드 승인) → `production`(그림·글자 제작) → `review`(검수·수정) → `final`(최종 승인)

`final`에서 카드를 고치면 `review`로 돌아가고 승인이 풀립니다.

### 프로젝트 상태 (`project.status`)

| 값 | 뜻 |
|---|---|
| `idle` | 할 일 없음 (최종 승인 뒤 등) |
| `running` | 작업 진행 중 |
| `waiting_for_user` | 사용자를 기다림. **실패가 아님** (후보 고르기, 질문, 스토리보드 승인, 검수) |
| `failed` / `interrupted` / `cancelled` | 실패·서버 재시작으로 중단·취소. `project.error`에 이유와 다음 행동 |

### 실행 (`run`)

| kind | 하는 일 | 프로젝트 단계 |
|---|---|---|
| `research` | 넓은 조사 | research |
| `deep_research` | 심층 조사·편집 방향(질문이 생기면 대기) | deep_research |
| `storyboard` | 카드별 기획 | storyboard |
| `images` | 그림이 없는 카드의 그림 만들기 + PNG | production |
| `card_image` | 카드 하나의 그림 다시 만들기(agy 또는 Claude 도형) + PNG | review |
| `render` | 글자만 고친 카드의 PNG 다시 만들기 | review |
| `revise` | 고른 카드를 요청대로 고치기 | review |

상태: `queued` → `running` → `succeeded` / `waiting_for_user` / `failed` / `cancelled` / `interrupted`. 질문이 생기면 실행은 `waiting_for_user`로 멈추고, 답변이 오면 **같은 run ID**가 다시 `queued` → `running`이 됩니다. 서버가 켜질 때 `queued`·`running`이던 실행은 `interrupted`가 되고, 그때 남아 있던 claude·agy 프로세스는 정리됩니다.

## API

### 조사·선택·질문

| 요청 | 입력 | 응답 |
|---|---|---|
| `GET /api/config` | — | `{ engine, mock, images, public }` (`engine`: gemini·openai·claude·mock, `images`: gemini·openai·agy·mock) |
| `GET /api/projects` | — | `{ projects: [{ id, topic, stage, status, updated_at, mock, engine }] }` (최근에 바뀐 순). **공개 모드에서는 빈 목록** — 화면이 브라우저에 기억한 자기 프로젝트 ID로 하나씩 불러옴 |
| `POST /api/projects` | `topic`(1~100자), `periodDays`(1~30, 기본 7), `requestId` | `{ projectId }`. 조사는 시작하지 않음. 공개 모드에서 하루 상한(전체 30개, 접속 주소당 1개)을 넘으면 `429 daily_limit`. 환경 변수 `CARDNEWS_UNLIMITED_IPS`의 주소는 주소당 상한에서 빠짐. `--trust-proxy`면 접속 주소를 `CF-Connecting-IP`·`True-Client-IP`·`X-Forwarded-For` 첫 값 순으로 읽음. 같은 `requestId` 재전송은 세지 않고, 센 숫자는 SQLite `usage` 표에 남음 |
| `GET /api/projects/{id}` | — | `{ project, runs }` (아래 「프로젝트 문서」, runs는 최근 5개) |
| `POST /api/projects/{id}/research` | `requestId`, `periodDays`(선택: 바꿔서 다시 조사) | `{ runId, status, alreadyRunning }`. 후보를 고른 뒤에는 `409 invalid_stage`. 조사 중이면 새로 시작하지 않고 진행 중인 run을 돌려줌 |
| `POST /api/projects/{id}/selection` | `candidateIds`(1~3개), `version`, `requestId` | `{ selection, runId, status }`. 선택을 저장하고 심층 조사 시작 |
| `POST /api/projects/{id}/answers` | `questionId`, `version`, `answer: { choice, text }`, `requestId` | `{ accepted, status, runId }`. 직접 입력은 `choice: "__free__"`와 `text`(500자 이하) |
| `POST /api/projects/{id}/retry` | `requestId` | `{ runId, status, retryOf }`. `failed`·`interrupted`·`cancelled`일 때 마지막 실행과 같은 종류·같은 입력으로 다시. 저장된 선택·답변·기획·이미 만든 카드는 그대로 씀 |
| `POST /api/runs/{runId}/cancel` | — | `{ runId, status }`. 진행 중이면 하위 프로세스까지 정리 |

답변 규칙: 같은 requestId로 두 번 오면 저장된 응답을 돌려주고 작업은 한 번만 이어 갑니다. 다른 requestId로 다시 답하면 `409 no_pending_question`, 지난 질문에 답하면 `409 stale_question`입니다.

### 스토리보드

| 요청 | 입력 | 응답 |
|---|---|---|
| `POST /api/projects/{id}/storyboard/revise` | `instruction`(1~500자), `version`, `requestId` | `{ runId, status }`. 변경 요청을 `storyboard_feedback`에 쌓고 스토리보드를 다시 씀 |
| `POST /api/projects/{id}/approve` | `stage: "storyboard"`, `version`, `requestId` | `{ stage: "production", runId }`. 스토리보드로 카드를 만들고 그림 제작 시작. **승인 전에는 그림을 만들지 않음** |

### 제작·검수

| 요청 | 입력 | 응답 |
|---|---|---|
| `POST /api/projects/{id}/images` | `requestId` | `{ runId, status, cards }`. 그림이 없는 카드만 다시 만듦. 완성된 카드는 건드리지 않음 |
| `POST /api/projects/{id}/cards/{cardId}/image` | `tool: "default" \| "claude"`, `requestId` | `{ runId, status }`. 그 카드만 카드의 `scene`으로 그림을 다시 만듦. `default`는 서버를 켤 때 고른 그림 작업자(gemini·openai·agy·mock, 예전 값 `"agy"`도 같은 뜻), `claude`는 Claude CLI가 SVG로 그린 도형 그림 |
| `POST /api/projects/{id}/cards/{cardId}/edit` | `title`(1~80자), `body`(400자 이하), `source_line`(200자 이하), `scene`(선택, 400자 이하), `style`, `version`, `requestId` | `{ runId, status }`. 글·스타일·그림 장면만 바꾸고 PNG를 다시 만듦. **그림은 그대로**이고, 바꾼 장면은 다음 '그림 다시 만들기' 때 쓰임 |
| `POST /api/projects/{id}/rerender` | `requestId` | `{ runId, status, cards }`. 그림은 두고 모든 카드의 글자 배치를 지금 레이아웃으로 다시 함. 카드마다 rev가 오르고 최종 승인이 풀림 |
| `POST /api/projects/{id}/revisions` | `cardIds`, `instruction`(1~500자), `scope: "auto" \| "text" \| "image"`, `version`, `requestId` | `{ runId, status }`. 고른 카드만 엔진이 고침. `text`는 글만, `image`는 그림만, `auto`는 엔진이 판단 |

`style`: `{ position: "top"|"bottom", font_scale: 0.7~1.4, text_color: "#RRGGBB", box_color: "#RRGGBB", box_opacity: 0~1 }`

카드를 바꾸는 요청은 `review`·`final` 단계에서, 진행 중인 작업이 없을 때만 됩니다(아니면 `409 invalid_stage`). 카드가 바뀌면 그 카드의 `rev`가 오르고, 바뀌기 전 내용은 `history`에 남습니다. `result_version`이 오르고 최종 승인은 풀립니다. 고르지 않은 카드의 그림·PNG 파일은 바뀌지 않습니다.

### 최종 승인·내려받기

| 요청 | 입력 | 응답 |
|---|---|---|
| `POST /api/projects/{id}/approve` | `stage: "final"`, `version`, `requestId` | `{ stage: "final", resultVersion, export }`. 모든 카드의 그림과 지금 버전의 PNG가 있어야 함(아니면 `409 not_ready`). 자동 검사 경고는 승인을 막지 않음 |
| `GET /api/projects/{id}/exports` | — | `{ resultVersion, approvedAt, zip, cards, documents, ... }`. 최종 승인한 버전이 지금 버전일 때만(아니면 `409 not_approved`) |
| `POST /api/projects/{id}/exports/verify` | — | `{ checks: [{ name, status: "pass"\|"fail"\|"unknown", detail }], checked_at }`. 내보낸 ZIP을 다시 열어 검사하고 결과를 `final.verification`에 저장 |
| `GET /api/projects/{id}/files/{path}` | — | 프로젝트 폴더 안의 파일. `exports/`는 내려받기로 줌. 프로젝트 폴더 밖 경로는 404 |

내보내기 폴더 `exports/v<결과 버전>/`:

| 파일 | 내용 |
|---|---|
| `cards/01_k1.png` … | 카드 PNG(1080×1350), 순서대로 |
| `manuscript.md` | 카드 원고(제목·본문·출처 줄), 조사 기준 시각·기간 |
| `sources.md` | 조사 기준 시각·기간, 카드별 출처 링크, 확인한 사실·발표 주체의 주장·미확인·자료끼리 어긋나는 점 |
| `image_log.json` | 카드별 그림 파일·도구(`agy`/`claude-svg`/`mock`)·만든 시각·프롬프트·agy 대화 ID·크기 |
| `cardnews_v<N>.zip` | 위 파일 전부 |

검증 결과 중 '원문 링크가 지금 열리는지', '카드 문장이 원문과 맞는지', '한글 글꼴·줄바꿈', '그림이 오해를 부르지 않는지'는 항상 `unknown`(확인 못 함)입니다. 사람이 직접 확인해야 합니다.

## 프로젝트 문서 (`GET /api/projects/{id}`의 `project`)

```json
{
  "id": "prj_…", "topic": "…", "period_days": 7, "engine": "claude", "mock": false,
  "stage": "review", "status": "waiting_for_user", "version": 110,
  "engine_session_id": "…", "active_run_id": "run_…", "error": null,

  "research": {
    "searched_at": "…", "timezone": "UTC+09:00", "period_days": 7, "period_start": "2026-10-16", "period_end": "2026-10-23",
    "notice": { "found": 6, "message": "…", "can_expand": true } ,
    "excluded": ["기간 밖이라 뺀 소식 제목"],
    "candidates": [{
      "id": "c1", "title": "한국어 제목", "original_title": "원문 제목", "summary": "…",
      "source_type": "통신사|언론|공식 발표|단체 성명|기타", "source_name": "…", "url": "…", "related_urls": [],
      "published_at": "YYYY-MM-DD 또는 null", "event_date": "YYYY-MM-DD 또는 null",
      "grouped_count": 2, "read_original": true, "value": "고를 가치", "uncertainty": "불확실성"
    }]
  },
  "selection": { "candidate_ids": ["c1", "c2"], "saved_at": "…" },
  "question": null,
  "answers": [{ "question_id": "format", "version": 1, "question": "…", "answer": { "choice": "comparison", "text": null }, "answer_label": "비교형", "answered_at": "…" }],

  "editorial": {
    "audience": "…", "why_useful": "…", "format": "…", "format_reason": "…",
    "format_comparison": [{ "format": "…", "fits": "…", "weak": "…" }],
    "hooks": ["…"],
    "notes": { "confirmed": [{ "text": "…", "source_url": "…" }], "claims": [], "unverified": [], "conflicts": [] },
    "sources": [{ "title": "…", "url": "…", "published_at": "…", "read_original": true }]
  },

  "storyboard": {
    "cards": [{ "id": "k1", "role": "표지", "title": "…", "key_sentence": "…", "body": "…",
                "image_plan": { "scene": "…", "purpose": "…" }, "sources": [{ "label": "Al Jazeera 10/17", "url": "…" }] }],
    "visual": { "style": "…", "palette": ["#1F2A44", "#E4572E"] },
    "note": "…", "generated_at": "…", "approved_at": "…"
  },
  "storyboard_feedback": [{ "instruction": "…", "at": "…" }],

  "result_version": 18,
  "cards": [{
    "id": "k1", "order": 1, "role": "표지", "title": "…", "body": "…", "key_sentence": "…",
    "source_line": "출처: Al Jazeera 10/17 · 조사 기준 2026-10-23", "sources": [{ "label": "…", "url": "…" }],
    "scene": "그림 장면", "style": { "position": "top", "font_scale": 1.0, "text_color": "#ffffff", "box_color": "#1F2A44", "box_opacity": 0.85 },
    "rev": 2, "image_count": 1,
    "image": { "status": "pending|running|done|failed", "file": "images/k1_i1.jpg", "tool": "agy|claude-svg|mock",
               "prompt": "…", "created_at": "…", "width": 1024, "height": 1024, "format": "JPEG", "conversation_id": "…", "error": null },
    "render": { "status": "pending|done", "file": "renders/k1_r2.png", "rev": 2, "size": [1080, 1350],
                "checks": { "overflow": [], "contrast": { "text": 9.16, "source": 6.78 } }, "warnings": [], "rendered_at": "…" },
    "history": [{ "rev": 1, "title": "…", "body": "…", "style": {}, "image_file": "…", "png_file": "renders/k1_r1.png", "replaced_at": "…", "reason": "화면에서 직접 고침(그림 유지)" }]
  }],
  "final": { "approved_at": "…", "result_version": 18, "export": { "zip": "exports/v18/cardnews_v18.zip", "cards": [], "documents": [] }, "verification": null }
}
```

`question`이 있을 때의 모양은 `{ id, version, run_id, asked_at, question, why, options: [{ value, label, description }], recommended: { value, reason }, multi_select, allow_free_text }`입니다. `version`은 같은 ID의 질문을 몇 번째로 물었는지입니다. 추천은 화면에서 표시만 하고 미리 선택하지 않으며, 사용자 답으로 간주하지 않습니다.

## 엔진 규약

웹 서버는 엔진을 직접 부르지 않고 작업 스레드(`cardnews/worker.py`)에 맡깁니다. 엔진은 `cardnews/engines/__init__.py`의 규약 하나만 구현합니다.

```python
engine.execute(task, session_id, emit, cancel) -> Outcome
```

| 인자 | 내용 |
|---|---|
| `task` | 앱이 만든 작업 설명. `stage`와 그 단계에 필요한 자료, `work_dir`(프로젝트마다 따로 둔 엔진 작업 폴더) |
| `session_id` | 이 프로젝트가 앞서 받은 엔진 세션 ID. 프로젝트마다 따로 저장하며 '가장 최근 세션'을 쓰지 않음 |
| `emit(message)` | 진행 내역 한 줄(검색어, 원문 읽기, 사용량 등) |
| `cancel` | 취소 신호. 설정되면 하위 프로세스를 정리하고 `Cancelled`를 올림 |

결과는 `Outcome(kind="result" | "need_input", data, session_id)`로 정규화하고, 실패는 `EngineError(code, message, next_action)`로 올립니다. 이 규약은 이 앱이 정한 것이며 CLI의 공통 규격이 아닙니다.

| stage | 하는 일 | Claude CLI 호출 |
|---|---|---|
| `research` | 넓은 조사, 후보 7~12개 | 새 세션, WebSearch·WebFetch 허용. 검색을 한 번도 안 하면 실패 처리 |
| `deep_research` | 원문 확인, 사실·주장·미확인·충돌 정리, 독자·형식·후킹. 필요하면 질문 하나 | 조사 세션을 `--resume`으로 이어 감, 웹 도구 허용 |
| `storyboard` | 카드 5~8장 기획, 공통 그림체·색 | 새 세션, 도구 없음 |
| `revise` | 고른 카드 고치기, 그림이 필요한지 판단 | 새 세션, 도구 없음 |
| `svg_image` | 도형 그림을 SVG 코드로 그리기 | 새 세션, 도구 없음 |

Claude CLI 호출 공통: `claude.exe -p --output-format stream-json --verbose --json-schema <스키마> --tools <도구> --model sonnet --effort medium --strict-mcp-config [--resume <세션 ID>]`, 프롬프트는 표준 입력으로 넘깁니다. 결과의 `structured_output`만 받아들입니다.

| 엔진 | 파일 |
|---|---|
| `gemini` (기본) | `cardnews/engines/gemini_api.py`. Interactions API(`POST https://generativelanguage.googleapis.com/v1beta/interactions`)를 `background: true`로 부르고 2초마다 `GET …/interactions/{id}`로 끝날 때까지 확인. 웹 도구가 필요한 단계는 `tools: [{"type":"google_search"},{"type":"url_context"}]`, 결과는 `response_format`의 JSON 스키마(`additionalProperties`를 빼고 null 허용 타입은 anyOf로 바꿈), `generation_config.thinking_level`. `previous_interaction_id`로 같은 대화를 이어 감. 진행 내역에는 검색어(`google_search_call`)와 연 주소(`url_context_result`, 성공만 원문 확인으로 셈)를 남김. 키는 환경 변수 `GEMINI_API_KEY`(없으면 `GOOGLE_API_KEY`). 기본 모델 `gemini-3.8-flash`, thinking medium |
| `openai` | `cardnews/engines/openai_api.py`. `POST https://api.openai.com/v1/responses`를 스트리밍으로 부름. 웹 도구가 필요한 단계는 `tools: [{"type":"web_search"}]`, 결과는 `text.format` json_schema(strict, null 허용 객체는 anyOf로 바꿈). `previous_response_id`로 같은 대화를 이어 가고, 저장된 앞 응답이 없으면 프롬프트만으로 새로 시작. 검색어(`action.type=search`)와 연 페이지(`open_page`)를 진행 내역에 남김. 기본 모델 `gpt-5.4-mini` |
| `claude` | `cardnews/engines/claude_cli.py`. 프롬프트·스키마·결과 검사의 원본(Gemini·OpenAI 엔진이 상속해서 씀) |
| `mock` | `cardnews/engines/mock.py`. 실제 검색 없음. 제목에 `[모의]`, 링크는 `example.com` |

공개 모드에서는 그림 한 장을 만들기 전에 하루 그림 상한을 확인하고, 넘으면 `rate_limited`로 그 카드를 멈춥니다.

## 그림 작업자와 렌더러

```python
images.generate(prompt, out_base, work_dir, emit, cancel) -> { file, width, height, format, conversation_id }
renderer.render([{ card, image_path, out_png, index, total, background }]) -> [{ file, size, checks, warnings }]
```

| 구현 | 내용 |
|---|---|
| `AgyImageMaker` (`cardnews/images.py`, `--images agy`) | `agy -p "<자연어 요청>" --output-format json`. 응답의 `conversation_id`로 `~/.gemini/antigravity-cli/brain/<id>/`에서 새 그림을 찾고, Pillow로 열어 형식·크기를 확인한 뒤 프로젝트 `images/`로 복사. 그림이 없으면 이번 호출의 agy 대화 기록에서 429 등 원인을 찾음 |
| `GeminiImageMaker` (기본) | 같은 Interactions API에 `model: gemini-3.1-flash-image`, `response_format: {type: image, aspect_ratio: "1:1", image_size: "1K", mime_type: image/jpeg}`로 요청. 응답 `model_output`의 이미지 블록(base64)을 JPEG로 저장하고 Pillow로 확인. 그림 없이 글만 오면 `image_blocked`, 아무것도 없으면 `image_missing`. 오류 분류는 엔진과 같음 |
| `OpenAIImageMaker` (`--images openai`) | `POST https://api.openai.com/v1/images/generations` (`model: gpt-image-1.5`, `size: 1024x1024`, `quality: medium`, `output_format: png`). 응답의 base64를 PNG로 저장하고 Pillow로 확인. 키는 환경 변수 `OPENAI_API_KEY`에서만 읽음. 401 → `auth_required`, 크레딧 부족·429 → `rate_limited`, 403·조직 인증 → `permission_denied`, 안전 기준 → `image_blocked` |
| `MockImageMaker` | Pillow로 단순한 도형 그림. `--mock-fail images`면 첫 그림을 할당량 초과로 실패 |
| `Renderer` (`cardnews/render.py`) | 글 상자와 그림을 위아래로 나눠 배치한 카드 HTML을 Playwright(Chromium)로 1080×1350 PNG로 찍음. 그림의 빈 가장자리는 잘라 내고, 카드 바탕은 한 편 전체에 같은 색. 같은 페이지에서 잘림·글과 그림의 겹침·그림 크기(360px 미만)·명암 대비(기준 4.5:1)를 잼. SVG 도형 그림을 PNG로 바꿀 때는 스크립트를 끈 페이지를 씀 |

그림 요청 문장(`images.build_prompt`)에는 카드뉴스 주제와 장면, 공통 그림체·색이 들어갑니다. 주제와 다른 나라·도시로 보이는 지도·국기·건물·종교 상징, 얼굴의 눈·코·입, 흐림·빛 번짐·그라데이션은 넣지 않게 하고, 바탕은 단색, 그림은 가운데에 모으게 합니다.
