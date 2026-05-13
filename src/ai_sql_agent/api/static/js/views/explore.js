/* 数据浏览：不提问，先看看系统里有什么。

   两层：目录（业务域 → 表）与单张表（字段说明 + 只读预览）。
   目录来自 /catalog，预览来自 /catalog/{库}/{表}/rows——两个都是服务端按当前身份
   算出来的。这个文件里没有任何表名、字段名或样例数据：服务端没给的，这里画不出来。 */

import { api } from '../api.js';
import {
  catalog, domainTitle, domains, ensureColumns, findDomain, findTable, loadCatalog,
} from '../catalog.js';
import { $, announce, copyText, delegate, html, icon, setHtml, toast } from '../dom.js';
import { descriptionOf, formatRange, formatTime, labelOf, localized } from '../format.js';
import { has, t, tn } from '../i18n.js';
import { emit, on, state } from '../state.js';
import { notice } from '../steps.js';
import { renderTable } from '../table.js';

const PAGE_SIZES = [25, 50, 100];
const OPERATORS = {
  text: ['contains', 'eq', 'starts_with', 'is_empty', 'not_empty'],
  number: ['eq', 'gte', 'lte', 'gt', 'lt', 'is_empty', 'not_empty'],
  date: ['eq', 'gte', 'lte', 'is_empty', 'not_empty'],
  binary: ['is_empty', 'not_empty'],     // 二进制字段只能问「有没有内容」
};
const VALUELESS = ['is_empty', 'not_empty'];

let root;
let route = { database: '', table: '' };
let tab = 'records';
let query = freshQuery();
let draft = { column: '', op: '' };
let preview = { status: 'idle', data: null, error: null };
let controller = null;
let sequence = 0;
let stale = false;            // 这一页不在眼前时目录变了（换了身份、重连）：回来要重读

function freshQuery() {
  return { page: 1, pageSize: PAGE_SIZES[0], search: '', filters: [], sort: null };
}

const operatorsFor = (column) => OPERATORS[column && OPERATORS[column.kind] ? column.kind : 'text'];
const serverOperators = () => (state.service && state.service.preview && state.service.preview.operators) || null;
const maxFilters = () => (state.service && state.service.preview && state.service.preview.max_filters) || 5;

/* 说明文字。数据字典没有当前语言的版本时照原文给，并标明是原文。 */
function description(entity) {
  const { text, original } = descriptionOf(entity);
  if (!text) return html`<span class="muted">${t('data.noDescription')}</span>`;
  return original
    ? html`<span class="original-note" lang="zh" title="${t('data.originalNote')}">${text}</span>`
    : html`<span>${text}</span>`;
}

/* 英文界面而数据字典只有中文说明时，先给一句英文概要，原文跟在后面。
   概要只是把目录里已有的名字列出来（这张表有哪些字段、这个域有哪些表），
   不是对原文的翻译——口径这种东西译错一个词，比让人看到一段中文危险。 */
function summarized(entity, key, names) {
  const { original } = descriptionOf(entity);
  if (!original || !names.length) return description(entity);
  return html`<span>${t(key, { names: names.join(', ') })}</span>${description(entity)}`;
}

const tableSummary = (table) => summarized(
  table,
  'data.holds',
  (table.columns || [])
    .filter((column) => column.status === 'available')
    .map((column) => (column.unit ? `${labelOf(column)} (${column.unit})` : labelOf(column))),
);

const domainSummary = (domain) => summarized(domain, 'data.contains', domain.tables.map(labelOf));

/* ─── 目录还没拿到时的几种状态 ──────────────────────────────────── */

function catalogProblem() {
  if (catalog.status === 'idle' || catalog.status === 'loading') {
    return html`<div class="loading" role="status"><span class="spinner"></span>${t('data.loading')}</div>`;
  }
  if (catalog.status === 'unsupported') {
    return notice('info', 'info', t('data.unsupported.title'), html`<p>${t('data.unsupported.body')}</p>`);
  }
  if (catalog.status === 'disabled') {
    return notice('info', 'info', t('data.disabled.title'), html`<p>${t('data.disabled.body')}</p>`);
  }
  if (catalog.status === 'error') {
    const error = catalog.error || {};
    const key = error.status === 0 ? 'error.network'
      : has('error.' + error.status) ? 'error.' + error.status
        : error.status >= 500 ? 'error.5xx' : 'error.generic';
    return html`${notice('danger', 'error', t('data.error.title'), html`<p>${t(key)}</p>
        <p class="original">${error.status ? `HTTP ${error.status}` : t('error.noResponse')}${error.serverMessage
          ? html` · <span lang="zh">${error.serverMessage}</span>` : ''}</p>`)}
      <div class="row"><button type="button" class="btn btn-sm" data-action="reload">${icon('refresh', 'icon-sm')}${t('action.tryAgain')}</button>
        <button type="button" class="btn btn-sm btn-ghost" data-action="open-settings">${t('action.checkSettings')}</button></div>`;
  }
  return '';
}

/* ─── 目录 ─────────────────────────────────────────────────────── */

function domainCard(domain) {
  const unavailable = domain.status !== 'available';
  const reason = has('data.reason.' + domain.status_reason) ? t('data.reason.' + domain.status_reason) : t('data.reason.unknown');
  return html`<section class="domain" aria-labelledby="domain-${domain.database}">
    <div class="domain-head">
      <h2 class="domain-title" id="domain-${domain.database}">${domainTitle(domain)}
        <span class="tech">${domain.database}</span>
        ${unavailable ? html`<span class="chip chip-danger">${icon('error', 'icon-sm')}${t('data.unavailable')}</span>` : ''}</h2>
      <p class="domain-desc">${domainSummary(domain)}</p>
    </div>
    ${unavailable
      ? html`<div class="card-pad">${notice('danger', 'error', t('data.unavailable.title'), html`<p>${reason}</p><p>${t('data.unavailable.body')}</p>`)}</div>`
      : domain.tables.length
        ? html`<ul class="table-list">${domain.tables.map((table) => html`<li>
            <a class="table-link" href="#/data/${encodeURIComponent(domain.database)}/${encodeURIComponent(table.name)}">
              <span><span class="name">${labelOf(table)}</span> <span class="tech">${table.name}</span></span>
              <span class="desc">${tableSummary(table)}</span>
              <span class="count">${table.status === 'missing'
                ? html`<span class="chip chip-warn">${t('data.missing')}</span>`
                : tn('data.fieldCount', (table.columns || []).length || table.column_count || 0)}</span>
            </a></li>`)}</ul>`
        : html`<p class="card-pad muted">${t('data.noTables')}</p>`}
  </section>`;
}

function drawCatalog() {
  const problem = catalogProblem();
  const data = catalog.data;
  setHtml(root, html`<div class="page stack-lg">
    <div class="page-head">
      <div><h1 class="page-title" tabindex="-1" data-role="view-title">${t('data.title')}</h1>
        <p class="page-sub">${t('data.lead')}</p></div>
      ${data ? html`<div class="row"><span class="muted">${t('data.readAt', { time: formatTime(data.generated_at) })}</span>
        <button type="button" class="btn btn-sm" data-action="reload">${icon('refresh', 'icon-sm')}${t('action.refresh')}</button></div>` : ''}
    </div>
    ${problem}
    ${data ? html`
      ${data.access_limited ? notice('info', 'info', t('data.limited.title'), html`<p>${t('data.limited.body')}</p>`) : ''}
      ${domains().length ? domains().map(domainCard) : html`<div class="empty-state"><strong>${t('data.empty.title')}</strong>${t('data.empty.body')}</div>`}
      ${data.glossary && data.glossary.length ? html`<details class="disclosure">
        <summary>${t('data.glossary', { n: data.glossary.length })}</summary>
        <div class="disclosure-body"><p class="muted">${t('data.glossaryNote')}</p>
          <dl class="glossary">${data.glossary.map((item) => {
            const term = localized(item, 'term');
            const definition = localized(item, 'definition');
            return html`<div><dt ${term.original ? html`lang="zh"` : ''}>${term.text}</dt>
              <dd ${definition.original ? html`lang="zh" class="original-note" title="${t('data.originalNote')}"` : ''}>${definition.text}</dd></div>`;
          })}</dl></div>
      </details>` : ''}
      ${state.learning ? html`<section class="learn"><div class="learn-head"><span class="row">${icon('learn')}${t('learn.catalog.title')}</span></div>
        <div class="learn-body"><p class="learn-note">${t('learn.catalog.note')}</p>
        <p class="learn-src">src/ai_sql_agent/catalog/service.py · api/catalog.py · semantic/loader.py · retrieval/access.py</p></div></section>` : ''}
    ` : ''}
  </div>`);
}

/* ─── 单张表 ───────────────────────────────────────────────────── */

function previewColumns(table, data) {
  const entries = new Map((table.columns || []).map((column) => [column.name, column]));
  return data.columns.map((column) => {
    const entry = entries.get(column.name);
    return {
      key: column.name,
      label: entry ? labelOf(entry) : column.name,
      unit: entry ? entry.unit : '',
      numeric: column.kind === 'number',
      sortable: column.sortable !== false,
      plain: Boolean(entry && (entry.role === 'join_key' || entry.primary_key)),
      hint: entry ? descriptionOf(entry).text : '',
    };
  });
}

function filterLabel(table, filter) {
  const entry = (table.columns || []).find((column) => column.name === filter.column);
  const name = entry ? labelOf(entry) : filter.column;
  return VALUELESS.includes(filter.op)
    ? `${name} ${t('filter.op.' + filter.op)}`
    : `${name} ${t('filter.op.' + filter.op)} ${filter.value}`;
}

function toolbar(table) {
  const columns = (table.columns || [])
    .filter((column) => column.status === 'available' && column.previewable !== false);
  const chosen = columns.find((column) => column.name === draft.column) || columns[0];
  const allowed = serverOperators();
  const operators = operatorsFor(chosen).filter((op) => !allowed || allowed.includes(op));
  const op = operators.includes(draft.op) ? draft.op : operators[0];
  const full = query.filters.length >= maxFilters();

  return html`<form class="filters" data-form="search" role="search">
      <div class="field field-wide"><label for="tableSearch">${t('data.search')}</label>
        <input class="input" id="tableSearch" type="search" maxlength="100" value="${query.search}"
          placeholder="${t('data.searchPlaceholder')}" autocomplete="off"></div>
      <button type="submit" class="btn">${t('action.search')}</button>
    </form>
    <form class="filters" data-form="filter">
      <div class="field"><label for="filterColumn">${t('filter.field')}</label>
        <select class="select" id="filterColumn" data-input="filter-column">${columns.map((column) => html`<option
          value="${column.name}" ${chosen && column.name === chosen.name ? 'selected' : ''}>${labelOf(column)}</option>`)}</select></div>
      <div class="field"><label for="filterOp">${t('filter.condition')}</label>
        <select class="select" id="filterOp" data-input="filter-op">${operators.map((item) => html`<option
          value="${item}" ${item === op ? 'selected' : ''}>${t('filter.op.' + item)}</option>`)}</select></div>
      <div class="field" ${VALUELESS.includes(op) ? 'hidden' : ''}><label for="filterValue">${t('filter.value')}</label>
        <input class="input" id="filterValue" maxlength="200" autocomplete="off"
          ${chosen && chosen.kind === 'number' ? html`inputmode="decimal"` : ''}
          placeholder="${chosen && chosen.kind === 'date' ? 'YYYY-MM-DD' : chosen && chosen.value_range ? chosen.value_range : ''}"></div>
      <button type="submit" class="btn" ${full ? 'disabled' : ''}>${icon('plus', 'icon-sm')}${t('filter.add')}</button>
    </form>
    ${query.filters.length || query.search ? html`<div class="filter-chips" aria-label="${t('filter.active')}">
      ${query.search ? html`<span class="filter-chip">${t('filter.searchChip', { term: query.search })}
        <button type="button" data-action="clear-search" title="${t('filter.remove')}"
          aria-label="${t('filter.removeNamed', { name: t('filter.searchChip', { term: query.search }) })}">${icon('close', 'icon-sm')}</button></span>` : ''}
      ${query.filters.map((filter, index) => html`<span class="filter-chip">${filterLabel(table, filter)}
        <button type="button" data-action="remove-filter" data-index="${index}" title="${t('filter.remove')}"
          aria-label="${t('filter.removeNamed', { name: filterLabel(table, filter) })}">${icon('close', 'icon-sm')}</button></span>`)}
      <button type="button" class="link-btn" data-action="clear-filters">${t('filter.clear')}</button>
    </div>` : ''}`;
}

function previewError(error) {
  const code = error.code || '';
  const key = has('data.error.' + code) ? 'data.error.' + code
    : error.status === 0 ? 'error.network'
      : error.status === 401 ? 'error.401'
        : error.status === 403 ? 'error.403'
          : 'data.error.failed';
  return html`${notice('danger', 'error', t('data.error.previewTitle'), html`<p>${t(key)}</p>
      <p class="original">${error.status ? `HTTP ${error.status}` : t('error.noResponse')}${code ? ` · ${code}` : ''}${error.serverMessage
        ? html` · <span lang="zh">${error.serverMessage}</span>` : ''}</p>`)}
    <div class="row"><button type="button" class="btn btn-sm" data-action="retry-preview">${icon('refresh', 'icon-sm')}${t('action.tryAgain')}</button></div>`;
}

function results(table) {
  if (preview.status === 'error') return previewError(preview.error);
  if (!preview.data) {
    return html`<div class="loading" role="status"><span class="spinner"></span>${t('data.loadingRows')}</div>`;
  }

  const data = preview.data;
  const filtered = Boolean(query.search || query.filters.length);
  const columns = previewColumns(table, data);
  const from = data.offset + 1;
  const to = data.offset + data.rows.length;
  const entries = new Map((table.columns || []).map((column) => [column.name, column]));
  const dates = (data.date_ranges || []).map((range) =>
    `${entries.has(range.column) ? labelOf(entries.get(range.column)) : range.column} ${formatRange(range)}`);

  const count = data.rows.length === 0
    ? ''
    : data.total_rows != null
      ? t('data.range', { from, to, total: data.total_rows })
      : t('data.rangeUnknown', { from, to });

  let body;
  if (!data.rows.length && filtered) {
    body = html`<div class="empty-state"><strong>${t('data.noMatch.title')}</strong>${t('data.noMatch.body')}
      <div><button type="button" class="btn btn-sm" data-action="clear-filters">${t('filter.clear')}</button></div></div>`;
  } else if (!data.rows.length && data.offset > 0) {
    body = html`<div class="empty-state"><strong>${t('data.pastEnd.title')}</strong>
      <div><button type="button" class="btn btn-sm" data-action="page" data-page="1">${t('data.firstPage')}</button></div></div>`;
  } else if (!data.rows.length) {
    body = html`<div class="empty-state"><strong>${t('data.emptyTable.title')}</strong>${t('data.emptyTable.body')}</div>`;
  } else {
    body = renderTable({
      columns,
      rows: data.rows,
      sort: query.sort,
      sortAction: 'sort',
      label: t('data.tableLabel', { table: labelOf(table) }),
    });
  }

  return html`<div class="stack" aria-busy="${preview.status === 'loading' ? 'true' : 'false'}">
    <div class="result-meta" role="status">
      ${count ? html`<span><strong>${count}</strong></span>` : ''}
      <span><strong>${t('meta.recordDates')}:</strong> ${dates.length ? dates.join('; ') : t('data.noDateFields')}</span>
      <span title="${t('meta.readHint')}">${data.as_of_source === 'snapshot'
        ? t('meta.readSnapshot', { time: formatTime(data.as_of) })
        : data.as_of ? t('meta.readLive', { time: formatTime(data.as_of) }) : t('meta.readUnknown')}</span>
      ${preview.status === 'loading' ? html`<span class="row"><span class="spinner"></span>${t('data.loadingRows')}</span>` : ''}
    </div>
    ${data.columns_hidden ? html`<p class="muted">${icon('info', 'icon-sm')} ${t('data.columnsHidden')}</p>` : ''}
    ${(data.columns_unsupported || []).length ? html`<p class="muted">${icon('info', 'icon-sm')} ${t('data.columnsUnsupported', {
      names: data.columns_unsupported.join(', '),
    })}</p>` : ''}
    ${data.search_columns_limited ? html`<p class="muted">${icon('info', 'icon-sm')} ${t('data.searchLimited', {
      n: (data.search_columns || []).length,
    })}</p>` : ''}
    ${data.cells_truncated ? html`<p class="muted">${icon('info', 'icon-sm')} ${t('data.cellsTruncated', { n: data.cell_limit })}</p>` : ''}
    ${data.stable_order === false && (data.has_more || data.page > 1)
      ? html`<p class="muted">${icon('info', 'icon-sm')} ${t('data.unstableOrder')}</p>` : ''}
    ${body}
    <div class="table-foot">
      <label class="row">${t('data.pageSize')}
        <select class="select select-auto" data-input="page-size">${PAGE_SIZES.map((size) => html`<option
          value="${size}" ${size === query.pageSize ? 'selected' : ''}>${size}</option>`)}</select></label>
      <span class="pager">
        <button type="button" class="btn btn-sm" data-action="page" data-which="prev" data-page="${data.page - 1}" ${data.page <= 1 ? 'disabled' : ''}>${t('table.prev')}</button>
        <span>${t('data.pageNumber', { page: data.page })}</span>
        <button type="button" class="btn btn-sm" data-action="page" data-which="next" data-page="${data.page + 1}" ${data.has_more ? '' : 'disabled'}>${t('table.next')}</button>
      </span>
    </div>
    ${state.learning ? html`<section class="learn"><div class="learn-head"><span class="row">${icon('learn')}${t('learn.preview.title')}</span>
        <span class="chip chip-info chip-wrap">POST /catalog/${route.database}/${route.table}/rows</span></div>
      <div class="learn-body stack">
        <div class="row-between"><span class="muted">${t('learn.preview.sql', { dialect: data.dialect })}</span>
          <button type="button" class="btn btn-sm btn-ghost" data-action="copy-sql">${icon('copy', 'icon-sm')}${t('action.copySql')}</button></div>
        <pre class="code">${data.sql}</pre>
        <dl class="kv"><dt>${t('process.traceId')}</dt><dd><span class="mono">${data.trace_id}</span></dd></dl>
        <p class="learn-note">${t('learn.preview.note')}</p>
        <p class="learn-src">src/ai_sql_agent/catalog/preview.py · catalog/service.py · execution/sqlite_executor.py · auth/enforcement.py</p>
      </div></section>` : ''}
  </div>`;
}

function fieldItem(column) {
  const role = column.role && has('role.' + column.role) ? t('role.' + column.role) : '';
  const note = descriptionOf(column);
  const usage = localized(column, 'usage');
  // 数据字典里当前语言能读的说明排最前；只有原文的那几段，先给界面语言能读的
  // 结构化信息（角色、取值、关联），原文退到后面。说明和用途各自判断，互不连带。
  const dictionary = [note, usage.text && usage.text !== note.text ? usage : null]
    .filter((item) => item && item.text);
  const paragraph = (item) => html`<p ${item.original
    ? html`class="original-note" lang="zh" title="${t('data.originalNote')}"` : ''}>${item.text}</p>`;
  const readable = dictionary.filter((item) => !item.original).map(paragraph);
  const originals = dictionary.filter((item) => item.original).map(paragraph);
  return html`<li>
    <div>
      <div class="field-name">${labelOf(column)}</div>
      <div class="tech">${column.name}</div>
      <div class="field-facts">
        ${column.status === 'missing' ? html`<span class="chip chip-warn">${t('data.missing')}</span>` : ''}
        ${column.primary_key ? html`<span class="chip">${t('data.key')}</span>` : ''}
        ${column.status !== 'missing' && column.previewable === false ? html`<span class="chip chip-warn">${t('data.notInPreview')}</span>` : ''}
        ${has('kind.' + column.kind) && column.status !== 'missing' ? html`<span class="chip">${t('kind.' + column.kind)}</span>` : ''}
        ${column.unit ? html`<span class="chip">${t('data.unit', { unit: column.unit })}</span>` : ''}
        ${column.data_type ? html`<span class="tech">${column.data_type}</span>` : ''}
      </div>
    </div>
    <div class="field-desc">
      ${readable}
      ${role ? html`<p>${role}</p>` : ''}
      ${column.value_range ? html`<p><span class="muted">${t('data.values')}</span> <span class="mono">${column.value_range}</span></p>` : ''}
      ${(column.references || []).map((reference) => {
        const target = findTable(reference.database, reference.table);
        const domain = findDomain(reference.database);
        const name = `${domain ? domainTitle(domain) : reference.database} › ${target ? labelOf(target) : reference.table} › ${reference.column}`;
        const remark = localized(reference, 'note');
        return html`<p>${icon('link', 'icon-sm')} <span class="muted">${t(reference.relation === 'cross_database' ? 'data.linkedAcross' : 'data.linked')}</span>
          <a href="#/data/${encodeURIComponent(reference.database)}/${encodeURIComponent(reference.table)}">${name}</a>
          ${remark.text ? html`<span ${remark.original ? html`class="original-note" lang="zh"` : html`class="muted"`}>${remark.text}</span>` : ''}</p>`;
      })}
      ${originals}
      ${!dictionary.length ? html`<p class="muted">${t(column.documented ? 'data.noDescription' : 'data.undocumented')}</p>` : ''}
    </div>
  </li>`;
}

function drawTable() {
  const problem = catalogProblem();
  const domain = findDomain(route.database);
  const table = findTable(route.database, route.table);

  if (problem || !catalog.data) {
    setHtml(root, html`<div class="page stack-lg"><h1 class="page-title" tabindex="-1" data-role="view-title">${t('data.title')}</h1>${problem}</div>`);
    return;
  }
  if (domain && domain.status !== 'available' && !table) {
    // 库读不到时目录里没有它的表清单：这不是「没有这张表」，是「现在看不了」
    const reason = has('data.reason.' + domain.status_reason) ? t('data.reason.' + domain.status_reason) : t('data.reason.unknown');
    setHtml(root, html`<div class="page stack-lg">
      <nav class="breadcrumbs" aria-label="${t('data.breadcrumb')}"><a href="#/data">${t('data.title')}</a>
        <span aria-hidden="true">›</span><span>${domainTitle(domain)}</span></nav>
      <h1 class="page-title" tabindex="-1" data-role="view-title">${route.table}</h1>
      ${notice('danger', 'error', t('data.unavailable.title'), html`<p>${reason}</p><p>${t('data.unavailable.body')}</p>`)}
      <div class="row"><button type="button" class="btn btn-sm" data-action="reload">${icon('refresh', 'icon-sm')}${t('action.tryAgain')}</button>
        <a class="btn btn-sm btn-ghost" href="#/data">${icon('back', 'icon-sm')}${t('data.backToCatalog')}</a></div>
    </div>`);
    return;
  }
  if (!domain || !table) {
    setHtml(root, html`<div class="page stack-lg">
      <nav class="breadcrumbs" aria-label="${t('data.breadcrumb')}"><a href="#/data">${t('data.title')}</a></nav>
      <h1 class="page-title" tabindex="-1" data-role="view-title">${t('data.notFound.title')}</h1>
      ${notice('info', 'info', t('data.notFound.title'), html`<p>${t('data.notFound.body')}</p>`)}
      <p><a class="btn" href="#/data">${icon('back', 'icon-sm')}${t('data.backToCatalog')}</a></p>
    </div>`);
    return;
  }

  const available = table.status === 'available';
  const columns = table.columns || [];
  const unavailableDomain = domain.status !== 'available';
  setHtml(root, html`<div class="page stack-lg">
    <nav class="breadcrumbs" aria-label="${t('data.breadcrumb')}">
      <a href="#/data">${t('data.title')}</a><span aria-hidden="true">›</span>
      <span>${domainTitle(domain)}</span><span aria-hidden="true">›</span><span>${labelOf(table)}</span>
    </nav>
    <div class="page-head">
      <div><h1 class="page-title" tabindex="-1" data-role="view-title">${labelOf(table)}</h1>
        <p class="tech">${domain.database}.${table.name} · ${t('data.readOnly')}</p>
        <p class="page-sub">${tableSummary(table)}</p></div>
      <div class="row">
        <button type="button" class="btn btn-sm" data-action="ask-about">${icon('ask', 'icon-sm')}${t('data.askAbout')}</button>
      </div>
    </div>
    ${!available ? notice('warn', 'warn', t('data.missing.title'), html`<p>${t('data.missing.body')}</p>`) : ''}
    ${unavailableDomain ? notice('danger', 'error', t('data.unavailable.title'), html`<p>${t('data.unavailable.body')}</p>`) : ''}
    <div class="tabs" role="tablist" aria-label="${labelOf(table)}">
      <button type="button" class="tab" role="tab" id="tab-records" aria-controls="tabpanel" aria-selected="${String(tab === 'records')}"
        tabindex="${tab === 'records' ? '0' : '-1'}" data-action="tab" data-tab="records">${t('data.tab.records')}</button>
      <button type="button" class="tab" role="tab" id="tab-fields" aria-controls="tabpanel" aria-selected="${String(tab === 'fields')}"
        tabindex="${tab === 'fields' ? '0' : '-1'}" data-action="tab" data-tab="fields">${t('data.tab.fields', { n: columns.length })}</button>
    </div>
    <div id="tabpanel" role="tabpanel" aria-labelledby="tab-${tab}" class="stack">
      ${tab === 'fields'
        ? html`<ul class="field-list">${columns.map(fieldItem)}</ul>`
        : !available || unavailableDomain
          ? html`<p class="muted">${t('data.noPreview')}</p>`
          : !table.previewable
            ? notice('info', 'info', t('data.previewUnsupported.title'), html`<p>${t(domain.preview_supported
              ? 'data.previewUnsupported.names' : 'data.previewUnsupported.body')}</p>`)
            : html`<div data-role="toolbar" class="stack">${toolbar(table)}</div><div data-role="results">${results(table)}</div>`}
    </div>
  </div>`);
}

function drawResults() {
  const slot = root.querySelector('[data-role="results"]');
  const table = findTable(route.database, route.table);
  if (slot && table) setHtml(slot, results(table));
}

/* 重画工具栏。keep 说明哪些「打了一半还没提交」的输入要留住：
   换一下筛选字段不该把刚打的搜索词清掉，加完一个筛选条件则应该把值清空等下一个。 */
function drawToolbar(keep = {}) {
  const slot = root.querySelector('[data-role="toolbar"]');
  const table = findTable(route.database, route.table);
  if (!slot || !table) return;
  const typed = {
    search: keep.search && $('tableSearch') ? $('tableSearch').value : null,
    value: keep.value && $('filterValue') ? $('filterValue').value : null,
  };
  setHtml(slot, toolbar(table));
  if (typed.search !== null && $('tableSearch')) $('tableSearch').value = typed.search;
  if (typed.value !== null && $('filterValue')) $('filterValue').value = typed.value;
}

async function loadPreview() {
  const table = findTable(route.database, route.table);
  if (!table || table.status !== 'available' || !table.previewable) return;
  if (controller) controller.abort();
  controller = new AbortController();
  const ticket = ++sequence;
  preview = { ...preview, status: 'loading', error: null };
  drawResults();

  try {
    const data = await api.rows(route.database, route.table, {
      page: query.page,
      page_size: query.pageSize,
      search: query.search,
      filters: query.filters,
      sort: query.sort ? { column: query.sort.key, direction: query.sort.direction } : null,
    }, controller.signal);
    if (ticket !== sequence) return;       // 更晚发出的请求已经接管了
    preview = { status: 'ready', data, error: null };
    announce(data.rows.length
      ? (data.total_rows != null
        ? t('data.range', { from: data.offset + 1, to: data.offset + data.rows.length, total: data.total_rows })
        : t('data.rangeUnknown', { from: data.offset + 1, to: data.offset + data.rows.length }))
      : t(query.search || query.filters.length ? 'data.noMatch.title' : 'data.emptyTable.title'));
  } catch (error) {
    if ((error && error.name === 'AbortError') || ticket !== sequence) return;
    preview = { status: 'error', data: null, error };
    announce(t('data.error.previewTitle'));
  }
  drawResults();
}

function draw() {
  if (route.table) drawTable();
  else drawCatalog();
}

/* 路由进来：#/data 或 #/data/<库>/<表> */
export async function showData(database = '', table = '') {
  const changed = route.database !== database || route.table !== table;
  route = { database, table };
  if (changed) {
    query = freshQuery();
    draft = { column: '', op: '' };
    preview = { status: 'idle', data: null, error: null };
    if (controller) controller.abort();
  } else if (stale) {
    // 离开这一页的时候换过身份：留着的那一页数据是按上一个身份读的，不能再摆出来
    preview = { status: 'idle', data: null, error: null };
    if (controller) controller.abort();
  }
  stale = false;
  if (catalog.status === 'idle') loadCatalog();
  draw();
  if (!table || catalog.status !== 'ready') return;

  try { await ensureColumns(database, table); } catch (_) { /* 取不到字段说明时预览照常 */ }
  if (route.database !== database || route.table !== table) return;
  draw();
  if (tab === 'records' && (changed || preview.status === 'idle')) loadPreview();
}

function applyQuery(change) {
  query = { ...query, ...change };
  loadPreview();
}

export function initExplore() {
  root = $('view-data');

  root.addEventListener('submit', (event) => {
    const form = event.target;
    if (form.dataset.form === 'search') {
      event.preventDefault();
      applyQuery({ search: $('tableSearch').value.trim(), page: 1 });
      drawToolbar({ value: true });
    } else if (form.dataset.form === 'filter') {
      event.preventDefault();
      const column = $('filterColumn').value;
      const op = $('filterOp').value;
      const value = VALUELESS.includes(op) ? '' : $('filterValue').value.trim();
      if (!column || (!VALUELESS.includes(op) && !value)) {
        if ($('filterValue')) $('filterValue').focus();
        return;
      }
      if (query.filters.length >= maxFilters()) return;
      applyQuery({ filters: [...query.filters, { column, op, value }], page: 1 });
      drawToolbar({ search: true });
    }
  });

  root.addEventListener('change', (event) => {
    const kind = event.target.dataset.input;
    // 重画是因为可选的条件、输入提示跟着字段变。焦点留在刚才操作的那个下拉框上
    //（重画按 id 找回它）：用方向键挨个看选项时，每按一下都会触发一次 change。
    if (kind === 'filter-column') { draft = { column: event.target.value, op: '' }; drawToolbar({ search: true, value: true }); }
    if (kind === 'filter-op') { draft = { ...draft, column: $('filterColumn').value, op: event.target.value }; drawToolbar({ search: true, value: true }); }
    if (kind === 'page-size') applyQuery({ pageSize: Number(event.target.value) || PAGE_SIZES[0], page: 1 });
  });

  root.addEventListener('keydown', (event) => {
    // 标签页：左右方向键切换（roving tabindex）
    if (!event.target.matches('[role="tab"]') || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    tab = tab === 'records' ? 'fields' : 'records';
    draw();
    if (tab === 'records' && preview.status === 'idle') loadPreview();
    $('tab-' + tab).focus();
  });

  delegate(root, 'click', {
    reload: () => loadCatalog({ refresh: true }),
    'open-settings': () => emit('open-settings'),
    tab: (target) => {
      tab = target.dataset.tab;
      draw();
      if (tab === 'records' && preview.status === 'idle') loadPreview();
      $('tab-' + tab).focus();
    },
    sort: (target) => {
      const key = target.dataset.key;
      let sort = null;
      if (!query.sort || query.sort.key !== key) sort = { key, direction: 'asc' };
      else if (query.sort.direction === 'asc') sort = { key, direction: 'desc' };
      applyQuery({ sort, page: 1 });
    },
    page: (target) => applyQuery({ page: Math.max(1, Number(target.dataset.page) || 1) }),
    'remove-filter': (target) => {
      const index = Number(target.dataset.index);
      applyQuery({ filters: query.filters.filter((_, position) => position !== index), page: 1 });
      drawToolbar({ search: true, value: true });
    },
    'clear-search': () => { applyQuery({ search: '', page: 1 }); drawToolbar({ value: true }); },
    'clear-filters': () => { applyQuery({ search: '', filters: [], page: 1 }); drawToolbar(); },
    'retry-preview': () => loadPreview(),
    'ask-about': () => {
      const table = findTable(route.database, route.table);
      if (!table) return;
      pendingPrefill = t('data.askPrefill', { table: labelOf(table) });
      location.hash = '#/ask';
    },
    'copy-sql': async () => {
      if (preview.data) toast(t(await copyText(preview.data.sql) ? 'toast.copied' : 'toast.copyFailed'));
    },
  });

  on('catalog', () => {
    if (root.hidden) { stale = true; return; }
    if (route.table && catalog.status === 'ready') {
      // 目录刷新后表可能变了：这页的预览也跟着重读，不留一份对不上的旧数据
      preview = { status: 'idle', data: null, error: null };
      showData(route.database, route.table);
    } else {
      draw();
    }
  });
  on('lang', () => { if (!root.hidden) draw(); });
  on('learning', () => { if (!root.hidden) draw(); });
}

/* 「就这张表提问」带去问答页的话头；问答页显示出来之后取走。 */
let pendingPrefill = '';
export function takePrefill() {
  const text = pendingPrefill;
  pendingPrefill = '';
  return text;
}
