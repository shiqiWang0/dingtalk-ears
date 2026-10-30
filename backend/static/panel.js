const token = '__TOKEN__';
let filter = 'all',
  snapshot = null,
  showHandled = false,
  loading = false,
  last = '';
const edits = new Map();
const generatingMessages = new Map();
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
// 将网络底层异常转换为更准确的中文提示并保留原始报错
function humanizeError(text) {
  if (!text) return '';
  const str = String(text);
  // 判断是否为服务停止或网络中断导致的请求失败
  if (
    str.includes('Failed to fetch') ||
    str.includes('Load failed') ||
    str.includes('NetworkError')
  ) {
    return '无法连接本地服务，后台进程可能已停止（' + str + '）';
  }
  return str;
}
function error(text) {
  const box = document.getElementById('error');
  box.hidden = !text;
  box.textContent = humanizeError(text);
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
      // 避免覆写外部正在 loading 中的禁用状态
      if (!node.classList.contains('loading')) {
        node.disabled = false;
      }
    }
  };
  return node;
}
// 触发重新生成草稿，并记录消息 key 用于 loading 与禁用交互
async function regenerateMessage(message) {
  if (generatingMessages.has(message.key)) return;
  // 记录重新生成发起时的旧草稿 id 和时间戳
  generatingMessages.set(message.key, {
    draftId: message.draft ? message.draft.id : null,
    startTime: Date.now(),
  });
  if (message.draft) {
    edits.delete(message.draft.id);
  }
  render();
  try {
    await action('regenerate', { key: message.key });
  } catch (e) {
    generatingMessages.delete(message.key);
    render();
    throw e;
  }
}
// 检查正在重新生成的消息是否已生成新草稿或异常结束
function checkGeneratingStatus(next) {
  if (!generatingMessages.size) return;
  const now = Date.now();
  // 检查是否发生全局 AI 服务错误或异常退出
  const isGlobalError =
    next.status.summary_state === 'paused_after_error' || Boolean(next.status.error);

  for (const [key, info] of Array.from(generatingMessages.entries())) {
    // 遇到全局错误或超过两分钟超时时解除生成中状态
    if (isGlobalError || now - info.startTime > 120000) {
      generatingMessages.delete(key);
      continue;
    }
    const msg = next.messages.find((m) => m.key === key);
    // 消息已被处理或不存在时清理状态
    if (!msg || msg.handled) {
      generatingMessages.delete(key);
      continue;
    }
    // 原先无草稿且现在已生成草稿
    if (!info.draftId && msg.draft) {
      generatingMessages.delete(key);
      continue;
    }
    // 原先有草稿且新草稿 id 已更新，代表重新生成已完成
    if (info.draftId && msg.draft && msg.draft.id !== info.draftId) {
      generatingMessages.delete(key);
      continue;
    }
  }
}
function clean(text) {
  return text
    .replace(/\[图片消息\]\(mediaId=[^)]*\)/g, '[图片消息，暂未识别]')
    .replace('注意：如需下载使用dws chat message download-media命令下载', '');
}
function isGroupSource(source) {
  return source === 'ued' || source.startsWith('group-');
}
function groupNames(status) {
  return status.group_names || { ued: status.group_name || '关注群' };
}
function sourceName(source) {
  return { direct: '单聊', at: '@我' }[source] || groupNames(snapshot.status)[source] || source;
}
function renderGroups(groups) {
  const root = document.getElementById('group-list');
  root.replaceChildren();
  for (const group of groups || []) {
    const item = el('div', undefined, 'group-item');
    item.append(el('div', group.name, 'group-name'), el('div', group.id, 'group-id'));
    item.append(button('删除', async () => {
      if (confirm('停止关注「' + group.name + '」？重启服务后生效。')) await action('groups/remove', { id: group.id });
    }));
    root.append(item);
  }
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
  // 排除已忽略的待办项
  const todos = active.flatMap((message) =>
    (message.todos || []).filter((task) => task.status !== 'ignored').map((task) => ({ task, message })),
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
        button('忽略', () => action('todo-ignore', { id: task.id })),
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
        sourceName(source),
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
  const isGenerating = generatingMessages.has(message.key);
  const actions = el('div', undefined, 'actions');
  if (draft) {
    const labelText = isGenerating
      ? '正在重新生成回复建议...'
      : draft.status === 'pending' && !draft.text.trim()
        ? '暂无回复建议，可手动填写'
        : statuses[draft.status] || draft.status;
    card.append(el('div', labelText, 'label'));
    if (draft.status === 'pending') {
      const field = el('textarea');
      field.setAttribute('aria-label', '给' + message.sender + '的回复草稿');
      field.value = edits.has(draft.id) ? edits.get(draft.id) : draft.text;
      field.placeholder = isGenerating ? '正在重新生成草稿，请稍候...' : '写下你希望回复的内容';
      // 正在重新生成时禁用输入框
      if (isGenerating) {
        field.disabled = true;
      }
      const send = button(
        '确认发送',
        async () => {
          if (!confirm('确认将当前草稿发送给「' + message.sender + '」所在的原会话吗？')) return;
          await action('send', { id: draft.id, version: draft.version, text: field.value });
          edits.delete(draft.id);
        },
        'primary',
      );
      send.disabled = isGenerating || !field.value.trim();
      field.oninput = () => {
        edits.set(draft.id, field.value);
        send.disabled = isGenerating || !field.value.trim();
      };
      card.append(field);
      actions.append(send);
    }
    if (draft.status !== 'sending') {
      // 重新生成中展示 loading 状态并禁用按钮以防止多次点击
      if (isGenerating) {
        const regen = button('重新生成中...', () => {}, 'loading');
        regen.disabled = true;
        actions.append(regen);
      } else {
        actions.append(button('重新生成', () => regenerateMessage(message)));
      }
    }
    if (draft.receipt)
      card.append(el('div', draft.receipt.error || '请在钉钉核对投递结果。', 'receipt'));
  } else {
    card.append(
      el(
        'div',
        isGenerating
          ? '正在生成草稿，请稍候...'
          : '消息已收到，等待 AI 整理。图片和附件不会自动识别。',
        'label',
      ),
    );
    const manualBtn = button('手动回复', () => action('manual', { key: message.key }));
    const genBtn = isGenerating
      ? button('生成中...', () => {}, 'primary loading')
      : button('生成草稿', () => regenerateMessage(message), 'primary');
    // 正在生成草稿时禁用对应操作按钮
    if (isGenerating) {
      manualBtn.disabled = true;
      genBtn.disabled = true;
    }
    actions.append(manualBtn, genBtn);
  }
  if (!draft || draft.status !== 'sending') {
    const todoBtn = button('添加待办', () => manualTodo(message));
    const handledBtn = button('标记已处理', () => action('handled', { key: message.key }));
    // 正在重新生成时禁用待办与标记已处理按钮
    if (isGenerating) {
      todoBtn.disabled = true;
      handledBtn.disabled = true;
    }
    actions.append(todoBtn, handledBtn);
  }
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
  document.querySelector('[data-filter=groups]').textContent = '关注群';
  const ready = Object.values(status.listeners || {}).filter(
    (item) => item.state === 'ready',
  ).length;
  const listeningEl = document.getElementById('listening');
  // 连接异常时展示红色文字与红色呼吸灯，正常时展示绿色呼吸灯
  if (degraded) {
    listeningEl.textContent = '连接异常';
    listeningEl.className = 'status-danger';
  } else {
    listeningEl.textContent =
      ready === expected
        ? '已订阅 ' + ready + ' 路'
        : ready + '/' + expected + ' 已订阅';
    listeningEl.className = 'status-normal';
  }
  const counts = Object.fromEntries(
    Object.entries(status.listeners || {}).map(([key, value]) => [key, value.accepted || 0]),
  );
  document.getElementById('listener-note').textContent =
    '本次进程实际接收：单聊 ' +
    (counts.direct || 0) +
    ' 条，@我 ' +
    (counts.at || 0) +
    ' 条，' +
    Object.entries(groupNames(status)).map(([source, name]) => name + ' ' + (counts[source] || 0) + ' 条').join('，') +
    ' 条。历史补读不计入实时接收；订阅就绪不代表已验证群事件投递。';
  const agentLabel = document.getElementById('agent-label');
  if (agentLabel) {
    agentLabel.textContent = status.ai_provider || 'AI 助手';
  }
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
  renderGroups(snapshot.groups);
  const root = document.getElementById('messages');
  root.replaceChildren();
  const visible = snapshot.messages.filter(
    (message) => filter === 'all' || message.sources.includes(filter) || (filter === 'groups' && message.sources.some(isGroupSource)),
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
      next.groups,
    ]);
    const prevCount = generatingMessages.size;
    checkGeneratingStatus(next);
    const generatingChanged = prevCount !== generatingMessages.size;
    snapshot = next;
    // 数据签名变动或重新生成状态完成时触发视图重绘
    if (signature !== last || generatingChanged) {
      const active = document.activeElement;
      if (active && active.tagName === 'TEXTAREA') return;
      last = signature;
      render();
    } else
      document.getElementById('updated').textContent =
        '更新于 ' + (next.status.updated || '刚刚') + ' · 展示最近 100 条消息；仅确认后写入钉钉。';
  } catch (e) {
    error(e.message);
    const listeningEl = document.getElementById('listening');
    // 服务连接失败时同步将监听状态置为异常
    if (listeningEl) {
      listeningEl.textContent = '连接异常';
      listeningEl.className = 'status-danger';
    }
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
  const btn = document.getElementById('refresh-overview');
  btn.disabled = true;
  try {
    await action('overview', {});
  } catch (e) {
    error(e.message);
  } finally {
    btn.disabled = false;
  }
};
document.getElementById('todo-cancel').onclick = () => {
  pendingTodo = null;
  document.getElementById('todo-dialog').close();
};
document.getElementById('add-group').onclick = () => {
  document.getElementById('group-form').reset();
  selectedGroups.clear();
  renderSelectedGroups();
  closeGroupOptions();
  document.getElementById('group-dialog-error').textContent = '';
  document.getElementById('group-dialog').showModal();
  loadAvailableGroups();
};
document.getElementById('group-cancel').onclick = () => document.getElementById('group-dialog').close();
document.getElementById('group-form').onsubmit = async (event) => {
  event.preventDefault();
  try {
    if (!selectedGroups.size) throw Error('请至少选择一个群');
    for (const group of selectedGroups.values()) {
      await action('groups', { id: group.id, name: group.name });
    }
    document.getElementById('group-dialog').close();
  } catch (e) {
    document.getElementById('group-dialog-error').textContent = e.message;
  }
};
let availableGroups = [];
const selectedGroups = new Map();

function closeGroupOptions() {
  const input = document.getElementById('group-search');
  document.getElementById('group-options').hidden = true;
  input.setAttribute('aria-expanded', 'false');
}

function renderSelectedGroups() {
  const selection = document.getElementById('group-selection');
  selection.replaceChildren();
  for (const group of selectedGroups.values()) {
    const tag = el('span', undefined, 'group-selection-tag');
    const remove = el('button', '×', 'group-selection-remove');
    remove.type = 'button';
    remove.setAttribute('aria-label', '移除 ' + group.name);
    remove.onclick = (event) => {
      event.stopPropagation();
      selectedGroups.delete(group.id);
      renderSelectedGroups();
      renderAvailableGroups(document.getElementById('group-search').value);
    };
    tag.append(document.createTextNode(group.name), remove);
    selection.append(tag);
  }
}

function toggleGroup(group) {
  if (selectedGroups.has(group.id)) selectedGroups.delete(group.id);
  else selectedGroups.set(group.id, group);
  renderSelectedGroups();
  document.getElementById('group-dialog-error').textContent = '';
  renderAvailableGroups(document.getElementById('group-search').value);
}

function renderAvailableGroups(query = '') {
  const options = document.getElementById('group-options');
  const input = document.getElementById('group-search');
  const normalizedQuery = query.trim().toLowerCase();
  const groups = availableGroups.filter((item) => item.name.toLowerCase().includes(normalizedQuery));
  options.replaceChildren();
  for (const group of groups) {
    const option = el('li', group.name, 'group-option');
    option.setAttribute('role', 'option');
    option.setAttribute('aria-selected', String(selectedGroups.has(group.id)));
    option.onclick = (event) => {
      event.stopPropagation();
      toggleGroup(group);
    };
    options.append(option);
  }
  if (!groups.length) options.append(el('li', '没有匹配的群', 'group-option-empty'));
  options.hidden = false;
  input.setAttribute('aria-expanded', 'true');
}
async function loadAvailableGroups() {
  try {
    const response = await fetch('/api/groups/available', { cache: 'no-store' });
    const value = await response.json();
    if (!response.ok) throw Error(value.error || '无法读取群列表');
    availableGroups = value.groups || [];
    renderAvailableGroups(document.getElementById('group-search').value);
  } catch (e) { document.getElementById('group-dialog-error').textContent = e.message; }
}
document.getElementById('group-search').oninput = (event) => {
  renderAvailableGroups(event.target.value);
};
document.getElementById('group-search').onfocus = (event) => renderAvailableGroups(event.target.value);
document.getElementById('group-search').onclick = (event) => renderAvailableGroups(event.target.value);
document.getElementById('group-search').onkeydown = (event) => {
  if (event.key === 'Escape') closeGroupOptions();
};
document.addEventListener('click', (event) => {
  if (!event.target.closest('.group-combobox')) closeGroupOptions();
});
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
refresh();
setInterval(refresh, 1500);
