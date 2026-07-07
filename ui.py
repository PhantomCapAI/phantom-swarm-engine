"""Minimal self-contained web UI for the Automated AI Bundler.

Served at GET /bundle/ui. No build step, no external assets — one HTML string
with inline CSS/JS. It POSTs to /bundle/create (sending the X-Phantom-Internal
header) and renders the live SSE stream from /bundle/stream/{id}, then surfaces
a download link when the job completes.
"""

BUNDLER_UI = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Phantom Bundler</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; font: 15px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
    background: #0b0d10; color: #e6e6e6;
  }
  header { padding: 20px 24px; border-bottom: 1px solid #1c2128; }
  h1 { margin: 0; font-size: 18px; letter-spacing: .5px; color: #D4A853; }
  .sub { color: #7d8590; font-size: 13px; margin-top: 4px; }
  main { max-width: 900px; margin: 0 auto; padding: 24px; }
  label { display: block; font-size: 12px; color: #7d8590; margin: 14px 0 6px; text-transform: uppercase; letter-spacing: .5px; }
  textarea, input, select {
    width: 100%; background: #12151a; border: 1px solid #262c36; color: #e6e6e6;
    border-radius: 8px; padding: 10px 12px; font: inherit;
  }
  textarea { min-height: 90px; resize: vertical; }
  .row { display: flex; gap: 12px; }
  .row > div { flex: 1; }
  button {
    margin-top: 16px; background: #D4A853; color: #0b0d10; border: 0;
    border-radius: 8px; padding: 11px 20px; font: inherit; font-weight: 700;
    cursor: pointer;
  }
  button:disabled { opacity: .5; cursor: default; }
  #status { margin: 18px 0 8px; font-size: 13px; color: #7d8590; }
  #feed { display: flex; flex-direction: column; gap: 8px; }
  .msg { background: #12151a; border-left: 3px solid #444; border-radius: 6px; padding: 8px 12px; }
  .msg .who { font-weight: 700; }
  .msg .phase { font-size: 11px; color: #7d8590; float: right; text-transform: uppercase; letter-spacing: .5px; }
  .msg .txt { margin-top: 3px; white-space: pre-wrap; }
  .msg.decision { background: #16130c; }
  .msg.consensus { background: #0f1a12; border-left-color: #7ECFB3; }
  .msg.tool_call { opacity: .85; }
  #done { margin-top: 20px; }
  #done a {
    display: inline-block; background: #1f6feb; color: #fff; text-decoration: none;
    padding: 11px 20px; border-radius: 8px; font-weight: 700;
  }
  .err { color: #ff7b72; }
</style>
</head>
<body>
<header>
  <h1>◆ PHANTOM BUNDLER</h1>
  <div class="sub">20-agent hive mind → drop-in AI bundle</div>
</header>
<main>
  <label for="spec">Spec (natural language or JSON)</label>
  <textarea id="spec" placeholder="A 3-agent code-review swarm: a linter, a security auditor, and a summarizer. Targets: Claude Code and Cursor."></textarea>
  <div class="row">
    <div>
      <label for="mode">Mode</label>
      <select id="mode">
        <option value="full">Full — choose crew size (richest)</option>
        <option value="lite">Lite — 5 essential agents (faster/cheaper)</option>
      </select>
    </div>
    <div>
      <label for="size">Agents (full mode)</label>
      <input id="size" type="number" min="5" max="20" step="1" value="20"/>
    </div>
    <div>
      <label for="secret">X-Phantom-Internal secret</label>
      <input id="secret" type="password" placeholder="(leave blank if auth disabled)"/>
    </div>
  </div>
  <div id="cryptobox" style="display:none; margin-top:14px; background:#12151a; border:1px solid #262c36; border-radius:8px; padding:12px;">
    <div style="font-size:12px; color:#7d8590; text-transform:uppercase; letter-spacing:.5px;">Pay with crypto</div>
    <div style="margin-top:6px;">Send <b id="c_amount"></b> to:</div>
    <code id="c_wallet" style="display:block; margin:6px 0; padding:8px; background:#0b0d10; border-radius:6px; word-break:break-all; color:#7ECFB3;"></code>
    <label for="txsig">Transaction signature</label>
    <input id="txsig" placeholder="paste the tx signature after paying"/>
  </div>

  <label>Targets</label>
  <div id="targets" style="display:flex; flex-wrap:wrap; gap:14px; font-size:13px;">
    <label style="display:flex; align-items:center; gap:6px; text-transform:none; letter-spacing:0; margin:0; color:#e6e6e6;"><input type="checkbox" value="claude-code" checked style="width:auto;"/> Claude Code</label>
    <label style="display:flex; align-items:center; gap:6px; text-transform:none; letter-spacing:0; margin:0; color:#e6e6e6;"><input type="checkbox" value="cursor" checked style="width:auto;"/> Cursor</label>
    <label style="display:flex; align-items:center; gap:6px; text-transform:none; letter-spacing:0; margin:0; color:#e6e6e6;"><input type="checkbox" value="windsurf" checked style="width:auto;"/> Windsurf</label>
    <label style="display:flex; align-items:center; gap:6px; text-transform:none; letter-spacing:0; margin:0; color:#e6e6e6;"><input type="checkbox" value="config" checked style="width:auto;"/> Config</label>
    <label style="display:flex; align-items:center; gap:6px; text-transform:none; letter-spacing:0; margin:0; color:#e6e6e6;"><input type="checkbox" value="langgraph" style="width:auto;"/> LangGraph</label>
  </div>

  <button id="go">Create Bundle</button>
  <span id="price" style="margin-left:12px;color:#7d8590;font-size:13px;"></span>

  <div id="status"></div>
  <div id="feed"></div>
  <div id="done"></div>

  <section id="recent" style="margin-top:32px;">
    <div style="display:flex; align-items:center; gap:10px;">
      <h2 style="font-size:14px; color:#7d8590; text-transform:uppercase; letter-spacing:.5px; margin:0;">Recent bundles</h2>
      <button id="refresh" style="margin:0; padding:4px 10px; font-size:12px; background:#12151a; color:#7d8590; border:1px solid #262c36;">↻</button>
    </div>
    <div id="recentlist" style="margin-top:10px;"></div>
  </section>

  <section id="viewer" style="display:none; margin-top:24px;">
    <div style="display:flex; align-items:center; gap:10px;">
      <h2 style="font-size:14px; color:#7d8590; text-transform:uppercase; letter-spacing:.5px; margin:0;">Files — <span id="viewer_name"></span></h2>
      <button id="viewer_close" style="margin:0; padding:4px 10px; font-size:12px; background:#12151a; color:#7d8590; border:1px solid #262c36;">close</button>
    </div>
    <div style="display:flex; gap:12px; margin-top:10px;">
      <div id="filetree" style="flex:0 0 260px; max-height:420px; overflow:auto; background:#12151a; border:1px solid #262c36; border-radius:8px; padding:8px;"></div>
      <pre id="filebody" style="flex:1; max-height:420px; overflow:auto; background:#0b0d10; border:1px solid #262c36; border-radius:8px; padding:12px; margin:0; white-space:pre; font-size:12px;"></pre>
    </div>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
const seen = new Set();

function addMsg(m) {
  if (m.id && seen.has(m.id)) return;
  if (m.id) seen.add(m.id);
  const el = document.createElement('div');
  el.className = 'msg ' + (m.type || 'message');
  el.style.borderLeftColor = m.color || '#444';
  el.innerHTML =
    '<span class="phase"></span>' +
    '<span class="who"></span> <span class="role"></span>' +
    '<div class="txt"></div>';
  el.querySelector('.phase').textContent = m.phase || '';
  el.querySelector('.who').textContent = m.agent || '';
  el.querySelector('.who').style.color = m.color || '#e6e6e6';
  el.querySelector('.role').textContent = m.role ? '· ' + m.role : '';
  el.querySelector('.role').style.color = '#7d8590';
  el.querySelector('.txt').textContent = m.text || '';
  $('feed').appendChild(el);
  window.scrollTo(0, document.body.scrollHeight);
}

let PRICING = { enabled: false };

async function loadPricing() {
  try {
    PRICING = await (await fetch('/bundle/pricing')).json();
  } catch (e) { PRICING = { enabled: false }; }
  if (!PRICING.enabled) return;

  const opts = (PRICING.options && PRICING.options.length)
    ? PRICING.options : [{ price: PRICING.price, asset: PRICING.asset }];
  const amounts = opts.map(o => o.price + ' ' + o.asset).join(' or ');
  $('price').textContent = 'Price: ' + amounts + ' (' + PRICING.network + ')';
  $('c_amount').textContent = amounts;
  $('c_wallet').textContent = PRICING.pay_to;
  $('cryptobox').style.display = 'block';
  $('go').textContent = "I've Paid — Create";
}

// Entry point: pay with crypto (paste tx signature) or create directly.
async function start() {
  const spec = $('spec').value.trim();
  if (!spec) { $('status').innerHTML = '<span class="err">Enter a spec.</span>'; return; }

  const hasSecret = $('secret').value.trim().length > 0;

  // Paywall on, no admin secret: user pays from their wallet, pastes the tx sig.
  if (PRICING.enabled && !hasSecret) {
    const tx = $('txsig').value.trim();
    if (!tx) {
      $('status').innerHTML = '<span class="err">Pay the amount shown to the wallet above, ' +
        'then paste the transaction signature.</span>';
      return;
    }
    run(spec, { tx });
    return;
  }

  run(spec, {});
}

async function run(spec, opts) {
  opts = opts || {};
  $('go').disabled = true;
  $('feed').innerHTML = ''; $('done').innerHTML = ''; seen.clear();
  $('status').textContent = 'Starting job…';

  const headers = { 'Content-Type': 'application/json' };
  if ($('secret').value) headers['X-Phantom-Internal'] = $('secret').value;
  if (opts.tx) headers['X-Payment-Tx'] = opts.tx;

  const payload = { spec, mode: $('mode').value };
  if (payload.mode === 'full') payload.agents = parseInt($('size').value, 10) || 20;
  const targets = Array.from(document.querySelectorAll('#targets input:checked')).map(c => c.value);
  if (targets.length) payload.targets = targets;

  let res, data;
  try {
    res = await fetch('/bundle/create', {
      method: 'POST', headers, body: JSON.stringify(payload)
    });
    data = await res.json();
  } catch (e) {
    $('status').innerHTML = '<span class="err">Request failed: ' + e + '</span>';
    $('go').disabled = false; return;
  }
  if (!res.ok) {
    const msg = res.status === 402 ? 'Payment required.' : (data.error || res.status);
    $('status').innerHTML = '<span class="err">' + msg + '</span>';
    $('go').disabled = false; return;
  }

  const sid = data.session_id;
  $('status').textContent = 'Session ' + sid + ' — hive deliberating…';
  const es = new EventSource('/bundle/stream/' + sid);

  es.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === 'ping') return;
    addMsg(m);
    if (m.type === 'consensus' || m.phase === 'package') {
      es.close();
      $('status').textContent = 'Bundle ready.';
      $('done').innerHTML = '<a href="/bundle/' + sid + '/download">⬇ Download ' + sid + '.zip</a>' +
        ' <a href="#" id="viewnow" style="margin-left:14px; color:#7B8CDE; text-decoration:none;">view files</a>';
      const vn = document.getElementById('viewnow');
      if (vn) vn.addEventListener('click', (e) => { e.preventDefault(); openViewer(sid, ''); });
      $('go').disabled = false;
      loadRecent();
    } else if (m.phase === 'error') {
      es.close();
      $('status').innerHTML = '<span class="err">Job failed.</span>';
      $('go').disabled = false;
    }
  };
  es.onerror = () => { es.close(); $('go').disabled = false; };
}

async function loadRecent() {
  let data;
  try {
    data = await (await fetch('/bundle/list')).json();
  } catch (e) { return; }
  const bundles = (data && data.bundles) || [];
  if (!bundles.length) {
    $('recentlist').innerHTML = '<div style="color:#7d8590; font-size:13px;">No bundles yet.</div>';
    return;
  }
  $('recentlist').innerHTML = bundles.map(b => {
    const when = (b.saved_at || '').replace('T', ' ').slice(0, 16);
    const name = (b.name || 'bundle');
    return '<div style="display:flex; justify-content:space-between; align-items:center; ' +
      'background:#12151a; border:1px solid #262c36; border-radius:6px; padding:8px 12px; margin-bottom:6px;">' +
      '<div><b>' + name + '</b> <span style="color:#7d8590;">v' + (b.version || '?') +
      ' · ' + (b.file_count || 0) + ' files · ' + when + '</span></div>' +
      '<div style="display:flex; gap:12px;">' +
      '<a href="#" data-view="' + b.session_id + '" data-name="' + name + '" style="color:#7B8CDE;">view</a>' +
      '<a href="/bundle/' + b.session_id + '/download" style="color:#7ECFB3;">⬇ zip</a>' +
      '<a href="#" data-del="' + b.session_id + '" style="color:#ff7b72;">🗑</a></div></div>';
  }).join('');
  document.querySelectorAll('#recentlist a[data-view]').forEach(a =>
    a.addEventListener('click', (e) => {
      e.preventDefault();
      openViewer(a.getAttribute('data-view'), a.getAttribute('data-name'));
    }));
  document.querySelectorAll('#recentlist a[data-del]').forEach(a =>
    a.addEventListener('click', (e) => {
      e.preventDefault();
      deleteBundle(a.getAttribute('data-del'));
    }));
}

async function deleteBundle(sid) {
  if (!confirm('Delete bundle ' + sid + '? This cannot be undone.')) return;
  const headers = {};
  if ($('secret').value) headers['X-Phantom-Internal'] = $('secret').value;
  let res;
  try {
    res = await fetch('/bundle/' + sid, { method: 'DELETE', headers });
  } catch (e) { alert('Delete failed: ' + e); return; }
  if (res.status === 403) { alert('Unauthorized — enter the admin secret to delete.'); return; }
  if (!res.ok && res.status !== 404) { alert('Delete failed (' + res.status + ').'); return; }
  loadRecent();
}

// Inline file viewer — fetches the manifest (path -> content) and renders a
// tree + content pane. No unzip needed.
async function openViewer(sid, name) {
  $('viewer').style.display = 'block';
  $('viewer_name').textContent = name || sid;
  $('filetree').innerHTML = 'Loading…';
  $('filebody').textContent = '';
  let data;
  try {
    data = await (await fetch('/bundle/' + sid + '/download?format=manifest')).json();
  } catch (e) { $('filetree').textContent = 'Failed to load.'; return; }
  const files = (data && data.files) || {};
  const paths = Object.keys(files).sort();
  $('filetree').innerHTML = paths.map(p =>
    '<div class="fitem" data-path="' + encodeURIComponent(p) + '" ' +
    'style="cursor:pointer; padding:3px 4px; border-radius:4px; font-size:12px; color:#c9d1d9; word-break:break-all;">' +
    p + '</div>').join('');
  $('viewer').scrollIntoView({ behavior: 'smooth' });
  document.querySelectorAll('#filetree .fitem').forEach(el =>
    el.addEventListener('click', () => {
      const p = decodeURIComponent(el.getAttribute('data-path'));
      $('filebody').textContent = files[p];
      document.querySelectorAll('#filetree .fitem').forEach(x => x.style.background = 'transparent');
      el.style.background = '#1c2230';
    }));
  if (paths.length) $('filetree').querySelector('.fitem').click();  // preview first
}

$('go').addEventListener('click', start);
$('refresh').addEventListener('click', loadRecent);
$('viewer_close').addEventListener('click', () => { $('viewer').style.display = 'none'; });

// On load: pricing (shows wallet/amount when paywall on) + recent bundles.
loadPricing();
loadRecent();
</script>
</body>
</html>"""
