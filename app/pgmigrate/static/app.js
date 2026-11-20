// pg-migrate UI. No framework: hash routes, fetch, and template strings.
// Every dynamic value goes through esc() before it reaches innerHTML;
// logs are set with textContent.
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const app = $('#app');
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtNum = (n) => (n == null ? '–' : Number(n).toLocaleString('en-US'));

// ------------------------------------------------------------------ api
async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (method !== 'GET') {
    opts.headers['Content-Type'] = 'application/json';
    opts.headers['X-PGM-Request'] = '1';
    opts.body = JSON.stringify(body ?? {});
  }
  const res = await fetch(path, opts);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
  if (!res.ok) {
    if (res.status === 401) toast('Not signed in, or your session expired. Reload the page to sign in again.', 'bad');
    const err = new Error((data && data.error) || res.statusText);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

// ----------------------------------------------------------------- time
function ago(iso) {
  if (!iso) return '–';
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
function duration(a, b) {
  if (!a) return '';
  const s = Math.round(((b ? new Date(b) : new Date()) - new Date(a)) / 1000);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

// ---------------------------------------------------------------- theme
(function theme() {
  const saved = (() => { try { return localStorage.getItem('pgm-theme'); } catch { return null; } })();
  if (saved) document.documentElement.dataset.theme = saved;
  $('#theme-toggle').addEventListener('click', () => {
    const cur = document.documentElement.dataset.theme
      || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('pgm-theme', next); } catch { /* private mode */ }
  });
})();

// ------------------------------------------------------- shared derived
let system = null;
async function loadSystem(fresh) {
  try {
    system = await api('GET', '/api/system' + (fresh ? '?fresh=1' : ''));
    const chip = $('#cli-chip');
    if (!system.render_ready) { chip.className = 'chip bad'; chip.textContent = 'Render API not configured'; }
    else if (!system.latest_image) { chip.className = 'chip'; chip.textContent = 'No image yet'; }
    else { chip.className = 'chip'; chip.textContent = `Image ${system.latest_image.tag}`; }
  } catch (e) { /* shown on the Image & settings page */ }
  return system;
}

function report(v) { return (v.status && v.status.report) || null; }
function remote(v) { const r = report(v); return (r && r.status) || null; }
function sandbox(v) { const r = report(v); return (r && r.sandbox) || null; }
function markers(v) { const s = remote(v); return (s && s.markers) || {}; }
// Render suspends a sandbox that's idle between steps; the next step wakes it.
const ASLEEP = ['suspending', 'suspended', 'resuming'];
function usable(v) { const sb = sandbox(v); return !!sb && (sb.status === 'running' || ASLEEP.includes(sb.status)); }
function asleep(v) { const sb = sandbox(v); return !!sb && ASLEEP.includes(sb.status); }
function minutes(sec) { return sec % 60 === 0 ? `${sec / 60} minute${sec === 60 ? '' : 's'}` : `${sec} seconds`; }

const PHASES = {
  'not-started': 'Not started', 'schema-loaded': 'Schema loaded', copying: 'Copying', copied: 'Copied',
  indexing: 'Building indexes', replicating: 'Replicating', 'cutting-over': 'Cutting over',
  'cutover-done': 'Cut over', unknown: 'Unknown',
};
// Torn down before any sync started (e.g. to change connection settings):
// nothing happened on Heroku, and a new sandbox picks up where it left off.
// Torn down after a sync is the end of a migration.
const restartable = (v) => !!v.torn_down && v.sync_started === false;
function phaseOf(v) {
  if (v.torn_down && !restartable(v)) return { key: 'done', label: 'Finished' };
  if (!v.sandbox_id || v.torn_down) return { key: 'new', label: 'No sandbox' };
  const s = remote(v);
  if (v.preflight && !v.preflight.passed && (!s || ['not-started', 'schema-loaded'].includes(s.phase))) {
    return { key: 'blocked', label: 'Preflight blocked' };
  }
  if (!s) return { key: 'pending', label: 'Waiting for status' };
  return { key: s.phase, label: PHASES[s.phase] || s.phase };
}
function phaseChip(v) {
  const p = phaseOf(v);
  const cls = ['cutover-done', 'done'].includes(p.key) ? 'ok' : ['unknown', 'blocked'].includes(p.key) ? 'bad' : 'strong';
  return `<span class="chip ${cls}">${esc(p.label)}</span>`;
}
function healthChip(v) {
  const s = remote(v);
  if (!s || v.torn_down) return '';
  if (['copying', 'copied', 'indexing', 'replicating'].includes(s.phase)) {
    return s.healthy ? '<span class="chip ok"><span class="dot ok"></span>Healthy</span>'
      : '<span class="chip bad"><span class="dot bad"></span>Needs attention</span>';
  }
  return '';
}
function sandboxChip(v) {
  if (v.torn_down) return '<span class="chip">Torn down</span>';
  if (!v.sandbox_id) return '<span class="chip">None</span>';
  const sb = sandbox(v);
  const st = sb ? sb.status : 'unknown';
  const cls = st === 'running' ? 'ok' : ['errored', 'terminated', 'missing'].includes(st) ? 'bad' : '';
  return `<span class="chip ${cls}">${esc(st)}</span>`;
}

// --------------------------------------------------------------- router
// Each navigation bumps routeGen. Async renders check stale() after every
// await so a slow response for an old route can't overwrite the new page,
// and timers registered by an old route stop themselves.
let timers = [];
let routeGen = 0;
function clearTimers() { timers.forEach(clearInterval); timers = []; }
function every(ms, fn) {
  const gen = routeGen;
  const t = setInterval(() => { if (gen !== routeGen) { clearInterval(t); return; } fn(); }, ms);
  timers.push(t);
}
function stale(gen) { return gen !== routeGen; }

async function route() {
  clearTimers();
  routeGen += 1;
  const hash = location.hash || '#/';
  document.querySelectorAll('[data-nav]').forEach((a) => a.classList.remove('active'));
  let m;
  if (hash === '#/' || hash === '#') { navActive('migrations'); return renderList(); }
  if (hash === '#/new') { navActive('migrations'); return renderNew(); }
  if (hash === '#/image' || hash === '#/environment') { navActive('image'); return renderImage(); }
  if ((m = hash.match(/^#\/m\/([a-z0-9-]+)$/))) { navActive('migrations'); return renderDetail(m[1]); }
  app.innerHTML = '<div class="empty">Not found. <a href="#/">Back to migrations</a></div>';
}
function navActive(name) { const a = document.querySelector(`[data-nav="${name}"]`); if (a) a.classList.add('active'); }
window.addEventListener('hashchange', route);

// ------------------------------------------------------------ list view
async function renderList() {
  app.innerHTML = `
    <div class="page-head">
      <div class="titles"><h1>Migrations</h1>
        <p class="muted">Each migration runs in its own Render sandbox with its own Bucardo.</p></div>
      <a class="btn btn-primary" href="#/new">New migration</a>
    </div>
    <div class="card" id="list-card"><div class="empty">Loading…</div></div>`;
  const gen = routeGen;
  const draw = async () => {
    let list;
    try { list = await api('GET', '/api/migrations'); } catch (e) {
      if (stale(gen)) return;
      $('#list-card').innerHTML = `<div class="card-body"><div class="notice bad">${esc(e.message)}</div></div>`; return;
    }
    if (stale(gen)) return;
    if (!list.length) {
      $('#list-card').innerHTML = `<div class="empty"><p>No migrations yet.</p>
        <p class="faint">Create one with the Heroku and Render connection strings. Build and import the image first under
        <a href="#/image">Image &amp; settings</a>.</p>
        <a class="btn btn-primary" href="#/new">New migration</a></div>`;
      return;
    }
    $('#list-card').innerHTML = `<div class="card-body flush"><table>
      <thead><tr><th>Name</th><th>Heroku</th><th>Render</th><th>Phase</th><th>Health</th>
      <th class="r">Rows on Render</th><th>Sandbox</th><th>Activity</th></tr></thead>
      <tbody>${list.map(listRow).join('')}</tbody></table></div>`;
    document.querySelectorAll('tr[data-id]').forEach((tr) => tr.addEventListener('click', () => { location.hash = `#/m/${tr.dataset.id}`; }));
  };
  await draw();
  every(5000, draw);
}

function listRow(v) {
  const s = remote(v);
  let rows = '–';
  if (s && s.totals && s.totals.source_rows && s.markers && s.markers['sync.started'] && !v.torn_down) {
    const pct = Math.min(100, Math.round((100 * s.totals.target_rows) / s.totals.source_rows));
    rows = `<div class="num">${fmtNum(s.totals.target_rows)} / ${fmtNum(s.totals.source_rows)}</div>
      <div class="bar" title="${pct}%"><span style="width:${pct}%"></span></div>`;
  }
  const activity = v.active_job ? `<span class="chip"><span class="dot run"></span>${esc(v.active_job.label)}</span>`
    : (v.status ? `<span class="faint">status ${esc(ago(v.status.at))}</span>` : '');
  return `<tr class="clickable" data-id="${esc(v.id)}">
    <td><a href="#/m/${esc(v.id)}">${esc(v.title)}</a>${v.title !== v.id ? `<div class="faint mono">${esc(v.id)}</div>` : ''}</td>
    <td class="mono">${esc(v.source.host)}<div class="faint">${esc(v.source.db)}</div></td>
    <td class="mono">${esc(v.target.host)}<div class="faint">${esc(v.target.db)}</div></td>
    <td>${phaseChip(v)}</td><td>${healthChip(v)}</td>
    <td class="r">${rows}</td><td>${sandboxChip(v)}</td><td>${activity}</td></tr>`;
}

// ------------------------------------------------------------- new view
function renderNew() {
  const plans = (system && system.plans) || ['1c-2g', '2c-4g', '4c-8g', '8c-16g'];
  app.innerHTML = `
    <div class="page-head"><div class="titles"><div class="crumbs"><a href="#/">Migrations</a> /</div><h1>New migration</h1></div></div>
    <div class="card"><div class="card-body">
    <form class="form" id="new-form" novalidate autocomplete="off">
      <div class="cols-2">
        <label class="field"><span class="label">Name</span>
          <input class="input mono" name="id" required pattern="[a-z0-9][a-z0-9-]{0,39}" maxlength="40" placeholder="acme-prod" spellcheck="false">
          <span class="hint">Lowercase letters, digits and dashes. The sandbox is named <code>pg-migrate-&lt;name&gt;</code>.</span>
          <span class="err" data-err="id"></span></label>
        <label class="field"><span class="label">Title <span class="faint">(optional)</span></span>
          <input class="input" name="title" maxlength="80" placeholder="Acme production"></label>
      </div>
      ${secretField('heroku_url', 'Heroku database URL', 'postgres://user:password@ec2-….compute-1.amazonaws.com:5432/dbname',
        'From <code>heroku config:get DATABASE_URL -a &lt;app&gt;</code>.')}
      ${secretField('render_url', 'Render database URL (external)', 'postgresql://user:password@dpg-….render.com/dbname',
        'The <em>external</em> URL from the database\'s Connect menu. Allow the sandbox\'s outbound IP in the database\'s inbound IP rules.')}
      ${secretField('render_superuser_url', 'Render superuser URL (optional)', 'postgresql://migration_super_user:password@dpg-….render.com/dbname',
        SUPERUSER_HINT)}
      <details><summary>Advanced</summary>
        <div class="form">
          <div class="cols-2">
            <label class="field"><span class="label">Schemas</span>
              <input class="input mono" name="schemas" placeholder="public" spellcheck="false">
              <span class="hint">Comma-separated. Default <code>public</code>.</span><span class="err" data-err="schemas"></span></label>
            <label class="field"><span class="label">Bucardo sync name</span>
              <input class="input mono" name="sync_name" placeholder="migrate_&lt;name&gt;" spellcheck="false">
              <span class="err" data-err="sync_name"></span></label>
          </div>
          <div class="cols-2">
            <label class="field"><span class="label">Sandbox plan</span>
              <select class="input" name="plan">${plans.map((p) => `<option ${p === '2c-4g' ? 'selected' : ''}>${esc(p)}</option>`).join('')}</select></label>
            ${secretField('slack_webhook_url', 'Slack webhook (optional)', 'https://hooks.slack.com/services/…', 'Watchdog restarts and cutover completion are posted here.')}
          </div>
        </div>
      </details>
      <div class="notice">Connection strings are stored encrypted in this app's database and passed to the
        migration's sandbox when it is created. They are never sent back to the browser.</div>
      <div class="btn-row"><button class="btn btn-primary" type="submit">Create migration</button><a class="btn btn-ghost" href="#/">Cancel</a></div>
      <div class="err" id="form-err"></div>
    </form></div></div>`;
  wireSecretToggles(app);
  $('#new-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    const body = Object.fromEntries([...f.entries()].map(([k, v]) => [k, String(v).trim()]));
    document.querySelectorAll('[data-err]').forEach((e) => { e.textContent = ''; });
    document.querySelectorAll('.input.invalid').forEach((e) => e.classList.remove('invalid'));
    $('#form-err').textContent = '';
    try {
      const v = await api('POST', '/api/migrations', body);
      location.hash = `#/m/${v.id}`;
    } catch (e) {
      const fields = (e.data && e.data.fields) || {};
      for (const [k, msg] of Object.entries(fields)) {
        const slot = document.querySelector(`[data-err="${k}"]`);
        if (slot) slot.textContent = msg;
        const input = document.querySelector(`[name="${k}"]`);
        if (input) input.classList.add('invalid');
      }
      if (!Object.keys(fields).length) $('#form-err').textContent = e.message;
    }
  });
}

const SUPERUSER_HINT = 'Only if preflight asks for it: the same database, logging in as the superuser Render support set up. '
  + 'Every step logs in with it, and cutover gives everything it creates to the user in the Render database URL.';

function secretField(name, label, placeholder, hint) {
  return `<label class="field"><span class="label">${esc(label)}</span>
    <span class="input-row"><input class="input mono" type="password" name="${esc(name)}" placeholder="${esc(placeholder)}" spellcheck="false" autocomplete="off">
    <button class="btn btn-ghost btn-sm" type="button" data-reveal="${esc(name)}">Show</button></span>
    <span class="hint">${hint}</span><span class="err" data-err="${esc(name)}"></span></label>`;
}
function wireSecretToggles(root) {
  root.querySelectorAll('[data-reveal]').forEach((b) => b.addEventListener('click', () => {
    const input = root.querySelector(`[name="${b.dataset.reveal}"]`);
    input.type = input.type === 'password' ? 'text' : 'password';
    b.textContent = input.type === 'password' ? 'Show' : 'Hide';
  }));
}

// ---------------------------------------------------------- detail view
let selectedJob = null;
let logState = { job: null, offset: 0 };

async function renderDetail(id) {
  const gen = routeGen;
  selectedJob = null;
  logState = { job: null, offset: 0 };
  stepsHtml = '';
  app.innerHTML = '<div class="empty">Loading…</div>';
  let v;
  try { v = await api('GET', `/api/migrations/${id}`); } catch (e) {
    if (!stale(gen)) app.innerHTML = `<div class="empty">${esc(e.message)} <a href="#/">Back</a></div>`;
    return;
  }
  if (stale(gen)) return;
  app.innerHTML = `
    <div class="page-head">
      <div class="titles"><div class="crumbs"><a href="#/">Migrations</a> / <span class="mono">${esc(id)}</span></div>
        <h1 id="d-title"></h1></div>
      <div class="btn-row" id="d-chips"></div>
    </div>
    <div class="grid-detail">
      <div class="stack">
        <div class="card"><div class="card-head"><h2>Runbook</h2><span class="faint" id="d-next"></span></div>
          <ol class="steps" id="d-steps"></ol></div>
        <div class="card"><div class="card-head"><h2>More actions</h2></div><div class="card-body" id="d-more"></div></div>
        <div class="card"><div class="card-head"><h2>Settings</h2><button class="btn btn-ghost btn-sm" id="d-edit" type="button">Edit</button></div>
          <div class="card-body" id="d-settings"></div></div>
        <div class="card"><div class="card-head"><h2>Artifacts</h2></div><div class="card-body" id="d-artifacts"></div></div>
      </div>
      <div class="stack">
        <div class="card"><div class="card-head"><h2>Status</h2>
          <span class="btn-row"><span class="faint" id="d-status-at"></span>
          <button class="btn btn-ghost btn-sm" id="d-refresh" type="button">Refresh</button></span></div>
          <div class="card-body" id="d-status"></div></div>
        <div class="card" id="d-preflight-card" hidden><div class="card-head"><h2>Preflight</h2><span id="d-preflight-at" class="faint"></span></div>
          <div class="card-body" id="d-preflight"></div></div>
        <div class="card"><div class="card-head"><h2>Activity</h2><span id="d-log-actions" class="btn-row"></span></div>
          <div class="card-body flush"><table class="jobs-list"><tbody id="d-jobs"></tbody></table>
          <pre class="log" id="d-log" aria-label="Job output"></pre></div></div>
        <div class="card"><div class="card-head"><h2>Tables</h2><span class="faint" id="d-tables-note"></span></div>
          <div class="card-body flush" id="d-tables"></div></div>
      </div>
    </div>`;
  $('#d-refresh').addEventListener('click', async () => {
    $('#d-status-at').textContent = 'refreshing…';
    try { await api('POST', `/api/migrations/${id}/refresh`); } catch (e) { toast(e.message, 'bad'); }
  });
  $('#d-edit').addEventListener('click', () => editSettings(id));
  const draw = (data) => { if (!stale(gen)) drawDetail(id, data); };
  draw(v);
  tailLog(id);
  every(4000, async () => { try { draw(await api('GET', `/api/migrations/${id}`)); } catch { /* keep last */ } });
  every(1500, () => tailLog(id));
}

let lastDetail = null;
function drawDetail(id, v) {
  lastDetail = v;
  $('#d-title').textContent = v.title;
  $('#d-chips').innerHTML = `${phaseChip(v)} ${healthChip(v)} <span class="faint">Sandbox</span> ${sandboxChip(v)}`;
  drawSteps(id, v);
  drawMore(id, v);
  drawSettings(v);
  drawStatus(v);
  drawPreflight(v);
  drawJobs(id, v);
  drawTables(v);
  drawArtifacts(id, v);
}

function drawArtifacts(id, v) {
  const list = v.artifacts || [];
  $('#d-artifacts').innerHTML = list.length
    ? `<ul class="plain">${list.map((a) => `<li><a href="/api/migrations/${esc(id)}/artifacts/${esc(a.id)}" download>${esc(a.name)}</a>
        <span class="faint">${esc(Math.max(1, Math.round(a.size / 1024)))} KB · ${esc(ago(a.created_at))}</span></li>`).join('')}</ul>`
    : '<p class="muted">Schema dumps, reports and logs from the sandbox. Use Fetch artifacts; teardown saves a final copy.</p>';
}

function lastJob(v, action) { return (v.jobs || []).find((j) => j.action === action); }

// SQL shown in the runbook, by step, for the Copy buttons.
const stepSql = {};
// <details> the user opened, so the 4-second redraw keeps them open.
const openDetails = new Set();

function sqlBlock(key, summary, sql) {
  stepSql[key] = sql;
  return `<details class="sql-details" data-details="${esc(key)}" ${openDetails.has(key) ? 'open' : ''}>
    <summary>${esc(summary)}</summary><pre class="sql">${esc(sql)}</pre></details>`;
}

// Tables without a key Bucardo can use (keys.sh's plan, from preflight).
function keysTableList(tables) {
  return `<ul class="plain keys-list">${tables.map((t) => {
    const notes = [];
    if (t.manual) notes.push(`No SQL for this one: ${esc(t.manual)}.`);
    else {
      if (!t.owner_ok) notes.push('The Heroku role doesn\'t own it, so its owner must run the SQL.');
      if ((t.update_triggers || []).length) {
        notes.push(`The backfill updates every row once, which fires its UPDATE trigger${t.update_triggers.length > 1 ? 's' : ''}
          <span class="mono">${esc(t.update_triggers.join(', '))}</span> for each row. Check they don't change data.`);
      }
    }
    const size = [t.rows ? `~${fmtNum(t.rows)} rows` : '', t.size || ''].filter(Boolean).join(', ');
    return `<li><span class="mono">${esc(t.table)}</span>: ${esc(t.reason)}${size ? ` <span class="faint">(${esc(size)})</span>` : ''}
      ${notes.map((n) => `<div class="notice warn">${n}</div>`).join('')}</li>`;
  }).join('')}</ul>`;
}

function keysSteps(v, running, cleanupDone) {
  const kp = v.keys_plan || {};
  const mk = markers(v);
  const need = kp.tables || [];
  const auto = need.filter((t) => !t.manual);
  const added = kp.added || [];
  const addJob = lastJob(v, 'add_keys');
  const dropJob = lastJob(v, 'drop_keys');
  const out = {};
  if (need.length || added.length || addJob || mk['keys.added']) {
    const col = (auto[0] && auto[0].col) || 'mig_id';
    out.add = { key: 'add_keys', title: 'Replication keys', done: !need.length,
      buttonLabel: 'Run it for me',
      enabled: running && !!kp.add_sha && auto.length > 0 && auto.every((t) => t.owner_ok) && !mk['schema.done'],
      copy: kp.add_sql ? 'add_keys' : null };
    if (!need.length) {
      out.add.desc = added.length
        ? `Bucardo uses the key${added.length > 1 ? 's' : ''} pg-migrate added: ${added.map((a) => `<span class="mono">${esc(a.table)}.${esc(a.col)}</span>`).join(', ')}.
           Load schema copies ${added.length > 1 ? 'them' : 'it'} to Render.`
        : 'Every table has a key Bucardo can use.';
    } else {
      out.add.desc = `Bucardo needs a primary key or a unique index on every table, and ${need.length === 1 ? 'this table has' : `these ${need.length} tables have`}
        none it can use:`;
      out.add.body = keysTableList(need) + (auto.length ? `
        <p class="step-desc">The SQL gives ${auto.length === 1 ? 'it' : 'each'} a key without locking the table: a <code>bigint</code> column
          <code>${esc(col)}</code> with a sequence default, filled in batches of 100 pages, and a unique index built with
          <code>CREATE INDEX CONCURRENTLY</code>. Load schema then copies the column, its default and the index to Render.</p>
        ${sqlBlock('add_keys', 'SQL for Heroku', kp.add_sql)}
        <p class="hint">Paste it into <code>heroku pg:psql -a &lt;app&gt;</code> and run preflight again, or let this app run it
          with the migration's Heroku credentials: each statement and its result appear under Activity, then preflight
          runs again. It's safe to run again if it stops partway. The backfill writes every row once, so a large table
          means a lot of WAL. Afterwards <code>SELECT *</code> on these tables returns the extra column, so check code
          or <code>COPY</code> jobs that expect a fixed set of columns.</p>` : '');
      if (mk['schema.done']) out.add.body += '<div class="notice warn">The schema is already on Render, so a column added on Heroku now wouldn\'t reach it, and Bucardo would refuse the sync. Add keys before Load schema: start over with an empty Render database.</div>';
    }
  }
  if (added.length || dropJob || mk['keys.dropped']) {
    const dropped = !added.length && !!(mk['keys.dropped'] || (dropJob && dropJob.state === 'succeeded'));
    out.drop = { key: 'drop_keys', title: 'Remove replication keys', optional: true, danger: true, done: dropped,
      buttonLabel: 'Run it for me', copy: !dropped && kp.drop_sql ? 'drop_keys' : null,
      enabled: running && cleanupDone && added.length > 0 && !!kp.drop_sha };
    out.drop.desc = dropped ? 'Removed from Heroku and Render.' : `Optional. A unique surrogate key is usually harmless: keep it unless something
      depends on the table having no key, or on its exact columns. It also helps if you later use logical replication.
      ${cleanupDone ? '' : '<br>Available after Clean up Heroku: Bucardo\'s triggers on Heroku use the column, and with it gone every write to the table would fail.'}`;
    if (!dropped && kp.drop_sql) {
      out.drop.body = `${sqlBlock('drop_keys', 'SQL for Heroku and Render', kp.drop_sql)}
        <p class="hint">Run it on both databases with psql, or let this app run it on Heroku, then Render. Each index is
          dropped concurrently, then its column, which takes a brief exclusive lock; a 5-second lock timeout keeps it from
          stalling other queries, and it's safe to run again.${v.torn_down ? '' : ' Do it before Tear down, or run it yourself afterwards.'}</p>`;
    }
  }
  return out;
}

function stepModel(v) {
  const s = remote(v);
  const mk = markers(v);
  const running = usable(v); // a suspended sandbox wakes for the step
  const phase = s ? s.phase : null;
  const imported = system && system.latest_image && system.latest_image.tag;
  const cleanupDone = !!(mk['cleanup_source.done'] || v.cleanup_done_locally);
  // Only Start sync puts Bucardo objects on Heroku; without it there's nothing to clean up.
  const forceTeardown = !cleanupDone && v.sync_started === false;
  const keys = keysSteps(v, running, cleanupDone);
  // Blocked only by tables without a key: the Replication keys step is next.
  const pf = v.preflight;
  const keysOnly = !!(pf && !pf.passed && pf.blockers.length && keys.add && !keys.add.done
    && pf.blockers.every((b) => b.startsWith('tables without a primary key')));
  return [
    { key: 'create_sandbox', title: 'Sandbox', done: v.torn_down ? !restartable(v) : (!!v.sandbox_id && running),
      desc: v.sandbox_id && !restartable(v) ? `<span class="mono">${esc(v.sandbox_id)}</span>${v.sandbox_info ? ` · image <span class="mono">${esc(v.sandbox_info.IMAGE_TAG || '')}</span>` : ''}${networkNote(v.sandbox_info)}${sleepNote(v)}`
        : imported ? `${restartable(v) ? 'The last sandbox was torn down. A new one uses the current settings, and preflight runs again. ' : ''}Starts a sandbox from image <span class="mono">${esc(imported)}</span> with no lifetime limit and unrestricted outbound network access (Render's allow-lists can't admit Postgres yet). ${idlePolicy(system.config.SANDBOX_IDLE_TIMEOUT_SECONDS)}`
          : 'Build and import the image first under <a href="#/image">Image &amp; settings</a>.',
      enabled: (v.torn_down ? restartable(v) : (!v.sandbox_id || !running)) && !!imported },
    { key: 'preflight', title: 'Preflight', done: !!(v.preflight && v.preflight.passed),
      failed: !!(v.preflight && !v.preflight.passed),
      desc: v.preflight ? (v.preflight.passed ? `Passed with ${v.preflight.warnings.length} warning(s).`
        : keysOnly ? 'Blocked only by tables without a key. Add them in the next step, then preflight runs again.'
          : `${v.preflight.blockers.length} blocker(s). Fix them and rerun.`)
        : 'Read-only go/no-go checks against both databases.', enabled: running, deferred: keysOnly },
    keys.add,
    { key: 'schema', title: 'Load schema', done: !!mk['schema.done'],
      desc: v.preflight && v.preflight.passed ? 'Loads the schema into Render without its indexes.' : 'Loads the schema into Render without its indexes. Needs a passing preflight.',
      enabled: running && !mk['schema.done'] && !!(v.preflight && v.preflight.passed) },
    { key: 'start_sync', title: 'Start replication', done: !!mk['sync.started'] && phase && !['copying'].includes(phase),
      desc: phase === 'copying' ? 'Initial copy in progress.' : 'Bucardo copies every table, then keeps replicating changes.',
      enabled: running && !!mk['schema.done'] && !mk['cutover.done'] },
    { key: 'build_indexes', title: 'Build indexes', done: !!mk['indexes.done'],
      desc: 'Builds the deferred indexes concurrently on Render.', enabled: running && ['copied', 'indexing', 'replicating'].includes(phase) && !mk['indexes.done'] },
    { key: 'cutover', title: 'Cut over', done: !!mk['cutover.done'], danger: true,
      desc: 'After Heroku writes stop: waits for replication, checks exact counts, sets sequences, stops Bucardo.',
      enabled: running && phase === 'replicating' },
    { key: 'cleanup_source', title: 'Clean up Heroku', done: cleanupDone, danger: true,
      desc: 'Drops the bucardo schema and its triggers on Heroku (and Render).', enabled: running && !!mk['cutover.done'] && !cleanupDone },
    keys.drop,
    { key: 'teardown', title: 'Tear down', done: !!v.torn_down && !restartable(v), danger: true, force: forceTeardown,
      desc: forceTeardown
        ? 'No sync was started, so Bucardo has nothing on Heroku to clean up. Fetches artifacts one last time and terminates the sandbox.'
        : 'Fetches artifacts one last time and terminates the sandbox.',
      enabled: !!v.sandbox_id && !v.torn_down && (cleanupDone || forceTeardown) },
  ].filter(Boolean);
}

// The Sandbox step's network line: what the sandbox can reach.
function networkNote(info) {
  if (!info || !info.NETWORK_ALLOW_LIST) return '';
  const hosts = `<span class="mono">${esc(info.NETWORK_ALLOW_LIST.join(', '))}</span>`;
  // Sandboxes recorded without NETWORK_POLICY were created under their allow-list.
  if (info.NETWORK_POLICY !== 'allow-all') {
    return `<br>Network: allow-list ${hosts}. Render's allow-lists admit HTTPS only, so this sandbox can't reach Postgres. `
      + 'Tear it down and create the sandbox again: new sandboxes are allow-all.';
  }
  return info.NETWORK_ALLOW_LIST.length
    ? `<br>Network: allow-all. Allow-list ${hosts} not enforced: Render's allow-lists admit HTTPS only, which would block Postgres.`
    : '<br>Network: allow-all.';
}

// When the sandbox suspends, and whether it's awake now.
function idlePolicy(sec) {
  if (!sec) return 'It never suspends.';
  return `It suspends after ${minutes(sec)} with no step running, except while Bucardo replicates, and the next step wakes it.`;
}
function sleepNote(v) {
  if (v.torn_down || !v.sandbox_info || v.sandbox_info.IDLE_TIMEOUT_SECONDS === undefined) return '';
  const r = report(v);
  const now = asleep(v) ? ' Suspended now.' : r && r.keep_awake ? ` Kept awake: ${esc(r.keep_awake)}.` : '';
  return `<br>${idlePolicy(v.sandbox_info.IDLE_TIMEOUT_SECONDS)}${now}`;
}

let stepsHtml = '';
function drawSteps(id, v) {
  const steps = stepModel(v);
  // Optional steps (Remove replication keys) are never "next", nor is a
  // preflight that waits for the Replication keys step.
  const current = steps.findIndex((st) => !st.done && !st.optional && !st.deferred);
  const busy = !!v.active_job;
  $('#d-next').textContent = current === -1 ? 'Complete' : `Next: ${steps[current].title}`;
  const html = steps.map((st, i) => {
    const job = lastJob(v, st.key);
    const failed = st.failed || (job && job.state === 'failed' && !st.done);
    const state = st.done ? 'done' : failed ? 'failed' : i === current ? 'current' : 'pending';
    const running = v.active_job && v.active_job.action === st.key;
    const action = (system && system.actions || []).find((a) => a.name === st.key);
    const label = st.buttonLabel || (action ? action.label : st.title);
    const btnCls = st.danger ? 'btn-danger' : i === current ? 'btn-primary' : 'btn-secondary';
    return `<li class="step ${state}">
      <span class="step-n">${st.done ? '&#10003;' : i + 1}</span>
      <div><div class="step-title">${esc(st.title)}
        ${st.optional && !st.done ? '<span class="chip">Optional</span>' : ''}
        ${running ? '<span class="chip"><span class="dot run"></span>Running</span>' : ''}
        ${failed && !running ? '<span class="chip bad">Failed</span>' : ''}</div>
        <div class="step-desc">${st.desc}</div>
        ${st.body ? `<div class="step-body">${st.body}</div>` : ''}
        ${st.done && st.key !== 'preflight' ? '' : `<div class="step-actions btn-row">
          <button class="btn btn-sm ${btnCls}" data-action="${st.key}" ${st.force ? 'data-force="1"' : ''} ${(!st.enabled || busy) && st.key !== 'preflight' ? 'disabled' : ''}
            ${st.key === 'preflight' && (!st.enabled) ? 'disabled' : ''}>${esc(label)}</button>
          ${st.copy ? `<button class="btn btn-sm btn-ghost" type="button" data-copy="${esc(st.copy)}">Copy SQL</button>` : ''}</div>`}
      </div></li>`;
  }).join('');
  // Redraw only on change, so an open <details>, a text selection or a
  // scrolled SQL block survives the 4-second refresh.
  if (html === stepsHtml) return;
  stepsHtml = html;
  $('#d-steps').innerHTML = html;
  $('#d-steps').querySelectorAll('[data-action]').forEach((b) => b.addEventListener('click',
    () => runAction(id, b.dataset.action, { force: b.dataset.force === '1' })));
  $('#d-steps').querySelectorAll('[data-copy]').forEach((b) => b.addEventListener('click',
    () => copyText(stepSql[b.dataset.copy] || '')));
  $('#d-steps').querySelectorAll('details[data-details]').forEach((d) => d.addEventListener('toggle', () => {
    if (d.open) openDetails.add(d.dataset.details); else openDetails.delete(d.dataset.details);
  }));
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('SQL copied');
    return;
  } catch { /* not a secure context, or no permission: fall back */ }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  ta.remove();
  toast(ok ? 'SQL copied' : 'Couldn\'t copy: open the SQL and select it', ok ? '' : 'bad');
}

function drawMore(id, v) {
  const running = usable(v);
  const busy = !!v.active_job;
  const btn = (name, label, enabled, cls = 'btn-ghost') =>
    `<button class="btn btn-sm ${cls}" data-more="${name}" ${enabled ? '' : 'disabled'}>${esc(label)}</button>`;
  $('#d-more').innerHTML = `<div class="btn-row">
      ${btn('fetch_artifacts', 'Fetch artifacts', running)}
      ${btn('status_exact', 'Exact row counts', running)}
      ${btn('snapshot', 'Snapshot sandbox', running)}
      ${btn('diagnose', 'Diagnostics', running)}
      ${btn('start_sync', 'Restart Bucardo / watchdog', running && !busy && !!markers(v)['sync.started'] && !markers(v)['cutover.done'])}
    </div>
    <div class="btn-row">
      ${btn('reset_target', 'Reset Render target', running && !busy && !markers(v)['cutover.done'], 'btn-danger')}
      ${btn('delete', 'Delete migration', (!v.sandbox_id || v.torn_down) && !busy, 'btn-danger')}
    </div>
    <p class="hint">Restart reruns <code>start_sync.sh</code>, which is idempotent: it starts whatever stopped.
      Reset truncates Render and drops the built indexes so the copy can run again.
      Diagnostics reports the sandbox's outbound IP for the Render database's inbound IP rules.</p>`;
  $('#d-more').querySelectorAll('[data-more]').forEach((b) => b.addEventListener('click', () => {
    if (b.dataset.more === 'delete') return deleteMigration(id);
    runAction(id, b.dataset.more);
  }));
}

function drawSettings(v) {
  $('#d-settings').innerHTML = `<dl class="kv">
    <dt>Heroku</dt><dd class="mono">${esc(v.source.display) || '–'}</dd>
    <dt>Render</dt><dd class="mono">${esc(v.target.display) || '–'}</dd>
    ${v.target.superuser ? `<dt>Render superuser</dt><dd class="mono">${esc(v.target.superuser)}</dd>` : ''}
    <dt>Schemas</dt><dd class="mono">${esc(v.schemas)}</dd>
    <dt>Sync name</dt><dd class="mono">${esc(v.sync_name)}</dd>
    <dt>Plan</dt><dd class="mono">${esc(v.plan)}</dd>
    <dt>Slack</dt><dd>${v.has_slack ? 'Configured' : 'Off'}</dd>
    <dt>Created</dt><dd>${esc(new Date(v.created_at).toLocaleString())}${v.created_by ? ` by ${esc(v.created_by)}` : ''}</dd></dl>
    ${v.sandbox_id && !v.torn_down ? `<p class="hint">Connection settings are locked while the sandbox exists: it keeps the values it was created with.${v.sync_started === false
    ? ' To change them before replication starts, tear the sandbox down, edit them here, and create a new sandbox.' : ''}</p>` : ''}`;
  $('#d-edit').disabled = !!(v.sandbox_id && !v.torn_down);
}

function drawStatus(v) {
  const r = report(v);
  const s = remote(v);
  const at = v.status ? `updated ${ago(v.status.at)}` : '';
  $('#d-status-at').textContent = at;
  if (!v.sandbox_id) {
    $('#d-status').innerHTML = '<p class="muted">No sandbox yet. Create one to see live status.</p>';
    return;
  }
  const parts = [];
  if (v.status && v.status.error) parts.push(`<div class="notice bad">${esc(v.status.error)}</div>`);
  if (r && r.error) parts.push(`<div class="notice">${esc(r.error)}</div>`);
  if (asleep(v)) {
    parts.push(`<div class="notice">The sandbox is ${esc(sandbox(v).status)}: it was idle between steps, and it wakes when you run the next one.${s ? ' The status below is from its last check.' : ''}</div>`);
  }
  if (!s) {
    parts.push(`<p class="muted">${v.status ? 'No status from inside the sandbox.' : 'Waiting for the first status check…'}</p>`);
    $('#d-status').innerHTML = parts.join('');
    return;
  }
  (s.errors || []).forEach((e) => parts.push(`<div class="notice bad">${esc(e)}</div>`));
  const proc = (ok, label) => `<span class="chip ${ok ? 'ok' : ''}"><span class="dot ${ok ? 'ok' : ''}"></span>${esc(label)}</span>`;
  const lr = (s.bucardo_sync && s.bucardo_sync.last_run) || null;
  parts.push(`<dl class="kv">
    <dt>Phase</dt><dd><strong>${esc(PHASES[s.phase] || s.phase)}</strong> <span class="muted">${esc(s.phase_detail)}</span></dd>
    <dt>Processes</dt><dd class="btn-row">${proc(s.processes.bucardo, 'Bucardo')}${proc(s.processes.watchdog, 'Watchdog')}${proc(s.processes.local_postgres, 'Control DB')}</dd>
    <dt>Last sync run</dt><dd>${lr ? `${esc(lr.status || '')} <span class="faint">${esc(ago(lr.ended || lr.started))}</span>` : '–'}</dd>
    <dt>Totals</dt><dd class="num">${fmtNum(s.totals.target_rows)} on Render / ${fmtNum(s.totals.source_rows)} on Heroku <span class="faint">(${s.exact_counts ? 'exact' : 'estimated'})</span></dd>
    <dt>Image</dt><dd class="mono">${esc(s.image_revision)}</dd>
  </dl>`);
  if ((s.copy_progress || []).length) {
    parts.push(`<div><div class="overline">Copying</div>${s.copy_progress.map((c) => `<div class="mono">${esc(c.table)} · ${fmtNum(c.tuples)} rows</div>`).join('')}</div>`);
  }
  if ((s.index_progress || []).length) {
    parts.push(`<div><div class="overline">Building</div>${s.index_progress.map((c) => {
      const pct = c.blocks_total ? Math.round((100 * c.blocks_done) / c.blocks_total) : null;
      return `<div class="mono">${esc(c.index)} · ${esc(c.phase)}${pct != null ? ` · ${pct}%` : ''}</div>`;
    }).join('')}</div>`);
  }
  if ((s.invalid_indexes || []).length) parts.push(`<div class="notice warn">Invalid indexes: ${esc(s.invalid_indexes.join(', '))}</div>`);
  if ((s.watchdog_restarts || []).length) {
    parts.push(`<details><summary>Watchdog restarts (${s.watchdog_restarts.length})</summary><pre class="log" style="border-radius:4px">${esc(s.watchdog_restarts.join('\n'))}</pre></details>`);
  }
  if ((s.log_tail || []).filter(Boolean).length) {
    parts.push(`<details><summary>Bucardo log</summary><pre class="log" style="border-radius:4px">${esc(s.log_tail.join('\n'))}</pre></details>`);
  }
  $('#d-status').innerHTML = parts.join('');
}

// Preflight puts SQL between backticks: show each statement as a block with a
// Copy button. Text with an unpaired backtick is shown as it is.
function withSql(text) {
  const parts = String(text).split('`');
  if (parts.length % 2 === 0) return esc(text);
  return parts.map((part, i) => (i % 2
    ? `<div class="sql-snippet"><pre class="sql">${esc(part)}</pre><button class="btn btn-sm btn-ghost" type="button" data-copy-sql>Copy SQL</button></div>`
    : esc(part))).join('');
}

function drawPreflight(v) {
  const p = v.preflight;
  $('#d-preflight-card').hidden = !p;
  if (!p) return;
  $('#d-preflight-at').textContent = ago(p.at);
  const list = (items) => `<ul class="plain">${items.map((x) => `<li>${withSql(x)}</li>`).join('')}</ul>`;
  $('#d-preflight').innerHTML = `
    ${p.passed ? '<div class="notice ok">Passed. No blockers.</div>' : `<div class="notice bad"><strong>Blocked</strong>${list(p.blockers)}</div>`}
    ${p.warnings.length ? `<div class="notice warn"><strong>Warnings</strong>${list(p.warnings)}</div>` : ''}
    ${p.info.length ? `<details><summary>Details (${p.info.length})</summary>${list(p.info)}</details>` : ''}`;
  $('#d-preflight').querySelectorAll('[data-copy-sql]').forEach((b) => b.addEventListener('click',
    () => copyText(b.previousElementSibling.textContent)));
}

function jobChip(j) {
  if (j.state === 'running') return '<span class="chip"><span class="dot run"></span>Running</span>';
  if (j.state === 'succeeded') return '<span class="chip ok">Succeeded</span>';
  if (j.state === 'failed') return `<span class="chip bad">Failed${j.exit_code != null ? ` (${j.exit_code})` : ''}</span>`;
  return `<span class="chip">${esc(j.state)}</span>`;
}

function drawJobs(id, v) {
  const jobs = v.jobs || [];
  if (!selectedJob && jobs.length) selectedJob = jobs[0].id;
  if (v.active_job && logState.job !== v.active_job.id && selectedJob !== v.active_job.id && jobs[0] && jobs[0].id === v.active_job.id) {
    selectedJob = v.active_job.id;
  }
  $('#d-jobs').innerHTML = jobs.length ? jobs.slice(0, 8).map((j) => `
    <tr class="clickable ${j.id === selectedJob ? 'selected' : ''}" data-job="${esc(j.id)}">
      <td>${esc(j.label)}</td><td>${jobChip(j)}</td><td class="faint">${esc(j.started_by || '')}</td>
      <td class="faint">${esc(ago(j.started_at))}</td><td class="faint r num">${esc(duration(j.started_at, j.finished_at))}</td></tr>`).join('')
    : '<tr><td class="faint">No jobs yet. Output from each step appears here.</td></tr>';
  $('#d-jobs').querySelectorAll('[data-job]').forEach((tr) => tr.addEventListener('click', () => {
    selectedJob = tr.dataset.job; drawJobs(id, lastDetail); tailLog(id);
  }));
  const sel = jobs.find((j) => j.id === selectedJob);
  $('#d-log-actions').innerHTML = sel && sel.state === 'running'
    ? `<button class="btn btn-ghost btn-sm" id="d-cancel" type="button">Cancel job</button>` : '';
  const c = $('#d-cancel');
  if (c) c.addEventListener('click', async () => {
    if (!confirm('Cancel stops the local command. A step already running inside the sandbox may keep going. Cancel anyway?')) return;
    try { await api('POST', `/api/migrations/${id}/jobs/${selectedJob}/cancel`); } catch (e) { toast(e.message, 'bad'); }
  });
}

async function tailLog(id) {
  if (!selectedJob) { $('#d-log').textContent = ''; return; }
  if (logState.job !== selectedJob) { logState = { job: selectedJob, offset: 0 }; $('#d-log').textContent = ''; }
  const pre = $('#d-log');
  if (!pre) return;
  try {
    const want = logState.job;
    const res = await api('GET', `/api/migrations/${id}/jobs/${want}/log?offset=${logState.offset}`);
    if (logState.job !== want) return;
    if (res.text) {
      const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
      appendLog(pre, res.text);
      if (atBottom) pre.scrollTop = pre.scrollHeight;
    }
    logState.offset = res.next_offset;
  } catch { /* job may have been pruned */ }
}

function appendLog(pre, text) {
  const frag = document.createDocumentFragment();
  for (const line of text.split(/(?<=\n)/)) {
    const span = document.createElement('span');
    if (/\b(ERROR|BLOCKER|FAIL|MISMATCH)\b/.test(line)) span.className = 'l-err';
    else if (/\bWARN/.test(line)) span.className = 'l-warn';
    else if (/RESULT: pass|\bexit 0 \(succeeded\)/.test(line)) span.className = 'l-ok';
    span.textContent = line;
    frag.appendChild(span);
  }
  pre.appendChild(frag);
}

function drawTables(v) {
  const s = remote(v);
  if (!s || !(s.tables || []).length) { $('#d-tables').innerHTML = '<p class="card-body muted">No table counts yet.</p>'; $('#d-tables-note').textContent = ''; return; }
  $('#d-tables-note').textContent = s.exact_counts ? 'exact counts' : 'estimated counts';
  $('#d-tables').innerHTML = `<table><thead><tr><th>Table</th><th class="r">Heroku</th><th class="r">Render</th><th></th></tr></thead><tbody>
    ${s.tables.map((t) => {
      const pct = t.source_rows ? Math.min(100, Math.round((100 * (t.target_rows || 0)) / t.source_rows)) : (t.target_rows ? 100 : 0);
      return `<tr><td class="mono">${esc(t.table)}</td><td class="r num">${fmtNum(t.source_rows)}</td><td class="r num">${fmtNum(t.target_rows)}</td>
        <td style="width:120px"><div class="bar"><span style="width:${pct}%"></span></div></td></tr>`;
    }).join('')}</tbody></table>`;
}

// ---------------------------------------------------------------- actions
const CONFIRM = {
  cutover: {
    title: 'Cut over to Render',
    body: `<p>Run this only after writes to Heroku have stopped. The script waits for replication to catch up,
      requires exact row counts to match on every table, sets sequences, refreshes materialized views,
      runs ANALYZE, and stops Bucardo.</p>
      <label class="check"><input type="checkbox" required> Heroku maintenance mode is on</label>
      <label class="check"><input type="checkbox" required> Every dyno and worker that writes is scaled to zero</label>`,
  },
  cleanup_source: { title: 'Clean up Heroku', body: '<p>Drops the <code>bucardo</code> schema on Heroku, which removes Bucardo\'s triggers from every table, and on Render if present. Run after cutover.</p>' },
  reset_target: { title: 'Reset the Render target', body: '<p>Stops Bucardo, <strong>truncates every migrated table on Render</strong>, drops the indexes built so far, and clears Bucardo\'s local configuration. Use it to rerun the copy.</p>' },
  teardown: { title: 'Tear down the sandbox', body: '<p>Saves the artifacts one last time (downloadable here), then terminates the sandbox. Refused unless cleanup of Heroku has succeeded, or no sync was ever started.</p>' },
  teardown_force: { title: 'Tear down the sandbox', body: '<p>No sync was ever started on this migration, so Bucardo has created nothing on Heroku and there is nothing to clean up there. This skips the Heroku cleanup check, saves the artifacts if the sandbox is running, then terminates it. You can create a new sandbox afterwards, for example after changing connection settings.</p>' },
};

// Run it for me: the dialog shows the exact SQL, and the request carries its
// hash, so the server (and keys.sh in the sandbox) run that SQL or nothing.
function keysConfirm(action, plan) {
  if (action === 'add_keys') {
    return { wide: true, expect: plan.add_sha, title: 'Add replication keys on Heroku',
      body: `<p>Runs this SQL on Heroku with the migration's credentials. It adds a column to each table and fills it
        in batches, then builds a unique index without locking the table. Each statement and its result appear under
        Activity; preflight runs again afterwards.</p><pre class="sql">${esc(plan.add_sql)}</pre>` };
  }
  return { wide: true, expect: plan.drop_sha, title: 'Remove replication keys',
    body: `<p>Runs this SQL on Heroku, then on Render, where the app now writes. It drops each index concurrently,
      then its column, which takes a brief exclusive lock.</p><pre class="sql">${esc(plan.drop_sql)}</pre>` };
}

async function runAction(id, action, opts = {}) {
  const force = !!opts.force && action === 'teardown';
  const keys = ['add_keys', 'drop_keys'].includes(action);
  const c = keys ? keysConfirm(action, (lastDetail && lastDetail.keys_plan) || {}) : CONFIRM[force ? 'teardown_force' : action];
  let confirmValue = '';
  if (c) {
    const ok = await confirmDialog(c.title, c.body, id, c.wide);
    if (!ok) return;
    confirmValue = id;
  }
  const body = force ? { confirm: confirmValue, force: true } : { confirm: confirmValue };
  if (keys) body.expect = c.expect;
  try {
    const job = await api('POST', `/api/migrations/${id}/actions/${action}`, body);
    selectedJob = job.id;
    logState = { job: null, offset: 0 };
    toast(`Started: ${job.label}`);
    const fresh = await api('GET', `/api/migrations/${id}`);
    if ($('#d-steps')) drawDetail(id, fresh);
  } catch (e) { toast(e.message, 'bad'); }
}

function confirmDialog(title, bodyHTML, expect, wide = false) {
  const dlg = $('#confirm-dialog');
  dlg.classList.toggle('wide', !!wide);
  $('#confirm-title').textContent = title;
  $('#confirm-body').innerHTML = bodyHTML;
  $('#confirm-expect').textContent = expect;
  const input = $('#confirm-input');
  const ok = $('#confirm-ok');
  input.value = '';
  const checks = [...dlg.querySelectorAll('#confirm-body input[type=checkbox]')];
  const update = () => { ok.disabled = input.value !== expect || checks.some((c) => !c.checked); };
  input.oninput = update; checks.forEach((c) => { c.onchange = update; });
  update();
  return new Promise((resolve) => {
    dlg.onclose = () => resolve(dlg.returnValue === 'ok' && input.value === expect && !checks.some((c) => !c.checked));
    dlg.returnValue = '';
    dlg.showModal();
    input.focus();
  });
}

async function deleteMigration(id) {
  const ok = await confirmDialog('Delete migration', '<p>Removes this migration\'s directory, including its stored connection strings, job logs and downloaded artifacts. The databases are not touched.</p>', id);
  if (!ok) return;
  try { await api('DELETE', `/api/migrations/${id}`, { confirm: id }); location.hash = '#/'; } catch (e) { toast(e.message, 'bad'); }
}

function editSettings(id) {
  const v = lastDetail;
  const plans = (system && system.plans) || [];
  $('#d-settings').innerHTML = `<form class="form" id="edit-form" novalidate autocomplete="off">
    <label class="field"><span class="label">Title</span><input class="input" name="title" value="${esc(v.title)}"></label>
    ${secretField('heroku_url', 'Heroku database URL', 'leave blank to keep the current value', '')}
    ${secretField('render_url', 'Render database URL (external)', 'leave blank to keep the current value', '')}
    ${secretField('render_superuser_url', 'Render superuser URL (optional)', 'leave blank to keep the current value', SUPERUSER_HINT)}
    <label class="field"><span class="label">Schemas</span><input class="input mono" name="schemas" value="${esc(v.schemas)}"><span class="err" data-err="schemas"></span></label>
    <label class="field"><span class="label">Sync name</span><input class="input mono" name="sync_name" value="${esc(v.sync_name)}"><span class="err" data-err="sync_name"></span></label>
    <label class="field"><span class="label">Plan</span><select class="input" name="plan">${plans.map((p) => `<option ${p === v.plan ? 'selected' : ''}>${esc(p)}</option>`).join('')}</select></label>
    ${secretField('slack_webhook_url', 'Slack webhook', 'leave blank to keep the current value', '')}
    <div class="btn-row"><button class="btn btn-primary btn-sm" type="submit">Save</button><button class="btn btn-ghost btn-sm" type="button" id="edit-cancel">Cancel</button></div>
    <div class="err" id="edit-err"></div></form>`;
  wireSecretToggles($('#d-settings'));
  $('#edit-cancel').addEventListener('click', () => drawSettings(lastDetail));
  $('#edit-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const body = Object.fromEntries([...new FormData(ev.target).entries()].map(([k, val]) => [k, String(val).trim()]));
    try { const nv = await api('PATCH', `/api/migrations/${id}`, body); drawDetail(id, nv); toast('Saved'); } catch (e) {
      const fields = (e.data && e.data.fields) || {};
      Object.entries(fields).forEach(([k, msg]) => { const slot = document.querySelector(`#edit-form [data-err="${k}"]`); if (slot) slot.textContent = msg; });
      $('#edit-err').textContent = Object.keys(fields).length ? '' : e.message;
    }
  });
}

// ------------------------------------------------------------- image view
let globalLog = { job: null, offset: 0 };
let globalSelected = null;
async function renderImage() {
  app.innerHTML = `
    <div class="page-head"><div class="titles"><h1>Image &amp; settings</h1>
      <p class="muted">One image for every migration, built in a Render sandbox and pushed to your GitHub registry.</p></div>
      <button class="btn btn-ghost" id="env-refresh" type="button">Recheck</button></div>
    <div class="grid-detail">
      <div class="stack">
        <div class="card"><div class="card-head"><h2>Settings</h2></div><div class="card-body" id="img-checks"></div></div>
        <div class="card"><div class="card-head"><h2>Configuration</h2></div><div class="card-body" id="img-config"></div></div>
        <div class="card"><div class="card-head"><h2>Brokered credential</h2></div><div class="card-body" id="img-cred"></div></div>
      </div>
      <div class="stack">
        <div class="card"><div class="card-head"><h2>Image</h2></div><div class="card-body" id="img-image"></div></div>
        <div class="card"><div class="card-head"><h2>Image jobs</h2></div>
          <div class="card-body flush"><table class="jobs-list"><tbody id="env-jobs"></tbody></table><pre class="log" id="env-log"></pre></div></div>
        <div class="card"><div class="card-head"><h2>Builds</h2></div><div class="card-body flush" id="img-list"></div></div>
      </div>
    </div>`;
  $('#env-refresh').addEventListener('click', async () => { await loadSystem(true); drawImage(); });
  globalLog = { job: null, offset: 0 };
  const gen = routeGen;
  await loadSystem(true);
  if (stale(gen)) return;
  drawImage();
  every(4000, async () => { await loadSystem(false); drawImage(); });
  every(1500, tailGlobal);
}

function imageChip(state) {
  const cls = state === 'ready' ? 'ok' : state === 'failed' ? 'bad' : '';
  return `<span class="chip ${cls}">${esc(state || 'none')}</span>`;
}

async function drawImage() {
  if (!$('#img-checks')) return;
  if (!system) { $('#img-checks').innerHTML = '<div class="notice bad">Could not load settings.</div>'; return; }
  const c = system.config || {};
  $('#img-checks').innerHTML = (system.checks || []).map((k) => `<div class="btn-row">
      <span class="chip ${k.ok ? 'ok' : 'bad'}"><span class="dot ${k.ok ? 'ok' : 'bad'}"></span>${esc(k.name)}</span>
      <span class="muted">${esc(k.detail)}</span></div>`).join('')
    + '<p class="hint">Set these as environment variables on this web service in the Render Dashboard. The token is used for the brokered credential and for importing the image; it is never sent to a sandbox.</p>';
  $('#img-config').innerHTML = `<dl class="kv">
    <dt>Registry</dt><dd class="mono">${esc(c.GITHUB_REGISTRY || '–')}</dd>
    <dt>Image name</dt><dd class="mono">${esc(c.IMAGE_NAME)}</dd>
    <dt>Postgres versions</dt><dd class="mono">${esc((c.PG_CLIENT_MAJORS || '').split(' ').join(', '))}</dd>
    <dt>Control database</dt><dd class="mono">Postgres ${esc(c.PG_MAJOR)}</dd>
    <dt>Migration group</dt><dd class="mono">${esc(c.SANDBOX_GROUP)}</dd>
    <dt>Idle suspend</dt><dd>${c.SANDBOX_IDLE_TIMEOUT_SECONDS ? `after ${esc(minutes(c.SANDBOX_IDLE_TIMEOUT_SECONDS))} without a step <span class="faint">(new sandboxes)</span>` : 'never'}</dd>
    <dt>Build group</dt><dd class="mono">${esc(c.BUILD_SANDBOX_GROUP)} <span class="faint">(${esc(c.BUILD_SANDBOX_PLAN)})</span></dd>
    <dt>Build network</dt><dd class="mono">${esc(c.BUILD_NETWORK_POLICY)}</dd>
    <dt>BuildKit</dt><dd class="mono">${esc(c.BUILDKIT_VERSION)}</dd>
    <dt>Base image</dt><dd class="mono">${esc(c.BASE_IMAGE)}</dd></dl>`;
  const cred = system.credential;
  $('#img-cred').innerHTML = cred ? `<dl class="kv">
      <dt>ID</dt><dd class="mono">${esc(cred.id)}</dd>
      <dt>Group</dt><dd class="mono">${esc(cred.group)}</dd>
      <dt>Applies to</dt><dd class="mono">https://${esc(cred.domain)}${esc((cred.paths || []).join(', '))}</dd>
      <dt>Injection</dt><dd>HTTP ${esc(cred.injection)} auth (<span class="mono">${esc(system.config.REGISTRY_HOST ? 'user:token' : '')}</span>)</dd>
      <dt>Version</dt><dd>${esc(cred.version ?? '–')} ${cred.updated_at ? `<span class="faint">${esc(ago(cred.updated_at))}</span>` : ''}</dd>
      <dt>Enabled</dt><dd>${cred.enabled ? 'Yes' : 'No'}</dd></dl>
      <p class="hint">Render adds the token to the build sandbox's requests for the registry's token endpoint only. Code in the sandbox never sees it.</p>`
    : `<p class="muted">Created on the first build: <span class="mono">${esc(c.CREDENTIAL_ID)}</span> in group <span class="mono">${esc(c.BUILD_SANDBOX_GROUP)}</span>,
       adding HTTP Basic auth to <span class="mono">https://${esc(c.REGISTRY_HOST || '<registry>')}/token</span>.</p>`;

  const a = system.app || {};
  const images = system.images || [];
  const cur = images.find((i) => i.tag === a.image_tag);
  const busy = !!system.active_global_job;
  const latest = system.latest_image;
  const canImport = cur && cur.digest && cur.state !== 'ready';
  $('#img-image').innerHTML = `<dl class="kv">
      <dt>This deploy</dt><dd class="mono">${esc(a.git_sha)}</dd>
      <dt>Builds as</dt><dd class="mono">${esc(a.image_ref || 'set GITHUB_REGISTRY')}</dd>
      <dt>State</dt><dd>${imageChip(cur && cur.state)} ${cur && cur.detail ? `<span class="muted">${esc(cur.detail)}</span>` : ''}</dd>
      <dt>New sandboxes use</dt><dd class="mono">${latest ? `${esc(latest.tag)} → ${esc(latest.image_id)}` : 'nothing yet'}</dd></dl>
    <div class="btn-row">
      <button class="btn btn-sm ${cur && cur.state === 'ready' ? 'btn-secondary' : 'btn-primary'}" data-g="build_image" ${busy || !system.build_ready ? 'disabled' : ''}>Build &amp; push image</button>
      <button class="btn btn-sm btn-secondary" data-g="import_image" ${busy || !canImport ? 'disabled' : ''}>Import into sandbox group</button>
    </div>
    <p class="hint">The build runs in a sandbox started from Render's default image, uploads this deploy's <code>image/</code> and <code>sandbox/</code>,
      builds with BuildKit, pushes to the registry, then imports the image into the migration sandbox group. Running sandboxes keep the image they started with.</p>
    ${system.build_ready ? '' : '<div class="notice warn">Set the missing settings on the left before building.</div>'}`;
  $('#img-image').querySelectorAll('[data-g]').forEach((b) => b.addEventListener('click', async () => {
    try {
      const body = b.dataset.g === 'import_image' && cur ? { tag: cur.tag } : {};
      const j = await api('POST', `/api/global/actions/${b.dataset.g}`, body);
      globalSelected = j.id; toast(`Started: ${j.label}`);
    } catch (e) { toast(e.message, 'bad'); }
  }));
  $('#img-list').innerHTML = images.length ? `<table><thead><tr><th>Tag</th><th>State</th><th>Digest</th><th>Render image</th><th>Updated</th></tr></thead><tbody>
      ${images.map((i) => `<tr><td class="mono">${esc(i.tag)}</td><td>${imageChip(i.state)}</td>
        <td class="mono">${esc(i.digest ? i.digest.slice(0, 19) : '–')}</td><td class="mono">${esc(i.image_id || '–')}</td>
        <td class="faint">${esc(ago(i.updated_at))}</td></tr>`).join('')}</tbody></table>`
    : '<p class="card-body muted">No builds yet.</p>';

  let jobs = [];
  try { jobs = (await api('GET', '/api/global/jobs')).jobs || []; } catch { /* ignore */ }
  if (!$('#env-jobs')) return;
  if (!globalSelected && jobs.length) globalSelected = jobs[0].id;
  $('#env-jobs').innerHTML = jobs.length ? jobs.slice(0, 6).map((j) => `<tr class="clickable ${j.id === globalSelected ? 'selected' : ''}" data-gjob="${esc(j.id)}">
      <td>${esc(j.label)}</td><td>${jobChip(j)}</td><td class="faint">${esc(j.started_by || '')}</td><td class="faint">${esc(ago(j.started_at))}</td><td class="faint r num">${esc(duration(j.started_at, j.finished_at))}</td></tr>`).join('')
    : '<tr><td class="faint">No image jobs yet.</td></tr>';
  $('#env-jobs').querySelectorAll('[data-gjob]').forEach((tr) => tr.addEventListener('click', () => { globalSelected = tr.dataset.gjob; drawImage(); tailGlobal(); }));
}

async function tailGlobal() {
  const pre = $('#env-log');
  if (!pre || !globalSelected) return;
  if (globalLog.job !== globalSelected) { globalLog = { job: globalSelected, offset: 0 }; pre.textContent = ''; }
  try {
    const want = globalLog.job;
    const res = await api('GET', `/api/global/jobs/${want}/log?offset=${globalLog.offset}`);
    if (globalLog.job !== want) return;
    if (res.text) { const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24; appendLog(pre, res.text); if (atBottom) pre.scrollTop = pre.scrollHeight; }
    globalLog.offset = res.next_offset;
  } catch { /* ignore */ }
}

// ------------------------------------------------------------------ toast
let toastTimer = null;
function toast(msg, kind) {
  let el = $('#toast');
  if (!el) { el = document.createElement('div'); el.id = 'toast'; el.className = 'toast'; document.body.appendChild(el); }
  el.innerHTML = `<div class="notice ${kind === 'bad' ? 'bad' : ''}" role="status">${esc(msg)}</div>`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.innerHTML = ''; }, 5000);
}

// ------------------------------------------------------------------- boot
async function loadMe() {
  try {
    const me = await api('GET', '/api/me');
    $('#user-chip').textContent = me.authenticated ? (me.name || me.user || '') : 'authentication off (dev)';
    if (me.user) $('#user-chip').title = `Signed in as ${me.user}`;
  } catch { /* the request guard already answered */ }
}
loadMe();
loadSystem(false).then(route);
