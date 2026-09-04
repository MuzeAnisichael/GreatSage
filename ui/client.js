/** Authenticated local transport shared by the console and desktop companion. */
export class SageClient extends EventTarget {
  constructor() {
    super();
    this.baseUrl = location.origin;
    this.token = '';
    this.socket = null;
    this.retry = 0;
    this.timer = null;
    this.closed = false;
  }

  async initialize() {
    if (window.greatsage?.getConnection) {
      const connection = await window.greatsage.getConnection();
      this.baseUrl = String(connection.baseUrl).replace(/\/$/, '');
      this.token = connection.token || '';
    } else {
      const params = new URLSearchParams(location.hash.slice(1));
      const hashToken = params.get('token');
      if (hashToken) {
        this.token = hashToken;
        sessionStorage.setItem('greatsage-token', hashToken);
        history.replaceState(null, '', location.pathname + location.search);
      } else {
        this.token = sessionStorage.getItem('greatsage-token') || '';
      }
    }
    if (!/^https?:\/\//i.test(this.baseUrl)) throw new Error('请通过桌面应用或后端提供的 HTTP 地址打开界面。');
    this.connect();
    return this;
  }

  async request(path, options = {}) {
    const url = new URL(path, this.baseUrl + '/');
    if (url.origin !== new URL(this.baseUrl).origin) throw new Error('服务请求地址不属于已连接的后端。');
    const headers = { ...options.headers };
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    let response;
    try {
      response = await fetch(url, { ...options, headers, body: options.body === undefined ? undefined : JSON.stringify(options.body) });
    } catch {
      throw new Error('无法连接 GreatSage 服务，请检查后端是否正在运行。');
    }
    const type = response.headers.get('content-type') || '';
    const data = type.includes('json') ? await response.json() : await response.text();
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) throw new Error('连接凭据无效。请从桌面应用重新打开，或使用启动器提供的完整访问地址。');
      const detail = data?.detail || data?.error || data?.message;
      throw new Error(typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : `请求失败（HTTP ${response.status}）`);
    }
    return data;
  }

  async audioBlob(path, signal) {
    const url = new URL(path, this.baseUrl + '/');
    if (url.origin !== new URL(this.baseUrl).origin) throw new Error('音频必须由已连接的后端提供。');
    const response = await fetch(url, { headers: this.token ? { Authorization: `Bearer ${this.token}` } : {}, signal });
    if (!response.ok) throw new Error(`读取语音失败（HTTP ${response.status}）`);
    return response.blob();
  }

  connect() {
    this.closed = false;
    clearTimeout(this.timer);
    if (this.socket) {
      this.socket.onclose = null;
      this.socket.close();
    }
    const url = new URL('/ws', this.baseUrl);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    if (this.token) url.searchParams.set('token', this.token);
    this.dispatchEvent(new CustomEvent('connection', { detail: { state: 'connecting' } }));
    const socket = new WebSocket(url);
    this.socket = socket;
    socket.onopen = () => {
      this.retry = 0;
      this.dispatchEvent(new CustomEvent('connection', { detail: { state: 'connected' } }));
    };
    socket.onmessage = (message) => {
      try {
        const event = JSON.parse(message.data);
        if (event && typeof event.kind === 'string') this.dispatchEvent(new CustomEvent('event', { detail: event }));
      } catch { /* Non-JSON frames cannot be rendered as events. */ }
    };
    socket.onerror = () => {
      this.dispatchEvent(new CustomEvent('connection', { detail: { state: 'error', message: '实时事件连接失败，正在尝试重新连接。' } }));
    };
    socket.onclose = () => {
      if (this.closed) return;
      this.dispatchEvent(new CustomEvent('connection', { detail: { state: 'error', message: '实时连接已断开，正在尝试恢复。' } }));
      this.timer = setTimeout(() => this.connect(), Math.min(1000 * 2 ** this.retry++, 15000));
    };
  }

  close() {
    this.closed = true;
    clearTimeout(this.timer);
    this.socket?.close();
  }
}

export const stateLabels = {
  idle: '待命中', ready: '待命中', listening: '正在聆听', transcribing: '正在识别', thinking: '正在思考',
  responding: '正在回应', speaking: '正在说话', error: '需要处理', starting: '正在启动', stopped: '已停止',
};

export function sourceLabel(source) {
  if (!source) return '输入';
  if (typeof source === 'object') return source.name || source.type || '音频';
  if (String(source).startsWith('microphone:')) return `麦克风 · ${String(source).slice(11)}`;
  if (String(source).startsWith('process:')) return `程序 · PID ${String(source).slice(8)}`;
  return ({ microphone: '麦克风', mic: '麦克风', system: '系统声音', desktop: '桌面声音', process: '程序声音', text: '文字', keyboard: '文字', user: '文字', manual: '手动添加', assistant: '秘书', compression: '上下文摘要' })[source] || String(source);
}

export function timeLabel(value, date = false) {
  if (!value) return '刚刚';
  const time = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
  if (Number.isNaN(time.getTime())) return String(value);
  return new Intl.DateTimeFormat('zh-CN', date ? { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' } : { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(time);
}

export function safeString(value) {
  return typeof value === 'string' ? value : value === undefined || value === null ? '' : JSON.stringify(value);
}

export function redact(value) {
  if (Array.isArray(value)) return value.map(redact);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, /^(api_key|authorization|password|access_token|secret|token)$/i.test(key) ? '[已脱敏]' : redact(item)]));
  if (typeof value === 'string') return value.replace(/\bBearer\s+[^\s"']+/gi, 'Bearer [已脱敏]').replace(/\bsk-[A-Za-z0-9_-]{8,}/g, '[已脱敏]').replace(/([?&]token=)[^&\s"']+/gi, '$1[已脱敏]');
  return value;
}
