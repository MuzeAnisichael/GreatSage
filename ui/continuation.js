function yesterday() {
  const date = new Date();
  date.setDate(date.getDate() - 1);
  return [date.getFullYear(), String(date.getMonth() + 1).padStart(2, '0'), String(date.getDate()).padStart(2, '0')].join('-');
}

const localTime = value => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';

export function setupContinuation({ client, getSession, onCreated, toast }) {
  const dialog = document.createElement('dialog');
  dialog.className = 'record-dialog continuation-dialog';
  dialog.innerHTML = `<form><div class="dialog-actions"><button class="button ghost small continuation-close" type="button">关闭</button></div>
    <h2>接续工作</h2><p class="source-note continuation-session"></p>
    <label class="field"><span>关注的问题（可选）</span><textarea maxlength="4000" rows="2" placeholder="例如：昨天决定了什么，还有哪些事待确认？"></textarea></label>
    <label class="field"><span>会话记录范围 · 本机时间</span><select class="continuation-range"><option value="auto">会话至今（提到“昨天”时仅取昨天）</option><option value="day">指定日期</option></select></label>
    <label class="field continuation-date-field" hidden><span>日期</span><input type="date"></label>
    <p class="continuation-scope source-note" role="status"></p>
    <details><summary>附加 Markdown 资料（可选）</summary><div class="continuation-materials"></div></details>
    <p class="field-error continuation-error" role="alert" hidden></p>
    <div class="panel-tools"><button class="button primary continuation-start" type="submit" disabled>生成接续简报</button></div></form>`;
  document.body.append(dialog);
  const find = selector => dialog.querySelector(selector);
  const question = find('textarea'), range = find('select'), date = find('input[type=date]');
  const scope = find('.continuation-scope'), start = find('.continuation-start'), error = find('.continuation-error');
  const materials = find('.continuation-materials');
  const selectedDate = () => range.value === 'day' ? date.value : /昨天|昨日/.test(question.value) ? yesterday() : '';
  let session = '', revision = 0, serial = 0, requestedDate, valid = false, submitting = false, loaded = false;
  function sync() { start.disabled = !valid || submitting || !loaded; }
  function report(message) { error.textContent = message; error.hidden = !message; }
  function close() {
    serial++; revision++; session = ''; valid = false;
    if (dialog.open) dialog.close();
  }
  async function preview(force = false) {
    const day = selectedDate();
    if (!force && requestedDate === day) return;
    requestedDate = day;
    const token = ++revision;
    valid = false; sync(); report('');
    if (range.value === 'day' && !day) { scope.textContent = '请选择日期。'; return; }
    scope.textContent = '正在读取会话范围…';
    const query = new URLSearchParams({ session_id: session });
    if (day) query.set('local_date', day);
    try {
      const result = await client.request('/api/continuation/scope?' + query);
      if (token !== revision || !dialog.open) return;
      const period = day || '整个会话至今';
      valid = result.message_count > 0;
      scope.textContent = valid ? `${period} · ${result.message_count} 条会话记录 · ${localTime(result.first_at)} 至 ${localTime(result.last_at)}`
        : `${period}没有可用的会话记录。请调整日期或选择其他会话。`;
    } catch (err) {
      if (token === revision) { scope.textContent = '会话范围暂不可用。'; report(err.message); }
    } finally { if (token === revision) sync(); }
  }
  async function open() {
    const id = getSession();
    if (!id) { toast('请先打开一个会话。'); return; }
    const token = ++serial;
    session = id; submitting = false; loaded = false; valid = false;
    find('form').reset(); date.value = yesterday(); find('.continuation-date-field').hidden = true;
    find('details').open = false; find('.continuation-session').textContent = '使用当前会话 ' + id.slice(0, 8);
    materials.textContent = '正在读取资料…'; sync(); dialog.showModal();
    const scopeRequest = preview(true);
    try {
      const rows = await client.request('/api/materials');
      if (serial !== token) return;
      materials.replaceChildren();
      for (const item of rows.filter(row => row.status === 'active')) {
        const label = document.createElement('label'), input = document.createElement('input');
        input.type = 'checkbox'; input.value = item.id;
        label.append(input, document.createTextNode(item.name + ' · v' + item.version)); materials.append(label);
      }
      if (!materials.children.length) materials.textContent = '资料库中暂无可选 Markdown 文件。';
    } catch {
      if (serial === token) materials.textContent = '资料列表暂不可用，可以仅用会话原文生成。';
    } finally { if (token === serial) { loaded = true; sync(); } }
    await scopeRequest;
  }
  find('.continuation-close').addEventListener('click', close);
  dialog.addEventListener('cancel', close);
  range.addEventListener('change', () => { find('.continuation-date-field').hidden = range.value !== 'day'; preview(true); });
  date.addEventListener('change', () => preview(true));
  question.addEventListener('input', () => preview());
  find('form').addEventListener('submit', async event => {
    event.preventDefault();
    if (start.disabled || session !== getSession()) return;
    const token = serial, id = session, day = selectedDate();
    const body = { kind: 'continuation', session_ids: [id], material_ids: [...materials.querySelectorAll(':checked')].map(input => input.value), instructions: question.value.trim() };
    if (day) body.local_date = day;
    submitting = true; sync(); report('');
    try {
      const task = await client.request('/api/tasks', { method: 'POST', body });
      if (serial !== token || session !== getSession()) return;
      close(); onCreated(task, id);
    } catch (err) { if (serial === token) report(err.message); }
    finally { if (serial === token) { submitting = false; sync(); } }
  });
  return { open, close };
}
