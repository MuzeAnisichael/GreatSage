/** Record one correction and return its editable text to the active document. */
export function setupCorrectionVoice({ client, container, textarea, getTarget, onBusyChange }) {
  const startButton = button('录制语音纠正', 'button ghost small', start);
  const finishButton = button('停止并转写', 'button small', finish);
  const cancelButton = button('取消录音', 'button subtle small', () => reset('已取消语音纠正。'));
  const status = document.createElement('span');
  status.className = 'source-note';
  status.setAttribute('role', 'status');
  const controls = document.createElement('div');
  controls.className = 'dialog-actions';
  controls.append(startButton, finishButton, cancelButton);
  container.append(controls, status);
  let active = null;
  render('');

  function button(label, style, onClick) {
    const item = document.createElement('button');
    item.type = 'button';
    item.textContent = label;
    item.className = style;
    item.addEventListener('click', onClick);
    return item;
  }

  function render(message) {
    startButton.hidden = Boolean(active);
    finishButton.hidden = active?.stage !== 'recording';
    cancelButton.hidden = !active;
    cancelButton.textContent = active?.stage === 'transcribing' ? '取消转写' : '取消录音';
    status.textContent = message;
    onBusyChange?.(Boolean(active));
  }

  function matches(target, other) {
    return other && target.artifact_id === other.artifact_id && target.session_id === other.session_id
      && target.expected_version === other.expected_version;
  }

  function dropCapture(id) {
    if (id) client.request('/api/correction-voice/' + encodeURIComponent(id), { method: 'DELETE' }).catch(() => {});
  }

  function reset(message = '') {
    if (active) {
      clearTimeout(active.timer);
      dropCapture(active.id);
      active = null;
    }
    render(message);
  }

  async function start() {
    const target = getTarget();
    if (!target || active) return;
    const entry = { target: { ...target }, stage: 'opening', id: null, timer: null };
    active = entry;
    render('正在打开麦克风…');
    try {
      const recording = await client.request('/api/artifacts/' + encodeURIComponent(target.artifact_id) + '/correction-voice/start', {
        method: 'POST', body: { session_id: target.session_id, expected_version: target.expected_version },
      });
      if (active !== entry || !matches(entry.target, getTarget())) {
        dropCapture(recording.capture_id);
        if (active === entry) reset();
        return;
      }
      entry.id = recording.capture_id;
      entry.stage = 'recording';
      entry.timer = setTimeout(() => reset('录音已超时并丢弃，可重新录制。'), recording.max_seconds * 1000);
      render('正在录音，限 ' + recording.max_seconds + ' 秒。停止后将通过 ' + recording.provider + ' 转写。');
    } catch (error) {
      if (active === entry) reset(error.message);
    }
  }

  async function finish() {
    const entry = active;
    if (entry?.stage !== 'recording') return;
    clearTimeout(entry.timer);
    entry.stage = 'transcribing';
    render('正在转写，完成后可编辑…');
    try {
      const result = await client.request('/api/correction-voice/' + encodeURIComponent(entry.id) + '/finish', { method: 'POST' });
      if (active !== entry) return;
      if (!matches(entry.target, getTarget()) || !matches(entry.target, result)) {
        reset('文档或版本已变更，请重新录制。');
        return;
      }
      const separator = textarea.value && !/\s$/.test(textarea.value) ? '\n' : '';
      textarea.value += separator + result.text;
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
      active = null;
      render('转写已填入纠正框，可编辑后提交。');
      textarea.focus();
    } catch (error) {
      if (active === entry) reset(error.message);
    }
  }

  return { reset };
}
