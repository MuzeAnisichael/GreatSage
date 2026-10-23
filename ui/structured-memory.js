// Candidate records stay outside active memory until reviewed.
const kindLabels = { fact: '事实', preference: '偏好', relationship: '关系' };
const entityLabels = { person: '人物', project: '项目', organization: '组织', other: '其他', literal: '内容' };
const reviewLabels = { uncertain: '内容不确定', sensitive: '涉及敏感信息', ambiguous_entity: '实体归属待确认', confirmation_policy: '按当前策略确认', conflict: '与已有记忆冲突' };
const policyLabels = { auto: '自动生效', necessary: '仅必要时确认', mostly: '大部分确认' };
const extractionErrors = { invalid_extraction_schema: '模型未提供有效结构化结果，请检查模型能力后手动重试', missing_extraction_result: '模型没有返回提案或明确的空结果，请检查模型能力', invalid_request: '当前模型或接口不支持该请求，请调整模型设置', input_budget_exceeded: '原文超出当前上下文预算，已延后处理', invalid_credentials: '模型凭据无效，请检查设置', rate_limited: '服务暂时限流，请稍后重试', timeout: '请求超时，可稍后重试', extraction_failed: '模型提取失败，请查看服务配置', extraction_invalid: '模型输出未通过证据检查', invalid_response: '模型输出无法读取', extraction_superseded: '来源或设置已变化，旧结果已丢弃' };
const node = (tag, className, text) => { const el = document.createElement(tag); el.className = className; if (text !== undefined) el.textContent = String(text); return el; };
const button = (label, action, className = 'button ghost small') => { const el = node('button', className, label); el.type = 'button'; el.addEventListener('click', action); return el; };

export function appendStructuredEvidence(body, structured, showRecord) {
  if (!structured) return;
  body.append(node('h3', '', '提取依据'), node('p', 'source-note', '以下是根据用户原文提取的自述或派生信息，未经独立核实。'));
  body.append(node('p', 'structured-relation', `${structured.subject} · ${structured.predicate} · ${structured.value}`));
  body.append(node('p', 'source-note', `${kindLabels[structured.kind] || structured.kind} · ${entityLabels[structured.subject_type] || structured.subject_type}${structured.model ? ` · 提取模型：${structured.model}` : ''}`));
  for (const evidence of structured.evidence || []) {
    const quote = node('blockquote', 'evidence-quote', evidence.quote);
    const source = button(`查看原文 ${evidence.source_id.slice(0, 8)}${evidence.version ? ` · v${evidence.version}` : ''}`, () => showRecord(evidence.source_id), 'source-link');
    source.dataset.sourceId = evidence.source_id;
    body.append(quote, source);
  }
  if (!(structured.evidence || []).length) body.append(node('p', 'source-note', '原文证据不可用；请刷新后检查来源。'));
}

export function setupStructuredMemory({ client, state, toast, busy, refreshMemory, showRecord, focusMemory, showConflicts }) {
  const $ = selector => document.querySelector(selector);
  const settings = node('section', 'panel settings-section wide'); settings.id = 'structured-memory-settings';
  settings.innerHTML = `<div class="panel-heading"><h2>自动提取与记忆确认</h2></div><div class="panel-body form-grid">
    <label class="switch-row full-span"><span><strong>自动提取记忆</strong><small>默认关闭。启用后，在后台使用当前聊天模型和已有凭据处理用户文字与麦克风原文；云端模型会收到相应原文，并可能产生调用费用。</small></span><input type="checkbox" role="switch" name="memory_extraction_enabled" /></label>
    <label class="field full-span"><span>提取后的生效策略</span><select name="memory_confirmation"><option value="auto">自动生效</option><option value="necessary">仅必要时确认</option><option value="mostly" selected>大部分确认</option></select><small>自动生效：有效提取直接加入记忆。仅必要时确认：不确定、敏感或实体归属含糊时需确认。大部分确认：普通对话提取全部进入候选。</small></label>
    <p class="source-note full-span">三档策略都保留冲突裁决，不会覆盖已有记忆。更改设置不会自动批准已有候选。明确“记住”和手动添加仍沿用现有流程。</p>
  </div>`;
  $('#settings-form').append(settings);

  const extraction = node('section', 'panel extraction-panel');
  extraction.innerHTML = '<div class="panel-heading"><h2>后台记忆提取</h2><button type="button" class="button ghost small" id="refresh-extraction">刷新状态</button></div><div class="panel-body"><p id="extraction-status" role="status">正在读取提取状态…</p><p id="extraction-counts" class="source-note"></p><p id="extraction-error" class="source-note" role="status" hidden></p><div class="panel-tools"><button type="button" class="button primary small" id="extract-current-session">提取当前会话</button><button type="button" class="button ghost small" id="extraction-settings">提取设置</button></div><p class="source-note">只提取用户文字和麦克风原文；助手、工具与桌面旁听不会自动成为用户事实。手动提取也需要先启用并保存开关。</p></div>';
  const candidatesPanel = node('section', 'panel candidates-panel');
  candidatesPanel.innerHTML = '<div class="panel-heading"><h2>待确认候选<span class="count-badge" id="candidate-count">0</span></h2></div><p class="panel-hint">候选在确认前不参与长期记忆检索。拒绝或删除后，该旧来源版本不会再自动提取任何新记忆；保留的原文仍可被历史检索找到。</p><div id="memory-candidates" aria-live="polite"></div>';
  $('#view-memory .view-header').after(extraction, candidatesPanel);

  const entitiesPanel = node('section', 'panel entities-panel');
  entitiesPanel.innerHTML = '<div class="panel-heading"><h2>实体与关系<span class="count-badge" id="entity-count">0</span></h2><input type="search" class="search-input" id="entity-search" placeholder="搜索人物、项目或组织" aria-label="搜索实体" /></div><p class="panel-hint">按相同名称和类型归组；同名人物可能需要人工区分，暂不识别别名。条目均来自用户自述或其派生信息。</p><div id="memory-entities" class="entity-list"></div>';
  $('#memory-list').closest('.panel').after(entitiesPanel);
  const detail = node('dialog', 'record-dialog entity-dialog'); detail.id = 'entity-dialog';
  detail.innerHTML = '<div class="dialog-actions"><button type="button" class="button ghost small" id="entity-close">关闭</button></div><h2 id="entity-title">实体与关系</h2><div id="entity-detail"></div>';
  document.body.append(detail);
  let epoch = 0, entityEpoch = 0, statusEpoch = 0, selectedEntity = '', timer, searchTimer;
  $('#entity-close').onclick = () => detail.close();
  detail.addEventListener('close', () => { selectedEntity = ''; entityEpoch++; $('#entity-detail').replaceChildren(); });

  function openSettings() {
    $('[data-view=settings]').click();
    const input = $('[name=memory_extraction_enabled]'); input.scrollIntoView({ block: 'center' }); input.focus();
  }
  $('#extraction-settings').onclick = openSettings;

  async function refreshStatus() {
    const request = ++statusEpoch;
    try {
      const status = await client.request('/api/memory/extraction');
      if (request !== statusEpoch) return;
      const running = status.background?.running;
      const retry = status.background?.retry_in_seconds || 0;
      const phase = !status.enabled ? '已关闭' : running ? '提取中' : retry > 0 ? `${retry} 秒后重试` : ({ deferred: '原文超出预算，已延后', queued: '等待后台处理', running: '提取中', idle: '等待空闲', backoff: '等待重试', error: '提取失败', failed: '提取失败', needs_attention: '已暂停，请调整配置或手动重试', completed: '本批提取完成', discarded: '旧结果已丢弃', cancelled: '已取消' })[status.phase] || '等待空闲';
      $('#extraction-status').textContent = `${phase} · ${policyLabels[status.confirmation] || policyLabels.mostly} · 当前模型：${status.model || state.settings?.llm?.model || '未配置'}`;
      $('#extraction-counts').textContent = `来源：${status.eligible_sources ?? 0} 条可提取，${status.processed_sources ?? 0} 条已处理，${status.pending_sources ?? 0} 条待处理（${status.deferred_sources ?? 0} 条超出预算延后） · ${status.active ?? 0} 条有效提取 · ${status.pending ?? 0} 条候选 · ${status.conflicts ?? 0} 项冲突`;
      const error = status.error || status.background?.error || status.background?.last_error;
      $('#extraction-error').hidden = !error;
      $('#extraction-error').textContent = error ? `提取暂未完成：${extractionErrors[error] || error}` : '';
      $('#extract-current-session').textContent = status.enabled ? '提取当前会话' : '启用后提取当前会话';
    } catch (error) {
      if (request === statusEpoch) { $('#extraction-status').textContent = `无法读取提取状态：${error.message}`; $('#extraction-counts').textContent = ''; $('#extraction-error').hidden = true; }
    }
  }
  $('#refresh-extraction').onclick = () => busy($('#refresh-extraction'), refreshStatus);
  $('#extract-current-session').onclick = () => busy($('#extract-current-session'), async () => {
    if (!state.settings?.memory_extraction_enabled) { openSettings(); toast('请先开启“自动提取记忆”并保存设置，再提取当前会话。'); return; }
    const result = await client.request('/api/memory/extraction', { method: 'POST' });
    await refreshStatus();
    toast(result.scheduled ? '已安排提取当前会话，可继续对话。' : '本次未安排新的提取，请查看提取状态。');
  });

  function renderCandidates(items) {
    $('#candidate-count').textContent = items.length;
    const list = $('#memory-candidates'); list.replaceChildren();
    if (!items.length) list.append(node('p', 'small-empty', '暂无待确认候选。'));
    for (const item of items) {
      const row = node('article', 'candidate-card'); row.dataset.id = item.id;
      row.append(node('p', 'record-text candidate-text', item.text));
      const conflict = item.has_conflict || item.conflict_ids?.length;
      const reasons = item.review_reasons || item.structured?.review_reasons || [];
      const tags = node('div', 'memory-meta'); tags.append(node('span', 'tag', kindLabels[item.structured?.kind] || '记忆'));
      for (const reason of reasons) tags.append(node('span', 'tag', reviewLabels[reason] || reason));
      if (conflict) tags.append(node('span', 'tag', '需冲突裁决'));
      row.append(tags);
      const actions = node('div', 'panel-tools');
      actions.append(button('原文证据', () => showRecord(item.id)));
      if (conflict) actions.append(button('查看冲突裁决', () => showConflicts(item.id)));
      else {
        const accept = button('确认加入记忆', () => busy(accept, () => resolveCandidate(item.id, 'accept')), 'button primary small');
        actions.append(accept);
      }
      const reject = button('拒绝候选', () => busy(reject, () => resolveCandidate(item.id, 'reject')));
      actions.append(reject); row.append(actions); list.append(row);
    }
  }
  async function resolveCandidate(id, action) {
    try {
      await client.request(`/api/memory/candidates/${encodeURIComponent(id)}/resolve`, { method: 'POST', body: { action } });
      toast(action === 'accept' ? '候选已加入长期记忆。' : '候选已拒绝，同一旧来源不会再次恢复该条提取。');
    } finally {
      // A source may have been removed while the decision was in flight.
      await refreshMemory({ refreshChat: true });
    }
  }

  async function showEntity(id) {
    selectedEntity = id;
    const request = ++entityEpoch;
    $('#entity-detail').textContent = '正在读取…';
    if (!detail.open) detail.showModal();
    try {
      const result = await client.request(`/api/memory/entities/${encodeURIComponent(id)}`);
      if (request !== entityEpoch || selectedEntity !== id) return;
      $('#entity-title').textContent = `${result.entity.name} · ${entityLabels[result.entity.type] || result.entity.type}`;
      const body = $('#entity-detail'); body.replaceChildren();
      const facts = result.facts.filter(item => item.structured?.kind !== 'relationship');
      for (const [label, items] of [['事实与偏好', facts], ['关系', result.relationships]]) {
        body.append(node('h3', '', `${label} · ${items.length}`));
        if (!items.length) body.append(node('p', 'source-note', `暂无${label}。`));
        for (const item of items) {
          const row = node('article', 'entity-fact');
          row.append(node('p', 'record-text', item.text));
          const structured = item.structured;
          if (structured) row.append(node('p', 'structured-relation', `${structured.subject} · ${structured.predicate} · ${structured.value}`));
          row.append(node('span', 'tag', item.status === 'active' ? '已生效' : '待确认'));
          const actions = node('div', 'panel-tools'); actions.append(button('原文证据', () => showRecord(item.id)));
          actions.append(button(item.status === 'active' ? '修正或删除' : '查看候选', () => {
            detail.close();
            if (item.status === 'active') focusMemory(item.id);
            else { const candidate = [...$('#memory-candidates').children].find(el => el.dataset.id === item.id); candidate?.scrollIntoView({ block: 'center' }); candidate?.querySelector('button')?.focus(); }
          }));
          row.append(actions); body.append(row);
        }
      }
    } catch (error) { if (request === entityEpoch) { $('#entity-title').textContent = '实体与关系'; $('#entity-detail').textContent = `此实体当前不可用：${error.message}`; } }
  }
  async function refreshEntities() {
    const request = ++entityEpoch;
    try {
      const items = await client.request(`/api/memory/entities?q=${encodeURIComponent($('#entity-search').value.trim())}`);
      if (request !== entityEpoch) return;
      $('#entity-count').textContent = items.length;
      const list = $('#memory-entities'); list.replaceChildren();
      for (const item of items) {
        const card = button('', () => showEntity(item.id), 'entity-card'); card.dataset.id = item.id;
        card.append(node('strong', '', item.name), node('span', 'tag', entityLabels[item.type] || item.type), node('small', 'source-note', `${item.fact_count} 条有效记忆 · ${item.pending_count} 条待确认`)); list.append(card);
      }
      if (!items.length) list.append(node('p', 'small-empty', $('#entity-search').value.trim() ? '没有匹配的实体。' : '提取到明确名称和类型后，将在这里显示实体与关系。'));
      if (selectedEntity) await showEntity(selectedEntity);
    } catch (error) { if (request === entityEpoch) { $('#entity-count').textContent = '—'; $('#memory-entities').replaceChildren(node('p', 'small-empty', `实体读取失败：${error.message}`)); } }
  }
  $('#entity-search').addEventListener('input', () => { entityEpoch++; clearTimeout(searchTimer); searchTimer = setTimeout(refreshEntities, 250); });
  async function refresh() {
    const request = ++epoch;
    await Promise.all([refreshStatus(), refreshEntities(), (async () => {
      try { const items = await client.request('/api/memory/candidates'); if (request === epoch) renderCandidates(items); }
      catch (error) { if (request === epoch) { $('#candidate-count').textContent = '—'; $('#memory-candidates').replaceChildren(node('p', 'small-empty', `候选读取失败：${error.message}`)); } }
    })()]);
  }
  return { refresh, onEvent(event) {
    if (!['memory_updated', 'memory_extraction', 'settings_updated', 'session', 'stream_reset'].includes(event.kind)) return;
    epoch++; entityEpoch++; statusEpoch++; clearTimeout(timer);
    timer = setTimeout(refresh, 250);
  } };
}
