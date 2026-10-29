const token = '__TOKEN__';
let filter = 'all',
  snapshot = null,
  showHandled = false,
  loading = false,
  last = '';
const edits = new Map();
const seen = new Set();
const statuses = {
  pending: '待你审查',
  sending: '正在发送，请勿重复操作',
  delivered: '已投递',
  unknown: '结果未确认，请在钉钉核对',
  stale: '已有更新，旧草稿不可发送',
  ignored: '已忽略',
};
const todoStatuses = {
  creating: '正在添加',
  created: '已添加到钉钉',
  unknown: '创建结果待核验，请到钉钉查看',
};

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
function error(text) {
  const box = document.getElementById('error');
  box.hidden = !text;
  box.textContent = text || '';
}
async function action(path, data) {
  const response = await fetch('/api/' + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Panel-Token': token },
    body: JSON.stringify(data),
  });
  const value = await response.json();
  if (!response.ok) throw Error(value.error || '操作未完成');
  await refresh();
}
function button(label, fn, cls) {
  const node = el('button', label, cls);
  node.onclick = async () => {
    node.disabled = true;
    node.setAttribute('aria-busy', 'true');
    try {
      await fn();
    } catch (e) {
      error(e.message);
    } finally {
      node.disabled = false;
      node.removeAttribute('aria-busy');
    }
  };
  return node;
}
function clean(text) {
  return text
    .replace(/\[图片消息\]\(mediaId=[^)]*\)/g, '[图片消息，暂未识别]')
    .replace('注意：如需下载使用dws chat message download-media命令下载', '');
}
let pendingTodo = null;
function openTodo(target) {
  pendingTodo = target;
  document.getElementById('todo-title').value = target.title || '';
  document.getElementById('todo-dialog-error').textContent = '';
  document.getElementById('todo-dialog').showModal();
  document.getElementById('todo-title').focus();
}
async function manualTodo(message) {
  openTodo({ key: message.key, title: '' });
}
function renderTodos(active) {
  const root = document.getElementById('todo-list');
  root.replaceChildren();
  const todos = active.flatMap((message) =>
    (message.todos || []).map((task) => ({ task, message })),
  );
  document.getElementById('todo-count').textContent = todos.filter(
    (item) => item.task.status === 'candidate',
  ).length;
  if (!todos.length) {
    root.append(el('p', '暂无', 'meta-line'));
    return;
  }
  for (const { task, message } of todos) {
    const item = el(
      'div',
      undefined,
      'todo-item ' +
        ({ created: 'done', unknown: 'unknown', creating: 'creating' }[task.status] || ''),
    );
    item.append(
      el('div', task.title, 'todo-name'),
      el('div', message.sender + ' · ' + task.evidence, 'evidence'),
    );
    if (task.status === 'candidate') {
      const actions = el('div', undefined, 'todo-action');
      actions.append(button('添加', () => openTodo({ id: task.id, title: task.title }), 'primary'));
      item.append(actions);
    } else item.append(el('div', todoStatuses[task.status] || task.status, 'todo-state'));
    root.append(item);
  }
}
function messageCard(message) {
  const draft = message.draft;
  const needs =
    !message.handled &&
    draft &&
    ((draft.status === 'pending' && draft.text.trim()) || draft.status === 'unknown');
  const card = el(
      'article',
      undefined,
      (seen.has(message.key) ? '' : 'enter') + (needs ? ' needs' : ''),
    ),
    meta = el('div', undefined, 'meta'),
    who = el('div', undefined, 'who');
  seen.add(message.key);
  who.append(el('span', message.sender, 'name'));
  for (const source of message.sources)
    who.append(
      el(
        'span',
        { direct: '单聊', at: '@我', ued: snapshot.status.group_name || '关注群' }[source] ||
          source,
        'tag',
      ),
    );
  if (message.ingest_mode === 'history_backfill') who.append(el('span', '历史补读', 'tag'));
  const time = el(
    'time',
    String(message.time || '时间未提供').replace(
      /^\d{4}-(\d{2}-\d{2}) (\d{2}:\d{2}):\d{2}.*$/,
      '$1 $2',
    ),
    'time',
  );
  time.title = String(message.time || '');
  meta.append(time, who);
  card.append(meta, el('div', clean(message.text), 'original'));
  const codex = el('div', undefined, 'codex');
  if (draft) codex.append(el('div', draft.summary || '手动回复', 'summary'));
  if (message.handled) {
    if (draft && draft.status === 'delivered') codex.append(el('div', draft.text, 'sent'));
    else codex.append(el('div', '已处理', 'label'));
    card.append(codex);
    return card;
  }
  card.append(codex);
  const actions = el('div', undefined, 'actions');
  if (draft) {
    if (draft.status !== 'pending')
      codex.append(
        el('div', statuses[draft.status] || draft.status, 'label state-' + draft.status),
      );
    if (draft.status === 'pending') {
      const field = el('textarea');
      field.setAttribute('aria-label', '给' + message.sender + '的回复草稿');
      field.value = edits.has(draft.id) ? edits.get(draft.id) : draft.text;
      field.placeholder = draft.text.trim() ? '写下你希望回复的内容' : '暂无回复建议，可手动填写';
      const send = button(
        '确认发送',
        async () => {
          if (!confirm('确认将当前草稿发送给「' + message.sender + '」所在的原会话吗？')) return;
          await action('send', { id: draft.id, version: draft.version, text: field.value });
          edits.delete(draft.id);
        },
        'primary',
      );
      send.disabled = !field.value.trim();
      field.oninput = () => {
        edits.set(draft.id, field.value);
        send.disabled = !field.value.trim();
      };
      codex.append(field);
      actions.append(send);
    }
    if (draft.status !== 'sending')
      actions.append(button('重新生成', () => action('regenerate', { key: message.key })));
    if (draft.receipt)
      codex.append(el('div', draft.receipt.error || '请在钉钉核对投递结果。', 'receipt'));
  } else {
    codex.append(el('div', '等待整理', 'label state-none'));
    actions.append(
      button('手动回复', () => action('manual', { key: message.key })),
      button('生成草稿', () => action('regenerate', { key: message.key }), 'primary'),
    );
  }
  if (!draft || draft.status !== 'sending')
    actions.append(
      button('添加待办', () => manualTodo(message)),
      button('标记已处理', () => action('handled', { key: message.key })),
    );
  codex.append(actions);
  return card;
}
function render() {
  if (!snapshot) return;
  const status = snapshot.status,
    active = snapshot.messages.filter((message) => !message.handled);
  const degraded = Object.values(status.listeners || {}).some(
    (item) =>
      ['degraded', 'error', 'disconnected'].includes(item.transport?.state) ||
      item.state === 'stopped',
  );
  const expected = status.listener_count || 3;
  document.querySelector('[data-filter=ued]').textContent = status.group_name || '关注群';
  const ready = Object.values(status.listeners || {}).filter(
    (item) => item.state === 'ready',
  ).length;
  document.getElementById('status').dataset.state =
    degraded || status.summary_state === 'paused_after_error'
      ? 'degraded'
      : ready === expected
        ? 'live'
        : 'starting';
  document.getElementById('listening').textContent = degraded
    ? '连接异常'
    : ready === expected
      ? ready + ' 路'
      : ready + '/' + expected + ' 已订阅';
  const counts = Object.fromEntries(
    Object.entries(status.listeners || {}).map(([key, value]) => [key, value.accepted || 0]),
  );
  const note = document.getElementById('listener-note');
  note.replaceChildren(
    ...[
      ['单聊', counts.direct],
      ['@我', counts.at],
      [status.group_name || '关注群', counts.ued],
    ].map(([key, value]) => {
      const row = el('div');
      row.append(el('dt', key), el('dd', (value || 0) + ' 条'));
      return row;
    }),
  );
  note.title = '历史补读不计入实时接收；订阅就绪不代表已验证群事件投递。';
  document.getElementById('agent').textContent =
    status.summary_state === 'generating'
      ? '生成中'
      : status.summary_state === 'paused_after_error'
        ? '需处理'
        : status.appserver === 'ready'
          ? '已连接'
          : '连接中';
  const pendingCount = active.filter(
    (message) => message.draft && message.draft.status === 'pending' && message.draft.text.trim(),
  ).length;
  document.getElementById('pending').textContent = pendingCount;
  document.getElementById('pending').parentElement.dataset.n = pendingCount;
  error(
    status.error ||
      (degraded ? '消息连接已降级，请检查监听日志；订阅就绪不代表正在正常接收。' : null),
  );
  document.title = '钉钉消息助手 · ' + active.length + ' 条待处理';
  const overview = snapshot.overview || {};
  document.getElementById('overview-text').textContent = overview.text || '尚未生成整体总结';
  document.getElementById('overview-note').textContent =
    overview.error ||
    (overview.updated
      ? '基于最近 ' +
        overview.count +
        ' 条 · ' +
        new Date(overview.updated * 1000).toLocaleString('zh-CN', {
          timeZone: 'Asia/Shanghai',
          hour12: false,
        })
      : '');
  renderTodos(active);
  const root = document.getElementById('messages');
  root.replaceChildren();
  const visible = snapshot.messages.filter(
    (message) => filter === 'all' || message.sources.includes(filter),
  );
  const pending = visible.filter((message) => !message.handled);
  for (const message of pending) root.append(messageCard(message));
  if (!pending.length) root.append(el('div', '没有待处理消息', 'empty'));
  const handled = visible.filter((message) => message.handled);
  if (handled.length) {
    const section = el('details', undefined, 'archive');
    section.open = showHandled;
    section.ontoggle = () => {
      showHandled = section.open;
    };
    const summary = el('summary', '已处理');
    summary.append(el('span', String(handled.length), 'count'));
    section.append(summary);
    for (const message of handled) section.append(messageCard(message));
    root.append(section);
  }
  updated(status);
}
function updated(status) {
  const node = document.getElementById('updated');
  node.textContent = String(status.updated || '刚刚').replace(
    /^\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2}).*$/,
    '$1',
  );
  node.title = (status.updated || '') + ' · 最近 100 条';
}
async function refresh() {
  if (loading) return;
  loading = true;
  try {
    const response = await fetch('/api/state', { cache: 'no-store' });
    if (!response.ok) throw Error('无法连接消息服务');
    const next = await response.json();
    const signature = JSON.stringify([
      next.messages,
      next.overview,
      next.status.summary_state,
      next.status.error,
      next.status.appserver,
      next.status.listeners,
    ]);
    snapshot = next;
    if (signature !== last) {
      const active = document.activeElement;
      if (active && active.tagName === 'TEXTAREA') return;
      last = signature;
      render();
    } else updated(next.status);
  } catch (e) {
    error(e.message);
  } finally {
    loading = false;
  }
}
document.getElementById('tabs').onclick = (event) => {
  if (!event.target.dataset.filter) return;
  filter = event.target.dataset.filter;
  document.querySelectorAll('nav button').forEach((node) => {
    node.classList.toggle('active', node.dataset.filter === filter);
    node.setAttribute('aria-pressed', String(node.dataset.filter === filter));
  });
  render();
};
document.getElementById('refresh-overview').onclick = async (event) => {
  const node = event.currentTarget;
  node.setAttribute('aria-busy', 'true');
  try {
    await action('overview', {});
  } catch (e) {
    error(e.message);
  } finally {
    node.removeAttribute('aria-busy');
  }
};
document.getElementById('todo-cancel').onclick = () => {
  pendingTodo = null;
  document.getElementById('todo-dialog').close();
};
document.getElementById('todo-form').onsubmit = async (event) => {
  event.preventDefault();
  const target = pendingTodo,
    title = document.getElementById('todo-title').value.trim();
  if (!target || !title) return;
  const submit = document.getElementById('todo-submit');
  submit.disabled = true;
  submit.setAttribute('aria-busy', 'true');
  try {
    await action(target.id ? 'todo' : 'todo-manual', Object.assign({}, target, { title }));
    pendingTodo = null;
    document.getElementById('todo-dialog').close();
  } catch (e) {
    document.getElementById('todo-dialog-error').textContent = e.message;
  } finally {
    submit.disabled = false;
    submit.removeAttribute('aria-busy');
  }
};
// Theme: the head script applies the saved choice before first paint; this wires the swatches.
const THEME_KEY = 'dte-panel-theme';
function markTheme(name) {
  for (const pick of document.querySelectorAll('[data-theme-pick]'))
    pick.setAttribute('aria-pressed', String(pick.dataset.themePick === name));
}
function setTheme(name) {
  const root = document.documentElement;
  if (root.dataset.theme === name) return;
  if (!matchMedia('(prefers-reduced-motion: reduce)').matches) {
    root.classList.add('theme-shift');
    clearTimeout(setTheme.timer);
    setTheme.timer = setTimeout(() => root.classList.remove('theme-shift'), 400);
  }
  root.dataset.theme = name;
  markTheme(name);
  try {
    localStorage.setItem(THEME_KEY, name);
  } catch {
    // Storage blocked: the theme still applies for this visit.
  }
}
for (const pick of document.querySelectorAll('[data-theme-pick]'))
  pick.onclick = () => setTheme(pick.dataset.themePick);
markTheme(document.documentElement.dataset.theme || 'kargul');
refresh();
setInterval(refresh, 1500);
