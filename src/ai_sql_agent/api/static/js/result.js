/* 一轮问答的结果卡片。

   从上到下：结论 → 需要人注意的提示 → 表格 → 来源与数据日期 → 操作与反馈 →
   查询过程（普通模式默认收起；学习模式换成逐步讲解）。

   页面不显示服务端 answer 字段里那串「已完成数据库查询。查询结果：[JSON]」——
   那是给下游程序留的文本。这里的结论由结构化字段（行数、列、单位）直接拼出来：
   能拼出什么就说什么，拼不出来就只报行数，不编一句话。 */

import { domainTitleOf } from './catalog.js';
import { html, icon } from './dom.js';
import { descriptionOf, formatCell, formatRange, formatTime, recordDates, toTsv } from './format.js';
import { has, t, tn } from './i18n.js';
import { classify, splitSteps } from './model.js';
import { renderLearning, renderProcess } from './process.js';
import { renderRichText } from './richtext.js';
import { state } from './state.js';
import {
  anomalyHelp, anomalyLabel, failureReason, notice, sourceLabel, stepColumns, stepMatrix,
} from './steps.js';
import { renderPager, renderTable, sortRows } from './table.js';

const PAGE_SIZE = 25;
const MAX_FACTS = 8;
const MAX_NAMES = 5;

function tableState(turn, step) {
  turn.ui.tables[step.index] = turn.ui.tables[step.index] || { page: 1, sort: null };
  return turn.ui.tables[step.index];
}

/* 结论：{ headline, facts, names }。全部来自这一轮返回的行。 */
function conclusion(result, view, answers) {
  if (view.kind === 'no_data') return { headline: t('result.noData'), facts: [], names: '' };
  if (view.kind === 'failed') return { headline: t('result.failed'), facts: [], names: '' };

  const rowTotal = answers.reduce((sum, step) => sum + step.rowCount, 0);
  const facts = [];
  if (answers.every((step) => step.rowCount === 1)) {
    answers.forEach((step) => {
      const columns = stepColumns(step);
      columns.forEach((column) => {
        const cell = formatCell(step.rows[0][column.key], { unit: column.unit, plain: column.plain });
        facts.push({ label: column.label, value: cell.text, unit: cell.empty ? '' : column.unit });
      });
    });
  }

  let names = '';
  const last = answers[answers.length - 1];
  if (last && answers.length === 1 && last.rowCount > 1) {
    const columns = stepColumns(last);
    const naming = columns.find((column) => column.entry && column.entry.role === 'output_dimension')
      || columns.find((column) => /(^|_)name$/i.test(column.key));
    if (naming) {
      const values = [...new Set(last.rows.map((row) => row[naming.key]).filter((value) => value != null))];
      // 名字本身带逗号时（「3/4 in panel, 4 x 8 ft」）换成分号分隔，否则数不清有几个
      const separator = values.some((value) => String(value).includes(',')) ? '; ' : ', ';
      names = values.slice(0, MAX_NAMES).join(separator);
      if (values.length > MAX_NAMES) names += t('result.andMore', { n: values.length - MAX_NAMES });
    }
  }

  let headline;
  if (view.kind === 'partial') {
    headline = t('result.partial', { ok: result.steps.length - view.failedSteps.length, total: result.steps.length });
  } else if (answers.length > 1) {
    headline = t('result.parts', { n: answers.length, rows: rowTotal });
  } else if (view.truncated) {
    headline = t('result.recordsTruncated', { n: rowTotal });
  } else {
    headline = rowTotal === 1 ? t('result.oneRecord') : t('result.records', { n: rowTotal });
  }
  return { headline, facts: facts.length <= MAX_FACTS ? facts : [], names };
}

/* 复制用的纯文本结论。 */
export function summaryText(turn) {
  const result = turn.result;
  const view = classify(result);
  // 复制出去的文字会离开这个页面单独流传，页面上的提示必须跟着走：
  // 少一句「结果被截断」或「来自降级链路」，贴到邮件里就成了一个不带条件的结论。
  const caveats = [];
  if (view.degraded) caveats.push(t('notice.degraded.title'));
  if (view.kind === 'conversation') {
    return [turn.question, '', result.answer, '', t('notice.conversation.title'), ...caveats].join('\n');
  }
  const { answers } = splitSteps(result, turn.question);
  const summary = conclusion(result, view, answers);
  const lines = [turn.question, '', summary.headline + (summary.names ? `: ${summary.names}` : '')];
  summary.facts.forEach((fact) => {
    lines.push(`${fact.label}: ${fact.value}${fact.unit ? ' ' + fact.unit : ''}`);
  });
  const sources = [...new Set(answers.map((step) => sourceLabel(step.database)))];
  if (sources.length) lines.push('', `${t('meta.source')}: ${sources.join(', ')}`);
  if (result.asOf) lines.push(`${t('meta.readAtPlain')}: ${result.asOf}`);
  if (view.kind === 'partial') caveats.unshift(t('notice.partial.title'));
  if (view.truncated) caveats.unshift(t('notice.truncated.title'));
  if (view.needsReview) caveats.unshift(t('notice.review.title'));
  if (caveats.length) lines.push('', ...caveats);
  return lines.join('\n');
}

/* 这一轮有了结果时念给读屏的那句话：状态加结论，页面上最先该知道的那两样。 */
export function announcement(turn) {
  if (turn.status === 'stopped') return t('notice.stopped.title');
  if (turn.status === 'error' || !turn.result) return t('notice.service.title');
  const view = classify(turn.result);
  if (view.kind === 'conversation') return t('status.conversation');
  const { answers } = splitSteps(turn.result, turn.question);
  const summary = conclusion(turn.result, view, answers);
  const parts = [summary.headline + (summary.names ? `: ${summary.names}` : '')];
  if (view.needsReview) parts.push(t('status.review'));
  if (view.truncated) parts.push(t('status.truncated'));
  if (view.degraded) parts.push(t('status.degraded'));
  return parts.join('. ');
}

export function tableText(turn, stepIndex) {
  const step = turn.result.steps.find((item) => item.index === stepIndex);
  if (!step) return '';
  const columns = stepColumns(step);
  return toTsv(
    columns.map((column) => (column.unit ? `${column.label} (${column.unit})` : column.label)),
    stepMatrix(step, columns),
  );
}

/* 状态标签。每种状态都是「图标 + 文字」，颜色只是第三重提示。 */
function statusChips(view) {
  const map = {
    success: ['chip-ok', 'check', 'status.success'],
    no_data: ['chip-info', 'empty', 'status.no_data'],
    partial: ['chip-warn', 'warn', 'status.partial'],
    failed: ['chip-danger', 'error', 'status.failed'],
    conversation: ['', 'ask', 'status.conversation'],
  };
  const [tone, glyph, key] = map[view.kind];
  return html`<div class="status-line">
    <span class="chip ${tone}">${icon(glyph, 'icon-sm')}${t(key)}</span>
    ${view.needsReview ? html`<span class="chip chip-warn">${icon('warn', 'icon-sm')}${t('status.review')}</span>` : ''}
    ${view.truncated ? html`<span class="chip chip-warn">${t('status.truncated')}</span>` : ''}
    ${view.degraded ? html`<span class="chip chip-warn">${t('status.degraded')}</span>` : ''}
  </div>`;
}

function notices(result, view) {
  const out = [];

  if (view.kind === 'failed') {
    const reasons = view.failedSteps.map((step) => failureReason(step.error));
    const first = reasons[0] || { key: result.degraded ? 'fail.modelDown' : 'fail.generic', detail: '' };
    out.push(notice('danger', 'error', t('notice.failed.title'), html`
      <p>${t(first.key)}${first.detail ? html` <span class="mono">${first.detail}</span>` : ''}</p>
      <p>${t('notice.failed.hint')}</p>`));
  }

  if (view.kind === 'no_data') {
    const notes = view.findings.filter((finding) => finding.anomaly === 'empty_result');
    out.push(notice('info', 'empty', t('notice.noData.title'), html`
      <p>${t('notice.noData.body')}</p>
      ${notes.map((finding) => html`<p class="original" lang="zh">${t('notice.validatorNote')} ${finding.detail}</p>`)}`));
  }

  if (view.kind === 'partial') {
    out.push(notice('warn', 'warn', t('notice.partial.title'), html`
      <p>${t('notice.partial.body')}</p>
      <ul>${view.failedSteps.map((step) => {
        const reason = failureReason(step.error);
        return html`<li>${t('process.step', { n: step.index + 1 })} · ${sourceLabel(step.database)}: ${t(reason.key)}
          ${reason.detail ? html`<span class="mono">${reason.detail}</span>` : ''}</li>`;
      })}</ul>`));
  }

  if (view.needsReview) {
    const findings = view.findings.filter((finding) => !(view.kind === 'no_data' && finding.anomaly === 'empty_result'));
    out.push(notice('warn', 'warn', t('notice.review.title'), html`
      <p>${t('notice.review.body')}</p>
      ${findings.length ? html`<ul>${findings.map((finding) => html`<li>
          <strong>${anomalyLabel(finding.anomaly)}</strong>${finding.severity === 'block' ? ` · ${t('notice.review.block')}` : ''}
          ${anomalyHelp(finding.anomaly) ? html` — ${anomalyHelp(finding.anomaly)}` : ''}
          ${finding.detail ? html`<br><span class="original" lang="zh">${t('notice.validatorNote')} ${finding.detail}</span>` : ''}
        </li>`)}</ul>` : html`<p>${t('notice.review.unspecified')}</p>`}`));
  }

  if (view.truncated) {
    const limits = [...new Set(result.steps.filter((step) => step.truncated && step.rowLimit).map((step) => step.rowLimit))];
    out.push(notice('warn', 'warn', t('notice.truncated.title'), html`<p>${limits.length
      ? t('notice.truncated.body', { limit: limits.join(' / ') }) : t('notice.truncated.bodyNoLimit')}</p>`));
  }

  if (view.degraded && view.kind !== 'failed') {
    out.push(notice('warn', 'info', t('notice.degraded.title'), html`<p>${t('notice.degraded.body')}</p>`));
  }
  return out;
}

function answerTable(turn, step, multiple) {
  const columns = stepColumns(step);
  const ui = tableState(turn, step);
  let rows = stepMatrix(step, columns);
  if (ui.sort) {
    const index = columns.findIndex((column) => column.key === ui.sort.key);
    if (index >= 0) rows = sortRows(rows, index, ui.sort.direction);
  }
  const pageCount = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const page = Math.min(Math.max(1, ui.page), pageCount);
  const from = (page - 1) * PAGE_SIZE;
  const shown = rows.slice(from, from + PAGE_SIZE);
  const documented = columns.filter((column) => column.entry);
  const title = `${sourceLabel(step.database)} — ${tn('result.rows', step.rowCount)}`;

  return html`<div class="answer-table" data-step="${step.index}">
    <div class="table-toolbar">
      <span class="table-title">${multiple ? `${t('process.step', { n: step.index + 1 })} · ` : ''}${title}</span>
      <button type="button" class="btn btn-sm btn-ghost" data-action="copy-table" data-step="${step.index}">
        ${icon('copy', 'icon-sm')}${t('action.copyTable')}</button>
    </div>
    ${renderTable({ columns, rows: shown, sort: ui.sort, sortAction: 'sort-table', scope: step.index, label: title })}
    ${rows.length > PAGE_SIZE || step.truncated ? html`<div class="table-foot">
      <span>${rows.length > PAGE_SIZE
        ? t('table.range', { from: from + 1, to: from + shown.length, total: rows.length })
        : tn('result.rows', rows.length)}${step.truncated ? ` · ${t('table.truncatedRows')}` : ''}${ui.sort && step.truncated
        ? ` · ${t('table.sortedPartial')}` : ''}</span>
      ${renderPager({ page, pageCount, action: 'page-table', scope: step.index })}
    </div>` : ''}
    ${documented.length ? html`<details class="disclosure" data-detail="legend-${step.index}" ${turn.ui.open['legend-' + step.index] ? 'open' : ''}>
      <summary>${t('result.legend')}</summary>
      <div class="disclosure-body"><dl class="kv">${columns.map((column) => {
        const description = column.entry ? descriptionOf(column.entry) : null;
        return html`<dt>${column.label}${column.unit ? ` (${column.unit})` : ''}<br><span class="tech">${column.key}</span></dt>
          <dd>${description && description.text
            ? html`<span ${description.original ? html`lang="zh"` : ''}>${description.text}</span>`
            : html`<span class="muted">${column.entry ? t('data.noDescription') : t('result.computedColumn')}</span>`}</dd>`;
      })}</dl><p class="muted">${t('result.legendNote')}</p></div>
    </details>` : ''}
  </div>`;
}

function metaLine(result, answers) {
  const sources = [...new Set(answers.map((step) => sourceLabel(step.database)))];
  const dates = answers.flatMap((step) => {
    const columns = stepColumns(step);
    return recordDates(columns, step.rows, (row, column) => row[column.key])
      .map((range) => `${formatRange(range)} (${range.column.label})`);
  });
  const read = !result.asOf
    ? t('meta.readUnknown')
    : result.asOfSource === 'query_time'
      ? t('meta.readLive', { time: formatTime(result.asOf) })
      : result.asOfSource === 'snapshot'
        ? t('meta.readSnapshot', { time: formatTime(result.asOf) })
        : t('meta.readUnknown');

  return html`<div class="result-meta">
    ${sources.length ? html`<span><strong>${t('meta.source')}:</strong> ${sources.join(', ')}</span>` : ''}
    <span><strong>${t('meta.recordDates')}:</strong> ${dates.length ? dates.join('; ') : t('meta.noRecordDates')}</span>
    <span title="${t('meta.readHint')}">${read}</span>
  </div>`;
}

function feedback(turn) {
  const traceId = turn.result.traceId;
  if (!traceId || (state.service && state.service.feedback_enabled === false)) return '';
  const sent = state.feedback[traceId];
  const ui = turn.ui.feedback;
  if (sent) {
    return html`<div class="feedback" role="status">${icon('check', 'icon-sm')}<span>${t('feedback.sent.' + sent)}</span>
      <button type="button" class="link-btn" data-action="feedback-reset">${t('feedback.change')}</button></div>`;
  }
  return html`<div class="feedback">
    <span>${t('feedback.ask')}</span>
    ${['correct', 'wrong', 'unsure'].map((verdict) => html`<button type="button" class="btn btn-sm"
      data-action="feedback" data-verdict="${verdict}" ${ui.sending ? 'disabled' : ''}>${t('feedback.' + verdict)}</button>`)}
    <button type="button" class="link-btn" data-action="feedback-comment" aria-expanded="${ui.open ? 'true' : 'false'}">${t('feedback.addComment')}</button>
    ${ui.open ? html`<textarea class="textarea" rows="2" data-input="feedback-comment" maxlength="2000"
      aria-label="${t('feedback.commentLabel')}" placeholder="${t('feedback.commentPlaceholder')}">${ui.comment}</textarea>` : ''}
    ${ui.error ? html`<span class="chip chip-danger chip-wrap" role="alert">${t('feedback.failed')}: ${ui.error}</span>` : ''}
  </div>`;
}

function actions(view, turn) {
  // 历史页回看缓存结果时不放「再问一次 / 追问」：那一页有自己的「作为新查询重新执行」，
  // 两个长得像的按钮放在一起，人分不清哪个会去读最新数据。
  return html`<div class="answer-actions">
    <button type="button" class="btn btn-sm" data-action="copy-summary">${icon('copy', 'icon-sm')}${t('action.copyAnswer')}</button>
    ${turn.cached ? '' : html`<button type="button" class="btn btn-sm" data-action="ask-again">${icon('refresh', 'icon-sm')}${t('action.askAgain')}</button>`}
    ${!turn.cached && view.kind !== 'failed' ? html`<button type="button" class="btn btn-sm btn-ghost" data-action="follow-up">${t('action.followUp')}</button>` : ''}
  </div>`;
}

/* 这一轮的来源标记：历史回看、重新执行。旧结果不能看起来像新的。 */
function originNotice(turn) {
  if (turn.cached) {
    return notice('info', 'history', t('origin.cached.title'), html`<p>${t('origin.cached.body', {
      time: formatTime(turn.result.receivedAt),
    })}</p>`);
  }
  if (turn.rerunOf) {
    // 「刚读出来的」只在这次真的查了库时才说
    const key = turn.result.queriedDatabase ? 'origin.rerun.body' : 'origin.rerun.bodyNoQuery';
    return notice('info', 'refresh', t('origin.rerun.title'), html`<p>${t(key, { time: turn.rerunOf })}</p>`);
  }
  return '';
}

export function renderPending(turn, elapsedSeconds) {
  const stage = turn.stageNow;
  const stageText = stage && has('stage.' + stage.stage)
    ? t('stage.' + stage.stage) + (stage.database ? ` (${domainTitleOf(stage.database)})` : '')
    : t('ask.working');
  return html`<div class="answer">
    <div class="loading"><span class="spinner"></span>
      <span role="status">${stageText}</span>
      <span class="muted" data-role="elapsed" aria-live="off">${elapsedSeconds >= 2 ? t('ask.elapsed', { n: elapsedSeconds }) : ''}</span>
      <button type="button" class="btn btn-sm btn-ghost" data-action="stop">${icon('stop', 'icon-sm')}${t('action.stop')}</button>
    </div>
    ${state.learning ? renderLearning(turn) : ''}
  </div>`;
}

export function renderStopped() {
  return html`<div class="answer">
    ${notice('info', 'info', t('notice.stopped.title'), html`<p>${t('notice.stopped.body')}</p>`)}
    <div class="answer-actions"><button type="button" class="btn btn-sm" data-action="ask-again">
      ${icon('refresh', 'icon-sm')}${t('action.askAgain')}</button></div>
  </div>`;
}

/* 请求本身没成功（没连上、被拒、服务端出错）——和「查询跑了但失败」是两回事。 */
export function renderServiceError(turn) {
  const error = turn.error || {};
  const status = error.status || 0;
  let key = 'error.generic';
  if (error.code === 'stream_interrupted') key = 'error.interrupted';
  else if (error.code === 'bad_response') key = 'error.badResponse';
  else if (status === 0) key = 'error.network';
  else if (status === 401) key = 'error.401';
  else if (status === 403) key = 'error.403';
  else if (status === 429) key = 'error.429';
  else if (status === 400 || status === 422) key = 'error.400';
  else if (status >= 500) key = 'error.5xx';
  const needsSettings = status === 0 || status === 401 || status === 403;

  return html`<div class="answer">
    ${notice('danger', 'error', t('notice.service.title'), html`
      <p>${t(key)}</p>
      <p class="original">${status ? `HTTP ${status}` : t('error.noResponse')}${error.serverMessage
        ? html` · <span lang="zh">${error.serverMessage}</span>` : ''}</p>`)}
    <div class="answer-actions">
      <button type="button" class="btn btn-sm" data-action="ask-again">${icon('refresh', 'icon-sm')}${t('action.tryAgain')}</button>
      ${needsSettings ? html`<button type="button" class="btn btn-sm btn-ghost" data-action="open-settings">${t('action.checkSettings')}</button>` : ''}
    </div>
    ${state.learning ? renderLearning(turn) : ''}
  </div>`;
}

export function renderResult(turn) {
  const result = turn.result;
  const view = classify(result);
  const { answers } = splitSteps(result, turn.question);

  if (view.kind === 'conversation') {
    return html`<div class="answer">
      ${originNotice(turn)}
      ${statusChips(view)}
      ${renderRichText(result.answer)}
      ${notice('info', 'info', t('notice.conversation.title'), html`<p>${t('notice.conversation.body')}</p>`)}
      ${view.degraded ? notice('warn', 'info', t('notice.degraded.title'), html`<p>${t('notice.degradedChat.body')}</p>`) : ''}
      ${actions(view, turn)}
      ${feedback(turn)}
      ${state.learning ? renderLearning(turn) : renderProcess(turn)}
    </div>`;
  }

  const summary = conclusion(result, view, answers);
  const showTables = view.kind === 'success' || view.kind === 'partial';
  const tables = showTables ? answers.filter((step) => step.rowCount > 0) : [];

  return html`<div class="answer">
    ${originNotice(turn)}
    ${statusChips(view)}
    <p class="conclusion">${summary.headline}${summary.names ? html`: <strong>${summary.names}</strong>` : ''}
      ${result.currency && showTables ? html`<span class="chip" title="${t('meta.currencyHint')}">${t('meta.currency', { currency: result.currency })}</span>` : ''}</p>
    ${summary.facts.length ? html`<div class="facts">${summary.facts.map((fact) => html`<div class="fact">
      <div class="fact-label">${fact.label}</div>
      <div class="fact-value">${fact.value}${fact.unit ? html`<span class="fact-unit">${fact.unit}</span>` : ''}</div>
    </div>`)}</div>` : ''}
    ${notices(result, view)}
    ${tables.map((step) => answerTable(turn, step, tables.length > 1))}
    ${view.kind !== 'failed' ? metaLine(result, answers) : ''}
    ${actions(view, turn)}
    ${feedback(turn)}
    ${state.learning ? renderLearning(turn) : renderProcess(turn)}
  </div>`;
}
