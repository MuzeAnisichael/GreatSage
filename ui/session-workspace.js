// Explicit session switching and session-scoped task navigation.
export function setupSessionWorkspace({ client, busy, toast, beforeActivate, onActivated, onOpenTasks, onContinueWork }) {
  const $ = (selector, parent = document) => parent.querySelector(selector);
  const node = (tag, className, text) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  };
  const when = value => value ? new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(new Date(value)) : '暂无活动';

  const dialog = node('dialog', 'session-dialog');
  dialog.innerHTML = `<div class="session-dialog-head"><div><h2>会话工作台</h2><p>继续过去的会话，或查看它关联的任务与产物。切换不会自动重启未完成任务。</p></div><button type="button" class="button ghost small session-close">关闭</button></div>
    <div class="session-dialog-summary"></div><div class="session-list"><div class="small-empty">正在读取会话…</div></div>`;
  document.body.append(dialog);

  let activeId = '';
  let requestGeneration = 0;
  let openIntent = false;
  let activating = false;

  function setActive(id) {
    activeId = id || '';
    const button = $('#session-label');
    button.textContent = activeId ? `会话 ${String(activeId).slice(0, 8)}` : '本地会话';
    button.title = activeId ? `当前会话 ${activeId}；点击切换` : '点击浏览会话';
    button.setAttribute('aria-label', button.title);
  }

  function invalidate() {
    openIntent = false;
    requestGeneration += 1;
    $('.session-list', dialog).replaceChildren();
  }

  function close() {
    invalidate();
    if (dialog.open) dialog.close();
  }

  function render(sessions) {
    const list = $('.session-list', dialog);
    list.replaceChildren();
    const ordered = [...sessions].sort((left, right) => new Date(
      right.updated_at || right.last_message_at || right.created_at || 0) - new Date(
      left.updated_at || left.last_message_at || left.created_at || 0));
    const current = ordered.find(session => session.active || session.id === activeId);
    $('.session-dialog-summary', dialog).textContent = current
      ? `当前：${current.title || `会话 ${String(current.id).slice(0, 8)}`} · ${current.message_count || 0} 条原文`
      : '当前会话尚未出现在历史列表中。';
    if (!ordered.length) {
      list.append(node('div', 'small-empty', '还没有可继续的历史会话。新建会话后会显示在这里。'));
      return;
    }
    for (const session of ordered) {
      const active = Boolean(session.active || session.id === activeId);
      const card = node('article', `session-card${active ? ' active' : ''}`);
      const header = node('div', 'session-card-head');
      const title = node('div', 'session-card-title');
      title.append(node('strong', '', session.title || `会话 ${String(session.id).slice(0, 8)}`),
        node('small', '', `${when(session.updated_at || session.last_message_at || session.created_at)} · ${session.message_count || 0} 条原文`));
      header.append(title, node('span', `badge ${active ? 'ok' : ''}`, active ? '当前会话' : '历史会话'));
      const actions = node('div', 'session-card-actions');
      for (const brief of active ? [true] : [false, true]) {
        const activate = node('button', brief ? 'button ghost small' : 'button primary small', brief ? '接续工作' : '继续此会话');
        activate.type = 'button';
        activate.addEventListener('click', () => busy(activate, async () => {
          if (activating) return;
          const generation = requestGeneration;
          activating = true;
          for (const button of dialog.querySelectorAll('button')) button.disabled = true;
          try {
            if (!active) {
              beforeActivate?.();
              const result = await client.request(`/api/sessions/${encodeURIComponent(session.id)}/activate`, { method: 'POST' });
              if (!openIntent || generation !== requestGeneration) return;
              await onActivated?.(result, session.id);
              setActive(result?.status?.session_id || result?.session_id || session.id);
            }
            close();
            if (brief) await onContinueWork();
            else toast('已继续所选会话；未完成任务不会自动恢复。');
          } finally {
            activating = false;
            for (const button of dialog.querySelectorAll('button')) button.disabled = false;
          }
        }));
        actions.append(activate);
      }
      const tasks = node('button', 'button ghost small', '查看关联任务');
      tasks.type = 'button';
      tasks.addEventListener('click', async () => { close(); await onOpenTasks?.(session.id); });
      actions.append(tasks);
      card.append(header, actions);
      list.append(card);
    }
  }

  async function open() {
    openIntent = true;
    const generation = ++requestGeneration;
    $('.session-list', dialog).replaceChildren(node('div', 'small-empty', '正在读取会话…'));
    if (!dialog.open) dialog.showModal();
    try {
      const result = await client.request('/api/sessions');
      if (!openIntent || generation !== requestGeneration) return;
      render(Array.isArray(result) ? result : result.sessions || []);
    } catch (error) {
      if (!openIntent || generation !== requestGeneration) return;
      $('.session-list', dialog).replaceChildren(node('div', 'small-empty', error.message || '会话加载失败。'));
    }
  }

  $('#session-label').addEventListener('click', open);
  $('.session-close', dialog).addEventListener('click', close);
  dialog.addEventListener('cancel', event => {
    if (activating) event.preventDefault();
    else invalidate();
  });
  dialog.addEventListener('close', invalidate);

  return { open, close, setActive };
}
