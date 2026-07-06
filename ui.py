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
  textarea, input {
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
      <label for="secret">X-Phantom-Internal secret</label>
      <input id="secret" type="password" placeholder="(leave blank if auth disabled)"/>
    </div>
  </div>
  <button id="go">Create Bundle</button>
  <span id="price" style="margin-left:12px;color:#7d8590;font-size:13px;"></span>

  <div id="status"></div>
  <div id="feed"></div>
  <div id="done"></div>
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

// Detect return-from-Stripe (?paid=<checkout_session_id>).
function paidParam() {
  return new URLSearchParams(location.search).get('paid');
}

async function loadPricing() {
  try {
    PRICING = await (await fetch('/bundle/pricing')).json();
  } catch (e) { PRICING = { enabled: false }; }
  if (PRICING.enabled) {
    $('price').textContent = 'Price: ' + PRICING.price + ' ' + (PRICING.currency || 'usd').toUpperCase();
    $('go').textContent = 'Pay ' + PRICING.price + ' ' + (PRICING.currency || 'usd').toUpperCase() + ' & Create';
  }
}

// Entry point: decides between paying (Stripe) and creating directly.
async function start() {
  const spec = $('spec').value.trim();
  if (!spec) { $('status').innerHTML = '<span class="err">Enter a spec.</span>'; return; }

  const paid = paidParam();
  const hasSecret = $('secret').value.trim().length > 0;

  // Paywall on, not yet paid, no admin secret -> send to Stripe Checkout.
  if (PRICING.enabled && !paid && !hasSecret) {
    $('go').disabled = true;
    $('status').textContent = 'Creating secure checkout…';
    try {
      const r = await fetch('/bundle/checkout', { method: 'POST' });
      const d = await r.json();
      if (!r.ok || !d.url) throw new Error(d.error || 'checkout failed');
      localStorage.setItem('phantom_spec', spec);   // survive the redirect
      location.href = d.url;
    } catch (e) {
      $('status').innerHTML = '<span class="err">' + e + '</span>';
      $('go').disabled = false;
    }
    return;
  }

  run(spec, paid);
}

async function run(spec, paid) {
  $('go').disabled = true;
  $('feed').innerHTML = ''; $('done').innerHTML = ''; seen.clear();
  $('status').textContent = 'Starting job…';

  const headers = { 'Content-Type': 'application/json' };
  if ($('secret').value) headers['X-Phantom-Internal'] = $('secret').value;
  if (paid) headers['X-Payment-Session'] = paid;

  let res, data;
  try {
    res = await fetch('/bundle/create', {
      method: 'POST', headers, body: JSON.stringify({ spec })
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
      $('done').innerHTML = '<a href="/bundle/' + sid + '/download">⬇ Download ' + sid + '.zip</a>';
      $('go').disabled = false;
    } else if (m.phase === 'error') {
      es.close();
      $('status').innerHTML = '<span class="err">Job failed.</span>';
      $('go').disabled = false;
    }
  };
  es.onerror = () => { es.close(); $('go').disabled = false; };
}

$('go').addEventListener('click', start);

// On load: fetch pricing, and if we just came back from Stripe (?paid=...),
// restore the spec and auto-run the (now paid) bundle.
(async () => {
  await loadPricing();
  const paid = paidParam();
  if (paid) {
    const saved = localStorage.getItem('phantom_spec');
    if (saved) $('spec').value = saved;
    $('status').textContent = 'Payment received. Building…';
    run($('spec').value.trim(), paid);
  }
})();
</script>
</body>
</html>"""
