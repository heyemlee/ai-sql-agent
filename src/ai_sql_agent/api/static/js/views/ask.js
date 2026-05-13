/* 业务查数：一个输入框，问完看结论与表格，接着追问。

   一轮问答是一个 turn 对象；界面状态（哪个折叠打开、表格翻到第几页、反馈草稿）
   都记在 turn.ui 上，重绘只是把 turn 再画一遍——所以切语言、切学习模式、
   回填耗时都不会把用户展开的东西收回去。 */

import { api } from '../api.js';
import { catalog, ensureIdentity, hasKinds } from '../catalog.js';
import { $, announce, delegate, html, icon, setHtml } from '../dom.js';
import { formatDateTime } from '../format.js';
import { findHistory, loadHistory } from '../historyStore.js';
import { t } from '../i18n.js';
import { normalizeResult } from '../model.js';
import {
  announcement, renderPending, renderResult, renderServiceError, renderStopped,
} from '../result.js';
import { emit, newSessionId, on, resultCache, save, state, uid } from '../state.js';
import { bindTurnActions, newTurnUi } from '../turnActions.js';

/* 示例问题。只写问法，不写答案；每条标了它需要哪一类业务域，
   当前权限下看不到那个域的示例不显示——点了问不出结果的示例比没有示例更糟。 */
const EXAMPLES = [
  { id: 'supplier', kinds: ['erp'] },
  { id: 'budget', kinds: ['pm'] },
  { id: 'payment', kinds: ['erp'] },
  { id: 'production', kinds: ['erp', 'mes'] },
  { id: 'inventory', kinds: ['wms'] },
];

const turns = [];
let busy = null;              // 正在等结果的那一轮
let streamUnsupported = false;
let root;

const newTurn = (question, extra = {}) => ({
  id: 't' + uid().slice(0, 10),
  question,
  askedAt: new Date().toISOString(),
  status: 'pending',          // pending | done | error | stopped
  result: null,
  error: null,
  transport: 'query',
  stages: [],
  stageNow: null,
  startedAt: performance.now(),
  controller: null,
  rerunOf: null,
  cached: false,
  ui: newTurnUi(),
  ...extra,
});

function chooseTransport() {
  // 长期记忆只有 /query 支持；服务端没说支持流式（或连目录都没拿到）就不走流
  const streaming = Boolean(state.service && state.service.streaming) && !streamUnsupported;
  if (state.longTerm || !streaming || state.transport === 'query') return 'query';
  if (state.transport === 'stream') return 'a2a-stream';
  return state.learning ? 'a2a-stream' : 'query';
}

/* ─── 绘制 ─────────────────────────────────────────────────────── */

function composer(placeholderKey) {
  return html`<form class="composer" data-form="ask">
    <label class="sr-only" for="askInput">${t('ask.inputLabel')}</label>
    <textarea id="askInput" rows="1" placeholder="${t(placeholderKey)}" autocomplete="off"></textarea>
    <button class="send-btn" type="submit" aria-label="${t('action.send')}" title="${t('action.send')}">${icon('send')}</button>
  </form>
  <p class="composer-hint">${t('ask.hint')}</p>`;
}

function examples() {
  const usable = catalog.status === 'ready'
    ? EXAMPLES.filter((example) => hasKinds(example.kinds))
    : catalog.status === 'loading' || catalog.status === 'idle' ? [] : EXAMPLES;
  if (!usable.length) {
    return catalog.status === 'ready'
      ? html`<p class="muted">${t('ask.noExamples')} <a href="#/data">${t('ask.browseData')}</a></p>`
      : '';
  }
  return html`<section aria-labelledby="examplesTitle" class="stack">
    <h2 class="section-title" id="examplesTitle">${t('ask.examplesTitle')}</h2>
    <div class="examples">${usable.map((example) => html`<button type="button" class="example"
      data-action="example" data-example="${example.id}">
      <span class="topic">${t(`example.${example.id}.topic`)}</span>
      <span>${t(`example.${example.id}.question`)}</span></button>`)}</div>
    <p class="muted">${t('ask.examplesNote')} <a href="#/data">${t('ask.browseData')}</a></p>
  </section>`;
}

function shell() {
  const previous = $('askInput');
  const draft = previous ? previous.value : '';
  // 用键盘回车发的问题，发完光标留在输入框；点按钮发的（多半是手机）不抢焦点，免得键盘弹回来
  const refocus = Boolean(previous) && document.activeElement === previous;
  if (!turns.length) {
    setHtml(root, html`<div class="page page-narrow ask-home stack-lg">
      <div><h1 tabindex="-1" data-role="view-title">${t('ask.title')}</h1><p class="lead">${t('ask.lead')}</p></div>
      <div>${composer('ask.placeholder')}</div>
      <div data-role="examples">${examples()}</div>
    </div>`);
  } else {
    setHtml(root, html`<div class="conversation">
      <div class="turns"><div class="turns-inner">
        <div class="row-between">
          <h1 class="section-title" tabindex="-1" data-role="view-title">${t('ask.conversation')}</h1>
          <button type="button" class="btn btn-sm" data-action="new">${icon('plus', 'icon-sm')}${t('ask.new')}</button>
        </div>
        ${turns.map((turn) => html`<article class="turn" id="${turn.id}" data-turn="${turn.id}"></article>`)}
      </div></div>
      <div class="composer-dock"><div class="composer-dock-inner">${composer('ask.followUpPlaceholder')}</div></div>
    </div>`);
    turns.forEach(drawTurn);
  }
  if (draft) $('askInput').value = draft;
  if (refocus) $('askInput').focus();
  syncComposer();
}

function drawTurn(turn) {
  const element = $(turn.id);
  if (!element) return;
  const elapsed = Math.floor((performance.now() - turn.startedAt) / 1000);
  let body;
  if (turn.status === 'pending') body = renderPending(turn, elapsed);
  else if (turn.status === 'stopped') body = renderStopped();
  else if (turn.status === 'error') body = renderServiceError(turn);
  else body = renderResult(turn);

  setHtml(element, html`<div class="question">
      <div>
        <h2 class="question-text">${turn.question}</h2>
        <p class="question-meta">${t(turn.rerunOf ? 'ask.askedRerun' : 'ask.asked', { time: formatDateTime(turn.askedAt) })}</p>
      </div>
      <button type="button" class="icon-btn" data-action="copy-question" aria-label="${t('action.copyQuestion')}"
        title="${t('action.copyQuestion')}">${icon('copy')}</button>
    </div>
    <div>${body}</div>`);
}

function syncComposer() {
  const input = $('askInput');
  if (!input) return;
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 180) + 'px';
  // 没到高度上限时不留滚动条：自动长高的输入框旁边多一截滚动条只会让人以为还有内容
  input.style.overflowY = input.scrollHeight > 180 ? 'auto' : 'hidden';
  const send = root.querySelector('.send-btn');
  if (send) send.disabled = Boolean(busy) || !input.value.trim();
}

/* ─── 提问 ─────────────────────────────────────────────────────── */

async function ask(question, extra = {}) {
  const text = String(question || '').trim();
  if (!text || busy) return;

  const turn = newTurn(text, extra);
  turn.controller = new AbortController();
  turns.push(turn);
  busy = turn;
  shell();
  const element = $(turn.id);
  if (element) element.scrollIntoView({ block: 'start' });

  // 发问之前先确认身份与服务能力（通常已经有了；页面刚打开、或者刚在设置里换过连接时
  // 要等几十毫秒）。没有它，user_id 只能填占位值，绑定了用户的 Key 会因此被拒；
  // 也不知道该不该走流式。目录拿不到不影响这一步——身份另有来源。
  await ensureIdentity();
  if (turn.controller.signal.aborted) {
    turn.status = 'stopped';
    turn.controller = null;
    busy = null;
    drawTurn(turn);
    syncComposer();
    return;
  }
  turn.transport = chooseTransport();
  drawTurn(turn);

  const timer = setInterval(() => {
    const slot = $(turn.id) && $(turn.id).querySelector('[data-role="elapsed"]');
    const seconds = Math.floor((performance.now() - turn.startedAt) / 1000);
    if (slot && seconds >= 2) slot.textContent = t('ask.elapsed', { n: seconds });
  }, 1000);

  try {
    let payload;
    if (turn.transport === 'a2a-stream') {
      try {
        const streamed = await api.stream(text, {
          signal: turn.controller.signal,
          onStage: (stage) => { turn.stages.push(stage); turn.stageNow = stage; drawTurn(turn); },
        });
        payload = streamed.payload;
      } catch (error) {
        // 服务端明确说流式没开（-32004）：这是「换条路」，不是失败。
        // 其余错误不重试——流已经打开过的话，重发等于让同一个问题再跑一遍。
        if (error.code !== 'stream_unsupported') throw error;
        streamUnsupported = true;
        turn.transport = 'query';
        turn.stages = [];
        turn.stageNow = null;
        drawTurn(turn);
        payload = await api.query(text, turn.controller.signal);
      }
    } else {
      payload = await api.query(text, turn.controller.signal);
    }

    turn.result = normalizeResult(payload, {
      transport: turn.transport,
      stages: turn.stages,
      clientMs: performance.now() - turn.startedAt,
    });
    turn.status = 'done';
    if (turn.result.traceId) {
      resultCache.set(turn.result.traceId, {
        result: turn.result, question: text, capturedAt: turn.result.receivedAt,
      });
    }
    state.live = true;
  } catch (error) {
    if (error && error.name === 'AbortError') {
      turn.status = 'stopped';
    } else {
      turn.status = 'error';
      turn.error = error;
      if (!error.status) { state.live = false; emit('connection'); }
    }
  } finally {
    clearInterval(timer);
    turn.controller = null;
    busy = null;
    drawTurn(turn);
    syncComposer();
    revealAnswer(turn);
    announce(announcement(turn));
  }

  if (turn.status === 'done') backfill(turn);
  else if (turn.status === 'stopped') loadHistory();
}

/* 答案回来时把这一轮顶到视口上方。等待态只有一行，答案比它长得多，
   不动的话结论常常落在底部输入框后面。用户已经滚到别处去看东西了就不打扰。 */
function revealAnswer(turn) {
  const element = $(turn.id);
  if (!element) return;
  const box = element.getBoundingClientRect();
  if (box.bottom <= 0 || box.top >= window.innerHeight) return;
  // 直接跳过去，不做平滑滚动：紧接着还有一次回填重画，动画会被它打断，停在半路
  element.scrollIntoView({ block: 'start' });
}

/* 服务端记账的耗时、token 与检索命中的表不在 /query 的响应里，查完从 /history 取回。 */
async function backfill(turn) {
  await loadHistory();
  const entry = findHistory(turn.result.traceId);
  if (!entry) return;
  turn.result.serverMs = typeof entry.duration_ms === 'number' ? entry.duration_ms : null;
  turn.result.totalTokens = typeof entry.total_tokens === 'number' ? entry.total_tokens : null;
  turn.result.candidateTables = Array.isArray(entry.tables) ? entry.tables : [];
  // 查询过程即使收着也重画：它的内容在折叠里已经画好了，不重画的话展开看到的是回填之前的那份
  drawTurn(turn);
}

/* ─── 对外 ─────────────────────────────────────────────────────── */

/* 历史页的「重新执行」：一次新的查询，新的一轮上标明它是重跑以及原记录的时间。

   必须换一个新会话。沿用当前会话的话，服务端的路由会发现「这个问题刚才问过、
   结果还在上下文里」，直接根据记忆作答而不去查库——那样「重新执行」拿回来的
   就是旧结果的复述，而页面却在说这是刚读出来的数据。 */
export function rerun(question, originalTime) {
  if (busy) return undefined;
  turns.length = 0;
  state.sessionId = newSessionId();
  save();
  return ask(question, { rerunOf: originalTime || '' });
}

/* 数据浏览页的「就这张表提问」：只把话头填进输入框，问什么由人写。 */
export function prefill(text) {
  const input = $('askInput');
  if (!input) return;
  input.value = text;
  syncComposer();
  input.focus();
  input.setSelectionRange(text.length, text.length);
}

export function focusComposer() {
  const input = $('askInput');
  if (input) input.focus();
}

const turnOf = (target) => {
  const holder = target.closest('[data-turn]');
  return holder ? turns.find((turn) => turn.id === holder.dataset.turn) : null;
};

export function initAsk() {
  root = $('view-ask');

  root.addEventListener('submit', (event) => {
    if (!event.target.matches('[data-form="ask"]')) return;
    event.preventDefault();
    const input = $('askInput');
    const text = input.value;
    if (!text.trim() || busy) return;
    input.value = '';
    ask(text);
  });

  root.addEventListener('input', (event) => {
    if (event.target.id === 'askInput') syncComposer();
  });

  root.addEventListener('keydown', (event) => {
    // Enter 发送，Shift+Enter 换行；输入法正在组字时的回车不算
    if (event.target.id !== 'askInput' || event.key !== 'Enter') return;
    if (event.shiftKey || event.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    event.target.form.requestSubmit();
  });

  bindTurnActions(root, { turnOf, redraw: drawTurn });

  delegate(root, 'click', {
    example: (target) => prefill(t(`example.${target.dataset.example}.question`)),
    new: () => {
      if (busy) return;
      turns.length = 0;
      state.sessionId = newSessionId();
      save();
      shell();
      focusComposer();
    },
    stop: (target) => {
      const turn = turnOf(target);
      if (turn && turn.controller) turn.controller.abort();
    },
    'ask-again': (target) => {
      const turn = turnOf(target);
      if (turn) ask(turn.question);
    },
    'follow-up': () => focusComposer(),
    'open-settings': () => emit('open-settings'),
  });

  on('lang', shell);
  on('learning', () => turns.forEach(drawTurn));
  // 换了服务地址或 Key：上一台服务说过「不支持流式」，不代表这一台也不支持
  on('reconnect', () => { streamUnsupported = false; });
  on('catalog', () => {
    if (!turns.length) {
      const slot = root.querySelector('[data-role="examples"]');
      if (slot) setHtml(slot, examples());
    } else {
      turns.forEach((turn) => { if (turn.status === 'done') drawTurn(turn); });
    }
  });

  shell();
}
