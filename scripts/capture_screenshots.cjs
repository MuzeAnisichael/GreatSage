'use strict';

// Regenerates the README screenshots from fictional data.
// Run: node_modules/electron/dist/electron.exe scripts/capture_screenshots.cjs [--write]
// The desktop app runs with an isolated data directory. A scripted local model
// answers every request, so no API key, network or microphone is used. The real
// task pipeline, approval flow and pet run unchanged. Images are written to
// ignored .runtime/screenshots/<run>/; --write also copies them to docs/assets/.
if (!process.versions.electron) {
  console.error('Run this script with the installed Electron executable, not Node.js.');
  process.exit(2);
}
const { app, BrowserWindow, nativeTheme } = require('electron');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const childProcess = require('node:child_process');

const root = path.resolve(__dirname, '..');
const runDir = path.join(root, '.runtime', 'screenshots', new Date().toISOString().replace(/[:.]/g, '-'));
const assets = path.join(root, 'docs', 'assets');
fs.mkdirSync(runDir, { recursive: true });
app.commandLine.appendSwitch('force-device-scale-factor', '2');
app.setPath('userData', path.join(runDir, 'electron-profile'));
process.env.GREATSAGE_DATA_DIR = path.join(runDir, 'data');
nativeTheme.themeSource = 'light';
BrowserWindow.prototype.show = function () {};
BrowserWindow.prototype.focus = function () {};
app.on('browser-window-created', (_event, window) => {
  window.setSkipTaskbar(true);
  window.webContents.setBackgroundThrottling(false);
  window.webContents.setAudioMuted(true);
  window.webContents.on('did-finish-load', () => {
    window.webContents.insertCSS('*{animation:none!important;transition:none!important;scroll-behavior:auto!important;caret-color:transparent!important}').catch(() => {});
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

// ---- fictional meeting ------------------------------------------------------
const MATERIALS = {
  '发布计划.md': '# 发布计划\n\n## 时间\n正式发布需要在周五评审通过后确定。\n\n## 风险\n如果内测反馈超过 50 条，发布可能推迟一周。\n',
  '内测反馈规范.md': '# 内测反馈规范\n\n## 记录方式\n所有问题统一记录在共享反馈表中，并标注严重程度。\n\n## 严重程度\n- 高：阻断主要流程\n- 中：影响效率，但有替代办法\n- 低：文案或样式问题\n',
};
const CONVERSATION = [
  ['user', '大贤者，帮我旁听这次产品周会，重点记下负责人和期限。', 'microphone:0'],
  ['assistant', '好的，我会记录会议里的结论、负责人和期限，需要时随时问我。', 'text'],
  ['observation', '王敏：新版控制台下周二开始内测，范围是产品和运营两个组。', 'system'],
  ['observation', '李航：内测期间我负责收集问题，周四下午前汇总成表格。', 'system'],
  ['observation', '王敏：发布时间先不定，等周五评审通过后再确认。', 'system'],
  ['observation', '陈晨：我来准备发布说明，下周一给初稿。', 'system'],
  ['user', '刚才定下的负责人都有谁？', 'microphone:0'],
  ['assistant', '目前有两项：李航负责汇总内测问题，周四下午前完成；陈晨准备发布说明，下周一交初稿。发布时间要等周五评审通过后再确认。', 'text'],
  ['user', '预算的事还没有结论，下次再讨论。', 'text'],
  ['assistant', '好的，我把预算记为未决问题。', 'text'],
];
const SAVE_REQUEST = '把这次会议的结论整理好，保存到 notes/周会结论.md。';
const NOTE = '# 周会结论\n\n- 新版控制台下周二开始内测，范围是产品和运营两个组\n- 发布时间等周五评审通过后确认\n- 李航：周四下午前汇总内测问题\n- 陈晨：下周一提交发布说明初稿\n- 未决：预算安排\n';

// ---- scripted local model -----------------------------------------------------
function handles(records, ...texts) {
  return records.filter(record => texts.some(text => record.text.includes(text))).map(record => record.handle);
}
function answer(body) {
  const messages = body.messages || [];
  const system = String(messages[0]?.content || '');
  const records = () => JSON.parse(messages[1].content).records;
  if (system.includes('meeting minutes')) {
    const list = records();
    return { content: JSON.stringify({
      summary: '新版控制台下周二开始内测，覆盖产品和运营两个组；发布时间等周五评审通过后确定。内测问题由李航汇总，发布说明由陈晨准备，预算尚无结论。',
      points: [
        { text: '新版控制台下周二开始内测，范围是产品和运营两个组', sources: handles(list, '下周二开始内测') },
        { text: '内测问题统一记录在共享反馈表中，并标注严重程度', sources: handles(list, '共享反馈表') },
        { text: '内测反馈超过 50 条时，发布可能推迟一周', sources: handles(list, '50 条') },
      ],
      decisions: [{ text: '发布时间在周五评审通过后确认', sources: handles(list, '周五评审') }],
      todos: [
        { task: '汇总内测问题并整理成表格', owner: '李航', due: '周四下午前', sources: handles(list, '收集问题') },
        { task: '准备发布说明初稿', owner: '陈晨', due: '下周一', sources: handles(list, '发布说明，下周一') },
        { task: '确认内测参与名单', owner: '', due: '', sources: [] },
      ],
      questions: [{ text: '预算安排尚无结论，下次讨论', sources: handles(list, '预算的事') }],
    }) };
  }
  if (system.includes('partial meeting summaries')) return { content: '{"summary":"新版控制台下周二开始内测，发布时间待周五评审后确定。"}' };
  if (system.includes('document draft')) {
    const list = records();
    const cite = (...texts) => handles(list, ...texts).map(handle => `[${handle}]`).join('');
    return { content: `# 新版控制台内测说明\n\n## 内测安排\n\n新版控制台将于下周二开始内测，参与范围为产品和运营两个组 ${cite('下周二开始内测')}。内测期间的问题统一记录在共享反馈表中，并标注严重程度 ${cite('共享反馈表')}。\n\n## 负责人与期限\n\n- 李航负责汇总内测问题，周四下午前整理成表格 ${cite('收集问题')}。\n- 陈晨负责准备发布说明，下周一提交初稿 ${cite('发布说明，下周一')}。\n- 内测参与名单的确认：待确认。\n\n## 发布时间\n\n发布时间将在周五评审通过后确认 ${cite('周五评审')}。如果内测反馈超过 50 条，发布可能推迟一周 ${cite('50 条')}。\n` };
  }
  if (system.startsWith('Summarize records concisely')) return { content: '产品周会：新版控制台下周二开始内测（产品、运营两组）；李航周四下午前汇总内测问题；陈晨下周一交发布说明初稿；发布时间待周五评审后确定；预算未决。' };
  if (system.startsWith('Check explicit user memories')) return { content: '{"conflict_ids":[]}' };
  if (messages.at(-1)?.role === 'tool') return { content: '已保存到工作目录的 notes/周会结论.md。' };
  const last = String([...messages].reverse().find(message => message.role === 'user')?.content || '');
  if (body.tools && last.includes('notes/周会结论.md')) {
    return { content: '好的，结论已经整理好。写入文件前需要你在控制台确认。', call: { name: 'write_file', arguments: { path: 'notes/周会结论.md', content: NOTE } } };
  }
  return { content: '好的。' };
}
function startModel() {
  const server = http.createServer((request, response) => {
    if (request.method !== 'POST') { response.end('screenshot fixture model'); return; }
    let raw = '';
    request.setEncoding('utf8');
    request.on('data', chunk => { raw += chunk; });
    request.on('end', () => {
      const reply = answer(JSON.parse(raw));
      const send = value => response.write(`data: ${JSON.stringify(value)}\n\n`);
      response.writeHead(200, { 'Content-Type': 'text/event-stream' });
      // The first chunk waits about as long as the measured live median in
      // docs/validation-v0.3.md, so the latency card is not misleadingly fast.
      setTimeout(() => {
        send({ choices: [{ index: 0, delta: { content: reply.content }, finish_reason: null }] });
        if (reply.call) {
          send({ choices: [{ index: 0, delta: { tool_calls: [{ index: 0, id: 'call_fixture', type: 'function',
            function: { name: reply.call.name, arguments: JSON.stringify(reply.call.arguments) } }] }, finish_reason: null }] });
        }
        send({ choices: [{ index: 0, delta: {}, finish_reason: reply.call ? 'tool_calls' : 'stop' }] });
        response.end('data: [DONE]\n\n');
      }, 640);
    });
  });
  return new Promise(resolve => server.listen(0, '127.0.0.1', () => resolve(server)));
}

// ---- capture ------------------------------------------------------------------
const captured = [];
async function capture(window, name, rect) {
  await window.webContents.executeJavaScript("document.querySelectorAll('.toast').forEach(node => node.remove());document.activeElement?.blur()", true).catch(() => {});
  window.webContents.invalidate();
  await window.webContents.capturePage(rect, { stayHidden: true, stayAwake: true });
  await sleep(250);
  const image = await window.webContents.capturePage(rect, { stayHidden: true, stayAwake: true });
  const file = path.join(runDir, `${name}.png`);
  fs.writeFileSync(file, image.toPNG());
  captured.push(file);
  return image;
}
// WebP keeps the committed screenshots small; the page's own Chromium encodes it.
// Transparent captures (the pet) are trimmed to their visible content.
async function webp(window, file, quality = 0.9) {
  const encoded = await window.webContents.executeJavaScript(`(async () => {
    const bytes = Uint8Array.from(atob(${JSON.stringify(fs.readFileSync(file).toString('base64'))}), c => c.charCodeAt(0));
    const bitmap = await createImageBitmap(new Blob([bytes], { type: 'image/png' }));
    let canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
    const context = canvas.getContext('2d');
    context.drawImage(bitmap, 0, 0);
    const pixels = context.getImageData(0, 0, bitmap.width, bitmap.height).data;
    let [left, top, right, bottom] = [bitmap.width, bitmap.height, -1, -1];
    for (let y = 0; y < bitmap.height; y += 1) for (let x = 0; x < bitmap.width; x += 1) {
      if (pixels[(y * bitmap.width + x) * 4 + 3] > 8) { left = Math.min(left, x); right = Math.max(right, x); top = Math.min(top, y); bottom = Math.max(bottom, y); }
    }
    if (right >= 0 && (left > 0 || top > 0 || right < bitmap.width - 1 || bottom < bitmap.height - 1)) {
      const margin = 16;
      [left, top] = [Math.max(0, left - margin), Math.max(0, top - margin)];
      [right, bottom] = [Math.min(bitmap.width - 1, right + margin), Math.min(bitmap.height - 1, bottom + margin)];
      const trimmed = new OffscreenCanvas(right - left + 1, bottom - top + 1);
      trimmed.getContext('2d').drawImage(canvas, left, top, trimmed.width, trimmed.height, 0, 0, trimmed.width, trimmed.height);
      canvas = trimmed;
    }
    const blob = await canvas.convertToBlob({ type: 'image/webp', quality: ${quality} });
    const data = new Uint8Array(await blob.arrayBuffer());
    let text = '';
    for (let index = 0; index < data.length; index += 32768) text += String.fromCharCode(...data.subarray(index, index + 32768));
    return btoa(text);
  })()`, true);
  const output = file.replace(/\.png$/, '.webp');
  fs.writeFileSync(output, Buffer.from(encoded, 'base64'));
  return output;
}

function socialCard(consoleImage) {
  const sage = fs.readFileSync(path.join(root, 'ui', 'sage.svg'), 'utf8');
  const shot = `data:image/png;base64,${consoleImage.toPNG().toString('base64')}`;
  return `<!doctype html><meta charset="utf-8"><style>
    *{box-sizing:border-box;margin:0}
    body{width:1280px;height:640px;overflow:hidden;background:#f6f6f4;color:#1d1f22;
      font-family:"Segoe UI Variable Text","Segoe UI","Microsoft YaHei UI",sans-serif;display:flex;align-items:center}
    .text{width:600px;padding:0 0 0 84px;flex:none}
    .sage{width:120px;height:120px;margin-left:-8px}
    h1{font-size:68px;font-weight:700;letter-spacing:-1.5px;line-height:1.05;margin-top:18px}
    h1 small{display:block;font-size:34px;font-weight:600;letter-spacing:2px;color:#3e5064;margin-top:10px}
    p{font-size:24px;line-height:1.5;color:#4b4f55;margin-top:20px}
    .chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:28px;max-width:540px}
    .chips span{font-size:16px;padding:6px 12px;border-radius:999px;background:#e7eef6;color:#2f5d8c}
    .shot{position:absolute;left:660px;top:78px;width:880px;border-radius:14px;border:1px solid #e3e3de;
      box-shadow:0 24px 60px rgb(20 20 18 / 16%),0 3px 10px rgb(20 20 18 / 8%)}
  </style><div class="text"><div class="sage">${sage}</div><h1>GreatSage<small>大贤者</small></h1>
  <p>会听、会记、会动手的 Windows 桌面秘书</p>
  <div class="chips"><span>语音对话与旁听</span><span>跨会话记忆</span><span>带出处的纪要</span><span>操作先确认</span></div></div>
  <img class="shot" src="${shot}">`;
}

(async () => {
  const model = await startModel();
  let main;
  let failed = false;
  try {
    require('../desktop/main.cjs');
    await app.whenReady();
    await until(() => {
      main = BrowserWindow.getAllWindows().find(window => /^http:\/\/127\.0\.0\.1:\d+\/$/.test(window.webContents.getURL()));
      return Boolean(main);
    }, 'desktop startup', 45000);
    main.setContentSize(1280, 800);
    const evaluate = code => main.webContents.executeJavaScript(code, true);
    await until(() => evaluate("document.querySelector('#connection-label')?.textContent === '本地服务已连接'"), 'backend connection');
    const connection = await evaluate('window.greatsage.getConnection()');
    const request = async (url, method = 'GET', body) => {
      const response = await fetch(connection.baseUrl + url, {
        method, headers: { Authorization: `Bearer ${connection.token}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(30000),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}: ${url}`);
      return response.json();
    };
    await request('/api/settings', 'PUT', { output_language: 'zh-CN', voice_enabled: false, embedding: { enabled: false },
      llm: { provider: 'openai', base_url: `http://127.0.0.1:${model.address().port}/v1`, model: '本地演示模型', api_key_env: '' } });
    await until(() => evaluate("!document.querySelector('#refresh-sources').disabled"), 'audio source enumeration');

    const seed = `import json,sys\nfrom pathlib import Path\nfrom greatsage.memory import MemoryStore\ndata=json.load(sys.stdin)\nstore=MemoryStore(Path(data['directory']))\nfor role,text,source in data['conversation']: store.add_message(role,text,source,data['session'])\nstore.add_memory('我喜欢简短、直接的回答。')\nstore.add_memory('会议纪要默认使用中文，人名和数字保留原文。')\nstore.close()`;
    const session = (await request('/api/status')).session_id;
    const seeded = childProcess.spawnSync(path.join(root, '.venv', 'Scripts', 'python.exe'), ['-c', seed], {
      cwd: root, encoding: 'utf8', windowsHide: true, timeout: 15000,
      input: JSON.stringify({ directory: process.env.GREATSAGE_DATA_DIR, session, conversation: CONVERSATION }) });
    if (seeded.status !== 0) throw new Error(`Seeding failed: ${seeded.stderr}`);
    const folder = path.join(runDir, '内测资料');
    fs.mkdirSync(folder);
    for (const [name, text] of Object.entries(MATERIALS)) fs.writeFileSync(path.join(folder, name), text, 'utf8');
    const materials = (await request('/api/materials/import', 'POST', { path: folder })).materials;

    const task = await request('/api/tasks', 'POST', { title: '产品周会纪要', material_ids: materials.map(item => item.id), document: true, use_session: true });
    await until(async () => (await request(`/api/tasks/${task.id}`)).status === 'succeeded', 'minutes task', 60000);
    await new Promise(resolve => { main.webContents.once('did-finish-load', resolve); main.webContents.reload(); });
    // The chat shows your messages and replies; overheard lines stay in history and sources.
    await until(() => evaluate("document.querySelectorAll('.chat-message').length >= 6"), 'seeded conversation');

    await evaluate('window.greatsage.showPet()');
    let pet;
    await until(() => { pet = BrowserWindow.getAllWindows().find(window => window !== main && window.webContents.getURL().endsWith('/pet.html')); return Boolean(pet); }, 'pet window');
    await until(() => pet.webContents.executeJavaScript("Boolean(document.querySelector('#character-art svg'))"), 'pet art');
    await sleep(800);
    await evaluate(`document.querySelector('#chat-input').value=${JSON.stringify(SAVE_REQUEST)};document.querySelector('#chat-form').requestSubmit()`);
    await until(async () => (await request('/api/approvals')).length === 1, 'approval request', 30000);
    await until(() => evaluate("!document.querySelector('.chat-text.streaming') && !document.querySelector('#approval-indicator').hidden"), 'reply and approval indicator');
    await until(() => pet.webContents.executeJavaScript("document.querySelector('#pet-message').textContent.includes('notes/周会结论.md')"), 'pet approval bubble');
    if (await evaluate("Boolean(document.querySelector('.toast.error'))")) throw new Error('An error toast is visible.');

    const toBottom = "document.querySelector('#chat-messages').scrollTop = document.querySelector('#chat-messages').scrollHeight";
    await evaluate(toBottom);
    const lightConsole = await capture(main, 'console-light');
    nativeTheme.themeSource = 'dark';
    await sleep(600);
    await evaluate(toBottom);
    await capture(main, 'console-dark');
    nativeTheme.themeSource = 'light';
    await sleep(600);
    // Before any settings change: saving settings interrupts replies and resets the bubble.
    await capture(pet, 'pet');

    await evaluate("document.querySelector('[data-view=tasks]').click()");
    await until(() => evaluate("document.querySelectorAll('.task-item').length >= 2 && !document.querySelector('#approvals-panel').hidden"), 'task list');
    await evaluate("[...document.querySelectorAll('.task-item')].find(node => node.textContent.includes('产品周会纪要')).click()");
    await until(() => evaluate("document.querySelector('#task-detail').textContent.includes('文档草稿')"), 'task detail');
    await capture(main, 'tasks');

    await evaluate("[...document.querySelectorAll('#task-detail .artifact-row')].find(row => row.querySelector('strong').textContent === '产品周会纪要').querySelector('button').click()");
    await until(() => evaluate("[...document.querySelectorAll('dialog')].some(node => node.open && [...node.querySelectorAll('textarea')].some(area => area.value.includes('[S')))"), 'minutes dialog');
    await capture(main, 'artifact');
    await evaluate("[...document.querySelectorAll('dialog')].forEach(node => node.open && node.close())");

    // Pages that print local paths (materials, workspace) are left out of the README images.
    await evaluate("document.querySelector('[data-view=memory]').click()");
    await until(() => evaluate("document.querySelector('#view-memory').innerText.includes('我喜欢简短、直接的回答')"), 'memory view');
    await capture(main, 'memory');

    // Example rules; none of them matches the pending write above.
    await request('/api/settings', 'PUT', {
      tool_rules: [
        { tool: 'write_file', match: 'drafts/*', action: 'allow' },
        { tool: 'open_url', match: 'https://docs.example.com/*', action: 'allow' },
        { tool: 'delete_file', match: 'archive/*', action: 'deny' },
        { tool: '*', match: '*.exe', action: 'deny' },
      ],
      app_launchers: [{ name: '记事本', path: 'C:\\Windows\\System32\\notepad.exe' }],
    });
    await new Promise(resolve => { main.webContents.once('did-finish-load', resolve); main.webContents.reload(); });
    await until(() => evaluate("document.querySelector('#connection-label')?.textContent === '本地服务已连接'"), 'reconnect');
    await evaluate("document.querySelector('[data-view=settings]').click()");
    await until(() => evaluate("document.querySelectorAll('#rule-editor select, #rule-editor input').length >= 4"), 'rule editor');
    // A taller window fits the whole section; the crop starts below the workspace path row.
    main.setContentSize(1280, 1400);
    await sleep(300);
    const rect = await evaluate(`(() => {
      const row = document.querySelector('[name=voice_approval]').closest('.switch-row');
      let scroller = row.parentElement;
      while (scroller && scroller.scrollHeight <= scroller.clientHeight) scroller = scroller.parentElement;
      (scroller || document.scrollingElement).scrollTop += row.getBoundingClientRect().top - 300;
      const top = Math.max(row.getBoundingClientRect().top - 24, document.querySelector('#workspace-path').getBoundingClientRect().bottom + 8);
      const box = document.querySelector('#tool-settings').getBoundingClientRect();
      return { x: Math.round(box.left), y: Math.round(top), width: Math.round(box.width), height: Math.round(Math.min(box.bottom, innerHeight) - top) };
    })()`);
    await sleep(300);
    await capture(main, 'tools', rect);
    main.setContentSize(1280, 800);

    const card = new BrowserWindow({ width: 1280, height: 640, useContentSize: true, show: false, webPreferences: { sandbox: true } });
    card.setContentSize(1280, 640);
    const cardFile = path.join(runDir, 'social-card.html');
    fs.writeFileSync(cardFile, socialCard(lightConsole), 'utf8');
    await card.loadFile(cardFile);
    await sleep(300);
    const socialImage = await capture(card, 'social-preview');
    fs.writeFileSync(path.join(runDir, 'social-preview.png'), socialImage.resize({ width: 1280, quality: 'best' }).toPNG());
    card.destroy();

    const outputs = [];
    for (const file of captured) outputs.push(path.basename(file) === 'social-preview.png' ? file : await webp(main, file));
    if (process.argv.includes('--write')) {
      fs.mkdirSync(assets, { recursive: true });
      for (const file of outputs) fs.copyFileSync(file, path.join(assets, path.basename(file)));
    }
    console.log(`Captured ${outputs.length} images in ${runDir}${process.argv.includes('--write') ? ' and docs/assets' : ''}`);
  } catch (error) {
    failed = true;
    console.error(`FAIL: ${error.message}`);
    if (main && !main.isDestroyed()) await capture(main, 'failure').catch(() => {});
  } finally {
    model.close();
    process.exitCode = failed ? 1 : 0;
    app.quit();
  }
})();
