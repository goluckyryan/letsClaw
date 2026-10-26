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
  '/clear':     'forget the conversation, keep the archives',
  '/rollover':  'archive it, carry a handoff forward, start a fresh window',
  '/new':       "wipe everything — conversation and this session's archives",
  '/model':     'list models, or /model <name> to switch',
  '/info':      'recent history and context usage',
  '/behavior':  'print the loaded behavior files (base + model + session)',
  '/reasoning': "cycle the model's thinking: off → folded → open (or name one)",
  '/stop':      'interrupt the turn in progress',
  '/steering':       'send a correction to the turn in progress',
  '/reload':    're-read config.yaml into the running core',
  '/restart':   'restart the core process (/restart force cancels running turns)',
  '/help':      'this list',
};

// /reasoning and /help are handled here, not by the core: they change this
// browser, not the conversation.

const FRAMES = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏';   // the same spinner ui.py draws in the terminal
const PROTOCOL_VERSION = 1;

/* Sidebar width, user-adjustable by dragging its right edge. Persisted like the
   other per-browser prefs (token, reasoning toggle, sidebar visibility). */
const SIDE_W_MIN = 160, SIDE_W_MAX = 480, SIDE_W_DEF = 210;
function clampW(w) { return Math.min(SIDE_W_MAX, Math.max(SIDE_W_MIN, w || SIDE_W_DEF)); }

/* The composer's ↑ recall list is per session: what you typed while working on
   one conversation has no business surfacing in another. The other prefs
   (token, reasoning, sidebar) are genuinely per browser and stay shared.
   Migration: the key was global until it leaked between sessions, so the first
   load seeds this session's list from it and drops the old one. */
const sentKey = (name) => `letsclaw.sent.${name}`;
function loadSent(name) {
  const here = localStorage.getItem(sentKey(name));
  if (here !== null) return JSON.parse(here);
  const legacy = localStorage.getItem('letsclaw.sent');
  if (legacy !== null) {
    localStorage.setItem(sentKey(name), legacy);
    localStorage.removeItem('letsclaw.sent');
    return JSON.parse(legacy);
  }
  return [];
}

/* How much of the model's thinking this browser shows. One setting, not two,
   because the three states are one dimension — none of it, the headline, the
   lot — and the 🧠 button walks them in that order. 'collapsed' and
   'expanded' differ only in how a *new* block arrives: either way it is a
   <details> the reader can open or shut on its own.

   Builds before this wrote '1' / '0' into the same key; those still read. */
const REASON_MODES = ['off', 'collapsed', 'expanded'];
function loadReasonMode() {
  const v = localStorage.getItem('letsclaw.reasoning');
  if (REASON_MODES.includes(v)) return v;
  return v === '1' ? 'expanded' : 'off';
}
const reasonShown = () => S.reason !== 'off';

const SESSION0 = new URLSearchParams(location.search).get('session') || 'web';

const S = {
  ws: null,
  session: SESSION0,
  token: localStorage.getItem('letsclaw.token') || '',
  lastSeq: 0,
  model: null,
  budget: 0,
  tripPct: 0,
  rollovers: 0,
  reason: loadReasonMode(),
  theme: 'auto',
  sideW: clampW(parseInt(localStorage.getItem('letsclaw.sideW'), 10)),
  busy: false,
  closing: false,
  backoff: 500,
  reconnectTimer: null,
  // Set by submit(), consumed by turn_start: did the turn now starting come
  // from this tab? turn_start is a broadcast, so nothing else can tell.
  weAsked: false,
  turn: null,
  sent: loadSent(SESSION0),
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

/* A number off the wire, rendered as a number.

   Token counts, percentages and timeouts are ints from the core today, which
   is why none of the innerHTML templates that interpolate them bare has ever
   gone wrong. But that makes the markup's safety a property of a producer
   three files away rather than of the markup, and the two drift apart the
   first time a field becomes optional or a string. Coercing at the boundary
   costs nothing and makes it true by construction. */
function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? String(n) : '?';
}

/* ![alt](src) -> <img>, but only for an image this core is serving.
 *
 * The src is model output, so it is not a URL we trust: a remote one would let
 * an answer beacon to a third party (and leak the fact, and the timing, of what
 * is on screen) just by being rendered. So nothing is accepted but a path under
 * this core's own /files route -- `![plot](/files/run42.png)`, or the bare
 * `![plot](run42.png)` the model will write half the time anyway.
 *
 * The name is then restricted to a flat filename: no slashes, no `..`, no
 * scheme, no `//host`. The server checks all of this again and more; this is
 * the half that stops a bad src ever reaching the DOM.
 */
const IMG_OK = /^[\w.-]+\.(?:png|jpe?g|gif|webp|svg|bmp|ico|avif)$/i;

/* Where a file served by the core lives, token and all.

   The token rides in the query because the two things that ask for these URLs
   — an <img> and the media panel's <video>/<audio> — set their own src and
   cannot carry an Authorization header. Both built this by hand; the media
   panel's copy even said so in a comment (*"Same URL imgTag() builds, for the
   same reason"*), which is a duplicate admitting to being one rather than
   being called.

   Not authQuery(): that joins with `&`, for a URL that already has a query. */
const fileURL = (name) => `/files/${encodeURIComponent(name)}`
                        + (S.token ? `?token=${encodeURIComponent(S.token)}` : '');

function imgTag(alt, src) {
  // inlineMd runs on text esc() has already been over, so '&' here is already
  // '&amp;'. Escaping again would show '&amp;amp;' in the alt -- the entities
  // are decoded back out for the src, and left as they are for the attributes,
  // which are HTML and want the escaped form.
  const raw = String(src).trim()
    .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'")
    .replace(/^\/files\//, '');
  if (!IMG_OK.test(raw)) return null;
  const url = fileURL(raw);
  // loading=lazy: a long transcript of plots should not fetch them all on
  // attach. The alt doubles as the caption when the file is missing.
  return `<img class="md-img" src="${url.replace(/"/g, '&quot;')}" alt="${alt}" ` +
         `loading="lazy" title="${alt || raw.replace(/"/g, '&quot;')}">`;
}

function inlineMd(s) {
  // Code spans come out first so emphasis can't reach inside them.
  const spans = [];
  s = s.replace(/`([^`]+)`/g, (_, c) => `\u0001${spans.push(c) - 1}\u0001`);
  s = s
    // Images before links: the syntax differs by one leading '!', and the link
    // rule would otherwise match the bracket half and leave a stray '!'.
    // A src we refuse falls through to its own literal text rather than
    // silently vanishing -- the path is usually the useful part of the message.
    // A src we refuse is parked in the code-span array as plain text, not left
    // in the stream: the link rule below would otherwise match what remains of
    // it and turn `![x](http://evil/a.png)` into a live outbound link, which is
    // the exact thing imgTag just declined to make.
    .replace(/!\[([^\]]*)\]\(([^\s)]+)\)/g, (m, alt, src) => {
      const tag = imgTag(alt, src);
      return tag !== null ? tag : `\u0001${spans.push(m) - 1}\u0001`;
    })
    .replace(/\[([^\]]+)\]\(((?:https?:\/\/|mailto:)[^\s)]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*\w])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  return s.replace(/\u0001(\d+)\u0001/g, (_, i) => `<code>${spans[i]}</code>`);
}

/* TeX -> HTML, via the KaTeX vendored in web/vendor/katex. Never throws: a bad
   formula shows as its own source in a code span rather than taking the whole
   message down with it, and a page served without KaTeX (or the headless
   renderer test, which evaluates this section with no window) degrades to the
   same literal source. */
function renderMath(m) {
  const K = (typeof katex !== 'undefined' && katex)
         || (typeof window !== 'undefined' && window.katex);
  if (!K) return `<code class="math-raw">${esc(m.src)}</code>`;
  try {
    return K.renderToString(m.src, {
      displayMode: m.display,
      throwOnError: false,      // renders the offending bit in red, keeps going
      strict: false,            // a lab answer is not a LaTeX lint target
      // KaTeX paints a rejected fragment with an inline colour and no class, so
      // a stylesheet cannot reach it. Hand it the theme's own variable instead,
      // or the error is #cc0000 on both themes and near-invisible on the dark one.
      errorColor: 'var(--err)',
      trust: false,             // no \href / \url injecting links from model output
      maxExpand: 1000,          // a macro bomb must not hang the tab
    });
  } catch (e) {
    return `<code class="math-err" title="${esc(String((e && e.message) || e))}">${esc(m.src)}</code>`;
  }
}

function renderMd(src) {
  // Strip control characters first: the block sentinels below are \u0000,
  // \u0001, \u0002 and \u0003, and a tool echoing raw bytes back through the
  // model must not be able to forge one. Done before extraction, so it cannot
  // eat the sentinels we insert immediately afterwards.
  src = String(src).replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, '');

  // An unbalanced fence means we're mid-stream inside a code block; close it so
  // the partial block still renders as code instead of as prose.
  if (((src.match(/^```/gm) || []).length) % 2) src += '\n```';

  const blocks = [];
  src = src.replace(/```([\w+#.-]*)[ \t]*\n?([\s\S]*?)```/g, (_, lang, code) =>
    `\u0000${blocks.push({ lang, code }) - 1}\u0000`);

  // --- math -----------------------------------------------------------------
  // After the fences (so $ inside a code block is never a formula) and before
  // esc() and inlineMd (so * and _ in an exponent are not eaten as emphasis --
  // the bug where $a^*b^*$ silently became italics).
  //
  // Inline code is parked first for the same reason a fence is: `echo $PATH and
  // $HOME` is a shell line, not a formula. It is handed straight back before
  // inlineMd runs, so code spans still belong to inlineMd exactly as before.
  const spans = [];
  src = src.replace(/`[^`\n]+`/g, (m) => `\u0003${spans.push(m) - 1}\u0003`);

  const maths = [];
  const stash = (body, display) => `\u0002${maths.push({ src: body, display }) - 1}\u0002`;

  // Mid-stream: an odd number of $$ means the closing pair has not arrived yet.
  if (((src.match(/\$\$/g) || []).length) % 2) src += '$$';

  src = src
    .replace(/\$\$([\s\S]+?)\$\$/g, (_, b) => stash(b.trim(), true))      // $$ ... $$
    .replace(/\\\[([\s\S]+?)\\\]/g, (_, b) => stash(b.trim(), true))      // \[ ... \]
    .replace(/\\\(([\s\S]+?)\\\)/g, (_, b) => stash(b.trim(), false))     // \( ... \)
    // $ ... $ is the risky one: a lone $ is money, a prompt, a shell variable.
    // So the open must not be followed by a space or a digit, the close must not
    // be preceded by a space, neither may be escaped, and the body may not span
    // a blank line. Anything that fails those stays literal text.
    .replace(/(?<![\\$\w])\$(?![\s$\d])((?:[^$\\\n]|\\[\s\S])+?)(?<![\s\\])\$(?!\$|\w)/g,
             (m, b) => (/^[\d\s.,]*$/.test(b) ? m : stash(b, false)));

  src = src.replace(/\u0003(\d+)\u0003/g, (_, i) => spans[i]);

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

    // Display math alone on a line is a block: KaTeX emits a <div> for it, and a
    // <div> inside the <p> this would otherwise join gets torn out by the parser.
    if ((m = t.match(/^\u0002(\d+)\u0002$/)) && maths[+m[1]].display) {
      flush(); endList(); out.push(t); continue;
    }

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

  return out.join('\n')
    .replace(/\u0000(\d+)\u0000/g, (_, i) => {
      const b = blocks[i];
      return `<pre data-lang="${esc(b.lang || '')}"><button class="copy" title="copy">⧉</button>` +
             `<code>${esc(b.code.replace(/\n$/, ''))}</code></pre>`;
    })
    .replace(/\u0002(\d+)\u0002/g, (_, i) => renderMath(maths[i]));
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

/* The other per-turn record: where this turn's output is being written.

   S.turn's sibling. newThink() has been a factory since it was written and
   this one was three copies of the same object literal — turn_start, and the
   two mid-turn paths that have to invent a record because the page attached
   after the turn began. Adding a field meant finding all three, and the one
   you missed would read `undefined` only on a mid-turn reload. */
function newTurn() {
  return {
    textEl: null,    // the .body the answer streams into
    reasonEl: null,  // the open thinking block, if one is showing
    tools: new Map() // tool id -> its slab, so a result can find its call
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
  // Into that block's *summary*, since the summary is the part that survives
  // folding — what a think cost is exactly what you want from a shut block.
  const on = S.turn && S.turn.reasonEl;
  if (on) {
    const tag = node('thought', esc(text));
    (on.closest('details.think')?.querySelector('summary') || on).appendChild(tag);
  } else {
    slab('thought', esc(text));
  }
}

/* Auto-scroll is an *intent*, not a measurement. `follow` is true while the
   user wants to ride the bottom; it flips only when they actually scroll —
   up past FOLLOWS to read (off), back to the bottom (on). Content growth never
   touches it, so a single update that grows the log more than the old 80px
   atBottom() allowance (a tall question, a round ending mid-batch) can no
   longer read "not at bottom" and freeze the follow for the rest of the
   session.

   Four sites write it, and only the first is reading the view:

     the scroll listener below  the user scrolled; measure where they are
     submit()                   this tab asked, so this tab follows the answer
     turn_start                 the same intent, arriving as a broadcast, so
                                it is gated on S.weAsked
     resetView()                there is nothing on screen to be scrolled up
                                in, and the next thing to arrive is the top

   Keep this list honest. It was wrong for as long as `submit()` was missing
   from it, which is the state someone reads while debugging a scroll bug, and
   `test_webui_shape.py` now fails if the sites and the list disagree. */
let follow = true;
const FOLLOWS = 80;
log.addEventListener('scroll', () => {
  follow = atBottom();
  updateNavSoon();    // the arrows' idea of where you are moves with the view
});

/* Room to scroll past the last message.

   The prompts in the final screenful have nothing below them to scroll
   against, so without this they can never be brought to the top line: ↑ either
   leaves the view still and looks dead, or steps over them and leaves them
   unreachable for the rest of the session. A tail of blank under the log
   removes that clamp, and removing it is what lets promptIndex() below be
   plain geometry — the prompt on the top line is the one you jumped to, so
   nothing has to remember the jump.

   Sized to what the newest prompt needs and no more: a turn already taller
   than the pane gets a tail of zero, which is the common case for a long
   answer and leaves the bottom of the log exactly as it was. */
/* The top line: the log's padding edge, which is where a block comes to rest
   once the view is scrolled to it — not the border edge, which sits `padding`
   px higher. Reading, sizing and jumping all measure from here, because one
   ruler is the whole reason the geometry can be trusted: measure the reading
   from the border edge and every landing reports the prompt *before* the one
   you jumped to, the full 18px of padding being more than the 1px tolerance. */
function topLine() {
  return log.getBoundingClientRect().top
       + parseFloat(getComputedStyle(log).paddingTop || 0);
}

let tail = 0;
function retail() {
  const els = userPrompts();
  const last = els[els.length - 1];
  // Where the view would have to sit to put `last` on the top line, in content
  // coordinates — which do not move when the log scrolls, rect.top falling as
  // scrollTop rises by the same px.
  const want = last ? log.scrollTop + last.getBoundingClientRect().top - topLine()
                    : 0;
  const grown = Math.max(0, Math.round(want + log.clientHeight
                                       - (log.scrollHeight - tail)));
  if (grown === tail) return;    // writing the variable every frame thrashes layout
  tail = grown;
  log.style.setProperty('--tail', tail + 'px');
}

/* The bottom of what was actually said, which is no longer the bottom of the
   scroll box — that now runs `tail` px further into the blank. Seven sites
   spelled this `log.scrollTop = log.scrollHeight` and would ride into the
   tail, parking the newest text at the top of the pane mid-stream. */
function toBottom() {
  retail();
  log.scrollTop = log.scrollHeight - tail - log.clientHeight;
}

/* An image settles its height only once it has decoded, which is always after
   the append that placed it scrolled the log. Without this the bottom of the
   view lands mid-plot and stays there. Capture phase, because `load` from an
   <img> does not bubble. A broken one is swapped for its alt text so a missing
   file reads as a missing file rather than a silent gap. */
log.addEventListener('load', (e) => {
  if (e.target.tagName === 'IMG' && follow) toBottom();
}, true);
log.addEventListener('error', (e) => {
  const img = e.target;
  if (img.tagName !== 'IMG' || img.dataset.failed) return;
  img.dataset.failed = '1';
  const miss = document.createElement('span');
  miss.className = 'md-img-missing';
  miss.textContent = `🖼 ${img.getAttribute('alt') || 'image'} — not found`;
  // The alt, not the src: imgTag puts ?token= in the URL, and the rest of the
  // file goes out of its way to keep that out of anything the user can see and
  // paste (see the lightbox note below). A hover tooltip is exactly that.
  miss.title = img.getAttribute('alt') || '';
  // Remembered under the src so the next repaint re-adopts the notice rather
  // than the <img> that produced it; otherwise a streaming message reinstates
  // the broken image every 60ms and this handler runs again, and again.
  if (img._kept) img._kept.set(img.getAttribute('src'), miss);
  img.replaceWith(miss);
}, true);

/* While a replay is building the log, every append() scrolls and every pin
   re-measures — both read layout, so the browser reflows once per replayed
   message and a few hundred of them take visible seconds on a cold switch.
   Nothing is on screen until the replay finishes anyway, so suspend both and
   do each exactly once at the end. */
let batching = false;
function batch(fn) {
  batching = true;
  try { fn(); } finally { batching = false; }
  layoutPins();
  retail();          // the replay just changed what the last prompt is
  updateNav();       // a replayed session has a whole history to point at
  if (follow) toBottom();
}

function append(el) {
  log.insertBefore(el, waitEl);          // the spinner stays last
  trimLog();
  if (follow && !batching) toBottom();
  updateNavSoon();   // a new prompt changes what the arrows can reach
  return el;
}

/* Cap the log.

   Nothing trimmed it, so a session left open all day grows without bound: every
   message, every tool slab and every decoded image stays in the document, and
   the layout reads append() and layoutPins() do get slower in step with it.
   What is dropped here is only the *view* — the session transcript is the
   record, and switching back to the session replays it. */
const LOG_MAX = 1500;

/* The direct children of #log that the turn in flight is still writing into.
   Trimming stops at the oldest of them: a single enormous turn must never have
   the element its own deltas are appending to pulled out from under it. */
function liveTops() {
  const tops = new Set();
  if (!S.turn) return tops;
  const add = (el) => {
    while (el && el.parentElement && el.parentElement !== log) el = el.parentElement;
    if (el && el.parentElement === log) tops.add(el);
  };
  add(S.turn.textEl);
  add(S.turn.reasonEl);
  for (const t of S.turn.tools.values()) add(t);
  return tops;
}

function trimLog() {
  let n = log.childElementCount;
  if (n <= LOG_MAX) return;
  const live = liveTops();
  let droppedPin = false;
  let el = log.firstElementChild;
  while (el && n > LOG_MAX) {
    const next = el.nextElementSibling;
    if (el === waitEl || live.has(el)) break;
    if (el.classList.contains('pin')) droppedPin = true;
    el.remove();
    n -= 1;
    el = next;
  }
  // The stack is offset by the measured height of the pins above each one, so
  // losing one off the top leaves every pin below it sitting too low.
  if (droppedPin) layoutPins();
}

/* Ride the bottom, at most once a frame.

   Reading scrollHeight forces the browser to lay out the whole log, so doing it
   per token — which the reasoning path did, being the one path with no buffer
   in front of it — reflows thousands of times across a single long think. Once
   a frame looks identical and costs one reflow per 16ms instead of per token. */
let scrollQueued = false;
function scrollSoon() {
  if (scrollQueued) return;
  scrollQueued = true;
  requestAnimationFrame(() => {
    scrollQueued = false;
    // Re-read follow at paint time, not at queue time: the user may have
    // scrolled up in between, and this is the one place that would drag them
    // back down after they did.
    if (follow) toBottom();
  });
}

/* Append plain text to an element without rebuilding what is already there.

   `el.textContent += delta` reads the whole accumulated string back out of the
   DOM and writes a fresh text node in its place for every single token — O(n²)
   over a think that routinely runs to tens of thousands of characters. It also
   flattens anything in the block that is not text, which is how the 🧠 cost
   stamp stampThinking() hangs off a reasoning block would come back as bare
   characters. Appending to the trailing text node touches only what arrived. */
function appendText(el, delta) {
  let tail = el._tail;
  if (!tail || tail.parentNode !== el || tail !== el.lastChild) {
    tail = document.createTextNode('');
    el.appendChild(tail);
    el._tail = tail;
  }
  tail.appendData(delta);
}

/* Keep images alive across a streaming repaint.

   stream() replaces the body's whole innerHTML every 60ms, which destroys and
   recreates every <img> inside it ~16 times a second. A decoded image returns
   from cache fast enough to look stable, but one that is still loading restarts
   its load on each pass and can take the rest of the turn to settle, and one
   that is broken has the missing-image notice below swapped back out for a
   fresh <img> that fails again — so the placeholder strobes for as long as the
   message streams. Re-adopting the node already on screen means each src is
   fetched once, fails at most once, and then stays put. */
function adoptImages(el) {
  const kept = el._imgs || (el._imgs = new Map());
  const placed = new Set();
  for (const img of el.querySelectorAll('img')) {
    const src = img.getAttribute('src');
    if (!src) continue;
    const prev = kept.get(src);
    // `placed` covers one message showing the same src twice: the second
    // occurrence must not steal the node standing at the first.
    if (prev && !placed.has(prev)) { img.replaceWith(prev); placed.add(prev); continue; }
    if (!prev) { img._kept = kept; kept.set(src, img); }
    placed.add(img);
  }
}

function node(cls, html) {
  const el = document.createElement('div');
  el.className = cls;
  if (html !== undefined) el.innerHTML = html;
  return el;
}

/* The left gutter: when a block happened, and where in the session it sits.
   Date over time — 2026-10-13 / 13:49:02 — then the turn, and the round
   inside it: #47 for the question, #47.3 for the third round of work under
   it. Right-aligned, so all three march straight down the left edge. The full
   local time rides in `title` on hover, the pair spelled out under the
   number. Every block the user can refer to — prompt, answer, thinking, tool
   call, tool result — carries one, stamped by the core at the moment it
   happened, so a live turn and its replay read identically.

   Takes the record itself — an event live, a history message on replay —
   because the three fields travel together; a bare ISO string still works for
   the blocks that have only a time. Sessions written before the core stamped
   these show whichever parts they have, and an empty gutter rather than a
   guess when they have none. */
function tsEl(src) {
  const r = (src && typeof src === 'object') ? src : { at: src };
  const el = document.createElement('div');
  el.className = 'ts';
  if (r.at) {
    el.title = r.at;
    el.textContent = r.at.slice(0, 10) + '\n' + r.at.slice(11, 19);
  }
  // Turn 0 is where a session sits before its first question, so a falsy turn
  // is "no turn", not turn zero. Round 0 is the question itself and anything
  // outside a round — those show the turn on its own.
  if (r.turn) {
    const n = document.createElement('span');
    n.className = 'ts-turn';
    n.textContent = `#${r.turn}${r.round ? '.' + r.round : ''}`;
    n.title = r.round ? `turn ${r.turn}, round ${r.round}` : `turn ${r.turn}`;
    el.appendChild(n);
  }
  return el;
}

function msg(kind, who, html, src) {
  const el = node(`msg ${kind}`);
  el.innerHTML = `<div class="who">${esc(who)}</div><div class="body"></div>`;
  el.insertBefore(tsEl(src), el.firstChild);
  if (html !== undefined) el.querySelector('.body').innerHTML = html;
  return append(el);
}

/* The newest user message pins to the top of the log for the whole turn —
   without it, the reasoning and tool output streaming below would scroll it
   off the top and you'd hunt up the log to re-read what you asked. A
   /steering correction pins *in addition*, stacked under the question, so
   what you asked and what you corrected are both in view while the model
   re-plans. pinUser is the primary pin: it clears the group and pins just
   this one, which is what a fresh question wants. addPin keeps the existing
   pins and appends this one, which is what a steering wants. */
function pinUser(el) {
  for (const old of log.querySelectorAll('.msg.user.pin'))
    if (old !== el) unpin(old);
  el.classList.add('pin');
  layoutPins();
}

/* Back to an ordinary message in the flow: the clamp, the expanded state and
   the stacking offset are all things only a pin wears. */
function unpin(p) {
  p.classList.remove('pin', 'long', 'open');
  p.style.top = '';
  p.querySelector('.body')?.removeAttribute('title');
}

function addPin(el) {
  el.classList.add('pin');
  layoutPins();
}

/* Stack the pinned prompts: the first holds at the top, each of the rest is
   offset by the *measured* height of every pin above it. Measured, not
   assumed, because a pin's height changes with the pane width — the question
   re-wraps as the log narrows — so a ResizeObserver re-runs this whenever the
   log's box changes (window resize, sidebar drag, sidebar toggle). */
function layoutPins() {
  // offsetHeight below is a layout read; batch() calls this once when the
  // replay that would otherwise trigger one per message is finished.
  if (batching) return;
  let top = 0;
  for (const p of log.querySelectorAll('.msg.user.pin')) {
    measurePin(p);                      // the clamp decides the height read below
    p.style.top = top + 'px';
    top += p.offsetHeight;
  }
}
if (typeof ResizeObserver !== 'undefined')
  // retail() too: the tail is measured against the pane's height, which is
  // exactly what this observer fires on.
  new ResizeObserver(() => { layoutPins(); retail(); }).observe(log);

/* How much of a pinned prompt stays on screen. Four lines is enough to
   recognise what you asked without the band swallowing the answer. */
const PIN_LINES = 4;

/* Mark a pin that overflows PIN_LINES. Measured unclamped and in px rather
   than counted in newlines, because a one-line question wraps to three in a
   narrow pane: whether a prompt is "too long" changes with the log's box,
   which is why this rides along with layoutPins and its ResizeObserver. */
function measurePin(p) {
  const body = p.querySelector('.body');
  if (!body) return;
  p.classList.remove('long');           // read the full height, not the clamped one
  const cs = getComputedStyle(body);
  const lh = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.55;
  const pad = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
  // clientHeight is the padding box — the same box `pad` was measured in.
  if (body.clientHeight > lh * PIN_LINES + pad + 1) {
    p.classList.add('long');
    body.title = p.classList.contains('open') ? 'Click to collapse' : 'Click to expand';
  } else {
    p.classList.remove('open');         // it fits; nothing left to have opened
    body.removeAttribute('title');
  }
}

/* Prompt history arrows, next to Send.

   Scroll-to, not recall-to: ↑ lands on the previous question in the log, ↓ on
   the next. Recall (↑ in an empty box, above) puts the old text back in the
   composer; these take you back to where it was asked and answered, with the
   answer still below it. Both are useful and neither is the other.

   The index is read from the DOM, not kept in a JS array: every block the core
   stamps already sits in the log in order, and an array would need rebuilding
   at all three sites that fill the log (live, replay, session switch) and go
   stale at each. Steering interjections are excluded — a 📌 mid-turn correction
   is not a question you would want to land on.

   The limit is the *view*, not the session: LOG_MAX trims what is rendered, so
   ↑ stops at the oldest prompt still on screen. Switching back to the session
   replays the whole transcript, so nothing is lost, only out of reach. */
function userPrompts() {
  return [...log.querySelectorAll('.msg.user:not(.steering)')];
}

/* Where we are: the last prompt whose top has reached the top line.

   Read off the log every time, with nothing remembered between presses. That
   is only honest because of the tail retail() keeps under the log — every
   prompt can be scrolled to the line, so the prompt sitting on it is always
   the one the last press chose. An earlier version recorded the jump and the
   scroll position it was made from, and invalidated the pair whenever the view
   moved; all of that existed to paper over prompts that could not reach the
   line, and none of it is needed once they can. */
function promptIndex() {
  const els = userPrompts();
  if (!els.length) return { els, idx: -1 };
  const edge = topLine();
  let idx = -1, resting = false;
  for (let i = 0; i < els.length; i++) {
    const d = els[i].getBoundingClientRect().top - edge;
    if (d <= 1) { idx = i; resting = d >= -1; }
  }
  // A prompt resting *on* the line is there because a press put it there, and
  // that reading is exact wherever in the log it happens to sit. Only when
  // nothing is on the line does riding the bottom mean the newest question —
  // the case a short final answer leaves below the line, where the scan above
  // would otherwise point several questions further back.
  //
  // Position alone cannot tell these apart: a prompt jumped to near the end of
  // the log can land inside FOLLOWS of the bottom, and reading that as "newest"
  // is how ↑ would bounce back to the last question part-way up the log.
  if (!resting && atBottom()) return { els, idx: els.length - 1 };
  return { els, idx };
}

function atBottom() {
  // Past the end still counts: reaching one of the newest prompts means
  // scrolling beyond the last message into the tail, and that is not "scrolled
  // up to read", which is the distinction `follow` is asking about.
  return log.scrollHeight - tail - log.scrollTop - log.clientHeight < FOLLOWS;
}

/* Bring `el` to the top of the pane. The padding edge, not the border edge:
   that is where a block actually sits when the log is scrolled to it.

   No allowance for the pinned band, which is what the first version of this
   did: the newest prompt is the pinned one, so every prompt ↑ can reach is
   above it, and scrolling to any of them carries the pin's own flow position
   below the top of the pane — where sticky releases it and it goes back to
   being an ordinary block in the text. Measured: with the compensation and
   without it, the landing is identical. */
function scrollToPrompt(el) {
  log.scrollTop += el.getBoundingClientRect().top - topLine();
}

/* ↑ / ↓ one question. One step, every time — the tail under the log means the
   step can always be taken, so there is nothing to walk past and no end-of-log
   fallback to reach for. Past the last prompt in either direction, take the
   end of the log, which is what "the next one" means once you are out of
   questions. */
function goPrompt(step) {
  const { els, idx } = promptIndex();
  if (!els.length) return;
  // At scrollTop 0 nothing has reached the top line, so idx is -1 — a real
  // reading, not a miss. Clamping it into range would send ↑ *down* the log.
  if (idx < 0 && step < 0) return;
  const to = idx + step;
  if (to < 0) log.scrollTop = 0;
  else if (to >= els.length) toBottom();
  else scrollToPrompt(els[to]);
  updateNav();
}

/* The scroll listener owns `follow` and nothing here writes it: landing mid-log
   puts the view past FOLLOWS of the bottom, which turns following off and lets
   the stream run on underneath without dragging you back into it. Land on the
   newest prompt and it stays on, which is also right. */
let navQueued = false;
function updateNavSoon() {
  if (navQueued) return;
  navQueued = true;
  requestAnimationFrame(() => { navQueued = false; retail(); updateNav(); });
}

function updateNav() {
  // The view, not the index: at either end goPrompt() takes the end of the
  // log, so "can still scroll that way" is exactly "a press will do
  // something". Neither button is ever live and dead.
  $('#nav-up').disabled = log.scrollTop <= 0;
  $('#nav-down').disabled = atBottom();
}

/* Click the bubble to open a clamped prompt, click again to put it back.
   Ignored when the click ended a drag-select: highlighting the question to
   copy it is not a request to fold it away. */
log.addEventListener('click', (e) => {
  const body = e.target.closest?.('.msg.user.pin.long > .body');
  if (!body) return;
  if (window.getSelection()?.toString()) return;
  const open = body.parentElement.classList.toggle('open');
  body.title = open ? 'Click to collapse' : 'Click to expand';
  layoutPins();                         // the stack below it just moved
});

/* Two elements, not one: the outer div does the positioning that lines a slab up
   under the message bodies, the inner one is the visible box. .tool and .roll set
   their own padding, and on a single element that shorthand would wipe out the
   50px gutter offset. Callers get the inner box, so `.slab.roll button` and the
   like still match as descendants. */
function slab(cls, html, src) {
  const outer = node(`slab ${cls}`);
  outer.appendChild(tsEl(src));
  const sp = document.createElement('div');
  sp.className = 'ts-spacer';
  outer.appendChild(sp);
  outer.appendChild(node('slab-box', html));
  append(outer);
  return outer.querySelector('.slab-box');
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
    el.innerHTML = renderMd(el._raw);
    adoptImages(el);
    scrollSoon();
  }, 60);
}

function flushStream(el) {
  if (!el) return;
  el._pending = false;
  el.innerHTML = renderMd(el._raw || '');
  adoptImages(el);
  // A round can end mid-batch: the last chunk is still in the 60ms buffer, so
  // this re-render grows the log with no stream tick to re-pin. Do it here, or
  // the next turn_start reads the view as "scrolled up" and never follows.
  // Synchronous, not scrollSoon(): turn_start can land in the same task as this
  // one, and it reads the view to decide whether to keep following.
  if (follow) toBottom();
  // Models routinely emit a bare "\n\n" before calling a tool. Rendered, that is an
  // avatar next to nothing; drop the whole message rather than leave a stray 🐱.
  if (!(el._raw || '').trim()) el.closest('.msg').remove();
}

/* The turn's open answer and thinking blocks are finished with; whatever comes
   next starts its own.

   What a tool call and a steering interjection both do before they draw
   anything, and the two spellings were character-for-character identical.
   Naming it says what they are doing, which "three statements guarded by an
   if" did not: the next words from the model are a new block, not more of the
   one on screen. */
function closeBlocks() {
  if (!S.turn) return;
  flushStream(S.turn.textEl);
  S.turn.textEl = null;
  S.turn.reasonEl = null;
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
  if (follow) toBottom();
}

function clearWaiting() {
  if (waitTimer) { clearInterval(waitTimer); waitTimer = null; }
  if (waitEl) { waitEl.remove(); waitEl = null; }
}

/* Empty the log and put the view back to how a fresh session starts.

   The three statements always belonged together and were written out four
   times anyway — switching session, `cleared`, `wiped`, `deleted` — and the
   fifth site, hello()'s replay after a session renumbers its events, had only
   the middle one. That is what a copied group costs: the one site that forgets
   a line looks exactly like the four that did not. Scrolled up when the replay
   arrived, you got the whole transcript with following still off and whatever
   spinner was on screen left orphaned above it, because nothing had cleared
   the wait.

   Anything clearing the log wants all three. A spinner outlives the messages
   it belonged to; `follow` is about where the next line goes, and after a wipe
   the only sensible answer is the bottom. */
function resetView() {
  clearWaiting();
  log.innerHTML = '';
  follow = true;
}

/* Copy to the clipboard, saying whether it worked.

   navigator.clipboard exists only on a secure origin. Served over plain http
   from the LAN address config.example.yaml contemplates, it is `undefined`, so
   the copy button threw on every click and still painted a ✓ — the code was
   never copied and the page said it was. The textarea fallback is the pre-
   Clipboard-API route and still works on http; if that fails too, say so. */
async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (err) { /* fall through to the old route */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    // Off-screen rather than hidden: execCommand copies from the selection, and
    // display:none elements cannot hold one.
    ta.style.cssText = 'position:fixed;top:-1000px;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    const did = document.execCommand('copy');
    ta.remove();
    return did;
  } catch (err) {
    return false;
  }
}

log.addEventListener('click', (e) => {
  const btn = e.target.closest('.copy');
  if (btn) {
    const code = btn.parentElement.querySelector('code');
    copyText(code ? code.textContent : '').then((did) => {
      btn.textContent = did ? '✓' : '✗';
      btn.title = did ? '' : 'the browser would not let the page copy';
      setTimeout(() => { btn.textContent = '⧉'; btn.title = ''; }, 1200);
    });
    return;
  }
  // A plot in a chat column is small. Clicking it opens it full size over the
  // page -- cheaper than a new tab, which would have to carry the token in a
  // URL the user can see and paste somewhere it does not belong.
  const img = e.target.closest('img.md-img');
  if (img) showLightbox(img.getAttribute('src'), img.getAttribute('alt'));
});

/* Full-size view of one image. Built on demand and thrown away on close: it
   holds a token-bearing URL, and nothing in the DOM should keep that around
   after it has been looked at. */
function showLightbox(src, alt) {
  const box = document.createElement('div');
  box.className = 'lightbox';
  const big = document.createElement('img');
  big.src = src;
  big.alt = alt || '';
  box.appendChild(big);
  const close = () => { box.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = (ev) => { if (ev.key === 'Escape') close(); };
  box.addEventListener('click', close);
  document.addEventListener('keydown', onKey);
  document.body.appendChild(box);
}

/* ---------------------------------------------------------------- the header */

function setStatus(cls, text) {
  const el = $('#status');
  el.className = `status ${cls}`;
  el.textContent = text;
}

/* The core's version, beside the status pill. The core's and not this page's:
   a tab left open across an upgrade is serving app.js from its own cache, so a
   constant baked in here would report the build you are no longer running.
   Nothing to show until a hello lands, and an older core sends none — hidden
   in both cases rather than reading 'v undefined'. */
function setVersion(v) {
  const el = $('#version');
  el.hidden = !v;
  el.textContent = v ? `v${v}` : '';
  el.title = v ? `letsClaw core ${v}` : '';
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

// The gauge after /clear or /new. Never zero: the window the core just rebuilt
// holds the three behavior layers and the tool schemas, and drawing 0% over it
// reads as the MD files having been dropped. An older core sends no figure, so
// fall back to zero rather than to NaN.
function freshGauge(e) {
  setGauge(e.used || 0, e.budget || S.budget, false);
}

function setBusy(on) {
  S.busy = on;
  $('#stop').hidden = !on;
  $('#send').disabled = on;
}

/* webui.theme from the core. auto leaves <html> unmarked so the OS
   preference decides; dark/light set the attribute the forced palettes in
   style.css key off. */
function applyTheme(t) {
  S.theme = t;
  const root = document.documentElement;
  if (t === 'dark' || t === 'light') root.dataset.theme = t;
  else delete root.dataset.theme;
}

/* The pin's style from the core, as inline custom properties on <html>.
   The /theme.css <link> loads once and the browser will not re-fetch it when
   the server's copy changes, so a tab that is already open keeps styling the
   pin from the stylesheet it loaded — stale after a save. Inline properties
   beat the :root rules in that stylesheet, so the event is the live path.
   hello() applies it on attach; the reloaded handler applies it after a
   save, which is why a change reaches the tab you are watching mid-turn. */
function applyPin(pin) {
  const root = document.documentElement.style;
  if (!pin) return;
  // Clear first: a key dropped from config.yaml must fall back to the
  // stylesheet default, not the value the previous event left here.
  for (const k of ['background', 'transparency', 'font-size', 'font-color'])
    root.removeProperty(`--pin-${k}`);
  for (const [k, v] of Object.entries(pin)) {
    if (v === null || v === undefined) continue;
    root.setProperty(`--pin-${k.replace('_', '-')}`, String(v));
  }
  // font-size re-wraps the pinned prompts: both the clamp and the stack
  // offsets are measured in lines, and neither resizes #log, so the
  // ResizeObserver would not catch this one.
  layoutPins();
}

function setHint(extra) {
  const ro = (S.tripPct ? `rollover at ${S.tripPct}%` : 'rollover off')
           + rolloverCount({ count: S.rollovers });
  $('#hint').textContent =
    `session '${S.session}' · ${S.model || '?'} · ${ro} · thinking ${S.reason}` +
    (extra ? ` · ${extra}` : '');
}

/* --------------------------------------------------------------- the socket */

function authQuery() { return S.token ? `&token=${encodeURIComponent(S.token)}` : ''; }
function authHeaders() { return S.token ? { Authorization: `Bearer ${S.token}` } : {}; }

function connect() {
  clearReconnect();
  // A socket that is open, or still opening, is the one to keep. Without this
  // a stray timer firing next to a deliberate connect() — a reconnect pending
  // when you switch sessions, a tab waking while one is queued — left two
  // live sockets on one page, both attached to the same session, every event
  // rendered twice. The next pair doubled it again.
  if (S.ws && (S.ws.readyState === WebSocket.OPEN
               || S.ws.readyState === WebSocket.CONNECTING)) return;
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
    // Nothing is coming back for these now. Left pending, each one sat on its
    // own five-minute timer and rejected long after the fact — into whatever
    // session the page had switched to by then, so an /info asked for session
    // A surfaced as a timeout against session B.
    rejectRpcs('the link to the core went away');
    // Same reason the pending RPCs go: a submit that was in flight when the
    // socket died has no turn_start coming either, and the flag outlives the
    // reconnect.
    S.weAsked = false;
    if (S.closing) return;
    if (ev.code === 1013) notice('the core dropped this client for falling behind — reconnecting', 'warn');
    // 1012 is SERVICE_RESTART, and the core sends it from one place only. The
    // reconnect below would have brought the page back either way, but with no
    // word on why it went — and a restart someone else triggered is otherwise
    // indistinguishable here from the core dying.
    if (ev.code === 1012) notice('♻️ the core is restarting — reconnecting', 'warn');
    setStatus('off', 'offline');
    scheduleReconnect();
  };

  ws.onerror = () => { /* onclose always follows; report there */ };
}

function clearReconnect() {
  if (S.reconnectTimer !== null) { clearTimeout(S.reconnectTimer); S.reconnectTimer = null; }
}

function scheduleReconnect() {
  // At most one pending, ever. Three places schedule a reconnect — connect()'s
  // own catch, every onclose, and this timer's own retry — and the handle was
  // not kept, so they stacked. See the guard at the top of connect() for what
  // that cost.
  clearReconnect();
  S.reconnectTimer = setTimeout(() => {
    S.reconnectTimer = null;
    if (!S.closing) connect();
  }, S.backoff);
  S.backoff = Math.min(S.backoff * 2, 10000);
}

/* Fail every in-flight command at once. Called when the socket goes away: the
   core answers a command to the asking client only, so a reply to a request
   made on a socket that no longer exists is never coming. */
function rejectRpcs(why) {
  if (!S.rpcs.size) return;
  const pending = [...S.rpcs.values()];
  S.rpcs.clear();
  for (const r of pending) {
    clearTimeout(r.timer);
    if (r.reject) r.reject(new Error(why));
  }
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
    // /rollover summarises with the model before replying, so this waits minutes, not seconds.
    const timer = setTimeout(() => { S.rpcs.delete(request_id); reject(new Error('timed out')); }, 300000);
    // reject is kept too, so rejectRpcs can fail this the moment the socket
    // goes away rather than leaving it to time out five minutes later.
    S.rpcs.set(request_id, { resolve, reject, timer, silent });
  });
}

function switchSession(name) {
  if (!name || name === S.session) return;
  if (S.ws) { S.ws._replaced = true; S.ws.close(); }
  // A reconnect queued for the session being left would otherwise fire after
  // connect() below and open a second socket on the new one.
  clearReconnect();
  // Nothing in flight belongs to the new session; _replaced makes onclose
  // return before it can do this itself.
  rejectRpcs('switched session');
  S.session = name;
  S.lastSeq = 0;
  S.turn = null;
  S.sent = loadSent(name);         // the recall list follows the session, not the tab
  S.histIdx = -1;
  resetView();                 // a fresh session starts at the bottom, following
  history.replaceState(null, '', `?session=${encodeURIComponent(name)}`);
  refreshSessions();          // move the highlight now, don't wait for the poll
  connect();
}

/* ---------------------------------------------------------------- rendering */

function toolSlab(name, args, src) {
  const el = slab('tool', null, src);
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

/* A run of thinking, as a <details> the reader can fold. A model that thinks
   hard writes pages before it says a word, and scrolling back through a
   finished turn meant scrolling past all of it; folded, the block is one line
   that still says the thinking happened and what it cost. S.reason decides
   how a new one arrives, and the reader overrides it per block from there.

   Returns the element the text streams into — not the block — because that is
   what the turn holds on to and appends every delta to. */
function reasonBlock(src) {
  const body = msg('reasoning', '🧠', null, src).querySelector('.body');
  const d = document.createElement('details');
  d.className = 'think';
  d.open = S.reason === 'expanded';
  d.innerHTML = '<summary><span class="think-lead">thinking</span></summary>';
  const text = node('think-text');
  d.appendChild(text);
  body.appendChild(d);
  return text;
}

function handle(e) {
  switch (e.t) {

  case 'hello': return hello(e);

  case 'turn_start':
    S.turn = newTurn();
    think = newThink();
    setBusy(true);
    // Asking is the intent to follow, whatever the view was doing: re-assert
    // it before the question lands. This is also what broke the old cascade —
    // a view left mid-log by a previous round resumes the moment the user
    // asks again.
    //
    // But only for the tab that asked. turn_start is broadcast to every
    // subscriber, so re-asserting it unconditionally meant a question typed
    // in one tab, or in Discord, yanked a second tab back to the bottom while
    // someone was reading halfway up it. The flag is set in submit() and
    // consumed here; a turn started anywhere else leaves this view where its
    // reader put it.
    if (S.weAsked) follow = true;
    S.weAsked = false;
    pinUser(msg('user', '👤', esc(e.text), e));
    setWaiting('thinking');
    return;

  case 'reasoning':
    // Counted before the display check on purpose: with the thinking hidden
    // this counter is the only feedback that the model is still working.
    think.chars += e.delta.length;
    thinkGrew();
    if (!reasonShown()) return;
    if (!S.turn) S.turn = newTurn();
    if (!S.turn.reasonEl) S.turn.reasonEl = reasonBlock(e);
    appendText(S.turn.reasonEl, e.delta);
    scrollSoon();
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
    if (!S.turn) S.turn = newTurn();
    else S.turn.reasonEl = null;   // any further thinking opens its own block
    if (!S.turn.textEl) S.turn.textEl = msg('llm', '🐱', null, e).querySelector('.body');
    stream(S.turn.textEl, e.delta);
    return;

  case 'tool_call': {
    stampThinking();
    clearWaiting();
    closeBlocks();
    const el = toolSlab(e.name, e.arguments, e);
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
      tag.textContent = ` ↳ ${e.size} chars` + (e.at ? ` · ${e.at.slice(11, 19)}` : '');
      el.querySelector('summary').appendChild(tag);
    } else {
      // Attached mid-turn: we never saw the tool_call this answers.
      slab('tool', `⚙️ <b>${esc(e.name)}</b> <span class="done">↳ ${num(e.size)} chars`
        + (e.at ? ` · ${esc(e.at.slice(11, 19))}` : '') + `</span>`, e);
    }
    setWaiting('thinking');
    return;
  }

  case 'steering': {
    // A correction landed mid-turn: the round was interrupted and the model
    // re-plans. Close the open blocks so its next words start fresh, and show
    // the interjection as a user message — it is one, so pinUser pins it.
    stampThinking();
    clearWaiting();
    closeBlocks();
    const st = msg('user', '📌', esc(e.text), e);
    st.classList.add('steering');
    addPin(st);
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
    // The compaction summary itself, not just where it was written: what the
    // fresh window carries forward. Open by default — the user asked to see it.
    const handoff = e.handoff_text
      ? `<details class="handoff" open><summary>🧠 what the new window carries</summary>` +
        `<pre>${esc(e.handoff_text)}</pre></details>`
      : '';
    slab('roll', `✨ New session — ${num(e.used)}/${num(e.budget)} tok` +
                 (paths.length ? `<div class="paths">${paths.join('<br>')}</div>` : '') +
                 handoff);
    separator('fresh context from here');
    if (e.count != null) S.rollovers = e.count;
    setGauge(e.used, e.budget, false);
    setHint();
    return;
  }

  case 'session_state':
    if (e.what === 'cleared') {
      resetView();
      notice('🧹 History cleared (/rollover archives it instead).');
      if (e.record) notice(`📓 what was said is still in ${e.record}`);
      freshGauge(e);
    } else if (e.what === 'wiped') {
      resetView();
      notice(`🔥 Everything wiped — conversation and ${e.files || 0} archive file(s).`);
      freshGauge(e);
    } else if (e.what === 'model') {
      S.model = e.model;
      S.budget = e.budget;
      showModel(e.model);
      notice(`🤖 Switched to ${e.model} — context budget ${e.budget} tok.`);
      setHint();
    } else if (e.what === 'reloaded') {
      // Broadcast to every session the reload touched, not just the one that
      // asked. The budget can have moved under this tab, so the gauge is redrawn
      // against the new one and the dropdown refilled — a model may have been
      // added to or dropped from the file.
      S.model = e.model;
      S.budget = e.budget;
      if (e.theme) applyTheme(e.theme);
      applyPin(e.pin);
      notice(`♻️ Config reloaded — ${e.model}, context budget ${e.budget} tok.`);
      loadModels().then(() => showModel(e.model)).catch(() => {});
      rpc('info', '', true)
        .then((r) => r.info && setGauge(r.info.used, r.info.budget, false))
        .catch(() => {});
      if (isOpen('#settings')) settingsRefresh();   // the panel shows the live config — redraw it
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
      resetView();
      setGauge(0, S.budget, false);
      // Name the archives too. They are the half that used to survive a
      // delete, so saying nothing about them now reads as if they still do.
      const arch = e.files ? ` ${e.files} archived file(s) went with it.` : '';
      notice(`🗑️ Session '${e.session}' was deleted.${arch}`
             + ' Anything you send starts it over, empty.', 'warn');
    }
    refreshSessions();
    return;

  case 'busy':
    clearWaiting();
    setBusy(false);
    // We asked and were turned away, so no turn_start is coming to spend the
    // flag. Left set, the next one — minutes later, from another tab or the
    // Discord bot on the same session — would read it and yank this reader to
    // the bottom.
    S.weAsked = false;
    notice(`⏳ ${e.reason}`, 'warn');
    return;

  case 'notice':
    if (S.busy) clearWaiting();
    notice(e.text, {warn: 'warn', error: 'err'}[e.level] || '');
    if (S.busy) setWaiting('thinking');
    return;

  case 'digest_done': {
    // Same shape as the handoff above, one scale down — but folded shut, where
    // the handoff opens. A rollover is something you asked for and read once;
    // a digest happens at the end of every turn, and a block of it unfolded
    // each time would push the conversation off the screen. The headline is
    // the line the old notice used to be, so nothing is lost by leaving it
    // closed.
    if (S.busy) clearWaiting();
    slab('digest',
      `<details><summary>🗜️ turn ${num(e.turn_id)} collapsed to a ` +
      `${num(e.tokens)} tok digest</summary>` +
      `<pre>${esc(e.digest_text || '')}</pre></details>`, e);
    if (S.busy) setWaiting('thinking');
    return;
  }

  case 'error':
    clearWaiting();
    setBusy(false);
    notice(`❌ ${e.msg}`, 'err');
    return;

  case 'response': return response(e);

  case 'pong': return;
  }
}

function hello(e) {
  if (e.proto !== PROTOCOL_VERSION) {
    notice(`the core speaks protocol ${e.proto}, this page speaks ${PROTOCOL_VERSION} — update one of them.`, 'warn');
  }
  // The core numbers events per session from 1, and a session that was deleted
  // and made again starts over. S.lastSeq only ever goes up, so it stayed above
  // the new numbering for good: every last_seq this page sent then asked for
  // events past a point the core had not reached yet, so `missed` came back
  // empty and `gap` false, and everything the new session said was dropped
  // with nothing on screen to say why. Clearing the log makes the replay below
  // fire, which is how the page catches up.
  //
  // `fresh` is read far below, and is captured here because this branch is
  // about to write to the log. It cleared the log so the replay would fire,
  // then appended its own warning line — so by the time the replay asked
  // "is the log empty?" the answer was no, and the catch-up it had just gone
  // to the trouble of arranging never happened. Measured on the real page:
  // the notice was the only thing on screen afterwards.
  let fresh = log.childElementCount === 0;
  if (typeof e.seq === 'number' && e.seq < S.lastSeq) {
    S.lastSeq = e.seq;
    resetView();
    fresh = true;
    notice('this session restarted its event numbering — replaying it from the top', 'warn');
  }
  S.model = e.model;
  S.budget = e.budget;
  setVersion(e.version);
  // Zeroed before the replay below: attaching to a turn already in flight, we
  // never saw its earlier thinking, so counting on from a previous turn's total
  // would invent tokens. A replayed turn_start resets it again, harmlessly. The
  // core's next reasoning_stat carries the turn's real total, so a mid-turn
  // attach catches up within a quarter-second rather than counting from zero.
  think = newThink();
  S.tripPct = (e.rollover && e.rollover.percent) || 0;
  S.rollovers = (e.rollover && e.rollover.count) || 0;
  S.session = e.session;
  applyTheme(e.theme || 'auto');
  applyPin(e.pin);
  document.title = `${e.session} · letsClaw`;
  // Empty means boot()'s /models fetch never landed. The core is plainly up now, or
  // this event would not be here, so refill the list before selecting in it.
  const selectModel = () => showModel(e.model);
  if ($('#model').options.length) selectModel(); else loadModels().then(selectModel);
  setHint();

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
  batch(() => replayInto(messages));
}

function replayInto(messages) {
  const pending = new Map();
  for (const m of messages) {
    if (m.role === 'user') {
      pinUser(msg('user', '👤', esc(m.content || ''), m));
    // 'assistant' here is the wire role the server stores, not a display name:
    // the chat template switches on that exact string. The bubble is .msg.llm.
    } else if (m.role === 'assistant' && m.tool_calls) {
      // .trim(): a model heading straight for a tool usually still emits "\n\n",
      // which is truthy and would replay as an avatar beside an empty bubble.
      if ((m.content || '').trim()) msg('llm', '🐱', renderMd(m.content), m);
      for (const tc of m.tool_calls) {
        pending.set(tc.id, toolSlab(tc.function.name, tc.function.arguments, m));
      }
    } else if (m.role === 'assistant') {
      if (!(m.content || '').trim()) continue;
      msg('llm', '🐱', renderMd(m.content), m);
    } else if (m.role === 'tool') {
      const el = pending.get(m.tool_call_id);
      if (!el) continue;
      const out = m.content || '';
      const tag = document.createElement('span');
      tag.className = out.startsWith('error:') ? 'fail' : 'done';
      tag.textContent = ` ↳ ${out.length} chars` + (m.at ? ` · ${m.at.slice(11, 19)}` : '');
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
    `⚠️ Context at ${num(e.used)}/${num(e.budget)} (${num(e.percent)}%). ` +
    `Roll over to a new session?` +
    `<div class="paths">no answer in <b class="cd">${num(e.timeout_s)}</b>s keeps it</div>`);
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
  if (e.ok === false) { notice(`❌ ${e.error}`, 'err'); return; }
  if (e.changed) return showReload(e);
  if (e.info) return showInfo(e.info);
  if ('behavior' in e) {
    let md = '';
    if (e.base) md += '**base**\n```markdown\n' + e.base + '\n```\n';
    if (e.behavior) md += '**model**\n```markdown\n' + e.behavior + '\n```\n';
    if (e.session_behavior) {
      md += `**session** (${e.session || 'this session'})\n\`\`\`markdown\n`
          + e.session_behavior + '\n```\n';
    }
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
    `<br><br>${num(e.updated)} session(s) updated` +
    (e.deferred ? `, ${num(e.deferred)} waiting for a turn to finish` : ''));
}

function rolloverCount(ro) {
  if (!ro) return '';
  const n = ro.count || 0;
  return ` · ${num(n)} rollover${n !== 1 ? 's' : ''}`;
}
// What the server was probed to actually do, against what the config assumes.
// Shown even when the two agree: a line that only appears when something is
// wrong is a line nobody learns to read.
function serverLine(s) {
  if (!s) return '';
  const bits = [`thinking=${esc(s.reasoning_channel)}`];
  // Not letsClaw's number: n_ctx as the model server reported it, which is the
  // strongest case in the file for coercing at the boundary.
  if (s.server_ctx) bits.push(`window ${num(s.server_ctx)}`);
  if (s.usage_reported != null)
    bits.push(s.usage_reported ? 'usage reported' : 'usage withheld');
  return `🛰️ ${bits.join(' · ')}<br>` +
    (s.warnings || []).map((w) => `<b>⚠️ ${esc(w)}</b><br>`).join('');
}

function showInfo(i) {
  const pct = i.budget ? Math.round((i.used * 100) / i.budget) : 0;
  const rows = i.recent.map((m) => {
    const emoji = { system: '🤖', user: '👤', tool: '🔧' }[m.role] || '🐱';
    return `${emoji} ${esc(m.role)}: ${esc(m.content)} (${num(m.tokens)} tok)`;
  });
  slab('notice',
    // Omitted rather than blank on a core too old to send it.
    (i.version ? `<b>🧠 letsClaw ${esc(i.version)}</b><br>` : '') +
    `<b>🤖 ${esc(i.model)}</b> @ ${esc(i.base_url)}<br>` +
    // The id as well as the name: archives are filed under it, and it is the
    // half that survives a rename.
    `💬 ${num(i.messages)} messages in session '${esc(i.session)}'` +
    (i.session_id ? ` (id <b>${esc(i.session_id)}</b> — archives are ${esc(i.session_id)}_*)` : '') +
    '<br>' +
    // The ~ marks the estimator's padded guess, and drops away once the server
    // has reported a real prompt_tokens for this window — same convention as
    // the per-turn stats line.
    `⚡ context ${i.estimated === false ? '' : '~'}${num(i.used)}/${num(i.budget)} tok ` +
    `(${num(pct)}%) · ${num(i.tools_tokens)} tok of tool schemas<br>` +
    (i.output_total != null
      ? `📤 ${num(i.output_total)} output tok generated in this session (an odometer — /clear does not rewind it)<br>`
      : '') +
    // "≥" rather than the "~" above: the tilde marks a guess that could land
    // either side, this is a sum with the unreported rounds left out and can
    // only be low. The cache line is omitted entirely when no round ever
    // reported a figure — 0% would be a claim about a server that was silent.
    (i.input_total != null
      ? `📥 ${i.input_exact === false ? '≥' : ''}${num(i.input_total)} input tok sent in this session ` +
        `(every round of every turn — the window is re-sent each time)<br>`
      : '') +
    (i.cache_hit_pct != null
      ? `💾 ${num(i.cache_hit_pct)}% of that served from the server's KV cache (${num(i.cached_total || 0)} tok)<br>`
      : '') +
    (i.rollover.percent
      ? `🔄 rollover ${esc(i.rollover.mode)} at ${num(i.rollover.percent)}% (${num(i.rollover.trip)} tok)${rolloverCount(i.rollover)}<br>`
      : `🔄 rollover disabled (/rollover still works)${rolloverCount(i.rollover)}<br>`) +
    serverLine(i.server) +
    `<br>📜 recent:<br>${rows.join('<br>')}`);
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
  localStorage.setItem(sentKey(S.session), JSON.stringify(S.sent));
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
        .map(([c, d]) => `<b>${esc(c)}</b> — ${esc(d)}`).join('<br>'));
    }
    if (word === '/reasoning') {
      const want = args.trim().toLowerCase();
      if (want && !REASON_MODES.includes(want))
        return notice(`/reasoning takes ${REASON_MODES.join(', ')} — or nothing, to cycle`, 'warn');
      return cycleReasoning(want || undefined);
    }
    if (word === '/stop')      { send({ t: 'stop' }); return; }
    if (word === '/steering') {
      if (!args) {
        input.value = text; autosize();
        return notice('/steering needs text — /steering the right IP is 10.0.0.5', 'warn');
      }
      if (!send({ t: 'steering', text: args })) { input.value = text; autosize(); return; }
      return;
    }
    if (!(word in COMMANDS))   return notice(`unknown command ${word} — /help lists them`, 'warn');

    if (word === '/rollover') setWaiting('archiving and summarising');
    rpc(word.slice(1), args).catch((err) => {
      clearWaiting();
      notice(`${word}: ${err.message}`, 'warn');
    });
    return;
  }

  if (!send({ t: 'submit', text })) return;   // keep the text if we're offline
  // This tab asked, so this tab follows the answer. turn_start is a broadcast
  // — see where it is handled.
  S.weAsked = true;
  follow = true;
  remember(text);
  input.value = '';
  autosize();
}

const REASON_SAID = {
  off: 'hidden',
  collapsed: 'shown, new blocks folded',
  expanded: 'shown, new blocks open',
};

/* The 🧠 button and a bare /reasoning walk the three states in that order;
   /reasoning <mode> jumps straight to one. Blocks already on screen follow
   the new setting: switching to expanded to read the think that just went
   past, and finding it still shut, would be the wrong answer. A block the
   reader opened by hand is overridden too — this is the same deliberate act,
   one level up. */
function cycleReasoning(mode) {
  const i = REASON_MODES.indexOf(S.reason);
  S.reason = mode || REASON_MODES[(i + 1) % REASON_MODES.length];
  localStorage.setItem('letsclaw.reasoning', S.reason);
  showReasonMode();
  for (const d of log.querySelectorAll('details.think')) d.open = S.reason === 'expanded';
  notice(`🧠 thinking ${REASON_SAID[S.reason]}`);
  setHint();
}

/* The button carries its state in the label as well as the colour, because
   two of the three states are lit: ▸ folded, ▾ open, bare when it is off. */
function showReasonMode() {
  const b = $('#btn-reasoning');
  b.classList.toggle('on', reasonShown());
  b.textContent = '🧠 think'
    + (S.reason === 'collapsed' ? ' ▸' : S.reason === 'expanded' ? ' ▾' : '');
  b.title = `the model's thinking: ${REASON_SAID[S.reason]}`
          + ` — click to cycle (/reasoning off|collapsed|expanded)`;
}

/* ------------------------------------------------------------------- wiring */

$('#send').onclick = submit;
$('#nav-up').onclick = () => goPrompt(-1);
$('#nav-down').onclick = () => goPrompt(1);
$('#stop').onclick = () => send({ t: 'stop' });
$('#btn-reasoning').onclick = () => cycleReasoning();
$('#btn-info').onclick = () => rpc('info').catch(() => {});
$('#btn-behavior').onclick = () => rpc('behavior').catch(() => {});

$('#btn-clear').onclick = () => {
  if (confirm('Forget this conversation? The archives are kept — /rollover carries a summary forward instead.')) rpc('clear').catch(() => {});
};
$('#btn-rollover').onclick = () => {
  setWaiting('archiving and summarising');
  rpc('rollover').catch((err) => { clearWaiting(); notice(`/rollover: ${err.message}`, 'warn'); });
};
// The destructive one: it deletes this session's transcripts and journals from
// disk, so it asks, and it says what it is about to take.
$('#btn-new').onclick = () => {
  if (!confirm('Wipe everything?\n\nThis deletes the conversation AND every archived transcript and journal this session has written. It cannot be undone.')) return;
  rpc('new').catch((err) => notice(`/new: ${err.message}`, 'warn'));
};
$('#btn-reload').onclick = () => {
  // Errors come back as ok:false, which response() already renders — a rejected
  // reload has changed nothing, so there is nothing to undo here.
  rpc('reload').catch((err) => notice(`/reload: ${err.message}`, 'warn'));
};
// Asks, like #btn-new does, because it takes everyone's socket down and not
// just this page's — a terminal client attached to the same core has no
// reconnect and will have to be started again.
$('#btn-restart').onclick = () => {
  if (!confirm('Restart the core?\n\nEvery client is disconnected for about a second. Conversations are saved and restored; the Discord bot is a separate process and keeps running.')) return;
  // Not .then-chained onto a second rpc blindly: the core refuses while a turn
  // is running and says which sessions, and that refusal is the one worth
  // putting back to the person rather than overriding for them. A rejection
  // (socket gone) is something else entirely and is reported as itself.
  rpc('restart', '', true).then((reply) => {
    if (reply.ok) return;
    if (!reply.busy) { notice(`❌ ${reply.error}`, 'err'); return; }
    if (confirm(`A turn is running in ${reply.busy.join(', ')}.\n\nRestart anyway? Those turns are lost.`)) {
      rpc('restart', 'force').catch((err) => notice(`/restart: ${err.message}`, 'warn'));
    }
  }).catch((err) => notice(`/restart: ${err.message}`, 'warn'));
};

/* The core is what decides: the dropdown shows the session's model, and only a
   session_state(model) event moves it. A rejected or lost switch — the socket
   down, the core mid-reconnect — used to be swallowed, leaving the control
   naming a model the session was not on and the next turn running on the old
   one with nothing said. Put it back and say so instead. */
$('#model').onchange = (e) => {
  const want = e.target.value;
  if (!want || want === LOST || want === S.model) return;
  // Two ways to fail, and both used to leave the control lying. A refusal comes
  // back as a resolved {ok:false} — response() has already drawn the error, so
  // only put the dropdown back. A dead socket rejects, and nothing else reports
  // that at all, so say it here.
  rpc('model', want)
    .then((r) => { if (r.ok === false) showModel(S.model); })
    .catch((err) => {
      showModel(S.model);
      notice(`could not switch to ${want}: ${err.message}`, 'warn');
    });
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
    // More than one client on a session is the thing that makes a model switch
    // look like it leaked: the other tab is on this same conversation, so it
    // moves too — correctly. The count was already here, but only in the title
    // tooltip, which is to say nowhere. Shown from two up; one is the normal case
    // and a badge on every row would just be noise.
    const ct = node('ct');
    ct.textContent = s.messages || '';
    const eyes = node('eyes');
    eyes.hidden = !(s.clients > 1);
    eyes.textContent = `⧉${s.clients}`;
    eyes.title = `${s.clients} tabs are on this session — a model switch in any of them moves all of them`;
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

    row.append(dot, nm, eyes, ct, ren, del);
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
  // Discards the conversation AND every transcript and journal it archived, so
  // ask first — and say how much is about to go, which is the number that makes
  // people hesitate. The file count is not one of them: the core knows it, the
  // browser does not, so the archives are named rather than counted.
  if (!confirm(`Delete session "${name}"?`
             + (n ? `\n\n${n} message${n === '1' ? '' : 's'} will be discarded,` : '\n\nThis discards the conversation')
             + ' along with every archived transcript and journal this session has written.'
             + '\n\nThis cannot be undone.')) return;
  try {
    const r = await fetch(`/sessions/${encodeURIComponent(name)}`,
                          { method: 'DELETE', headers: authHeaders() });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      notice(`could not delete ${name}: ${d.error || r.status}`, 'warn');
    } else if (name !== S.session) {
      // Only when we were not attached to it: a tab that was gets the same news
      // from the session_state event, and two notices for one click is noise.
      const arch = d.files ? `, with ${d.files} archived file(s)` : '';
      notice(`🗑️ Deleted '${name}'${arch}.`);
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

/* Drag the right edge of the session sidebar to resize it. Pointer events, so it
   works with a mouse or a finger; the width is remembered across reloads. */
{
  const grip = $('#side-grip');
  const side = $('#side');
  let dragging = false;

  grip.addEventListener('pointerdown', (e) => {
    dragging = true;
    grip.setPointerCapture(e.pointerId);
    document.body.classList.add('resizing');
    e.preventDefault();
  });
  grip.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    S.sideW = clampW(e.clientX);   // the sidebar starts at x=0, so the pointer's x is the width
    side.style.width = S.sideW + 'px';
  });
  const end = () => {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove('resizing');
    localStorage.setItem('letsclaw.sideW', String(S.sideW));
  };
  grip.addEventListener('pointerup', end);
  grip.addEventListener('pointercancel', end);
  grip.addEventListener('dblclick', () => {
    S.sideW = SIDE_W_DEF;
    side.style.width = S.sideW + 'px';
    localStorage.setItem('letsclaw.sideW', String(S.sideW));
  });
}

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

/* --------------------------------------------------------------- overlays

   Settings and media are one object with two bodies. index.html says so where
   it builds them — *"Same .set-* box as Settings on purpose"* — and the
   wiring should say it too. A panel is a backdrop holding a box: the sidebar
   button opens it, and the ✕, a click on the backdrop and Escape close it.
   What differs is the body, which is what gets passed in.

   There is no `settingsOpen`/`mediaOpen` pair any more. Each was always
   exactly `!$('#settings').hidden`, written in the same statement as the
   attribute and read in two places that can as easily read the attribute: a
   second copy of a fact the DOM already holds, kept in step by hand at six
   sites. `isOpen()` asks the DOM instead, so there is nothing to keep in step.

   One Escape listener for both, not one each. The backdrop covers the page,
   so at most one panel can be open; the listener closes whichever it is. The
   lightbox keeps its own and should — it is built on demand and takes its
   listener away with it when it goes. */

const isOpen = (sel) => !$(sel).hidden;

const overlays = [];

function overlay({ panel, button, closer, onOpen, onClose }) {
  const close = () => { $(panel).hidden = true; if (onClose) onClose(); };
  $(button).onclick = async () => { $(panel).hidden = false; await onOpen(); };
  $(closer).onclick = close;
  $(panel).addEventListener('click', (e) => { if (e.target === e.currentTarget) close(); });
  overlays.push({ panel, close });
}

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  for (const o of overlays) if (isOpen(o.panel)) o.close();
});

/* --------------------------------------------------------------- settings */

/* Everything in config.yaml, read from GET /config so the panel always shows
   what the core is actually running — no second copy of the schema to drift.
   Read-only except the webui section: a YAML round-trip of the whole file
   would delete every comment in it, and the restart-only keys (core.bind,
   core.port, logging.file, discord.enabled) would save fine and apply
   never — the bot is a separate process, and only the next serve.sh
   reads the flag — which is worse than not offering them. */

const SET_ORDER = ['core', 'models', 'behavior', 'tools', 'conversation',
                   'memory', 'logging', 'webui', 'discord'];
const SET_RESTART = new Set(['core.bind', 'core.port', 'logging.file',
                             'discord.enabled']);
const SET_SECRETS = new Set(['token', 'api_key']);

function deepEq(a, b) {
  if (a === b) return true;
  if (typeof a !== 'object' || typeof b !== 'object' || !a || !b) return false;
  const ka = Object.keys(a), kb = Object.keys(b);
  if (ka.length !== kb.length) return false;
  return ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && deepEq(a[k], b[k]));
}

function cfgEl(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function cfgValue(v) {
  if (v === null || v === undefined) return cfgEl('span', 'cfg-val', '(unset)');
  if (typeof v === 'boolean')
    return cfgEl('span', `chip ${v ? 'on' : 'off'}`, v ? 'true' : 'false');
  if (Array.isArray(v)) {
    const s = cfgEl('span', 'cfg-val');
    for (const item of v) s.append(cfgEl('span', 'chip', String(item)));
    return s;
  }
  return cfgEl('span', 'cfg-val', String(v));
}

function cfgSecret() {
  // There used to be an eye here. The value it revealed no longer arrives:
  // /config replaces core.token, discord.token and every models.*.api_key
  // with a placeholder before the response leaves the core, so a reveal could
  // only ever show the placeholder — and a button that promises the secret and
  // shows "********" is worse than no button. A secret that reaches the page
  // is in devtools, in a HAR export and on screen during a screen-share; this
  // is the one place that can keep it out of all three. The hint says where
  // the real value lives instead of implying the panel is withholding it.
  const wrap = cfgEl('span', 'cfg-secret');
  wrap.append(cfgEl('span', 'dots', '••••••••'),
              cfgEl('span', 'cfg-hint', 'set — in config.yaml'));
  return wrap;
}

function cfgRow(key, v, path) {
  const row = cfgEl('div', 'cfg-row');
  const k = cfgEl('span', 'cfg-key', key);
  if (SET_RESTART.has(path)) k.append(cfgEl('span', 'badge', 'restart'));
  // `&& v` still matters: an unset secret stays "(unset)" rather than claiming
  // to be a value the panel is keeping from you.
  row.append(k, SET_SECRETS.has(key) && v ? cfgSecret() : cfgValue(v));
  return row;
}

function cfgCard(title, map, path) {
  const card = cfgEl('div', 'cfg-card');
  card.append(cfgEl('div', 'cfg-card-title', title));
  for (const [k, v] of Object.entries(map)) {
    if (v && typeof v === 'object' && !Array.isArray(v))
      card.append(cfgCard(k, v, `${path}.${k}`));
    else
      card.append(cfgRow(k, v, `${path}.${k}`));
  }
  return card;
}

let settingsCfg = null;
let setNotice = null;       // {text, ok} — survives a re-render so "saved" is not wiped by it

async function fetchConfig() {
  const r = await fetch('/config', { headers: authHeaders() });
  if (r.status === 401) { location.reload(); return null; }
  if (!r.ok) throw new Error(`/config → ${r.status}`);
  return r.json();
}

function renderSettings(data) {
  settingsCfg = data.config;
  $('#set-path').textContent = data.path;
  const body = $('#set-body');
  body.innerHTML = '';
  const keys = [...SET_ORDER.filter((k) => k in settingsCfg),
                ...Object.keys(settingsCfg).filter((k) => !SET_ORDER.includes(k))];
  for (const key of keys) {
    const val = settingsCfg[key];
    const det = cfgEl('details');
    if (key === 'webui') det.open = true;
    const sum = cfgEl('summary');
    sum.append(cfgEl('span', '', key));
    if (val && typeof val === 'object')
      sum.append(cfgEl('span', 'count', `${Object.keys(val).length} key(s)`));
    if (key === 'webui') sum.append(cfgEl('span', 'editable-tag', 'editable · applies live'));
    det.append(sum);
    if (!val || typeof val !== 'object' || Array.isArray(val)) {
      det.append(cfgEl('div', 'set-rows')).append(cfgRow(key, val, key));
    } else if (key === 'webui') {
      det.append(webuiEditor(val));
    } else {
      const rows = cfgEl('div', 'set-rows');
      for (const [k, v] of Object.entries(val))
        rows.append(v && typeof v === 'object' && !Array.isArray(v)
                    ? cfgCard(k, v, `${key}.${k}`)
                    : cfgRow(k, v, `${key}.${k}`));
      det.append(rows);
    }
    body.append(det);
  }
}

function webuiEditor(val) {
  const rows = cfgEl('div', 'set-rows');
  const form = { theme: String(val.theme || 'auto'),
                 pin: { background: val.pin?.background ?? 'var(--bg)',
                        transparency: val.pin?.transparency ?? 1,
                        font_size: val.pin?.font_size ?? '15px',
                        font_color: val.pin?.font_color ?? 'var(--fg)' } };
  // The form exactly as it was built, which is what "pristine" means. Compared
  // against this rather than against `val`, because the form fills in a default
  // for everything the file leaves out: a config with no `pin:` block read as
  // dirty before the panel had even been looked at, so Save was live on arrival
  // and clicking it wrote those defaults into the file and reloaded the core
  // for nothing.
  const base = { theme: form.theme, pin: { ...form.pin } };

  const themeRow = cfgEl('div', 'cfg-row cfg-edit');
  themeRow.append(cfgEl('span', 'cfg-key', 'theme'));
  const sel = cfgEl('select');
  for (const t of ['auto', 'dark', 'light']) {
    const o = cfgEl('option', '', t);
    o.value = t;
    sel.append(o);
  }
  sel.value = form.theme;
  const themeHint = cfgEl('span', 'set-notice', 'auto follows the OS');
  sel.onchange = () => {
    form.theme = sel.value;
    themeHint.textContent = sel.value === 'auto' ? 'auto follows the OS' : `force ${sel.value}`;
    dirty();
  };
  themeRow.append(sel, themeHint);
  rows.append(themeRow);

  const pinDefs = [['background', 'text', 'pin.background'],
                   ['transparency', 'number', 'pin.transparency'],
                   ['font_size', 'text', 'pin.font_size'],
                   ['font_color', 'text', 'pin.font_color']];
  for (const [k, type, label] of pinDefs) {
    const row = cfgEl('div', 'cfg-row cfg-edit');
    row.append(cfgEl('span', 'cfg-key', label));
    const inp = cfgEl('input');
    inp.type = type;
    inp.value = String(form.pin[k]);
    if (type === 'number') { inp.min = '0'; inp.max = '1'; inp.step = '0.05'; }
    inp.oninput = () => {
      form.pin[k] = type === 'number' ? Number(inp.value) : inp.value;
      dirty();
    };
    row.append(inp);
    rows.append(row);
  }

  const foot = cfgEl('div', 'set-foot');
  const save = cfgEl('button', 'set-save', 'Save');
  const notice = cfgEl('span', 'set-notice');
  if (setNotice) {
    notice.textContent = setNotice.text;
    notice.className = `set-notice ${setNotice.ok ? 'ok' : 'err'}`;
  }
  save.onclick = async () => {
    save.disabled = true;
    notice.className = 'set-notice';
    notice.textContent = 'saving…';
    try {
      const r = await fetch('/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ webui: { theme: form.theme, pin: { ...form.pin } } }),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || !j.ok) {
        setNotice = { text: j.error || `save failed (${r.status})`, ok: false };
        notice.className = 'set-notice err';
        notice.textContent = setNotice.text;
        return;
      }
      setNotice = { text: (j.changed && j.changed.length)
        ? j.changed.join(' · ')
        : 'no change — the file already said this', ok: true };
      notice.className = 'set-notice ok';
      notice.textContent = setNotice.text;
      if (isOpen('#settings')) await settingsRefresh();   // redraw against the live config
    } catch (e) {
      setNotice = { text: String(e), ok: false };
      notice.className = 'set-notice err';
      notice.textContent = String(e);
    } finally {
      // Not `disabled = false`: a save that went through leaves the form
      // matching what was just written, and re-enabling unconditionally
      // invited a second identical POST and a second core reload.
      dirty();
    }
  };
  // A semantic compare, not a string one: the server's key order is the file's,
  // and the form builds its own — a string diff would call an unchanged form
  // dirty (or the reverse) whenever the order differs.
  const dirty = () => { save.disabled = deepEq({ theme: form.theme, pin: { ...form.pin } }, base); };
  dirty();   // pristine on arrival, so Save starts disabled
  foot.append(save, notice);
  rows.append(foot);
  return rows;
}

async function settingsRefresh() {
  try {
    renderSettings(await fetchConfig());
  } catch (e) {
    console.warn('settings refresh failed', e);
  }
}

overlay({
  panel: '#settings', button: '#side-settings', closer: '#set-close',
  onOpen: async () => {
    setNotice = null;
    $('#set-body').innerHTML = '<div class="set-notice">loading…</div>';
    await settingsRefresh();
  },
});

/* ------------------------------------------------------------------- media

   Everything in webui.files_dir, which is where the agent's show_image tool
   puts pictures. The log only holds what was shown and not yet trimmed; this
   is the directory itself, so a plot from a cleared conversation is still here.

   Built on the settings overlay's parts deliberately — same box, same head,
   same close. The only new idea is the grid. */

let mediaData = null;     // the last listing, whole, so the filter can re-render
                          // without a round trip and without losing `truncated`

/* Failures go here and not to notice(): notice() appends into #log, which is
   behind this overlay, so the user would learn nothing until they closed the
   panel they were trying to use. */
function medNotice(text, cls) {
  const body = $('#med-body');
  body.insertBefore(cfgEl('div', `med-note${cls ? ` ${cls}` : ''}`, text), body.firstChild);
}

/* Which pictures this conversation has shown, read off the log when the panel
   opens. A scan rather than a Set kept as messages arrive: five sites do
   log.innerHTML = '' and a Set would need clearing at every one, which is five
   chances to forget. A scan cannot drift.

   The src *attribute*, not the .src property — the property resolves to an
   absolute URL and the prefix strip stops matching.

   Two things it deliberately misses: images trimmed away by LOG_MAX, and the
   detached nodes adoptImages() parks in its _imgs map. Neither is on screen,
   which is exactly what this filter claims to list. */
function sessionImageNames() {
  const out = new Set();
  for (const img of log.querySelectorAll('img.md-img')) {
    const src = img.getAttribute('src') || '';
    if (!src.startsWith('/files/')) continue;
    try { out.add(decodeURIComponent(src.split('?')[0].slice('/files/'.length))); }
    catch { /* a src we did not build; not ours to claim */ }
  }
  return out;
}

const medSize = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB`
                      : n >= 1024 ? `${Math.round(n / 1024)} KB` : `${n} B`);


function renderMedia(data) {
  mediaData = data;
  const all = data.files || [];
  const canDelete = data.can_delete !== false;
  $('#med-path').textContent = data.dir || '';
  const body = $('#med-body');
  body.innerHTML = '';

  const filtering = $('#med-session').checked;
  // Scanned once, not inside the filter: with the listing at its 500 cap that
  // would be 500 passes over the log.
  const onScreen = filtering ? sessionImageNames() : null;
  const shown = filtering ? all.filter((f) => onScreen.has(f.name)) : all;

  // Three empty states, because they are three different problems: nothing has
  // ever been put here, nothing from *this* conversation, or the directory is
  // gone. One "no media" for all three sends you looking in the wrong place.
  if (!shown.length) {
    body.append(cfgEl('div', 'med-empty', filtering && all.length
      ? 'Nothing from this conversation. Untick “this session” to see the rest.'
      : 'Nothing here yet. Ask for a plot — the agent puts pictures here with show_image.'));
    return;
  }

  const grid = cfgEl('div');
  grid.className = 'med-grid';
  for (const f of shown) {
    const tile = cfgEl('div');
    tile.className = 'med-tile';
    tile.dataset.name = f.name;

    // Full images scaled by CSS: there is no thumbnailer and Pillow is not a
    // dependency this core can assume. loading=lazy and the aspect-ratio in
    // the stylesheet are what keep that affordable; the server's cap is the
    // backstop when they are not.
    const img = cfgEl('img');
    img.className = 'med-thumb';
    img.src = fileURL(f.name);
    img.alt = f.name;
    img.loading = 'lazy';
    img.decoding = 'async';
    img.title = `${f.name} — click to view full size`;
    img.onclick = () => showLightbox(img.src, f.name);

    const cap = cfgEl('div');
    cap.className = 'med-cap';
    cap.textContent = f.name;
    cap.title = f.name;

    const foot = cfgEl('div');
    foot.className = 'med-foot';
    const meta = cfgEl('span');
    meta.className = 'med-meta';
    meta.textContent = `${medSize(f.size)} · ${new Date(f.mtime * 1000).toLocaleString()}`;
    foot.append(meta);
    if (canDelete) {
      const del = cfgEl('button');
      del.className = 'med-del';
      del.textContent = '🗑';
      del.title = `delete ${f.name}`;
      del.onclick = () => deleteMedia(f);
      foot.append(del);
    }

    tile.append(img, cap, foot);
    grid.append(tile);
  }
  body.append(grid);

  if (data.truncated) {
    const el = cfgEl('div', 'med-note',
      `Showing the ${all.length} newest of ${data.total} files in this directory.`);
    body.insertBefore(el, grid);
  }
}

async function fetchMedia() {
  const r = await fetch('/files', { headers: authHeaders() });
  if (r.status === 401) { location.reload(); return null; }
  if (!r.ok) throw new Error(`/files → ${r.status}`);
  return r.json();
}

async function mediaRefresh() {
  try {
    const d = await fetchMedia();
    if (d) renderMedia(d);
  } catch (e) {
    $('#med-body').innerHTML = '';
    medNotice(`could not list the media directory: ${e.message}`, 'err');
  }
}

// Native confirm(), like deleteSession() — the project's idiom for "this cannot
// be undone", and a hand-rolled inline one here would be a second idiom for the
// same question.
async function deleteMedia(f) {
  if (!confirm(`Delete ${f.name}?\n\n${medSize(f.size)}, from `
             + `${new Date(f.mtime * 1000).toLocaleString()}.`
             + '\n\nIt is removed from the directory on disk. This cannot be undone.'
             + '\nAny message already showing it will show a broken image.')) return;
  try {
    const r = await fetch(`/files/${encodeURIComponent(f.name)}`,
                          { method: 'DELETE', headers: authHeaders() });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { medNotice(`could not delete ${f.name}: ${d.error || r.status}`, 'err'); return; }
  } catch {
    medNotice(`could not delete ${f.name}: the core is unreachable`, 'err');
    return;
  }
  // Re-fetch rather than splice the tile out: the directory is shared by every
  // session, so what is left is a question only the server can answer.
  await mediaRefresh();
}

overlay({
  panel: '#media', button: '#side-media', closer: '#med-close',
  onOpen: async () => {
    $('#med-body').innerHTML = '<div class="med-note">loading…</div>';
    await mediaRefresh();
  },
  // Emptying #med-body is not housekeeping: every src in it carries the token,
  // for the reason showLightbox gives about itself. It also drops a grid of
  // decoded bitmaps the page has no further use for.
  onClose: () => { $('#med-body').innerHTML = ''; },
});

// Per browser, not per session: it is a way of looking at the panel.
const medSessionBox = $('#med-session');
medSessionBox.checked = localStorage.getItem('letsclaw.mediaSession') === '1';
medSessionBox.onchange = () => {
  localStorage.setItem('letsclaw.mediaSession', medSessionBox.checked ? '1' : '0');
  if (mediaData) renderMedia(mediaData);
};

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
  showModel(S.model || def);
  return true;
}

/* Point the dropdown at the model the session is actually on — always, even when
   that model is not in the configured list.

   The old code only assigned when a matching <option> existed, so a session on a
   model since dropped from config.yaml left the control showing whatever it held
   before: the default, or the model of the session this tab was looking at a
   moment ago. A dropdown that quietly names the wrong model is worse than an
   empty one, because every other surface agrees with it. The stand-in carries
   the real name, is disabled so it cannot be chosen again once left, and is
   rebuilt each time so only one is ever present. */
const LOST = '__lost__';
function showModel(name) {
  const sel = $('#model');
  sel.querySelector(`option[value="${LOST}"]`)?.remove();
  if (!name) return;
  if (!sel.querySelector(`option[value="${CSS.escape(name)}"]`)) {
    const o = document.createElement('option');
    o.value = LOST;
    o.disabled = true;
    o.textContent = `${name} (not in config)`;
    sel.append(o);
    sel.value = LOST;
    return;
  }
  sel.value = name;
}

async function boot() {
  showReasonMode();
  $('#side').hidden = localStorage.getItem('letsclaw.side') === '0';
  $('#side').style.width = S.sideW + 'px';
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
