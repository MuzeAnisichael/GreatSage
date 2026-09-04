import { SageClient, stateLabels, redact } from './client.js';

const $ = selector => document.querySelector(selector);
const client = new SageClient();
let lastText = '我在这里。需要时，随时叫我。';
let currentText = '';
let currentState = 'idle';
let touching = false;
let touchTimer;
let throughTimer;
let currentTrace = null;
let streamGap = false;

if (!window.greatsage) document.body.classList.add('browser-mode');
$('#pet-through').hidden = !window.greatsage?.setClickThrough;
$('#pet-console').hidden = !window.greatsage?.showConsole;

function setMessage(text, eyebrow = 'GREAT SAGE', streaming = false) {
  if (touching) return;
  $('#pet-message').textContent = redact(String(text || ''));
  $('#bubble-eyebrow').textContent = eyebrow;
  $('#pet-message').classList.toggle('streaming', streaming);
  $('#pet-message').scrollTop = $('#pet-message').scrollHeight;
}

function setState(state) {
  currentState = state || 'idle';
  $('#pet-state').textContent = stateLabels[state] || (state === 'offline' ? '未连接' : state);
  const visual = ['transcribing', 'listening'].includes(state) ? 'listening' : ['thinking', 'responding'].includes(state) ? 'thinking' : state;
  $('.pet-status').className = `pet-status ${visual}`;
  $('#pet-character').classList.toggle('speaking', visual === 'speaking');
  $('#pet-character').classList.toggle('thinking', visual === 'thinking');
  $('#pet-character').classList.toggle('disconnected', visual === 'offline');
}

$('#pet-character').addEventListener('click', () => {
  touching = true;
  clearTimeout(touchTimer);
  $('#pet-character').classList.remove('petted');
  void $('#pet-character').offsetWidth;
  $('#pet-character').classList.add('petted');
  $('#bubble-eyebrow').textContent = 'A LITTLE MOMENT';
  $('#pet-message').textContent = '摸摸已收到 ♡';
  $('#pet-message').classList.remove('streaming');
  touchTimer = setTimeout(() => {
    touching = false;
    $('#pet-character').classList.remove('petted');
    setMessage(currentText || lastText, currentText ? '正在回应' : 'GREAT SAGE', Boolean(currentText));
  }, 2100);
});

$('#pet-hide').addEventListener('click', async () => {
  if (window.greatsage?.hidePet) await window.greatsage.hidePet();
  else window.close();
});
$('#pet-console').addEventListener('click', () => window.greatsage?.showConsole());
$('#pet-through').addEventListener('click', async () => {
  if (!window.greatsage?.setClickThrough) return;
  try {
    await window.greatsage.setClickThrough(true);
    $('#pet-through').classList.add('active');
    setMessage('鼠标穿透已开启，10 秒后自动恢复。', '桌宠设置');
    clearTimeout(throughTimer);
    throughTimer = setTimeout(async () => { await window.greatsage.setClickThrough(false); $('#pet-through').classList.remove('active'); setMessage(currentText || lastText); }, 10000);
  } catch (error) { setMessage(error.message || '无法设置鼠标穿透。', '需要处理'); }
});

client.addEventListener('connection', event => {
  if (event.detail.state === 'connected') {
    client.request('/api/status').then(status => setState(status.state || 'idle')).catch(error => setMessage(error.message, '连接提示'));
    if (!currentText) setMessage(lastText);
  } else if (event.detail.state === 'error') { setState('offline'); setMessage('连接暂时断开，正在等待本地服务恢复。', '连接提示'); }
});
client.addEventListener('event', event => {
  const item = event.detail;
  const data = item.data || {};
  switch (item.kind) {
    case 'state': setState(data.state); break;
    case 'memory_updated':
      if (data.deleted_id || data.cleared || ['revise', 'delete', 'clear'].includes(data.action)) {
        currentText = ''; currentTrace = null; lastText = '记录已更新。我在这里，随时听你说。';
        setMessage(lastText, '记忆已更新');
      }
      break;
    case 'stream_reset': streamGap = true; currentText = ''; currentTrace = null; setMessage('实时内容正在重新同步…', '连接提示'); client.request('/api/status').then(status => setState(status.state)).catch(() => {}); break;
    case 'response_start': streamGap = false; currentTrace = item.trace_id; currentText = ''; setState('thinking'); setMessage('让我想一想…', '正在思考'); break;
    case 'response_delta':
      if (streamGap) break;
      if (currentTrace !== item.trace_id) { currentTrace = item.trace_id; currentText = ''; }
      currentText += data.text || data.delta || '';
      setMessage(currentText, '正在回应', true);
      break;
    case 'response_done':
      streamGap = false;
      lastText = data.text || currentText || lastText; currentText = ''; currentTrace = null;
      setMessage(lastText, 'GREAT SAGE');
      break;
    case 'interrupt': currentText = ''; currentTrace = null; setMessage('我在听，你说。', '已停止回应'); setState('listening'); break;
    case 'error': setMessage(data.message || data.error || '遇到一点问题，请查看控制台运行日志。', '需要处理'); setState('error'); break;
  }
});

try {
  const response = await fetch('sage.svg');
  if (!response.ok) throw new Error('无法加载桌宠形象。');
  const xml = new DOMParser().parseFromString(await response.text(), 'image/svg+xml');
  $('#character-art').append(document.importNode(xml.documentElement, true));
  await client.initialize();
} catch (error) { setState('offline'); setMessage(error.message, '连接提示'); }
window.addEventListener('beforeunload', () => { clearTimeout(throughTimer); client.close(); if (window.greatsage?.setClickThrough) window.greatsage.setClickThrough(false); });
