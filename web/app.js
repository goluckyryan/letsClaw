/* letsClaw web client.
 *
 * A browser window onto the core, speaking the same WebSocket protocol as
 * chat.py. Deliberately dependency-free: the core often runs on a lab network
 * with no route to a CDN, so a <script src="https://…"> would simply hang.
 *
 * Two things worth knowing before editing:
 *
 *  - Nothing is rendered optimistically. Your own message appears when the core
 *    echoes it back in `turn_start`, so every attached client shows the exact
 *    same transcript in the exact same order.
 *  - Re-rendering markdown on every token is quadratic on a long answer, so
 *    streaming text accumulates into a raw buffer and repaints on a frame timer.
 */

'use strict';

const $ = (s) => document.querySelector(s);

const COMMANDS = {
  '/clear':     'discard the conversation and start over',
  '/new':       'archive the session, keep the objective, start fresh',
  '/model':     'list models, or /model <name> to switch',
  '/info':      'recent history and context usage',
  '/behavior':  'print the loaded behavior files (base + model)',
  '/reasoning': "toggle live display of the model's thinking",
  '/stop':      'interrupt the turn in progress',
  '/reload':    're-read config.yaml into the running core',
  '/help':      'this list',
};

// /reasoning and /help are handled here, not by the core: they change this
// browser, not the conversation.

const FRAMES = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏';   // the same spinner ui.py draws in the terminal
const PROTOCOL_VERSION = 1;

const S = {
  ws: null,
  session: new URLSearchParams(location.search).get('session') || 'web',
  token: localStorage.getItem('letsclaw.token') || '',
  lastSeq: 0,
  model: null,
  budget: 0,
  tripPct: 0,
  rollovers: 0,
  showReasoning: localStorage.getItem('letsclaw.reasoning') === '1',
  busy: false,
  closing: false,
  backoff: 500,
  turn: null,
  sent: JSON.parse(localStorage.getItem('letsclaw.sent') || '[]'),
  histIdx: -1,
  rpcs: new Map(),
  rpcId: 0,
};

const log = $('#log');

/* ------------------------------------------------------------------ markdown */

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function inlineMd(s) {
  // Code spans come out first so emphasis can't reach inside them.
  const spans = [];
  s = s.replace(/`([^`]+)`/g, (_, c) => `\u0001${spans.push(c) - 1}\u0001`);
  s = s
    .replace(/\[([^\]]+)\]\(((?:https?:\/\/|mailto:)[^\s)]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*\w])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  return s.replace(/\u0001(\d+)\u0001/g, (_, i) => `<code>${spans[i]}</code>`);
}

function renderMd(src) {
  // Strip control characters first: the block sentinels below are \u0000
  // and \u0001, and a tool echoing raw bytes back through the model must not
  // be able to forge one. Done before extraction, so it cannot eat the
  // sentinels we insert immediately afterwards.
  src = String(src).replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, '');

  // An unbalanced fence means we're mid-stream inside a code block; close it so
  // the partial block still renders as code instead of as prose.
  if (((src.match(/^```/gm) || []).length) % 2) src += '\n```';

  const blocks = [];
  src = src.replace(/```([\w+#.-]*)[ \t]*\n?([\s\S]*?)```/g, (_, lang, code) =>
    `\u0000${blocks.push({ lang, code }) - 1}\u0000`);

  const lines = esc(src).split('\n');
  const out = [];
  let para = [], list = null;

  const flush = () => { if (para.length) { out.push(`<p>${inlineMd(para.join(' '))}</p>`); para = []; } };
  const endList = () => { if (list) { out.push(`</${list}>`); list = null; } };

  const cells = (row) => row.replace(/^\s*\|?|\|?\s*$/g, '').split('|').map((c) => c.trim());

  for (let i = 0; i < lines.length; i++) {
    const t = lines[i].trim();
    let m;

    if (!t) { flush(); endList(); continue; }

    if (/^\u0000\d+\u0000$/.test(t)) { flush(); endList(); out.push(t); continue; }

    // table: a header row followed by a |---|---| rule
    if (t.includes('|') && /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(lines[i + 1] || '')
        && cells(lines[i + 1]).length === cells(t).length) {
      flush(); endList();
      const head = cells(t);
      const rows = [];
      let j = i + 2;
      for (; j < lines.length && lines[j].includes('|') && lines[j].trim(); j++) rows.push(cells(lines[j]));
      i = j - 1;
      out.push('<table><thead><tr>' + head.map((c) => `<th>${inlineMd(c)}</th>`).join('') +
               '</tr></thead><tbody>' +
               rows.map((r) => '<tr>' + r.map((c) => `<td>${inlineMd(c)}</td>`).join('') + '</tr>').join('') +
               '</tbody></table>');
      continue;
    }

    if ((m = t.match(/^(#{1,6})\s+(.*)$/))) {
      flush(); endList();
      const n = Math.min(m[1].length + 1, 6);   // h1 in a chat bubble is shouting
      out.push(`<h${n}>${inlineMd(m[2])}</h${n}>`);
      continue;
    }
    if (/^([-*_])\1{2,}$/.test(t)) { flush(); endList(); out.push('<hr>'); continue; }
    if ((m = t.match(/^&gt;\s?(.*)$/))) {
      flush(); endList();
      out.push(`<blockquote>${inlineMd(m[1])}</blockquote>`);
      continue;
    }
    if ((m = t.match(/^[-*+]\s+(.*)$/))) {
      flush();
      if (list !== 'ul') { endList(); out.push('<ul>'); list = 'ul'; }
      out.push(`<li>${inlineMd(m[1])}</li>`);
      continue;
    }
    if ((m = t.match(/^\d+[.)]\s+(.*)$/))) {
      flush();
      if (list !== 'ol') { endList(); out.push('<ol>'); list = 'ol'; }
      out.push(`<li>${inlineMd(m[1])}</li>`);
      continue;
    }
    endList();
    para.push(t);
  }
  flush(); endList();

  return out.join('\n').replace(/\u0000(\d+)\u0000/g, (_, i) => {
    const b = blocks[i];
    return `<pre data-lang="${esc(b.lang || '')}"><button class="copy" title="copy">⧉</button>` +
           `<code>${esc(b.code.replace(/\n$/, ''))}</code></pre>`;
  });
}

/* ------------------------------------------------------------------- the log */

let waitEl = null;
let waitTimer = null;

/* The thinking counter. The core counts the tokens with the same tokeniser the
   ⚡ stats line uses and pushes a running total (reasoning_stat), so the figure
   climbing in the spinner is the one the turn ends on. Characters are still
   counted as a fallback: against an older core no stat ever arrives, and a
   chars/4 guess under a ~ beats no counter at all. */
const CHARS_PER_TOKEN = 4;   // the same fallback ratio token_counter.py uses
let think = newThink();

function newThink() {
  return {
    chars: 0,        // reasoning characters this turn (the fallback's raw material)
    tok: 0,          // tokens this turn, from the core
    exact: false,    // has a reasoning_stat landed? (drops the ~)
    stamped: 0,      // tokens already written to a 🧠 stamp
    blockT0: 0,      // when the current run of thinking began
    last: 0,         // when thinking last moved — the stamp's end point
    samples: [],     // [ms, tokens] for the rate, trimmed to RATE_WINDOW
  };
}

const RATE_WINDOW = 5000;

function thinkTokens() {
  return think.exact ? think.tok : Math.round(think.chars / CHARS_PER_TOKEN);
}

/* Tokens per second over the last few seconds, or 0 while there is too little
   to divide by — a rate off two samples 80 ms apart is noise, not information. */
function thinkRate() {
  const s = think.samples;
  if (s.length < 2) return 0;
  const [t0, n0] = s[0], [t1, n1] = s[s.length - 1];
  const dt = (t1 - t0) / 1000;
  return dt >= 1 ? Math.round((n1 - n0) / dt) : 0;
}

/* The figure beside the clock: what this run of thinking has produced, over the
   turn's running total once a tool round has split the turn into more than one
   run. While a tool runs nothing is being thought, so only the total is left
   standing — and it says so, or it reads as the tool's own count. */
function thinkFigure() {
  const total = thinkTokens();
  if (!total) return '';
  const block = total - think.stamped;
  const n = (v) => v.toLocaleString();
  // One ~ at the front covers both numbers: a core too old to send a count
  // leaves every figure here a chars/4 estimate, not just the first.
  const tilde = think.exact ? '' : '~';
  const rate = block ? thinkRate() : 0;
  const body = !block ? `${tilde}${n(total)} tok this turn`
             : block === total ? `${tilde}${n(total)} tok`
             : `${tilde}${n(block)} / ${n(total)} tok`;
  return `<span class="wait-tok">🧠 ${body}`
       + `${rate > 0 ? ` · ${n(rate)} tok/s` : ''}</span>`;
}

/* One reasoning event's worth of progress, from either source. */
function thinkGrew() {
  const now = Date.now();
  if (!think.blockT0) think.blockT0 = now;
  think.last = now;
  think.samples.push([now, thinkTokens()]);
  while (think.samples.length > 2 && now - think.samples[0][0] > RATE_WINDOW) {
    think.samples.shift();
  }
}

/* Called when a run of thinking has plainly ended — the answer started, or a
   tool was called. Leaves what it cost on screen, since the spinner carrying
   the live figure is about to be cleared. */
function stampThinking() {
  const tok = thinkTokens() - think.stamped;
  if (tok <= 0) return;
  const secs = think.blockT0 ? Math.max(0, (think.last - think.blockT0) / 1000) : 0;
  think.stamped += tok;
  think.blockT0 = 0;
  think.samples = [];
  const text = `🧠 thought ${think.exact ? '' : '~'}${tok.toLocaleString()} tok`
             + (secs >= 0.1 ? ` in ${secs.toFixed(1)}s` : '');
  // Onto the thinking block when there is one to hang it off, so the figure sits
  // with the text it measures; on its own line when the thinking is hidden.
  const on = S.turn && S.turn.reasonEl;
  if (on) {
    const tag = node('thought', esc(text));
    on.appendChild(tag);
  } else {
    slab('thought', esc(text));
  }
}

function atBottom() {
  return log.scrollHeight - log.scrollTop - log.clientHeight < 80;
}

function append(el) {
  const stick = atBottom();
  log.insertBefore(el, waitEl);          // the spinner stays last
  if (stick) log.scrollTop = log.scrollHeight;
  return el;
}

function node(cls, html) {
  const el = document.createElement('div');
  el.className = cls;
  if (html !== undefined) el.innerHTML = html;
  return el;
}

function msg(kind, who, html) {
  const el = node(`msg ${kind}`);
  el.innerHTML = `<div class="who">${who}</div><div class="body"></div>`;
  if (html !== undefined) el.querySelector('.body').innerHTML = html;
  return append(el);
}

/* Two elements, not one: the outer div does the positioning that lines a slab up
   under the message bodies, the inner one is the visible box. .tool and .roll set
   their own padding, and on a single element that shorthand would wipe out the
   50px gutter offset. Callers get the inner box, so `.slab.roll button` and the
   like still match as descendants. */
function slab(cls, html) {
  const box = node('slab-box', html);
  append(node(`slab ${cls}`)).appendChild(box);
  return box;
}

function notice(text, level) {
  return slab(`notice ${level || ''}`, esc(text));
}

function separator(text) { return append(node('sep', esc(text))); }

/* Streaming text: buffer the raw markdown, repaint on a frame timer. */
function stream(el, delta) {
  el._raw = (el._raw || '') + delta;
  if (el._pending) return;
  el._pending = true;
  setTimeout(() => {
    el._pending = false;
    const stick = atBottom();
    el.innerHTML = renderMd(el._raw);
    if (stick) log.scrollTop = log.scrollHeight;
  }, 60);
}

function flushStream(el) {
  if (!el) return;
  el._pending = false;
  el.innerHTML = renderMd(el._raw || '');
  // Models routinely emit a bare "\n\n" before calling a tool. Rendered, that is an
  // avatar next to nothing; drop the whole message rather than leave a stray 🐱.
  if (!(el._raw || '').trim()) el.closest('.msg').remove();
}

function setWaiting(label) {
  clearWaiting();
  waitEl = node('slab waiting');
  log.appendChild(waitEl);
  const t0 = Date.now();
  let i = 0;
  const tick = () => {
    const s = (Date.now() - t0) / 1000;
    // The clock restarts with each wait; the token figure does not — a resumed
    // lap is the same thinking continuing, and the turn total spans the lot. On
    // a long think it is the only sign the model is still getting somewhere,
    // which is why it shows even when the thinking text is hidden.
    waitEl.innerHTML = `<span class="spin">${FRAMES[i++ % FRAMES.length]}</span> `
      + `${esc(label)}… <span class="wait-clock">${s.toFixed(1)}s</span>${thinkFigure()}`;
  };
  tick();
  waitTimer = setInterval(tick, 100);
  if (atBottom()) log.scrollTop = log.scrollHeight;
}

function clearWaiting() {
  if (waitTimer) { clearInterval(waitTimer); waitTimer = null; }
  if (waitEl) { waitEl.remove(); waitEl = null; }
}

log.addEventListener('click', (e) => {
  const btn = e.target.closest('.copy');
  if (!btn) return;
  navigator.clipboard.writeText(btn.parentElement.querySelector('code').textContent);
  btn.textContent = '✓';
  setTimeout(() => { btn.textContent = '⧉'; }, 1200);
});

/* ---------------------------------------------------------------- the header */

function setStatus(cls, text) {
  const el = $('#status');
  el.className = `status ${cls}`;
  el.textContent = text;
}

const short = (n) => (n >= 1000 ? `${Math.round(n / 1000)}k` : String(n));

function setGauge(used, budget, measured) {
  const g = $('#gauge');
  if (!budget) { $('#gauge-label').textContent = '—'; return; }
  const pct = Math.round((used * 100) / budget);
  $('#gauge-fill').style.width = `${Math.min(pct, 100)}%`;
  // Budget abbreviated: spelled out in full it runs under the trip marker at the
  // right-hand end. The exact figure is one hover away in the title, and in /info.
  $('#gauge-label').textContent =
    `${measured === false ? '~' : ''}${used.toLocaleString()} / ${short(budget)} · ${pct}%`;
  g.title = `${used.toLocaleString()} of ${budget.toLocaleString()} tokens`
          + (measured === false ? ' (estimated)' : ' (measured)');
  g.classList.toggle('warn', S.tripPct > 0 && pct >= S.tripPct - 10 && pct < 100);
  g.classList.toggle('over', pct >= 100);
  const trip = $('#gauge-trip');
  trip.hidden = !S.tripPct;
  trip.style.left = `${S.tripPct}%`;
}

function setBusy(on) {
  S.busy = on;
  $('#stop').hidden = !on;
  $('#send').disabled = on;
}

function setHint(extra) {
  const ro = (S.tripPct ? `rollover at ${S.tripPct}%` : 'rollover off')
           + rolloverCount({ count: S.rollovers });
  $('#hint').textContent =
    `session '${S.session}' · ${S.model || '?'} · ${ro} · thinking ${S.showReasoning ? 'shown' : 'hidden'}` +
    (extra ? ` · ${extra}` : '');
}

/* --------------------------------------------------------------- the socket */

function authQuery() { return S.token ? `&token=${encodeURIComponent(S.token)}` : ''; }
function authHeaders() { return S.token ? { Authorization: `Bearer ${S.token}` } : {}; }

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${proto}://${location.host}/ws?session=${encodeURIComponent(S.session)}`
            + `&last_seq=${S.lastSeq}${authQuery()}`;
  setStatus('wait', 'connecting');
  let ws;
  try {
    ws = new WebSocket(url);
  } catch (e) {
    scheduleReconnect();
    return;
  }
  S.ws = ws;

  ws.onopen = () => {
    S.backoff = 500;
    setStatus('on', 'connected');
  };

  ws.onmessage = (m) => {
    let e;
    try { e = JSON.parse(m.data); } catch { return; }
    if (typeof e.seq === 'number') S.lastSeq = Math.max(S.lastSeq, e.seq);
    try {
      handle(e);
    } catch (err) {
      // One unrenderable event must not take the stream down with it.
      notice(`could not render ${e.t}: ${err}`, 'warn');
    }
  };

  ws.onclose = (ev) => {
    // Checked on the socket, not on S: switchSession() closes this one and opens
    // the next in the same tick, and the close event only lands afterwards.
    if (ws._replaced) return;
    clearWaiting();
    setBusy(false);
    if (S.closing) return;
    if (ev.code === 1013) notice('the core dropped this client for falling behind — reconnecting', 'warn');
    setStatus('off', 'offline');
    scheduleReconnect();
  };

  ws.onerror = () => { /* onclose always follows; report there */ };
}

function scheduleReconnect() {
  setTimeout(() => {
    if (!S.closing) connect();
  }, S.backoff);
  S.backoff = Math.min(S.backoff * 2, 10000);
}

function send(obj) {
  if (S.ws && S.ws.readyState === WebSocket.OPEN) { S.ws.send(JSON.stringify(obj)); return true; }
  notice('not connected to the core — start it with ./serve.sh', 'warn');
  return false;
}

/* A command is a request/response pair keyed by request_id — the core replies to
   the asking client only, so /info never lands in everyone else's log.
   `silent` suppresses the default rendering for calls made on the page's behalf. */
function rpc(name, args, silent) {
  const request_id = `w${++S.rpcId}`;
  return new Promise((resolve, reject) => {
    if (!send({ t: 'command', name, args: args || '', request_id })) return reject(new Error('offline'));
    // /new summarises with the model before replying, so this waits minutes, not seconds.
    const timer = setTimeout(() => { S.rpcs.delete(request_id); reject(new Error('timed out')); }, 300000);
    S.rpcs.set(request_id, { resolve, timer, silent });
  });
}

function switchSession(name) {
  if (!name || name === S.session) return;
  if (S.ws) { S.ws._replaced = true; S.ws.close(); }
  S.session = name;
  S.lastSeq = 0;
  S.turn = null;
  clearWaiting();
  log.innerHTML = '';
  history.replaceState(null, '', `?session=${encodeURIComponent(name)}`);
  refreshSessions();          // move the highlight now, don't wait for the poll
  connect();
}

/* ---------------------------------------------------------------- rendering */

function toolSlab(name, args) {
  const el = slab('tool');
  const d = document.createElement('details');
  d.innerHTML = `<summary>⚙️ <b>${esc(name)}</b> <span class="arg"></span></summary>`;
  d.querySelector('.arg').textContent = (args || '').slice(0, 160);
  if (args) {
    const pre = document.createElement('pre');
    pre.textContent = pretty(args);
    d.appendChild(pre);
  }
  el.appendChild(d);
  return el;
}

function pretty(args) {
  try { return JSON.stringify(JSON.parse(args), null, 2); } catch { return args; }
}

function handle(e) {
  switch (e.t) {

  case 'hello': return hello(e);

  case 'turn_start':
    S.turn = { textEl: null, reasonEl: null, tools: new Map() };
    think = newThink();
    setBusy(true);
    msg('user', '👤').querySelector('.body').textContent = e.text;
    setWaiting('thinking');
    return;

  case 'reasoning':
    // Counted before the display check on purpose: with the thinking hidden
    // this counter is the only feedback that the model is still working.
    think.chars += e.delta.length;
    thinkGrew();
    if (!S.showReasoning) return;
    if (!S.turn) S.turn = { textEl: null, reasonEl: null, tools: new Map() };
    if (!S.turn.reasonEl) S.turn.reasonEl = msg('reasoning', '🧠').querySelector('.body');
    S.turn.reasonEl.textContent += e.delta;
    if (atBottom()) log.scrollTop = log.scrollHeight;
    return;

  // The core's own count, pushed a few times a second while it thinks. It
  // arrives whether or not this client displays the thinking, and it is the
  // same figure the ⚡ line ends the turn on.
  case 'reasoning_stat':
    think.tok = e.tokens;
    think.exact = true;
    thinkGrew();
    return;

  case 'text':
    // Thinking is over the moment words start: stamp what it cost before the
    // spinner holding the live figure goes away.
    stampThinking();
    clearWaiting();
    if (!S.turn) S.turn = { textEl: null, reasonEl: null, tools: new Map() };
    else S.turn.reasonEl = null;   // any further thinking opens its own block
    if (!S.turn.textEl) S.turn.textEl = msg('llm', '🐱').querySelector('.body');
    stream(S.turn.textEl, e.delta);
    return;

  case 'tool_call': {
    stampThinking();
    clearWaiting();
    if (S.turn) { flushStream(S.turn.textEl); S.turn.textEl = null; S.turn.reasonEl = null; }
    const el = toolSlab(e.name, e.arguments);
    if (S.turn) S.turn.tools.set(e.id, el);
    setWaiting(e.name);
    return;
  }

  case 'tool_result': {
    clearWaiting();
    const el = S.turn && S.turn.tools.get(e.id);
    if (el) {
      const tag = document.createElement('span');
      tag.className = 'done';
      tag.textContent = ` ↳ ${e.size} chars`;
      el.querySelector('summary').appendChild(tag);
    } else {
      // Attached mid-turn: we never saw the tool_call this answers.
      slab('tool', `⚙️ <b>${esc(e.name)}</b> <span class="done">↳ ${e.size} chars</span>`);
    }
    setWaiting('thinking');
    return;
  }

  case 'stats': {
    const p = [];
    if (e.ttft != null) p.push(`ttft ${e.ttft.toFixed(1)}s`);
    p.push(`user ${e.user_tokens} + LLM ${e.llm_tokens} tok`);
    if (e.reasoning_tokens) p.push(`reasoning ${e.reasoning_tokens} tok`);
    if (e.tool_calls) p.push(`${e.tool_calls} tool call${e.tool_calls === 1 ? '' : 's'}`);
    // Everything generated this turn — answer, thinking and tool arguments across
    // every round — which is more than the LLM text above. Its own ~: the
    // prompt side and the output side are measured or estimated independently.
    if (e.output_tokens != null) {
      const tot = e.output_total ? ` (${e.output_total} this session)` : '';
      p.push(`out ${e.output_measured ? '' : '~'}${e.output_tokens} tok${tot}`);
    }
    const pct = e.budget ? Math.round((e.used * 100) / e.budget) : 0;
    p.push(`context ${e.measured ? '' : '~'}${e.used}/${e.budget} (${pct}%)`);
    slab('stats', `⚡ ${esc(p.join(' · '))}${e.budget && e.used > e.budget ? '  ⚠️ over budget' : ''}`);
    setGauge(e.used, e.budget, e.measured);
    return;
  }

  case 'turn_end':
    // A turn can end on thinking alone — budget gone, nothing said. The stamp is
    // the only record of it until the ⚡ line, which that turn may never reach.
    stampThinking();
    clearWaiting();
    if (S.turn) flushStream(S.turn.textEl);
    S.turn = null;
    setBusy(false);
    refreshSessions();            // the message count just moved
    return;

  case 'rollover_start':
    clearWaiting();
    slab('roll', `🔄 Rolling over — ${esc(e.reason)}.`);
    setWaiting('archiving and summarising');
    return;

  case 'rollover_ask':  return askRollover(e);

  case 'rollover_done': {
    clearWaiting();
    const paths = [];
    if (e.transcript) paths.push(`💾 transcript  ${esc(e.transcript)}`);
    if (e.handoff)    paths.push(`🧠 handoff     ${esc(e.handoff)}`);
    slab('roll', `✨ New session — ${e.used}/${e.budget} tok` +
                 (paths.length ? `<div class="paths">${paths.join('<br>')}</div>` : ''));
    separator('fresh context from here');
    if (e.count != null) S.rollovers = e.count;
    setGauge(e.used, e.budget, false);
    setHint();
    return;
  }

  case 'session_state':
    if (e.what === 'cleared') {
      clearWaiting();
      log.innerHTML = '';
      notice('🧹 History cleared (discarded — /new archives it instead).');
      if (e.record) notice(`📓 what was said is still in ${e.record}`);
      setGauge(0, S.budget, false);
    } else if (e.what === 'model') {
      S.model = e.model;
      S.budget = e.budget;
      $('#model').value = e.model;
      notice(`🤖 Switched to ${e.model} — context budget ${e.budget} tok.`);
      setHint();
    } else if (e.what === 'reloaded') {
      // Broadcast to every session the reload touched, not just the one that
      // asked. The budget can have moved under this tab, so the gauge is redrawn
      // against the new one and the dropdown refilled — a model may have been
      // added to or dropped from the file.
      S.model = e.model;
      S.budget = e.budget;
      notice(`♻️ Config reloaded — ${e.model}, context budget ${e.budget} tok.`);
      loadModels().then(() => { $('#model').value = e.model; }).catch(() => {});
      rpc('info', '', true)
        .then((r) => r.info && setGauge(r.info.used, r.info.budget, false))
        .catch(() => {});
      setHint();
    } else if (e.what === 'renamed') {
      // e.session is already the new name — emit() stamped it after the rename.
      // Nothing about the conversation changed, so the scrollback stays put; only
      // the labels that carry the name need moving, the URL included, or a reload
      // would take this tab back to a name that no longer exists.
      S.session = e.session;
      document.title = `${e.session} · letsClaw`;
      history.replaceState(null, '', `?session=${encodeURIComponent(e.session)}`);
      setHint();
      notice(`✎ Renamed '${e.was}' to '${e.session}'.`);
    } else if (e.what === 'deleted') {
      // Deleted from another window, or from this one while we were attached.
      // The socket stays up: the core re-homes it onto a fresh session of the
      // same name as soon as we say anything, so the name stays usable.
      clearWaiting();
      log.innerHTML = '';
      setGauge(0, S.budget, false);
      notice(`🗑️ Session '${e.session}' was deleted. Anything you send starts it over, empty.`, 'warn');
    }
    refreshSessions();
    return;

  case 'busy':
    clearWaiting();
    setBusy(false);
    notice(`⏳ ${e.reason}`, 'warn');
    return;

  case 'notice':
    if (S.busy) clearWaiting();
    notice(e.text, e.level === 'warn' ? 'warn' : '');
    if (S.busy) setWaiting('thinking');
    return;

  case 'error':
    clearWaiting();
    setBusy(false);
    slab('notice err', `❌ ${esc(e.msg)}`);
    return;

  case 'response': return response(e);

  case 'pong': return;
  }
}

function hello(e) {
  if (e.proto !== PROTOCOL_VERSION) {
    notice(`the core speaks protocol ${e.proto}, this page speaks ${PROTOCOL_VERSION} — update one of them.`, 'warn');
  }
  S.model = e.model;
  S.budget = e.budget;
  // Zeroed before the replay below: attaching to a turn already in flight, we
  // never saw its earlier thinking, so counting on from a previous turn's total
  // would invent tokens. A replayed turn_start resets it again, harmlessly. The
  // core's next reasoning_stat carries the turn's real total, so a mid-turn
  // attach catches up within a quarter-second rather than counting from zero.
  think = newThink();
  S.tripPct = (e.rollover && e.rollover.percent) || 0;
  S.rollovers = (e.rollover && e.rollover.count) || 0;
  S.session = e.session;
  document.title = `${e.session} · letsClaw`;
  // Empty means boot()'s /models fetch never landed. The core is plainly up now, or
  // this event would not be here, so refill the list before selecting in it.
  const selectModel = () => {
    if ($('#model').querySelector(`option[value="${CSS.escape(e.model || '')}"]`)) $('#model').value = e.model;
  };
  if ($('#model').options.length) selectModel(); else loadModels().then(selectModel);
  setHint();

  const fresh = log.childElementCount === 0;
  if (fresh) {
    replayHistory(e.messages || []);
    if ((e.messages || []).length) separator(`rejoined — ${e.messages.length} messages already in this session`);
  } else if (e.gap) {
    notice('some output was missed while disconnected', 'warn');
  }
  (e.missed || []).forEach(handle);

  setBusy(!!e.busy);
  if (e.busy) setWaiting('a turn is already running');

  // The gauge has no reading until a turn completes; /info gives an estimate now.
  rpc('info', '', true)
    .then((r) => r.info && setGauge(r.info.used, r.info.budget, false))
    .catch(() => {});
}

/* Draw the history the core hands us on attach, pairing each tool call with its
   result so a reloaded page looks like the session you left. */
function replayHistory(messages) {
  const pending = new Map();
  for (const m of messages) {
    if (m.role === 'user') {
      msg('user', '👤').querySelector('.body').textContent = m.content || '';
    // 'assistant' here is the wire role the server stores, not a display name:
    // the chat template switches on that exact string. The bubble is .msg.llm.
    } else if (m.role === 'assistant' && m.tool_calls) {
      // .trim(): a model heading straight for a tool usually still emits "\n\n",
      // which is truthy and would replay as an avatar beside an empty bubble.
      if ((m.content || '').trim()) msg('llm', '🐱', renderMd(m.content));
      for (const tc of m.tool_calls) {
        pending.set(tc.id, toolSlab(tc.function.name, tc.function.arguments));
      }
    } else if (m.role === 'assistant') {
      if (!(m.content || '').trim()) continue;
      msg('llm', '🐱', renderMd(m.content));
    } else if (m.role === 'tool') {
      const el = pending.get(m.tool_call_id);
      if (!el) continue;
      const out = m.content || '';
      const tag = document.createElement('span');
      tag.className = out.startsWith('error:') ? 'fail' : 'done';
      tag.textContent = ` ↳ ${out.length} chars`;
      el.querySelector('summary').appendChild(tag);
      const pre = document.createElement('pre');
      pre.textContent = out.slice(0, 4000);
      el.querySelector('details').appendChild(pre);
    }
  }
}

function askRollover(e) {
  clearWaiting();
  const el = slab('roll',
    `⚠️ Context at ${e.used}/${e.budget} (${e.percent}%). Roll over to a new session?` +
    `<div class="paths">no answer in <b class="cd">${e.timeout_s}</b>s keeps it</div>`);
  const yes = document.createElement('button');
  yes.className = 'yes';
  yes.textContent = 'Roll over';
  const no = document.createElement('button');
  no.textContent = 'Keep it';
  el.append(yes, no);

  let left = e.timeout_s;
  const cd = setInterval(() => {
    left -= 1;
    if (left <= 0) { clearInterval(cd); el.dataset.answered = 'timeout'; return; }
    el.querySelector('.cd').textContent = left;
  }, 1000);

  const answer = (v) => {
    clearInterval(cd);
    el.dataset.answered = String(v);
    // Idempotent core-side: another client may have answered first.
    send({ t: 'rollover_reply', request_id: e.request_id, yes: v });
    if (v) setWaiting('archiving and summarising');
  };
  yes.onclick = () => answer(true);
  no.onclick = () => answer(false);
}

function response(e) {
  const waiter = S.rpcs.get(e.request_id);
  if (waiter) {
    clearTimeout(waiter.timer);
    S.rpcs.delete(e.request_id);
    waiter.resolve(e);
    if (waiter.silent) return;
  }
  if (e.ok === false) { slab('notice err', `❌ ${esc(e.error)}`); return; }
  if (e.changed) return showReload(e);
  if (e.info) return showInfo(e.info);
  if ('behavior' in e) {
    let md = '';
    if (e.base) md += '**base**\n```markdown\n' + e.base + '\n```\n';
    if (e.behavior) md += '**model**\n```markdown\n' + e.behavior + '\n```\n';
    return msg('llm', '📄', renderMd(md || '_No behavior file loaded._'));
  }
  if (e.configured) {
    return notice(`🤖 current: ${e.current}   ·   configured: ${e.configured.join(', ') || '(none)'}`);
  }
}

function showReload(e) {
  if (!e.changed.length) { notice(`♻️ ${e.note || 'nothing changed'}`); return; }
  // A deferred session is mid-turn; it picks the change up when that turn ends.
  slab('notice',
    `<b>♻️ Config reloaded</b><br>` +
    e.changed.map((c) => esc(c)).join('<br>') +
    `<br><br>${e.updated} session(s) updated` +
    (e.deferred ? `, ${e.deferred} waiting for a turn to finish` : ''));
}

function rolloverCount(ro) {
  if (!ro) return '';
  const n = ro.count || 0;
  return ` · ${n} rollover${n !== 1 ? 's' : ''}`;
}
function showInfo(i) {
  const pct = i.budget ? Math.round((i.used * 100) / i.budget) : 0;
  const rows = i.recent.map((m) => {
    const emoji = { system: '🤖', user: '👤', tool: '🔧' }[m.role] || '🐱';
    return `${emoji} ${esc(m.role)}: ${esc(m.content)} (${m.tokens} tok)`;
  }).join('<br>');
  slab('notice',
    `<b>🤖 ${esc(i.model)}</b> @ ${esc(i.base_url)}<br>` +
    // The id as well as the name: archives are filed under it, and it is the
    // half that survives a rename.
    `💬 ${i.messages} messages in session '${esc(i.session)}'` +
    (i.session_id ? ` (id <b>${esc(i.session_id)}</b> — archives are ${esc(i.session_id)}_*)` : '') +
    '<br>' +
    `⚡ context ~${i.used}/${i.budget} tok (${pct}%) · ${i.tools_tokens} tok of tool schemas<br>` +
    (i.output_total != null
      ? `📤 ${i.output_total} output tok generated in this session (an odometer — /clear does not rewind it)<br>`
      : '') +
    (i.rollover.percent
      ? `🔄 rollover ${esc(i.rollover.mode)} at ${i.rollover.percent}% (${i.rollover.trip} tok)${rolloverCount(i.rollover)}<br>`
      : `🔄 rollover disabled (/new still works)${rolloverCount(i.rollover)}<br>`) +
    `<br>📜 recent:<br>${rows}`);
  setGauge(i.used, i.budget, false);
}

/* ---------------------------------------------------------------- composer */

const input = $('#input');
const ac = $('#ac');
let acItems = [], acSel = 0;

function autosize() {
  input.style.height = 'auto';
  input.style.height = `${input.scrollHeight}px`;
}

function acUpdate() {
  const v = input.value;
  let items = [];
  if (/^\/[a-z]*$/i.test(v)) {
    items = Object.keys(COMMANDS).filter((c) => c.startsWith(v))
                  .map((c) => ({ text: c + ' ', label: c, desc: COMMANDS[c] }));
  } else {
    const m = v.match(/^\/model\s+(\S*)$/);
    if (m) {
      items = [...$('#model').options].map((o) => o.value)
        .filter((n) => n.startsWith(m[1]))
        .map((n) => ({ text: `/model ${n}`, label: n, desc: '' }));
    }
  }
  acItems = items;
  acSel = 0;
  ac.hidden = !items.length;
  ac.innerHTML = items.map((it, i) =>
    `<div class="${i === 0 ? 'sel' : ''}" data-i="${i}">${esc(it.label)}<span>${esc(it.desc)}</span></div>`).join('');
}

function acMove(d) {
  acSel = (acSel + d + acItems.length) % acItems.length;
  [...ac.children].forEach((c, i) => c.classList.toggle('sel', i === acSel));
}

function acAccept() {
  input.value = acItems[acSel].text;
  ac.hidden = true;
  acItems = [];
  autosize();
}

ac.addEventListener('mousedown', (e) => {
  const d = e.target.closest('[data-i]');
  if (!d) return;
  e.preventDefault();
  acSel = +d.dataset.i;
  acAccept();
  input.focus();
});

input.addEventListener('input', () => { autosize(); acUpdate(); S.histIdx = -1; });

input.addEventListener('keydown', (e) => {
  if (!ac.hidden && acItems.length) {
    if (e.key === 'ArrowDown') { e.preventDefault(); return acMove(1); }
    if (e.key === 'ArrowUp')   { e.preventDefault(); return acMove(-1); }
    if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) { e.preventDefault(); return acAccept(); }
    if (e.key === 'Escape')    { ac.hidden = true; acItems = []; return; }
  }
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); return submit(); }
  // Terminal habit: an empty box plus ArrowUp recalls what you sent before.
  if (e.key === 'ArrowUp' && (!input.value || S.histIdx >= 0) && S.sent.length) {
    e.preventDefault();
    S.histIdx = Math.min(S.histIdx + 1, S.sent.length - 1);
    input.value = S.sent[S.sent.length - 1 - S.histIdx];
    autosize();
  } else if (e.key === 'ArrowDown' && S.histIdx >= 0) {
    e.preventDefault();
    S.histIdx -= 1;
    input.value = S.histIdx < 0 ? '' : S.sent[S.sent.length - 1 - S.histIdx];
    autosize();
  }
});

function remember(text) {
  if (S.sent[S.sent.length - 1] !== text) S.sent.push(text);
  S.sent = S.sent.slice(-100);
  localStorage.setItem('letsclaw.sent', JSON.stringify(S.sent));
  S.histIdx = -1;
}

function submit() {
  const text = input.value.trim();
  if (!text) return;
  ac.hidden = true;
  acItems = [];

  if (text.startsWith('/')) {
    const [word, ...rest] = text.split(/\s+/);
    const args = rest.join(' ');
    remember(text);
    input.value = ''; autosize();

    if (word === '/help') {
      return slab('notice', Object.entries(COMMANDS)
        .map(([c, d]) => `<b>${c}</b> — ${esc(d)}`).join('<br>'));
    }
    if (word === '/reasoning') return toggleReasoning();
    if (word === '/stop')      { send({ t: 'stop' }); return; }
    if (!(word in COMMANDS))   return notice(`unknown command ${word} — /help lists them`, 'warn');

    if (word === '/new') setWaiting('archiving and summarising');
    rpc(word.slice(1), args).catch((err) => {
      clearWaiting();
      notice(`${word}: ${err.message}`, 'warn');
    });
    return;
  }

  if (!send({ t: 'submit', text })) return;   // keep the text if we're offline
  remember(text);
  input.value = '';
  autosize();
}

function toggleReasoning() {
  S.showReasoning = !S.showReasoning;
  localStorage.setItem('letsclaw.reasoning', S.showReasoning ? '1' : '0');
  $('#btn-reasoning').classList.toggle('on', S.showReasoning);
  notice(`🧠 reasoning display ${S.showReasoning ? 'ON' : 'OFF'}`);
  setHint();
}

/* ------------------------------------------------------------------- wiring */

$('#send').onclick = submit;
$('#stop').onclick = () => send({ t: 'stop' });
$('#btn-reasoning').onclick = toggleReasoning;
$('#btn-info').onclick = () => rpc('info').catch(() => {});
$('#btn-behavior').onclick = () => rpc('behavior').catch(() => {});

$('#btn-clear').onclick = () => {
  if (confirm('Discard this conversation? /new archives it instead.')) rpc('clear').catch(() => {});
};
$('#btn-new').onclick = () => {
  setWaiting('archiving and summarising');
  rpc('new').catch((err) => { clearWaiting(); notice(`/new: ${err.message}`, 'warn'); });
};
$('#btn-reload').onclick = () => {
  // Errors come back as ok:false, which response() already renders — a rejected
  // reload has changed nothing, so there is nothing to undo here.
  rpc('reload').catch((err) => notice(`/reload: ${err.message}`, 'warn'));
};

$('#model').onchange = (e) => {
  const want = e.target.value;
  if (want && want !== S.model) rpc('model', want).catch(() => {});
};

/* ------------------------------------------------------------------ sidebar */

/* The list is the core's, not ours: it is rebuilt from GET /sessions rather than
   patched locally, so a session someone else created or deleted in another
   browser shows up here too. The session you are *in* is always listed, even
   before the core has one, because it exists the moment you type into it. */
async function refreshSessions() {
  let list = [];
  try {
    const r = await fetch('/sessions', { headers: authHeaders() });
    if (!r.ok) return;
    ({ sessions: list } = await r.json());
  } catch { return; }        // core down; the status pill already says so

  if (!list.some((s) => s.name === S.session)) {
    list.push({ name: S.session, messages: 0, clients: 1, busy: false, pending: true });
  }
  list.sort((a, b) => a.name.localeCompare(b.name));

  const el = $('#side-list');
  el.textContent = '';
  for (const s of list) {
    const row = node(`srow${s.name === S.session ? ' on' : ''}`);
    row.dataset.name = s.name;
    row.title = `${s.name} — ${s.messages} messages · ${s.model || 'no model yet'}`
              + (s.clients ? ` · ${s.clients} attached` : '');

    const dot = node('dot');
    dot.hidden = !s.busy;
    const nm = node('nm');
    nm.textContent = s.name;                      // textContent: names are user input
    const ct = node('ct');
    ct.textContent = s.messages || '';
    const ren = document.createElement('button');
    ren.className = 'ren';
    // Drawn, not typed. Every pencil codepoint falls back to the colour-emoji face
    // here — which ignores `color`, so it cannot go white on the selected row, and
    // is tofu on a box with no emoji font. stroke="currentColor" gets both for free.
    // Static markup, no interpolation: the session name never goes near innerHTML.
    ren.innerHTML = '<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true">'
                  + '<path d="M11.3 1.7l3 3L5 14l-3.5.5L2 11z" fill="none"'
                  + ' stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    ren.title = `rename ${s.name}`;
    const del = document.createElement('button');
    del.className = 'del';
    del.textContent = '✕';
    del.title = `delete ${s.name}`;

    row.append(dot, nm, ct, ren, del);
    el.appendChild(row);
  }
}

$('#side-list').addEventListener('click', (e) => {
  const row = e.target.closest('.srow');
  if (!row) return;
  if (e.target.closest('.del')) { deleteSession(row.dataset.name); return; }
  if (e.target.closest('.ren')) { renameSession(row.dataset.name); return; }
  switchSession(row.dataset.name);
});

// The name is the obvious thing to double-click, so let that mean rename too.
$('#side-list').addEventListener('dblclick', (e) => {
  const row = e.target.closest('.srow');
  if (row && e.target.closest('.nm')) renameSession(row.dataset.name);
});

async function renameSession(name) {
  const to = prompt(`Rename session "${name}" to:`, name);
  if (to === null) return;                       // cancelled, as distinct from cleared
  if (!to.trim() || to.trim() === name) return;
  try {
    const r = await fetch(`/sessions/${encodeURIComponent(name)}/rename`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ to: to.trim() }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      notice(`could not rename ${name}: ${d.error || r.status}`, 'warn');
    } else if (name !== S.session) {
      // Renaming the session we are *in* is announced by the core over our own
      // socket; for any other one, nothing would say so. Say it here.
      notice(`✎ '${name}' is now '${to.trim()}'.`);
    }
  } catch {
    notice(`could not rename ${name}: the core is unreachable`, 'warn');
  }
  refreshSessions();
}

async function deleteSession(name) {
  const row = [...$('#side-list').children].find((r) => r.dataset.name === name);
  const n = row ? row.querySelector('.ct').textContent : '';
  // Discards the conversation and writes nothing, so ask first — and say how
  // much is about to go, which is the number that makes people hesitate.
  if (!confirm(`Delete session "${name}"?`
             + (n ? `\n\n${n} message${n === '1' ? '' : 's'} will be discarded.` : '')
             + '\n\nThis cannot be undone.')) return;
  try {
    const r = await fetch(`/sessions/${encodeURIComponent(name)}`,
                          { method: 'DELETE', headers: authHeaders() });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      notice(`could not delete ${name}: ${d.error || r.status}`, 'warn');
    }
  } catch {
    notice(`could not delete ${name}: the core is unreachable`, 'warn');
  }
  refreshSessions();
}

$('#side-toggle').onclick = () => {
  const side = $('#side');
  side.hidden = !side.hidden;
  localStorage.setItem('letsclaw.side', side.hidden ? '0' : '1');
};

$('#side-new').onclick = () => {
  const form = $('#side-add');
  form.hidden = !form.hidden;
  if (!form.hidden) $('#side-name').focus();
};

$('#side-add').onsubmit = (e) => {
  e.preventDefault();
  const name = $('#side-name').value.trim();
  $('#side-name').value = '';
  $('#side-add').hidden = true;
  // Nothing is created core-side here: sessions spring into existence on attach,
  // so switching to a name that does not exist yet *is* creating it.
  if (name) switchSession(name);
};

$('#side-name').addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { $('#side-add').hidden = true; input.focus(); }
});

/* Poll, because the list shows *other* sessions and our socket only ever hears
   about this one. Cheap (a small JSON read against a local core), and paused
   while the tab is hidden so a backgrounded window costs nothing. */
setInterval(() => { if (!document.hidden) refreshSessions(); }, 5000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshSessions(); });

window.addEventListener('beforeunload', () => { S.closing = true; if (S.ws) S.ws.close(); });

/* --------------------------------------------------------------------- boot */

$('#gate-form').onsubmit = (e) => {
  e.preventDefault();
  S.token = $('#gate-token').value.trim();
  localStorage.setItem('letsclaw.token', S.token);
  $('#gate').hidden = true;
  boot();
};

/* The model list arrives over HTTP, not the socket — a separate request from the one
   that delivered this script, and one nothing retries. Restart the core in between and
   the dropdown is empty for the life of the page even though the socket reconnects
   fine, so hello() calls this again whenever it finds the list still empty.
   Returns 401 if the core wants a token, true on success, null if it is unreachable. */
async function loadModels() {
  let r;
  try { r = await fetch('/models', { headers: authHeaders() }); } catch { return null; }
  if (r.status === 401) return 401;
  if (!r.ok) return null;
  const { configured, default: def } = await r.json();
  $('#model').innerHTML = configured.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join('');
  $('#model').value = S.model || def;
  return true;
}

async function boot() {
  $('#btn-reasoning').classList.toggle('on', S.showReasoning);
  $('#side').hidden = localStorage.getItem('letsclaw.side') === '0';
  document.title = `${S.session} · letsClaw`;
  setHint();

  if (await loadModels() === 401) {
    // core.token is set and ours is missing or wrong — ask, don't retry blindly.
    $('#gate').hidden = false;
    $('#gate-token').focus();
    return;
  }
  refreshSessions();
  connect();
  input.focus();
}

boot();
