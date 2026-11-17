// v0.3 work surface: materials, background tasks, approvals, artifacts and tool settings.
export function setupWork({ client, toast, busy, confirmAction }) {
  const $ = (selector, parent = document) => parent.querySelector(selector);
  const node = (tag, className, text) => { const el = document.createElement(tag); if (className) el.className = className; if (text !== undefined) el.textContent = text; return el; };
  const action = (label, className, handler) => { const el = node('button', className, label); el.type = 'button'; if (handler) el.addEventListener('click', () => busy(el, handler)); return el; };
  const work = { tasks: [], materials: [], approvals: [], tools: [], selected: null, artifact: null };
  const statusLabels = { queued: '排队中', running: '进行中', started: '进行中', waiting_approval: '等待确认', succeeded: '已完成', completed: '已完成', failed: '失败', cancelled: '已取消', interrupted: '已中断', pending: '未开始', unknown: '结果未知', expired: '已失效', denied: '已拒绝', approved: '已批准', proposed: '待决定', awaiting_approval: '等待确认', undone: '已撤销', active: '有效', stale: '来源已变更', missing: '原文件已移走', unreadable: '无法读取' };
  const tones = { succeeded: 'ok', completed: 'ok', active: 'ok', approved: 'ok', running: 'busy', started: 'busy', queued: 'busy', waiting_approval: 'warn', awaiting_approval: 'warn', interrupted: 'warn', unknown: 'warn', stale: 'warn', missing: 'warn', failed: 'bad', unreadable: 'bad' };
  const badge = status => node('span', `badge ${tones[status] || ''}`, statusLabels[status] || status);
  const when = value => value ? new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value)) : '';
  const kinds = { minutes: '纪要', todos: '待办', document: '文档草稿' };
  const origins = { user_text: '你的文字', user_voice: '你的语音', console: '控制台' };
  const taskKinds = { action: '操作', minutes: '纪要', research: '研究文档' };
  const active = status => ['queued', 'running', 'waiting_approval'].includes(status);
  const toolTitle = name => work.tools.find(tool => tool.name === name)?.title || name;

  const finite = value => typeof value === 'number' && Number.isFinite(value) ? value : 0;
  const compactNumber = value => new Intl.NumberFormat('zh-CN', { notation: value >= 10000 ? 'compact' : 'standard', maximumFractionDigits: 1 }).format(value);
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
  async function refreshTasks() {
    work.tasks = await client.request('/api/tasks');
    renderTaskList();
    if (work.selected) await showTask(work.selected);
  }

  function renderTaskList() {
    const list = $('#task-list');
    list.replaceChildren();
    if (!work.tasks.length) list.append(node('div', 'small-empty', '还没有任务。可以新建纪要，或让研究任务核对资料并生成文档。'));
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
      item.append(head, progress, node('small', '', `${taskKinds[task.kind] || task.kind} · ${origins[task.origin] || task.origin} · ${when(task.created_at)}${step}`));
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
      scopeItem('资料范围', materialsLabel, materialIds.length ? '仅使用任务启动时选定的资料快照' : ''),
      scopeItem('保存位置', savePath, params.overwrite ? '已允许覆盖；执行前仍需确认' : '工作目录内的 Markdown 文件'),
    );
    overview.append(node('h4', '', '研究范围'), scope);
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
    titleLine.append(node('h3', '', task.title), node('span', 'tag', taskKinds[task.kind] || task.kind));
    title.append(titleLine, node('small', '', `${origins[task.origin] || task.origin}发起 · ${when(task.created_at)}`));
    const actions = node('div', 'panel-tools');
    if (active(task.status)) actions.append(action('取消任务', 'button ghost small danger-hover', async () => { await client.request(`/api/tasks/${id}/cancel`, { method: 'POST' }); await refreshTasks(); }));
    if (['interrupted', 'failed', 'cancelled'].includes(task.status)) actions.append(action('继续', 'button ghost small', async () => { await client.request(`/api/tasks/${id}/resume`, { method: 'POST' }); await refreshTasks(); }));
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
        row.append(node('strong', '', artifact.title), node('span', 'tag', kinds[artifact.kind] || artifact.kind), badge(artifact.status),
                   node('small', '', `v${artifact.current_version}`), action('打开', 'button ghost small', () => openArtifact(artifact.id)));
        detail.append(row);
      }
    }
  }
  $('#refresh-tasks').addEventListener('click', () => busy($('#refresh-tasks'), () => Promise.all([refreshTasks(), refreshApprovals()])));

  // ---- approvals ----------------------------------------------------------
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
      card.append(head, node('code', '', call.target || '（无目标）'), node('pre', 'call-arguments', JSON.stringify(call.arguments, null, 2)));
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
    <div class="field"><span>资料</span><div class="material-picker"></div><small class="field-hint">研究代理只能读取这里选定的资料快照。本版最多使用 8 段来源、共 64,000 字符，单段最多 32,000；超出时会要求缩小范围，不会自动截断原文。</small></div>
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

  // ---- artifacts ----------------------------------------------------------
  const artifactDialog = node('dialog', 'record-dialog artifact-dialog');
  artifactDialog.innerHTML = `<div class="dialog-actions"><select class="artifact-version" aria-label="选择版本"></select><button type="button" class="button small ghost artifact-close">关闭</button></div>
    <div class="artifact-head"><h2></h2><span class="artifact-badges"></span></div>
    <p class="approval-warning artifact-stale" hidden>部分来源已删除或已变更，对应引用已失效。可以按修改意见重新生成，或核对后自己修改。</p>
    <div class="artifact-body"></div>
    <h3>来源</h3><div class="citation-list"></div>
    <div class="regenerate-form" hidden><label class="field"><span>修改意见</span><textarea rows="2" maxlength="4000" placeholder="例如：负责人写全名；删掉与发布无关的内容。"></textarea></label><div class="panel-tools"><button type="button" class="button primary small regenerate-start">开始重新生成</button><button type="button" class="button subtle small regenerate-cancel">取消</button></div></div>
    <div class="panel-tools artifact-actions"><button type="button" class="button primary small artifact-save">保存为新版本</button><button type="button" class="button ghost small artifact-regenerate">按修改意见重新生成</button><button type="button" class="button ghost small artifact-export">导出 .md</button><button type="button" class="button subtle small danger-hover artifact-delete">删除</button></div>`;
  document.body.append(artifactDialog);
  const inDialog = selector => $(selector, artifactDialog);
  let artifactRequestGeneration = 0;
  let artifactOpenIntent = false;

  function invalidateArtifactDialog() {
    artifactOpenIntent = false;
    artifactRequestGeneration += 1;
    inDialog('.artifact-body').replaceChildren();
    inDialog('.citation-list').replaceChildren();
  }
  function closeArtifactDialog() {
    invalidateArtifactDialog();
    if (artifactDialog.open) artifactDialog.close();
  }
  artifactDialog.addEventListener('cancel', invalidateArtifactDialog);
  artifactDialog.addEventListener('close', invalidateArtifactDialog);

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

  async function openArtifact(id, version, requestOpen = true) {
    if (requestOpen) artifactOpenIntent = true;
    else if (!artifactOpenIntent) return;
    const generation = ++artifactRequestGeneration;
    const artifact = await client.request(`/api/artifacts/${encodeURIComponent(id)}${version ? `?version=${version}` : ''}`);
    if (!artifactOpenIntent || generation !== artifactRequestGeneration) return;
    work.artifact = artifact;
    inDialog('h2').textContent = artifact.title;
    inDialog('.artifact-badges').replaceChildren(node('span', 'tag', kinds[artifact.kind] || artifact.kind), badge(artifact.status));
    inDialog('.artifact-version').replaceChildren(...artifact.versions.map(item => new Option(`v${item.version} · ${({ generated: '生成', edited: '编辑', regenerated: '重新生成' })[item.origin] || item.origin} · ${when(item.created_at)}`, item.version)));
    inDialog('.artifact-version').value = artifact.version;
    inDialog('.artifact-stale').hidden = artifact.status !== 'stale';
    const body = inDialog('.artifact-body');
    if (artifact.kind === 'todos') body.replaceChildren(todoTable(artifact));
    else {
      const editor = node('textarea', 'artifact-editor');
      editor.value = artifact.content; editor.rows = 18; editor.setAttribute('aria-label', 'Markdown 内容');
      body.replaceChildren(editor);
    }
    inDialog('.artifact-save').hidden = artifact.kind === 'todos';
    const citations = inDialog('.citation-list');
    citations.replaceChildren(...artifact.citations.map(item => {
      const row = node('div', `citation${item.valid ? '' : ' invalid'}`);
      row.append(node('strong', '', `[${item.marker}]`), node('span', '', item.label), node('small', '', item.valid ? item.excerpt || '' : '来源已删除或已变更'));
      return row;
    }), ...artifact.unknown_markers.map(marker => node('div', 'citation invalid', `[${marker}] 没有对应来源`)));
    if (!citations.children.length) citations.append(node('small', 'field-hint', '这份产物没有引用标记。'));
    inDialog('.regenerate-form').hidden = true;
    if (!artifactDialog.open) artifactDialog.showModal();
  }

  inDialog('.artifact-close').addEventListener('click', closeArtifactDialog);
  inDialog('.artifact-version').addEventListener('change', event => busy(event.target, () => openArtifact(work.artifact.id, Number(event.target.value), false)));
  inDialog('.artifact-save').addEventListener('click', () => busy(inDialog('.artifact-save'), async () => {
    const id = work.artifact.id;
    const content = inDialog('.artifact-editor').value;
    if (!content.trim()) throw new Error('内容不能为空。');
    await client.request(`/api/artifacts/${encodeURIComponent(id)}`, { method: 'PUT', body: { content } });
    await openArtifact(id, undefined, false);
    toast('已保存为新版本，引用仍指向原来的来源。');
  }));
  inDialog('.artifact-regenerate').addEventListener('click', () => { inDialog('.regenerate-form').hidden = false; inDialog('.regenerate-form textarea').focus(); });
  inDialog('.regenerate-cancel').addEventListener('click', () => { inDialog('.regenerate-form').hidden = true; });
  inDialog('.regenerate-start').addEventListener('click', () => busy(inDialog('.regenerate-start'), async () => {
    if (work.artifact.kind === 'todos' && !await confirmAction('重新生成待办清单？', '新清单会替换当前的待办，你手动修改过的条目会被覆盖；旧清单保留在版本记录中。', '重新生成')) return;
    const task = await client.request(`/api/artifacts/${encodeURIComponent(work.artifact.id)}/regenerate`, { method: 'POST', body: { instructions: inDialog('.regenerate-form textarea').value.trim() } });
    closeArtifactDialog();
    work.selected = task.id;
    await refreshTasks();
    toast('已开始重新生成，完成后会成为新的版本。');
  }));
  inDialog('.artifact-export').addEventListener('click', () => busy(inDialog('.artifact-export'), async () => {
    const text = await client.request(`/api/artifacts/${encodeURIComponent(work.artifact.id)}/export`);
    const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }));
    const link = node('a');
    link.href = url; link.download = `${work.artifact.title.replace(/[<>:"/\\|?*]/g, '')}.md`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }));
  inDialog('.artifact-delete').addEventListener('click', () => busy(inDialog('.artifact-delete'), async () => {
    if (!await confirmAction('删除这份产物？', '会删除它的所有版本和引用记录。已经导出的文件不受影响。')) return;
    await client.request(`/api/artifacts/${encodeURIComponent(work.artifact.id)}`, { method: 'DELETE' });
    closeArtifactDialog();
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
    init: () => Promise.all([refreshApprovals(), loadTools()]),
    fillSettings: () => editors.forEach(render => render()),
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
          if (index >= 0) work.tasks[index] = data; else work.tasks.unshift(data);
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
