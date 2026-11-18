import { briefColumns, setupContinuation } from './continuation.js';
import { setupCorrectionVoice } from './correction-voice.js';

export function setupWork({ client, toast, busy, confirmAction, showTasks }) {
  const $ = (selector, parent = document) => parent.querySelector(selector);
  const node = (tag, className, text) => { const el = document.createElement(tag); if (className) el.className = className; if (text !== undefined) el.textContent = text; return el; };
  const action = (label, className, handler) => { const el = node('button', className, label); el.type = 'button'; if (handler) el.addEventListener('click', () => busy(el, handler)); return el; };
  const work = { tasks: [], materials: [], approvals: [], tools: [], selected: null, artifact: null,
                 activeSessionId: '', sessionFilter: 'all' };
  let taskRefreshGeneration = 0;
  const statusLabels = { queued: '排队中', running: '进行中', started: '进行中', waiting_approval: '等待确认', succeeded: '已完成', completed: '已完成', failed: '失败', cancelled: '已取消', interrupted: '已中断', pending: '未开始', unknown: '结果未知', expired: '已失效', denied: '已拒绝', approved: '已批准', proposed: '待决定', awaiting_approval: '等待确认', undone: '已撤销', active: '有效', stale: '来源已变更', missing: '原文件已移走', unreadable: '无法读取' };
  const tones = { succeeded: 'ok', completed: 'ok', active: 'ok', approved: 'ok', running: 'busy', started: 'busy', queued: 'busy', waiting_approval: 'warn', awaiting_approval: 'warn', interrupted: 'warn', unknown: 'warn', stale: 'warn', missing: 'warn', failed: 'bad', unreadable: 'bad' };
  const badge = status => node('span', `badge ${tones[status] || ''}`, statusLabels[status] || status);
  const when = value => value ? new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value)) : '';
  const kinds = { minutes: '纪要', todos: '待办', document: '文档草稿' };
  const origins = { user_text: '你的文字', user_voice: '你的语音', console: '控制台' };
  const taskKinds = { action: '操作', minutes: '纪要', research: '研究文档' };
  const taskKind = task => (task.profile || task.params?.profile) === 'continuation' ? '接续简报' : taskKinds[task.kind] || task.kind;
  const active = status => ['queued', 'running', 'waiting_approval'].includes(status);
  const toolTitle = name => work.tools.find(tool => tool.name === name)?.title || name;
  let firstBrief = null;
  function cancelBriefPreview() {
    if (firstBrief?.opening && !artifactDialog.open) closeArtifactDialog();
    firstBrief = null;
  }
  const continuation = setupContinuation({ client, toast, getSession: () => work.activeSessionId,
    onCreated(task, sessionId) {
      setSessionFilter(sessionId);
      work.selected = task.id;
      firstBrief = { id: task.id, sessionId, opening: false };
      showTasks();
    },
  });
  for (const button of document.querySelectorAll('[data-open-continuation]')) {
    button.addEventListener('click', () => busy(button, continuation.open));
  }

  const finite = value => typeof value === 'number' && Number.isFinite(value) ? value : 0;
  const compactNumber = value => new Intl.NumberFormat('zh-CN', { notation: value >= 10000 ? 'compact' : 'standard', maximumFractionDigits: 1 }).format(value);
  const integerNumber = value => new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 }).format(Number(value) || 0);
  function callUsage(calls = []) {
    const totals = { calls: calls.length, input: 0, output: 0, total: 0, cost: 0, hasTokens: false, hasCost: false };
    for (const call of calls) {
      const usage = call?.usage || {};
      const input = finite(usage.prompt_tokens ?? usage.input_tokens);
      const output = finite(usage.completion_tokens ?? usage.output_tokens);
      const total = finite(usage.total_tokens) || input + output;
      if (input || output || total) totals.hasTokens = true;
      totals.input += input; totals.output += output; totals.total += total;
      if (typeof usage.cost === 'number' && Number.isFinite(usage.cost)) {
        totals.hasCost = true;
        totals.cost += usage.cost;
      }
    }
    return totals;
  }
  const allCalls = task => (task.steps || []).flatMap(step => Array.isArray(step.detail?.calls) ? step.detail.calls : []);
  function usageText(calls) {
    const usage = callUsage(calls);
    const parts = [`模型调用 ${usage.calls} 次`];
    if (usage.hasTokens) parts.push(usage.input || usage.output ? `输入 ${compactNumber(usage.input)} · 输出 ${compactNumber(usage.output)} tokens` : `共 ${compactNumber(usage.total)} tokens`);
    if (usage.hasCost) parts.push(`实际费用 US$${usage.cost < .0001 ? usage.cost.toPrecision(2) : usage.cost.toFixed(4)}`);
    return parts.join(' · ');
  }
  function safeLines(value, depth = 0) {
    if (value == null || value === '') return [];
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return [String(value)];
    if (depth > 2) return [];
    if (Array.isArray(value)) return value.flatMap(item => safeLines(item, depth + 1)).slice(0, 30);
    if (typeof value === 'object') {
      const preferred = value.text ?? value.title ?? value.summary ?? value.description ?? value.question ?? value.issue ?? value.detail;
      if (preferred != null) return safeLines(preferred, depth + 1);
      return Object.entries(value).flatMap(([key, item]) => safeLines(item, depth + 1).map(text => `${key}：${text}`)).slice(0, 30);
    }
    return [];
  }
  function lineList(value, className = '') {
    const list = node('ul', className);
    for (const text of safeLines(value)) list.append(node('li', '', text));
    return list;
  }

  // ---- materials ----------------------------------------------------------
  async function refreshMaterials() {
    work.materials = await client.request('/api/materials');
    $('#material-count').textContent = work.materials.length;
    const list = $('#material-list');
    list.replaceChildren();
    if (!work.materials.length) list.append(node('div', 'small-empty', '还没有资料。导入 .md 文件或文件夹后，对话和纪要任务就能引用它们。'));
    for (const item of work.materials) {
      const row = node('article', 'material-row');
      const body = node('div', 'material-body');
      const title = node('div', 'material-title');
      title.append(node('strong', '', item.name), badge(item.status));
      body.append(title, node('small', '', item.path), node('small', '', `v${item.version} · ${item.chunk_count} 段 · ${(item.size / 1024).toFixed(1)} KB · 更新于 ${when(item.updated_at)}`));
      const actions = node('div', 'record-actions');
      actions.append(action('查看', 'delete-button', () => showMaterial(item.id)), action('删除', 'delete-button', async () => {
        if (!await confirmAction('删除这份资料？', '资料的分段和索引会被移除；引用它的对话回答会一并删除，引用它的产物会标记为“来源已变更”。电脑上的原文件不受影响。')) return;
        await client.request(`/api/materials/${encodeURIComponent(item.id)}`, { method: 'DELETE' });
        await refreshMaterials();
        toast('资料已删除。');
      }));
      row.append(body, actions);
      list.append(row);
    }
  }

  const materialDialog = node('dialog', 'record-dialog');
  materialDialog.innerHTML = '<div class="dialog-actions"><button type="button" class="button small ghost">关闭</button></div><h2></h2><p class="source-note"></p><div class="chunk-list"></div>';
  document.body.append(materialDialog);
  $('button', materialDialog).addEventListener('click', () => materialDialog.close());
  async function showMaterial(id) {
    const material = await client.request(`/api/materials/${encodeURIComponent(id)}`);
    $('h2', materialDialog).textContent = material.name;
    $('.source-note', materialDialog).textContent = `${material.path} · v${material.version} · ${material.chunks.length} 段`;
    $('.chunk-list', materialDialog).replaceChildren(...material.chunks.map(chunk => {
      const item = node('article', 'chunk');
      item.append(node('small', '', `${chunk.heading || '（开头）'} · 第 ${chunk.line_start}–${chunk.line_end} 行`), node('p', 'record-text', chunk.text));
      return item;
    }));
    materialDialog.showModal();
  }

  $('#material-form').addEventListener('submit', event => {
    event.preventDefault();
    busy($('button[type=submit]', event.target), async () => {
      const path = $('#material-path').value.trim();
      if (!path) return;
      const result = await client.request('/api/materials/import', { method: 'POST', body: { path } });
      $('#material-path').value = '';
      await refreshMaterials();
      const failed = result.errors.map(item => item.error).join('；');
      toast(failed ? `已导入 ${result.materials.length} 份资料；以下文件没有导入：${failed}` : `已导入 ${result.materials.length} 份资料。`, failed ? 'warning' : '', failed ? 10000 : 5000);
    });
  });
  for (const [selector, kind] of [['#choose-material-file', 'file'], ['#choose-material-folder', 'folder']]) {
    $(selector).addEventListener('click', () => busy($(selector), async () => {
      if (!window.greatsage?.chooseMaterialPath) throw new Error('浏览器模式下请直接填写完整路径。');
      const path = await window.greatsage.chooseMaterialPath(kind);
      if (path) $('#material-path').value = path;
    }));
  }
  $('#rescan-materials').addEventListener('click', () => busy($('#rescan-materials'), async () => {
    const result = await client.request('/api/materials/rescan', { method: 'POST' });
    await refreshMaterials();
    const updated = result.materials.filter(item => item.action === 'updated').length;
    toast(updated ? `${updated} 份资料有新版本；引用旧版本的产物已标记。` : '资料没有变化。');
  }));

  // ---- tasks --------------------------------------------------------------
  function filteredSessionId() {
    if (work.sessionFilter === 'active') return work.activeSessionId || '';
    return work.sessionFilter.startsWith('session:') ? work.sessionFilter.slice(8) : '';
  }

  function setSessionFilter(sessionId) {
    cancelBriefPreview();
    const select = $('#task-session-filter');
    for (const option of [...select.options]) if (option.dataset.dynamic) option.remove();
    if (sessionId) {
      const option = new Option(`会话 ${String(sessionId).slice(0, 8)}`, `session:${sessionId}`);
      option.dataset.dynamic = 'true';
      select.add(option);
      select.value = option.value;
      work.sessionFilter = option.value;
    } else {
      select.value = 'all';
      work.sessionFilter = 'all';
    }
    work.selected = null;
    work.tasks = [];
    renderTaskList();
    $('#task-detail').replaceChildren(node('div', 'small-empty', '选择左侧的任务，查看步骤、操作和产物。'));
  }

  async function refreshTasks() {
    const sessionId = filteredSessionId();
    const generation = ++taskRefreshGeneration;
    const result = await client.request(`/api/tasks${sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''}`);
    if (generation !== taskRefreshGeneration || sessionId !== filteredSessionId()) return;
    work.tasks = Array.isArray(result) ? result : result.tasks || [];
    renderTaskList();
    if (work.selected && work.tasks.some(task => task.id === work.selected)) await showTask(work.selected);
    else if (work.selected) {
      work.selected = null;
      $('#task-detail').replaceChildren(node('div', 'small-empty', '当前会话筛选中没有这项任务。'));
    }
  }

  function renderTaskList() {
    const list = $('#task-list');
    list.replaceChildren();
    if (!work.tasks.length) list.append(node('div', 'small-empty', filteredSessionId()
      ? '此会话还没有关联任务。可以切换到“全部会话”，或从当前会话新建任务。'
      : '还没有任务。可以新建纪要，或让研究任务核对资料并生成文档。'));
    for (const task of work.tasks) {
      const item = node('button', `task-item${work.selected === task.id ? ' selected' : ''}`);
      item.type = 'button';
      const head = node('div', 'task-item-head');
      head.append(node('strong', '', task.title), badge(task.status));
      const progress = node('div', 'task-progress');
      const bar = node('i');
      bar.style.width = `${Math.round(100 * task.steps_done / Math.max(1, task.steps_total))}%`;
      progress.append(bar);
      const step = active(task.status) && task.current_step ? ` · ${task.current_step}${task.progress ? ` ${task.progress}` : ''}` : '';
      item.append(head, progress, node('small', '', `${taskKind(task)} · ${origins[task.origin] || task.origin} · ${when(task.created_at)}${step}`));
      item.addEventListener('click', () => showTask(task.id).catch(error => toast(error.message, 'error')));
      list.append(item);
    }
  }

  function callRow(call) {
    const row = node('div', 'call-row');
    const head = node('div', 'call-head');
    head.append(node('strong', '', toolTitle(call.tool)), node('span', `effect ${call.effect}`, work.tools.find(tool => tool.effect === call.effect)?.effect_label || call.effect), badge(call.status));
    const notes = [call.approval ? `${call.approval.approved ? '已批准' : '已拒绝'}（${call.approval.via === 'voice' ? '语音' : '点击'}${call.approval.scope === 'task' ? '，本任务内允许' : ''}）` : call.reason];
    if (call.error) notes.push(call.error);
    row.append(head, node('code', '', call.target || '（无目标）'), node('small', '', notes.filter(Boolean).join(' · ')));
    if (call.status === 'succeeded' && call.undo) {
      row.append(action('撤销', 'button subtle small', async () => {
        const detail = { restore: '文件会恢复到被覆盖前的内容。', remove: '会删除这次新建的文件。', restore_trash: '文件会从回收区恢复到原位置。' }[call.undo.action];
        if (!await confirmAction('撤销这项操作？', detail, '撤销')) return;
        await client.request(`/api/tool-calls/${encodeURIComponent(call.id)}/undo`, { method: 'POST' });
        toast('已撤销。');
        await refreshTasks();
      }));
    }
    return row;
  }

  const researchRole = (child, index = 0) => ({
    planner: '研究主任务',
    organize: '资料整理', organizer: '资料整理', materials: '资料整理', source_researcher: '资料整理',
    verify: '证据核对', verifier: '证据核对', review: '证据核对', reviewer: '证据核对', evidence_reviewer: '证据核对',
  })[child?.name] || child?.name || `只读子任务 ${index + 1}`;
  const researchAgentName = value => typeof value === 'string'
    ? researchRole({ name: value })
    : value?.name ? researchRole(value) : safeLines(value)[0] || '研究任务';

  function researchOverview(task) {
    const params = task.params || {};
    const research = (task.steps || []).find(step => step.name === 'research');
    const researchDetail = research?.detail || {};
    const calls = allCalls(task);
    const usage = callUsage(calls);
    const selectedSessions = new Set(Array.isArray(params.session_ids) ? params.session_ids.filter(Boolean) : []);
    if (params.use_session && task.session_id) selectedSessions.add(task.session_id);
    const materialIds = Array.isArray(params.material_ids) ? params.material_ids : [];
    const materialNames = materialIds.map(id => work.materials.find(item => item.id === id)?.name).filter(Boolean);
    const savePath = params.save_path || `documents/${task.id}.md`;
    const children = Array.isArray(researchDetail.children) ? researchDetail.children : [];
    const completedChildren = children.filter(child => ['succeeded', 'completed'].includes(child.status)).length;

    const overview = node('section', 'research-overview');
    const scope = node('div', 'research-source-grid');
    const scopeItem = (label, value, hint = '') => {
      const item = node('div', 'research-source-item');
      item.append(node('span', '', label), node('strong', '', value));
      if (hint) item.append(node('small', '', hint));
      return item;
    };
    const sessionLabel = selectedSessions.size
      ? `${selectedSessions.size} 个会话${selectedSessions.has(task.session_id) ? '（含当前会话）' : ''}`
      : '不含会话原文';
    const materialsLabel = materialIds.length
      ? (materialNames.length === materialIds.length ? materialNames.join('、') : `${materialIds.length} 份已选资料`)
      : '未选择资料';
    scope.append(
      scopeItem('会话范围', sessionLabel),
      scopeItem('资料范围', materialsLabel, materialIds.length ? '只在启动时圈定的资料内选择' : ''),
      scopeItem('片段选取', params.selection_mode === 'full' ? '完整范围' : '自动相关',
        params.selection_mode === 'full' ? '超出预算会停止，不会截断' : '按问题选取相关原文，可固定或排除'),
      scopeItem('保存位置', savePath, params.overwrite ? '已允许覆盖；执行前仍需确认' : '工作目录内的 Markdown 文件'),
    );
    overview.append(node('h4', '', '研究范围'), scope);
    const timeRange = params.source_scope?.time_range;
    if (params.profile === 'continuation' && timeRange) {
      scope.append(scopeItem('会话时间（本机）', timeRange.local_date || '会话至任务创建时',
        `${timeRange.message_count} 条 · ${when(timeRange.first_at)} 至 ${when(timeRange.last_at)}`));
    }
    if (params.instructions) {
      const brief = node('div', 'research-brief');
      brief.append(node('span', '', '任务要求'), node('p', '', params.instructions));
      overview.append(brief);
    }

    const metrics = node('div', 'research-metrics');
    const metric = (label, value, hint) => {
      const item = node('div', 'research-metric');
      item.append(node('span', '', label), node('strong', '', value), node('small', '', hint));
      return item;
    };
    metrics.append(
      metric('研究职责', children.length ? `${completedChildren}/${children.length} 完成` : '等待分派', children.length ? children.map(researchRole).join('、') : '主任务会安排资料整理与证据核对'),
      metric('模型调用', `${usage.calls} 次`, usage.hasTokens ? (usage.input || usage.output ? `输入 ${compactNumber(usage.input)} · 输出 ${compactNumber(usage.output)} tokens` : `共 ${compactNumber(usage.total)} tokens`) : '服务尚未返回 token 用量'),
      metric('实际费用', usage.hasCost ? `US$${usage.cost < .0001 ? usage.cost.toPrecision(2) : usage.cost.toFixed(4)}` : '未返回', '只汇总服务实际返回的费用，不作价格估算'),
    );
    overview.append(metrics);

    if (researchDetail.plan?.length) {
      const plan = node('div', 'research-detail-block');
      plan.append(node('h4', '', '研究计划'), lineList(researchDetail.plan, 'research-plan'));
      overview.append(plan);
    }
    if (children.length) {
      const agents = node('div', 'research-detail-block');
      const list = node('div', 'research-agents');
      for (const [index, child] of children.entries()) {
        const row = node('div', 'research-agent');
        row.append(node('strong', '', researchRole(child, index)), badge(child.status || 'pending'));
        list.append(row);
      }
      agents.append(node('h4', '', '研究职责进度'), list);
      overview.append(agents);
    }
    if (researchDetail.review_summary || researchDetail.uncertainties?.length) {
      const review = node('div', 'research-review');
      review.append(node('h4', '', '证据核对'));
      if (researchDetail.review_summary) review.append(node('p', 'research-review-summary', researchDetail.review_summary));
      if (researchDetail.review_summary) review.append(node('small', 'research-review-note', '“有来源支持”是模型对所选资料的核对结果，不等于事实已经认证。'));
      if (researchDetail.uncertainties?.length) {
        review.append(node('strong', 'research-subtitle', '待确认项'), lineList(researchDetail.uncertainties, 'research-uncertainties'));
      }
      overview.append(review);
    } else if (research?.status === 'running') {
      const review = node('div', 'research-review pending');
      review.append(node('h4', '', '证据核对'), node('p', '', '只读研究任务正在整理资料并核对证据，结果会在这里更新。'));
      overview.append(review);
    }

    const saveCall = (task.tool_calls || []).find(call => call.target === savePath || /(?:save|write).*artifact|write_file/i.test(call.tool || ''));
    if (task.status === 'waiting_approval' || saveCall?.status === 'awaiting_approval') {
      overview.append(node('p', 'approval-warning research-save-state', `文档已生成，正在等待你批准保存到 ${saveCall?.target || savePath}。批准前不会写入文件。`));
    } else if (saveCall?.status === 'succeeded') {
      overview.append(node('p', 'research-save-state ok', `已保存到 ${saveCall.target || savePath}。`));
    } else if (['denied', 'expired', 'unknown', 'failed'].includes(saveCall?.status)) {
      overview.append(node('p', 'research-save-state warn', `文档没有保存：${statusLabels[saveCall.status] || saveCall.status}。`));
    }
    return overview;
  }

  async function showTask(id) {
    if (firstBrief && firstBrief.id !== id) cancelBriefPreview();
    work.selected = id;
    renderTaskList();
    let task;
    try { task = await client.request(`/api/tasks/${encodeURIComponent(id)}`); }
    catch (error) { work.selected = null; $('#task-detail').replaceChildren(node('div', 'small-empty', error.message)); return; }
    if (work.selected !== id) return;
    const detail = $('#task-detail');
    detail.replaceChildren();
    const head = node('div', 'task-detail-head');
    const title = node('div');
    const titleLine = node('div', 'task-title-line');
    titleLine.append(node('h3', '', task.title), node('span', 'tag', taskKind(task)));
    title.append(titleLine, node('small', '', `${origins[task.origin] || task.origin}发起 · ${when(task.created_at)}`));
    const actions = node('div', 'panel-tools');
    if (active(task.status)) actions.append(action('取消任务', 'button ghost small danger-hover', async () => { await client.request(`/api/tasks/${id}/cancel`, { method: 'POST' }); await refreshTasks(); }));
    if (['interrupted', 'failed', 'cancelled'].includes(task.status)) actions.append(action('继续', 'button ghost small', async () => { await client.request(`/api/tasks/${id}/resume`, { method: 'POST' }); await refreshTasks(); }));
    if (task.kind === 'research') actions.append(action('查看 / 调整来源', 'button ghost small', () => openResearchSources(task.id)));
    if (!active(task.status)) actions.append(action('删除记录', 'button subtle small danger-hover', async () => {
      if (!await confirmAction('删除这条任务记录？', '只删除任务、步骤和操作记录；已经生成的产物会保留。')) return;
      await client.request(`/api/tasks/${id}`, { method: 'DELETE' });
      work.selected = null;
      $('#task-detail').replaceChildren(node('div', 'small-empty', '任务记录已删除。'));
      await refreshTasks();
    }));
    head.append(title, badge(task.status), actions);
    detail.append(head);
    if (task.error) detail.append(node('p', 'task-error', task.error));
    if (task.kind === 'research') detail.append(researchOverview(task));
    const steps = node('ol', 'step-list');
    for (const step of task.steps) {
      const seconds = step.started_at && step.finished_at ? ` · ${((new Date(step.finished_at) - new Date(step.started_at)) / 1000).toFixed(1)} 秒` : '';
      const calls = Array.isArray(step.detail?.calls) && step.detail.calls.length ? ` · ${usageText(step.detail.calls)}` : '';
      const agent = step.detail?.agent ? ` · ${researchAgentName(step.detail.agent)}` : '';
      const running = step.status === 'running';
      const retries = step.detail?.retries?.length ? (running && step.detail.retrying ? ` · 模型服务波动，正在重试 ${step.detail.retrying}` : ` · 重试 ${step.detail.retries.length} 次`) : '';
      const item = node('li', `step ${step.status}`);
      item.append(node('span', 'step-dot'), node('strong', '', step.title), node('small', '', `${statusLabels[step.status] || step.status}${step.detail?.progress && running ? ` · ${step.detail.progress}` : ''}${agent}${seconds}${calls}${retries}`));
      steps.append(item);
    }
    detail.append(node('h4', '', '步骤'), steps);
    if (task.tool_calls?.length) detail.append(node('h4', '', '操作记录'), ...task.tool_calls.map(callRow));
    if (task.artifacts?.length) {
      detail.append(node('h4', '', '产物'));
      for (const artifact of task.artifacts) {
        const row = node('div', 'artifact-row');
        const brief = task.params?.profile === 'continuation';
        const version = brief ? artifact.task_versions?.at(-1) : undefined;
        row.append(node('strong', '', artifact.title), node('span', 'tag', brief ? '接续简报' : kinds[artifact.kind] || artifact.kind), badge(artifact.status),
                   node('small', '', `v${version || artifact.current_version}`), action(brief ? '查看简报' : '打开', 'button ghost small', () => openArtifact(artifact.id, version)));
        detail.append(row);
      }
    }
    const intent = firstBrief;
    if (intent?.id === id && !intent.opening && intent.sessionId === work.activeSessionId && visible('tasks')) {
      const created = task.artifacts?.find(item => item.task_versions?.includes(1));
      if (created && !document.querySelector('dialog[open]')) {
        intent.opening = true;
        try { await openArtifact(created.id, 1, true, id); }
        finally { if (firstBrief === intent) firstBrief = null; }
      } else if (created || !active(task.status)) cancelBriefPreview();
    }
  }
  $('#refresh-tasks').addEventListener('click', () => busy($('#refresh-tasks'), () => Promise.all([refreshTasks(), refreshApprovals()])));
  $('#task-session-filter').addEventListener('change', event => busy(event.target, async () => {
    cancelBriefPreview();
    work.sessionFilter = event.target.value;
    work.selected = null;
    work.tasks = [];
    renderTaskList();
    $('#task-detail').replaceChildren(node('div', 'small-empty', '选择左侧的任务，查看步骤、操作和产物。'));
    await refreshTasks();
  }));

  // ---- approvals ----------------------------------------------------------
  function approvalSummary(call) {
    const args = call.arguments || {};
    if (call.tool === 'export_artifact') {
      const version = args.version ? ` v${args.version}` : '';
      const fingerprint = args.content_hash ? ` · 内容指纹 ${String(args.content_hash).slice(0, 12)}` : '';
      return `将已审阅的产物${version}保存到 ${call.target || args.path || '工作目录'}${fingerprint}。批准只绑定当前版本和内容；版本或来源变化后需要重新确认。`;
    }
    if (call.effect === 'write') return `将写入 ${call.target || '指定目标'}。请先核对目标和任务说明。`;
    if (call.effect === 'launch') return `将打开 ${call.target || '外部目标'}。`;
    return call.reason || '请核对目标与影响后再决定。';
  }

  async function refreshApprovals() {
    work.approvals = await client.request('/api/approvals');
    const count = work.approvals.length;
    $('#approvals-panel').hidden = !count;
    $('#approval-count').textContent = count;
    $('#approval-indicator').hidden = !count;
    $('#approval-indicator-count').textContent = count;
    const list = $('#approvals-list');
    list.replaceChildren();
    for (const call of work.approvals) {
      const card = node('article', 'approval-card');
      const head = node('div', 'call-head');
      head.append(node('strong', '', call.title), node('span', `effect ${call.effect}`, call.effect_label), node('small', '', `来自「${call.task_title}」· ${origins[call.origin] || call.origin}`));
      const technical = node('details', 'approval-technical');
      technical.append(node('summary', '', '技术详情'), node('pre', 'call-arguments', JSON.stringify(call.arguments, null, 2)));
      card.append(head, node('p', 'approval-summary', approvalSummary(call)), node('code', '', call.target || '（无目标）'), technical);
      if (call.effect === 'execute') card.append(node('p', 'approval-warning', '命令可能修改文件、访问网络或产生其他影响，而且无法撤销。只在你理解这条命令时批准。'));
      if (call.effect === 'destructive') card.append(node('p', 'approval-warning', '文件会移入 GreatSage 回收区，之后可以在操作记录中撤销。'));
      const actions = node('div', 'panel-tools');
      actions.append(action('允许一次', 'button primary small', () => decide(call.id, true, 'once')));
      if (call.task_scope) actions.append(action('本任务内都允许', 'button ghost small', () => decide(call.id, true, 'task')));
      actions.append(action('拒绝', 'button subtle small danger-hover', () => decide(call.id, false)));
      if (call.voice) actions.append(node('small', 'voice-hint', '也可以对麦克风说“确认执行”或“取消执行”'));
      card.append(actions);
      list.append(card);
    }
  }
  async function decide(id, approved, scope = 'once') {
    await client.request(`/api/tool-calls/${encodeURIComponent(id)}/${approved ? 'approve' : 'deny'}`, { method: 'POST', body: approved ? { scope } : {} });
    await refreshApprovals();
  }

  // ---- new task -----------------------------------------------------------
  const taskDialog = node('dialog', 'task-dialog');
  taskDialog.innerHTML = `<form method="dialog"><h2>新建纪要任务</h2><p>模型只根据你选择的来源整理要点、决定和待办，并给每条内容标注出处。</p>
    <label class="field"><span>标题</span><input name="task_title" maxlength="80" placeholder="例如：产品周会纪要" /></label>
    <label class="switch-row"><span><strong>包含当前会话原文</strong><small>你的发言和旁听到的内容</small></span><input type="checkbox" role="switch" name="use_session" checked /></label>
    <div class="field"><span>资料</span><div class="material-picker"></div></div>
    <label class="field"><span>补充要求</span><textarea name="instructions" rows="3" maxlength="4000" placeholder="例如：重点整理与发布有关的决定。"></textarea></label>
    <label class="switch-row"><span><strong>同时生成文档草稿</strong><small>在纪要基础上写一篇带出处的 Markdown 文档</small></span><input type="checkbox" role="switch" name="document" /></label>
    <p class="field-error" hidden></p>
    <div class="dialog-actions"><button class="button ghost" value="cancel" formnovalidate>取消</button><button class="button primary" value="start">开始整理</button></div></form>`;
  document.body.append(taskDialog);
  const taskForm = $('form', taskDialog);
  const field = name => taskForm.elements.namedItem(name);
  function fillMaterialPicker(dialog, emptyText) {
    const picker = $('.material-picker', dialog);
    picker.replaceChildren(...(work.materials.length ? work.materials.map(item => {
      const label = node('label');
      const box = node('input');
      box.type = 'checkbox'; box.name = 'material'; box.value = item.id;
      label.append(box, document.createTextNode(`${item.name} · ${item.chunk_count} 段`));
      return label;
    }) : [node('small', 'field-hint', emptyText)]));
  }
  $('#new-task').addEventListener('click', () => busy($('#new-task'), async () => {
    await refreshMaterials();
    fillMaterialPicker(taskDialog, '资料库里还没有资料，可以只整理当前会话。');
    $('.field-error', taskDialog).hidden = true;
    taskDialog.showModal();
  }));
  taskForm.addEventListener('input', () => { $('.field-error', taskDialog).hidden = true; });
  taskForm.addEventListener('submit', event => {
    if (event.submitter?.value !== 'start') return;
    event.preventDefault();
    const body = { title: field('task_title').value.trim(), use_session: field('use_session').checked, document: field('document').checked,
                   instructions: field('instructions').value.trim(), material_ids: [...taskForm.querySelectorAll('[name=material]:checked')].map(item => item.value) };
    if (!body.use_session && !body.material_ids.length) {
      const error = $('.field-error', taskDialog);
      error.textContent = '请至少包含当前会话，或选择一份资料。';
      error.hidden = false;
      return;
    }
    busy(event.submitter, async () => {
      const task = await client.request('/api/tasks', { method: 'POST', body });
      taskDialog.close();
      taskForm.reset();
      work.selected = task.id;
      await refreshTasks();
      toast('任务已开始，进度会显示在任务页。');
    });
  });

  const researchDialog = node('dialog', 'task-dialog research-task-dialog');
  researchDialog.innerHTML = `<form method="dialog"><h2>新建研究文档任务</h2>
    <p class="research-dialog-lead">GreatSage 会在后台规划研究、整理选定资料并核对证据，再生成带出处的 Markdown 文档。</p>
    <label class="field"><span>标题</span><input name="task_title" maxlength="80" placeholder="例如：实时语音 Agent 技术方案调研" required /></label>
    <label class="field"><span>研究问题与交付要求</span><textarea name="instructions" rows="5" maxlength="4000" placeholder="说明要回答的问题、重点比较项和期望的文档结构。" required></textarea></label>
    <label class="switch-row"><span><strong>包含当前会话原文</strong><small>把当前会话作为研究来源之一</small></span><input type="checkbox" role="switch" name="use_session" checked /></label>
    <div class="field"><span>资料范围</span><div class="material-picker"></div><small class="field-hint">先圈定允许使用的资料；默认会按研究问题自动选取相关片段，不会在范围外检索。</small></div>
    <label class="field"><span>片段选取方式</span><select name="selection_mode"><option value="relevant">自动选择相关片段（推荐）</option><option value="full">读取范围内全部片段</option></select><small class="field-hint">最终最多读取 8 段、共 64,000 字符。完整范围或固定片段超出预算时任务会停止，不会静默截断或改成局部研究。</small></label>
    <label class="field"><span>保存位置</span><input name="save_path" maxlength="240" placeholder="留空则保存为 documents/任务ID.md" /><small class="field-hint">工作目录内的相对 .md 路径，例如 documents/realtime-agent.md</small></label>
    <label class="switch-row"><span><strong>允许覆盖同名文件</strong><small>默认关闭；即使开启，写入前仍需点击批准</small></span><input type="checkbox" role="switch" name="overwrite" /></label>
    <div class="research-flow" aria-label="研究流程"><span>收集来源</span><i></i><span>规划 · 整理 · 核对</span><i></i><span>生成文档</span><i></i><span>审批保存</span></div>
    <p class="research-approval-note">任务会在后台运行。文档生成后会出现在“待确认的操作”中；只有你点击批准才会写入工作目录，开始任务不会下载文件。</p>
    <p class="field-error" hidden></p>
    <div class="dialog-actions"><button type="button" class="button ghost research-cancel">取消</button><button class="button primary" value="start">开始研究</button></div></form>`;
  document.body.append(researchDialog);
  const researchForm = $('form', researchDialog);
  const researchField = name => researchForm.elements.namedItem(name);
  $('.research-cancel', researchDialog).addEventListener('click', () => researchDialog.close());
  $('#new-research-task').addEventListener('click', () => busy($('#new-research-task'), async () => {
    await refreshMaterials();
    fillMaterialPicker(researchDialog, '资料库里还没有资料，可以使用当前会话；也可以先去资料库导入 Markdown 文件。');
    $('.field-error', researchDialog).hidden = true;
    researchDialog.showModal();
  }));
  researchForm.addEventListener('input', () => { $('.field-error', researchDialog).hidden = true; });
  researchForm.addEventListener('submit', event => {
    if (event.submitter?.value !== 'start') return;
    event.preventDefault();
    const body = {
      kind: 'research',
      title: researchField('task_title').value.trim(),
      instructions: researchField('instructions').value.trim(),
      material_ids: [...researchForm.querySelectorAll('[name=material]:checked')].map(item => item.value),
      use_session: researchField('use_session').checked,
      selection_mode: researchField('selection_mode').value || 'relevant',
      pinned_source_ids: [],
      excluded_source_ids: [],
      save_path: researchField('save_path').value.trim(),
      overwrite: researchField('overwrite').checked,
    };
    const error = $('.field-error', researchDialog);
    if (!body.title || !body.instructions) {
      error.textContent = '请填写标题和研究问题。';
      error.hidden = false;
      return;
    }
    if (!body.use_session && !body.material_ids.length) {
      error.textContent = '请至少包含当前会话，或选择一份资料。';
      error.hidden = false;
      return;
    }
    const segments = body.save_path.replace(/\\/g, '/').split('/');
    if (body.save_path && (/^[\\/]/.test(body.save_path) || body.save_path.includes(':') || segments.includes('..') || !/\.md$/i.test(body.save_path))) {
      error.textContent = '保存位置必须是工作目录内、不含“..”的相对 .md 路径。';
      error.hidden = false;
      return;
    }
    busy(event.submitter, async () => {
      const task = await client.request('/api/tasks', { method: 'POST', body });
      researchDialog.close();
      researchForm.reset();
      work.selected = task.id;
      await refreshTasks();
      toast('研究任务已在后台开始；需要保存时会在任务页等待你批准。');
    });
  });

  // ---- research source selection -----------------------------------------
  const sourceDialog = node('dialog', 'record-dialog research-sources-dialog');
  sourceDialog.innerHTML = `<div class="dialog-actions"><button type="button" class="button small ghost source-close">关闭</button></div>
    <div class="source-dialog-head"><div><h2>研究来源</h2><p>这里显示任务范围内的候选与本轮选中的原文片段。固定、排除或更改模式会创建新任务，原任务与产物不变。</p></div></div>
    <div class="source-selection-alert" hidden></div>
    <div class="source-selection-stats"></div>
    <fieldset class="source-mode"><legend>片段选取方式</legend><label><input type="radio" name="source_mode" value="relevant" /> 自动选择相关片段</label><label><input type="radio" name="source_mode" value="full" /> 读取范围内全部片段</label><small>固定片段或完整范围超出 8 段 / 64,000 字符预算时会停止，不会截断或自动改成局部研究。</small></fieldset>
    <div class="source-candidates"></div>
    <label class="switch-row source-update-scope" hidden><span><strong>采用更新后的来源</strong><small>原资料或会话已变化；只有明确开启才会用新版本替换旧范围。</small></span><input type="checkbox" role="switch" /></label>
    <p class="field-error source-selection-error" hidden></p>
    <div class="dialog-actions"><button type="button" class="button primary source-rerun">按此范围重新运行</button></div>`;
  document.body.append(sourceDialog);
  let sourceRequestGeneration = 0;
  let sourceOpenIntent = false;
  let sourceSnapshot = null;
  let sourcePinned = new Set();
  let sourceExcluded = new Set();

  const sourceReason = reason => ({
    pinned: '用户固定', relevant: '与研究问题相关', full: '完整范围', full_scope: '完整范围',
    small_scope: '范围较小，已全部选取', selected: '已选取', excluded: '用户排除',
    not_relevant: '相关性不足', budget: '超出预算', not_selected: '未被选取', unavailable: '来源已失效',
  })[reason] || reason || '候选来源';

  function invalidateSourceDialog() {
    sourceOpenIntent = false;
    sourceRequestGeneration += 1;
    sourceSnapshot = null;
    sourcePinned = new Set();
    sourceExcluded = new Set();
    $('.source-candidates', sourceDialog).replaceChildren();
  }

  function closeSourceDialog() {
    invalidateSourceDialog();
    if (sourceDialog.open) sourceDialog.close();
  }

  async function loadSourceText(taskId, candidate, container, generation) {
    container.replaceChildren(node('div', 'small-empty', '正在读取任务范围内的原文…'));
    try {
      const result = await client.request(`/api/tasks/${encodeURIComponent(taskId)}/sources/${encodeURIComponent(candidate.id)}`);
      if (!sourceOpenIntent || generation !== sourceRequestGeneration || sourceSnapshot?.task_id !== taskId) return;
      const pre = node('pre', 'source-original', result.text || '');
      const meta = node('small', 'source-original-meta', `${result.label || candidate.label} · ${result.available === false ? '已失效' : '内容哈希 ' + String(result.content_hash || '').slice(0, 12)}`);
      container.replaceChildren(meta, pre);
    } catch (error) {
      if (!sourceOpenIntent || generation !== sourceRequestGeneration) return;
      container.closest('details')?.removeAttribute('data-loaded');
      container.replaceChildren(node('div', 'small-empty', error.message || '原文已不可用。'));
    }
  }

  function renderResearchSources(snapshot, generation) {
    sourceSnapshot = snapshot;
    sourcePinned = new Set(snapshot.pinned_source_ids || []);
    sourceExcluded = new Set(snapshot.excluded_source_ids || []);
    const mode = sourceDialog.querySelector(`[name=source_mode][value="${snapshot.mode === 'full' ? 'full' : 'relevant'}"]`);
    if (mode) mode.checked = true;
    const stats = $('.source-selection-stats', sourceDialog);
    const stat = (label, value, hint) => {
      const item = node('div', 'source-selection-stat');
      item.append(node('span', '', label), node('strong', '', value), node('small', '', hint));
      return item;
    };
    stats.replaceChildren(
      stat('实际选取', `${snapshot.selected_count || 0} / ${snapshot.total_sources || 0} 段`, snapshot.all_selected ? '范围内候选已全部选中' : '仅在选中来源内研究'),
      stat('字符预算', `${integerNumber(snapshot.selected_chars)} / ${integerNumber(snapshot.max_chars || 64000)}`, `上限 ${snapshot.max_sources || 8} 段`),
      stat('范围', `${snapshot.scope?.material_ids?.length || 0} 份资料 · ${snapshot.scope?.session_ids?.length || 0} 个会话`, snapshot.mode === 'full' ? '完整范围模式' : '相关片段模式'),
    );
    const alert = $('.source-selection-alert', sourceDialog);
    const messages = [];
    if (snapshot.selection_status === 'stale') messages.push('任务来源已变化；重跑时不会自动采用新内容。');
    if (snapshot.selection_status === 'needs_attention') messages.push('当前来源范围需要处理后才能运行。');
    if (snapshot.error) messages.push(snapshot.error);
    if (snapshot.candidates_limited) messages.push('候选列表只显示前 100 段；已选片段优先显示，其余请缩小来源范围。');
    alert.hidden = !messages.length;
    alert.className = `source-selection-alert ${snapshot.selection_status === 'selected' ? '' : 'warn'}`;
    alert.textContent = messages.join(' ');
    const update = $('.source-update-scope', sourceDialog);
    update.hidden = snapshot.selection_status !== 'stale';
    $('input', update).checked = false;

    const list = $('.source-candidates', sourceDialog);
    list.replaceChildren();
    if (!snapshot.candidates?.length) list.append(node('div', 'small-empty', '此范围没有可用原文。请返回任务并调整资料或会话范围。'));
    for (const candidate of snapshot.candidates || []) {
      const card = node('article', `source-candidate${candidate.selected ? ' selected' : ''}${candidate.available === false ? ' unavailable' : ''}`);
      const head = node('div', 'source-candidate-head');
      const title = node('div', 'source-candidate-title');
      title.append(node('strong', '', candidate.label || `来源 ${String(candidate.id).slice(0, 8)}`),
        node('small', '', `${candidate.kind === 'message' ? '会话原文' : '资料片段'} · ${compactNumber(candidate.chars || 0)} 字符 · ${sourceReason(candidate.reason)}`));
      const badges = node('div', 'source-candidate-badges');
      if (candidate.handle) badges.append(node('span', 'tag', candidate.handle));
      if (candidate.selected) badges.append(node('span', 'badge ok', '本轮选中'));
      if (candidate.available === false) badges.append(node('span', 'badge bad', '已失效'));
      head.append(title, badges);
      card.append(head, node('p', 'source-excerpt', candidate.excerpt || '没有可显示的摘要。'));
      const controls = node('div', 'source-candidate-controls');
      const pin = node('label', 'check-label');
      const pinBox = node('input'); pinBox.type = 'checkbox'; pinBox.checked = sourcePinned.has(candidate.id);
      pin.append(pinBox, document.createTextNode('固定选取'));
      const exclude = node('label', 'check-label');
      const excludeBox = node('input'); excludeBox.type = 'checkbox'; excludeBox.checked = sourceExcluded.has(candidate.id);
      exclude.append(excludeBox, document.createTextNode('排除'));
      pinBox.addEventListener('change', () => {
        if (pinBox.checked) { sourcePinned.add(candidate.id); sourceExcluded.delete(candidate.id); excludeBox.checked = false; }
        else sourcePinned.delete(candidate.id);
      });
      excludeBox.addEventListener('change', () => {
        if (excludeBox.checked) { sourceExcluded.add(candidate.id); sourcePinned.delete(candidate.id); pinBox.checked = false; }
        else sourceExcluded.delete(candidate.id);
      });
      controls.append(pin, exclude);
      if (candidate.available !== false) {
        const details = node('details', 'source-original-details');
        details.append(node('summary', '', '查看任务范围内原文'), node('div', 'source-original-wrap'));
        details.addEventListener('toggle', () => {
          if (details.open && !details.dataset.loaded) {
            details.dataset.loaded = 'true';
            loadSourceText(snapshot.task_id, candidate, $('.source-original-wrap', details), generation);
          }
        });
        controls.append(details);
      }
      card.append(controls);
      list.append(card);
    }
  }

  async function openResearchSources(taskId) {
    sourceOpenIntent = true;
    const generation = ++sourceRequestGeneration;
    $('.source-candidates', sourceDialog).replaceChildren(node('div', 'small-empty', '正在读取候选与实际选取范围…'));
    $('.source-selection-stats', sourceDialog).replaceChildren();
    $('.source-selection-alert', sourceDialog).hidden = true;
    $('.source-selection-error', sourceDialog).hidden = true;
    if (!sourceDialog.open) sourceDialog.showModal();
    try {
      const snapshot = await client.request(`/api/tasks/${encodeURIComponent(taskId)}/sources`);
      if (!sourceOpenIntent || generation !== sourceRequestGeneration) return;
      renderResearchSources(snapshot, generation);
    } catch (error) {
      if (!sourceOpenIntent || generation !== sourceRequestGeneration) return;
      $('.source-candidates', sourceDialog).replaceChildren(node('div', 'small-empty', error.message || '研究来源加载失败。'));
    }
  }

  $('.source-close', sourceDialog).addEventListener('click', closeSourceDialog);
  sourceDialog.addEventListener('cancel', invalidateSourceDialog);
  sourceDialog.addEventListener('close', invalidateSourceDialog);
  $('.source-rerun', sourceDialog).addEventListener('click', () => busy($('.source-rerun', sourceDialog), async () => {
    if (!sourceSnapshot) return;
    const snapshot = sourceSnapshot;
    const generation = sourceRequestGeneration;
    const error = $('.source-selection-error', sourceDialog);
    error.hidden = true;
    const mode = sourceDialog.querySelector('[name=source_mode]:checked')?.value || 'relevant';
    const updateSources = !$('.source-update-scope', sourceDialog).hidden && $('input', $('.source-update-scope', sourceDialog)).checked;
    const task = await client.request(`/api/tasks/${encodeURIComponent(snapshot.task_id)}/reselect`, {
      method: 'POST', body: { selection_mode: mode, pinned_source_ids: [...sourcePinned],
        excluded_source_ids: [...sourceExcluded], update_sources: updateSources },
    });
    if (!sourceOpenIntent || generation !== sourceRequestGeneration || sourceSnapshot !== snapshot) {
      toast('来源重选任务已创建；你已离开原来源窗口，可在任务列表查看进度。');
      return;
    }
    closeSourceDialog();
    work.selected = task.id;
    await refreshTasks();
    toast(updateSources ? '已采用更新后的来源创建新任务；保存仍需重新批准。' : '已按新的来源范围创建任务；原任务与产物保持不变。');
  }));

  // ---- artifacts ----------------------------------------------------------
  const artifactDialog = node('dialog', 'record-dialog artifact-dialog');
  artifactDialog.innerHTML = `<div class="dialog-actions"><select class="artifact-version" aria-label="选择版本"></select><button type="button" class="button small ghost artifact-close">关闭</button></div>
    <div class="artifact-head"><h2></h2><span class="artifact-badges"></span></div>
    <p class="approval-warning artifact-stale" hidden>部分来源已删除或已变更，对应引用已失效。可以按修改意见重新生成，或核对后自己修改。</p>
    <div class="artifact-body"></div>
    <div class="brief-controls" hidden><button type="button" class="button primary small brief-correct">纠正简报</button><button type="button" class="button ghost small brief-approval">查看保存审批</button></div>
    <div class="brief-feedback" hidden role="status"></div>
    <section class="artifact-review-panel"><div class="artifact-review-head"><div><h3>证据审阅与版本对比</h3><small>引用状态只说明标记能否定位原文，不代表结论已经过事实认证。</small></div><label><span>对比</span><select class="artifact-compare-version" aria-label="选择对比版本"></select></label></div><div class="artifact-review-content"><div class="small-empty">正在读取证据审阅信息…</div></div></section>
    <h3>引用原文与定位</h3><div class="citation-list"></div>
    <div class="regenerate-form" hidden><label class="field"><span>纠正或修改要求</span><textarea rows="3" maxlength="4000" placeholder="例如：负责人应为林岚；保留两个冲突日期并标为待确认。"></textarea><small>纠正会单独保存为用户来源；旧模型产物只作为待修改对象，不会成为原始事实证据。</small></label><label class="switch-row regenerate-update-sources"><span><strong>采用更新后的来源</strong><small>只有明确开启，重新生成任务才会用当前资料替换旧来源版本</small></span><input type="checkbox" role="switch" /></label><p class="field-error regenerate-error" hidden></p><div class="panel-tools"><button type="button" class="button primary small regenerate-start">开始重新生成</button><button type="button" class="button subtle small regenerate-cancel">取消</button></div></div>
    <div class="panel-tools artifact-actions"><button type="button" class="button primary small artifact-save">保存为新版本</button><button type="button" class="button ghost small artifact-regenerate">按修改意见重新生成</button><button type="button" class="button ghost small artifact-export">导出 .md</button><button type="button" class="button subtle small danger-hover artifact-delete">删除</button></div>`;
  document.body.append(artifactDialog);
  const inDialog = selector => $(selector, artifactDialog);
  let artifactRequestGeneration = 0;
  let artifactOpenIntent = false;
  let revisionIntent = null;
  let revisionTimer;
  let submittingCorrection = false;
  let voiceBusy = false;
  const voiceArea = node('div', 'brief-voice');
  const correctionForm = inDialog('.regenerate-form'), correctionInput = $('textarea', correctionForm);
  correctionForm.insertBefore(voiceArea, inDialog('.regenerate-update-sources'));
  const correctionVoice = setupCorrectionVoice({ client, container: voiceArea, textarea: correctionInput,
    getTarget() {
      const a = work.artifact;
      return artifactOpenIntent && !correctionForm.hidden && a?.profile === 'continuation' && a.session_id === work.activeSessionId
        ? { artifact_id: a.id, expected_version: a.version, session_id: a.session_id } : null;
    },
    onBusyChange(value) { voiceBusy = value; inDialog('.regenerate-start').disabled = value || submittingCorrection; },
  });
  function cancelRevisionWatch() { revisionIntent = null; clearTimeout(revisionTimer); }
  function feedback(text) {
    const area = inDialog('.brief-feedback');
    area.replaceChildren(document.createTextNode(text)); area.hidden = !text;
    return area;
  }

  function invalidateArtifactDialog() {
    correctionVoice.reset();
    cancelRevisionWatch();
    feedback('');
    artifactOpenIntent = false;
    artifactRequestGeneration += 1;
    work.artifact = null;
    inDialog('.artifact-body').replaceChildren();
    inDialog('.citation-list').replaceChildren();
    inDialog('.artifact-review-content').replaceChildren();
  }
  function closeArtifactDialog() {
    invalidateArtifactDialog();
    if (artifactDialog.open) artifactDialog.close();
  }
  artifactDialog.addEventListener('cancel', invalidateArtifactDialog);

  function todoTable(artifact) {
    const table = node('div', 'todo-table');
    if (!artifact.todos.length) table.append(node('small', 'field-hint', '来源中没有明确的待办。'));
    for (const todo of artifact.todos) {
      const row = node('div', `todo-row${todo.done ? ' done' : ''}`);
      const done = node('input'); done.type = 'checkbox'; done.checked = todo.done; done.setAttribute('aria-label', '完成');
      const inputs = [['text', todo.text, '待办内容'], ['owner', todo.owner, '负责人（未确认）'], ['due', todo.due, '期限（未确认）']].map(([key, value, placeholder]) => {
        const input = node('input'); input.type = 'text'; input.value = value; input.placeholder = placeholder; input.dataset.key = key; input.setAttribute('aria-label', placeholder);
        return input;
      });
      const save = changes => busy(done, async () => { await client.request(`/api/todos/${encodeURIComponent(todo.id)}`, { method: 'PUT', body: changes }); row.classList.toggle('done', done.checked); });
      done.addEventListener('change', () => save({ done: done.checked }));
      for (const input of inputs) input.addEventListener('change', () => save({ [input.dataset.key]: input.value }));
      row.append(done, ...inputs, node('small', '', todo.markers.map(marker => `[${marker}]`).join('') || '未标注来源'));
      table.append(row);
    }
    return table;
  }

  const citationStatusLabel = { cited: '引用有效', invalid: '引用已失效', unknown: '未知引用', uncited: '未标引用' };
  function locatorText(locator) {
    if (!locator) return '';
    if (locator.kind === 'chunk') {
      return [locator.name, locator.version ? `v${locator.version}` : '', locator.heading,
        locator.line_start ? `第 ${locator.line_start}–${locator.line_end || locator.line_start} 行` : ''].filter(Boolean).join(' · ');
    }
    return [`会话 ${String(locator.session_id || '').slice(0, 8)}`, locator.created_at ? when(locator.created_at) : ''].filter(Boolean).join(' · ');
  }

  function renderCitations(artifact, review) {
    const citations = inDialog('.citation-list');
    const rows = review?.citations || artifact.citations || [];
    citations.replaceChildren(...rows.map(item => {
      const row = node('details', `citation${item.valid ? '' : ' invalid'}`);
      row.dataset.marker = item.marker;
      const summary = node('summary');
      summary.append(node('strong', '', `[${item.marker}]`), node('span', '', item.label || '未命名来源'),
        node('small', '', item.valid ? locatorText(item.locator) || '可定位原文' : '来源已删除或已变更'));
      const body = node('div', 'citation-evidence');
      body.append(node('small', '', '引用原文'), node('p', 'record-text', item.valid ? item.excerpt || '此引用没有可显示的摘录。' : '失效引用不再显示旧原文。'));
      if (item.locator) body.append(node('small', '', `定位：${locatorText(item.locator)}`));
      if (artifact.profile === 'continuation' && item.valid && item.source_id && artifact.generating_task_id) {
        const full = node('pre', 'record-text');
        const read = action('读取原文', 'button ghost small', async () => {
          const source = await client.request(`/api/tasks/${encodeURIComponent(artifact.generating_task_id)}/sources/${encodeURIComponent(item.source_id)}`);
          if (work.artifact !== artifact || !row.isConnected) return;
          full.textContent = source.available === false ? '原文已失效。' : source.text || '没有原文可显示。';
        });
        body.append(read, full);
      }
      row.append(summary, body);
      return row;
    }), ...(review?.unknown_markers || artifact.unknown_markers || []).map(marker => node('div', 'citation invalid', `[${marker}] 没有对应来源`)));
    if (!citations.children.length) citations.append(node('small', 'field-hint', '这份产物没有引用标记。'));
  }

  function renderArtifactReview(artifact, review, compareTo = '') {
    const container = inDialog('.artifact-review-content');
    if (!review || review.error) {
      container.replaceChildren(node('div', 'small-empty', review?.error || '证据审阅信息暂不可用。'));
      renderCitations(artifact, null);
      return;
    }
    const compare = inDialog('.artifact-compare-version');
    const versions = artifact.versions || [];
    const parentLabel = review.parent_version ? `父版本 v${review.parent_version}（默认）` : '首版（没有父版本）';
    compare.replaceChildren(new Option(parentLabel, ''), ...versions
      .filter(item => Number(item.version) !== Number(review.version))
      .map(item => new Option(`v${item.version} · ${when(item.created_at)}`, item.version)));
    compare.value = compareTo === '' || compareTo == null ? '' : String(compareTo);
    compare.disabled = versions.length <= 1;

    const summary = node('div', 'artifact-review-summary');
    const origin = versions.find(item => Number(item.version) === Number(review.version));
    const fields = [
      ['当前审阅', `v${review.version}${Number(review.version) === Number(review.current_version) ? '（最新）' : `（最新为 v${review.current_version}）`}`],
      ['生成任务', review.generating_task_id ? String(review.generating_task_id).slice(0, 12) : '原始任务'],
      ['版本来源', origin?.origin === 'edited' ? '手动编辑' : origin?.origin === 'regenerated' ? '按纠正重新生成' : '模型生成'],
      ['用户纠正', review.correction_source_id ? (review.correction_source_valid === false ? '纠正来源已失效' : '已保存并关联') : '无'],
    ];
    for (const [label, value] of fields) {
      const item = node('div'); item.append(node('span', '', label), node('strong', '', value)); summary.append(item);
    }
    container.replaceChildren(summary);
    if (Number(review.version) !== Number(review.current_version)) {
      container.append(node('p', 'inline-warning artifact-version-warning', `你正在审阅旧版本 v${review.version}。旧版本只读；请切换到最新版本 v${review.current_version} 后再保存或重新生成，保存前会再次检查版本以防误覆盖。`));
    }
    if (review.semantic_verification === false) {
      container.append(node('p', 'artifact-review-note', '本页核对引用标记、原文定位和版本来源；不会把模型结论标记为已经事实认证。'));
    }
    const passages = node('div', 'artifact-passages');
    for (const passage of review.passages || []) {
      const row = node('article', `artifact-passage ${passage.citation_status || 'uncited'}`);
      const head = node('div', 'artifact-passage-head');
      head.append(node('strong', '', `第 ${passage.line_number} 行`), node('span', 'tag', citationStatusLabel[passage.citation_status] || passage.citation_status));
      const markers = node('div', 'artifact-passage-markers');
      for (const marker of passage.markers || []) {
        const button = node('button', 'source-link', `[${marker}]`); button.type = 'button';
        button.addEventListener('click', () => {
          const target = inDialog(`.citation[data-marker="${CSS.escape(marker)}"]`);
          if (target) { target.open = true; target.scrollIntoView({ block: 'center' }); target.focus(); }
        });
        markers.append(button);
      }
      head.append(markers);
      row.append(head, node('p', 'record-text', passage.text || ''));
      if (passage.text_truncated) row.append(node('small', 'field-hint', '此行文本在审阅响应中已截断。'));
      passages.append(row);
    }
    if (!passages.children.length) passages.append(node('div', 'small-empty', '没有可审阅的 Markdown 正文行。'));
    container.append(node('h4', '', '正文与引用覆盖'), passages);
    if (review.passages_truncated) container.append(node('p', 'inline-warning', '正文行数超过审阅上限，只显示有界片段；请勿把此视图当作全文。'));

    const diffBlock = node('div', 'artifact-diff');
    diffBlock.append(node('h4', '', '版本差异'));
    if (review.diff) {
      diffBlock.append(node('small', '', `v${review.diff.from_version} → v${review.diff.to_version}`), node('pre', '', review.diff.text || '没有文本差异。'));
      if (review.diff.truncated || review.diff.input_truncated || review.diff.output_truncated) {
        diffBlock.append(node('p', 'inline-warning', '差异或参与比较的版本超过显示上限，当前内容已明确截断。'));
      }
    } else diffBlock.append(node('p', 'source-note', review.parent_version ? '该版本没有可显示的差异。' : '首版没有父版本可比较。'));
    container.append(diffBlock);
    if (artifact.profile === 'continuation' && review.diff) container.insertBefore(diffBlock, summary.nextSibling);
    renderCitations(artifact, review);
  }

  function syncArtifactActions(artifact = work.artifact) {
    if (!artifact) return;
    const latest = Number(artifact.version) === Number(artifact.current_version);
    const brief = artifact.profile === 'continuation';
    inDialog('.brief-controls').hidden = !brief;
    inDialog('.brief-correct').disabled = !latest || work.activeSessionId !== artifact.session_id || Boolean(revisionIntent);
    inDialog('.brief-correct').title = !latest ? '请选择最新版本进行纠正' : work.activeSessionId !== artifact.session_id ? '请先继续简报所属会话，再提交纠正' : '用文字或麦克风提出纠正';
    inDialog('.brief-approval').disabled = !latest || !artifact.generating_task_id;
    inDialog('.artifact-regenerate').hidden = brief;
    inDialog('.artifact-export').hidden = brief;
    inDialog('.artifact-save').hidden = brief || artifact.kind === 'todos';
    inDialog('.artifact-save').disabled = !latest;
    inDialog('.artifact-regenerate').disabled = !latest;
    inDialog('.artifact-export').disabled = !latest;
    inDialog('.artifact-save').title = latest ? '把当前内容保存为新版本' : '旧版本只读，请先切换到最新版本';
    inDialog('.artifact-regenerate').title = latest ? '根据纠正生成新版本' : '旧版本只读，请先切换到最新版本';
    inDialog('.artifact-export').title = latest ? '导出当前最新版本' : '当前接口只导出最新版本，请先切换到最新版本';
  }

  function resyncArtifactActionsAfterBusy() {
    setTimeout(() => syncArtifactActions(), 0);
  }

  async function loadArtifactReview(compareTo = '') {
    const artifact = work.artifact;
    if (!artifact) return;
    const generation = ++artifactRequestGeneration;
    const query = new URLSearchParams({ version: String(artifact.version) });
    if (compareTo !== '' && compareTo != null) query.set('compare_to', String(compareTo));
    inDialog('.artifact-review-content').replaceChildren(node('div', 'small-empty', '正在读取证据与版本差异…'));
    try {
      const review = await client.request(`/api/artifacts/${encodeURIComponent(artifact.id)}/review?${query}`);
      if (!artifactOpenIntent || generation !== artifactRequestGeneration || work.artifact?.id !== artifact.id || work.artifact?.version !== artifact.version) return;
      renderArtifactReview(artifact, review, compareTo);
    } catch (error) {
      if (!artifactOpenIntent || generation !== artifactRequestGeneration) return;
      renderArtifactReview(artifact, { error: error.message }, compareTo);
    }
  }

  async function openArtifact(id, version, requestOpen = true, expectedTask = '') {
    if (requestOpen) artifactOpenIntent = true;
    else if (!artifactOpenIntent) return;
    correctionVoice.reset();
    cancelRevisionWatch();
    feedback('');
    const previous = work.artifact;
    work.artifact = null;
    correctionForm.hidden = true;
    const generation = ++artifactRequestGeneration;
    const affected = [inDialog('.artifact-actions'), inDialog('.brief-controls')];
    for (const panel of affected) panel.inert = true;
    let artifact;
    try {
      artifact = await client.request(`/api/artifacts/${encodeURIComponent(id)}${version ? `?version=${version}` : ''}`);
      if (expectedTask && artifact.generating_task_id !== expectedTask) throw new Error('版本与提交的纠正任务不匹配，请从任务记录核对。');
    } catch (err) {
      if (!artifactOpenIntent || generation !== artifactRequestGeneration) return;
      work.artifact = previous;
      if (previous) inDialog('.artifact-version').value = previous.version;
      throw err;
    } finally {
      if (generation === artifactRequestGeneration) for (const panel of affected) panel.inert = false;
    }
    if (!artifactOpenIntent || generation !== artifactRequestGeneration) return;
    work.artifact = artifact;
    inDialog('h2').textContent = artifact.title;
    inDialog('.artifact-badges').replaceChildren(node('span', 'tag', kinds[artifact.kind] || artifact.kind), badge(artifact.status));
    inDialog('.artifact-version').replaceChildren(...artifact.versions.map(item => new Option(`v${item.version} · ${({ generated: '生成', edited: '编辑', regenerated: '重新生成' })[item.origin] || item.origin} · ${when(item.created_at)}`, item.version)));
    inDialog('.artifact-version').value = artifact.version;
    inDialog('.artifact-compare-version').replaceChildren(new Option('父版本（默认）', ''));
    inDialog('.artifact-stale').hidden = artifact.status !== 'stale';
    const body = inDialog('.artifact-body');
    const brief = artifact.profile === 'continuation';
    artifactDialog.insertBefore(correctionForm, inDialog(brief ? '.brief-feedback' : '.artifact-actions'));
    if (brief) body.replaceChildren(briefColumns(artifact.content, marker => {
      const target = inDialog(`.citation[data-marker="${CSS.escape(marker)}"]`);
      if (!target) { toast('未找到这一标记对应的原文。'); return; }
      target.open = true; target.scrollIntoView({ block: 'center' }); target.focus();
    }));
    else if (artifact.kind === 'todos') body.replaceChildren(todoTable(artifact));
    else {
      const editor = node('textarea', 'artifact-editor');
      editor.value = artifact.content; editor.rows = 18; editor.setAttribute('aria-label', 'Markdown 内容');
      body.replaceChildren(editor);
    }
    syncArtifactActions(artifact);
    renderCitations(artifact, null);
    inDialog('.regenerate-form').hidden = true;
    inDialog('.regenerate-form textarea').value = '';
    inDialog('.regenerate-update-sources input').checked = false;
    voiceArea.hidden = !brief;
    inDialog('.regenerate-update-sources').hidden = brief;
    inDialog('.regenerate-start').textContent = brief ? '提交纠正，查看差异' : '开始重新生成';
    inDialog('.regenerate-error').hidden = true;
    if (!artifactDialog.open) {
      if (firstBrief?.opening && document.querySelector('dialog[open]')) { invalidateArtifactDialog(); return; }
      artifactDialog.showModal();
    }
    await loadArtifactReview();
    if (expectedTask && artifact.parent_version && artifactOpenIntent && work.artifact === artifact) {
      inDialog('.artifact-diff')?.scrollIntoView({ block: 'center' });
    }
  }

  inDialog('.artifact-close').addEventListener('click', closeArtifactDialog);
  function showCorrection() {
    if (!work.artifact) return;
    correctionForm.hidden = false; inDialog('.regenerate-error').hidden = true;
    correctionInput.focus();
  }
  inDialog('.brief-correct').addEventListener('click', showCorrection);
  inDialog('.brief-approval').addEventListener('click', () => busy(inDialog('.brief-approval'), async () => {
    const a = work.artifact;
    if (!a) return;
    const task = await client.request(`/api/tasks/${encodeURIComponent(a.generating_task_id)}`);
    if (!artifactOpenIntent || work.artifact !== a) return;
    const call = (task.tool_calls || []).find(item => item.tool === 'export_artifact' &&
      item.arguments?.artifact_id === a.id && Number(item.arguments.version) === Number(a.version));
    if (!call) { feedback('此版本尚无保存请求。可在任务详情查看进度，再查询审批。'); return; }
    if (call.status !== 'awaiting_approval') {
      feedback(`v${a.version} 保存状态：${statusLabels[call.status] || call.status} · ${call.target}`); return;
    }
    const panel = feedback(approvalSummary(call));
    const controls = node('div', 'panel-tools');
    for (const [label, approved] of [['批准保存此版本', true], ['拒绝保存', false]]) {
      controls.append(action(label, 'button small ' + (approved ? 'primary' : 'ghost'), async () => {
        if (!artifactOpenIntent || work.artifact !== a || !controls.isConnected) return;
        for (const button of controls.children) button.disabled = true;
        try {
          await decide(call.id, approved);
          if (work.artifact === a) feedback(approved ? `已批准保存 v${a.version}。可再次查询写入结果。` : `已拒绝保存 v${a.version}。`);
        } finally { for (const button of controls.children) button.disabled = false; }
      }));
    }
    panel.append(controls); panel.scrollIntoView({ block: 'nearest' });
  }));

  async function watchRevision(intent) {
    if (revisionIntent !== intent || !artifactOpenIntent) return;
    try {
      const task = await client.request(`/api/tasks/${encodeURIComponent(intent.taskId)}`);
      if (revisionIntent !== intent || !artifactOpenIntent) return;
      const version = task.artifacts?.find(item => item.id === intent.artifactId)?.task_versions?.find(v => v > intent.parentVersion);
      if (version) {
        await openArtifact(intent.artifactId, version, false, intent.taskId);
        return;
      }
      if (!active(task.status)) {
        cancelRevisionWatch(); syncArtifactActions();
        feedback(task.error || `纠正任务${statusLabels[task.status] || task.status}，没有生成新版本。`); return;
      }
      revisionTimer = setTimeout(() => watchRevision(intent), 1500);
    } catch (err) {
      if (!artifactOpenIntent || (revisionIntent && revisionIntent !== intent)) return;
      if (work.artifact?.id !== intent.artifactId || work.activeSessionId !== intent.sessionId) return;
      feedback(err.message + '。可以从任务记录重新查看。');
      cancelRevisionWatch(); syncArtifactActions();
    }
  }
  inDialog('.artifact-version').addEventListener('change', event => busy(event.target, () => openArtifact(work.artifact.id, Number(event.target.value), false)));
  inDialog('.artifact-compare-version').addEventListener('change', event => busy(event.target, () => loadArtifactReview(event.target.value)));
  inDialog('.artifact-save').addEventListener('click', () => busy(inDialog('.artifact-save'), async () => {
    const artifact = work.artifact;
    const id = artifact.id;
    const content = inDialog('.artifact-editor').value;
    if (!content.trim()) throw new Error('内容不能为空。');
    try {
      await client.request(`/api/artifacts/${encodeURIComponent(id)}`, { method: 'PUT', body: { content, expected_version: artifact.version } });
      if (!artifactOpenIntent || work.artifact !== artifact) {
        toast('新版本已保存；当前打开的产物没有被旧请求覆盖。');
        return;
      }
      await openArtifact(id, undefined, false);
      toast('已保存为新版本，引用仍指向原来的来源。');
    } finally {
      resyncArtifactActionsAfterBusy();
    }
  }));
  inDialog('.artifact-regenerate').addEventListener('click', showCorrection);
  inDialog('.regenerate-cancel').addEventListener('click', () => { correctionVoice.reset(); correctionForm.hidden = true; inDialog('.regenerate-error').hidden = true; });
  inDialog('.regenerate-start').addEventListener('click', () => busy(inDialog('.regenerate-start'), async () => {
    const artifact = work.artifact;
    if (!artifact || voiceBusy || submittingCorrection) return;
    if (artifact.kind === 'todos' && !await confirmAction('重新生成待办清单？', '新清单会替换当前的待办，你手动修改过的条目会被覆盖；旧清单保留在版本记录中。', '重新生成')) return;
    const instructions = inDialog('.regenerate-form textarea').value.trim();
    const updateSources = inDialog('.regenerate-update-sources input').checked;
    const error = inDialog('.regenerate-error');
    if (!instructions && !updateSources) {
      error.textContent = '请填写纠正或修改要求，或明确选择采用更新后的来源。';
      error.hidden = false;
      return;
    }
    error.hidden = true;
    const brief = artifact.profile === 'continuation';
    if (brief && work.activeSessionId !== artifact.session_id) return;
    submittingCorrection = true;
    voiceArea.inert = true;
    let task;
    try {
      task = await client.request(`/api/artifacts/${encodeURIComponent(artifact.id)}/regenerate`, {
        method: 'POST', body: { instructions, expected_version: artifact.version, update_sources: updateSources },
      });
    } finally { submittingCorrection = false; voiceArea.inert = false; }
    if (!artifactOpenIntent || work.artifact !== artifact) {
      toast('重新生成任务已创建；你已离开原产物窗口，可在任务列表查看进度。');
      return;
    }
    if (brief) {
      correctionVoice.reset();
      correctionForm.hidden = true;
      const intent = { taskId: task.id, artifactId: artifact.id, parentVersion: artifact.version, sessionId: artifact.session_id };
      revisionIntent = intent; syncArtifactActions();
      feedback(`已提交基于 v${artifact.version} 的纠正，生成后将显示本次差异。可关闭此窗口，稍后在任务列表查看。`);
      void watchRevision(intent);
      return;
    }
    closeArtifactDialog();
    work.selected = task.id;
    await refreshTasks();
    toast(updateSources ? '已按纠正和更新后的来源开始生成新版本；完成后仍需重新批准保存。' : '已按纠正开始生成新版本；完成后仍需重新批准保存。');
  }));
  inDialog('.artifact-export').addEventListener('click', () => busy(inDialog('.artifact-export'), async () => {
    const artifact = work.artifact;
    try {
      const text = await client.request(`/api/artifacts/${encodeURIComponent(artifact.id)}/export`);
      const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }));
      const link = node('a');
      link.href = url; link.download = `${artifact.title.replace(/[<>:"/\\|?*]/g, '')}.md`; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } finally {
      resyncArtifactActionsAfterBusy();
    }
  }));
  inDialog('.artifact-delete').addEventListener('click', () => busy(inDialog('.artifact-delete'), async () => {
    const artifact = work.artifact;
    if (!await confirmAction('删除这份产物？', '会删除它的所有版本和引用记录。已经导出的文件不受影响。')) return;
    await client.request(`/api/artifacts/${encodeURIComponent(artifact.id)}`, { method: 'DELETE' });
    if (artifactOpenIntent && work.artifact === artifact) closeArtifactDialog();
    await refreshTasks();
    toast('产物已删除。');
  }));

  // ---- tool settings ------------------------------------------------------
  const warnings = {
    voice: ['开启语音确认？', '开启后，等待确认的写入和打开类操作可以用麦克风说“确认执行”批准。语音识别可能出错，身边的人或扬声器回声也可能触发确认。删除和命令类操作仍然只能点击确认。', '我了解风险，开启'],
    command: ['启用命令工具？', '命令工具从工作目录启动，但可以执行任意 PowerShell 命令，不受工作目录限制：可能修改或删除电脑上的任何文件、访问网络，而且无法撤销。每次执行前仍需要你点击确认；只在理解命令含义时批准。', '我了解风险，启用'],
  };
  for (const input of document.querySelectorAll('[data-warning]')) {
    input.addEventListener('change', async () => {
      if (!input.checked) return;
      input.checked = false;
      const [title, message, label] = warnings[input.dataset.warning];
      if (await confirmAction(title, message, label)) input.checked = true;
    });
  }

  function editor(container, input, { blank, fields, addLabel, empty }) {
    const read = () => { try { return JSON.parse(input.value || '[]'); } catch { return []; } };
    const store = rows => { input.value = JSON.stringify(rows); };
    const render = () => {
      const rows = read();
      container.replaceChildren();
      rows.forEach((row, index) => {
        const line = node('div', 'list-row');
        for (const spec of fields) {
          const control = spec.options ? node('select') : node('input');
          if (spec.options) for (const [value, label] of spec.options(row)) control.add(new Option(label, value));
          else { control.placeholder = spec.placeholder; control.maxLength = spec.max || 1024; }
          if (spec.className) control.className = spec.className;
          control.setAttribute('aria-label', spec.label);
          control.value = spec.get(row);
          if (spec.disable) for (const option of control.options || []) option.disabled = spec.disable(row, option.value);
          control.addEventListener('change', () => { const next = read(); next[index] = spec.set(next[index], control.value); store(next); if (spec.rerender) render(); });
          line.append(control);
        }
        line.append(action('移除', 'button subtle small', () => { const next = read(); next.splice(index, 1); store(next); render(); }));
        container.append(line);
      });
      if (!rows.length) container.append(node('small', 'field-hint', empty));
      container.append(action(addLabel, 'button ghost small', () => { store([...read(), blank()]); render(); }));
    };
    return render;
  }
  const noStandingAllow = tool => tool === '*' || work.tools.some(item => item.name === tool && !item.standing_allow);
  const editors = [
    editor($('#workspace-editor'), $('[name=workspace_dirs]'), {
      blank: () => '', addLabel: '添加目录', empty: '只使用默认工作目录。',
      fields: [{ label: '目录完整路径', placeholder: '例如 D:\\Documents\\会议', get: row => row, set: (_, value) => value.trim() }],
    }),
    editor($('#rule-editor'), $('[name=tool_rules]'), {
      blank: () => ({ tool: 'write_file', match: '', action: 'ask' }), addLabel: '添加规则', empty: '没有规则，所有工具按默认策略执行。',
      fields: [
        { label: '工具', options: () => [['*', '所有工具'], ...work.tools.map(tool => [tool.name, `${tool.title}（${tool.effect_label}）`])], get: row => row.tool, rerender: true,
          set: (row, value) => ({ ...row, tool: value, action: row.action === 'allow' && noStandingAllow(value) ? 'ask' : row.action }) },
        { label: '匹配目标', placeholder: '例如 notes/*.md，留空表示全部', max: 512, get: row => row.match, set: (row, value) => ({ ...row, match: value.trim() }) },
        { label: '动作', className: 'action-select', options: () => [['ask', '询问'], ['allow', '允许'], ['deny', '拒绝']], get: row => row.action,
          disable: (row, value) => value === 'allow' && noStandingAllow(row.tool), set: (row, value) => ({ ...row, action: value }) },
      ],
    }),
    editor($('#app-editor'), $('[name=app_launchers]'), {
      blank: () => ({ name: '', path: '' }), addLabel: '添加程序', empty: '没有登记程序，秘书不能启动任何程序。',
      fields: [
        { label: '名称', placeholder: '例如 记事本', max: 64, get: row => row.name, set: (row, value) => ({ ...row, name: value.trim() }) },
        { label: '程序完整路径', placeholder: '例如 C:\\Windows\\notepad.exe', get: row => row.path, set: (row, value) => ({ ...row, path: value.trim() }) },
      ],
    }),
  ];
  async function loadTools() {
    const result = await client.request('/api/tools');
    work.tools = result.tools;
    $('#workspace-path').textContent = result.workspace;
    editors.forEach(render => render());
  }
  $('#open-workspace').addEventListener('click', () => busy($('#open-workspace'), async () => {
    if (window.greatsage?.openWorkspace) await window.greatsage.openWorkspace();
    else toast(`工作目录：${$('#workspace-path').textContent}`);
  }));
  $('#approval-indicator').addEventListener('click', () => document.querySelector('[data-view=tasks]').click());

  // ---- events -------------------------------------------------------------
  let taskTimer;
  const scheduleTasks = () => { clearTimeout(taskTimer); taskTimer = setTimeout(() => refreshTasks().catch(() => {}), 300); };
  const visible = view => !document.querySelector(`#view-${view}`).hidden;
  return {
    openContinuation: continuation.open, cancelBriefPreview,
    init: () => Promise.all([refreshApprovals(), loadTools()]),
    fillSettings: () => editors.forEach(render => render()),
    setActiveSession(id) {
      if (work.activeSessionId !== (id || '')) {
        cancelBriefPreview();
        continuation.close();
        closeArtifactDialog();
      }
      work.activeSessionId = id || '';
      const activeOption = $('#task-session-filter option[value="active"]');
      activeOption.textContent = id ? `当前会话 ${String(id).slice(0, 8)}` : '当前会话';
      if (work.sessionFilter === 'active' && visible('tasks')) {
        work.selected = null;
        work.tasks = [];
        renderTaskList();
        refreshTasks().catch(error => toast(error.message, 'error'));
      }
    },
    setSessionFilter,
    async refresh(view) {
      if (view === 'tasks') { await Promise.all([refreshApprovals(), refreshMaterials()]); await refreshTasks(); }
      if (view === 'materials') await refreshMaterials();
    },
    onEvent(event) {
      const data = event.data || {};
      switch (event.kind) {
        case 'mcp_updated': loadTools().catch(() => {}); break;
        case 'task_updated': {
          const index = work.tasks.findIndex(task => task.id === data.id);
          const sessionId = filteredSessionId();
          if (sessionId && data.session_id !== sessionId) {
            if (index >= 0) work.tasks.splice(index, 1);
          } else if (index >= 0) work.tasks[index] = data;
          else work.tasks.unshift(data);
          if (visible('tasks')) { renderTaskList(); if (work.selected === data.id) scheduleTasks(); }
          break;
        }
        case 'task_finished': {
          const task = work.tasks.find(item => item.id === data.task_id);
          if (data.status === 'succeeded') toast(`「${task?.title || '任务'}」已完成。`);
          else if (data.status === 'failed') toast(`「${task?.title || '任务'}」没有完成：${data.error || '请查看任务详情。'}`, 'error', 9000);
          if (visible('tasks')) scheduleTasks();
          break;
        }
        case 'approval_requested':
          refreshApprovals().catch(() => {});
          toast(`有一项操作等你确认：${data.title}`, 'warning');
          break;
        case 'approval_resolved': case 'tool_call': case 'task_deleted':
          refreshApprovals().catch(() => {});
          if (visible('tasks')) scheduleTasks();
          break;
        case 'approval_hint': toast(data.message, 'warning'); break;
        case 'materials_updated': if (visible('materials')) refreshMaterials().catch(() => {}); break;
        case 'artifact_updated': if (visible('tasks')) scheduleTasks(); break;
      }
    },
  };
}
