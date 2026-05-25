/* 设置里的「Embedding」：Schema 检索把表、字段的说明变成向量时用哪个模型。

   和上面的「模型连接」分开存、分开恢复：那块管回答问题的对话模型，这块只管检索。
   本地哈希向量不调服务、没有语义；换成真正的 Embedding 之后，「多少钱」这类说法才召得回金额字段。

   保存不只是改个配置：服务要先用新模型把整个检索索引重建一遍（几秒到几十秒），建成了才换上，
   中途失败什么都不动。Key 和上面一样只往服务端送，留空表示接着用这台主机上现有的那把。 */

import { api } from '../api.js';
import { $, html } from '../dom.js';
import { t } from '../i18n.js';
import { canKeepEmbeddingKey, embeddingPrefill, embeddingRequestBody, originOf } from '../modelForm.js';
import { notice } from '../steps.js';
import { currentModelStatus, replaceModelStatus } from './settingsModel.js';

let redraw = () => {};
let form = null;           // { baseUrl, model, dimensions }：表单里现在的值
let typedKey = '';         // 输入框里还没提交的 Key。只在内存里，保存成功或关掉对话框就清掉
let busy = '';             // '' | 'test' | 'save' | 'restore'
let report = null;         // 最近一次连接测试的结果
let outcome = null;        // 保存 / 恢复之后的那句话：{ ok, text }

/* 对话框刚打开：上次没保存就关掉的输入不留。 */
export function resetEmbeddingSection() {
  form = null;
  typedKey = '';
  busy = '';
  report = null;
  outcome = null;
}

/* 重画之前把输入框里的值记下来。 */
export function captureEmbeddingForm() {
  if (!form || !$('embBase')) return;
  form = {
    baseUrl: $('embBase').value,
    model: $('embModel').value,
    dimensions: $('embDims').value,
  };
  typedKey = $('embKey').value;
}

function failureText(error) {
  if (error.serverMessage) return error.serverMessage;
  if (error.status === 0) return t('error.network');
  if (error.status === 401) return t('error.401');
  if (error.status === 403) return t('error.403');
  return t(error.status >= 500 ? 'error.5xx' : 'error.400');
}

function adopt(next) {
  replaceModelStatus(next);
  form = embeddingPrefill(next);
  typedKey = '';
}

async function run(kind, task) {
  captureEmbeddingForm();
  busy = kind;
  outcome = null;
  report = null;
  redraw();
  try {
    await task();
  } catch (error) {
    // 保存前的测试没通过：服务端把测试结果一起带回来了，照样列出来
    if (error.detail && error.detail.test) report = error.detail.test;
    outcome = { ok: false, text: failureText(error) };
  }
  busy = '';
  redraw();
}

const test = () => run('test', async () => {
  report = await api.testEmbedding(embeddingRequestBody(form, typedKey));
});

function save() {
  captureEmbeddingForm();
  const status = currentModelStatus();
  if (!status) return;
  const defaults = status.embedding.defaults || {};
  const body = embeddingRequestBody(form, typedKey);
  const address = originOf(body.base_url || defaults.base_url) || body.base_url;
  const model = body.model || defaults.model;
  if (!window.confirm(t('settings.embedding.confirm.save', { address, model }))) return;
  run('save', async () => {
    const result = await api.saveEmbedding(body);
    adopt(result.status);
    report = result.test;
    outcome = {
      ok: true,
      text: t('settings.embedding.saved', {
        texts: result.rebuild.texts,
        seconds: (result.rebuild.duration_ms / 1000).toFixed(1),
      }),
    };
  });
}

function restore() {
  const status = currentModelStatus();
  if (!status) return;
  const server = status.embedding.server || {};
  const target = server.backend === 'remote'
    ? t('settings.embedding.backend.remote', { model: server.model, dimensions: server.dimensions })
    : t('settings.embedding.backend.local');
  if (!window.confirm(t('settings.embedding.confirm.restore', { target }))) return;
  run('restore', async () => {
    adopt((await api.clearEmbedding()).status);
    outcome = { ok: true, text: t('settings.embedding.restored') };
  });
}

export const embeddingActions = {
  'embedding-test': test,
  'embedding-save': save,
  'embedding-restore': restore,
};

const keyPlaceholder = (status) => t(
  canKeepEmbeddingKey(status, form) ? 'settings.embedding.apiKey.keep' : 'settings.model.apiKey.required',
);

/* 地址一边打字一边变：只更新 Key 输入框的提示，不重画——重画会把光标弄丢。 */
export function embeddingAddressTyped() {
  captureEmbeddingForm();
  const status = currentModelStatus();
  if (status && $('embKey')) $('embKey').placeholder = keyPlaceholder(status);
}

function inUse(entry) {
  if (!entry || entry.backend !== 'remote') return t('settings.embedding.backend.local');
  return t('settings.embedding.backend.remote', { model: entry.model, dimensions: entry.dimensions });
}

function statusRows(embedding) {
  const rows = [[t('settings.model.current'), inUse(embedding.active && embedding.active.backend
    ? embedding.active : embedding)]];
  if (embedding.endpoint) {
    rows.push([t('settings.model.endpoint'), html`<span class="mono">${embedding.endpoint}</span>`]);
  }
  if (embedding.backend === 'remote') {
    let key = t('settings.model.key.missing');
    if (embedding.key_configured) {
      key = embedding.key_hint ? t('settings.model.key.hint', { hint: embedding.key_hint }) : t('settings.model.key.configured');
    }
    rows.push([t('settings.model.keyStatus'), key]);
  }
  if (embedding.saved_at !== undefined) {
    const saved = embedding.source === 'page'
      ? [t('settings.model.source.page'), (embedding.saved_at || '').replace('T', ' ').slice(0, 16), embedding.saved_by]
        .filter(Boolean).join(' · ')
      : t('settings.model.source.server');
    rows.push([t('settings.model.source'), saved]);
  }
  return html`<dl class="kv">${rows.map(([label, value]) => html`<dt>${label}</dt><dd>${value}</dd>`)}</dl>`;
}

function warnings(embedding) {
  const items = [];
  if (embedding.fallback) {
    items.push(notice('danger', 'error', t('settings.embedding.warn.fallback'),
      html`${t('settings.embedding.warn.fallback.body')}<p class="original">${embedding.fallback}</p>`));
  }
  if (embedding.store_problem) {
    items.push(notice('warn', 'warn', t('settings.embedding.warn.storeProblem'),
      t('settings.embedding.warn.storeProblem.body')));
  }
  return items;
}

function reportBlock() {
  if (!report) return '';
  const lines = report.results.map((item) => html`<li><span class="mono">${item.model}</span>${t('settings.model.report.line', {
    roles: t('settings.model.role.embedding'),
    outcome: t('settings.model.outcome.' + item.outcome),
  })}${item.dimensions ? ` · ${t('settings.embedding.report.dimensions', { dimensions: item.dimensions })}` : ''}${item.status_code && !item.ok
    ? ` · HTTP ${item.status_code}` : ''} · ${item.duration_ms} ms${item.message
    ? html`<p class="original">${t('settings.model.report.server', { message: item.message })}</p>` : ''}</li>`);
  const notes = (report.warnings || []).map((code) => html`<li>${t('settings.model.note.' + code)}</li>`);
  return notice(
    report.ok ? 'info' : 'danger',
    report.ok ? 'check' : 'error',
    t(report.ok ? 'settings.model.report.ok' : 'settings.model.report.failed'),
    html`<ul>${lines}${notes}</ul>`,
  );
}

function formBlock(status) {
  const embedding = status.embedding;
  const defaults = embedding.defaults || {};
  const off = busy ? 'disabled' : '';
  return html`<form class="stack" data-form="embedding">
    <div class="field"><label for="embBase">${t('settings.model.baseUrl')}</label>
      <input class="input" id="embBase" value="${form.baseUrl}" placeholder="${defaults.base_url || ''}"
        autocomplete="off" spellcheck="false" ${off}>
      <span class="hint">${t('settings.embedding.baseUrl.hint', { official: defaults.base_url || '' })}</span></div>
    <div class="field"><label for="embKey">${t('settings.model.apiKey')}</label>
      <input class="input" id="embKey" type="password" value="${typedKey}" placeholder="${keyPlaceholder(status)}"
        autocomplete="off" spellcheck="false" ${off}>
      <span class="hint">${t('settings.embedding.apiKey.hint')}</span></div>
    <div class="grid-2">
      <div class="field"><label for="embModel">${t('settings.embedding.model')}</label>
        <input class="input" id="embModel" value="${form.model}" placeholder="${defaults.model || ''}"
          autocomplete="off" spellcheck="false" ${off}>
        <span class="hint">${t('settings.embedding.model.hint')}</span></div>
      <div class="field"><label for="embDims">${t('settings.embedding.dimensions')}</label>
        <input class="input" id="embDims" inputmode="numeric" value="${form.dimensions}"
          placeholder="${String(defaults.dimensions || '')}" autocomplete="off" ${off}>
        <span class="hint">${t('settings.embedding.dimensions.hint')}</span></div>
    </div>
    <div class="row">
      <button type="button" class="btn btn-sm" data-action="embedding-test" ${off}>${t('settings.model.test')}</button>
      <button type="submit" class="btn btn-sm btn-primary" ${off}>${t('settings.embedding.save')}</button>
      ${embedding.source === 'page' || embedding.store_problem ? html`<button type="button" class="btn btn-sm btn-ghost"
        data-action="embedding-restore" ${off}>${t('settings.model.restore')}</button>` : ''}
    </div>
    <p class="desc">${t('settings.embedding.save.hint')}</p>
  </form>`;
}

/* 模型连接那一块还没取到状态（或者服务太老、没有 Embedding 这一节）时什么都不画：
   那一块已经在说明在取、取不到、还是没有权限。 */
export function embeddingSection() {
  const status = currentModelStatus();
  if (!status || !status.embedding) return '';
  if (!form) form = embeddingPrefill(status);
  const embedding = status.embedding;
  return html`<section class="stack model-settings">
    <h3>${t('settings.embedding')}</h3>
    <p class="desc">${t('settings.embedding.desc')}</p>
    ${statusRows(embedding)}
    ${warnings(embedding)}
    ${status.editable ? formBlock(status) : ''}
    <div class="stack-sm" role="status">
      ${busy ? html`<div class="loading"><span class="spinner"></span>${t('settings.embedding.busy.' + busy)}</div>` : ''}
      ${outcome && outcome.ok ? html`<p><span class="chip chip-ok">${outcome.text}</span></p>` : ''}
      ${outcome && !outcome.ok ? notice('danger', 'error', t('settings.model.failed'), outcome.text) : ''}
      ${reportBlock()}
    </div>
  </section>`;
}

export function initEmbeddingSection(draw) {
  redraw = draw;
}
