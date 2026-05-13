/* 历史记录的页面侧缓存。问答页用它回填服务端记账的耗时与 token，历史页用它列记录。

   服务端的历史来自审计表，只有问题、SQL 与元数据，**没有结果集**。
   某条记录在这个标签页里有没有结果可看，取决于 state.resultCache，不取决于这里。 */

import { api } from './api.js';
import { emit } from './state.js';

export const history = {
  status: 'idle',     // idle | loading | ready | error
  items: [],
  error: null,
  loadedAt: null,
};

let pending = null;

export function loadHistory() {
  if (pending) return pending;
  if (history.status !== 'ready') history.status = 'loading';
  emit('history');
  pending = api.history(50)
    .then((data) => {
      history.items = Array.isArray(data.items) ? data.items : [];
      history.status = 'ready';
      history.error = null;
      history.loadedAt = new Date().toISOString();
    })
    .catch((error) => {
      history.status = 'error';
      history.error = error;
    })
    .finally(() => {
      pending = null;
      emit('history');
    });
  return pending;
}

export const findHistory = (traceId) => history.items.find((item) => item.trace_id === traceId) || null;
