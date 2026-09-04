'use strict';

const { app, BrowserWindow, Menu, Tray, nativeImage, ipcMain, dialog, shell, session } = require('electron');
const { spawn } = require('node:child_process');
const { randomBytes } = require('node:crypto');
const net = require('node:net');
const fs = require('node:fs');
const path = require('node:path');

const projectRoot = path.resolve(__dirname, '..');
const connectionToken = randomBytes(32).toString('hex');
let baseUrl = '';
let mainWindow = null;
let petWindow = null;
let tray = null;
let backend = null;
let logStream = null;
let dataDir = '';
let quitting = false;
let quitAllowed = false;
let backendFailure = null;
let ready = false;

const escapeHtml = value => String(value).replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);

function redact(value) {
  return String(value)
    .replaceAll(connectionToken, '[local-token-redacted]')
    .replace(/\bBearer\s+[^\s"']+/gi, 'Bearer [redacted]')
    .replace(/\bsk-[A-Za-z0-9_-]{8,}/g, '[api-key-redacted]')
    .replace(/([?&]token=)[^&\s"']+/gi, '$1[redacted]')
    .replace(/(["']?(?:api_key|access_token|authorization|password|secret)["']?\s*[:=]\s*)["']?[^\s,"'}]+/gi, '$1[redacted]');
}

function writeLog(message) {
  if (logStream && !logStream.destroyed) logStream.write(`${new Date().toISOString()} ${redact(message)}\n`);
}

function pipeLog(stream, label) {
  let pending = '';
  stream.setEncoding('utf8');
  stream.on('data', chunk => {
    pending += chunk;
    let end;
    while ((end = pending.indexOf('\n')) !== -1) {
      writeLog(`[${label}] ${pending.slice(0, end).trimEnd()}`);
      pending = pending.slice(end + 1);
    }
    // Do not persist partial secrets if a child writes one unusually long line.
    if (pending.length > 1024 * 1024) { writeLog(`[${label}] oversized log line omitted`); pending = ''; }
  });
  stream.on('end', () => { if (pending) writeLog(`[${label}] ${pending}`); });
}

function windowOptions(extra = {}) {
  return {
    show: false,
    title: 'GreatSage · 大贤者',
    backgroundColor: '#0c1021',
    icon: makeIcon(),
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      spellcheck: false,
    },
    ...extra,
  };
}

function allowedPage(url) {
  try {
    const parsed = new URL(url);
    return parsed.origin === baseUrl && ['/', '/index.html', '/pet.html'].includes(parsed.pathname);
  } catch { return false; }
}

function secureWindow(window) {
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', (event, url) => { if (!allowedPage(url)) event.preventDefault(); });
  window.webContents.on('will-redirect', (event, url) => { if (!allowedPage(url)) event.preventDefault(); });
  window.webContents.on('will-attach-webview', event => event.preventDefault());
  window.webContents.on('render-process-gone', (_event, details) => writeLog(`Renderer stopped: ${details.reason}`));
}

function makeMainWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) return mainWindow;
  mainWindow = new BrowserWindow(windowOptions({ width: 1400, height: 950, minWidth: 780, minHeight: 620 }));
  secureWindow(mainWindow);
  mainWindow.once('ready-to-show', () => { if (!quitting) mainWindow.show(); });
  mainWindow.on('close', event => {
    if (!quitting && tray) { event.preventDefault(); mainWindow.hide(); }
  });
  mainWindow.on('closed', () => { mainWindow = null; });
  if (ready) mainWindow.loadURL(`${baseUrl}/`);
  return mainWindow;
}

function showMain() {
  const window = makeMainWindow();
  if (window.isMinimized()) window.restore();
  window.show(); window.focus();
}

function showPet() {
  if (!ready) throw new Error('本地服务尚未就绪。');
  if (!petWindow || petWindow.isDestroyed()) {
    petWindow = new BrowserWindow(windowOptions({
      width: 360, height: 420, minWidth: 280, minHeight: 340,
      frame: false, transparent: true, backgroundColor: '#00000000',
      alwaysOnTop: true, skipTaskbar: true, resizable: false, maximizable: false,
      fullscreenable: false, hasShadow: false,
    }));
    secureWindow(petWindow);
    petWindow.once('ready-to-show', () => petWindow?.show());
    petWindow.on('close', event => { if (!quitting) { event.preventDefault(); petWindow.hide(); petWindow.setIgnoreMouseEvents(false); updateTray(); } });
    petWindow.on('closed', () => { petWindow = null; updateTray(); });
    petWindow.loadURL(`${baseUrl}/pet.html`);
  } else { petWindow.setIgnoreMouseEvents(false); petWindow.show(); }
  updateTray();
}

function hidePet() {
  if (petWindow && !petWindow.isDestroyed()) { petWindow.setIgnoreMouseEvents(false); petWindow.hide(); }
  updateTray();
}

function makeIcon() {
  const width = 32;
  const bitmap = Buffer.alloc(width * width * 4);
  for (let y = 0; y < width; y++) for (let x = 0; x < width; x++) {
    const dx = x - 15.5; const dy = y - 15.5;
    const radius = Math.sqrt(dx * dx + dy * dy);
    const star = Math.abs(dx) + Math.abs(dy) < 10 && (Math.abs(dx) < 4 || Math.abs(dy) < 4);
    const offset = (y * width + x) * 4;
    const rgb = star ? [134, 230, 209] : [25, 46, 61];
    bitmap[offset] = rgb[2]; bitmap[offset + 1] = rgb[1]; bitmap[offset + 2] = rgb[0]; bitmap[offset + 3] = radius < 15.5 ? 255 : 0;
  }
  return nativeImage.createFromBitmap(bitmap, { width, height: width, scaleFactor: 1 });
}

function updateTray() {
  if (!tray || tray.isDestroyed()) return;
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '打开 GreatSage 控制台', click: showMain },
    { label: '显示桌宠', enabled: ready, click: () => { try { showPet(); } catch (error) { writeLog(error.message); } } },
    { label: '收起桌宠', enabled: Boolean(petWindow && !petWindow.isDestroyed() && petWindow.isVisible()), click: hidePet },
    { label: '恢复桌宠鼠标操作', enabled: Boolean(petWindow && !petWindow.isDestroyed()), click: () => petWindow?.setIgnoreMouseEvents(false) },
    { type: 'separator' },
    { label: '退出 GreatSage', click: () => app.quit() },
  ]));
}

function createTray() {
  try {
    tray = new Tray(makeIcon());
    tray.setToolTip('GreatSage · 大贤者');
    tray.on('double-click', showMain);
    updateTray();
  } catch (error) { writeLog(`Tray unavailable: ${error.message}`); tray = null; }
}

function validateSender(event) {
  const validWindow = [mainWindow, petWindow].some(window => window && !window.isDestroyed() && window.webContents === event.sender);
  if (!validWindow || event.senderFrame !== event.sender.mainFrame || !allowedPage(event.senderFrame.url)) throw new Error('此页面无权调用桌面接口。');
}

function registerBridge() {
  const handle = (name, handler) => ipcMain.handle(`greatsage:${name}`, (event, ...args) => { validateSender(event); return handler(event, ...args); });
  handle('connection', () => ({ baseUrl, token: connectionToken }));
  handle('show-pet', () => showPet());
  handle('hide-pet', () => hidePet());
  handle('show-console', () => showMain());
  handle('click-through', (_event, enabled) => { if (petWindow && !petWindow.isDestroyed()) petWindow.setIgnoreMouseEvents(Boolean(enabled), { forward: true }); });
  handle('minimize', event => BrowserWindow.fromWebContents(event.sender)?.minimize());
  handle('open-external', async (_event, value) => {
    const url = new URL(String(value));
    if (url.protocol !== 'https:' || url.username || url.password) throw new Error('只能打开 HTTPS 网页。');
    await shell.openExternal(url.href);
  });
  handle('choose-skill-directory', async event => {
    const result = await dialog.showOpenDialog(BrowserWindow.fromWebContents(event.sender), { title: '选择包含 SKILL.md 的技能文件夹', properties: ['openDirectory'] });
    return result.canceled ? null : result.filePaths[0] || null;
  });
}

function selectPort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => { const port = server.address().port; server.close(error => error ? reject(error) : resolve(port)); });
  });
}

async function startBackend() {
  const port = await selectPort();
  baseUrl = `http://127.0.0.1:${port}`;
  const executable = app.isPackaged
    ? path.join(process.resourcesPath, 'backend', 'greatsage-backend.exe')
    : path.join(projectRoot, '.venv', 'Scripts', 'python.exe');
  if (!fs.existsSync(executable)) throw new Error(app.isPackaged ? '找不到已打包的后端程序。请重新安装完整版本。' : '找不到项目 Python 环境。请先运行项目安装脚本，再启动 GreatSage。');
  const args = [...(app.isPackaged ? [] : ['-m', 'greatsage']), '--port', String(port), '--data-dir', dataDir, '--exclude-pid', String(process.pid)];
  backend = spawn(executable, args, { cwd: app.isPackaged ? dataDir : projectRoot, env: { ...process.env, GREATSAGE_TOKEN: connectionToken, PYTHONUNBUFFERED: '1' }, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
  pipeLog(backend.stdout, 'stdout'); pipeLog(backend.stderr, 'stderr');
  backend.on('error', error => { backendFailure = error; writeLog(`Backend launch failed: ${error.message}`); });
  backend.on('exit', (code, signal) => {
    writeLog(`Backend exited (code=${code}, signal=${signal || 'none'})`);
    backendFailure ||= new Error('本地服务已停止，请重新启动 GreatSage。');
    if (ready && !quitting) {
      ready = false; updateTray();
      const options = { type: 'error', title: 'GreatSage 服务已停止', message: '本地服务意外停止。', detail: `请重新启动应用。排查记录保存在：\n${path.join(dataDir, 'backend.log')}` };
      const notice = mainWindow && !mainWindow.isDestroyed() ? dialog.showMessageBox(mainWindow, options) : dialog.showMessageBox(options);
      notice.catch(() => {});
    }
  });
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline && !quitting) {
    if (backendFailure) throw backendFailure;
    try {
      const response = await fetch(`${baseUrl}/health`, { signal: AbortSignal.timeout(1200) });
      if (response.ok && (await response.json()).ok === true) {
        // Verify that readiness belongs to our authenticated child, not another local process.
        const status = await fetch(`${baseUrl}/api/status`, { headers: { Authorization: `Bearer ${connectionToken}` }, signal: AbortSignal.timeout(1200) });
        if (status.ok) { ready = true; updateTray(); return; }
      }
    } catch { /* Local service is still starting. */ }
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  if (!quitting) throw new Error('本地服务在 30 秒内未完成启动。请查看 backend.log，确认依赖已安装并且端口可用。');
}

function showStartupPage(error) {
  const window = makeMainWindow();
  const title = error ? '大贤者暂时无法启动' : '正在唤醒你的大贤者';
  const message = error ? redact(error.message) : '正在启动本地服务，即将进入你的工作空间。';
  const details = error ? `<p class="path">排查记录：${escapeHtml(path.join(dataDir, 'backend.log'))}</p><p>完成修复后重新打开应用。可通过系统托盘或菜单退出。</p>` : '<div class="dots"><i></i><i></i><i></i></div>';
  const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'"><title>GreatSage</title><style>*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0c1021;color:#e6e9f5;font-family:'Segoe UI','Microsoft YaHei UI',sans-serif}.card{max-width:660px;text-align:center;padding:48px}.mark{font-size:65px;color:#86e6d1;margin-bottom:25px}h1{font-size:25px;font-weight:550;letter-spacing:-.5px}p{font-size:13px;color:#99abc5;line-height:1.9;overflow-wrap:anywhere}.path{padding:13px;background:#1b2540;border:1px solid #354564;border-radius:10px;font-size:11px;margin-top:25px}.dots{display:flex;gap:8px;justify-content:center;margin:26px}.dots i{width:5px;height:5px;border-radius:50%;background:#86e6d1;animation:pulse 1s infinite}.dots i:nth-child(2){animation-delay:.2s}.dots i:nth-child(3){animation-delay:.4s}@keyframes pulse{50%{opacity:.25}}</style></head><body><div class="card"><div class="mark">✦</div><h1>${escapeHtml(title)}</h1><p>${escapeHtml(message)}</p>${details}</div></body></html>`;
  window.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`).catch(error => writeLog(error.message));
  window.show();
}

async function stopBackend() {
  if (!backend || backend.exitCode !== null || backend.signalCode !== null) return;
  const child = backend;
  await new Promise(resolve => {
    let settled = false;
    const finish = () => { if (!settled) { settled = true; clearTimeout(timer); resolve(); } };
    const timer = setTimeout(() => {
      if (process.platform === 'win32' && child.pid) {
        const killer = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
        killer.once('error', finish); killer.once('exit', finish);
        setTimeout(finish, 1800);
      } else { try { child.kill('SIGKILL'); } catch {} finish(); }
    }, 1500);
    child.once('exit', finish);
    try { child.kill('SIGTERM'); } catch { finish(); }
  });
}

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => { if (app.isReady()) showMain(); });
  app.whenReady().then(async () => {
    dataDir = process.env.GREATSAGE_DATA_DIR
      ? path.resolve(process.env.GREATSAGE_DATA_DIR)
      : app.isPackaged ? app.getPath('userData') : path.join(projectRoot, '.runtime');
    fs.mkdirSync(dataDir, { recursive: true });
    const logPath = path.join(dataDir, 'backend.log');
    if (fs.existsSync(logPath) && fs.statSync(logPath).size > 5 * 1024 * 1024) {
      const previous = path.join(dataDir, 'backend.previous.log');
      if (fs.existsSync(previous)) fs.unlinkSync(previous);
      fs.renameSync(logPath, previous);
    }
    logStream = fs.createWriteStream(logPath, { flags: 'a' });
    logStream.on('error', () => {});
    writeLog('GreatSage desktop starting');
    session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    session.defaultSession.setPermissionCheckHandler(() => false);
    registerBridge(); createTray();
    Menu.setApplicationMenu(Menu.buildFromTemplate([
      { label: 'GreatSage', submenu: [{ label: '打开控制台', click: showMain }, { label: '显示桌宠', click: () => { if (ready) showPet(); } }, { type: 'separator' }, { label: '退出', accelerator: 'Alt+F4', click: () => app.quit() }] },
      { label: '编辑', submenu: [{ role: 'undo', label: '撤销' }, { role: 'redo', label: '重做' }, { type: 'separator' }, { role: 'cut', label: '剪切' }, { role: 'copy', label: '复制' }, { role: 'paste', label: '粘贴' }, { role: 'selectAll', label: '全选' }] },
      { label: '视图', submenu: [{ role: 'reload', label: '重新加载' }, { role: 'resetZoom', label: '实际大小' }, { role: 'zoomIn', label: '放大' }, { role: 'zoomOut', label: '缩小' }] },
    ]));
    makeMainWindow(); showStartupPage();
    try { await startBackend(); if (!quitting) await mainWindow.loadURL(`${baseUrl}/`); }
    catch (error) { writeLog(error.message); if (!quitting) showStartupPage(error); }
  }).catch(error => { dialog.showErrorBox('GreatSage 启动失败', redact(error.message)); app.quit(); });
  app.on('activate', () => { if (app.isReady()) showMain(); });
  app.on('window-all-closed', () => { if (!tray) app.quit(); });
  app.on('before-quit', event => {
    if (quitAllowed) return;
    event.preventDefault();
    if (quitting) return;
    quitting = true;
    stopBackend().finally(() => {
      tray?.destroy(); tray = null;
      writeLog('GreatSage desktop stopped');
      logStream?.end();
      quitAllowed = true; app.quit();
    });
  });
}
