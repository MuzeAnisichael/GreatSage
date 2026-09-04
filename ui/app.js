import { SageClient, stateLabels, sourceLabel, timeLabel, safeString, redact } from './client.js';

const $ = (selector, parent = document) => parent.querySelector(selector);
const $$ = (selector, parent = document) => [...parent.querySelectorAll(selector)];
const client = new SageClient();
const state = { settings: null, status: {}, history: [], memories: [], skills: [], events: [], seenEvents: new Set(), streams: new Map(), sources: {}, listening: false, connected: false, initialized: false, resyncing: false, streamGap: false, pendingEvents: [] };
const modeLabels = { conversation: '对话伙伴', listen: '旁听助手', proactive: '主动秘书' };
const modeDescriptions = { conversation: '听你说完，再作回应', listen: '持续旁听，提问时回应', proactive: '根据指令主动解释与提醒' };
const languageLabels = { 'zh-CN': '简体中文', zh: '中文', en: 'English', 'en-US': 'English', ja: '日本語' };
let memoryRefreshTimer;
let eventRenderTimer;

function element(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = safeString(content);
  return node;
}

function toast(message, type = '', duration = 5000) {
  const item = element('div', `toast ${type}`, redact(safeString(message)));
  $('#toasts').append(item);
  while ($('#toasts').children.length > 4) $('#toasts').firstElementChild.remove();
  setTimeout(() => { item.classList.add('fade-out'); setTimeout(() => item.remove(), 300); }, duration);
}

async function busy(button, operation) {
  const disabled = button.disabled;
  button.disabled = true;
  try { return await operation(); }
  catch (error) { toast(error.message || '操作失败，请查看运行日志。', 'error', 8000); return undefined; }
  finally { button.disabled = disabled; }
}

function showView(view) {
  if (!$('#view-' + view)) return;
  $$('.view').forEach(node => { node.hidden = node.id !== 'view-' + view; node.classList.toggle('active', !node.hidden); });
  $$('.nav-item').forEach(node => node.classList.toggle('active', node.dataset.view === view));
  $('#page-breadcrumb').textContent = ({ conversation: '对话与感知', memory: '长期记忆', skills: '技能库', logs: '运行日志', settings: '设置' })[view];
  if (view === 'memory') refreshMemory().catch(error => toast(error.message, 'error'));
  if (view === 'skills') refreshSkills().catch(error => toast(error.message, 'error'));
  if (view === 'logs') renderEvents();
}

function showConnection(stateName, message) {
  state.connected = stateName === 'connected';
  $('#connection-dot').className = `connection-dot ${stateName}`;
  $('#connection-label').textContent = ({ connected: '本地服务已连接', connecting: '正在连接', error: '连接已断开' })[stateName];
  if (stateName === 'connected') $('#connection-banner').hidden = true;
  else if (message) {
    $('#connection-banner').hidden = false;
    $('#connection-error').textContent = redact(message);
  }
  if (!state.connected) updateState('offline');
}

function updateState(name, listening) {
  if (listening !== undefined) state.listening = Boolean(listening);
  if (name) state.status.state = name;
  const current = state.status.state || 'idle';
  const pill = $('#state-pill');
  pill.className = `status-pill ${current === 'transcribing' ? 'listening' : current === 'responding' ? 'thinking' : current}`;
  $('span', pill).textContent = current === 'offline' ? '未连接' : stateLabels[current] || current;
  $('#capture-tag').textContent = state.listening ? '监听中' : '未监听';
  $('#toggle-listening').textContent = state.listening ? '■ 暂停聆听' : '▶ 开始聆听';
  $('#transcript-wave').classList.toggle('active', state.listening);
  for (const selector of ['#quick-microphone', '#quick-desktop-source', '#quick-process']) $(selector).disabled = state.listening;
  $('#quick-microphone-device').disabled = state.listening || !$('#quick-microphone').checked;
}

function getPath(object, path) { return path.split('.').reduce((current, key) => current?.[key], object); }
function setPath(object, path, value) { const parts = path.split('.'); const leaf = parts.pop(); let parent = object; for (const part of parts) parent = parent[part] ||= {}; parent[leaf] = value; }

function fillSettings(settings) {
  state.settings = settings;
  for (const input of $$('#settings-form [name]')) {
    if (input.name.endsWith('.api_key')) { input.value = ''; input.placeholder = getPath(settings, input.name.split('.')[0] + '.key_configured') ? '密钥已配置，留空保留' : '可填写密钥，或使用环境变量'; continue; }
    const value = getPath(settings, input.name);
    if (input.type === 'checkbox') input.checked = Boolean(value);
    else if (value !== undefined && value !== null) input.value = value;
  }
  updateQuickSettings();
  refreshProviderControls();
  if (settings.tts?.provider === 'system') refreshVoiceList().catch(error => { $('#voice-catalog-note').textContent = error.message; });
}

function updateQuickSettings() {
  const settings = state.settings;
  if (!settings) return;
  $('#quick-mode').value = settings.mode || 'conversation';
  $('#quick-microphone').checked = Boolean(settings.microphone);
  $('#quick-desktop-source').value = settings.desktop_source || 'none';
  ensureOption($('#quick-microphone-device'), settings.microphone_device, '已保存的设备');
  $('#quick-microphone-device').value = settings.microphone_device ?? '';
  ensureOption($('#quick-process'), settings.desktop_process_id, `已保存的程序 (${settings.desktop_process_id})`);
  $('#quick-process').value = settings.desktop_process_id ?? '';
  $('#process-field').hidden = settings.desktop_source !== 'process';
  $('#quick-microphone-device').disabled = state.listening || !settings.microphone;
  $('#mode-stat').textContent = modeLabels[settings.mode] || settings.mode;
  $('#mode-description').textContent = modeDescriptions[settings.mode] || '由全局指令控制';
  $('#model-stat').textContent = settings.llm?.model || '未配置模型';
  $('#provider-stat').textContent = ({ openrouter: 'OpenRouter · 云端 API', ollama: 'Ollama · 本地推理', openai: 'OpenAI 兼容 API' })[settings.llm?.provider] || settings.llm?.provider || '等待配置';
  $('#voice-stat').textContent = settings.voice_enabled ? '文字 + 语音' : '仅文字';
  $('#language-stat').textContent = languageLabels[settings.output_language] || settings.output_language || '—';
  $('#source-note').textContent = state.sources.errors?.length ? state.sources.errors.join('；') : settings.record_audio ? '已开启本地录音保存，可在设置中关闭。' : '音频来源分别标记，原始录音默认关闭。';
}

function ensureOption(select, value, label) {
  if (value === null || value === undefined || value === '') return;
  if (![...select.options].some(option => option.value === String(value))) select.add(new Option(label, value));
}

async function saveSettings() {
  if (!state.settings) throw new Error('配置尚未读取，请先连接本地服务。');
  if (!$('#settings-form').reportValidity()) return;
  const result = structuredClone(state.settings);
  for (const input of $$('#settings-form [name]')) {
    if (input.name.endsWith('.api_key') && !input.value.trim()) continue;
    const value = input.type === 'checkbox' ? input.checked : input.type === 'number' ? Number(input.value) : input.value;
    setPath(result, input.name, value);
  }
  await client.request('/api/settings', { method: 'PUT', body: result });
  fillSettings(await client.request('/api/settings'));
  toast('设置已保存。新的请求将使用当前配置。');
}

function setupProviderExtras() {
  const localAsr = element('div', 'local-provider-tools');
  localAsr.id = 'local-asr-tools';
  const prepare = element('button', 'button ghost full', '↓ 准备本地模型');
  prepare.id = 'prepare-asr'; prepare.type = 'button';
  const note = element('p', 'source-note', '先保存本地识别配置，再手动准备模型。首次准备可能下载模型文件；开启监听不会自动下载。');
  localAsr.append(prepare, note);
  $('#test-asr').before(localAsr);
  prepare.addEventListener('click', () => busy(prepare, async () => {
    const output = $('#test-asr');
    output.className = 'provider-result';
    if (state.settings?.asr?.provider !== 'faster_whisper') throw new Error('请先保存 faster-whisper 本地语音识别配置。');
    output.textContent = '正在准备本地模型。首次下载需要一些时间，请保持应用运行…';
    try { const result = await client.request('/api/providers/warmup', { method: 'POST', body: {} }); output.textContent = `模型已就绪：${result.model || state.settings.asr.model}。可以开始聆听。`; }
    catch (error) { output.classList.add('error'); output.textContent = redact(error.message); }
  }));
  const field = element('label', 'field'); field.id = 'system-voice-field';
  const select = element('select'); select.id = 'system-voice-select'; select.setAttribute('aria-label', '已安装的系统音色'); select.add(new Option('按语音语言自动选择', ''));
  field.append(element('span', '', '已安装的系统音色'), select);
  const voiceNote = element('small', '', '选择系统语音并保存设置后，可以读取已安装的音色。'); voiceNote.id = 'voice-catalog-note'; field.append(voiceNote);
  const refresh = element('button', 'button small subtle', '↻ 刷新系统音色'); refresh.id = 'refresh-voices'; refresh.type = 'button';
  const wrapper = element('div', 'system-voice-tools'); wrapper.id = 'system-voice-tools'; wrapper.append(field, refresh);
  $('[name="tts.voice"]').closest('label').after(wrapper);
  select.addEventListener('change', () => { $('[name="tts.voice"]').value = select.value; renderVoiceOptions(); });
  refresh.addEventListener('click', () => busy(refresh, refreshVoiceList));
  for (const component of ['asr', 'tts']) $(`[name="${component}.provider"]`).addEventListener('change', () => {
    if (component === 'tts' && $('[name="tts.provider"]').value === 'system' && state.settings?.tts?.provider !== 'system') $('[name="tts.voice"]').value = '';
    refreshProviderControls();
  });
  $('[name="voice_language"]').addEventListener('change', renderVoiceOptions);
}

function refreshProviderControls() {
  const localAsr = $('[name="asr.provider"]').value === 'faster_whisper';
  $('#local-asr-tools').hidden = !localAsr;
  for (const name of ['base_url', 'api_key_env', 'api_key']) $(`[name="asr.${name}"]`).closest('label').hidden = localAsr;
  const system = $('[name="tts.provider"]').value === 'system';
  $('#system-voice-tools').hidden = !system;
  for (const name of ['base_url', 'model', 'api_key_env', 'api_key', 'voice']) $(`[name="tts.${name}"]`).closest('label').hidden = system;
  if (system) renderVoiceOptions();
}

async function refreshVoiceList() {
  if (state.settings?.tts?.provider !== 'system') throw new Error('请先保存“本地系统语音”配置，再刷新音色。');
  const result = await client.request('/api/voices');
  state.voices = Array.isArray(result) ? result : result.voices || [];
  renderVoiceOptions();
}

function renderVoiceOptions() {
  if (!$('#system-voice-select')) return;
  const selected = $('[name="tts.voice"]').value;
  const language = $('[name="voice_language"]').value.split('-')[0].toLowerCase();
  const voices = state.voices || [];
  const supportsLanguage = voice => (voice.languages || [voice.language || '']).some(item => String(item).split('-')[0].toLowerCase() === language);
  const select = $('#system-voice-select'); select.replaceChildren(new Option('按语音语言自动选择', ''));
  for (const voice of voices) {
    const languages = voice.languages?.length ? voice.languages.join(' / ') : voice.language || '未知语言';
    const option = new Option(`${voice.name} · ${languages}${supportsLanguage(voice) ? '' : '（不匹配当前语言）'}`, voice.id);
    option.disabled = !supportsLanguage(voice); select.add(option);
  }
  ensureOption(select, selected, '已配置音色（等待验证）'); select.value = selected;
  const matches = voices.filter(supportsLanguage);
  $('#voice-catalog-note').textContent = !voices.length ? '尚未读取到系统音色，请保存配置后刷新。' : matches.length ? `有 ${matches.length} 个已安装音色支持当前语音语言。` : '没有匹配当前语音语言的系统音色。请安装对应语言音色，或改用云端语音服务。';
}

async function saveQuickSettings() {
  if (!state.settings) throw new Error('配置尚未读取，请先连接本地服务。');
  const microphoneId = $('#quick-microphone-device').value;
  const microphone = state.sources.microphones?.find(item => String(item.id) === microphoneId);
  const settings = { ...state.settings, mode: $('#quick-mode').value, microphone: $('#quick-microphone').checked, microphone_device: microphone?.id ?? (microphoneId || null), desktop_source: $('#quick-desktop-source').value, desktop_process_id: $('#quick-process').value ? Number($('#quick-process').value) : null };
  if (settings.mode !== state.settings.mode) settings.allow_proactive = settings.mode === 'proactive';
  if (settings.desktop_source === 'process' && !settings.desktop_process_id) throw new Error('请先选择需要监听的程序。');
  await client.request('/api/settings', { method: 'PUT', body: settings });
  state.settings = settings;
  // Keep unsaved advanced form fields intact; only synchronize controls changed here.
  for (const key of ['mode', 'allow_proactive']) { const input = $(`[name="${key}"]`); if (input.type === 'checkbox') input.checked = settings[key]; else input.value = settings[key]; }
  updateQuickSettings();
}

async function refreshSources() {
  state.sources = await client.request('/api/audio/sources');
  const microphone = $('#quick-microphone-device');
  const process = $('#quick-process');
  microphone.replaceChildren(new Option('系统默认设备', ''));
  process.replaceChildren(new Option('请选择程序', ''));
  for (const source of state.sources.microphones || []) microphone.add(new Option(source.name, source.id));
  for (const source of state.sources.processes || []) process.add(new Option(`${source.name} · PID ${source.id}`, source.id));
  const systemOption = $('#quick-desktop-source option[value="system"]');
  const processOption = $('#quick-desktop-source option[value="process"]');
  systemOption.disabled = state.sources.system_available === false;
  processOption.disabled = state.sources.process_available === false;
  systemOption.textContent = systemOption.disabled ? '系统整体声音（当前不可用）' : '系统整体声音';
  processOption.textContent = processOption.disabled ? '指定程序声音（当前不可用）' : '指定程序声音';
  updateQuickSettings();
}

function chatScroll(force = false) {
  const messages = $('#chat-messages');
  if (force || messages.scrollHeight - messages.scrollTop - messages.clientHeight < 180) messages.scrollTop = messages.scrollHeight;
}

function messageNode(message) {
  $('#chat-empty')?.remove();
  const node = element('article', `chat-message ${message.role === 'user' ? 'user' : 'assistant'}`);
  node.dataset.id = message.id || '';
  const avatar = element('div', 'chat-avatar', message.role === 'user' ? '你' : '✦');
  const body = element('div', 'chat-message-body');
  const meta = element('div', 'chat-meta');
  meta.append(element('strong', '', message.role === 'user' ? '你' : 'GreatSage'), element('time', '', timeLabel(message.created_at)));
  if (message.source) meta.append(element('span', 'source', sourceLabel(message.source)));
  body.append(meta, element('div', 'chat-text', message.text));
  node.append(avatar, body);
  if (message.trace_id) attachTrace(node, message.trace_id);
  $('#chat-messages').append(node);
  $('#message-count').textContent = `${$$('.chat-message').length} 条`;
  return node;
}

function attachTrace(node, trace) {
  if ($('.trace-button', node)) return;
  const button = element('button', 'trace-button', `追踪 ${String(trace).slice(0, 12)} ↗`);
  button.type = 'button';
  button.addEventListener('click', () => { $('#log-search').value = trace; showView('logs'); });
  $('.chat-message-body', node).append(button);
}

function findMessage(id) { return $$('.chat-message').find(node => node.dataset.id === String(id)); }

function renderChat() {
  const history = state.history.filter(message => !state.status.session_id || !message.session_id || message.session_id === state.status.session_id);
  if (!history.length) {
    if ($$('.chat-message').length) {
      $('#chat-messages').replaceChildren(element('div', 'small-empty', '新会话已开始。输入问题，或开始聆听。'));
      $('#message-count').textContent = '0 条';
    }
    return;
  }
  $('#chat-messages').replaceChildren();
  for (const message of history) if (message.role === 'user' || message.role === 'assistant') messageNode(message);
  chatScroll(true);
}

function addTranscript(data, event) {
  $('#transcript-empty')?.remove();
  const source = typeof data.source === 'string' ? data.source : safeString(data.source || 'microphone');
  let row = $$('.transcript-item.interim').find(item => item.dataset.source === source);
  if (!row) {
    row = element('div', 'transcript-item');
    row.dataset.source = source;
    const meta = element('div', 'transcript-meta');
    meta.append(element('strong', '', sourceLabel(data.source)), element('time', '', timeLabel(event.created_at)));
    row.append(meta, element('div', 'transcript-text'));
    $('#transcripts').append(row);
  }
  $('.transcript-text', row).textContent = data.text || '';
  row.classList.toggle('interim', data.final === false);
  while ($('#transcripts').children.length > 60) $('#transcripts').firstElementChild.remove();
  $('#transcripts').scrollTop = $('#transcripts').scrollHeight;
}

function streamNode(event, create = true) {
  const key = event.trace_id || 'current';
  if (!state.streams.has(key) && create) state.streams.set(key, messageNode({ role: 'assistant', text: '', created_at: event.created_at, trace_id: event.trace_id }));
  return state.streams.get(key);
}

function onEvent(event) {
  if (state.resyncing && event.kind !== 'stream_reset') { state.pendingEvents.push(event); if (state.pendingEvents.length > 1000) state.pendingEvents.shift(); return; }
  if (event.id && state.seenEvents.has(event.id)) return;
  addEvent(event);
  const data = event.data || {};
  switch (event.kind) {
    case 'state': updateState(data.state, data.listening); break;
    case 'transcript': addTranscript(data, event); break;
    case 'observation_message': scheduleMemoryRefresh(); break;
    case 'stream_reset': resyncStream().catch(error => toast(error.message, 'error')); break;
    case 'user_message': {
      if (!data.id || !findMessage(data.id)) messageNode({ ...data, role: 'user', created_at: event.created_at, trace_id: event.trace_id });
      chatScroll(true);
      break;
    }
    case 'response_start': {
      state.streamGap = false;
      const node = streamNode(event);
      $('.chat-text', node).classList.add('streaming');
      updateState('thinking');
      chatScroll();
      break;
    }
    case 'response_delta': {
      if (state.streamGap) break;
      const node = streamNode(event);
      const text = $('.chat-text', node);
      text.textContent += data.text || data.delta || '';
      text.classList.add('streaming');
      chatScroll();
      break;
    }
    case 'response_done': {
      state.streamGap = false;
      const node = data.id && findMessage(data.id) || streamNode(event);
      const text = $('.chat-text', node);
      if (data.text !== undefined) text.textContent = data.text;
      text.classList.remove('streaming');
      if (data.id) node.dataset.id = data.id;
      if (!text.textContent.trim()) node.remove();
      $('#message-count').textContent = `${$$('.chat-message').length} 条`;
      state.streams.delete(event.trace_id || 'current');
      updateState(audioPlayer.current ? 'speaking' : state.listening ? 'listening' : 'idle');
      chatScroll();
      scheduleMemoryRefresh();
      break;
    }
    case 'audio': audioPlayer.enqueue(data, event.trace_id); break;
    case 'interrupt': stopLocalResponse(); break;
    case 'error': toast(data.message || data.error || '服务发生错误，请查看运行日志。', 'error', 10000); break;
    case 'metrics': {
      const latency = data.first_text_ms ?? data.ttft_ms ?? data.first_token_ms ?? data.first_response_ms ?? data.llm_ttft_ms ?? (data.component === 'llm' ? data.latency_ms : undefined);
      if (latency !== undefined) $('#latency-stat').replaceChildren(document.createTextNode(Number(latency).toFixed(0)), element('em', '', ' ms'));
      break;
    }
    case 'memory_updated': scheduleMemoryRefresh(); break;
    case 'skills_updated': refreshSkills().catch(error => toast(error.message, 'error')); break;
  }
}

async function resyncStream() {
  if (state.resyncing) return;
  state.resyncing = true;
  state.streamGap = true;
  stopLocalResponse();
  try {
    const [status, history, events] = await Promise.all([client.request('/api/status'), client.request('/api/history'), client.request('/api/events')]);
    state.status = status;
    state.history = Array.isArray(history) ? history : history.messages || history.history || [];
    renderChat(); renderHistory(); state.streams.clear();
    updateState(status.state, status.listening);
    for (const event of Array.isArray(events) ? events : events.events || []) addEvent(event);
    toast('实时事件已重新同步。当前回答将在完整生成后显示。', 'warning');
  } finally {
    state.resyncing = false;
    const pending = state.pendingEvents.splice(0);
    for (const event of pending) onEvent(event);
  }
}

function addEvent(event) {
  if (event.id && state.seenEvents.has(event.id)) return;
  if (event.id) state.seenEvents.add(event.id);
  state.events.push(redact(event));
  while (state.events.length > 2000) { const removed = state.events.shift(); if (removed.id) state.seenEvents.delete(removed.id); }
  clearTimeout(eventRenderTimer);
  eventRenderTimer = setTimeout(renderEvents, 80);
}

function eventSummary(event) {
  const data = event.data || {};
  return safeString(data.message || data.text || data.state || data.error || data.detail || data.name || data).slice(0, 600);
}

function renderEvents() {
  if ($('#view-logs').hidden) return;
  const timestamp = value => new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value).getTime();
  state.events.sort((a, b) => timestamp(a.created_at) - timestamp(b.created_at));
  const kind = $('#log-kind').value;
  const query = $('#log-search').value.toLowerCase();
  const filtered = state.events.filter(event => (kind === 'all' || event.kind.includes(kind)) && (!query || JSON.stringify(event).toLowerCase().includes(query)));
  $('#event-count').textContent = `${filtered.length} / ${state.events.length} 条`;
  const list = $('#events-list');
  const opened = new Set($$('details[open]', list).map(node => node.dataset.id));
  const previousTop = list.scrollTop;
  list.replaceChildren();
  if (!filtered.length) list.append(element('div', 'small-empty', '没有符合条件的运行事件。'));
  for (const [index, event] of filtered.entries()) {
    const row = element('details', `event-row ${event.kind === 'error' ? 'error' : ''}`);
    row.dataset.id = event.id || `${event.created_at}-${index}`;
    row.open = opened.has(row.dataset.id);
    const summary = element('summary');
    summary.append(element('time', '', timeLabel(event.created_at)), element('span', 'event-kind', event.kind), element('span', 'event-summary', eventSummary(event)));
    row.append(summary, element('pre', '', JSON.stringify(event, null, 2)));
    list.append(row);
  }
  list.scrollTop = $('#log-follow').checked ? list.scrollHeight : previousTop;
}

function scheduleMemoryRefresh() { clearTimeout(memoryRefreshTimer); memoryRefreshTimer = setTimeout(() => refreshMemory().catch(() => {}), 600); }

async function refreshMemory() {
  const [memories, history] = await Promise.all([client.request('/api/memories'), client.request('/api/history')]);
  state.memories = Array.isArray(memories) ? memories : memories.memories || [];
  state.history = Array.isArray(history) ? history : history.messages || history.history || [];
  renderMemories(); renderHistory();
}

function renderMemories() {
  const query = $('#memory-search').value.toLowerCase();
  const memories = state.memories.filter(item => JSON.stringify(item).toLowerCase().includes(query));
  $('#memory-count').textContent = state.memories.length;
  const list = $('#memory-list');
  list.replaceChildren();
  if (!memories.length) list.append(element('div', 'small-empty', query ? '没有匹配的记忆。' : '这里会保存偏好、重要事实与上下文摘要。你也可以添加第一条记忆。'));
  for (const memory of memories) {
    const row = element('article', 'memory-card');
    const body = element('div', 'memory-content');
    body.append(element('p', 'memory-text', memory.text || memory.content || memory.summary));
    const meta = element('div', 'memory-meta');
    const type = memory.kind || memory.type || 'memory';
    meta.append(element('span', 'tag', ({ summary: '摘要', preference: '偏好', fact: '事实', manual: '手动添加', memory: '长期记忆' })[type] || type), element('time', '', timeLabel(memory.updated_at || memory.created_at, true)));
    const sources = memory.source_ids || memory.sources || memory.source_message_ids;
    if (sources?.length) meta.append(element('span', '', `来源：${sources.map(source => typeof source === 'object' ? source.id || source.message_id : source).join(', ')}`));
    else if (memory.source) meta.append(element('span', '', sourceLabel(memory.source)));
    body.append(meta);
    const remove = element('button', 'delete-button', '删除');
    remove.type = 'button';
    remove.addEventListener('click', () => busy(remove, async () => {
      if (!await confirmAction('删除这条记忆？', '此记忆将不再参与后续检索。需要同时移除来源时，请在下方会话原文中删除对应记录。')) return;
      await client.request(`/api/memories/${encodeURIComponent(memory.id)}`, { method: 'DELETE' });
      await refreshMemory();
      toast('记忆已删除。');
    }));
    const revise = element('button', 'delete-button', '修正'); revise.type = 'button';
    revise.addEventListener('click', () => busy(revise, async () => {
      const text = await editText('修正这条记忆', memory.text || memory.content || memory.summary);
      if (text === null) return;
      await client.request(`/api/memories/${encodeURIComponent(memory.id)}`, { method: 'PUT', body: { text } });
      await refreshMemory(); toast('记忆已修正，旧版本不再用于检索。');
    }));
    const actions = element('div', 'record-actions'); actions.append(revise, remove);
    row.append(element('span', 'memory-icon', '◈'), body, actions);
    list.append(row);
  }
}

function renderHistory() {
  const list = $('#history-list'); list.replaceChildren();
  const history = state.history.slice(-150).reverse();
  if (!history.length) list.append(element('div', 'small-empty', '暂无会话原文。'));
  for (const message of history) {
    const row = element('article', 'history-row');
    const body = element('div');
    body.append(element('small', '', `${message.role === 'assistant' ? 'GreatSage' : message.role === 'observation' ? '旁听内容' : '你'} · ${sourceLabel(message.source)} · ${timeLabel(message.created_at, true)} · ${String(message.session_id || '').slice(0, 10)}`), element('p', '', message.text || message.content));
    const remove = element('button', 'delete-button', '删除');
    remove.type = 'button';
    remove.addEventListener('click', () => busy(remove, async () => {
      if (!await confirmAction('删除这条原文？', '此原文及其关联记忆将按后端规则同步移除。此操作不可撤销。')) return;
      await client.request(`/api/history/${encodeURIComponent(message.id)}`, { method: 'DELETE' });
      await refreshMemory();
      findMessage(message.id)?.remove();
      $('#message-count').textContent = `${$$('.chat-message').length} 条`;
      toast('原文已删除。');
    }));
    const revise = element('button', 'delete-button', '修正'); revise.type = 'button';
    revise.addEventListener('click', () => busy(revise, async () => {
      const text = await editText('修正会话原文', message.text || message.content);
      if (text === null) return;
      await client.request(`/api/history/${encodeURIComponent(message.id)}`, { method: 'PUT', body: { text } });
      await refreshMemory(); renderChat(); toast('原文已修正，关联记忆已同步处理。');
    }));
    const actions = element('div', 'record-actions'); actions.append(revise, remove);
    row.append(body, actions); list.append(row);
  }
}

async function refreshSkills() {
  const result = await client.request('/api/skills');
  state.skills = Array.isArray(result) ? result : result.skills || [];
  const list = $('#skills-list'); list.replaceChildren();
  if (!state.skills.length) list.append(element('div', 'small-empty', '还没有导入技能。添加一个包含 SKILL.md 的目录，开始扩展秘书的能力。'));
  for (const skill of state.skills) {
    const card = element('article', 'skill-card');
    const head = element('div', 'skill-card-head');
    const toggle = element('input'); toggle.type = 'checkbox'; toggle.setAttribute('role', 'switch'); toggle.checked = Boolean(skill.enabled); toggle.setAttribute('aria-label', `${toggle.checked ? '停用' : '启用'}技能 ${skill.name}`);
    toggle.addEventListener('change', () => busy(toggle, async () => {
      try { await client.request(`/api/skills/${encodeURIComponent(skill.id)}`, { method: 'PUT', body: { enabled: toggle.checked } }); toast(toggle.checked ? '技能已启用。' : '技能已停用。'); await refreshSkills(); }
      catch (error) { toggle.checked = !toggle.checked; throw error; }
    }));
    head.append(element('h3', '', skill.name || '未命名技能'), toggle);
    const footer = element('div', 'skill-footer');
    footer.append(element('small', '', `${skill.path || skill.directory || ''}${skill.resources?.length ? ` · ${skill.resources.length} 份参考资料` : ''}`), element('span', 'tag', skill.version ? String(skill.version).slice(0, 12) : skill.content_hash ? String(skill.content_hash).slice(0, 8) : 'SKILL.md'));
    card.append(head, element('p', '', skill.description || '此技能未提供描述。'), footer);
    list.append(card);
  }
}

function confirmAction(title, message) {
  const dialog = $('#confirm-dialog');
  if (dialog.open) return Promise.resolve(false);
  $('#confirm-title').textContent = title;
  $('#confirm-message').textContent = message;
  dialog.returnValue = 'cancel';
  dialog.showModal();
  return new Promise(resolve => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true }));
}

function editText(title, value) {
  const dialog = element('dialog', 'edit-dialog');
  const form = element('form'); form.method = 'dialog';
  const heading = element('h2', '', title);
  const note = element('p', '', '保存修正后，后续回答将使用新的内容。来源与版本信息由后端保留。');
  const input = element('textarea'); input.rows = 6; input.value = value || ''; input.required = true; input.maxLength = 32000; input.setAttribute('aria-label', title);
  const actions = element('div', 'dialog-actions');
  const cancel = element('button', 'button ghost', '取消'); cancel.type = 'submit'; cancel.value = 'cancel'; cancel.formNoValidate = true;
  const save = element('button', 'button primary', '保存修正'); save.type = 'submit'; save.value = 'save';
  actions.append(cancel, save); form.append(heading, note, input, actions); dialog.append(form); document.body.append(dialog);
  form.addEventListener('submit', event => { if (event.submitter?.value === 'save' && !input.value.trim()) { event.preventDefault(); input.setCustomValidity('请输入修正内容。'); input.reportValidity(); } });
  input.addEventListener('input', () => input.setCustomValidity(''));
  dialog.returnValue = 'cancel'; dialog.showModal(); input.focus();
  return new Promise(resolve => dialog.addEventListener('close', () => { resolve(dialog.returnValue === 'save' ? input.value.trim() : null); dialog.remove(); }, { once: true }));
}

class AudioPlayer {
  constructor() { this.queue = []; this.current = null; this.generation = 0; this.loading = false; this.abort = null; }
  enqueue(data, trace) { if (!data.url) return; this.queue.push({ ...data, trace }); this.next(); }
  async next() {
    if (this.current || this.loading || !this.queue.length) return;
    this.loading = true;
    const generation = this.generation;
    const item = this.queue.shift();
    this.abort = new AbortController();
    try {
      const blob = await client.audioBlob(item.url, this.abort.signal);
      if (generation !== this.generation) return;
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      this.current = { audio, url, item };
      audio.addEventListener('ended', () => { if (this.current?.audio === audio) this.finish(); });
      audio.addEventListener('error', () => { if (this.current?.audio !== audio) return; toast('语音播放失败，请检查音频格式或输出设备。', 'error'); this.finish(); });
      audio.addEventListener('playing', () => { if (this.current?.audio !== audio) return; $('#audio-unlock').hidden = true; this.notify(true, item); updateState('speaking'); });
      await audio.play().catch(error => {
        if (error.name === 'NotAllowedError') $('#audio-unlock').hidden = false;
        else if (error.name !== 'AbortError' && this.current?.audio === audio) { toast(`语音播放失败：${error.message}`, 'error'); this.finish(); }
      });
    } catch (error) { if (error.name !== 'AbortError') toast(error.message || '读取语音失败。', 'error'); }
    finally { this.loading = false; if (!this.current) this.next(); }
  }
  notify(playing, item) { client.request('/api/playback', { method: 'POST', body: { playing, text: item?.text || '', trace_id: item?.trace || null } }).catch(() => {}); }
  finish() {
    const previous = this.current;
    this.current = null;
    if (previous) { URL.revokeObjectURL(previous.url); this.notify(false, previous.item); }
    $('#audio-unlock').hidden = true;
    if (!this.queue.length) updateState(state.listening ? 'listening' : 'idle');
    this.next();
  }
  stop() {
    this.generation += 1; this.queue = []; this.abort?.abort();
    if (this.current) { this.current.audio.pause(); this.current.audio.removeAttribute('src'); this.current.audio.load(); }
    this.finish();
  }
  async resume() { if (this.current) await this.current.audio.play(); }
}
const audioPlayer = new AudioPlayer();

function stopLocalResponse() {
  audioPlayer.stop();
  for (const node of state.streams.values()) $('.chat-text', node)?.classList.remove('streaming');
  state.streams.clear();
  updateState(state.listening ? 'listening' : 'idle');
}

async function loadInitial() {
  const status = await client.request('/api/status');
  state.status = status;
  $('#version').textContent = status.version ? `v${String(status.version).replace(/^v/, '')}` : 'v0.1';
  $('#session-label').textContent = status.session_id ? `会话 ${String(status.session_id).slice(0, 8)}` : '本地会话';
  updateState(status.state || 'idle', status.listening);
  const requests = [
    ['设置', async () => fillSettings(await client.request('/api/settings'))],
    ['音频来源', refreshSources],
    ['记忆与历史', async () => { await refreshMemory(); renderChat(); }],
    ['技能', refreshSkills],
    ['日志', async () => { const result = await client.request('/api/events'); const events = Array.isArray(result) ? result : result.events || []; for (const event of events) addEvent(event); }],
  ];
  const results = await Promise.allSettled(requests.map(([, action]) => action()));
  results.forEach((result, index) => { if (result.status === 'rejected') toast(`${requests[index][0]}加载失败：${result.reason.message}`, 'error', 8000); });
  state.initialized = true;
}

$$('.nav-item').forEach(button => button.addEventListener('click', () => showView(button.dataset.view)));
$('.brand').addEventListener('click', event => { event.preventDefault(); showView('conversation'); });
$('#settings-form').addEventListener('submit', event => { event.preventDefault(); busy($('#save-settings'), saveSettings); });
$('#quick-desktop-source').addEventListener('change', () => { $('#process-field').hidden = $('#quick-desktop-source').value !== 'process'; });
$('#quick-microphone').addEventListener('change', () => { $('#quick-microphone-device').disabled = !$('#quick-microphone').checked; });
$('#quick-mode').addEventListener('change', () => busy($('#quick-mode'), async () => { try { await saveQuickSettings(); toast(`已切换到${modeLabels[state.settings.mode]}。`); } catch (error) { updateQuickSettings(); throw error; } }));
$('#refresh-sources').addEventListener('click', () => busy($('#refresh-sources'), refreshSources));
$('#toggle-listening').addEventListener('click', () => busy($('#toggle-listening'), async () => {
  if (!state.listening) {
    if (!$('#quick-microphone').checked && $('#quick-desktop-source').value === 'none') throw new Error('请至少开启麦克风或选择一个桌面音源。');
    await saveQuickSettings();
  }
  const enabled = !state.listening;
  const result = await client.request('/api/listening', { method: 'POST', body: { enabled } });
  updateState(result?.state || (enabled ? 'listening' : 'idle'), result?.listening ?? enabled);
}));

$('#chat-form').addEventListener('submit', event => {
  event.preventDefault();
  const input = $('#chat-input'); const text = input.value.trim();
  if (!text) return;
  busy($('#send-message'), async () => {
    input.value = ''; input.style.height = '';
    try { await client.request('/api/chat', { method: 'POST', body: { text } }); }
    catch (error) { if (!input.value) input.value = text; throw error; }
    input.focus();
  });
});
$('#chat-input').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); if (!$('#send-message').disabled) $('#chat-form').requestSubmit(); } });
$('#chat-input').addEventListener('input', event => { event.target.style.height = 'auto'; event.target.style.height = Math.min(event.target.scrollHeight, 160) + 'px'; });
$$('[data-prompt]').forEach(button => button.addEventListener('click', () => { $('#chat-input').value = button.dataset.prompt; $('#chat-input').focus(); }));
$('#interrupt').addEventListener('click', () => { stopLocalResponse(); busy($('#interrupt'), () => client.request('/api/interrupt', { method: 'POST' })); });
$('#resume-audio').addEventListener('click', () => busy($('#resume-audio'), () => audioPlayer.resume()));
$('#new-session').addEventListener('click', () => busy($('#new-session'), async () => {
  stopLocalResponse();
  const result = await client.request('/api/sessions', { method: 'POST' });
  state.status.session_id = result.session_id || result.id;
  $('#session-label').textContent = `会话 ${String(state.status.session_id || '').slice(0, 8)}`;
  await refreshMemory(); renderChat();
  $('#transcripts').replaceChildren(element('div', 'small-empty', '新会话已开始，等待语音输入。'));
  showView('conversation'); toast('新会话已开始，长期记忆继续保留。');
}));

$('#memory-search').addEventListener('input', renderMemories);
$('#refresh-memory').addEventListener('click', () => busy($('#refresh-memory'), refreshMemory));
$('#memory-form').addEventListener('submit', event => { event.preventDefault(); busy($('button', event.target), async () => { const text = $('#memory-input').value.trim(); if (!text) return; await client.request('/api/memories', { method: 'POST', body: { text } }); $('#memory-input').value = ''; await refreshMemory(); toast('已加入长期记忆。'); }); });
$('#clear-history').addEventListener('click', () => busy($('#clear-history'), async () => {
  if (!await confirmAction('清空所有历史与关联记忆？', '将删除跨会话的对话原文，并清理关联记忆。此操作不可撤销。')) return;
  stopLocalResponse();
  await client.request('/api/history/clear', { method: 'POST', body: { confirmation: 'DELETE' } });
  await refreshMemory(); renderChat();
  toast('历史与关联记忆已清空。');
}));

$('#refresh-skills').addEventListener('click', () => busy($('#refresh-skills'), refreshSkills));
$('#skill-form').addEventListener('submit', event => { event.preventDefault(); busy($('button[type=submit]', event.target), async () => { const path = $('#skill-path').value.trim(); if (!path) return; await client.request('/api/skills/import', { method: 'POST', body: { path } }); $('#skill-path').value = ''; await refreshSkills(); toast('技能已导入。'); }); });
$('#choose-skill').addEventListener('click', () => busy($('#choose-skill'), async () => { if (!window.greatsage?.chooseSkillDirectory) throw new Error('浏览器模式下请直接填写本地技能目录的完整路径。'); const path = await window.greatsage.chooseSkillDirectory(); if (path) $('#skill-path').value = path; }));

$('#log-kind').addEventListener('change', renderEvents);
$('#log-search').addEventListener('input', renderEvents);
$('#log-follow').addEventListener('change', renderEvents);
$('#export-logs').addEventListener('click', () => {
  const kind = $('#log-kind').value; const query = $('#log-search').value.toLowerCase();
  const events = state.events.filter(event => (kind === 'all' || event.kind.includes(kind)) && (!query || JSON.stringify(event).toLowerCase().includes(query)));
  const url = URL.createObjectURL(new Blob([JSON.stringify({ exported_at: new Date().toISOString(), events: redact(events) }, null, 2)], { type: 'application/json' }));
  const link = element('a'); link.href = url; link.download = `greatsage-events-${new Date().toISOString().replace(/[:.]/g, '-')}.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast(`已导出 ${events.length} 条当前日志。`);
});

$$('.test-provider').forEach(button => button.addEventListener('click', () => busy(button, async () => {
  const output = $('#test-' + button.dataset.component);
  output.className = 'provider-result'; output.textContent = '正在测试已保存的配置，请稍候…';
  try {
    const result = await client.request('/api/providers/test', { method: 'POST', body: { component: button.dataset.component } });
    output.classList.toggle('error', result.ok === false || result.success === false);
    output.textContent = redact(safeString(result.message || result.detail || result.text || JSON.stringify(result, null, 2)));
  } catch (error) { output.classList.add('error'); output.textContent = redact(error.message); }
})));

$('#show-pet').addEventListener('click', () => busy($('#show-pet'), async () => {
  if (window.greatsage?.showPet) await window.greatsage.showPet();
  else { const url = new URL('pet.html', location.href); if (client.token) url.hash = new URLSearchParams({ token: client.token }).toString(); window.open(url, 'greatsage-pet', 'popup,width=350,height=430'); }
}));
$('#hide-pet').addEventListener('click', () => busy($('#hide-pet'), async () => { if (window.greatsage?.hidePet) await window.greatsage.hidePet(); else toast('浏览器模式下请直接关闭桌宠窗口。'); }));
$('#minimize').hidden = !window.greatsage?.minimize;
$('#minimize').addEventListener('click', () => window.greatsage?.minimize());
$('#reconnect').addEventListener('click', () => busy($('#reconnect'), async () => { client.connect(); await loadInitial(); }));
setupProviderExtras();

client.addEventListener('connection', event => {
  showConnection(event.detail.state, event.detail.message);
  if (event.detail.state === 'connected' && state.initialized) {
    client.request('/api/status').then(status => { state.status = status; updateState(status.state, status.listening); }).catch(error => showConnection('error', error.message));
    client.request('/api/events').then(result => { for (const item of Array.isArray(result) ? result : result.events || []) addEvent(item); }).catch(() => {});
  }
});
client.addEventListener('event', event => onEvent(event.detail));
window.addEventListener('beforeunload', () => { audioPlayer.stop(); client.close(); });

try { await client.initialize(); await loadInitial(); }
catch (error) { showConnection('error', error.message); $('#memory-list').replaceChildren(element('div', 'small-empty', '连接服务后即可读取记忆。')); $('#skills-list').replaceChildren(element('div', 'small-empty', '连接服务后即可读取技能。')); }
