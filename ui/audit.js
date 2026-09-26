export function setupAudit({ client, busy, toast, confirmAction }) {
  const panel = document.createElement('section'); panel.className = 'panel audit-panel';
  panel.innerHTML = '<div class="panel-heading"><h2>请求审计快照</h2><button class="button ghost small" id="audit-refresh">刷新</button></div><p class="panel-hint">默认保存配置、Skill 版本和请求摘要哈希。只有事先开启全文快照，才会保留实际输入；删除来源后相关正文失效。外部模型结果可能无法完全复现。</p><div class="panel-body"><label class="field"><span>按 trace 查找</span><input id="audit-trace" placeholder="留空查看最近 100 个请求" /></label><div id="audit-list"></div></div>';
  document.querySelector('#view-logs').prepend(panel);
  const dialog = document.createElement('dialog'); dialog.className = 'record-dialog';
  dialog.innerHTML = '<div class="dialog-actions"><button class="button ghost small audit-close">关闭</button></div><h2>实际请求证据</h2><p class="source-note">正文仅在开启归档时可用；展示前已移除应用凭据。</p><pre class="audit-content"></pre><div class="panel-tools"><button class="button ghost audit-body">查看正文</button><button class="button ghost audit-export">导出元数据</button><button class="button ghost audit-export-body">导出含正文副本</button></div>';
  document.body.append(dialog);
  const $ = name => panel.querySelector(name), d = name => dialog.querySelector(name);
  let selected = null, generation = 0;
  async function show(id, body = false) {
    selected = id; const epoch = ++generation;
    d('pre').textContent = '正在读取…'; if (!dialog.open) dialog.showModal();
    const record = await client.request(`/api/audit/${id}?include_content=${body}`);
    if (epoch !== generation) return;
    d('pre').textContent = JSON.stringify(record, null, 2);
    d('.audit-body').disabled = record.metadata.content_status !== 'saved';
    d('.audit-export-body').disabled = record.metadata.content_status !== 'saved';
  }
  async function refresh() {
    const trace = $('#audit-trace').value.trim();
    const rows = await client.request('/api/audit' + (trace ? '?trace_id=' + encodeURIComponent(trace) : ''));
    $('#audit-list').replaceChildren();
    for (const row of rows) {
      const button = document.createElement('button'); button.className = 'archive-record';
      const labels = { saved: '正文已存', disabled: '仅元数据', size_limit: '正文超出上限', source_deleted: '来源已删除' };
      button.textContent = `${row.purpose} · ${row.metadata.model} · ${labels[row.metadata.content_status]} · ${row.metadata.outcome}\n${row.created_at} · trace ${row.trace_id}`;
      button.addEventListener('click', () => busy(button, () => show(row.id))); $('#audit-list').append(button);
    }
    if (!rows.length) $('#audit-list').textContent = '还没有匹配的请求快照。';
  }
  async function download(body) {
    if (body && !await confirmAction('导出包含正文的审计副本？', '副本可能包含历史对话和 Skill 正文。导出后不会随应用内的删除操作自动清除，请选择合适的保存位置。')) return;
    const data = await client.request(`/api/audit/${selected}/export`, { method: 'POST', body: { include_content: body } });
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
    const link = document.createElement('a'); link.href = url; link.download = `greatsage-audit-${selected}.json`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000); toast('审计副本已交给下载器保存。');
  }
  $('#audit-refresh').addEventListener('click', () => busy($('#audit-refresh'), refresh));
  $('#audit-trace').addEventListener('keydown', event => { if (event.key === 'Enter') busy($('#audit-refresh'), refresh); });
  d('.audit-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { generation++; selected = null; d('pre').textContent = ''; });
  d('.audit-body').addEventListener('click', () => busy(d('.audit-body'), () => show(selected, true)));
  for (const [selector, body] of [['.audit-export', false], ['.audit-export-body', true]]) d(selector).addEventListener('click', () => busy(d(selector), () => download(body)));
  let timer;
  return { refresh, onEvent(event) {
    if (event.kind === 'memory_updated' && dialog.open && selected) show(selected).catch(error => { d('pre').textContent = error.message; });
    if (['request_snapshot', 'response_done', 'memory_updated', 'compression_done'].includes(event.kind)) {
      clearTimeout(timer); timer = setTimeout(() => refresh().catch(() => {}), 400);
    }
  } };
}
