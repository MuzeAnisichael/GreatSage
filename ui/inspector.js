import { describeVoiceSource } from './voice.js';

export function setupInspector({ client, busy, toast, voiceHealth }) {
  const nav = document.createElement('button'); nav.className = 'nav-item'; nav.dataset.view = 'inspector';
  nav.innerHTML = '<svg class="icon"><use href="#i-logs"/></svg><span>运行监控</span>';
  document.querySelector('[data-view=logs]').before(nav);
  const page = document.createElement('section'); page.className = 'view'; page.id = 'view-inspector'; page.hidden = true; page.setAttribute('aria-label', '运行监控');
  page.innerHTML = '<div class="view-header"><div><h2>秘书正在做什么</h2><p class="source-note">查看任务节点、实际请求的消息目录、上下文估算和记忆变更。</p></div><button class="button ghost small" id="inspector-refresh">刷新</button></div><p id="inspector-stamp" class="source-note" role="status">正在读取…</p><div class="stat-strip" id="inspector-stats"></div><div class="inspector-grid"><section class="panel"><div class="panel-heading"><h2>任务运行</h2></div><div class="panel-body" id="inspector-tasks"></div></section><section class="panel"><div class="panel-heading"><h2>长期记忆</h2><button class="button ghost small" id="inspector-memory-open">查看与管理</button></div><div class="panel-body" id="inspector-memory"></div></section></div><section class="panel inspector-context"><div class="panel-heading"><h2>模型请求与上下文</h2><button class="button ghost small" id="inspector-audit-open">请求证据</button></div><div class="panel-body"><label class="field"><span>最近请求</span><select id="inspector-request"></select></label><div id="inspector-budget"></div><div class="inspector-table-wrap"><table class="inspector-table"><thead><tr><th>顺序</th><th>角色</th><th>消息字节</th><th>内容指纹</th></tr></thead><tbody id="inspector-messages"></tbody></table></div><p class="source-note" id="inspector-sources"></p><p class="source-note">此页始终展示元数据。事先开启正文归档的请求，可在“请求证据”中单独查看正文。候选、实体关系与原文证据请在长期记忆页查看；多 Agent 和远端追踪尚未接入。</p></div></section>';
  const voicePanel = document.createElement('section'); voicePanel.className = 'panel inspector-context';
  voicePanel.innerHTML = '<div class="panel-heading"><h2>语音管线与播放反馈</h2></div><div class="panel-body"><p class="source-note" id="inspector-voice-state"></p><div class="inspector-table-wrap"><table class="inspector-table"><thead><tr><th>音源</th><th>状态</th><th>识别方式</th><th>排队音频帧</th><th>失败原因</th><th>处理</th></tr></thead><tbody id="inspector-voice-sources"></tbody></table></div><div class="stat-strip" id="inspector-voice-metrics"></div><p class="source-note">音频就绪记录合成完成；播放和停止由客户端回执确认，回执上界包含回传耗时。客户端起播记录收到音频事件到 playing 事件的间隔。这些数值均不是声卡实际发声测量。</p><p class="source-note" id="inspector-voice-window"></p></div>';
  page.querySelector('.inspector-grid').after(voicePanel);
  document.querySelector('main').append(page);
  const $ = selector => page.querySelector(selector);
  const element = (tag, text, className) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = String(text); if (className) node.className = className; return node; };
  const status = { idle: '待命', listening: '聆听中', thinking: '思考中', responding: '回答中', speaking: '播报中', queued: '排队中', running: '进行中', waiting_approval: '等待确认', succeeded: '完成', failed: '失败', cancelled: '已取消', interrupted: '中断' };
  const nodeNames = { collect: '收集来源', extract: '抽取要点', compose: '生成纪要', document: '撰写文档', tools: '工具执行／审批', model: '模型回应', reply: '保存回复' };
  const contentLabels = { saved: '正文已归档', disabled: '仅元数据', source_deleted: '来源已删除', size_limit: '正文超出归档上限' };
  let data, epoch = 0, selected = '', timer;
  function renderVoice() {
    const voice = data.voice || {}, sources = voiceHealth?.snapshot?.sources || voice.sources || [];
    $('#inspector-voice-state').textContent = sources.length
      ? '各音源分别处理；重试只接收新音频，不重放未完成的旧语音。采集停止时，请先检查设备或程序后重新开启聆听。'
      : data.listening ? '等待所选音源的音频。' : '尚未聆听，开启后显示各音源的管线状态。';
    $('#inspector-voice-sources').replaceChildren();
    for (const source of sources) {
      const row = element('tr');
      const detail = describeVoiceSource(source);
      const mode = source.streaming === true ? '实时流式' : source.streaming === false ? '分段识别' : '待恢复';
      const pending = Number.isInteger(source.pending_frames) && source.pending_frames >= 0 ? source.pending_frames.toLocaleString() : '待测';
      const phase = element('td', detail.state);
      if (source.streaming === true) phase.append(element('small', detail.pending === null ? '待确认语音：待测' : `待确认语音：${detail.pending} 段`, 'voice-turn-count'));
      row.append(element('td', detail.label), phase, element('td', mode), element('td', pending));
      const reason = element('td', detail.reason || '—');
      if (source.error_code) reason.append(element('small', source.error_code, 'voice-error-code'));
      const action = element('td'); const button = voiceHealth?.actionButton(source); if (button) action.append(button);
      row.append(reason, action);
      $('#inspector-voice-sources').append(row);
    }
    $('#inspector-voice-metrics').replaceChildren();
    const measurements = [
      ['audio_ready_ms', '音频就绪', '语音结束 → 合成完成'],
      ['first_audio_ms', '首段播放确认', '语音结束 → 起播回执上界'],
      ['client_event_to_playing_ms', '客户端起播', '收到音频事件 → playing'],
      ['interrupt_to_stop_ack_ms', '打断停止确认', '打断 → 停止回执上界'],
    ];
    for (const [name, label, description] of measurements) {
      const sample = voice.metrics?.[name], value = sample?.value_ms;
      const measured = typeof value === 'number' && Number.isFinite(value) && value >= 0;
      const note = measured && sample.created_at ? ` · ${new Date(sample.created_at).toLocaleTimeString()}` : '';
      const emptyStop = sample?.had_audio === false ? ' · 当时无播放' : '';
      const stat = element('div', undefined, 'stat');
      stat.append(element('span', label), element('strong', measured ? `${value.toLocaleString()} ms` : '待测'),
        element('small', `${description}${note}${emptyStop}`));
      $('#inspector-voice-metrics').append(stat);
    }
    $('#inspector-voice-window').textContent = `每项独立显示最近 ${voice.recent_event_limit || 80} 条日志内的最新有效样本，可能来自不同回答。“待测”表示此窗口没有记录，不等于零延迟。此页只展示状态与耗时。`;
  }
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
    renderVoice();
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
    const extraction = data.entity_memory?.extraction;
    if (extraction) $('#inspector-memory').append(element('p', `${extraction.enabled ? extraction.background?.running ? '正在提取' : '提取已启用' : '提取已关闭'} · ${extraction.pending} 条提取候选 · ${extraction.active} 条有效提取 · ${data.entity_memory.entities} 个实体`, 'source-note'));
    const labels = { update: '更新记忆', revise: '修正记忆', delete: '删除来源', clear: '清空历史', resolve_conflict: '处理冲突', extraction: '提取记忆', extract: '提取记忆', resolve_candidate: '处理记忆候选', accept: '确认记忆候选', reject: '拒绝记忆候选' };
    for (const change of memory.changes) $('#inspector-memory').append(element('p', `${new Date(change.created_at).toLocaleTimeString()} · ${change.kind === 'compression_done' ? '生成摘要' : labels[change.action] || '更新记忆'}`, 'source-note'));
    if (!memory.changes.length) $('#inspector-memory').append(element('p', '尚无记忆变更记录。', 'source-note'));
    if (!data.requests.some(row => row.id === selected)) selected = data.requests[0]?.id || '';
    $('#inspector-request').replaceChildren(...data.requests.map(row => { const option = element('option', `${row.purpose} · ${row.metadata.model || '未记录模型'} · ${new Date(row.created_at).toLocaleTimeString()}`); option.value = row.id; return option; }));
    $('#inspector-request').value = selected; renderRequest();
  }
  async function refresh() {
    const request = ++epoch;
    const [result] = await Promise.all([client.request('/api/inspector'), voiceHealth?.refresh()]);
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
  voiceHealth?.subscribe(() => { if (data) renderVoice(); });
  return { refresh, onEvent(event) {
    if (page.hidden || !['state', 'agent_node', 'task_updated', 'task_finished', 'request_snapshot', 'response_done', 'memory_updated', 'memory_extraction', 'compression_done', 'session', 'stream_reset', 'mcp_updated', 'metrics', 'audio_source', 'audio_gap', 'audio_recovery', 'voice_turn', 'listening', 'error'].includes(event.kind)) return;
    // Invalidate an older in-flight response immediately, before the debounce.
    epoch++; clearTimeout(timer); timer = setTimeout(() => refresh().catch(error => toast(error.message, 'error')), 250);
  } };
}
