// /cron page logic (vanilla JS, no framework). The HTML shell + CSS live in
// webapp/cron_views.py; this file is served at /static/cron.js with an
// mtime cache-buster. State hydrates from GET /cron/api/tree and saves via
// debounced whole-tree PUTs (version-guarded; see docs/cron-design.md).

// ---- helpers ----
function ppOpt(value, label){
  const o = document.createElement('option');
  o.value = value; o.textContent = label; return o;
}

// ---- populate the five schedule dropdowns ----
// Fill the five schedule dropdowns for a given id prefix ('f' = New-job
// builder, 'es' = Edit-schedule overlay) — fresh <option> nodes per prefix.
function cronFillScheduleSelects(prefix){
  const min = document.getElementById(prefix + '-min');
  [['*','Every minute'],['*/5','Every 5 minutes'],['*/10','Every 10 minutes'],
   ['*/15','Every 15 minutes'],['*/30','Every 30 minutes'],['0','At minute 0']]
    .forEach(p => min.appendChild(ppOpt(p[0], p[1])));
  for (let i = 1; i < 60; i++) min.appendChild(ppOpt(String(i), 'At minute ' + i));  // 0 is already a preset above

  const hour = document.getElementById(prefix + '-hour');
  hour.appendChild(ppOpt('*', 'Every hour'));
  for (let i = 0; i < 24; i++) hour.appendChild(ppOpt(String(i), 'At hour ' + i));

  const dom = document.getElementById(prefix + '-dom');
  dom.appendChild(ppOpt('*', 'Every day'));
  for (let i = 1; i <= 31; i++) dom.appendChild(ppOpt(String(i), 'Day ' + i));

  const mon = document.getElementById(prefix + '-mon');
  mon.appendChild(ppOpt('*', 'Every month'));
  ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
    .forEach((m, i) => mon.appendChild(ppOpt(String(i + 1), m)));

  const dow = document.getElementById(prefix + '-dow');
  dow.appendChild(ppOpt('*', 'Every weekday'));
  ['Sun','Mon','Tue','Wed','Thu','Fri','Sat']
    .forEach((d, i) => dow.appendChild(ppOpt(String(i), d)));
}
cronFillScheduleSelects('f');   // New-job builder
cronFillScheduleSelects('es');  // Edit-schedule overlay

// ---- assemble + display the cron string ----
function cronCurrentFrom(prefix){
  return [prefix + '-min', prefix + '-hour', prefix + '-dom', prefix + '-mon', prefix + '-dow']
    .map(id => document.getElementById(id).value).join(' ');
}
function cronCurrent(){ return cronCurrentFrom('f'); }
// Plain-English description of a cron string. Covers exactly the grammar the
// dropdowns can produce: each field is "*", a "*/N" step (minute only), or a
// specific number — no ranges or lists. Same function feeds the builder hint
// and the table's "explanation" column so they always agree.
const CRON_MONTHS = ['January','February','March','April','May','June','July',
  'August','September','October','November','December'];
const CRON_DAYS = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
function cronPad2(n){ return String(n).padStart(2, '0'); }
function cronTimeClause(mn, hr){
  const step = mn.match(/^\*\/(\d+)$/);
  if (hr === '*'){
    if (mn === '*') return 'every minute';
    if (step) return 'every ' + step[1] + ' minutes';
    return 'every hour at minute ' + Number(mn);
  }
  if (mn === '*') return 'every minute during hour ' + cronPad2(hr) + ' (24h)';
  if (step) return 'every ' + step[1] + ' minutes during hour ' + cronPad2(hr) + ' (24h)';
  return 'at ' + cronPad2(hr) + ':' + cronPad2(mn) + ' (24h)';
}
function cronDateClause(dom, mon, dow){
  const parts = [];
  if (dom !== '*' && mon !== '*'){
    parts.push('on ' + CRON_MONTHS[Number(mon) - 1] + ' ' + Number(dom));
  } else if (dom !== '*'){
    parts.push('on day ' + Number(dom) + ' of the month');
  } else if (mon !== '*'){
    parts.push('every day in ' + CRON_MONTHS[Number(mon) - 1]);
  }
  if (dow !== '*') parts.push('on ' + CRON_DAYS[Number(dow)]);
  return parts.join(', ');
}
function cronDescribe(cron){
  const p = (cron || '').trim().split(/\s+/);
  if (p.length !== 5) return '';
  let out = cronTimeClause(p[0], p[1]);
  const date = cronDateClause(p[2], p[3], p[4]);
  if (date) out += ' ' + date;
  else if (/^at \d/.test(out)) out += ' every day';
  return out;
}
function cronRefresh(){
  const s = cronCurrent();
  document.getElementById('cron-string').textContent = s;
  const d = cronDescribe(s);
  document.getElementById('cron-hint').textContent = d ? '— ' + d : '';
}
['f-min','f-hour','f-dom','f-mon','f-dow'].forEach(id =>
  document.getElementById(id).addEventListener('change', cronRefresh));
// Live preview inside the Edit-schedule overlay (its own 'es-' prefixed selects).
function cronRefreshEditSchedule(){
  const s = cronCurrentFrom('es');
  document.getElementById('es-cron-string').textContent = s;
  const d = cronDescribe(s);
  document.getElementById('es-cron-hint').textContent = d ? '— ' + d : '';
}
['es-min','es-hour','es-dom','es-mon','es-dow'].forEach(id =>
  document.getElementById(id).addEventListener('change', () => {
    cronRefreshEditSchedule(); cronUpdateSchedSaveState();
  }));
document.getElementById('es-tz').addEventListener('change', cronUpdateSchedSaveState);
// The Edit-schedule "Save" is disabled until the schedule (cron + timezone)
// differs from the job's original — captured when the overlay opens.
let cronSchedOrigCron = '';
let cronSchedOrigTz = '';
function cronUpdateSchedSaveState(){
  const changed = cronCurrentFrom('es') !== cronSchedOrigCron
    || document.getElementById('es-tz').value !== cronSchedOrigTz;
  document.getElementById('es-save').disabled = !changed;
}

// ---- action-type toggle (Message vs Command) ----
function cronActiveType(){
  return document.querySelector('input[name="atype"]:checked').value;
}
function cronToggleType(){
  const t = cronActiveType();
  document.getElementById('msg-fields').style.display = (t === 'message') ? '' : 'none';
  document.getElementById('cmd-fields').style.display = (t === 'command') ? '' : 'none';
}
document.querySelectorAll('input[name="atype"]').forEach(r =>
  r.addEventListener('change', cronToggleType));
// Same toggle, inside the Edit-action overlay ('ea-atype' radios).
function cronEditActiveType(){
  return document.querySelector('input[name="ea-atype"]:checked').value;
}
function cronToggleEditActionType(){
  const t = cronEditActiveType();
  document.getElementById('ea-msg-fields').style.display = (t === 'message') ? '' : 'none';
  document.getElementById('ea-cmd-fields').style.display = (t === 'command') ? '' : 'none';
}
document.querySelectorAll('input[name="ea-atype"]').forEach(r =>
  r.addEventListener('change', () => {
    cronToggleEditActionType(); cronUpdateActionSaveState();
  }));
['ea-message','ea-command'].forEach(id =>
  document.getElementById(id).addEventListener('input', cronUpdateActionSaveState));
// ea-target / ea-retries are <select>s; they fire 'change', not 'input'.
document.getElementById('ea-target').addEventListener('change', cronUpdateActionSaveState);
document.getElementById('ea-retries').addEventListener('change', cronUpdateActionSaveState);
// The Edit-action "Save" is disabled until the action (type/target/message/
// command/retries) differs from the job's original — captured when the overlay opens.
let cronActionOrig = {type: '', target: '', message: '', command: '', retries: 0};
function cronUpdateActionSaveState(){
  const changed = cronEditActiveType() !== cronActionOrig.type
    || document.getElementById('ea-target').value.trim() !== cronActionOrig.target
    || document.getElementById('ea-message').value.trim() !== cronActionOrig.message
    || document.getElementById('ea-command').value.trim() !== cronActionOrig.command
    || (parseInt(document.getElementById('ea-retries').value, 10) || 0) !== cronActionOrig.retries;
  document.getElementById('ea-save').disabled = !changed;
}
// Select `n` in a retries <select>, adding the option if the stored value
// isn't one of the presets (so an externally-saved value isn't silently lost).
function cronSetRetriesSelect(id, n){
  const sel = document.getElementById(id);
  if (![...sel.options].some(o => o.value === String(n))){
    sel.appendChild(ppOpt(String(n), n + '×'));
  }
  sel.value = String(n);
}

// ---- row state (browser-only, lost on refresh) ----
let cronRowsState = [];
let cronEditUuid = null;
let cronFolders = [];           // {id, name, parentId}
let cronSelectedFolder = null;  // null = "All jobs"
let cronExpanded = {};          // folder id -> false when collapsed (default expanded)
let cronDrag = null;            // {type:'folder'|'job', id} while a node is dragged
let cronCreating = false;       // true while the dedicated "+ Job" create form is open
let cronChatrooms = [];         // [{uuid, name}] for the message-target picker

// Current name of a chatroom uuid (a message job's target), for display. Falls
// back to "(unknown room)" / "(none)" so a deleted/blank target reads sensibly.
function cronRoomName(uuid){
  if (!uuid) return '(none)';
  const r = cronChatrooms.find(x => x.uuid === uuid);
  return r ? ('#' + r.name) : '(unknown room)';
}
// Fill a target <select> with the chatrooms (value = uuid, label = #name) and
// select `selectedUuid`. A first "(cron room)" option = empty target.
function cronPopulateTargetSelect(selectId, selectedUuid){
  const sel = document.getElementById(selectId);
  if (!sel) return;
  sel.innerHTML = '';
  sel.appendChild(ppOpt('', '(cron room — default)'));
  cronChatrooms.forEach(r => sel.appendChild(ppOpt(r.uuid, '#' + r.name)));
  sel.value = selectedUuid || '';
}

function escapeHtml(s){
  return (s || '').replace(/[&<>"]/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
// Next-run cell: when the scheduler will fire this job next (hydrated as the
// read-only job.next_run_at). Shows why it WON'T fire instead, where relevant:
// disabled (own/ancestor toggle), globally paused, or a draft (slot skipped).
function cronNextRunCell(r){
  if (!cronJobLive(r)) return '<td class="cron-nextrun-cell"><span class="muted">—</span></td>';
  if (cronPaused) return '<td class="cron-nextrun-cell"><span class="crh-pending" title="global pause is on — nothing fires until resumed">paused</span></td>';
  if (!r.next_run_at) return '<td class="cron-nextrun-cell"><span class="muted" title="not scheduled yet">—</span></td>';
  if (cronJobIsDraft(r)){
    return '<td class="cron-nextrun-cell"><span class="muted" title="draft — the slot will be skipped until the action is filled in">' +
      cronFmtDate(r.next_run_at) + '</span></td>';
  }
  return '<td class="cron-nextrun-cell"><span title="' + escapeHtml(cronFmtDateExact(r.next_run_at)) + '">' +
    cronFmtDate(r.next_run_at) + '</span></td>';
}
// Health cell for the All-jobs / Folder-details lists: the job's latest run
// outcome (hydrated as job.last_run), at a glance — hover for the details
// (when it fired, the trigger, and the error text on a failure).
function cronHealthCell(r){
  const lr = r.last_run;
  if (!lr) return '<td class="cron-health-cell"><span class="muted" title="never fired">—</span></td>';
  const when = cronFmtDate(lr.fired_at) + ' (' + lr.trigger + ')';
  let cls, label, title;
  if (lr.status === 'ok'){ cls = 'crh-ok'; label = '\u2713 ok'; title = 'last run ok · ' + when; }
  else if (lr.status === 'pending'){ cls = 'crh-pending'; label = '\u2026 running'; title = 'last run still in flight · ' + when; }
  else { cls = 'crh-error'; label = '\u2716 error'; title = 'last run failed · ' + when + (lr.error ? ' — ' + lr.error : ''); }
  return '<td class="cron-health-cell"><span class="' + cls + '" title="' + escapeHtml(title) + '">' + label + '</span></td>';
}
function cronActionText(r){
  if (r.type === 'message') return 'msg → ' + cronRoomName(r.target) + ': ' + r.message;
  if (r.type === 'backup') return 'backup → ' + (r.command || 'RAINBOX_BACKUP_REPO');
  return 'cmd: ' + r.command;
}
function cronFlattenTree(parentId){
  // Depth-first list of everything under parentId (null = whole tree), in the
  // same order as the left tree, each row tagged with its nesting `depth`.
  parentId = parentId || null;
  const out = [];
  const walk = (f, depth) => {
    out.push({kind: 'folder', node: f, depth: depth});
    cronChildFolders(f.id).forEach(c => walk(c, depth + 1));
    cronJobsInFolder(f.id).forEach(j => out.push({kind: 'job', node: j, depth: depth + 1}));
  };
  cronChildFolders(parentId).forEach(f => walk(f, 0));
  cronJobsInFolder(parentId).forEach(j => out.push({kind: 'job', node: j, depth: 0}));
  return out;
}
function cronListNodes(){
  // The rows for the right-pane list, in the same order as the left tree.
  if (cronEditUuid){  // single-job detail
    const j = cronRowsState.find(x => x.uuid === cronEditUuid);
    return j ? [{kind: 'job', node: j, depth: 0}] : [];
  }
  // All jobs (null = root) or a folder's full subtree.
  return cronFlattenTree(cronSelectedFolder);
}
function cronRender(){
  cronRenderRename();
  cronRenderDetail();
  cronRenderFolderDesc();
  cronRenderJobDetail();
  cronSyncUrl();
  // View composition: the New-job builder is a create-only modal; editing a job
  // shows the Job-details panel (#cron-job-detail) with its own edit overlays.
  const editing = !!cronEditUuid;
  document.getElementById('add-btn').textContent = 'Create job';
  document.getElementById('cancel-btn').style.display = cronCreating ? '' : 'none';
  const builder = document.getElementById('cron-builder');
  builder.hidden = !cronCreating;
  builder.classList.toggle('cron-as-modal', cronCreating);
  document.getElementById('cron-modal-backdrop').hidden = !cronCreating;
  const bt = document.getElementById('cron-builder-title');
  bt.textContent = 'New job';
  bt.hidden = !cronCreating;
  // Builder is create-only now, so its Name row is always shown when it's open.
  document.getElementById('cron-name-row').hidden = false;
  // Right-pane title: what the user is viewing details for. Stays visible
  // behind the create modal (based on the selection, not the creating flag).
  const paneTitle = document.getElementById('cron-pane-title');
  if (editing){
    paneTitle.hidden = false; paneTitle.textContent = 'Job details';
  } else if (cronSelectedFolder !== null){
    paneTitle.hidden = false; paneTitle.textContent = 'Folder details';
  } else {
    paneTitle.hidden = false; paneTitle.textContent = 'All jobs';
  }
  // The job list hides only while editing a single job (its Active toggle +
  // builder are the detail). It stays visible behind the create modal.
  document.getElementById('cron-table-wrap').hidden = editing;
  const tb = document.getElementById('cron-rows');
  tb.innerHTML = '';
  const nodes = cronListNodes();
  if (!nodes.length){
    const msg = cronSelectedFolder === null ? 'no jobs yet' : 'empty folder';
    tb.innerHTML = '<tr><td colspan="9"><i>' + msg + '</i></td></tr>';
    return;
  }
  nodes.forEach(item => {
    // Indent the name cell by nesting depth, mimicking the left tree (20px/level).
    const pad = 9 + item.depth * 20;
    if (item.kind === 'folder'){
      // Folder row: Active / uuid / name (tree folder icon) + its description
      // and a Details link; the schedule/command columns stay blank.
      const f = item.node;
      const tr = document.createElement('tr');
      tr.className = 'cron-folder-row' + (cronFolderEnabled(f.id) ? '' : ' cron-off');
      const short = f.id.split('-')[0];
      tr.innerHTML =
        '<td class="cron-active-cell">' + (f.enabled !== false ? 'Active' : 'Inactive') + '</td>' +
        '<td><code title="' + f.id + '">' + short + '</code></td>' +
        '<td class="cron-name-cell" style="padding-left:' + pad + 'px"><span class="cron-ficon">' + CRON_ICON_FOLDER + '</span> <span class="cron-name" title="' + escapeHtml(cronNodePath('folder', f)) + '">' + escapeHtml(f.name) + '</span></td>' +
        '<td></td><td></td><td></td><td></td><td>' + escapeHtml(f.description || '') + '</td>' +
        '<td><a href="#" class="row-details">Details</a></td>';
      tr.querySelector('.row-details').addEventListener('click', e => { e.preventDefault(); cronSelectFolder(f.id); });
      tb.appendChild(tr);
      return;
    }
    const r = item.node;
    const tr = document.createElement('tr');
    if (!cronJobLive(r)) tr.className = 'cron-off';
    const short = r.uuid.split('-')[0];
    // Schedule cell: cron string, then the explanation and the time zone (so
    // "06:45" is unambiguous between local time and UTC).
    const desc = cronDescribe(r.cron);
    const schedSub = (desc ? desc + ' · ' : '') + cronTzLabel(r.timezone);
    const activeLabel = r.enabled === false ? 'Inactive'
      : (cronJobIsDraft(r)
         ? '<span class="cron-draft" title="No command/message yet — the scheduler skips this job until its action is filled in">Draft</span>'
         : 'Active');
    tr.innerHTML =
      '<td class="cron-active-cell">' + activeLabel + '</td>' +
      '<td><code title="' + r.uuid + '">' + short + '</code></td>' +
      '<td class="cron-name-cell" style="padding-left:' + pad + 'px"><span class="cron-name" title="' + escapeHtml(cronNodePath('job', r)) + '">' + escapeHtml(r.name) + '</span></td>' +
      '<td class="cron-sched-cell"><code>' + escapeHtml(r.cron) + '</code><br><span class="muted">' + escapeHtml(schedSub) + '</span></td>' +
      cronNextRunCell(r) +
      cronHealthCell(r) +
      '<td>' + escapeHtml(cronActionText(r)) + '</td>' +
      '<td>' + escapeHtml(r.description) + '</td>' +
      '<td><a href="#" class="row-details">Details</a></td>';
    // "Details" opens the job's detail view; edit happens there, delete on the kebab.
    tr.querySelector('.row-details').addEventListener('click', e => { e.preventDefault(); cronSelectJob(r.uuid); });
    tb.appendChild(tr);
  });
}

function cronClearInputs(){
  ['f-name','f-message','f-command','f-desc'].forEach(id =>
    document.getElementById(id).value = '');
  cronPopulateTargetSelect('f-target', '');  // refresh rooms + reset to default
  document.getElementById('f-folder').value = cronSelectedFolder || '';
  document.getElementById('f-tz').value = 'localtime';  // default new jobs to local time
  document.getElementById('f-retries').value = '0';     // retries default off
}
// Human label for a stored timezone choice.
function cronTzLabel(tz){ return tz === 'UTC' ? 'UTC' : 'Local time'; }
// Optimistically stamp a node (folder or job) as just-modified, so the
// Created/Modified line in the details pane reflects an edit immediately. The
// server sets the authoritative updated_at on save; a reload reconciles.
function cronTouch(node){ if (node) node.updated_at = new Date().toISOString(); }
function cronAddOrUpdate(){
  const err = document.getElementById('form-err');
  err.textContent = '';
  const t = cronActiveType();
  const row = {
    name: document.getElementById('f-name').value.trim(),
    folderId: document.getElementById('f-folder').value || null,
    cron: cronCurrent(),
    timezone: document.getElementById('f-tz').value,
    type: t,
    target: document.getElementById('f-target').value.trim(),
    message: document.getElementById('f-message').value.trim(),
    command: document.getElementById('f-command').value.trim(),
    description: document.getElementById('f-desc').value.trim(),
    maxRetries: parseInt(document.getElementById('f-retries').value, 10) || 0,
  };
  if (!row.name){
    err.textContent = 'Name is required.'; return;
  }
  if (t === 'message' && !row.message){
    err.textContent = 'Message needs a message (target defaults to the cron room).'; return;
  }
  if (t === 'command' && !row.command){
    err.textContent = 'Command needs a command.'; return;
  }
  // The builder is create-only now (editing happens on the Job-details page via
  // the Edit schedule / Edit action overlays), so this always creates a job.
  row.uuid = crypto.randomUUID();
  row.enabled = true;
  cronRowsState.push(row);
  cronCreating = false;       // leave the create form (closes the modal)
  cronSelectJob(row.uuid);    // select the newly created job in the tree (renders)
  cronSave();
}
function cronToggle(uuid){
  const r = cronRowsState.find(x => x.uuid === uuid);
  if (!r) return;
  r.enabled = !r.enabled;
  cronTouch(r);
  cronRender();
  cronRenderTree();
  cronSave();
}
function cronEdit(uuid){
  // Open the Job details page for this job. Editing happens there via the
  // read-only summaries + the "Edit schedule" / "Edit action" overlays — NOT
  // the New-job builder (which is create-only). See cronRenderJobDetail.
  const r = cronRowsState.find(x => x.uuid === uuid);
  if (!r) return;
  cronEditUuid = uuid;
  cronCreating = false;
  cronCloseEditModals();
  cronRenderTree();
  cronRender();
}
// ---- Job-details edit overlays (Edit schedule / Edit action) ----
function cronOpenEditModal(id){
  document.getElementById('cron-edit-backdrop').hidden = false;
  document.getElementById(id).hidden = false;
}
function cronCloseEditModals(){
  document.getElementById('cron-edit-backdrop').hidden = true;
  ['cron-sched-modal', 'cron-action-modal'].forEach(id =>
    document.getElementById(id).hidden = true);
}
function cronEditSchedule(){
  const r = cronRowsState.find(x => x.uuid === cronEditUuid);
  if (!r) return;
  const parts = (r.cron || '* * * * *').split(' ');
  document.getElementById('es-min').value = parts[0];
  document.getElementById('es-hour').value = parts[1];
  document.getElementById('es-dom').value = parts[2];
  document.getElementById('es-mon').value = parts[3];
  document.getElementById('es-dow').value = parts[4];
  document.getElementById('es-tz').value = r.timezone || 'localtime';
  // Remember the starting point so Save stays disabled until something changes.
  cronSchedOrigCron = cronCurrentFrom('es');
  cronSchedOrigTz = document.getElementById('es-tz').value;
  cronRefreshEditSchedule();
  cronUpdateSchedSaveState();
  cronOpenEditModal('cron-sched-modal');
}
function cronSaveSchedule(){
  const r = cronRowsState.find(x => x.uuid === cronEditUuid);
  if (r){
    r.cron = cronCurrentFrom('es');
    r.timezone = document.getElementById('es-tz').value;
    cronTouch(r);
  }
  cronCloseEditModals();
  cronRenderTree();
  cronRender();
  cronSave();
}
function cronEditAction(){
  const r = cronRowsState.find(x => x.uuid === cronEditUuid);
  if (!r) return;
  document.querySelector('input[name="ea-atype"][value="' + r.type + '"]').checked = true;
  cronToggleEditActionType();
  cronPopulateTargetSelect('ea-target', r.target || '');
  document.getElementById('ea-message').value = r.message || '';
  document.getElementById('ea-command').value = r.command || '';
  cronSetRetriesSelect('ea-retries', r.maxRetries || 0);
  document.getElementById('ea-err').textContent = '';
  // Starting point so Save stays disabled until something changes.
  cronActionOrig = {type: r.type, target: r.target || '', message: r.message || '',
                    command: r.command || '', retries: r.maxRetries || 0};
  cronUpdateActionSaveState();
  cronOpenEditModal('cron-action-modal');
}
function cronSaveAction(){
  const r = cronRowsState.find(x => x.uuid === cronEditUuid);
  if (!r){ cronCloseEditModals(); return; }
  const t = cronEditActiveType();
  const target = document.getElementById('ea-target').value.trim();
  const message = document.getElementById('ea-message').value.trim();
  const command = document.getElementById('ea-command').value.trim();
  const err = document.getElementById('ea-err');
  if (t === 'message' && !message){
    err.textContent = 'Message needs a message (target defaults to the cron room).'; return;
  }
  if (t === 'command' && !command){
    err.textContent = 'Command needs a command.'; return;
  }
  r.type = t; r.target = target; r.message = message; r.command = command;
  r.maxRetries = parseInt(document.getElementById('ea-retries').value, 10) || 0;
  cronTouch(r);
  cronCloseEditModals();
  cronRenderTree();
  cronRender();
  cronSave();
}
// Mirrors db_cron.cron_job_is_draft: an empty action = a draft the scheduler
// skips (backups have no required field, so they are never drafts).
function cronJobIsDraft(j){
  if (j.type === 'command') return !(j.command || '').trim();
  if (j.type === 'message') return !(j.message || '').trim();
  return false;
}
// Read-only summary for the Job details page (filled when a job is selected).
function cronRenderJobDetail(){
  const el = document.getElementById('cron-job-detail');
  const r = cronEditUuid ? cronRowsState.find(x => x.uuid === cronEditUuid) : null;
  if (!r){ el.hidden = true; return; }
  el.hidden = false;
  document.getElementById('cjd-cron').textContent = r.cron;
  // Same combined form as the job lists: "— <explanation> · <time zone>".
  const d = cronDescribe(r.cron);
  document.getElementById('cjd-cron-desc').textContent =
    '— ' + (d ? d + ' · ' : '') + cronTzLabel(r.timezone);
  document.getElementById('cjd-action').textContent = cronActionText(r) +
    (r.maxRetries > 0 ? ' — retries up to ' + r.maxRetries + '× on failure' : '') +
    (cronJobIsDraft(r) ? ' — draft: the scheduler skips this job until its action is filled in' : '');
  cronFillDescValue(document.getElementById('cjd-desc-value'), r.description);
  document.getElementById('cjd-run-status').textContent = '';  // clear stale fire status
  cronLoadHealth(r.uuid);
}
// Health panel: run counts/last outcomes/next runs + a recent-runs mini table,
// fetched per selection (a stale response for a previously-selected job is
// dropped via the uuid guard).
async function cronLoadHealth(uuid){
  const el = document.getElementById('cjd-health');
  el.textContent = 'loading…';
  el.classList.add('muted');
  let h = null;
  try {
    const r = await fetch('/cron/api/jobs/' + encodeURIComponent(uuid) + '/health');
    h = await r.json();
  } catch (e) { /* fall through to the unavailable message */ }
  if (cronEditUuid !== uuid) return;  // selection moved on; drop this response
  if (!h || !h.ok){ el.textContent = '(health unavailable)'; return; }
  el.classList.remove('muted');
  const badge = s => '<span class="crh-' + s + '">' + s + '</span>';
  const lines = [];
  const total = h.ok_count + h.error_count + h.pending_count;
  lines.push(total === 0 ? '<span class="muted">never fired</span>'
    : h.ok_count + ' ok · ' + h.error_count + ' error · ' + h.pending_count + ' pending');
  if (total > 0){
    lines.push('<span class="muted">Last success:</span> ' + cronFmtDate(h.last_ok_at) +
               ' &nbsp; <span class="muted">Last error:</span> ' + cronFmtDate(h.last_error_at));
  }
  lines.push('<span class="muted">Next:</span> ' +
    (h.next_runs.length ? h.next_runs.map(cronFmtDate).join(' · ') : '—'));
  let html = lines.join('<br>');
  if (h.runs.length){
    html += '<table><tr><th>Fired</th><th>Trigger</th><th>Status</th><th>Error</th></tr>' +
      h.runs.map(r =>
        '<tr><td>' + cronFmtDate(r.fired_at) + '</td><td>' + escapeHtml(r.trigger) +
        (r.debug ? ' <span class="muted">· debug</span>' : '') +
        '</td><td>' + badge(r.status) + '</td><td>' + escapeHtml(r.error || '') + '</td></tr>'
      ).join('') + '</table>';
  }
  el.innerHTML = html;
}
// "Run now": fire the selected job immediately via the API, then watch the
// run's outcome (commands finish asynchronously in the workspace-shell agent)
// and surface the verdict inline. Events + output also land in the "cron"
// chatroom. debug=true is a dry-run: the fire reports what it WOULD do
// (message + destination, backup destination, validated command argv)
// without doing it.
function cronRunNow(debug){
  if (!cronEditUuid) return;
  const uuid = cronEditUuid;
  const status = document.getElementById('cjd-run-status');
  status.textContent = debug ? 'dry-running…' : 'firing…';
  fetch('/cron/api/jobs/' + encodeURIComponent(uuid) + '/run' + (debug ? '?debug=1' : ''),
        {method: 'POST'})
    .then(r => r.json())
    .then(d => {
      if (!d.ok){ status.textContent = 'error: ' + (d.error || 'failed'); return; }
      cronWatchRunOutcome(uuid, 0);
    })
    .catch(() => { status.textContent = 'network error'; });
}
// Poll the job's health until its newest run resolves (bounded: ~15s, then
// point at the chatroom), then show the verdict and refresh the Health panel.
// Selection changes abort the watch (the status element belongs to that job).
async function cronWatchRunOutcome(uuid, attempt){
  if (cronEditUuid !== uuid) return;
  const status = document.getElementById('cjd-run-status');
  let h = null;
  try {
    const r = await fetch('/cron/api/jobs/' + encodeURIComponent(uuid) + '/health');
    h = await r.json();
  } catch (e) { /* unresolved this round; retry below */ }
  if (cronEditUuid !== uuid) return;
  const lr = (h && h.ok && h.runs && h.runs[0]) ? h.runs[0] : null;
  if (lr && lr.status !== 'pending'){
    status.textContent = lr.status === 'ok'
      ? 'completed ✔'
      : ('failed ✖' + (lr.error ? ' — ' + lr.error : ''));
    cronLoadHealth(uuid);  // counts + recent-runs table just changed
    return;
  }
  if (attempt >= 15){
    status.textContent = 'still running — see the “cron” chatroom';
    cronLoadHealth(uuid);
    return;
  }
  status.textContent = 'running…';
  setTimeout(() => cronWatchRunOutcome(uuid, attempt + 1), 1000);
}
// Render a description value read-only ("(none)" + muted when empty); editing is
// via the Edit description overlay.
function cronFillDescValue(el, text){
  if (text){ el.textContent = text; el.classList.remove('muted'); }
  else { el.textContent = '(none)'; el.classList.add('muted'); }
}
// The node whose description the Edit-description overlay edits — the selected
// job, else the selected folder.
function cronCurrentDescNode(){
  if (cronEditUuid) return cronRowsState.find(x => x.uuid === cronEditUuid) || null;
  if (cronSelectedFolder !== null) return cronFolderById(cronSelectedFolder);
  return null;
}
function cronEditDescription(){
  const node = cronCurrentDescNode();
  if (!node) return;
  document.getElementById('cron-desc-input').value = node.description || '';
  document.getElementById('cron-desc-backdrop').hidden = false;
  document.getElementById('cron-desc-modal').hidden = false;
  document.getElementById('cron-desc-input').focus();
}
function cronCloseDescModal(){
  document.getElementById('cron-desc-backdrop').hidden = true;
  document.getElementById('cron-desc-modal').hidden = true;
}
function cronSaveDescription(){
  const node = cronCurrentDescNode();
  if (node){
    node.description = document.getElementById('cron-desc-input').value;
    cronTouch(node);
  }
  cronCloseDescModal();
  cronRenderTree();
  cronRender();
  cronSave();
}
function cronCancelEdit(){
  cronEditUuid = null;
  cronCreating = false;
  document.getElementById('form-err').textContent = '';
  cronRenderTree();   // leaving the form returns to the list view
  cronRender();       // resets Add/Cancel buttons + section visibility
}
function cronDelete(uuid){
  const before = cronRowsState.length;
  cronRowsState = cronRowsState.filter(r => r.uuid !== uuid);
  cronPendingDeletes += before - cronRowsState.length;  // declare to the save's tripwire
  if (cronEditUuid === uuid) cronCancelEdit();
  cronRender();
  cronRenderTree();
  cronSave();
}

// ---- folders + left tree ----
// Inlined Lucide icons (https://lucide.dev): folder (collapsed) / folder-open
// (expanded). Inlined rather than fetched so the page stays self-contained.
const CRON_ICON_FOLDER = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>';
const CRON_ICON_FOLDER_OPEN = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 14 1.45-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.55 6a2 2 0 0 1-1.94 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.93a2 2 0 0 1 1.66.9l.82 1.2a2 2 0 0 0 1.66.9H18a2 2 0 0 1 2 2v2"/></svg>';
function cronFolderById(id){ return cronFolders.find(f => f.id === id) || null; }
// Names of the ancestor folders, root-first, ending at the folder `folderId`
// (inclusive). Guards against a malformed parent cycle.
function cronAncestorFolderNames(folderId){
  const names = [];
  const guard = new Set();
  let cur = folderId || null;
  while (cur && !guard.has(cur)){
    guard.add(cur);
    const f = cronFolderById(cur);
    if (!f) break;
    names.unshift(f.name);
    cur = f.parentId || null;
  }
  return names;
}
// Breadcrumb path to a node — its ancestor folder names plus its own name,
// e.g. "My Life -> Computer -> Backup". Shown as the name-column tooltip.
function cronNodePath(kind, node){
  const parentFolderId = kind === 'folder' ? (node.parentId || null) : (node.folderId || null);
  const names = cronAncestorFolderNames(parentFolderId);
  names.push(node.name);
  return names.join(' -> ');
}
function cronChildFolders(parentId){ return cronFolders.filter(f => (f.parentId || null) === parentId); }
function cronJobsInFolder(id){ return cronRowsState.filter(j => (j.folderId || null) === id); }
function cronIsExpanded(id){ return cronExpanded[id] !== false; }
// Effective-enabled: a folder is live only if it AND every ancestor is enabled;
// a job is live only if its own flag is on AND its folder chain is enabled.
function cronFolderEnabled(id){
  let cur = id ? cronFolderById(id) : null;
  while (cur){
    if (cur.enabled === false) return false;
    cur = cur.parentId ? cronFolderById(cur.parentId) : null;
  }
  return true;
}
function cronJobLive(j){
  return j.enabled !== false && cronFolderEnabled(j.folderId || null);
}
function cronToggleFolderEnabled(id){
  const f = cronFolderById(id);
  if (!f) return;
  f.enabled = (f.enabled === false);  // flip; cascades to the subtree via cronFolderEnabled
  cronTouch(f);
  cronRenderTree();
  cronRender();  // also refreshes the folder detail
  cronSave();
}
// Right-pane header for a selected folder: name + activate/deactivate toggle.
function cronRenderDetail(){
  // The "Active" toggle for the selected node — folder or job — so the naming
  // is consistent across both. Folders add "applies to all child nodes".
  const el = document.getElementById('cron-folder-detail');
  el.innerHTML = '';
  let node = null, isFolder = false;
  if (cronEditUuid){
    node = cronRowsState.find(j => j.uuid === cronEditUuid);
  } else if (cronSelectedFolder !== null){
    node = cronFolderById(cronSelectedFolder); isFolder = true;
  }
  if (!node){ el.hidden = true; return; }
  el.hidden = false;
  const lbl = document.createElement('label');
  lbl.className = 'folder-active';
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = node.enabled !== false;
  cb.addEventListener('change', () =>
    isFolder ? cronToggleFolderEnabled(node.id) : cronToggle(node.uuid));
  lbl.appendChild(cb);
  lbl.appendChild(document.createTextNode(' Active'));
  el.appendChild(lbl);
  // Adjacent note explaining what active/inactive means for this node.
  const on = node.enabled !== false;
  const note = document.createElement('span');
  note.className = 'folder-active-note';
  if (isFolder){
    note.textContent = on ? 'Its child jobs will be executed on their schedule.'
                          : 'Its child jobs will not be executed.';
  } else {
    note.textContent = on ? 'The command will be executed on its schedule.'
                          : 'The command will not be executed.';
  }
  el.appendChild(note);
  const parentFolderId = isFolder ? (node.parentId || null) : (node.folderId || null);
  if (parentFolderId && !cronFolderEnabled(parentFolderId)){
    const hint = document.createElement('span');
    hint.className = 'hint';
    hint.textContent = '(a parent folder is deactivated)';
    el.appendChild(hint);
  }
  // Folder and Job details both show when the node was created and last
  // modified. These come from the backend (ISO timestamps), so they only
  // appear once the node has been persisted and reloaded.
  if (node.created_at || node.updated_at){
    const ts = document.createElement('span');
    ts.className = 'cron-timestamps';
    ts.textContent = 'Created ' + cronFmtDate(node.created_at) +
                     ' · Modified ' + cronFmtDate(node.updated_at);
    ts.title = 'Created ' + cronFmtDateExact(node.created_at) +
               '\nModified ' + cronFmtDateExact(node.updated_at);
    el.appendChild(ts);
  }
}
// Format an ISO timestamp from the backend into a short local date/time; an
// absent value renders as an em dash. cronFmtDateExact gives the full string
// for the hover tooltip.
function cronFmtDate(iso){
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  // Unambiguous European-friendly format: 2026-jun-06 23:49:21 (local time).
  const MON = ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'];
  const p2 = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + MON[d.getMonth()] + '-' + p2(d.getDate()) +
         ' ' + p2(d.getHours()) + ':' + p2(d.getMinutes()) + ':' + p2(d.getSeconds());
}
function cronFmtDateExact(iso){
  if (!iso) return '(unknown)';
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toString();
}
// Notes for the selected folder — "there may be notes related to the child
// nodes". Shown read-only with an Edit button (the overlay does the editing);
// only on Folder details, not while editing a job.
function cronRenderFolderDesc(){
  const el = document.getElementById('cron-folder-desc');
  el.innerHTML = '';
  const folder = (!cronEditUuid && cronSelectedFolder !== null)
    ? cronFolderById(cronSelectedFolder) : null;
  if (!folder){ el.hidden = true; return; }
  el.hidden = false;
  const sec = document.createElement('div');
  sec.className = 'cjd-section';
  const lbl = document.createElement('div');
  lbl.className = 'cjd-label';
  lbl.textContent = 'Description';
  const val = document.createElement('div');
  val.className = 'cjd-value';
  cronFillDescValue(val, folder.description);
  const btn = document.createElement('button');
  btn.textContent = 'Edit description';
  btn.addEventListener('click', cronEditDescription);
  sec.appendChild(lbl); sec.appendChild(val); sec.appendChild(btn);
  el.appendChild(sec);
}
// Rename field at the top of the right pane for the selected node (folder or
// job). Mirrors the /modelgroups rename: a text input + Rename button + Enter.
function cronRenderRename(){
  const el = document.getElementById('cron-node-rename');
  el.innerHTML = '';
  let node = null, kind = null;
  if (cronEditUuid){ node = cronRowsState.find(j => j.uuid === cronEditUuid); kind = 'job'; }
  else if (cronSelectedFolder !== null){ node = cronFolderById(cronSelectedFolder); kind = 'folder'; }
  if (!node){ el.hidden = true; return; }
  el.hidden = false;
  const input = document.createElement('input');
  input.type = 'text';
  input.id = 'cron-rename-field';
  input.value = node.name;
  const btn = document.createElement('button');
  btn.textContent = 'Rename';
  const doRename = () => {
    const v = input.value.trim();
    if (!v) return;
    node.name = v;
    cronTouch(node);
    if (kind === 'job') document.getElementById('f-name').value = v;  // keep builder in sync
    cronRenderTree();
    cronRender();  // re-renders table, detail, and this rename field
    cronSave();
  };
  btn.addEventListener('click', doRename);
  input.addEventListener('keydown', e => { if (e.key === 'Enter'){ e.preventDefault(); doRename(); } });
  el.appendChild(input); el.appendChild(btn);
}
function cronFolderClick(id){
  // First click selects the folder and clears any job/edit selection, so the
  // tree never shows an unrelated folder + job both selected. Clicking the
  // already-selected folder toggles its expand/collapse state.
  const wasSelected = (cronSelectedFolder === id) && !cronEditUuid;
  cronCancelEdit();
  if (wasSelected){
    cronExpanded[id] = !cronIsExpanded(id);
  } else {
    cronSelectedFolder = id;
    document.getElementById('f-folder').value = id || '';
  }
  cronRenderTree();
  cronRender();
}

function cronPopulateFolderSelect(){
  const sel = document.getElementById('f-folder');
  const cur = sel.value;
  sel.innerHTML = '';
  sel.appendChild(ppOpt('', '(unfiled)'));
  (function addTree(parentId, depth){
    cronChildFolders(parentId).forEach(f => {
      sel.appendChild(ppOpt(f.id, '\u00A0\u00A0'.repeat(depth) + f.name));
      addTree(f.id, depth + 1);
    });
  })(null, 0);
  sel.value = cur;
}

function cronCurrentSelectionId(){
  // The id of the node currently being inspected (job > folder > none).
  if (cronEditUuid) return cronEditUuid;
  if (cronSelectedFolder) return cronSelectedFolder;
  return null;
}
function cronSyncUrl(){
  // Reflect the selected node in the ?id= query param so the URL is a
  // shareable deep link to the folder/job being inspected.
  const url = new URL(window.location);
  const id = cronCurrentSelectionId();
  if (id) url.searchParams.set('id', id); else url.searchParams.delete('id');
  history.replaceState(null, '', url);
}
function cronSelectFolder(id){
  cronCancelEdit();  // selecting a folder clears any job/edit selection
  cronSelectedFolder = id;
  document.getElementById('f-folder').value = id || '';
  cronRenderTree();
  cronRender();
}
function cronSelectJob(uuid){
  cronEdit(uuid);  // loads the job into the builder
  const j = cronRowsState.find(x => x.uuid === uuid);
  cronSelectedFolder = j ? (j.folderId || null) : null;
  cronRenderTree();
  cronRender();
}
function cronSelectNode(type, id){
  // Select a node the same way clicking it would (used after a drag-drop).
  if (type === 'job') cronSelectJob(id);
  else cronSelectFolder(id);
}

function cronJobNode(j){
  const n = document.createElement('div');
  n.className = 'cron-job-node' + (cronEditUuid === j.uuid ? ' sel' : '') + (cronJobLive(j) ? '' : ' off');
  n.title = j.name;
  const label = document.createElement('span');
  label.className = 'cron-job-label';
  label.textContent = j.name;
  n.appendChild(label);
  n.addEventListener('click', () => cronSelectJob(j.uuid));
  cronMakeDraggable(n, 'job', j.uuid);
  cronMakeJobDrop(n, j.uuid);
  cronMakeKebab(n, {
    onDelete: () => cronConfirmDeleteJob(j.uuid),
    onDuplicate: () => cronDuplicateJob(j.uuid),
  });
  return n;
}
// A 3-dot overflow menu on a tree item (mirrors the chat page's room-kebab).
// opts: { onDelete, onDuplicate, onAddJob? }. "New job" is only present for
// folders (opts.onAddJob set).
function cronMakeKebab(node, opts){
  opts = opts || {};
  const kebab = document.createElement('button');
  kebab.type = 'button';
  kebab.className = 'cron-kebab';
  kebab.setAttribute('aria-label', 'Item actions');
  kebab.setAttribute('aria-haspopup', 'menu');
  const menu = document.createElement('div');
  menu.className = 'cron-menu';
  menu.setAttribute('role', 'menu');
  menu.hidden = true;
  const items = [];
  if (opts.onAddJob) items.push(['New job', '', opts.onAddJob]);
  items.push(['Duplicate', '', opts.onDuplicate || null]);
  items.push(['Delete', 'danger', opts.onDelete]);
  items.forEach(spec => {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'item' + (spec[1] ? ' ' + spec[1] : '');
    item.setAttribute('role', 'menuitem');
    item.textContent = spec[0];
    item.addEventListener('click', e => {
      e.stopPropagation();
      menu.hidden = true;
      if (spec[2]) spec[2]();
    });
    menu.appendChild(item);
  });
  kebab.addEventListener('click', e => {
    e.stopPropagation();  // don't select/toggle the underlying node
    const willOpen = menu.hidden;
    document.querySelectorAll('.cron-menu').forEach(m => { m.hidden = true; });
    if (willOpen){
      const r = kebab.getBoundingClientRect();
      menu.style.left = r.left + 'px';
      menu.style.top = (r.bottom + 4) + 'px';
      menu.hidden = false;
    }
  });
  node.appendChild(kebab);
  node.appendChild(menu);
}
function cronFolderLi(f){
  const li = document.createElement('li');
  const kids = cronChildFolders(f.id);
  const jobs = cronJobsInFolder(f.id);
  const hasKids = (kids.length + jobs.length) > 0;
  const expanded = cronIsExpanded(f.id);
  const node = document.createElement('div');
  node.className = 'cron-node'
    + ((cronSelectedFolder === f.id && !cronEditUuid) ? ' sel' : '')
    + (cronFolderEnabled(f.id) ? '' : ' off');
  const icon = document.createElement('span');
  icon.className = 'cron-ficon';
  icon.innerHTML = (expanded && hasKids) ? CRON_ICON_FOLDER_OPEN : CRON_ICON_FOLDER;
  const label = document.createElement('span');
  label.className = 'cron-folder-label';
  label.textContent = f.name;
  node.appendChild(icon); node.appendChild(label);
  node.addEventListener('click', () => cronFolderClick(f.id));
  cronMakeDraggable(node, 'folder', f.id);
  cronMakeFolderDrop(node, f.id);
  cronMakeKebab(node, {
    onDelete: () => cronConfirmDeleteFolder(f.id),
    onDuplicate: () => cronDuplicateFolder(f.id),
    onAddJob: () => cronNewJob(f.id),
  });
  li.appendChild(node);
  if (expanded && hasKids){
    const ul = document.createElement('ul');
    kids.forEach(c => ul.appendChild(cronFolderLi(c)));
    jobs.forEach(j => { const jli = document.createElement('li'); jli.appendChild(cronJobNode(j)); ul.appendChild(jli); });
    li.appendChild(ul);
  }
  return li;
}
function cronRenderTree(){
  // Highlight the static "All jobs" node (it lives above the action buttons).
  document.getElementById('cron-all-jobs').className =
    'cron-node' + ((cronSelectedFolder === null && !cronEditUuid) ? ' sel' : '');
  const root = document.getElementById('cron-tree-root');
  root.innerHTML = '';
  cronChildFolders(null).forEach(f => root.appendChild(cronFolderLi(f)));
  // Root-level (unfiled) jobs — e.g. nodes dropped on "Move to top level".
  cronJobsInFolder(null).forEach(j => {
    const jli = document.createElement('li');
    jli.appendChild(cronJobNode(j));
    root.appendChild(jli);
  });
}

function cronNewJob(preferFolderId){
  // Dedicated create form: shows ONLY the builder (no rename field, Active
  // toggle, or job list). The folder picker defaults to the selected folder,
  // or to preferFolderId when opened from a folder's "Add job" menu (the user
  // can still pick a different parent, or "(unfiled)" for none).
  cronEditUuid = null;
  cronCreating = true;
  cronClearInputs();
  document.getElementById('f-name').value = 'Unnamed';  // prefill a default name
  if (preferFolderId !== undefined){
    document.getElementById('f-folder').value = preferFolderId || '';
  }
  document.getElementById('form-err').textContent = '';
  cronRenderTree();
  cronRender();
  const nameEl = document.getElementById('f-name');
  nameEl.focus();
  nameEl.select();  // highlight the default so it's easy to overwrite
}
// New folder via a custom overlay (a native prompt can be permanently
// suppressed by the browser). asSub=true nests under the selected folder.
let cronAddFolderAsSub = false;
function cronAddFolder(asSub){
  cronAddFolderAsSub = !!asSub;
  document.getElementById('cron-folder-title').textContent = asSub ? 'New subfolder' : 'New folder';
  const input = document.getElementById('cron-folder-input');
  input.value = '';
  document.getElementById('cron-folder-create').disabled = true;
  document.getElementById('cron-folder-backdrop').hidden = false;
  document.getElementById('cron-folder-modal').hidden = false;
  input.focus();
}
function cronCloseFolderModal(){
  document.getElementById('cron-folder-backdrop').hidden = true;
  document.getElementById('cron-folder-modal').hidden = true;
}
function cronAddFolderConfirm(){
  const name = document.getElementById('cron-folder-input').value.trim();
  if (!name) return;
  const parentId = cronAddFolderAsSub ? cronSelectedFolder : null;
  const id = crypto.randomUUID();
  cronFolders.push({id: id, name: name, description: '', parentId: parentId, enabled: true});
  if (parentId) cronExpanded[parentId] = true;
  cronCloseFolderModal();
  cronPopulateFolderSelect();
  cronSelectFolder(id);   // select the newly created folder
  cronSave();
}
document.getElementById('cron-folder-input').addEventListener('input', () => {
  document.getElementById('cron-folder-create').disabled =
    document.getElementById('cron-folder-input').value.trim() === '';
});
document.getElementById('cron-folder-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('cron-folder-create').disabled){
    e.preventDefault(); cronAddFolderConfirm();
  }
});
function cronDeleteFolderById(id){
  const f = cronFolderById(id);
  if (!f) return;
  // Cascade: delete this folder, every descendant folder, and every job inside
  // any of them. (Confirmation is handled by cronConfirmDeleteFolder.)
  const folderIds = new Set([f.id]);
  let grew = true;
  while (grew){
    grew = false;
    cronFolders.forEach(c => {
      if (folderIds.has(c.parentId) && !folderIds.has(c.id)){ folderIds.add(c.id); grew = true; }
    });
  }
  const beforeF = cronFolders.length, beforeJ = cronRowsState.length;
  cronFolders = cronFolders.filter(x => !folderIds.has(x.id));
  cronRowsState = cronRowsState.filter(j => !folderIds.has(j.folderId));
  // Declare the cascade's deletions to the save's tripwire.
  cronPendingDeletes += (beforeF - cronFolders.length) + (beforeJ - cronRowsState.length);
  if (cronEditUuid && !cronRowsState.find(j => j.uuid === cronEditUuid)) cronCancelEdit();
  if (folderIds.has(cronSelectedFolder)) cronSelectedFolder = f.parentId || null;
  cronPopulateFolderSelect();
  document.getElementById('f-folder').value = cronSelectedFolder || '';
  cronRenderTree();
  cronRender();
  cronSave();
}
// Confirmation before deleting from the left-panel kebab, via a custom overlay
// (a native confirm/prompt can be permanently suppressed by the browser). A
// non-empty folder guards with a typed-name confirmation (it cascades); an empty
// folder or a job just needs the Delete button.
let cronDeleteOnConfirm = null;
let cronDeleteRequireName = null;
function cronOpenDeleteModal(opts){
  cronDeleteOnConfirm = opts.onConfirm;
  cronDeleteRequireName = opts.requireName || null;
  document.getElementById('cron-delete-msg').textContent = opts.message;
  const nameRow = document.getElementById('cron-delete-name-row');
  const input = document.getElementById('cron-delete-input');
  const btn = document.getElementById('cron-delete-confirm');
  if (cronDeleteRequireName){
    nameRow.hidden = false; input.value = ''; btn.disabled = true;
  } else {
    nameRow.hidden = true; btn.disabled = false;
  }
  document.getElementById('cron-delete-backdrop').hidden = false;
  document.getElementById('cron-delete-modal').hidden = false;
  if (cronDeleteRequireName) input.focus();
}
function cronCloseDeleteModal(){
  document.getElementById('cron-delete-backdrop').hidden = true;
  document.getElementById('cron-delete-modal').hidden = true;
  cronDeleteOnConfirm = null;
  cronDeleteRequireName = null;
}
function cronDeleteUpdateState(){
  const input = document.getElementById('cron-delete-input');
  document.getElementById('cron-delete-confirm').disabled =
    cronDeleteRequireName ? (input.value.trim() !== cronDeleteRequireName) : false;
}
document.getElementById('cron-delete-input').addEventListener('input', cronDeleteUpdateState);
document.getElementById('cron-delete-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !document.getElementById('cron-delete-confirm').disabled){
    e.preventDefault();
    document.getElementById('cron-delete-confirm').click();
  }
});
document.getElementById('cron-delete-confirm').addEventListener('click', () => {
  const fn = cronDeleteOnConfirm;
  cronCloseDeleteModal();
  if (fn) fn();
});
function cronConfirmDeleteJob(uuid){
  const j = cronRowsState.find(x => x.uuid === uuid);
  if (!j) return;
  cronOpenDeleteModal({
    message: 'Delete cronjob "' + j.name + '"?',
    onConfirm: () => cronDelete(uuid),
  });
}
function cronConfirmDeleteFolder(id){
  const f = cronFolderById(id);
  if (!f) return;
  const count = cronFlattenTree(f.id).length;  // descendant nodes deleted with it
  if (count === 0){
    cronOpenDeleteModal({
      message: 'Delete empty folder "' + f.name + '"?',
      onConfirm: () => cronDeleteFolderById(f.id),
    });
    return;
  }
  const noun = count === 1 ? 'child node' : 'child nodes';
  cronOpenDeleteModal({
    message: 'Deleting folder "' + f.name + '" will also delete ' + count + ' ' + noun + '.',
    requireName: f.name,
    onConfirm: () => cronDeleteFolderById(f.id),
  });
}
function cronDuplicateJob(uuid){
  const idx = cronRowsState.findIndex(j => j.uuid === uuid);
  if (idx < 0) return;
  const copy = Object.assign({}, cronRowsState[idx], {
    uuid: crypto.randomUUID(),
    name: cronRowsState[idx].name + ' (copy)',
    enabled: false,  // a fresh copy starts inactive so it doesn't run immediately
  });
  cronRowsState.splice(idx + 1, 0, copy);  // right after the original
  cronSelectJob(copy.uuid);                // select the new copy
  cronSave();
}
function cronDuplicateFolder(uuid){
  const src = cronFolderById(uuid);
  if (!src) return;
  // Deep-clone the whole subtree (child folders + jobs) with fresh uuids.
  const cloneSubtree = (f, newParentId) => {
    const nid = crypto.randomUUID();
    cronFolders.push(Object.assign({}, f, {id: nid, parentId: newParentId}));
    cronChildFolders(f.id).forEach(c => cloneSubtree(c, nid));
    cronJobsInFolder(f.id).forEach(j =>
      cronRowsState.push(Object.assign({}, j, {uuid: crypto.randomUUID(), folderId: nid})));
    return nid;
  };
  const topId = cloneSubtree(src, src.parentId || null);
  const top = cronFolderById(topId);
  top.name = src.name + ' (copy)';
  top.enabled = false;  // the copied subtree starts inactive (descendants keep their own flags)
  // Place the copy right after the original among its siblings.
  cronFolders = cronFolders.filter(f => f.id !== topId);
  const si = cronFolders.findIndex(f => f.id === src.id);
  cronFolders.splice(si + 1, 0, top);
  cronPopulateFolderSelect();
  cronSelectFolder(topId);                 // select the new copy
  cronSave();
}

// ---- drag & drop (one node at a time) ----
function cronFolderInSubtree(candidateId, rootId){
  // true if candidateId is rootId or nested anywhere under it (cycle guard)
  let cur = cronFolderById(candidateId);
  while (cur){
    if (cur.id === rootId) return true;
    cur = cur.parentId ? cronFolderById(cur.parentId) : null;
  }
  return false;
}
function cronMoveFolder(folderId, targetParentId, atStart){
  targetParentId = targetParentId || null;
  if (folderId === targetParentId) return;
  if (targetParentId && cronFolderInSubtree(targetParentId, folderId)) return;  // no cycles
  const f = cronFolderById(folderId);
  if (!f) return;
  f.parentId = targetParentId;
  cronFolders = cronFolders.filter(x => x.id !== folderId);
  if (atStart){
    // before the first existing sibling under the target parent
    const i = cronFolders.findIndex(x => (x.parentId || null) === targetParentId);
    if (i < 0) cronFolders.push(f); else cronFolders.splice(i, 0, f);
  } else {
    // after the last existing sibling under the target parent, else at the end
    let at = cronFolders.length;
    for (let i = cronFolders.length - 1; i >= 0; i--){
      if ((cronFolders[i].parentId || null) === targetParentId){ at = i + 1; break; }
    }
    cronFolders.splice(at, 0, f);
  }
  cronSave();
}
function cronMoveFolderBeside(folderId, targetFolderId, after){
  // Reorder: place folderId as a sibling immediately before/after targetFolderId
  // (same parent level), preserving array order for siblings.
  if (folderId === targetFolderId) return;
  const target = cronFolderById(targetFolderId);
  if (!target) return;
  const newParent = target.parentId || null;
  if (newParent && cronFolderInSubtree(newParent, folderId)) return;  // no cycles
  const f = cronFolderById(folderId);
  if (!f) return;
  f.parentId = newParent;
  cronFolders = cronFolders.filter(x => x.id !== folderId);
  const ti = cronFolders.findIndex(x => x.id === targetFolderId);
  if (ti < 0) cronFolders.push(f);
  else cronFolders.splice(after ? ti + 1 : ti, 0, f);
  cronSave();
}
function cronMoveJob(jobUuid, targetFolderId, beforeJobUuid){
  targetFolderId = targetFolderId || null;
  const idx = cronRowsState.findIndex(j => j.uuid === jobUuid);
  if (idx < 0) return;
  const job = cronRowsState.splice(idx, 1)[0];
  job.folderId = targetFolderId;
  let insertAt = beforeJobUuid ? cronRowsState.findIndex(j => j.uuid === beforeJobUuid) : -1;
  if (insertAt < 0){
    // append after the last job already in the target folder, else at the very end
    insertAt = cronRowsState.length;
    for (let i = cronRowsState.length - 1; i >= 0; i--){
      if ((cronRowsState[i].folderId || null) === targetFolderId){ insertAt = i + 1; break; }
    }
  }
  cronRowsState.splice(insertAt, 0, job);
  cronSave();
}
function cronMakeDraggable(el, type, id){
  el.draggable = true;
  el.addEventListener('dragstart', e => {
    cronDrag = {type: type, id: id};
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', id);  // required to start a drag in Firefox
    el.classList.add('cron-dragging');
    document.getElementById('cron-tree').classList.add('cron-dragging-on');  // reveal root drop zone
    e.stopPropagation();
  });
  el.addEventListener('dragend', () => {
    cronDrag = null;
    document.getElementById('cron-tree').classList.remove('cron-dragging-on');
    cronRenderTree();
  });
}
function cronDropInto(folderId, atStart){
  if (!cronDrag) return;
  const dragged = cronDrag;
  if (dragged.type === 'job'){
    let beforeUuid = null;
    if (atStart){
      const first = cronRowsState.find(j =>
        (j.folderId || null) === (folderId || null) && j.uuid !== dragged.id);
      beforeUuid = first ? first.uuid : null;
    }
    cronMoveJob(dragged.id, folderId, beforeUuid);
  } else {
    cronMoveFolder(dragged.id, folderId, atStart);
  }
  if (folderId) cronExpanded[folderId] = true;
  cronDrag = null;
  cronSelectNode(dragged.type, dragged.id);  // select the moved node (also renders)
}
function cronMakeFolderDrop(node, folderId){
  // Three zones on a folder node: top third = reorder before, bottom third =
  // reorder after (sibling), middle = nest into. Jobs always go "into".
  const zoneOf = e => {
    if (cronDrag && cronDrag.type === 'job') return 'into';
    const r = node.getBoundingClientRect();
    const y = e.clientY - r.top;
    if (y < r.height / 3) return 'before';
    if (y > r.height * 2 / 3) return 'after';
    return 'into';
  };
  const okFor = z => {
    if (!cronDrag) return false;
    if (cronDrag.type === 'job') return z === 'into';
    if (folderId === cronDrag.id) return false;            // not onto itself
    if (z === 'into') return !cronFolderInSubtree(folderId, cronDrag.id);
    const t = cronFolderById(folderId);                    // before/after: new parent = target's parent
    const np = t ? (t.parentId || null) : null;
    return !(np && cronFolderInSubtree(np, cronDrag.id));
  };
  const clear = () => node.classList.remove('cron-drop-before', 'cron-drop-after', 'cron-drop-target');
  node.addEventListener('dragover', e => {
    if (!cronDrag) return;
    e.stopPropagation();  // never let the container's root-drop claim a drop over a node
    const z = zoneOf(e);
    if (!okFor(z)){ clear(); return; }  // e.g. dropping a folder on itself → no-op
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    node.classList.toggle('cron-drop-before', z === 'before');
    node.classList.toggle('cron-drop-after', z === 'after');
    node.classList.toggle('cron-drop-target', z === 'into');
  });
  node.addEventListener('dragleave', clear);
  node.addEventListener('drop', e => {
    if (!cronDrag) return;
    e.stopPropagation();  // don't bubble to the container's root-drop
    const z = zoneOf(e);
    if (!okFor(z)){ clear(); return; }
    e.preventDefault();
    clear();
    if (z === 'into'){
      cronDropInto(folderId, false);  // job into folder, or nest folder at end of children
    } else {
      const draggedId = cronDrag.id;
      cronMoveFolderBeside(cronDrag.id, folderId, z === 'after');
      cronDrag = null;
      cronSelectNode('folder', draggedId);  // select the reordered folder
    }
  });
}
function cronMakeJobDrop(node, jobUuid){
  const isAfter = e => {
    const r = node.getBoundingClientRect();
    return (e.clientY - r.top) > r.height / 2;
  };
  node.addEventListener('dragover', e => {
    if (!cronDrag) return;
    e.preventDefault(); e.stopPropagation();
    e.dataTransfer.dropEffect = 'move';
    const after = isAfter(e);
    node.classList.toggle('cron-drop-after', after);
    node.classList.toggle('cron-drop-before', !after);
  });
  node.addEventListener('dragleave', () => node.classList.remove('cron-drop-before', 'cron-drop-after'));
  node.addEventListener('drop', e => {
    if (!cronDrag) return;
    e.preventDefault(); e.stopPropagation();
    const after = isAfter(e);
    node.classList.remove('cron-drop-before', 'cron-drop-after');
    cronDropOnJob(jobUuid, after);
  });
}
function cronDropOnJob(targetUuid, after){
  if (!cronDrag) return;
  if (cronDrag.type === 'job' && cronDrag.id === targetUuid) return;  // dropped on itself → no-op
  const dragged = cronDrag;
  const target = cronRowsState.find(j => j.uuid === targetUuid);
  const targetFolder = target ? (target.folderId || null) : null;
  if (dragged.type === 'job'){
    let beforeUuid = targetUuid;
    if (after){
      const ti = cronRowsState.findIndex(j => j.uuid === targetUuid);
      beforeUuid = (ti + 1 < cronRowsState.length) ? cronRowsState[ti + 1].uuid : null;
    }
    if (beforeUuid === dragged.id) beforeUuid = null;
    cronMoveJob(dragged.id, targetFolder, beforeUuid);
  } else {
    cronMoveFolder(dragged.id, targetFolder);
  }
  cronDrag = null;
  cronSelectNode(dragged.type, dragged.id);
}
function cronInitTreeDnD(){
  // Wire the persistent tree container ONCE (its children are rebuilt each
  // render, but the <ul> itself survives — avoid stacking listeners). Dropping
  // on empty space moves the node to the root level.
  const root = document.getElementById('cron-tree-root');
  root.addEventListener('dragover', e => {
    if (cronDrag){ e.preventDefault(); e.dataTransfer.dropEffect = 'move'; }
  });
  root.addEventListener('drop', e => {
    if (cronDrag){ e.preventDefault(); cronDropInto(null, false); }  // empty space → end of root
  });
  cronWireRootDrop(document.getElementById('cron-root-drop'), false);  // bottom zone → end
  // The static "All jobs" node selects all jobs; it is NOT a drop target
  // (use the "Move to top level" zone to move a node to the root).
  document.getElementById('cron-all-jobs').addEventListener('click', () => cronSelectFolder(null));
  // Dismiss any open kebab menu on an outside click or Escape.
  document.addEventListener('click', () => {
    document.querySelectorAll('.cron-menu').forEach(m => { m.hidden = true; });
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') document.querySelectorAll('.cron-menu').forEach(m => { m.hidden = true; });
  });
}
function cronWireRootDrop(el, atStart){
  el.addEventListener('dragover', e => {
    if (cronDrag){ e.preventDefault(); e.stopPropagation(); e.dataTransfer.dropEffect = 'move'; el.classList.add('over'); }
  });
  el.addEventListener('dragleave', () => el.classList.remove('over'));
  el.addEventListener('drop', e => {
    if (cronDrag){ e.preventDefault(); e.stopPropagation(); el.classList.remove('over'); cronDropInto(null, atStart); }
  });
}

// ---- persistence: load from / save to the backend ----
async function cronLoadTree(){
  try {
    const r = await fetch('/cron/api/tree');
    const data = await r.json();
    cronFolders = (data && data.folders) || [];
    cronRowsState = (data && data.jobs) || [];
    cronChatrooms = (data && data.chatrooms) || [];
    cronTreeVersion = (data && data.version) || null;
    cronPaused = !!(data && data.paused);
  } catch (e) {
    // Hydration failed: keep version null so a PUT of this empty state is
    // refused by the server (400) instead of wiping the real tree.
    cronFolders = []; cronRowsState = []; cronChatrooms = [];
    cronTreeVersion = null;
  }
}
// ---- global pause (one server-side flag; per-job/folder toggles untouched) ----
let cronPaused = false;
function cronRenderPaused(){
  document.getElementById('cron-paused-banner').hidden = !cronPaused;
  document.getElementById('cron-pause-btn').textContent =
    cronPaused ? 'Resume all' : 'Pause all';
}
async function cronTogglePause(){
  try {
    const r = await fetch(cronPaused ? '/cron/api/resume' : '/cron/api/pause', {method: 'POST'});
    const j = await r.json();
    if (j && j.ok){
      cronPaused = !!j.paused;
      cronRenderPaused();
      cronRender();  // the lists' next-run cells show "paused" while paused
      return;
    }
  } catch (e) { /* fall through */ }
  cronToast('Pause toggle failed — scheduler state unchanged.');
}
let cronToastTimer = null;
function cronToast(text){
  const el = document.getElementById('cron-toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(cronToastTimer);
  cronToastTimer = setTimeout(() => el.classList.remove('show'), 5000);
}
let cronSaveTimer = null;
let cronTreeVersion = null;    // token from hydrate; PUTs echo it (stale → 409)
let cronPendingDeletes = 0;    // deletions since the last successful save (declared to the server)
let cronSaveInFlight = false;
let cronSaveQueued = false;
function cronSave(){
  // Debounce so a burst of edits coalesces into one PUT of the whole tree.
  clearTimeout(cronSaveTimer);
  cronSaveTimer = setTimeout(cronSavePush, 250);
}
async function cronSavePush(){
  // Serialize PUTs: a save fired while one is in flight would still carry the
  // old version token and 409 against our own write. Queue it instead.
  if (cronSaveInFlight){ cronSaveQueued = true; return; }
  cronSaveInFlight = true;
  try {
    const r = await fetch('/cron/api/tree', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({folders: cronFolders, jobs: cronRowsState,
                            version: cronTreeVersion, deletes: cronPendingDeletes}),
    });
    const j = await r.json().catch(() => null);
    if (r.status === 409){
      // Another tab/editor changed the tree since this page hydrated. Their
      // version wins: re-hydrate rather than clobber. This page's last edit
      // burst is dropped (redo it on the fresh state).
      await cronLoadTree();
      cronPendingDeletes = 0;
      if (cronEditUuid && !cronRowsState.find(x => x.uuid === cronEditUuid)) cronEditUuid = null;
      if (cronSelectedFolder && !cronFolderById(cronSelectedFolder)) cronSelectedFolder = null;
      cronPopulateFolderSelect();
      cronRenderTree();
      cronRender();
      cronToast('Cron tree was changed elsewhere — reloaded. Your last edit was not saved.');
    } else if (!r.ok){
      cronToast('Save refused: ' + ((j && j.error) || ('HTTP ' + r.status)));
    } else {
      cronTreeVersion = (j && j.version) || cronTreeVersion;
      cronPendingDeletes = 0;
    }
  } catch (e) {
    // Network error: keep local state + version; the next edit retries.
  } finally {
    cronSaveInFlight = false;
    if (cronSaveQueued){ cronSaveQueued = false; cronSavePush(); }
  }
}

// ---- initial paint (after hydrating from the backend) ----
cronInitTreeDnD();
cronLoadTree().then(() => {
  cronRenderPaused();
  cronPopulateFolderSelect();
  cronPopulateTargetSelect('f-target', '');
  cronRefresh();
  cronToggleType();
  // Deep link: ?id=<uuid> selects that folder or job on load. Read it before
  // the first render (cronRender's cronSyncUrl would otherwise clear it).
  const wantId = new URLSearchParams(window.location.search).get('id');
  if (wantId && cronFolderById(wantId)){
    cronSelectFolder(wantId);
  } else if (wantId && cronRowsState.find(j => j.uuid === wantId)){
    cronSelectJob(wantId);
  } else {
    cronRenderTree();
    cronRender();
  }
});
