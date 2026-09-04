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
const runDir = path.resolve(__dirname, '..', '.runtime', 'desktop-smoke', new Date().toISOString().replace(/[:.]/g, '-'));
fs.mkdirSync(runDir, { recursive: true });
process.env.GREATSAGE_DATA_DIR = path.join(runDir, 'data');
const reports = [];
const children = [];
const originalSpawn = childProcess.spawn;
childProcess.spawn = function (...args) {
  const child = originalSpawn.apply(this, args);
  if (Array.isArray(args[1]) && args[1].includes('greatsage')) children.push(child);
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
  const result = { success: completed && reports.every(report => report.passed), live, parallelSources, reports };
  fs.writeFileSync(path.join(runDir, 'result.json'), JSON.stringify(result, null, 2));
  console.log(`${result.success ? 'PASS' : 'FAIL'}: ${path.join(runDir, 'result.json')}`);
  process.exitCode = result.success ? 0 : 1;
  if (!result.success) { event.preventDefault(); app.exit(1); }
});
require('../desktop/main.cjs');

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
    check((await request('/api/status')).listening === false, 'Listening starts disabled');
    await until(() => evaluate("!document.querySelector('#refresh-sources').disabled"), 'initial audio device enumeration');
    check(await evaluate("document.querySelector('#quick-microphone-device').options.length >= 1 && document.querySelector('#quick-process').options.length >= 1 && !document.querySelector('.toast.error')"), 'Audio sources finish loading without errors');
    if (parallelSources) {
      const results = await Promise.all([request('/api/audio/sources'), request('/api/audio/sources')]);
      check(results.every(result => Array.isArray(result.microphones) && Array.isArray(result.processes)) && (await request('/api/status')).listening === false, 'Concurrent source enumeration keeps backend healthy');
    }
    check(await evaluate(`document.querySelector('#quick-microphone').checked === ${Boolean(originalSettings.microphone)} && document.querySelector('#quick-desktop-source').value === ${JSON.stringify(originalSettings.desktop_source)}`), 'Microphone and desktop sources stay separate');
    check(await evaluate('document.body.scrollWidth <= document.documentElement.clientWidth + 1'), 'Console has no horizontal overflow');
    const sessionId = (await request('/api/status')).session_id;
    const fixtureCode = `import json,sys\nfrom pathlib import Path\nfrom greatsage.memory import MemoryStore\ndata=json.load(sys.stdin)\nstore=MemoryStore(Path(data['directory']))\nolder_session=store.new_session()\nolder=store.add_message('user','SYNTHETIC OLDER SOURCE FOR DESKTOP SEARCH',session_id=older_session)\nsummary=store.save_summary('SYNTHETIC READ ONLY DESKTOP SUMMARY',[older['id']],model='smoke-fixture')\nfor index in range(115): store.add_message('observation','SYNTHETIC FILLER '+str(index),source='system',session_id=older_session)\nsource=store.add_message('user','SYNTHETIC CASCADE SOURCE',session_id=data['session'])\nstore.add_message('assistant','SYNTHETIC CASCADE DERIVED ANSWER',session_id=data['session'],metadata={'source_ids':[source['id']]})\nstore.close()\nprint(json.dumps({'older_id':older['id'],'summary_id':summary['id'],'source_id':source['id']}))`;
    const fixture = childProcess.spawnSync(path.resolve(__dirname, '..', '.venv', 'Scripts', 'python.exe'), ['-c', fixtureCode], { cwd: path.resolve(__dirname, '..'), input: JSON.stringify({ directory: process.env.GREATSAGE_DATA_DIR, session: sessionId }), encoding: 'utf8', windowsHide: true, timeout: 15000 });
    if (fixture.status !== 0) throw new Error(`Memory fixture failed: ${redact(fixture.stderr)}`);
    const fixtureIds = JSON.parse(fixture.stdout);
    await new Promise(resolve => { main.webContents.once('did-finish-load', resolve); main.webContents.reload(); });
    await until(() => evaluate("document.querySelector('#connection-label')?.textContent === '本地服务已连接' && document.querySelector('#chat-messages')?.textContent.includes('SYNTHETIC CASCADE DERIVED ANSWER')"), 'fixture history reload');
    await evaluate("document.querySelector('[data-view=settings]').click();document.querySelector('#settings-form').requestSubmit(document.querySelector('#save-settings'))");
    await until(() => evaluate("document.querySelector('#toasts').textContent.includes('设置已保存')"), 'settings save');
    check(true, 'Settings load and save through the UI');

    // A fixture is generated inside this run; no personal Skill directory is read.
    const fixtureDir = path.join(runDir, 'fixture-skill');
    fs.mkdirSync(fixtureDir);
    fs.writeFileSync(path.join(fixtureDir, 'SKILL.md'), '---\nname: desktop-smoke-fixture\ndescription: Synthetic local desktop smoke-test fixture.\n---\nUse concise answers for synthetic desktop tests.\n');
    await evaluate(`document.querySelector('[data-view=skills]').click();document.querySelector('#skill-path').value=${JSON.stringify(fixtureDir)};document.querySelector('#skill-form').requestSubmit()`);
    await until(() => evaluate("[...document.querySelectorAll('.skill-card')].some(node=>node.textContent.includes('desktop-smoke-fixture'))"), 'fixture Skill import');
    check(true, 'Fixture SKILL.md imports and renders');

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
    await until(() => evaluate("[...document.querySelectorAll('.summary-card')].some(node=>node.textContent.includes('SYNTHETIC READ ONLY DESKTOP SUMMARY'))"), 'read-only summary card');
    check(await evaluate("[...document.querySelectorAll('.summary-card')].every(node=>!node.querySelector('.record-actions'))"), 'Summaries expose no memory mutation controls');
    await evaluate(`document.querySelector('.summary-card [data-source-id="${fixtureIds.older_id}"]').click()`);
    await until(() => evaluate("document.querySelector('#history-list').textContent.includes('SYNTHETIC OLDER SOURCE FOR DESKTOP SEARCH')"), 'source lookup beyond recent 100 messages');
    check(true, 'Summary source opens server-backed historical search');
    await evaluate("document.querySelector('#history-search').value='';document.querySelector('#history-search').dispatchEvent(new Event('input',{bubbles:true}))");
    check(await evaluate("!document.querySelector('#history-list').textContent.includes('SYNTHETIC OLDER SOURCE FOR DESKTOP SEARCH')"), 'Clearing search restores recent history');
    await evaluate(`document.querySelector('#history-search').value=${JSON.stringify(fixtureIds.source_id)};document.querySelector('#history-search').dispatchEvent(new Event('input',{bubbles:true}))`);
    await until(() => evaluate("[...document.querySelectorAll('.history-row')].some(node=>node.querySelector('p').textContent==='SYNTHETIC CASCADE SOURCE')"), 'cascade source search');
    await evaluate("[...document.querySelectorAll('.history-row')].find(node=>node.querySelector('p').textContent==='SYNTHETIC CASCADE SOURCE').querySelector('.record-actions button:last-child').click()");
    await until(() => evaluate("document.querySelector('#confirm-dialog').open"), 'source removal dialog');
    await evaluate("document.querySelector('#confirm-dialog button[value=confirm]').click()");
    await until(() => evaluate("!document.querySelector('#chat-messages').textContent.includes('SYNTHETIC CASCADE DERIVED ANSWER')"), 'derived response removed from chat');
    check(true, 'Deleting a source removes derived chat content from the renderer');

    if (live) {
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
