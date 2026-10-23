"use strict";

// 화면 상태. 서버 상태(S.data)는 폴링으로 받아 오고, 사용자가 고르는 중인 값(ui)은
// 서버에 보내기 전까지 브라우저에만 둔다. 서버 상태가 바뀔 때만 다시 그린다.
const S = { config: null, projectId: null, data: null, version: null, sig: "", timer: null, ui: {} };
const $app = document.getElementById("app");
const POLL_MS = 1500;

const STAGE = {
  created: "시작 전",
  research: "넓은 조사",
  select: "후보 고르기",
  deep_research: "심층 조사와 편집 판단",
  editorial: "편집 방향 확정",
  storyboard: "스토리보드 승인",
  production: "그림·글자 제작",
  review: "검수·수정",
  final: "최종 승인",
};
const TOOL = { agy: "AI 생성 이미지 (agy)", openai: "AI 생성 이미지 (OpenAI)", gemini: "AI 생성 이미지 (Gemini)", svg: "코드 도형 그림", "claude-svg": "코드 도형 그림", mock: "모의 그림" };
const STATUS = {
  idle: "대기",
  running: "진행 중",
  waiting_for_user: "선택 기다리는 중",
  failed: "실패",
  interrupted: "중단됨",
  cancelled: "취소됨",
};
const STEPS = ["research", "select", "deep_research", "storyboard", "production", "review", "final"];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const rid = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`);
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString("ko-KR", { dateStyle: "medium", timeStyle: "short" }) : "");

async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let json = null;
  try { json = await res.json(); } catch { /* 본문 없음 */ }
  if (!res.ok) throw json?.error || { code: `http_${res.status}`, message: `서버 응답 오류 (${res.status})` };
  return json;
}

function uiFor(id) {
  return (S.ui[id] ||= {
    selected: new Set(), choice: {}, text: {}, logOpen: false, busy: false, flash: null, pending: {},
    // 제작 단계: 고친 카드 선택, 카드별 편집 칸, 수정 요청 글
    cardSel: new Set(), editOpen: {}, edit: {}, sbText: "", revText: "", revScope: "auto",
  });
}

// 같은 동작을 다시 보낼 때는 같은 requestId를 쓴다. 서버가 응답했으면(성공·거절 모두) 버린다.
function requestIdFor(ui, key) {
  return (ui.pending[key] ||= rid());
}

// ---------- 라우팅과 폴링

window.addEventListener("hashchange", route);
document.addEventListener("DOMContentLoaded", init);

async function init() {
  try {
    S.config = await api("GET", "/api/config");
    document.getElementById("engine-badge").innerHTML = S.config.mock
      ? `<span class="tag mock">모의 엔진</span>`
      : `<span class="tag">엔진: ${esc(S.config.engine)} · 그림: ${esc(S.config.images)}</span>`;
  } catch (e) {
    $app.innerHTML = `<p class="err">서버에 연결하지 못했습니다: ${esc(e.message)}</p>`;
    return;
  }
  route();
}

async function route() {
  clearInterval(S.timer);
  const m = location.hash.match(/^#\/p\/([\w-]+)/);
  if (m) {
    S.projectId = m[1];
    S.version = null;
    S.sig = "";
    S.data = null;
    await refresh();
    S.timer = setInterval(refresh, POLL_MS);
  } else {
    S.projectId = null;
    await renderHome();
  }
}

async function refresh(force = false) {
  const id = S.projectId;
  if (!id) return;
  try {
    const data = await api("GET", `/api/projects/${id}`);
    if (id !== S.projectId) return;
    const version = data.project.version;
    const sig = data.runs.map((r) => `${r.id}:${r.updated_at}:${r.events.length}`).join(",");
    const changed = force || version !== S.version;
    S.data = data;
    if (changed) {
      S.version = version;
      S.sig = sig;
      render();
    } else if (sig !== S.sig) {
      // 진행 내역만 늘었을 때는 상태 줄과 내역만 바꾼다. 화면 전체를 다시 그리면 버튼이 눌리는 도중에 바뀐다.
      S.sig = sig;
      renderProgress();
    }
  } catch (e) {
    if (!S.data) $app.innerHTML = `<p class="err">${esc(e.message)}</p><p><a href="#/">목록으로</a></p>`;
  }
}

// ---------- 첫 화면

async function renderHome() {
  let projects = [];
  let listError = "";
  try {
    projects = (await api("GET", "/api/projects")).projects;
  } catch (e) {
    listError = e.message;
  }
  $app.innerHTML = `
    <section class="card">
      <h1>새 카드뉴스</h1>
      <form id="new-form" class="stack">
        <label>주제
          <input name="topic" maxlength="100" required placeholder="예: AI 최신소식, 프랑스 시위">
        </label>
        <label>조사 기간
          <select name="period"><option value="7">최근 7일</option><option value="30">최근 30일</option></select>
        </label>
        <button type="submit">조사 시작</button>
        <p class="err" id="new-err"></p>
      </form>
    </section>
    <section class="card">
      <h2>${S.config?.public ? "모든 프로젝트" : "프로젝트"}</h2>
      ${listError ? `<p class="err">${esc(listError)}</p>` : ""}
      ${projects.length
        ? `<ul class="plist">${projects.map((p) => `
            <li>
              <a href="#/p/${esc(p.id)}">${esc(p.topic)}</a>
              ${p.mock ? `<span class="tag mock">모의</span>` : ""}
              <div class="muted small">${esc(STAGE[p.stage] || p.stage)} · ${esc(STATUS[p.status] || p.status)} · ${fmtTime(p.updated_at)}</div>
            </li>`).join("")}</ul>`
        : `<p class="muted">아직 만든 프로젝트가 없습니다.</p>`}
    </section>`;

  const form = document.getElementById("new-form");
  let createId = rid();
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const button = form.querySelector("button");
    const err = document.getElementById("new-err");
    button.disabled = true;
    err.textContent = "";
    try {
      const topic = form.topic.value.trim();
      const periodDays = Number(form.period.value);
      const { projectId } = await api("POST", "/api/projects", { topic, periodDays, requestId: createId });
      await api("POST", `/api/projects/${projectId}/research`, { requestId: rid() });
      location.hash = `#/p/${projectId}`;
    } catch (e) {
      err.textContent = e.message || "요청에 실패했습니다.";
      if (e.code) createId = rid();
      button.disabled = false;
    }
  });
}

// ---------- 프로젝트 화면

function render() {
  if (!S.data) return;
  const { project: p, runs } = S.data;
  const ui = uiFor(p.id);
  const producing = (p.cards || []).length > 0;
  const planning = p.storyboard && !producing;
  // 기획·제작 단계에서는 앞 단계(조사·편집 방향)를 접어 둔다
  const earlier = [
    p.research ? researchSection(p, ui) : "",
    p.answers.length ? answersSection(p) : "",
    p.editorial ? editorialSection(p) : "",
  ].join("");
  $app.innerHTML = [
    p.mock ? `<div class="banner mock">모의 데이터입니다. 화면 흐름을 확인하기 위한 예시이며 실제 조사 결과가 아닙니다.</div>` : "",
    headSection(p),
    statusSection(p, runs[0], ui),
    p.question ? questionSection(p, ui) : "",
    p.stage === "created" && p.status !== "running" ? startSection(p) : "",
    p.final ? finalSection(p, ui) : "",
    producing ? cardsSection(p, ui) : "",
    planning ? storyboardSection(p, ui) : "",
    producing || planning
      ? `<details class="card fold"><summary>앞 단계 다시 보기: 조사 후보 · 답변 · 편집 방향${producing ? " · 스토리보드" : ""}</summary>
           ${producing ? storyboardSection(p, ui) : ""}${earlier}</details>`
      : earlier,
    `<div id="log-wrap">${logSection(runs, ui)}</div>`,
  ].join("");
}

function renderProgress() {
  const { project: p, runs } = S.data;
  const last = $app.querySelector("[data-last-event]");
  if (last) last.textContent = lastEvent(runs[0]);
  const wrap = document.getElementById("log-wrap");
  if (wrap) wrap.innerHTML = logSection(runs, uiFor(p.id));
}

const lastEvent = (run) => run?.events?.at(-1)?.message || "시작하는 중";

function headSection(p) {
  const at = STEPS.indexOf(p.stage === "editorial" ? "storyboard" : p.stage);
  return `
    <section class="card head">
      <a href="#/" class="back">← 목록</a>
      <h1>${esc(p.topic)}</h1>
      <p class="muted small">엔진 ${esc(p.engine)} · 만든 시각 ${fmtTime(p.created_at)} · 버전 ${p.version}</p>
      <ol class="steps">
        ${STEPS.map((s, i) => `<li class="${i < at ? "done" : i === at ? "now" : ""}">${esc(STAGE[s])}</li>`).join("")}
      </ol>
    </section>`;
}

function statusSection(p, run, ui) {
  const flash = ui.flash ? `<div class="flash ${ui.flash.kind}">${esc(ui.flash.text)}</div>` : "";
  let body = "";
  if (p.status === "running") {
    body = `
      <div class="status running">
        <span class="spin"></span>
        <div><b>${esc(STAGE[p.stage])} 진행 중</b><div class="small" data-last-event>${esc(lastEvent(run))}</div></div>
        ${run ? `<button class="ghost" data-action="cancel" data-run="${esc(run.id)}">취소</button>` : ""}
      </div>`;
  } else if (p.status === "waiting_for_user") {
    const what = p.question ? "아래 질문에 답해 주세요."
      : p.stage === "select" ? "다룰 소식을 1~3개 골라 주세요."
      : p.stage === "storyboard" ? "스토리보드를 확인하고 승인하거나 변경을 요청하세요. 승인하기 전에는 그림을 만들지 않습니다."
      : p.stage === "review" ? "카드를 검토하세요. 고칠 카드를 고쳐도 되고, 괜찮으면 최종 승인하세요."
      : "선택을 기다리고 있습니다.";
    const cardErr = p.error?.scope === "card"
      ? `<div class="small err">최근 카드 작업 실패: ${esc(p.error.message)} ${p.error.next_action ? `— ${esc(p.error.next_action)}` : ""}</div>` : "";
    body = `<div class="status waiting">👤 <div><b>${what}</b><div class="small">기다리는 것은 고장이 아니라 정상 상태입니다.</div>${cardErr}</div></div>`;
  } else if (["failed", "interrupted", "cancelled"].includes(p.status)) {
    const e = p.error || {};
    body = `
      <div class="status error">
        <div>
          <b>${esc(STATUS[p.status])}: ${esc(e.message || "")}</b>
          ${e.next_action ? `<div class="small">다음 행동: ${esc(e.next_action)}</div>` : ""}
          <div class="small muted">오류 종류: ${esc(e.code || "")}</div>
        </div>
        <button data-action="retry" ${ui.busy ? "disabled" : ""}>다시 시도</button>
      </div>`;
  } else if (p.stage === "final") {
    body = `<div class="status done">✓ 최종 승인했습니다 (결과 버전 ${p.final?.result_version}). 아래에서 내려받을 수 있습니다. 카드를 고치면 승인이 풀립니다.</div>`;
  }
  return flash + body;
}

function startSection(p) {
  return `
    <section class="card">
      <p>아직 조사를 시작하지 않았습니다.</p>
      <button data-action="research">조사 시작 (최근 ${p.period_days}일)</button>
    </section>`;
}

function researchSection(p, ui) {
  const r = p.research;
  const canSelect = p.stage === "select" && p.status === "waiting_for_user";
  const chosen = new Set(p.selection?.candidate_ids || []);
  const picked = canSelect ? ui.selected : chosen;
  const full = picked.size >= 3;
  const notice = r.notice
    ? `<div class="notice">${esc(r.notice.message)}
        ${r.notice.can_expand && canSelect ? `<button class="ghost" data-action="expand" ${ui.busy ? "disabled" : ""}>30일로 넓혀 다시 조사</button>` : ""}
       </div>`
    : "";
  const excluded = r.excluded?.length
    ? `<details class="small muted"><summary>조사 기간 밖이라 뺀 소식 ${r.excluded.length}개</summary><ul>${r.excluded.map((t) => `<li>${esc(t)}</li>`).join("")}</ul></details>`
    : "";
  const deadLinks = r.dead_link_excluded?.length
    ? `<details class="small muted"><summary>기사 주소가 열리지 않아 뺀 소식 ${r.dead_link_excluded.length}개</summary><ul>${r.dead_link_excluded.map((t) => `<li>${esc(t)}</li>`).join("")}</ul></details>`
    : "";
  const items = r.candidates.map((c) => {
    const on = picked.has(c.id);
    return `
      <li class="cand ${on ? "on" : ""}">
        <label>
          ${canSelect ? `<input type="checkbox" data-cand="${esc(c.id)}" ${on ? "checked" : ""} ${!on && full ? "disabled" : ""}>` : on ? "✔" : ""}
          <span class="title">${esc(c.title)}</span>
        </label>
        ${c.original_title && c.original_title !== c.title ? `<div class="small muted orig">${esc(c.original_title)}</div>` : ""}
        <div class="meta small">
          <span class="tag src">${esc(c.source_type)}</span> ${esc(c.source_name)}
          ${"read_original" in c ? (c.read_original ? ` <span class="tag ok">원문 확인함</span>` : ` <span class="tag">검색 결과만 봄</span>`) : ""}
          · 게시일 ${esc(c.published_at || "미확인")} · 사건일 ${esc(c.event_date || "미확인")}
          ${c.grouped_count > 1 ? ` · <b>같은 사건 기사 ${c.grouped_count}건 묶음</b>` : ""}
        </div>
        <p>${esc(c.summary)}</p>
        <dl class="small">
          <dt>고를 가치</dt><dd>${esc(c.value)}</dd>
          <dt>불확실성</dt><dd>${esc(c.uncertainty)}</dd>
        </dl>
        <a class="small" href="${esc(c.url)}" target="_blank" rel="noopener">원문 링크 ↗</a>
      </li>`;
  }).join("");
  return `
    <section class="card">
      <h2>조사 후보</h2>
      <p class="small">조사 기준 시각 <b>${fmtTime(r.searched_at)}</b> (${esc(r.timezone)}) · 기간 <b>${esc(r.period_start)} ~ ${esc(r.period_end)}</b> (최근 ${r.period_days}일) · 후보 ${r.candidates.length}개</p>
      ${notice}
      ${excluded}
      ${deadLinks}
      <ul class="cands">${items}</ul>
      ${canSelect ? `
        <div class="bar">
          <button data-action="select" ${picked.size === 0 || ui.busy ? "disabled" : ""}>고른 ${picked.size}개로 다음 단계</button>
          <span class="muted small">1~3개를 고르세요. 이 선택이 제작 범위가 됩니다.</span>
        </div>` : ""}
    </section>`;
}

function questionSection(p, ui) {
  const q = p.question;
  const key = `${q.id}@${q.version}`;
  const choice = ui.choice[key] ?? null;
  const text = ui.text[key] ?? "";
  const ready = choice && (choice !== "__free__" || text.trim());
  const option = (value, label, desc, extra = "") => `
    <label class="opt ${choice === value ? "on" : ""}">
      <input type="radio" name="q-${esc(key)}" value="${esc(value)}" data-qkey="${esc(key)}" ${choice === value ? "checked" : ""}>
      <span><span class="line"><b>${esc(label)}</b>${q.recommended?.value === value ? `<span class="tag rec">추천</span>` : ""}</span>
      ${desc ? `<span class="desc">${esc(desc)}</span>` : ""}${extra}</span>
    </label>`;
  return `
    <section class="card question">
      <h2>${esc(q.question)}</h2>
      ${q.why ? `<p class="muted">${esc(q.why)}</p>` : ""}
      <div class="options">
        ${q.options.map((o) => option(o.value, o.label, o.description)).join("")}
        ${q.allow_free_text ? option("__free__", "직접 입력", "", `<textarea rows="2" data-qtext="${esc(key)}" placeholder="원하는 답을 적어 주세요">${esc(text)}</textarea>`) : ""}
      </div>
      ${q.recommended ? `<p class="small rec-reason">추천 이유: ${esc(q.recommended.reason)} (추천은 미리 선택되지 않습니다)</p>` : ""}
      <button data-action="answer" data-qkey="${esc(key)}" ${ready && !ui.busy ? "" : "disabled"}>답변 제출</button>
      <p class="muted small">질문 ID ${esc(q.id)} · 버전 ${q.version} · ${fmtTime(q.asked_at)}에 받은 질문</p>
    </section>`;
}

function answersSection(p) {
  return `
    <section class="card">
      <h2>이미 답한 질문</h2>
      <ul class="answers">
        ${p.answers.map((a) => `<li><span class="muted">${esc(a.question)}</span> → <b>${esc(a.answer_label)}</b> <span class="muted small">${fmtTime(a.answered_at)}</span></li>`).join("")}
      </ul>
    </section>`;
}

function editorialSection(p) {
  const e = p.editorial;
  // 항목은 문자열이거나 {text, source_url}이다
  const item = (x) => (typeof x === "string"
    ? esc(x)
    : `${esc(x.text)}${x.source_url ? ` <a class="small" href="${esc(x.source_url)}" target="_blank" rel="noopener">근거 ↗</a>` : ` <span class="small muted">(근거 링크 없음)</span>`}`);
  const list = (xs) => (xs?.length ? `<ul>${xs.map((x) => `<li>${item(x)}</li>`).join("")}</ul>` : `<p class="muted small">없음</p>`);
  return `
    <section class="card">
      <h2>편집 방향</h2>
      <dl>
        <dt>독자</dt><dd>${esc(e.audience)}</dd>
        <dt>독자에게 유용한 이유</dt><dd>${esc(e.why_useful)}</dd>
        <dt>형식</dt><dd>${esc(e.format)}</dd>
        ${e.format_reason ? `<dt>형식 추천 이유</dt><dd>${esc(e.format_reason)}</dd>` : ""}
      </dl>
      <table class="cmp">
        <tr><th>형식</th><th>잘 맞을 때</th><th>약한 점</th></tr>
        ${e.format_comparison.map((f) => `<tr><td>${esc(f.format)}</td><td>${esc(f.fits)}</td><td>${esc(f.weak)}</td></tr>`).join("")}
      </table>
      <h3>표지 문장 후보</h3>${list(e.hooks)}
      <h3>확인한 사실</h3>${list(e.notes?.confirmed)}
      <h3>발표 주체의 주장</h3>${list(e.notes?.claims)}
      <h3>아직 확인하지 못한 것</h3>${list(e.notes?.unverified)}
      ${e.notes?.conflicts ? `<h3>자료끼리 어긋나는 점</h3>${list(e.notes.conflicts)}` : ""}
      ${e.sources?.length ? `
        <h3>근거 자료</h3>
        <ul>${e.sources.map((s) => `<li><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a>
          <span class="small muted">${esc(s.published_at || "게시일 미확인")}</span>
          ${"read_original" in s ? (s.read_original ? `<span class="tag ok">원문 확인함</span>` : `<span class="tag">원문 미확인</span>`) : ""}</li>`).join("")}</ul>` : ""}
    </section>`;
}

const fileUrl = (p, path) => `/api/projects/${encodeURIComponent(p.id)}/files/${path.split("/").map(encodeURIComponent).join("/")}`;

function storyboardSection(p, ui) {
  const sb = p.storyboard;
  const open = p.stage === "storyboard" && p.status === "waiting_for_user";
  const swatches = sb.visual.palette.map((c) => `<span class="swatch" style="background:${esc(c)}" title="${esc(c)}"></span>`).join("");
  const feedback = p.storyboard_feedback?.length
    ? `<div class="small muted">받은 변경 요청: ${p.storyboard_feedback.map((f) => `“${esc(f.instruction)}”`).join(", ")}</div>` : "";
  return `
    <section class="card">
      <h2>스토리보드 (카드 ${sb.cards.length}장)</h2>
      <p class="small">그림체: ${esc(sb.visual.style)} ${swatches}</p>
      ${sb.note ? `<p class="small muted">${esc(sb.note)}</p>` : ""}
      ${feedback}
      <ol class="board">
        ${sb.cards.map((c) => `
          <li>
            <div><span class="tag">${esc(c.role)}</span> <b>${esc(c.title)}</b></div>
            <div class="small key">핵심 문장: ${esc(c.key_sentence)}</div>
            <p>${esc(c.body)}</p>
            <div class="small muted">그림 계획: ${esc(c.image_plan.scene)} — ${esc(c.image_plan.purpose)}</div>
            <div class="small">출처: ${c.sources.map((s) => `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.label)}</a>`).join(", ") || "없음"}</div>
          </li>`).join("")}
      </ol>
      ${open ? `
        <div class="stack">
          <label>변경 요청 <textarea rows="2" data-bind="sbText" placeholder="예: 4번 카드의 숫자는 쉬운 말로 풀고 숫자는 하나만 남겨 줘">${esc(ui.sbText)}</textarea></label>
          <div class="bar">
            <button class="ghost" data-action="sb-revise" ${ui.busy ? "disabled" : ""}>변경 요청 보내기</button>
            <button data-action="sb-approve" ${ui.busy ? "disabled" : ""}>스토리보드 승인 → 그림 만들기</button>
          </div>
        </div>` : ""}
    </section>`;
}

function cardsSection(p, ui) {
  const cards = [...p.cards].sort((a, b) => a.order - b.order);
  const canEdit = ["review", "final"].includes(p.stage) && p.status !== "running";
  const done = cards.filter((c) => c.render.status === "done" && c.render.rev === c.rev).length;
  const warnCount = cards.reduce((n, c) => n + (c.render.warnings?.length || 0), 0);
  const missing = cards.filter((c) => c.image.status !== "done").length;
  const tiles = cards.map((c) => cardTile(p, c, ui, canEdit)).join("");
  const strip = cards.filter((c) => c.render.status === "done")
    .map((c) => `<img src="${fileUrl(p, c.render.file)}" alt="카드 ${c.order}" loading="lazy">`).join("");
  const sel = [...ui.cardSel].map((id) => cards.find((c) => c.id === id)?.order).filter(Boolean).sort((a, b) => a - b);
  return `
    <section class="card">
      <h2>카드 (${done}/${cards.length}장 완성 · 결과 버전 ${p.result_version})</h2>
      ${warnCount ? `<div class="notice">자동 검사 경고 ${warnCount}개가 있습니다. 카드 아래 경고를 확인하고 글자를 고쳐 보세요.</div>` : ""}
      ${canEdit ? `<p class="small muted">앱의 배치 방식이 바뀌었으면 그림은 두고 글자 배치만 다시 할 수 있습니다.
        <button class="ghost small-btn" data-action="rerender" ${ui.busy ? "disabled" : ""}>모든 카드 다시 배치</button></p>` : ""}
      ${missing && canEdit ? `<div class="notice">그림이 없는 카드가 ${missing}장 있습니다. 완성된 카드는 그대로 두고 이 카드들만 다시 만듭니다.
        <button class="ghost" data-action="images-retry" ${ui.busy ? "disabled" : ""}>실패한 카드 그림 다시 만들기</button></div>` : ""}
      <div class="tiles">${tiles}</div>
    </section>
    ${strip ? `
    <section class="card">
      <h2>휴대폰 크기로 넘겨 보기</h2>
      <p class="small muted">실제로 내보낼 PNG를 375px 너비로 보여 줍니다. 옆으로 넘기세요.</p>
      <div class="strip">${strip}</div>
    </section>` : ""}
    ${canEdit ? `
    <section class="card">
      <h2>골라서 고치기</h2>
      <p class="small">고를 카드: ${sel.length ? sel.join(", ") + "번" : "카드 위의 체크 상자로 고르세요"}</p>
      <div class="stack">
        <label>무엇을 어떻게 고칠까요?
          <textarea rows="2" data-bind="revText" placeholder="예: 3번 카드의 본문이 길어. 핵심 조건을 유지하며 두 문장으로 줄여 줘">${esc(ui.revText)}</textarea>
        </label>
        <div class="scope">
          ${[["auto", "자동 판단"], ["text", "글만 (그림 유지)"], ["image", "그림만"]].map(([v, l]) =>
            `<label class="inline"><input type="radio" name="rev-scope" value="${v}" data-scope ${ui.revScope === v ? "checked" : ""}> ${l}</label>`).join("")}
        </div>
        <div class="bar"><button data-action="revise" ${ui.busy ? "disabled" : ""}>수정 요청</button>
          <span class="small muted">고르지 않은 카드는 바뀌지 않습니다. 고치면 최종 승인이 풀립니다.</span></div>
      </div>
    </section>
    ${p.stage === "review" ? `
    <section class="card">
      <h2>최종 승인</h2>
      <p class="small">${done === cards.length ? "모든 카드가 완성됐습니다." : `완성되지 않은 카드가 ${cards.length - done}장 있어 아직 승인할 수 없습니다.`}
        ${warnCount ? ` 경고 ${warnCount}개가 남아 있습니다.` : ""}</p>
      <button data-action="final-approve" ${done !== cards.length || ui.busy ? "disabled" : ""}>최종 승인하고 내보내기</button>
    </section>` : ""}` : ""}`;
}

function cardTile(p, c, ui, canEdit) {
  const img = c.image || {};
  const r = c.render || {};
  let visual;
  if (r.status === "done") {
    visual = `<a href="${fileUrl(p, r.file)}" target="_blank" rel="noopener"><img class="thumb" src="${fileUrl(p, r.file)}" alt="카드 ${c.order}"></a>`;
  } else if (img.status === "running") {
    visual = `<div class="ph"><span class="spin"></span> 그림 만드는 중</div>`;
  } else if (img.status === "failed") {
    visual = `<div class="ph err">그림 실패<br><span class="small">${esc(img.error?.message || "")}</span></div>`;
  } else {
    visual = `<div class="ph">${img.status === "done" ? "글자 배치 대기" : "그림 대기 중"}</div>`;
  }
  const stale = r.status === "done" && r.rev !== c.rev ? `<div class="small warn">새 버전 렌더링 대기</div>` : "";
  const warns = (r.warnings || []).map((w) => `<li class="${w.kind === "overflow" ? "err" : "warn"}">${esc(w.message)}</li>`).join("");
  const lastErr = img.status === "done" && img.error ? `<div class="small err">최근 그림 다시 만들기 실패: ${esc(img.error.message)}</div>` : "";
  const e = ui.edit[c.id];
  const editor = canEdit && ui.editOpen[c.id] && e ? `
    <div class="editor stack">
      <label>제목 <input data-bind="edit:${esc(c.id)}:title" value="${esc(e.title)}" maxlength="80"></label>
      <label>본문 <textarea rows="3" data-bind="edit:${esc(c.id)}:body" maxlength="400">${esc(e.body)}</textarea></label>
      <label>출처 줄 <input data-bind="edit:${esc(c.id)}:source_line" value="${esc(e.source_line)}" maxlength="200"></label>
      <label>그림 장면 <span class="small muted">(바꾼 뒤 ‘그림 다시 만들기’를 누르면 이 장면으로 그립니다)</span>
        <textarea rows="2" data-bind="edit:${esc(c.id)}:scene" maxlength="400">${esc(e.scene)}</textarea></label>
      <div class="grid2">
        <label>글 위치 <select data-bind="edit:${esc(c.id)}:style.position">
          <option value="top" ${e.style.position === "top" ? "selected" : ""}>위</option>
          <option value="bottom" ${e.style.position === "bottom" ? "selected" : ""}>아래</option></select></label>
        <label>글자 크기 <input type="number" step="0.05" min="0.7" max="1.4" data-bind="edit:${esc(c.id)}:style.font_scale" value="${e.style.font_scale}"></label>
        <label>글자색 <input type="color" data-bind="edit:${esc(c.id)}:style.text_color" value="${esc(e.style.text_color)}"></label>
        <label>글 상자 색 <input type="color" data-bind="edit:${esc(c.id)}:style.box_color" value="${esc(e.style.box_color)}"></label>
        <label>상자 진하기 <input type="number" step="0.05" min="0" max="1" data-bind="edit:${esc(c.id)}:style.box_opacity" value="${e.style.box_opacity}"></label>
      </div>
      <button data-action="card-edit-save" data-card="${esc(c.id)}" ${ui.busy ? "disabled" : ""}>글자만 적용 (그림 유지)</button>
    </div>` : "";
  const history = c.history?.length ? `
    <details class="small"><summary>이전 버전 ${c.history.length}개</summary><ul>
      ${c.history.map((h) => `<li>버전 ${h.rev}: ${esc(h.reason)} · ${fmtTime(h.replaced_at)} ${h.png_file ? `<a href="${fileUrl(p, h.png_file)}" target="_blank" rel="noopener">PNG</a>` : ""}</li>`).join("")}
    </ul></details>` : "";
  return `
    <div class="tile ${ui.cardSel.has(c.id) ? "on" : ""}">
      <div class="tile-head">
        ${canEdit ? `<input type="checkbox" data-cardsel="${esc(c.id)}" ${ui.cardSel.has(c.id) ? "checked" : ""} aria-label="카드 ${c.order} 고르기">` : ""}
        <b>${c.order}. ${esc(c.role)}</b> <span class="small muted">버전 ${c.rev}</span>
      </div>
      ${visual}${stale}
      <div class="small">${img.tool ? `<span class="tag">${esc(TOOL[img.tool] || img.tool)}</span>` : ""}
        ${r.checks ? `<span class="muted">대비 ${r.checks.contrast.text}:1</span>` : ""}</div>
      ${warns ? `<ul class="warns small">${warns}</ul>` : ""}
      ${lastErr}
      ${canEdit ? `
        <div class="tile-actions">
          <button class="ghost small-btn" data-action="card-edit-toggle" data-card="${esc(c.id)}">${ui.editOpen[c.id] ? "편집 닫기" : "글자 고치기"}</button>
          <button class="ghost small-btn" data-action="card-image" data-tool="default" data-card="${esc(c.id)}" ${ui.busy ? "disabled" : ""}>그림 다시 만들기${S.config?.images ? ` (${esc(S.config.images)})` : ""}</button>
          <button class="ghost small-btn" data-action="card-image" data-tool="claude" data-card="${esc(c.id)}" ${ui.busy ? "disabled" : ""}>코드 도형 그림으로</button>
        </div>` : ""}
      ${editor}${history}
    </div>`;
}

function finalSection(p, ui) {
  const f = p.final;
  const ex = f.export;
  const name = (path) => path.split("/").pop();
  const v = f.verification;
  const mark = { pass: "✓ 통과", fail: "✗ 실패", unknown: "? 확인 못 함" };
  const counts = v ? ["pass", "fail", "unknown"].map((k) => `${mark[k]} ${v.checks.filter((c) => c.status === k).length}`).join(" · ") : "";
  return `
    <section class="card final">
      <h2>내려받기 (결과 버전 ${f.result_version} · ${fmtTime(f.approved_at)} 승인)</h2>
      <p><a class="btn" href="${fileUrl(p, ex.zip)}">전체 ZIP 내려받기</a></p>
      <p class="small">카드 PNG: ${ex.cards.map((c) => `<a href="${fileUrl(p, c)}">${esc(name(c))}</a>`).join(" · ")}</p>
      <p class="small">기록: ${ex.documents.map((d) => `<a href="${fileUrl(p, d)}" target="_blank" rel="noopener">${esc(name(d))}</a>`).join(" · ")}</p>
      <button class="ghost" data-action="verify" ${ui.busy ? "disabled" : ""}>내보낸 파일 검증하기</button>
      ${v ? `
        <p class="small"><b>${counts}</b> · ${fmtTime(v.checked_at)} 검사</p>
        <table class="cmp verify">
          <tr><th>검사</th><th>결과</th><th>내용</th></tr>
          ${v.checks.map((c) => `<tr class="${c.status}"><td>${esc(c.name)}</td><td>${mark[c.status]}</td><td>${esc(c.detail)}</td></tr>`).join("")}
        </table>
        <p class="small muted">‘확인 못 함’은 자동으로 확인할 수 없는 항목입니다. 직접 확인하세요.</p>` : ""}
    </section>`;
}

function logSection(runs, ui) {
  if (!runs.length) return "";
  const KIND = {
    research: "넓은 조사", deep_research: "심층 조사", storyboard: "스토리보드", images: "그림·글자 제작",
    card_image: "카드 그림 다시 만들기", render: "글자 다시 배치", revise: "골라서 고치기",
  };
  return `
    <details class="card log" data-log ${ui.logOpen ? "open" : ""}>
      <summary>작업 진행 내역</summary>
      ${runs.map((r) => `
        <div class="run">
          <div class="small"><b>${esc(KIND[r.kind] || r.kind)}</b> · ${esc(r.status)} · ${esc(r.id)}</div>
          <ul class="small">${r.events.map((ev) => `<li><span class="muted">${new Date(ev.at).toLocaleTimeString("ko-KR")}</span> ${esc(ev.message)}</li>`).join("")}</ul>
        </div>`).join("")}
    </details>`;
}

// ---------- 사용자 동작

$app.addEventListener("change", (ev) => {
  const t = ev.target;
  if (!S.data) return;
  const ui = uiFor(S.data.project.id);
  if (t.dataset.cand) {
    t.checked ? ui.selected.add(t.dataset.cand) : ui.selected.delete(t.dataset.cand);
    render();
  } else if (t.dataset.cardsel) {
    t.checked ? ui.cardSel.add(t.dataset.cardsel) : ui.cardSel.delete(t.dataset.cardsel);
    render();
  } else if (t.dataset.scope !== undefined) {
    ui.revScope = t.value;
  } else if (t.dataset.bind) {
    bindValue(ui, t);
  } else if (t.dataset.qkey && t.type === "radio") {
    ui.choice[t.dataset.qkey] = t.value;
    render();
  }
});

// data-bind="sbText" · "revText" · "edit:<카드 ID>:title" · "edit:<카드 ID>:style.font_scale"
// 입력 중에는 다시 그리지 않고 값만 기억한다(커서가 사라지지 않게).
function bindValue(ui, t) {
  const key = t.dataset.bind;
  if (!key.startsWith("edit:")) {
    ui[key] = t.value;
    return;
  }
  const [, cid, field] = key.split(":");
  const e = ui.edit[cid];
  if (!e) return;
  if (field.startsWith("style.")) {
    const name = field.slice(6);
    e.style[name] = ["font_scale", "box_opacity"].includes(name) ? Number(t.value) : t.value;
  } else {
    e[field] = t.value;
  }
}

$app.addEventListener("input", (ev) => {
  const t = ev.target;
  if (t.dataset.bind && S.data) {
    bindValue(uiFor(S.data.project.id), t);
    return;
  }
  if (!t.dataset.qtext || !S.data) return;
  const ui = uiFor(S.data.project.id);
  const key = t.dataset.qtext;
  ui.text[key] = t.value;
  if (ui.choice[key] !== "__free__") {
    ui.choice[key] = "__free__";
    $app.querySelector(`input[type="radio"][value="__free__"]`).checked = true;
    $app.querySelectorAll(".opt").forEach((o) => o.classList.toggle("on", o.contains(t)));
  }
  // 다시 그리면 입력 중인 글자 커서가 사라지므로 버튼 상태만 바꾼다
  const button = $app.querySelector(`button[data-action="answer"]`);
  if (button) button.disabled = !(ui.choice[key] && (ui.choice[key] !== "__free__" || t.value.trim()));
});

$app.addEventListener("toggle", (ev) => {
  if (ev.target.dataset?.log !== undefined && S.data) uiFor(S.data.project.id).logOpen = ev.target.open;
}, true);

$app.addEventListener("click", async (ev) => {
  const button = ev.target.closest("button[data-action]");
  if (!button || !S.data) return;
  const p = S.data.project;
  const ui = uiFor(p.id);
  if (ui.busy) return;
  const action = button.dataset.action;
  const base = `/api/projects/${p.id}`;
  const cid = button.dataset.card;
  if (action === "card-edit-toggle") {
    // 서버에 보내지 않는 동작: 편집 칸을 열면서 지금 카드 내용을 복사해 둔다
    ui.editOpen[cid] = !ui.editOpen[cid];
    const c = p.cards.find((x) => x.id === cid);
    if (ui.editOpen[cid]) ui.edit[cid] = { title: c.title, body: c.body, source_line: c.source_line, scene: c.scene, style: { ...c.style } };
    render();
    return;
  }
  const needText = { "sb-revise": [ui.sbText, "변경 요청을 적어 주세요."], revise: [ui.revText, "무엇을 고칠지 적어 주세요."] }[action];
  if (needText && !needText[0].trim()) {
    ui.flash = { kind: "err", text: needText[1] };
    render();
    return;
  }
  if (action === "revise" && !ui.cardSel.size) {
    ui.flash = { kind: "err", text: "고칠 카드를 1장 이상 골라 주세요." };
    render();
    return;
  }
  const calls = {
    research: () => api("POST", `${base}/research`, { requestId: requestIdFor(ui, "research") }),
    expand: () => api("POST", `${base}/research`, { requestId: requestIdFor(ui, "expand"), periodDays: 30 }),
    select: () => api("POST", `${base}/selection`, {
      candidateIds: [...ui.selected], version: p.version, requestId: requestIdFor(ui, "select"),
    }),
    answer: () => {
      const key = button.dataset.qkey;
      const choice = ui.choice[key];
      return api("POST", `${base}/answers`, {
        questionId: p.question.id,
        version: p.question.version,
        answer: { choice, text: choice === "__free__" ? ui.text[key] : null },
        requestId: requestIdFor(ui, `answer:${key}`),
      });
    },
    retry: () => api("POST", `${base}/retry`, { requestId: requestIdFor(ui, "retry") }),
    cancel: () => api("POST", `/api/runs/${button.dataset.run}/cancel`),
    "sb-revise": () => api("POST", `${base}/storyboard/revise`, { instruction: ui.sbText, version: p.version, requestId: requestIdFor(ui, "sb-revise") }),
    "sb-approve": () => api("POST", `${base}/approve`, { stage: "storyboard", version: p.version, requestId: requestIdFor(ui, "sb-approve") }),
    "card-image": () => api("POST", `${base}/cards/${cid}/image`, { tool: button.dataset.tool, requestId: requestIdFor(ui, `img:${cid}:${button.dataset.tool}`) }),
    "card-edit-save": () => api("POST", `${base}/cards/${cid}/edit`, { ...ui.edit[cid], version: p.version, requestId: requestIdFor(ui, `edit:${cid}`) }),
    revise: () => api("POST", `${base}/revisions`, {
      cardIds: [...ui.cardSel], instruction: ui.revText, scope: ui.revScope, version: p.version, requestId: requestIdFor(ui, "revise"),
    }),
    "final-approve": () => api("POST", `${base}/approve`, { stage: "final", version: p.version, requestId: requestIdFor(ui, "final") }),
    verify: () => api("POST", `${base}/exports/verify`),
    "images-retry": () => api("POST", `${base}/images`, { requestId: requestIdFor(ui, "images-retry") }),
    rerender: () => api("POST", `${base}/rerender`, { requestId: requestIdFor(ui, "rerender") }),
  };
  if (!calls[action]) return;

  ui.busy = true;
  ui.flash = null;
  render();
  try {
    await calls[action]();
    ui.pending = {};
    if (action === "expand") ui.selected.clear();
    if (action === "sb-revise") ui.sbText = "";
    if (action === "revise") { ui.revText = ""; ui.cardSel.clear(); }
    if (action === "card-edit-save") ui.editOpen[cid] = false;
  } catch (e) {
    if (e.code) ui.pending = {}; // 서버가 거절했으면 다음 시도는 새 요청 ID로
    ui.flash = { kind: "err", text: `${e.message}${e.nextAction ? ` — ${e.nextAction}` : ""}` };
  } finally {
    ui.busy = false;
    await refresh(true);
  }
});
