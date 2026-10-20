export function setupInspector({ client, busy, toast }) {
  const nav = document.createElement('button'); nav.className = 'nav-item'; nav.dataset.view = 'inspector';
  nav.innerHTML = '<svg class="icon"><use href="#i-logs"/></svg><span>运行监控</span>';
  document.querySelector('[data-view=logs]').before(nav);
  const page = document.createElement('section'); page.className = 'view'; page.id = 'view-inspector'; page.hidden = true; page.setAttribute('aria-label', '运行监控');
  page.innerHTML = '<div class="view-header"><div><h2>秘书正在做什么</h2><p class="source-note">查看任务节点、实际请求的消息目录、上下文估算和记忆变更。</p></div><button class="button ghost small" id="inspector-refresh">刷新</button></div><p id="inspector-stamp" class="source-note" role="status">正在读取…</p><div class="stat-strip" id="inspector-stats"></div><div class="inspector-grid"><section class="panel"><div class="panel-heading"><h2>任务运行</h2></div><div class="panel-body" id="inspector-tasks"></div></section><section class="panel"><div class="panel-heading"><h2>长期记忆</h2><button class="button ghost small" id="inspector-memory-open">查看与管理</button></div><div class="panel-body" id="inspector-memory"></div></section></div><section class="panel inspector-context"><div class="panel-heading"><h2>模型请求与上下文</h2><button class="button ghost small" id="inspector-audit-open">请求证据</button></div><div class="panel-body"><label class="field"><span>最近请求</span><select id="inspector-request"></select></label><div id="inspector-budget"></div><div class="inspector-table-wrap"><table class="inspector-table"><thead><tr><th>顺序</th><th>角色</th><th>消息字节</th><th>内容指纹</th></tr></thead><tbody id="inspector-messages"></tbody></table></div><p class="source-note" id="inspector-sources"></p><p class="source-note">此页始终展示元数据。事先开启正文归档的请求，可在“请求证据”中单独查看正文。实体记忆、多 Agent 和远端追踪尚未接入。</p></div></section>';
  document.querySelector('main').append(page);
  const $ = selector => page.querySelector(selector);
  const element = (tag, text, className) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = String(text); if (className) node.className = className; return node; };
  const status = { idle: '待命', listening: '聆听中', thinking: '思考中', responding: '回答中', speaking: '播报中', queued: '排队中', running: '进行中', waiting_approval: '等待确认', succeeded: '完成', failed: '失败', cancelled: '已取消', interrupted: '中断' };
  const nodeNames = { collect: '收集来源', extract: '抽取要点', compose: '生成纪要', document: '撰写文档', tools: '工具执行／审批', model: '模型回应', reply: '保存回复' };
  const contentLabels = { saved: '正文已归档', disabled: '仅元数据', source_deleted: '来源已删除', size_limit: '正文超出归档上限' };
  let data, epoch = 0, selected = '', timer;
  function renderRequest() {
    const request = data?.requests.find(row => row.id === selected);
    $('#inspector-messages').replaceChildren(); $('#inspector-budget').replaceChildren();
    $('#inspector-audit-open').disabled = !request;
    if (!request) { $('#inspector-budget').textContent = '还没有模型请求。发起对话或任务后，这里显示实际请求的消息目录。'; $('#inspector-sources').textContent = ''; return; }
    const meta = request.metadata, budget = meta.context;
    if (budget) {
      const used = budget.input_estimated + budget.tools_estimated + budget.schema_estimated + budget.protocol_reserved;
      const meter = element('progress'); meter.max = budget.window || Math.max(1, used); meter.value = Math.min(used, meter.max);
      const method = budget.method === 'utf8_upper_bound' ? 'UTF-8 保守上界' : 'tokenizer 估算';
      $('#inspector-budget').append(element('p', `${method}：输入及工具约 ${used.toLocaleString()}，上下文窗口 ${budget.window?.toLocaleString() || '未记录'}，输出预留 ${budget.output_reserved ?? '未记录'}。`));
      if (budget.window) $('#inspector-budget').append(meter);
    } else $('#inspector-budget').textContent = '这是一条迁移前的请求，没有记录上下文估算。';
    for (const message of meta.message_manifest || []) {
      const row = element('tr');
      for (const value of [message.index + 1, message.role, message.bytes.toLocaleString(), message.sha256.slice(0, 16)]) row.append(element('td', value));
      $('#inspector-messages').append(row);
    }
    const counts = {};
    for (const source of meta.source_manifest || []) counts[source.kind] = (counts[source.kind] || 0) + 1;
    const sources = meta.source_manifest ? `引用 ${counts.memory || 0} 条长期记忆、${counts.summary || 0} 条摘要、${counts.message || 0} 条原文、${counts.chunk || 0} 段资料。` : '旧请求未记录来源分类。';
    $('#inspector-sources').textContent = `${meta.message_count ?? '未记录'} 条消息 · ${contentLabels[meta.content_status] || meta.content_status} · ${sources}`;
  }
  function render() {
    $('#inspector-stamp').textContent = `更新于 ${new Date(data.at).toLocaleTimeString()} · 会话 ${data.session_id.slice(0, 8)} · 仅本机监控`;
    $('#inspector-stats').replaceChildren();
    const running = data.active_tasks;
    for (const [label, value, note] of [['当前状态', status[data.state] || data.state, data.listening ? '正在监听所选音源' : '未监听音源'], ['模型', data.model.name, data.model.provider], ['任务', running, 'LangGraph 编排'], ['外部工具', data.mcp_tools, '本次已发现的 MCP 工具']]) {
      const stat = element('div', undefined, 'stat'); stat.append(element('span', label), element('strong', value), element('small', note)); $('#inspector-stats').append(stat);
    }
    $('#inspector-tasks').replaceChildren();
    if (!data.tasks.length) $('#inspector-tasks').textContent = '尚无任务。简单对话直接流式回答，多步骤任务会在这里显示。';
    for (const task of data.tasks) {
      const row = element('article', undefined, 'inspector-run');
      const next = task.graph.next.map(name => nodeNames[name] || name).join(' → ');
      const node = nodeNames[task.graph.node] || task.graph.node || (task.status === 'succeeded' ? '已结束' : next || '等待运行');
      row.append(element('strong', task.title), element('p', `${status[task.status] || task.status} · ${node}${next ? ` · 继续位置：${next}` : ''}`), element('small', task.graph.checkpoint_id ? `已保存控制状态 · ${task.id.slice(0, 8)}` : '尚无 checkpoint'));
      $('#inspector-tasks').append(row);
    }
    const memory = data.memory;
    $('#inspector-memory').replaceChildren(element('p', `${memory.active_memories} 条事实／偏好 · ${memory.summaries} 条摘要`), element('p', `${memory.history_messages} 条跨会话原文 · ${memory.conflicts} 项待处理冲突`));
    const labels = { update: '更新记忆', revise: '修正记忆', delete: '删除来源', clear: '清空历史', resolve_conflict: '处理冲突' };
    for (const change of memory.changes) $('#inspector-memory').append(element('p', `${new Date(change.created_at).toLocaleTimeString()} · ${change.kind === 'compression_done' ? '生成摘要' : labels[change.action]}`, 'source-note'));
    if (!memory.changes.length) $('#inspector-memory').append(element('p', '尚无记忆变更记录。', 'source-note'));
    if (!data.requests.some(row => row.id === selected)) selected = data.requests[0]?.id || '';
    $('#inspector-request').replaceChildren(...data.requests.map(row => { const option = element('option', `${row.purpose} · ${row.metadata.model || '未记录模型'} · ${new Date(row.created_at).toLocaleTimeString()}`); option.value = row.id; return option; }));
    $('#inspector-request').value = selected; renderRequest();
  }
  async function refresh() {
    const request = ++epoch;
    const result = await client.request('/api/inspector');
    if (request !== epoch) return;
    data = result; render();
  }
  $('#inspector-request').onchange = () => { selected = $('#inspector-request').value; renderRequest(); };
  $('#inspector-refresh').onclick = () => busy($('#inspector-refresh'), refresh);
  $('#inspector-memory-open').onclick = () => document.querySelector('[data-view=memory]').click();
  $('#inspector-audit-open').onclick = () => {
    const request = data?.requests.find(row => row.id === selected); if (!request) return;
    document.querySelector('[data-view=logs]').click();
    document.querySelector('#audit-trace').value = request.trace_id;
    document.querySelector('#audit-refresh').click();
  };
  return { refresh, onEvent(event) {
    if (page.hidden || !['state', 'agent_node', 'task_updated', 'task_finished', 'request_snapshot', 'response_done', 'memory_updated', 'compression_done', 'session', 'stream_reset', 'mcp_updated'].includes(event.kind)) return;
    // Invalidate an older in-flight response immediately, before the debounce.
    epoch++; clearTimeout(timer); timer = setTimeout(() => refresh().catch(error => toast(error.message, 'error')), 250);
  } };
}
