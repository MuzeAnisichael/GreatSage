export function setupMCP({ client, busy, toast }) {
  const panel = document.createElement('section'); panel.className = 'panel audit-panel';
  panel.innerHTML = '<div class="panel-heading"><h2>外部工具 · MCP</h2><button type="button" class="button ghost small" id="mcp-disconnect">停用已发现工具</button></div><p class="panel-hint">连接你选择的 Streamable HTTP 服务，点击“发现工具”后才供秘书使用。每次外部调用需要在控制台确认；重启后需要重新发现。</p><div class="panel-body"><form id="mcp-form" class="form-grid"><label class="field"><span>服务 ID</span><input id="mcp-id" required pattern="[a-z][a-z0-9_]{0,19}" placeholder="例如 local_tools" maxlength="20" /></label><label class="field"><span>服务地址</span><input id="mcp-url" type="url" required placeholder="http://127.0.0.1:8000/mcp" /></label><label class="field"><span>凭据环境变量（可选）</span><input id="mcp-token-env" placeholder="例如 MY_MCP_TOKEN；不填写密钥本身" /></label><div><button type="submit" class="button ghost">保存服务</button></div></form><div id="mcp-list"></div></div>';
  document.querySelector('#view-settings').append(panel);
  const $ = selector => panel.querySelector(selector);
  const button = (text, run) => { const node = document.createElement('button'); node.type = 'button'; node.className = 'button ghost small'; node.textContent = text; node.onclick = () => busy(node, run); return node; };
  async function refresh() {
    const servers = await client.request('/api/mcp/servers');
    $('#mcp-list').replaceChildren();
    for (const server of servers) {
      const row = document.createElement('article'); row.className = 'record-card';
      const label = document.createElement('p'); label.textContent = `${server.id} · ${server.url} · ${server.tools.length ? `已发现 ${server.tools.length} 个工具` : '尚未发现工具'}`;
      const tools = document.createElement('p'); tools.className = 'source-note'; tools.textContent = server.tools.map(item => item.title).join('、');
      row.append(label, tools, button('发现工具', async () => { await client.request(`/api/mcp/servers/${encodeURIComponent(server.id)}/discover`, { method: 'POST' }); await refresh(); toast('工具已发现，每次调用仍需确认。'); }), button('移除服务', async () => {
        const settings = await client.request('/api/settings');
        await client.request('/api/settings', { method: 'PUT', body: { mcp_servers: settings.mcp_servers.filter(item => item.id !== server.id) } });
        await refresh();
      }));
      $('#mcp-list').append(row);
    }
    if (!servers.length) $('#mcp-list').textContent = '尚未配置外部工具服务。';
  }
  $('#mcp-form').onsubmit = event => {
    event.preventDefault();
    busy($('#mcp-form button'), async () => {
      const settings = await client.request('/api/settings');
      const server = { id: $('#mcp-id').value.trim(), url: $('#mcp-url').value.trim(), token_env: $('#mcp-token-env').value.trim() };
      const servers = settings.mcp_servers.filter(item => item.id !== server.id);
      await client.request('/api/settings', { method: 'PUT', body: { mcp_servers: [...servers, server] } });
      await refresh(); toast('服务已保存，点击“发现工具”开始接入。');
    });
  };
  $('#mcp-disconnect').onclick = () => busy($('#mcp-disconnect'), async () => {
    await client.request('/api/mcp/disconnect', { method: 'POST' }); await refresh(); toast('外部工具已停用。');
  });
  return { refresh };
}
