/* 设置里的「模型连接」：这台服务调用大模型用哪家协议、哪个地址、哪把 Key、哪两个模型。

   它和上面那块「连接」不是一回事——那块是这个标签页怎么连服务，只影响自己；
   这块存在服务端，一保存，所有人的问题都发往新的地方。所以只有管理员能改，
   没开鉴权的服务只认本机；保存前要再确认一次请求发给谁。

   Key 只往服务端送，不从服务端取：状态里只有「配没配」和末四位，输入框永远是空的，
   留空表示接着用现有的那把。 */

import { api } from '../api.js';
import { $, html } from '../dom.js';
import { t } from '../i18n.js';
import { canKeepKey, insecurePage, knownRejection, originOf, prefill, requestBody } from '../modelForm.js';
import { state } from '../state.js';
import { notice } from '../steps.js';

const PROVIDERS = ['anthropic', 'openai'];

let redraw = () => {};
let phase = 'idle';        // idle | loading | ready | error
let status = null;         // GET /admin/model 的结果
let failure = null;        // 取状态失败：{ status, message }
let form = null;           // { provider, baseUrl, chatModel, coderModel }：表单里现在的值
let typedKey = '';         // 输入框里还没提交的 Key。只在内存里，保存成功或关掉对话框就清掉
let busy = '';             // '' | 'test' | 'save' | 'restore'
let report = null;         // 最近一次连接测试的结果
let outcome = null;        // 保存 / 恢复之后的那句话：{ ok, text }

const isAdmin = () => Boolean(state.identity && state.identity.is_admin);

function adopt(next) {
  status = next;
  form = prefill(next, next.provider);
  typedKey = '';
}

const describe = (error) => ({ status: error.status || 0, message: error.serverMessage || '' });

/* 服务端给了原话就用原话（哪个字段不对、为什么不让改，它说得比状态码清楚）。 */
function failureText(error) {
  if (error.message) return error.message;
  if (error.status === 0) return t('error.network');
  if (error.status === 401) return t('error.401');
  if (error.status === 403) return t('error.403');
  return t(error.status >= 500 ? 'error.5xx' : 'error.400');
}

async function load() {
  phase = 'loading';
  failure = null;
  redraw();
  try {
    adopt(await api.modelStatus());
    phase = 'ready';
  } catch (error) {
    failure = describe(error);
    phase = 'error';
  }
  redraw();
}

/* 对话框刚打开：上次没保存就关掉的输入不留，状态重新取。 */
export function resetModelSection() {
  phase = 'idle';
  status = null;
  form = null;
  typedKey = '';
  busy = '';
  report = null;
  outcome = null;
}

/* 高级设置展开着、又还没取过状态时去取一次。不是管理员就不发请求——发了也只会被拒。 */
export function ensureModelLoaded() {
  if (phase === 'idle' && isAdmin()) load();
}

/* 重画之前把输入框里的值记下来：拨一下别的开关不该把刚填的地址和 Key 清掉。 */
export function captureModelForm() {
  if (!form || !$('modelProvider')) return;
  form = {
    provider: $('modelProvider').value,
    baseUrl: $('modelBase').value,
    chatModel: $('modelChat').value,
    coderModel: $('modelCoder').value,
  };
  typedKey = $('modelKey').value;
}

async function run(kind, task) {
  captureModelForm();
  busy = kind;
  outcome = null;
  report = null;
  redraw();
  try {
    await task();
  } catch (error) {
    outcome = { ok: false, text: failureText(describe(error)) };
  }
  busy = '';
  redraw();
}

const test = () => run('test', async () => {
  report = await api.testModel(requestBody(form, typedKey));
});

function save() {
  captureModelForm();
  const body = requestBody(form, typedKey);
  const official = (status.providers[body.provider] || {}).official_base_url;
  const address = originOf(body.base_url || official) || body.base_url;
  if (!window.confirm(t('settings.model.confirm.save', { address }))) return;
  run('save', async () => {
    adopt((await api.saveModel(body)).status);
    outcome = { ok: true, text: t('settings.model.saved') };
    // 保存不验证连接。存完用存下的那一份试一次：Key 不对的话现在就知道，不用等下一个问题失败
    busy = 'test';
    redraw();
    try { report = await api.testModel(requestBody(form, '')); } catch (_) { report = null; }
  });
}

function restore() {
  const server = status.server || {};
  const target = `${t('settings.model.provider.' + server.provider)} · ${server.chat_model}`;
  if (!window.confirm(t('settings.model.confirm.restore', { target }))) return;
  run('restore', async () => {
    adopt((await api.clearModel()).status);
    outcome = { ok: true, text: t('settings.model.restored') };
  });
}

export const modelActions = {
  'model-test': test,
  'model-save': save,
  'model-restore': restore,
  'model-reload': load,
};

/* 换协议：地址和模型名跟着换成那一家的，Key 要不要重填由地址决定。 */
export function modelProviderChanged(provider) {
  if (!form || !PROVIDERS.includes(provider)) return;
  form = prefill(status, provider);
  typedKey = '';
  report = null;
  outcome = null;
  redraw();
}

const keyPlaceholder = () => t(canKeepKey(status, form) ? 'settings.model.apiKey.keep' : 'settings.model.apiKey.required');

/* 地址一边打字一边变：只更新 Key 输入框的提示，不重画——重画会把光标弄丢。 */
export function modelAddressTyped() {
  captureModelForm();
  if ($('modelKey')) $('modelKey').placeholder = keyPlaceholder();
}

function statusRows() {
  const rows = [
    [t('settings.model.current'), html`${t('settings.model.provider.' + status.provider)} ·
      <span class="mono">${status.chat_model}</span>${status.coder_model !== status.chat_model
        ? html` · <span class="mono">${status.coder_model}</span>` : ''}`],
  ];
  if (status.endpoint) rows.push([t('settings.model.endpoint'), html`<span class="mono">${status.endpoint}</span>`]);

  let key = t('settings.model.key.missing');
  if (status.key_configured) {
    key = status.key_hint ? t('settings.model.key.hint', { hint: status.key_hint }) : t('settings.model.key.configured');
  }
  const rejection = knownRejection(status);
  rows.push([t('settings.model.keyStatus'), html`${key}${rejection
    ? html` <span class="chip chip-danger">${t('settings.model.key.rejected')}${rejection.status_code
      ? ` · HTTP ${rejection.status_code}` : ''}</span>` : ''}`]);

  if (status.editable) {
    const saved = status.source === 'page'
      ? [t('settings.model.source.page'), (status.saved_at || '').replace('T', ' ').slice(0, 16), status.saved_by]
        .filter(Boolean).join(' · ')
      : t('settings.model.source.server');
    rows.push([t('settings.model.source'), saved]);
  }
  return html`<dl class="kv">${rows.map(([label, value]) => html`<dt>${label}</dt><dd>${value}</dd>`)}</dl>`;
}

function warnings() {
  const items = [];
  if (insecurePage(window.location)) items.push('insecurePage');
  if (status.store_problem) items.push('storeProblem');
  if (status.custom_endpoint) items.push('customEndpoint');
  return items.map((name) => notice(
    'warn', 'warn', t('settings.model.warn.' + name), t('settings.model.warn.' + name + '.body'),
  ));
}

function reportBlock() {
  if (!report) return '';
  const lines = report.results.map((item) => html`<li><span class="mono">${item.model}</span>${t('settings.model.report.line', {
    roles: item.roles.map((role) => t('settings.model.role.' + role)).join(t('common.listSep')),
    outcome: t('settings.model.outcome.' + item.outcome),
  })}${item.status_code && !item.ok ? ` · HTTP ${item.status_code}` : ''} · ${item.duration_ms} ms${item.message
    ? html`<p class="original">${t('settings.model.report.server', { message: item.message })}</p>` : ''}</li>`);
  const notes = (report.warnings || []).map((code) => html`<li>${t('settings.model.note.' + code)}</li>`);
  return notice(
    report.ok ? 'info' : 'danger',
    report.ok ? 'check' : 'error',
    t(report.ok ? 'settings.model.report.ok' : 'settings.model.report.failed'),
    html`<ul>${lines}${notes}</ul>`,
  );
}

function formBlock() {
  const entry = status.providers[form.provider] || {};
  const off = busy ? 'disabled' : '';
  return html`<form class="stack" data-form="model">
    <div class="field"><label for="modelProvider">${t('settings.model.provider')}</label>
      <select class="select select-auto" id="modelProvider" ${off}>${PROVIDERS.map((name) => html`<option
        value="${name}" ${form.provider === name ? 'selected' : ''}>${t('settings.model.provider.' + name)}</option>`)}</select></div>
    <div class="field"><label for="modelBase">${t('settings.model.baseUrl')}</label>
      <input class="input" id="modelBase" value="${form.baseUrl}" placeholder="${entry.official_base_url || ''}"
        autocomplete="off" spellcheck="false" ${off}>
      <span class="hint">${t('settings.model.baseUrl.hint', { official: entry.official_base_url || '' })}</span></div>
    <div class="field"><label for="modelKey">${t('settings.model.apiKey')}</label>
      <input class="input" id="modelKey" type="password" value="${typedKey}" placeholder="${keyPlaceholder()}"
        autocomplete="off" spellcheck="false" ${off}>
      <span class="hint">${t('settings.model.apiKey.hint')}</span></div>
    <div class="grid-2">
      <div class="field"><label for="modelChat">${t('settings.model.chatModel')}</label>
        <input class="input" id="modelChat" value="${form.chatModel}" autocomplete="off" spellcheck="false" ${off}>
        <span class="hint">${t('settings.model.chatModel.hint')}</span></div>
      <div class="field"><label for="modelCoder">${t('settings.model.coderModel')}</label>
        <input class="input" id="modelCoder" value="${form.coderModel}" autocomplete="off" spellcheck="false" ${off}>
        <span class="hint">${t('settings.model.coderModel.hint')}</span></div>
    </div>
    <div class="row">
      <button type="button" class="btn btn-sm" data-action="model-test" ${off}>${t('settings.model.test')}</button>
      <button type="submit" class="btn btn-sm btn-primary" ${off}>${t('settings.model.save')}</button>
      ${status.source === 'page' || status.store_problem ? html`<button type="button" class="btn btn-sm btn-ghost"
        data-action="model-restore" ${off}>${t('settings.model.restore')}</button>` : ''}
    </div>
    <p class="desc">${t('settings.model.test.hint')}</p>
  </form>`;
}

function body() {
  if (!isAdmin()) return html`<p class="desc">${t('settings.model.denied')}</p>`;
  if (phase === 'idle' || phase === 'loading') {
    return html`<div class="loading" role="status"><span class="spinner"></span>${t('settings.model.loading')}</div>`;
  }
  if (phase === 'error') {
    // 老版本的服务没有这组接口：不是出错，是没有这个功能
    if (failure.status === 404) return html`<p class="desc">${t('settings.model.unsupported')}</p>`;
    return html`<div class="stack-sm" role="status"><p class="desc">${t('settings.model.loadFailed')} ${failureText(failure)}</p>
      <div><button type="button" class="btn btn-sm" data-action="model-reload">${t('action.tryAgain')}</button></div></div>`;
  }
  return html`${statusRows()}
    ${status.editable
      ? html`${warnings()}${formBlock()}`
      : notice('info', 'lock', t('settings.model.reason.' + status.reason), t('settings.model.reason.' + status.reason + '.body'))}
    <div class="stack-sm" role="status">
      ${busy ? html`<div class="loading"><span class="spinner"></span>${t('settings.model.busy.' + busy)}</div>` : ''}
      ${outcome && outcome.ok ? html`<p><span class="chip chip-ok">${outcome.text}</span></p>` : ''}
      ${outcome && !outcome.ok ? notice('danger', 'error', t('settings.model.failed'), outcome.text) : ''}
      ${reportBlock()}
    </div>`;
}

export function modelSection() {
  return html`<section class="stack model-settings">
    <h3>${t('settings.model')}</h3>
    <p class="desc">${t('settings.model.desc')}</p>
    ${body()}
  </section>`;
}

export function initModelSection(draw) {
  redraw = draw;
}
