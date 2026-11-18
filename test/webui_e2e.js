// Browser test for the web UI. Run through test/webui_e2e.sh.
const { chromium } = require('playwright');
const { execFileSync } = require('child_process');

const URL = process.env.UI_URL;
const SHOTS = process.env.SHOTS;
const HEROKU = 'postgres://app:app%3Apass@source:5432/app';
const RENDER = 'postgresql://migrator:migrator-pass@target:5432/app';
const RENDER_NOGRANT = 'postgresql://migrator:migrator-pass@target_nogrant:5432/app';

function srcSQL(sql) {
  execFileSync('docker', ['compose', '-f', 'test/docker-compose.yml', 'exec', '-T', '-e', 'PGPASSWORD=app:pass', 'source',
    'psql', '-X', '-q', '-v', 'ON_ERROR_STOP=1', '-h', '127.0.0.1', '-U', 'app', '-d', 'app', '-c', sql], { stdio: 'inherit' });
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function api(path) { const r = await fetch(URL + path); return r.json(); }
async function waitFor(desc, fn, timeoutMs = 180000) {
  const end = Date.now() + timeoutMs;
  for (;;) {
    const v = await fn();
    if (v) return v;
    if (Date.now() > end) throw new Error(`timed out waiting for ${desc}`);
    await sleep(1000);
  }
}
async function waitJob(id, action) {
  return waitFor(`${id}/${action} to finish`, async () => {
    const v = await api(`/api/migrations/${id}`);
    const j = (v.jobs || []).find((x) => x.action === action);
    return j && j.state !== 'running' ? j : null;
  }, 300000);
}
async function shot(page, name) { await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true }); console.log(`  screenshot ${name}`); }
async function click(page, action) {
  const b = page.locator(`[data-action="${action}"]`).first();
  await b.waitFor({ state: 'visible' });
  await waitFor(`${action} button enabled`, async () => !(await b.isDisabled()), 120000);
  await b.click();
}
async function confirmIn(page, name) {
  const dlg = page.locator('#confirm-dialog');
  await dlg.waitFor({ state: 'visible' });
  for (const box of await dlg.locator('input[type=checkbox]').all()) await box.check();
  await page.fill('#confirm-input', name);
  await page.click('#confirm-ok');
}
async function create(page, id, title, heroku, render) {
  await page.goto(`${URL}/#/new`);
  await page.fill('[name=id]', id);
  await page.fill('[name=title]', title);
  await page.fill('[name=heroku_url]', heroku);
  await page.fill('[name=render_url]', render);
  await page.click('summary');
  await page.fill('[name=schemas]', 'public,billing');
  await page.click('button[type=submit]');
  await page.waitForURL(`**/#/m/${id}`);
}

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 2 });
  page.on('pageerror', (e) => { console.error('page error:', e); process.exitCode = 1; });

  console.log('environment: import image');
  await page.goto(`${URL}/#/environment`);
  await page.click('[data-g="import_image"]');
  await waitFor('image import', async () => ((await api('/api/system?fresh=1')).images || {}).latest === 'test');
  await page.reload();
  await page.waitForSelector('#env-jobs tr[data-gjob]');
  await shot(page, '01-environment');

  console.log('create migrations');
  await create(page, 'alpha', 'Alpha production', HEROKU, RENDER);
  // Validation: a duplicate name and an internal Render URL are refused.
  await page.goto(`${URL}/#/new`);
  await page.fill('[name=id]', 'alpha');
  await page.fill('[name=heroku_url]', HEROKU);
  await page.fill('[name=render_url]', 'postgresql://u:p@dpg-abc123-a/app');
  await page.click('button[type=submit]');
  await page.waitForSelector('[data-err="render_url"]:not(:empty)');
  await shot(page, '02-new-migration-validation');
  await create(page, 'beta', 'Beta staging', HEROKU, RENDER_NOGRANT);

  console.log('beta: sandbox + preflight (expected to block)');
  await click(page, 'create_sandbox');
  await waitJob('beta', 'create_sandbox');
  await click(page, 'preflight');
  const pb = await waitJob('beta', 'preflight');
  if (pb.state !== 'failed') throw new Error('beta preflight should fail');

  console.log('alpha: sandbox');
  await page.goto(`${URL}/#/m/alpha`);
  await click(page, 'create_sandbox');
  await waitJob('alpha', 'create_sandbox');
  console.log('alpha: preflight (blocked by audit_log), fix, rerun');
  await click(page, 'preflight');
  await waitJob('alpha', 'preflight');
  await page.waitForSelector('#d-preflight .notice.bad');
  await page.waitForFunction(() => document.querySelector('#d-log').textContent.includes('BLOCKER: tables without a primary key'));
  if (!(await page.locator('[data-action="schema"]').isDisabled())) throw new Error('schema enabled despite blocked preflight');
  await shot(page, '03-alpha-preflight-blocked');
  srcSQL('ALTER TABLE public.audit_log ADD COLUMN id bigserial PRIMARY KEY');
  await sleep(1100);
  await click(page, 'preflight');
  await waitFor('alpha preflight pass', async () => {
    const v = await api('/api/migrations/alpha'); return v.preflight && v.preflight.passed;
  });

  console.log('alpha: schema, sync');
  await click(page, 'schema');
  await waitJob('alpha', 'schema');
  await click(page, 'start_sync');
  await waitJob('alpha', 'start_sync');
  await waitFor('phase copied', async () => {
    const v = await api('/api/migrations/alpha');
    const s = v.status && v.status.report && v.status.report.status;
    return s && ['copied', 'replicating'].includes(s.phase);
  });
  await page.reload();
  await page.waitForSelector('#d-tables table');
  await shot(page, '04-alpha-copied');

  console.log('alpha: indexes');
  await click(page, 'build_indexes');
  await waitJob('alpha', 'build_indexes');
  await waitFor('phase replicating', async () => {
    const v = await api('/api/migrations/alpha');
    const s = v.status && v.status.report && v.status.report.status;
    return s && s.phase === 'replicating';
  });
  await page.reload();
  await page.waitForSelector('[data-action="cutover"]:not([disabled])');
  await shot(page, '05-alpha-replicating');

  console.log('alpha: cutover');
  await click(page, 'cutover');
  await page.locator('#confirm-dialog').waitFor({ state: 'visible' });
  await shot(page, '06-cutover-confirm');
  await confirmIn(page, 'alpha');
  const cj = await waitJob('alpha', 'cutover');
  if (cj.state !== 'succeeded') throw new Error('cutover failed');

  console.log('alpha: cleanup + teardown');
  await waitFor('cutover marker', async () => {
    const v = await api('/api/migrations/alpha');
    const s = v.status && v.status.report && v.status.report.status;
    return s && s.markers && s.markers['cutover.done'];
  });
  await page.reload();
  await click(page, 'cleanup_source');
  await confirmIn(page, 'alpha');
  await waitJob('alpha', 'cleanup_source');
  await waitFor('cleanup marker', async () => {
    const v = await api('/api/migrations/alpha');
    const s = v.status && v.status.report && v.status.report.status;
    return s && s.markers && s.markers['cleanup_source.done'];
  });
  await page.reload();
  await click(page, 'teardown');
  await confirmIn(page, 'alpha');
  const tj = await waitJob('alpha', 'teardown');
  if (tj.state !== 'succeeded') throw new Error('teardown failed');
  await page.reload();
  await page.waitForSelector('#d-next');
  await shot(page, '07-alpha-finished');

  console.log('list + beta detail');
  await page.goto(`${URL}/#/m/beta`);
  await page.waitForSelector('#d-preflight .notice.bad');
  await page.waitForFunction(() => document.querySelector('#d-log').textContent.includes('session_replication_role'));
  if (!(await page.locator('#d-chips').textContent()).includes('Preflight blocked')) throw new Error('beta not shown as blocked');
  await shot(page, '08-beta-blocked');
  await page.goto(`${URL}/#/`);
  await page.waitForSelector('tr[data-id="beta"]');
  await sleep(500);
  await shot(page, '09-list');
  await page.emulateMedia({ colorScheme: 'dark' });
  await sleep(300);
  await shot(page, '10-list-dark');

  // Nothing secret reached the browser.
  const html = await page.content();
  const all = JSON.stringify(await api('/api/migrations')) + html;
  for (const secret of ['migrator-pass', 'app:pass', 'app%3Apass']) {
    if (all.includes(secret)) throw new Error(`secret ${secret} reached the browser`);
  }
  await browser.close();
  console.log('ok');
})().catch((e) => { console.error(e); process.exit(1); });
