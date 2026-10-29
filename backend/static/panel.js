const token = '__TOKEN__';
let filter = 'all',
  snapshot = null,
  showHandled = false,
  loading = false,
  last = '';
const edits = new Map();
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
    try {
      await fn();
    } catch (e) {
      error(e.message);
    } finally {
      node.disabled = false;
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
    root.append(el('p', '暂未发现明确的待办。也可以从消息卡片手动添加。', 'small'));
    return;
  }
  for (const { task, message } of todos) {
    const item = el('div', undefined, 'todo-item');
    item.append(
      el('div', task.title, 'todo-name'),
      el('div', '来自 ' + message.sender + '：' + task.evidence, 'evidence'),
    );
    if (task.status === 'candidate') {
      const actions = el('div', undefined, 'todo-action');
      actions.append(
        button('添加待办', () => openTodo({ id: task.id, title: task.title }), 'primary'),
      );
      item.append(actions);
    } else item.append(el('div', todoStatuses[task.status] || task.status, 'todo-state'));
    root.append(item);
  }
}
function messageCard(message) {
  const card = el('article'),
    meta = el('div', undefined, 'meta');
  meta.append(el('span', message.sender, 'name'));
  for (const source of message.sources)
    meta.append(
      el(
        'span',
        { direct: '单聊', at: '@我', ued: snapshot.status.group_name || '关注群' }[source] ||
          source,
        'tag',
      ),
    );
  if (message.ingest_mode === 'history_backfill') meta.append(el('span', '历史补读', 'tag'));
  meta.append(el('span', String(message.time || '时间未提供'), 'time'));
  card.append(meta, el('div', clean(message.text), 'original'));
  const draft = message.draft;
  if (draft)
    card.append(el('span', '消息整理', 'label'), el('div', draft.summary || '手动回复', 'summary'));
  if (message.handled) {
    if (draft && draft.status === 'delivered')
      card.append(el('span', '已发送 · 投递成功', 'label'), el('div', draft.text, 'sent'));
    else card.append(el('div', '已处理', 'receipt'));
    return card;
  }
  const actions = el('div', undefined, 'actions');
  if (draft) {
    card.append(
      el(
        'div',
        draft.status === 'pending' && !draft.text.trim()
          ? '暂无回复建议，可手动填写'
          : statuses[draft.status] || draft.status,
        'label',
      ),
    );
    if (draft.status === 'pending') {
      const field = el('textarea');
      field.setAttribute('aria-label', '给' + message.sender + '的回复草稿');
      field.value = edits.has(draft.id) ? edits.get(draft.id) : draft.text;
      field.placeholder = '写下你希望回复的内容';
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
      card.append(field);
      actions.append(send);
    }
    if (draft.status !== 'sending')
      actions.append(button('重新生成', () => action('regenerate', { key: message.key })));
    if (draft.receipt)
      card.append(el('div', draft.receipt.error || '请在钉钉核对投递结果。', 'receipt'));
  } else {
    card.append(el('div', '消息已收到，等待 AI 整理。图片和附件不会自动识别。', 'label'));
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
  card.append(actions);
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
  document.getElementById('listening').textContent = degraded
    ? '连接异常'
    : ready === expected
      ? '已订阅 ' + ready + ' 路'
      : ready + '/' + expected + ' 已订阅';
  const counts = Object.fromEntries(
    Object.entries(status.listeners || {}).map(([key, value]) => [key, value.accepted || 0]),
  );
  document.getElementById('listener-note').textContent =
    '本次进程实际接收：单聊 ' +
    (counts.direct || 0) +
    ' 条，@我 ' +
    (counts.at || 0) +
    ' 条，' +
    (status.group_name || '关注群') +
    ' ' +
    (counts.ued || 0) +
    ' 条。历史补读不计入实时接收；订阅就绪不代表已验证群事件投递。';
  document.getElementById('agent').textContent =
    status.summary_state === 'generating'
      ? '生成中'
      : status.summary_state === 'paused_after_error'
        ? '需处理'
        : status.appserver === 'ready'
          ? '已连接'
          : '连接中';
  document.getElementById('pending').textContent = active.filter(
    (message) => message.draft && message.draft.status === 'pending' && message.draft.text.trim(),
  ).length;
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
        ' 条未处理消息 · ' +
        new Date(overview.updated * 1000).toLocaleString('zh-CN', {
          timeZone: 'Asia/Shanghai',
          hour12: false,
        })
      : '消息整理后自动更新，也可以点击重新总结。');
  renderTodos(active);
  const root = document.getElementById('messages');
  root.replaceChildren();
  const visible = snapshot.messages.filter(
    (message) => filter === 'all' || message.sources.includes(filter),
  );
  const pending = visible.filter((message) => !message.handled);
  for (const message of pending) root.append(messageCard(message));
  if (!pending.length)
    root.append(el('div', '当前没有未处理消息。新消息到达后会自动出现。', 'empty'));
  const handled = visible.filter((message) => message.handled);
  if (handled.length) {
    const section = el('details', undefined, 'archive');
    section.open = showHandled;
    section.ontoggle = () => {
      showHandled = section.open;
    };
    section.append(el('summary', '已处理（' + handled.length + '） · 已投递消息已收起'));
    for (const message of handled) section.append(messageCard(message));
    root.append(section);
  }
  document.getElementById('updated').textContent =
    '更新于 ' +
    (status.updated || '刚刚') +
    ' · 展示最近 100 条消息；回复和待办仅在你确认后写入钉钉。';
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
    } else
      document.getElementById('updated').textContent =
        '更新于 ' + (next.status.updated || '刚刚') + ' · 展示最近 100 条消息；仅确认后写入钉钉。';
  } catch (e) {
    error(e.message);
  } finally {
    loading = false;
  }
}
document.getElementById('tabs').onclick = (event) => {
  if (!event.target.dataset.filter) return;
  filter = event.target.dataset.filter;
  document
    .querySelectorAll('nav button')
    .forEach((node) => node.classList.toggle('active', node.dataset.filter === filter));
  render();
};
document.getElementById('refresh-overview').onclick = async () => {
  try {
    await action('overview', {});
  } catch (e) {
    error(e.message);
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
  try {
    await action(target.id ? 'todo' : 'todo-manual', Object.assign({}, target, { title }));
    pendingTodo = null;
    document.getElementById('todo-dialog').close();
  } catch (e) {
    document.getElementById('todo-dialog-error').textContent = e.message;
  } finally {
    submit.disabled = false;
  }
};
function hookForm() {
  return {
    enabled: document.getElementById('hooks-enabled').checked,
    url: document.getElementById('hooks-url').value.trim(),
    secret: document.getElementById('hooks-secret').value,
    filter: {
      sources: [...document.querySelectorAll('[name=hooks-source]:checked')].map(
        (node) => node.value,
      ),
      needs_reply_only: document.getElementById('hooks-needs-reply').checked,
    },
  };
}
function hookResult(result) {
  const box = document.getElementById('hooks-result');
  box.className = 'small';
  if (!result) {
    box.textContent = '还没有投递记录。';
    return;
  }
  box.classList.add(result.ok ? 'hook-ok' : 'hook-failed');
  box.textContent =
    (result.event === 'hook.test' ? '测试投递' : '最近投递') +
    (result.ok ? '成功' : '失败') +
    ' · ' +
    (result.status ? 'HTTP ' + result.status : result.error || '无响应') +
    ' · 尝试 ' +
    result.attempts +
    ' 次 · ' +
    result.time;
}
function fillHooks(value) {
  document.getElementById('hooks-enabled').checked = value.enabled;
  document.getElementById('hooks-url').value = value.url;
  const secret = document.getElementById('hooks-secret');
  secret.value = '';
  secret.placeholder = value.secret_set ? '已设置，留空表示不修改' : '至少 16 个字符';
  for (const node of document.querySelectorAll('[name=hooks-source]'))
    node.checked = value.filter.sources.includes(node.value);
  document.getElementById('hooks-needs-reply').checked = value.filter.needs_reply_only;
  document.getElementById('hooks-group-name').textContent =
    (snapshot && snapshot.status.group_name) || '关注群';
  hookResult(value.last);
}
async function hookRequest(path, data) {
  const response = await fetch('/api/' + path, {
    method: data ? 'POST' : 'GET',
    headers: data ? { 'Content-Type': 'application/json', 'X-Panel-Token': token } : {},
    body: data ? JSON.stringify(data) : undefined,
    cache: 'no-store',
  });
  const value = await response.json();
  if (!response.ok) throw Error(value.error || '操作未完成');
  return value;
}
async function hookAction(path, data) {
  const errorBox = document.getElementById('hooks-error');
  errorBox.textContent = '';
  try {
    return await hookRequest(path, data);
  } catch (e) {
    errorBox.textContent = e.message;
    return null;
  }
}
document.getElementById('hooks-open').onclick = async () => {
  const value = await hookAction('hooks');
  if (value) fillHooks(value);
  document.getElementById('hooks-dialog').showModal();
};
document.getElementById('hooks-cancel').onclick = () =>
  document.getElementById('hooks-dialog').close();
document.getElementById('hooks-test').onclick = async (event) => {
  event.target.disabled = true;
  document.getElementById('hooks-result').textContent = '正在发送测试消息…';
  const result = await hookAction('hooks/test', hookForm());
  if (result) hookResult(result);
  else document.getElementById('hooks-result').textContent = '';
  event.target.disabled = false;
};
document.getElementById('hooks-form').onsubmit = async (event) => {
  event.preventDefault();
  const value = await hookAction('hooks', hookForm());
  if (value) {
    fillHooks(value);
    document.getElementById('hooks-result').textContent = '已保存。';
  }
};
refresh();
setInterval(refresh, 1500);
