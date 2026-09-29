// v0.2 controls for semantic memory, request budgets and auditable background work.
import { setupAudit } from './audit.js';
export function setupV02({ client, state, toast, busy, confirmAction, refreshMemory }) {
  const audit = setupAudit({ client, busy, toast, confirmAction });
  const $ = selector => document.querySelector(selector);
  const node = (tag, className, text) => { const el = document.createElement(tag); el.className = className; if (text !== undefined) el.textContent = text; return el; };
  const settings = document.createElement('section');
  settings.className = 'panel settings-section wide';
  settings.innerHTML = `<div class="panel-heading"><h2>语义记忆与审计</h2></div>
    <div class="panel-body form-grid">
      <label class="switch-row full-span"><span><strong>语义检索</strong><small>开启后建立历史文本索引；使用云端时文本会发送到所选嵌入服务</small></span><input type="checkbox" role="switch" name="embedding.enabled" /></label>
      <label class="field"><span>嵌入服务</span><select name="embedding.provider"><option value="openrouter">OpenRouter</option><option value="openai">OpenAI 兼容 API</option><option value="ollama">本地 Ollama</option></select></label>
      <label class="field"><span>嵌入模型</span><input name="embedding.model" /></label>
      <label class="field"><span>API 地址</span><input name="embedding.base_url" type="url" /></label>
      <label class="field"><span>密钥环境变量</span><input name="embedding.api_key_env" autocomplete="off" /></label>
      <label class="field"><span>设置新的 API 密钥</span><input name="embedding.api_key" type="password" autocomplete="new-password" placeholder="留空保留已配置的密钥" /></label>
      <label class="field"><span>回答前检索最多等待 / 秒</span><input name="embedding.query_timeout_seconds" type="number" min="0.2" max="10" step="0.1" /><small>超时后使用关键词检索；本地模型可能需要更长时间。</small></label>
      <div class="field"><span>保存后测试嵌入服务</span><button type="button" class="button ghost test-provider" data-component="embedding">测试连接</button><div class="provider-result" id="test-embedding" role="status"></div></div>
      <label class="switch-row full-span"><span><strong>旁听语义判断</strong><small>对含糊的提问交给语言模型判断；明确叫名直接回应，增加的调用会记入日志</small></span><input type="checkbox" role="switch" name="semantic_decisions" /></label>
      <label class="switch-row full-span"><span><strong>保存完整请求快照</strong><small>在本机保存实际模型输入和注入的 Skill 正文，便于审计；默认仅记元数据</small></span><input type="checkbox" role="switch" name="audit_content" /></label>
    </div>`;
  $('#settings-form').append(settings);
  const budget = document.createElement('div');
  budget.className = 'tokenizer-tools';
  budget.innerHTML = `<label class="field"><span>上下文编码</span><select name="llm.tokenizer"><option value="auto">自动识别；未知模型保守估算</option><option value="utf8">保守 UTF-8 字节预算</option><option value="cl100k_base">cl100k_base</option><option value="o200k_base">o200k_base</option></select></label><button type="button" class="button small ghost" id="prepare-tokenizer">准备所选编码</button><p class="source-note" id="tokenizer-note">编码文件需手动准备；未准备时使用保守预算。</p>`;
  $('[data-provider="llm"]').append(budget);
  const index = document.createElement('section');
  index.className = 'panel index-panel';
  index.innerHTML = `<div class="panel-heading"><h2>语义索引与后台任务</h2><button class="button small subtle" id="refresh-index">刷新状态</button></div><div class="panel-body"><p id="index-status" role="status">正在读取索引状态…</p><p class="source-note" id="background-status"></p><div class="panel-tools"><button class="button ghost small" id="rebuild-index">重建语义索引</button><button class="button subtle small" id="retry-background">立即重试后台任务</button></div><p class="source-note">原文保持在本机。后台在对话空闲时建立索引，服务不可用时仍可按关键词检索。</p></div>`;
  const conflicts = document.createElement('section');
  conflicts.className = 'panel conflict-panel'; conflicts.hidden = true;
  conflicts.innerHTML = '<div class="panel-heading"><h2>待确认的记忆</h2><span class="tag">保留你的决定</span></div><p class="panel-hint">新内容可能与已有记忆矛盾。在你选择之前，新候选不参与长期记忆检索。识别只是建议，请结合原文判断。</p><div id="memory-conflicts"></div>';
  $('#view-memory .view-header').after(conflicts);
  const details = document.createElement('dialog');
  details.className = 'record-dialog'; details.id = 'record-dialog';
  details.innerHTML = '<div class="dialog-actions"><button type="button" class="button small ghost" id="record-back">返回上一级</button><button type="button" class="button small ghost" id="record-close">关闭</button></div><h2>记录与来源</h2><div id="record-detail"></div>';
  document.body.append(details);
  let recordStack = [], recordEpoch = 0;
  async function showRecord(id, back = false) {
    if (!back) recordStack.push(id);
    const epoch = ++recordEpoch;
    $('#record-back').disabled = recordStack.length < 2;
    $('#record-detail').textContent = '正在读取…';
    if (!details.open) details.showModal();
    try {
      const result = await client.request(`/api/records/${encodeURIComponent(id)}`);
      if (epoch !== recordEpoch) return;
      const body = $('#record-detail'); body.replaceChildren();
      const record = result.record;
      body.append(node('p', 'source-note', `${record.kind} · ${record.created_at} · ${record.id}${record.level ? ` · 第 ${record.level} 层摘要` : ''}`), node('p', 'record-text', record.text), node('h3', '', `直接来源 · ${result.sources.length}`));
      for (const source of result.sources) {
        const button = node('button', 'source-record button ghost', `${source.kind} · ${source.id.slice(0, 8)}\n${source.text.slice(0, 160)}`);
        button.type = 'button'; button.addEventListener('click', () => showRecord(source.id)); body.append(button);
      }
      if (!result.sources.length) body.append(node('p', 'source-note', '这是原始记录或手动添加的记忆，没有上游来源。'));
    } catch (error) { if (epoch === recordEpoch) $('#record-detail').textContent = error.message; }
  }
  $('#record-close').addEventListener('click', () => details.close());
  details.addEventListener('close', () => { recordStack = []; recordEpoch++; $('#record-detail').replaceChildren(); });
  $('#record-back').addEventListener('click', () => { recordStack.pop(); showRecord(recordStack.at(-1), true); });

  const archive = document.createElement('details'); archive.className = 'panel archive-panel';
  archive.innerHTML = '<summary class="panel-heading">浏览更早的会话原文</summary><div class="panel-body"><label class="field"><span>会话</span><select id="archive-session"><option value="">所有会话</option></select></label><div id="archive-records"></div><button class="button ghost full" id="archive-more">加载原文</button></div>';
  $('#view-memory').append(archive, index);
  let archiveCursor = null, archiveEpoch = 0, archiveLoaded = false;
  async function loadArchive(reset = false) {
    if (reset) { archiveCursor = null; archiveEpoch++; $('#archive-records').replaceChildren(); }
    const epoch = archiveEpoch;
    const query = new URLSearchParams({ limit: '20' });
    if (archiveCursor) query.set('cursor', archiveCursor);
    if ($('#archive-session').value) query.set('session_id', $('#archive-session').value);
    const page = await client.request('/api/history/page?' + query);
    if (epoch !== archiveEpoch) return;
    for (const record of page.items) {
      const button = node('button', 'archive-record', `${record.role} · ${record.created_at} · 会话 ${record.session_id.slice(0, 8)}\n${record.text}`);
      button.type = 'button'; button.addEventListener('click', () => showRecord(record.id)); $('#archive-records').append(button);
    }
    archiveCursor = page.next_cursor;
    $('#archive-more').hidden = !archiveCursor;
    if (!$('#archive-records').children.length) $('#archive-records').append(node('p', 'source-note', '此会话没有原文。'));
  }
  archive.addEventListener('toggle', async () => {
    if (!archive.open || archiveLoaded) return;
    archiveLoaded = true;
    try {
      const sessions = await client.request('/api/sessions');
      for (const session of sessions.slice(0, 100)) $('#archive-session').add(new Option(`${session.created_at.slice(0, 16)} · ${session.message_count} 条 · ${session.id.slice(0, 8)}`, session.id));
      await loadArchive(true);
    } catch (error) { archiveLoaded = false; toast(error.message, 'error'); }
  });
  $('#archive-more').addEventListener('click', () => busy($('#archive-more'), () => loadArchive()));
  $('#archive-session').addEventListener('change', () => busy($('#archive-session'), () => loadArchive(true)));

  async function refreshConflicts() {
    const rows = await client.request('/api/memory/conflicts');
    conflicts.hidden = !rows.length;
    $('#memory-conflicts').replaceChildren();
    for (const item of rows) {
      const row = node('article', 'conflict-card');
      row.append(node('strong', '', '新候选'), node('p', 'record-text', item.candidate.text), node('strong', '', '已有记忆'));
      for (const existing of item.existing) {
        const link = node('button', 'source-record button ghost', existing.text); link.type = 'button';
        link.addEventListener('click', () => showRecord(existing.id)); row.append(link);
      }
      const actions = node('div', 'panel-tools');
      for (const [resolution, label] of [['replace', '用新内容替换'], ['keep_existing', '丢弃新候选'], ['keep_both', '同时保留']]) {
        const button = node('button', 'button small ghost', label); button.type = 'button';
        button.addEventListener('click', () => busy(button, async () => {
          if (resolution === 'replace' && !await confirmAction('用新内容替换这些记忆？', `将替换 ${item.existing.length} 条旧记忆，并使其派生回答失效。原始来源仍保留。`)) return;
          await client.request(`/api/memory/conflicts/${item.candidate.id}`, { method: 'POST', body: { resolution } });
          await refreshMemory({ refreshChat: true }); await refreshConflicts(); toast('记忆选择已保存。');
        })); actions.append(button);
      }
      row.append(actions); $('#memory-conflicts').append(row);
    }
  }

  async function refresh() {
    await audit.refresh();
    const [index, budget] = await Promise.all([client.request('/api/memory/index'), client.request('/api/tokenizer'), refreshConflicts()]);
    $('#index-status').textContent = index.enabled ? `已索引 ${index.indexed} / ${index.total} 条，${index.pending} 条待处理，${index.chunks} 个文本片段。` : '语义检索已关闭；当前使用关键词检索。';
    const labels = { index: '索引', compression: '摘要', retrieval: '检索' };
    $('#background-status').textContent = Object.entries(index.background || {}).map(([name, job]) => `${labels[name] || name}：${job.running ? '处理中' : job.retry_in_seconds > 0 ? `${job.retry_in_seconds} 秒后重试` : '等待空闲'}`).join(' · ');
    $('#tokenizer-note').textContent = `当前预算方式：${budget.method}。${budget.requested && !budget.ready ? '保存设置后准备编码文件即可启用。' : '预算仍包含协议开销预留。'}`;
  }
  $('#refresh-index').addEventListener('click', () => busy($('#refresh-index'), refresh));
  $('#rebuild-index').addEventListener('click', () => busy($('#rebuild-index'), async () => {
    await client.request('/api/memory/reindex', { method: 'POST' });
    await refresh(); toast('索引已重置，开启语义检索后将在空闲时重建。');
  }));
  $('#retry-background').addEventListener('click', () => busy($('#retry-background'), async () => {
    await client.request('/api/background/retry', { method: 'POST' }); await refresh();
  }));
  $('#prepare-tokenizer').addEventListener('click', () => busy($('#prepare-tokenizer'), async () => {
    if ($('[name="llm.tokenizer"]').value !== state.settings?.llm?.tokenizer || $('[name="llm.model"]').value !== state.settings?.llm?.model) throw new Error('请先保存语言模型和编码设置。');
    $('#tokenizer-note').textContent = '正在下载并校验编码文件…';
    const result = await client.request('/api/tokenizer/prepare', { method: 'POST' });
    toast(result.detail); await refresh();
  }));
  $('[name="embedding.provider"]').addEventListener('change', event => {
    const provider = event.target.value;
    const defaults = provider === state.settings?.embedding?.provider ? state.settings.embedding : {
      openrouter: { base_url: 'https://openrouter.ai/api/v1', model: 'openai/text-embedding-3-small', api_key_env: 'OPENROUTER_API_KEY' },
      openai: { base_url: 'https://api.openai.com/v1', model: 'text-embedding-3-small', api_key_env: 'OPENAI_API_KEY' },
      ollama: { base_url: 'http://127.0.0.1:11434', model: 'bge-m3:latest', api_key_env: '' },
    }[provider];
    for (const name of ['base_url', 'model', 'api_key_env']) $(`[name="embedding.${name}"]`).value = defaults[name];
    $('[name="embedding.api_key"]').value = '';
  });
  let timer;
  return {
    showRecord,
    refresh: () => refresh().catch(error => { $('#index-status').textContent = error.message; }),
    onEvent: event => {
      audit.onEvent(event);
      if (event.kind === 'memory_updated') {
        if (details.open && recordStack.length) showRecord(recordStack.at(-1), true);
        if (archive.open) loadArchive(true).catch(() => {});
      }
      if (['index_updated', 'index_reset', 'compression_done', 'settings_updated', 'memory_updated'].includes(event.kind)) {
        clearTimeout(timer); timer = setTimeout(() => refresh().catch(() => {}), 300);
      }
    },
  };
}
