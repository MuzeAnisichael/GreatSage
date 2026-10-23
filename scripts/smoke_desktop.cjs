'use strict';

// Run: node_modules/electron/dist/electron.exe scripts/smoke_desktop.cjs [--live] [--parallel-sources]
// Default: isolated local fixtures only. --live sends one short synthetic request
// to the configured LLM and tests muted Windows speech. Never opens a microphone.
// Results and screenshots remain in ignored .runtime/desktop-smoke/<run>/.
if (!process.versions.electron) {
  console.error('Run this script with the installed Electron executable, not Node.js.');
  process.exit(2);
}
const { app, BrowserWindow } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const childProcess = require('node:child_process');
const live = process.argv.includes('--live');
const parallelSources = process.argv.includes('--parallel-sources');
const packaged = process.argv.includes('--packaged');
const packageResources = path.resolve(__dirname, '..', 'release', 'win-unpacked', 'resources');
if (packaged) {
  Object.defineProperty(app, 'isPackaged', { value: true });
  Object.defineProperty(process, 'resourcesPath', { value: packageResources });
}
const runDir = path.resolve(__dirname, '..', '.runtime', 'desktop-smoke', new Date().toISOString().replace(/[:.]/g, '-'));
fs.mkdirSync(runDir, { recursive: true });
app.setPath('userData', path.join(runDir, 'electron-profile'));
process.env.GREATSAGE_DATA_DIR = path.join(runDir, 'data');
const reports = [];
const children = [];
const originalSpawn = childProcess.spawn;
childProcess.spawn = function (...args) {
  const child = originalSpawn.apply(this, args);
  if (String(args[0]).endsWith('greatsage-backend.exe') || Array.isArray(args[1]) && args[1].includes('greatsage')) children.push(child);
  return child;
};
// Instrument only this test's Electron process; leave user apps untouched.
BrowserWindow.prototype.show = function () {};
BrowserWindow.prototype.focus = function () {};
app.on('browser-window-created', (_event, window) => {
  window.setSkipTaskbar(true);
  window.webContents.setBackgroundThrottling(false);
  window.webContents.setAudioMuted(true);
  window.webContents.on('did-finish-load', () => {
    window.webContents.insertCSS('*{animation:none!important;transition:none!important}').catch(() => {});
  });
});
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(action, label, timeout = 30000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await action()) return;
    await sleep(100);
  }
  throw new Error(`Timed out: ${label}`);
}
function check(value, label) {
  reports.push({ check: label, passed: Boolean(value) });
  if (!value) throw new Error(label);
}
async function screenshot(window, name) {
  window.webContents.invalidate();
  await window.webContents.capturePage(undefined, { stayHidden: true, stayAwake: true });
  await sleep(160);
  fs.writeFileSync(path.join(runDir, name), (await window.webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG());
}
let token = '';
let request;
let originalSettings;
let completed = false;
function redact(value) {
  return String(value).replaceAll(token || '<no-token>', '[redacted]').replace(/sk-[\w-]+/g, '[redacted]').replace(/Bearer\s+[^\s"']+/gi, 'Bearer [redacted]');
}
app.on('will-quit', event => {
  const childrenStopped = children.length > 0 && children.every(child => child.exitCode !== null || child.signalCode !== null);
  reports.push({ check: 'Python child stopped on quit', passed: childrenStopped });
  const result = { success: completed && reports.every(report => report.passed), live, parallelSources, packaged, reports };
  fs.writeFileSync(path.join(runDir, 'result.json'), JSON.stringify(result, null, 2));
  console.log(`${result.success ? 'PASS' : 'FAIL'}: ${path.join(runDir, 'result.json')}`);
  process.exitCode = result.success ? 0 : 1;
  if (!result.success) { event.preventDefault(); app.exit(1); }
});
require(packaged ? path.join(packageResources, 'app.asar', 'desktop', 'main.cjs') : '../desktop/main.cjs');

(async () => {
  let main;
  try {
    await app.whenReady();
    await until(() => {
      main = BrowserWindow.getAllWindows().find(window => /^http:\/\/127\.0\.0\.1:\d+\/$/.test(window.webContents.getURL()));
      return Boolean(main);
    }, 'desktop startup', 45000);
    const evaluate = code => main.webContents.executeJavaScript(code, true);
    await until(() => evaluate("document.querySelector('#connection-label')?.textContent === '本地服务已连接' && document.querySelector('#model-stat')?.textContent !== '—'"), 'settings and WebSocket');
    const connection = await evaluate('window.greatsage.getConnection()');
    token = connection.token;
    request = async (url, method = 'GET', body) => {
      const response = await fetch(connection.baseUrl + url, {
        method, headers: { Authorization: `Bearer ${token}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(30000),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}: ${url}`);
      return response.json();
    };
    originalSettings = await request('/api/settings');
    if (!live) await request('/api/settings', 'PUT', { llm: { base_url: 'http://127.0.0.1:1/v1', api_key_env: '' }, embedding: { enabled: false } });
    check((await request('/api/status')).listening === false, 'Listening starts disabled');
    await until(() => evaluate("!document.querySelector('#refresh-sources').disabled"), 'initial audio device enumeration');
    check(await evaluate("document.querySelector('#quick-microphone-device').options.length >= 1 && document.querySelector('#quick-process').options.length >= 1 && !document.querySelector('.toast.error')"), 'Audio sources finish loading without errors');
    if (parallelSources) {
      const results = await Promise.all([request('/api/audio/sources'), request('/api/audio/sources')]);
      check(results.every(result => Array.isArray(result.microphones) && Array.isArray(result.processes)) && (await request('/api/status')).listening === false, 'Concurrent source enumeration keeps backend healthy');
    }
    check(await evaluate(`document.querySelector('#quick-microphone').checked === ${Boolean(originalSettings.microphone)} && document.querySelector('#quick-desktop-source').value === ${JSON.stringify(originalSettings.desktop_source)}`), 'Microphone and desktop sources stay separate');
    {
      // Synthetic health responses exercise recovery controls without opening any
      // device or calling a speech service, for source and final packaged UI alike.
      // All other routes use the real backend.
      await evaluate(`(async () => {
        const { SageClient } = await import('./client.js');
        window.__voiceSmoke = { originalFetch: window.fetch, calls: [], status: { listening: true, sources: [
          { source: 'microphone:default', state: 'failed', streaming: true, pending_frames: 0, error_code: 'streaming_asr_connection_closed', recovery_action: 'retry_source' },
          { source: 'system', state: 'running', streaming: true, pending_frames: 2, error_code: null, recovery_action: null }
        ] } };
        window.fetch = async (url, options = {}) => {
          const fixture = window.__voiceSmoke, route = new URL(url, location.href).pathname;
          if (route === '/api/audio/status' || route === '/api/audio/retry') {
            if (route.endsWith('/retry')) {
              fixture.calls.push({ route, body: JSON.parse(options.body), method: options.method });
              fixture.status.sources[0] = { ...fixture.status.sources[0], state: 'running', error_code: null, recovery_action: null };
            }
            return new Response(JSON.stringify(fixture.status), { headers: { 'Content-Type': 'application/json' } });
          }
          if (route === '/api/listening') fixture.calls.push({ route });
          return fixture.originalFetch.call(window, url, options);
        };
        const connect = SageClient.prototype.connect;
        SageClient.prototype.connect = function () {
          window.__voiceSmoke.client = this;
          SageClient.prototype.connect = connect;
          return connect.call(this);
        };
        window.__voiceSmoke.send = (kind, data) => window.__voiceSmoke.client.dispatchEvent(new CustomEvent('event', {
          detail: { id: crypto.randomUUID(), kind, data, created_at: new Date().toISOString() }
        }));
        document.querySelector('#reconnect').click();
      })()`);
      await until(() => evaluate("Boolean(document.querySelector('#voice-health [data-action=retry_source]')) && !document.querySelector('#reconnect').disabled"), 'recoverable voice failure');
      check(await evaluate("document.querySelector('#connection-label').textContent === '本地服务已连接' && document.querySelector('#toasts').textContent.indexOf('无法连接') === -1"), 'Voice fixtures preserve real backend connection');
      check(await evaluate("document.querySelector('#voice-health').textContent.includes('语音识别连接已断开') && document.querySelector('#voice-health [data-source=system]').dataset.state === 'running'"), 'Failed voice source has persistent explanation beside healthy source');
      await evaluate("document.querySelector('#voice-health').scrollIntoView({ block: 'center' })");
      await screenshot(main, 'voice-recovery.png');
      await evaluate("document.querySelector('#voice-health [data-action=retry_source]').click()");
      await until(() => evaluate("document.querySelector('#voice-health [data-source=\"microphone:default\"]').dataset.state === 'running'"), 'source retry health');
      check(await evaluate("window.__voiceSmoke.calls.length === 1 && window.__voiceSmoke.calls[0].method === 'POST' && window.__voiceSmoke.calls[0].body.source === 'microphone:default' && document.querySelector('#voice-health [data-source=system]').dataset.state === 'running' && window.__voiceSmoke.status.sources[1].pending_frames === 2"), 'Source retry sends exact source without global restart or changing other source');
      await evaluate("window.__voiceSmoke.status.sources[0] = { ...window.__voiceSmoke.status.sources[0], speech_phase: 'finalizing', pending_turns: 2 };window.__voiceSmoke.send('voice_turn', { source: 'microphone:default', phase: 'finalizing', pending_turns: 2 })");
      await until(() => evaluate("document.querySelector('#voice-health').textContent.includes('正在确认转写') && document.querySelector('#voice-health').textContent.includes('2 段语音等待确认')"), 'pending natural voice feedback');
      check(true, 'Pending natural voice is shown separately from active capture');
      await evaluate(`(() => {
        const send = window.__voiceSmoke.send;
        send('transcript', { source: 'microphone:default', turn_id: 'fixture-a', text: '上一句临时文字', final: false });
        send('transcript', { source: 'microphone:default', turn_id: 'fixture-b', text: '下一句仍在识别', final: false });
        send('transcript', { source: 'microphone:default', turn_id: 'fixture-a', text: '上一句已经确认', final: true });
        send('transcript', { source: 'microphone:default', turn_id: 'fixture-a', text: '旧的临时文字', final: false });
      })()`);
      check(await evaluate("document.querySelectorAll('.transcript-item').length === 2 && document.querySelector('[data-turn=fixture-a] .transcript-text').textContent === '上一句已经确认' && document.querySelector('[data-turn=fixture-a]').dataset.final === 'true' && document.querySelector('[data-turn=fixture-b]').classList.contains('interim') && document.querySelector('[data-turn=fixture-b] .transcript-text').textContent === '下一句仍在识别'"), 'Late final keeps the next turn interim and cannot revert to old partial');
      await evaluate("window.__voiceSmoke.send('audio_source', { source: 'microphone:default', kind: 'recognition_retry' })");
      check(await evaluate("document.querySelector('[data-turn=fixture-b]').classList.contains('incomplete') && document.querySelector('[data-turn=fixture-b] .transcript-status').textContent === '未完成，未提交' && document.querySelector('[data-turn=fixture-a]').dataset.final === 'true'"), 'Source retirement marks only unfinished transcripts as unsubmitted');
      await evaluate("window.__voiceSmoke.send('session', { session_id: 'fixture-voice-session' })");
      check(await evaluate("document.querySelectorAll('.transcript-item').length === 0 && Boolean(document.querySelector('#transcript-empty'))"), 'Session event clears prior live transcripts');
      await evaluate("window.__voiceSmoke.status.sources[0] = { ...window.__voiceSmoke.status.sources[0], state: 'stopped', error_code: 'voice_source_stopped', recovery_action: 'restart_listening' };document.querySelector('#reconnect').click()");
      await until(() => evaluate("Boolean(document.querySelector('#voice-health [data-action=restart_listening]')) && !document.querySelector('#reconnect').disabled"), 'capture stopped guidance');
      await evaluate("document.querySelector('#voice-health [data-action=restart_listening]').click()");
      check(await evaluate("window.__voiceSmoke.calls.length === 1 && document.activeElement.id === 'toggle-listening' && document.querySelector('#voice-health').textContent.includes('设备连接')"), 'Stopped capture guides device check without claiming automatic recovery');
      await evaluate("window.__voiceSmoke.status.sources[0] = { ...window.__voiceSmoke.status.sources[0], state: 'failed', error_code: 'streaming_asr_credentials_required', recovery_action: 'configure_asr' };document.querySelector('#reconnect').click()");
      await until(() => evaluate("Boolean(document.querySelector('#voice-health [data-action=configure_asr]')) && !document.querySelector('#reconnect').disabled"), 'voice settings guidance');
      await evaluate("document.querySelector('#voice-health [data-action=configure_asr]').click()");
      check(await evaluate("!document.querySelector('#view-settings').hidden && document.activeElement.name === 'asr.provider' && window.__voiceSmoke.calls.length === 1"), 'Missing speech credentials open recognition settings without retry');
      await evaluate("window.fetch = window.__voiceSmoke.originalFetch;delete window.__voiceSmoke;document.querySelector('[data-view=conversation]').click();document.querySelector('#reconnect').click()");
      await until(() => evaluate("!document.querySelector('#reconnect').disabled && document.querySelector('#voice-health-note').textContent.includes('尚未聆听')"), 'real voice status restored');
      check((await request('/api/status')).listening === false, 'Voice recovery fixtures never enabled real capture');
      await evaluate("document.querySelector('#toasts').replaceChildren()");
    }
    check(await evaluate('document.body.scrollWidth <= document.documentElement.clientWidth + 1'), 'Console has no horizontal overflow');
    const sessionId = (await request('/api/status')).session_id;
    const fixtureCode = `import json,sys\nfrom pathlib import Path\nfrom greatsage.memory import MemoryStore\ndata=json.load(sys.stdin)\nstore=MemoryStore(Path(data['directory']))\nolder_session=store.new_session()\nolder=store.add_message('user','SYNTHETIC OLDER SOURCE FOR DESKTOP SEARCH',session_id=older_session)\nsummary=store.save_summary('SYNTHETIC READ ONLY DESKTOP SUMMARY',[older['id']],model='smoke-fixture')\nfor index in range(115): store.add_message('observation','SYNTHETIC FILLER '+str(index),source='system',session_id=older_session)\nsource=store.add_message('user','SYNTHETIC CASCADE SOURCE',session_id=data['session'])\nstore.add_message('assistant','SYNTHETIC CASCADE DERIVED ANSWER',session_id=data['session'],metadata={'source_ids':[source['id']]})\nstore.close()\nprint(json.dumps({'older_id':older['id'],'summary_id':summary['id'],'source_id':source['id']}))`;
    const v02Fixture = fixtureCode.replace('store.close()', "old=store.add_memory('SYNTHETIC preference A')\nstore.add_memory('SYNTHETIC preference B',conflicts=[old['id']])\nstore.save_snapshot('fixture-audit-trace','fixture_audit',{'llm':{'model':'fixture'}},[{'role':'user','content':'SYNTHETIC PRIVATE REQUEST BODY'}],[source['id']],retain_content=True)\nstore.close()");
    const structuredFixture = `def add_structured(text,subject,predicate,value,kind='fact',policy='mostly',object_type='literal'):
 record=store.add_message('user',text,session_id=data['session'])
 snapshot=next(item for item in store.extraction_sources() if item['id']==record['id'])
 saved=store.save_extraction([{'text':text,'kind':kind,'subject':subject,'subject_type':'person','predicate':predicate,'value':value,'object_type':object_type,'evidence':[{'source_id':record['id'],'quote':text}],'review_reasons':[]}],[snapshot],policy,'smoke-fixture')
 return {'memory_id':saved['items'][0]['id'],'source_id':record['id']}
relation=add_structured('SYNTHETIC Alice <img src=x onerror=window.__memoryUnsafe=true> belongs to SYNTHETIC Orion.','SYNTHETIC Alice','member_of','SYNTHETIC Orion','relationship',object_type='project')
rejected=add_structured('SYNTHETIC UI rejected fact.','SYNTHETIC Rejected','note','rejected')
orphan=add_structured('SYNTHETIC UI source deletion fact.','SYNTHETIC Orphan','note','source deletion')
active=add_structured('SYNTHETIC UI language English.','SYNTHETIC Settings','language','English','preference','auto')
conflict=add_structured('SYNTHETIC UI language Chinese.','SYNTHETIC Settings','language','Chinese','preference')
`;
    const v06Fixture = v02Fixture.replace('store.close()', structuredFixture + 'store.close()').replace("'source_id':source['id']", "'source_id':source['id'],'relation':relation,'rejected':rejected,'orphan':orphan,'conflict':conflict");
    const fixture = childProcess.spawnSync(path.resolve(__dirname, '..', '.venv', 'Scripts', 'python.exe'), ['-c', v06Fixture], { cwd: path.resolve(__dirname, '..'), input: JSON.stringify({ directory: process.env.GREATSAGE_DATA_DIR, session: sessionId }), encoding: 'utf8', windowsHide: true, timeout: 15000 });
    if (fixture.status !== 0) throw new Error(`Memory fixture failed: ${redact(fixture.stderr)}`);
    const fixtureIds = JSON.parse(fixture.stdout);
    await new Promise(resolve => { main.webContents.once('did-finish-load', resolve); main.webContents.reload(); });
    await until(() => evaluate("document.querySelector('#connection-label')?.textContent === '本地服务已连接' && document.querySelector('#chat-messages')?.textContent.includes('SYNTHETIC CASCADE DERIVED ANSWER')"), 'fixture history reload');
    await evaluate("document.querySelector('[data-view=settings]').click();document.querySelector('#settings-form').requestSubmit(document.querySelector('#save-settings'))");
    await until(() => evaluate("document.querySelector('#toasts').textContent.includes('设置已保存')"), 'settings save');
    check(true, 'Settings load and save through the UI');
    check(await evaluate("!document.querySelector('[name=memory_extraction_enabled]').checked && document.querySelector('[name=memory_confirmation]').value === 'mostly'"), 'Memory extraction defaults off with mostly confirmation');
    for (const policy of ['necessary', 'auto', 'mostly']) {
      await evaluate(`document.querySelector('[name=memory_confirmation]').value=${JSON.stringify(policy)};document.querySelector('#settings-form').requestSubmit(document.querySelector('#save-settings'))`);
      await until(async () => !await evaluate("document.querySelector('#save-settings').disabled") && (await request('/api/settings')).memory_confirmation === policy, `memory confirmation ${policy}`);
      check(!(await request('/api/settings')).memory_extraction_enabled, `Saving ${policy} policy preserves disabled extraction`);
    }
    await evaluate("document.querySelector('#mcp-id').value='fixture';document.querySelector('#mcp-url').value='http://127.0.0.1:1/mcp';document.querySelector('#mcp-form').requestSubmit()");
    await until(async () => (await request('/api/settings')).mcp_servers.length === 1, 'MCP settings save');
    await evaluate("document.querySelector('#settings-form').requestSubmit(document.querySelector('#save-settings'))");
    await until(() => evaluate("!document.querySelector('#save-settings').disabled"), 'general settings save after MCP');
    check((await request('/api/settings')).mcp_servers.length === 1 && (await request('/api/mcp/servers'))[0].tools.length === 0, 'MCP config survives general settings save without auto-connection');

    // A fixture is generated inside this run; no personal Skill directory is read.
    const fixtureDir = path.join(runDir, 'fixture-skill');
    fs.mkdirSync(fixtureDir);
    fs.writeFileSync(path.join(fixtureDir, 'SKILL.md'), '---\nname: desktop-smoke-fixture\ndescription: Synthetic local desktop smoke-test fixture.\n---\nUse concise answers for synthetic desktop tests.\n');
    await evaluate(`document.querySelector('[data-view=skills]').click();document.querySelector('#skill-path').value=${JSON.stringify(fixtureDir)};document.querySelector('#skill-form').requestSubmit()`);
    await until(() => evaluate("[...document.querySelectorAll('.skill-card')].some(node=>node.textContent.includes('desktop-smoke-fixture'))"), 'fixture Skill import');
    check(true, 'Fixture SKILL.md imports and renders');

    const materialDir = path.join(runDir, 'fixture-materials');
    fs.mkdirSync(materialDir);
    fs.writeFileSync(path.join(materialDir, 'plan.md'), '# 发布计划\n\n## 时间\nSYNTHETIC 发布时间需要在周五前确认。\n');
    await evaluate(`document.querySelector('[data-view=materials]').click();document.querySelector('#material-path').value=${JSON.stringify(materialDir)};document.querySelector('#material-form').requestSubmit()`);
    await until(() => evaluate("[...document.querySelectorAll('.material-row')].some(node=>node.textContent.includes('plan.md') && node.textContent.includes('v1'))"), 'fixture material import');
    check(true, 'Fixture Markdown material imports with version and chunks');
    await evaluate("document.querySelector('[data-view=tasks]').click();document.querySelector('#new-task').click()");
    await until(() => evaluate("document.querySelector('.task-dialog').open && document.querySelectorAll('.task-dialog [name=material]').length === 1"), 'new task dialog');
    await evaluate("document.querySelector('.task-dialog button[value=cancel]').click()");
    check(true, 'New task dialog lists imported materials');
    await evaluate("document.querySelector('[data-view=settings]').click();document.querySelector('[name=voice_approval]').click()");
    await until(() => evaluate("document.querySelector('#confirm-dialog').open && document.querySelector('#confirm-title').textContent.includes('语音确认')"), 'voice approval warning');
    await evaluate("document.querySelector('#confirm-dialog button[value=cancel]').click()");
    check(!(await evaluate("document.querySelector('[name=voice_approval]').checked")), 'Voice approval stays off unless its warning is accepted');

    const marker = 'SYNTHETIC DESKTOP SMOKE: disposable test memory.';
    const revised = 'SYNTHETIC DESKTOP SMOKE: revised disposable test memory.';
    const card = value => `[...document.querySelectorAll('.memory-card')].find(node=>node.querySelector('.memory-text').textContent===${JSON.stringify(value)})`;
    await evaluate(`document.querySelector('[data-view=memory]').click();document.querySelector('#memory-input').value=${JSON.stringify(marker)};document.querySelector('#memory-form').requestSubmit()`);
    await until(() => evaluate(`Boolean(${card(marker)})`), 'fixture memory');
    await evaluate(`${card(marker)}.querySelector('.record-actions button').click()`);
    await until(() => evaluate("Boolean(document.querySelector('.edit-dialog[open]'))"), 'memory edit dialog');
    await evaluate(`document.querySelector('.edit-dialog textarea').value=${JSON.stringify(revised)};document.querySelector('.edit-dialog form').requestSubmit(document.querySelector('.edit-dialog button[value=save]'))`);
    await until(() => evaluate(`Boolean(${card(revised)})`), 'memory revision');
    await evaluate(`${card(revised)}.querySelector('.record-actions button:last-child').click()`);
    await until(() => evaluate("document.querySelector('#confirm-dialog').open"), 'memory delete dialog');
    await evaluate("document.querySelector('#confirm-dialog button[value=confirm]').click()");
    await until(() => evaluate(`!(${card(revised)})`), 'fixture memory removal');
    check(true, 'Synthetic memory add, revise and delete through the UI');
    await until(() => evaluate("document.querySelectorAll('.conflict-card').length === 2"), 'pending memory conflicts');
    await evaluate("[...document.querySelectorAll('.conflict-card')].find(node=>node.textContent.includes('SYNTHETIC preference B')).querySelector('.panel-tools button:last-child').click()");
    await until(() => evaluate("document.querySelectorAll('.conflict-card').length === 1"), 'resolve explicit memory conflict');
    check((await request('/api/memories')).filter(row => row.text.startsWith('SYNTHETIC preference')).length === 2, 'Conflict stays pending until explicit keep-both choice');
    await screenshot(main, 'memory-v02.png');
    await until(() => evaluate("document.querySelectorAll('.candidate-card').length === 4"), 'structured memory candidates');
    check(await evaluate("!document.querySelector('#memory-candidates img') && !window.__memoryUnsafe"), 'Extracted candidate text never becomes HTML');
    await evaluate("document.querySelector('#extract-current-session').click()");
    check(await evaluate("!document.querySelector('#view-settings').hidden && document.activeElement.name === 'memory_extraction_enabled'"), 'Manual extraction while disabled points to saved opt-in settings');
    await evaluate("document.querySelector('[data-view=memory]').click()");
    const candidate = id => `document.querySelector('.candidate-card[data-id="${id}"]')`;
    await evaluate(`${candidate(fixtureIds.relation.memory_id)}.querySelector('button').click()`);
    await until(() => evaluate("document.querySelector('#record-dialog').open && Boolean(document.querySelector('#record-detail .evidence-quote'))"), 'structured original evidence');
    check(await evaluate("document.querySelector('#record-detail .evidence-quote').textContent.includes('<img') && !document.querySelector('#record-detail img') && document.querySelector('#record-detail').textContent.includes('smoke-fixture')"), 'Evidence shows literal quote, provenance and extraction model');
    await evaluate("document.querySelector('#record-close').click()");
    await evaluate(`${candidate(fixtureIds.relation.memory_id)}.querySelector('.primary').click()`);
    await until(() => evaluate(`!${candidate(fixtureIds.relation.memory_id)} && Boolean(document.querySelector('.memory-card[data-id="${fixtureIds.relation.memory_id}"]'))`), 'candidate accepted into existing memory controls');
    await evaluate(`${candidate(fixtureIds.rejected.memory_id)}.querySelector('button:last-child').click()`);
    await until(() => evaluate(`!${candidate(fixtureIds.rejected.memory_id)}`), 'candidate rejected');
    check(!(await request('/api/memories')).some(row => row.id === fixtureIds.rejected.memory_id), 'Rejected extraction stays out of active memories');
    await evaluate("[...document.querySelectorAll('.entity-card')].find(node=>node.querySelector('strong').textContent==='SYNTHETIC Alice').click()");
    await until(() => evaluate("document.querySelector('#entity-dialog').open && document.querySelector('#entity-detail').textContent.includes('member_of')"), 'entity relationships');
    check(await evaluate("document.querySelector('#entity-detail').textContent.includes('已生效') && document.querySelector('#entity-detail').textContent.includes('SYNTHETIC Orion')"), 'Entity detail distinguishes active relationship and target entity');
    const entityRows = await request('/api/memory/entities');
    await evaluate(`(async () => {
      const { SageClient } = await import('./client.js');
      window.__entityRace = { original: SageClient.prototype.request, client: SageClient, releases: [], items: ${JSON.stringify(entityRows)} };
      SageClient.prototype.request = function (route, options) {
        const fixture = window.__entityRace;
        if (route.startsWith('/api/memory/entities?')) return new Promise(resolve => fixture.releases.push(resolve));
        return fixture.original.call(this, route, options);
      };
      document.querySelector('#refresh-memory').click();
    })()`);
    await until(() => evaluate("window.__entityRace.releases.length > 0"), 'entity refresh held before close');
    await evaluate(`(async () => {
      const fixture = window.__entityRace;
      [...document.querySelectorAll('#entity-detail button')].find(node=>node.textContent==='修正或删除').click();
      fixture.client.prototype.request = fixture.original;
      for (const release of fixture.releases) release(fixture.items);
      await Promise.resolve(); await Promise.resolve();
      fixture.reopened = document.querySelector('#entity-dialog').open;
    })()`);
    check(await evaluate("!window.__entityRace.reopened && !document.querySelector('#entity-dialog').open"), 'Closing entity detail invalidates refresh before the queued native close event');
    await until(() => evaluate("!document.querySelector('#refresh-memory').disabled"), 'entity refresh completes after close');
    check(await evaluate("!document.querySelector('#entity-dialog').open && !document.querySelector('#entity-detail').children.length"), 'Late entity refresh cannot reopen or refill a closed dialog');
    await evaluate("delete window.__entityRace");
    check(await evaluate(`!document.querySelector('#entity-dialog').open && document.activeElement.closest('.memory-card')?.dataset.id === ${JSON.stringify(fixtureIds.relation.memory_id)}`), 'Entity mutation links reuse existing memory controls');
    check(await evaluate(`!${candidate(fixtureIds.conflict.memory_id)}.querySelector('.primary')`), 'Conflicting candidate cannot bypass conflict resolution');
    await evaluate(`${candidate(fixtureIds.conflict.memory_id)}.querySelector('button:nth-child(2)').click()`);
    await until(() => evaluate(`document.activeElement.closest('.conflict-card')?.dataset.id === ${JSON.stringify(fixtureIds.conflict.memory_id)}`), 'candidate directs to existing conflict resolution');
    await evaluate(`document.querySelector('.conflict-card[data-id="${fixtureIds.conflict.memory_id}"] .panel-tools button:last-child').click()`);
    await until(() => evaluate(`!${candidate(fixtureIds.conflict.memory_id)} && document.querySelectorAll('.conflict-card').length === 0`), 'structured conflict resolved');
    await request(`/api/history/${fixtureIds.orphan.source_id}`, 'DELETE');
    await until(() => evaluate(`!${candidate(fixtureIds.orphan.memory_id)}`), 'deleted source removes visible candidate');
    check((await request('/api/memory/candidates')).length === 0, 'Accepted, rejected and source-deleted candidates clear after refresh');
    await screenshot(main, 'memory-v06.png');
    // Opt-in state and extraction responses are simulated in this renderer only.
    // The real backend remains disabled, so this never sends history to a model.
    await evaluate(`(async () => {
      const { SageClient } = await import('./client.js');
      window.__memorySmoke = { originalFetch: window.fetch, calls: [], status: { enabled: true, confirmation: 'mostly', model: 'fixture-extractor', phase: 'idle', eligible_sources: 7, processed_sources: 5, pending_sources: 2, active: 3, pending: 0, conflicts: 0, background: {} } };
      window.fetch = async (url, options = {}) => {
        const fixture = window.__memorySmoke, route = new URL(url, location.href).pathname;
        if (route === '/api/memory/extraction') {
          if (options.method === 'POST') { fixture.calls.push(options.method); fixture.status.phase = 'queued'; fixture.status.scheduled = true; }
          return new Response(JSON.stringify(fixture.status), { headers: { 'Content-Type': 'application/json' } });
        }
        const response = await fixture.originalFetch.call(window, url, options);
        if (route !== '/api/settings') return response;
        return new Response(JSON.stringify({ ...await response.json(), memory_extraction_enabled: true }), { headers: { 'Content-Type': 'application/json' } });
      };
      const connect = SageClient.prototype.connect;
      SageClient.prototype.connect = function () { window.__memorySmoke.client = this; SageClient.prototype.connect = connect; return connect.call(this); };
      document.querySelector('#reconnect').click();
    })()`);
    await until(() => evaluate("!document.querySelector('#reconnect').disabled && document.querySelector('#extraction-status').textContent.includes('fixture-extractor')"), 'synthetic enabled extraction status');
    await evaluate("document.querySelector('#extract-current-session').click()");
    await until(() => evaluate("window.__memorySmoke.calls.length === 1 && document.querySelector('#extraction-status').textContent.includes('等待后台处理')"), 'nonblocking manual extraction request');
    await evaluate("window.__memorySmoke.status.phase='failed';window.__memorySmoke.status.error='timeout';window.__memorySmoke.client.dispatchEvent(new CustomEvent('event',{detail:{id:crypto.randomUUID(),kind:'memory_extraction',data:{phase:'failed'},created_at:new Date().toISOString()}}))");
    await until(() => evaluate("!document.querySelector('#extraction-error').hidden && document.querySelector('#extraction-error').textContent.includes('超时')"), 'extraction event status and error');
    check(!(await request('/api/settings')).memory_extraction_enabled, 'Renderer extraction fixtures never enable backend model calls');
    await evaluate("window.fetch=window.__memorySmoke.originalFetch;delete window.__memorySmoke;document.querySelector('#reconnect').click()");
    await until(() => evaluate("!document.querySelector('#reconnect').disabled && document.querySelector('#extraction-status').textContent.includes('已关闭')"), 'real extraction state restored');
    if (!live) {
      const task = await request('/api/tasks', 'POST', { title: 'SYNTHETIC GRAPH INSPECTION', use_session: true });
      await until(async () => ['failed', 'succeeded'].includes((await request(`/api/tasks/${task.id}`)).status), 'graph fixture completion');
      const inspection = await request('/api/inspector');
      check(inspection.tasks.some(row => row.id === task.id && row.graph.checkpoint_id && row.graph.engine === 'langgraph'), 'Task graph checkpoints appear in inspector');
    }
    await evaluate("document.querySelector('[data-view=inspector]').click()");
    await until(() => evaluate("[...document.querySelector('#inspector-request').options].some(option=>option.textContent.includes('fixture_audit'))"), 'inspector requests');
    await evaluate("const select=document.querySelector('#inspector-request');select.value=[...select.options].find(option=>option.textContent.includes('fixture_audit')).value;select.dispatchEvent(new Event('change'));document.querySelector('#toasts').replaceChildren()");
    check(await evaluate("document.querySelectorAll('#inspector-messages tr').length===1 && !document.querySelector('#view-inspector').textContent.includes('SYNTHETIC PRIVATE REQUEST BODY')"), 'Inspector displays message metadata without archived body');
    check(await evaluate('document.body.scrollWidth <= document.documentElement.clientWidth + 1'), 'Inspector has no horizontal overflow');
    await screenshot(main, 'inspector-v04.png');
    await evaluate("document.querySelector('[data-view=logs]').click();document.querySelector('#audit-refresh').click()");
    await until(() => evaluate("[...document.querySelectorAll('#audit-list button')].some(node=>node.textContent.includes('fixture_audit'))"), 'audit fixture');
    await evaluate("[...document.querySelectorAll('#audit-list button')].find(node=>node.textContent.includes('fixture_audit')).click()");
    await until(() => evaluate("document.querySelector('.audit-content').textContent.includes('request_sha256')"), 'audit metadata');
    check(!await evaluate("document.querySelector('.audit-content').textContent.includes('SYNTHETIC PRIVATE REQUEST BODY')"), 'Audit opens metadata without request body');
    await evaluate("document.querySelector('.audit-body').click()");
    await until(() => evaluate("document.querySelector('.audit-content').textContent.includes('SYNTHETIC PRIVATE REQUEST BODY')"), 'audit opt-in body');
    check(true, 'Saved request body can be viewed explicitly');
    const exportPath = path.join(runDir, 'audit-metadata.json');
    const downloaded = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Audit download timed out')), 15000);
      main.webContents.session.once('will-download', (_event, item) => {
        item.setSavePath(exportPath);
        item.once('done', (_event, state) => { clearTimeout(timer); state === 'completed' ? resolve() : reject(new Error('Audit download ' + state)); });
      });
    });
    await evaluate("document.querySelector('.audit-export').click()");
    await downloaded;
    check(!fs.readFileSync(exportPath, 'utf8').includes('SYNTHETIC PRIVATE REQUEST BODY'), 'Metadata export downloads JSON without archived body');
    await screenshot(main, 'audit-v02.png');
    await evaluate("document.querySelector('.audit-close').click();document.querySelector('[data-view=memory]').click()");
    await until(() => evaluate("[...document.querySelectorAll('.summary-card')].some(node=>node.textContent.includes('SYNTHETIC READ ONLY DESKTOP SUMMARY'))"), 'read-only summary card');
    check(await evaluate("[...document.querySelectorAll('.summary-card')].every(node=>!node.querySelector('.record-actions'))"), 'Summaries expose no memory mutation controls');
    await evaluate(`document.querySelector('.summary-card [data-source-id="${fixtureIds.older_id}"]').click()`);
    await until(() => evaluate("document.querySelector('#record-dialog').open && document.querySelector('#record-detail').textContent.includes('SYNTHETIC OLDER SOURCE FOR DESKTOP SEARCH')"), 'source lookup beyond recent 100 messages');
    check(true, 'Summary source opens a source detail dialog');
    await evaluate("document.querySelector('#record-close').click();document.querySelector('.archive-panel').open=true");
    await until(() => evaluate("document.querySelectorAll('.archive-record').length >= 20"), 'archived session pagination');
    check(true, 'Old session originals can be paged');
    await evaluate("document.querySelector('#history-search').value='';document.querySelector('#history-search').dispatchEvent(new Event('input',{bubbles:true}))");
    check(await evaluate("!document.querySelector('#history-list').textContent.includes('SYNTHETIC OLDER SOURCE FOR DESKTOP SEARCH')"), 'Clearing search restores recent history');
    await evaluate(`document.querySelector('#history-search').value=${JSON.stringify(fixtureIds.source_id)};document.querySelector('#history-search').dispatchEvent(new Event('input',{bubbles:true}))`);
    await until(() => evaluate("[...document.querySelectorAll('.history-row')].some(node=>node.querySelector('p').textContent==='SYNTHETIC CASCADE SOURCE')"), 'cascade source search');
    await evaluate("[...document.querySelectorAll('.history-row')].find(node=>node.querySelector('p').textContent==='SYNTHETIC CASCADE SOURCE').querySelector('.record-actions button:last-child').click()");
    await until(() => evaluate("document.querySelector('#confirm-dialog').open"), 'source removal dialog');
    await evaluate("document.querySelector('#confirm-dialog button[value=confirm]').click()");
    await until(() => evaluate("!document.querySelector('#chat-messages').textContent.includes('SYNTHETIC CASCADE DERIVED ANSWER')"), 'derived response removed from chat');
    check(true, 'Deleting a source removes derived chat content from the renderer');
    const snapshot = (await request('/api/audit?trace_id=fixture-audit-trace'))[0];
    check((await request(`/api/audit/${snapshot.id}?include_content=true`)).content === null, 'Source deletion invalidates archived request body');

    if (live) {
      if (packaged) {
        await request('/api/settings', 'PUT', { llm: { tokenizer: 'o200k_base' } });
        const prepared = await request('/api/tokenizer/prepare', 'POST');
        check((await request('/api/tokenizer')).ready === true, 'Packaged tokenizer downloads, verifies and loads its native encoding');
      }
      await evaluate("document.querySelector('[data-view=settings]').click();document.querySelector('[name=voice_enabled]').checked=true;document.querySelector('[name=voice_language]').value='zh-CN';document.querySelector('[name=output_language]').value='zh-CN';document.querySelector('[name=\"tts.provider\"]').value='system';document.querySelector('[name=\"tts.provider\"]').dispatchEvent(new Event('change',{bubbles:true}));document.querySelector('[name=\"tts.voice\"]').value='';document.querySelector('#settings-form').requestSubmit(document.querySelector('#save-settings'))");
      await until(async () => (await request('/api/settings')).tts.provider === 'system', 'local speech settings');
      await until(() => evaluate("document.querySelector('#system-voice-select').options.length > 1"), 'system voice catalog');
      check(true, 'Installed system voices and Chinese language settings');
      const started = Date.now();
      await evaluate("document.querySelector('[data-view=conversation]').click();document.querySelector('#chat-input').value='SYNTHETIC DESKTOP INTEGRATION TEST. Reply in Chinese with exactly: 大贤者界面测试通过。现在准备停止语音。';document.querySelector('#chat-form').requestSubmit()");
      let streaming = false;
      await until(async () => {
        const frame = await evaluate("({streaming:Boolean(document.querySelector('.chat-text.streaming')),text:[...document.querySelectorAll('.chat-message.assistant .chat-text')].map(node=>node.textContent).join(''),errors:[...document.querySelectorAll('.toast.error')].map(node=>node.textContent).join(' ')})");
        streaming ||= frame.streaming;
        if (frame.errors && !frame.streaming && !frame.text) throw new Error(redact(frame.errors));
        return frame.text && !frame.streaming;
      }, 'real model response', 90000);
      check(streaming, 'Real model streaming response');
      if (await evaluate("!document.querySelector('#audio-unlock').hidden")) await evaluate("document.querySelector('#resume-audio').click()");
      await until(async () => (await request('/api/events')).some(event => event.kind === 'playback' && event.data.playing && new Date(event.created_at).getTime() >= started), 'muted Chinese speech', 45000);
      await evaluate("document.querySelector('#interrupt').click()");
      await until(async () => (await request('/api/status')).state !== 'speaking', 'playback interruption');
      check(true, 'Chinese speech playback and cancellation');
    }
    await evaluate("document.querySelector('[data-view=conversation]').click()");
    check(!(await evaluate('document.body.innerText')).includes(token), 'No connection token in visible UI');
    await screenshot(main, 'console.png');
    await evaluate("document.querySelector('[data-view=logs]').click();document.querySelector('#log-follow').checked=false;document.querySelector('#log-search').value='memory';document.querySelector('#log-search').dispatchEvent(new Event('input',{bubbles:true}))");
    await until(() => evaluate("document.querySelectorAll('.event-row').length > 0"), 'audit filtering');
    await evaluate("document.querySelector('.event-row').open=true");
    check(await evaluate("Boolean(JSON.parse(document.querySelector('.event-row pre').textContent).id)"), 'Audit event filtering and detail expansion');
    await screenshot(main, 'logs.png');
    await evaluate('window.greatsage.showPet()');
    let pet;
    await until(() => { pet = BrowserWindow.getAllWindows().find(window => window !== main && window.webContents.getURL().endsWith('/pet.html')); return Boolean(pet); }, 'pet window');
    await until(() => pet.webContents.executeJavaScript("Boolean(document.querySelector('#character-art svg'))"), 'pet art');
    await pet.webContents.executeJavaScript("document.querySelector('#pet-character').click()", true);
    check(await pet.webContents.executeJavaScript("document.querySelector('#pet-message').textContent.includes('摸摸已收到')"), 'Pet touch feedback');
    const bounds = pet.getBounds();
    check(Math.abs(bounds.width - 360) <= 1 && Math.abs(bounds.height - 420) <= 1 && pet.isAlwaysOnTop(), 'Pet dimensions with DPI rounding and always-on-top');
    await screenshot(pet, 'pet.png');
    await pet.webContents.executeJavaScript('window.greatsage.setClickThrough(true).then(()=>window.greatsage.setClickThrough(false)).then(()=>window.greatsage.showConsole())', true);
    await evaluate('window.greatsage.hidePet()');
    check(!pet.isVisible(), 'Pet show, click-through, console and hide bridge');
    check((await request('/api/status')).listening === false, 'No microphone or desktop listening started');
    completed = true;
  } catch (error) {
    reports.push({ check: redact(error.message), passed: false });
    if (main && !main.isDestroyed()) await screenshot(main, 'failure.png').catch(() => {});
  } finally {
    if (request && originalSettings) {
      try { await request('/api/interrupt', 'POST'); await request('/api/settings', 'PUT', originalSettings); }
      catch (error) { reports.push({ check: `Restore settings: ${redact(error.message)}`, passed: false }); }
    }
    app.quit();
  }
})();
