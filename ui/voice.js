import { sourceLabel } from './client.js';

const states = { starting: '正在连接', running: '运行中', closed: '已关闭', failed: '已停用', stopped: '采集已停止' };
const reasons = {
  voice_source_stopped: '音频采集已停止，请检查设备连接或程序是否仍在运行。',
  voice_source_audio_gap: '音频传输出现缺口，已停止本音源识别。',
  voice_source_backpressure: '识别处理跟不上音频输入，已停止本音源识别。',
  streaming_asr_credentials_required: '尚未配置流式识别密钥。',
  streaming_asr_configuration_invalid: '流式识别配置不可用。',
  streaming_asr_endpoint_not_allowed: '请使用受支持的语音识别服务地址。',
  streaming_asr_language_not_supported: '当前流式识别语言不受支持。',
  streaming_asr_model_not_supported: '当前流式识别模型不受支持。',
  streaming_asr_dependency_unavailable: '语音识别组件不可用，请检查应用安装与运行日志。',
  streaming_asr_connection_closed: '语音识别连接已断开。',
  streaming_asr_connection_failed: '未能连接语音识别服务，请检查网络与密钥。',
  streaming_asr_connection_timeout: '连接语音识别服务超时。',
  streaming_asr_connection_unavailable: '语音识别服务连接不可用。',
  streaming_asr_transport_failed: '语音识别传输中断。',
  streaming_asr_finalize_timeout: '等待完整转写超时，当前语音未提交。',
  streaming_asr_turn_overlap: '前一句尚未完成转写，新语音已经开始。',
  streaming_asr_segment_overlap: '转写时间范围重叠，已停止本音源以避免重复内容。',
  streaming_asr_timestamp_missing: '识别结果缺少时间信息，无法安全归入当前话轮。',
  streaming_asr_turn_too_large: '当前话轮超出处理上限，请分段说话。',
};

export function describeVoiceSource(source) {
  return {
    label: source.source === 'microphone:default' ? '麦克风 · 默认设备' : sourceLabel(source.source), state: states[source.state] || '等待音频',
    reason: source.error_code ? reasons[source.error_code] || '本音源识别已中断，请查看运行日志。' : '',
  };
}

/** Shared source health/actions. Fetch metadata only; never restart or replay automatically. */
export function setupVoiceHealth({ client, busy, toast, openAsrSettings, openAudioControls }) {
  const panel = document.createElement('section'); panel.id = 'voice-health'; panel.className = 'voice-health';
  panel.setAttribute('aria-label', '音源运行状态');
  const heading = document.createElement('strong'); heading.textContent = '音源状态';
  const note = document.createElement('p'); note.id = 'voice-health-note'; note.className = 'source-note'; note.setAttribute('role', 'status');
  const list = document.createElement('div'); list.id = 'voice-health-sources';
  panel.append(heading, note, list); document.querySelector('#refresh-sources').after(panel);
  let snapshot = null, epoch = 0, timer, connected = false, failed = false;
  const pending = new Set(), subscribers = new Set();
  const notify = () => { render(); for (const callback of subscribers) callback(); };

  function actionButton(source) {
    const labels = { retry_source: '重试此音源', restart_listening: '检查音源', configure_asr: '打开识别设置' };
    if (!labels[source.recovery_action]) return null;
    const button = document.createElement('button'); button.type = 'button'; button.className = 'button ghost small voice-recovery';
    button.textContent = pending.has(source.source) ? '正在重试…' : labels[source.recovery_action];
    button.dataset.source = source.source; button.dataset.action = source.recovery_action;
    button.disabled = !connected || pending.has(source.source);
    button.setAttribute('aria-label', `${labels[source.recovery_action]}：${sourceLabel(source.source)}`);
    button.onclick = () => busy(button, async () => {
      if (source.recovery_action === 'configure_asr') { openAsrSettings(); return; }
      if (source.recovery_action === 'restart_listening') {
        openAudioControls();
        toast('请先暂停聆听，检查设备连接或程序状态，必要时重新选择音源，再开始聆听。', '', 10000);
        return;
      }
      if (pending.has(source.source)) return;
      pending.add(source.source); ++epoch; clearTimeout(timer); notify();
      try {
        // Events may arrive while this POST runs. Read the authoritative snapshot
        // afterwards instead of allowing its older response to replace new state.
        await client.request('/api/audio/retry', { method: 'POST', body: { source: source.source } });
        toast('已请求重试此音源；仅处理新音频，未完成的旧语音不会重放。其他音源继续运行。');
      } finally {
        pending.delete(source.source);
        await refresh().catch(() => {});
        notify();
      }
    });
    return button;
  }

  function render() {
    const sources = snapshot?.sources || [];
    note.textContent = !connected ? '服务未连接，已有状态仅供参考。'
      : failed ? '暂时无法更新音源状态；请检查服务连接。'
      : !snapshot ? '正在读取音源状态…'
      : !snapshot.listening ? '尚未聆听。选择音源后点击“开始聆听”。'
      : !sources.length ? '等待所选音源的音频。'
      : sources.some(source => source.recovery_action) ? '需要处理的音源见下方。重试只接收新音频，不重放旧语音。'
      : '所选音源分别处理，桌面声音仅作为观察信息。';
    list.replaceChildren();
    for (const source of sources) {
      const detail = describeVoiceSource(source);
      const row = document.createElement('article'); row.className = 'voice-health-source'; row.dataset.source = source.source; row.dataset.state = source.state;
      const title = document.createElement('div'); title.className = 'voice-health-title';
      const label = document.createElement('strong'); label.textContent = detail.label;
      const state = document.createElement('span'); state.className = 'tag'; state.textContent = detail.state;
      title.append(label, state); row.append(title);
      if (detail.reason) {
        const reason = document.createElement('p'); reason.className = 'voice-health-reason'; reason.textContent = detail.reason;
        if (source.recovery_action === 'restart_listening') reason.textContent += ' 暂停聆听并检查后，再重新开启。';
        row.append(reason);
      }
      const button = actionButton(source); if (button) row.append(button);
      list.append(row);
    }
  }

  async function refresh() {
    clearTimeout(timer);
    const request = ++epoch;
    try {
      const result = await client.request('/api/audio/status');
      if (request !== epoch) return;
      snapshot = result; failed = false; notify();
    } catch (error) {
      if (request === epoch) { failed = true; notify(); }
      throw error;
    }
  }
  function schedule() {
    ++epoch; clearTimeout(timer);
    timer = setTimeout(() => refresh().catch(() => {}), 180);
  }
  render();
  return {
    refresh, actionButton, get snapshot() { return snapshot; },
    subscribe(callback) { subscribers.add(callback); },
    setConnected(value) { connected = value; if (!value) { ++epoch; clearTimeout(timer); } notify(); },
    onEvent(event) {
      if (['audio_source', 'audio_recovery', 'audio_gap', 'listening', 'state', 'error', 'session', 'stream_reset'].includes(event.kind)) schedule();
    },
  };
}
