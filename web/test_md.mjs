/* Headless check of the markdown renderer in app.js.
 *   node web/test_md.mjs
 * Extracts the markdown section (which touches no DOM) and exercises the cases
 * that actually come out of a coding model: fences mid-stream, tool output with
 * angle brackets in it, tables, and prose that merely looks like markup. */

import { readFileSync } from 'node:fs';

const src = readFileSync(new URL('./app.js', import.meta.url), 'utf8');
const start = src.indexOf('function esc(s)');
const end = src.indexOf('/* ------------------------------------------------------------------- the log */');
if (start < 0 || end < 0) throw new Error('could not find the markdown section');
const S = { token: '' };   // app.js's state object; imgTag reads S.token
const { esc, renderMd } = new Function('S', src.slice(start, end) + '\nreturn {esc, renderMd};')(S);

let failed = 0;
const check = (name, got, want) => {
  const ok = want instanceof RegExp ? want.test(got) : got.includes(want);
  if (!ok) { failed++; console.log(`FAIL  ${name}\n  got:  ${got}\n  want: ${want}`); }
  else console.log(`ok    ${name}`);
};
const reject = (name, got, bad) => {
  const hit = bad instanceof RegExp ? bad.test(got) : got.includes(bad);
  if (hit) { failed++; console.log(`FAIL  ${name}\n  got: ${got}\n  must not contain: ${bad}`); }
  else console.log(`ok    ${name}`);
};

// --- escaping: tool output is attacker-shaped by definition -----------------
check('script tag escaped', renderMd('<script>alert(1)</script>'), '&lt;script&gt;');
reject('no live script', renderMd('hello <script>alert(1)</script>'), /<script>/);
reject('no img onerror', renderMd('<img src=x onerror=alert(1)>'), /<img/);
check('code block escapes html', renderMd('```html\n<b>hi</b>\n```'), '&lt;b&gt;hi&lt;/b&gt;');
reject('javascript: link not linkified', renderMd('[x](javascript:alert(1))'), /<a /);
check('http link ok', renderMd('[docs](https://example.com/a?b=1&c=2)'),
      '<a href="https://example.com/a?b=1&amp;c=2"');

// --- the regression the sentinel scheme exists for --------------------------
reject('bare number is not a code block', renderMd('there are 5 files in state/'), /<pre/);
reject('bare number is not a code span', renderMd('there are 5 files'), /<code>/);
check('digits survive intact', renderMd('exec returned 137 after 8 s'), 'exec returned 137 after 8 s');

// --- forged sentinels from tool output --------------------------------------
const forged = 'oops \u00003\u0000 and \u00019\u0001 done';   // as if a tool echoed raw bytes
reject('forged NUL sentinel neutralised', renderMd(forged), /<pre/);
reject('forged SOH sentinel neutralised', renderMd(forged), /<code>/);
check('forged sentinel keeps the text', renderMd(forged), 'oops 3 and 9 done');

// --- code -------------------------------------------------------------------
check('fenced block', renderMd('```python\nx = 1\n```'), '<pre data-lang="python">');
check('fence content', renderMd('```python\nx = 1\n```'), '<code>x = 1</code>');
check('unterminated fence still renders as code',
      renderMd('here:\n```bash\nfuser -k -n tcp 8770'), '<pre data-lang="bash">');
check('inline code', renderMd('run `./serve.sh` first'), '<code>./serve.sh</code>');
reject('no emphasis inside code spans', renderMd('use `a*b*c` here'), /<em>/);
check('code span content preserved', renderMd('use `a*b*c` here'), '<code>a*b*c</code>');

// --- blocks -----------------------------------------------------------------
check('heading demoted one level', renderMd('# Title'), '<h2>Title</h2>');
check('bullets', renderMd('- one\n- two'), '<ul>\n<li>one</li>\n<li>two</li>\n</ul>');
check('numbered', renderMd('1. one\n2. two'), '<ol>');
check('blockquote', renderMd('> quoted'), '<blockquote>quoted</blockquote>');
check('rule', renderMd('---'), '<hr>');
check('bold', renderMd('**loud**'), '<strong>loud</strong>');
check('italic', renderMd('an *aside* here'), '<em>aside</em>');
reject('bullet line is not italic', renderMd('* item one'), /<em>/);

// --- tables -----------------------------------------------------------------
const table = renderMd('| model | ctx |\n|---|---|\n| qwen38 | 262144 |\n| local | 100000 |');
check('table head', table, '<th>model</th>');
check('table body', table, '<td>262144</td>');
check('table closed', table, '</tbody></table>');
reject('pipes in prose are not a table', renderMd('run a | b to pipe'), /<table/);

// --- streaming: every prefix of a real answer must render without throwing ---
const answer = '# Result\n\nThe DAQ reported **412.5 A**. Try:\n\n```bash\nls -la | grep foo\n```\n\n'
             + '| step | ok |\n|---|---|\n| arm | yes |\n\n- check `run.sh`\n- then *retry*\n';
for (let i = 0; i <= answer.length; i++) {
  try { renderMd(answer.slice(0, i)); }
  catch (e) { failed++; console.log(`FAIL  prefix ${i} threw: ${e}`); break; }
}
console.log(`ok    all ${answer.length + 1} streaming prefixes render`);

// --- images -----------------------------------------------------------------
// ![alt](name.png) becomes an <img> pointed at this core's own /files route,
// and nothing else is allowed to become one at all.
check('local image renders', renderMd('![plot](run42.png)'), '<img class="md-img"');
check('image src goes to /files', renderMd('![plot](run42.png)'), 'src="/files/run42.png"');
check('alt preserved', renderMd('![the plot](a.png)'), 'alt="the plot"');
check('/files prefix accepted', renderMd('![p](/files/a.png)'), 'src="/files/a.png"');
check('image is lazy', renderMd('![p](a.png)'), 'loading="lazy"');
check('jpg ok', renderMd('![p](a.jpg)'), '<img');
check('svg ok', renderMd('![p](a.svg)'), '<img');

// Everything below must NOT become an image, and must not become a link either.
reject('remote http image refused', renderMd('![x](http://evil.com/a.png)'), /<img/);
reject('remote image is not a link either', renderMd('![x](http://evil.com/a.png)'), /<a /);
reject('protocol-relative refused', renderMd('![x](//evil.com/a.png)'), /<img/);
reject('https remote refused', renderMd('![x](https://evil.com/a.png)'), /<img/);
reject('path traversal refused', renderMd('![x](../../etc/passwd)'), /<img/);
reject('subdirectory refused', renderMd('![x](sub/a.png)'), /<img/);
reject('data url refused', renderMd('![x](data:image/png;base64,AAAA)'), /<img/);
reject('javascript url refused', renderMd('![x](javascript:alert(1))'), /<img/);
reject('non-image extension refused', renderMd('![x](passwd.txt)'), /<img/);
reject('no extension refused', renderMd('![x](passwd)'), /<img/);
check('a refused image keeps its text', renderMd('![x](http://evil.com/a.png)'),
      '![x](http://evil.com/a.png)');

// Attribute injection through the src or the alt.
reject('quote in src cannot escape the attribute',
       renderMd('![p](a.png" onerror="alert(1))'), /onerror="alert/);
reject('quote in alt cannot escape the attribute',
       renderMd('![" onerror="alert(1)](a.png)'), /onerror="alert/);
check('ampersand in alt escaped once', renderMd('![a & b](p.png)'), 'alt="a &amp; b"');
reject('alt is not double-escaped', renderMd('![a & b](p.png)'), /&amp;amp;/);

// An image is still markdown: the surrounding syntax must survive, and the
// image syntax must not fire where it is not meant to.
check('image beside bold', renderMd('**hot** ![p](a.png)'), '<strong>hot</strong>');
reject('image syntax in a code span is inert', renderMd('use `![p](a.png)` here'), /<img/);
reject('image syntax in a fence is inert',
       renderMd('```md\n![p](a.png)\n```'), /<img/);
check('plain link still works beside images',
      renderMd('[docs](https://example.com) and ![p](a.png)'), '<a href="https://example.com"');
reject('a bare link is not an image', renderMd('[p](a.png)'), /<img/);

// The token rides in the query, because an <img> cannot carry a header.
S.token = 'sekrit';
check('token appended to the src', renderMd('![p](a.png)'), 'src="/files/a.png?token=sekrit"');
S.token = '';
reject('no token, no query', renderMd('![p](a.png)'), /\?token=/);

// --- math -------------------------------------------------------------------
// No KaTeX in this harness (no window, no script tag), so renderMath takes its
// documented fallback: the source in a .math-raw code span. That is exactly
// what we assert -- it proves the extraction found the formula and that the
// page degrades instead of throwing when KaTeX fails to load.
const raw = (s) => `<code class="math-raw">${s}</code>`;

check('inline $..$ extracted', renderMd('the mass $E = mc^2$ here'), raw('E = mc^2'));
check('display $$..$$ extracted', renderMd('$$\\int_0^1 x\\,dx$$'), raw('\\int_0^1 x\\,dx'));
check('\\( .. \\) extracted', renderMd('so \\(a+b\\) then'), raw('a+b'));
check('\\[ .. \\] extracted', renderMd('\\[a+b\\]'), raw('a+b'));

// The bug this whole change fixes: * and _ inside maths were eaten as emphasis.
reject('no emphasis inside maths', renderMd('$a^*b^*$'), /<em>/);
check('stars survive in maths', renderMd('$a^*b^*$'), raw('a^*b^*'));
reject('no emphasis across two formulas', renderMd('$x^*$ and $y^*$'), /<em>/);
check('underscores survive', renderMd('$x_1 + x_2$'), raw('x_1 + x_2'));

// Display maths alone on a line must be its own block: KaTeX emits a <div>,
// and a <div> inside a <p> is torn out by the HTML parser.
reject('display block is not wrapped in a p',
       renderMd('before\n\n$$x=1$$\n\nafter'), /<p>[^<]*<code class="math-raw">x=1/);

// --- maths that is not maths -------------------------------------------------
// $ is money, a shell prompt and a variable far more often than it is TeX.
reject('shell variables are not maths', renderMd('echo $HOME and $PATH now'), /math-raw/);
reject('prices are not maths', renderMd('it cost $5 and $10 total'), /math-raw/);
reject('lone dollar is not maths', renderMd('costs $ 40 per unit'), /math-raw/);
reject('$ in a code span is not maths', renderMd('run `echo $A and $B` here'), /math-raw/);
reject('$ in a fenced block is not maths',
       renderMd('```bash\nfor f in $a $b; do echo $f; done\n```'), /math-raw/);
check('fenced $ stays literal',
      renderMd('```bash\nfor f in $a $b; do echo $f; done\n```'), 'for f in $a $b');
reject('escaped dollars are not maths', renderMd('a \\$x\\$ b'), /math-raw/);

// --- maths is still escaped --------------------------------------------------
// The fallback path must not become an HTML injection hole.
reject('no live tag from maths', renderMd('$<script>alert(1)</script>$'), /<script>/);
check('maths angle brackets escaped', renderMd('$a < b$'), '&lt;');

// --- streaming maths ---------------------------------------------------------
// Every prefix of an answer containing maths must render without throwing --
// including the half-typed '$$' and the unclosed '$'.
const mathAnswer = 'The result is $E = mc^2$, and in full:\n\n'
                 + '$$\\frac{\\partial u}{\\partial t} = \\alpha \\nabla^2 u$$\n\n'
                 + 'with $\\alpha > 0$ and a rate of **412.5 A**.\n';
for (let i = 0; i <= mathAnswer.length; i++) {
  try { renderMd(mathAnswer.slice(0, i)); }
  catch (e) { failed++; console.log(`FAIL  math prefix ${i} threw: ${e}`); break; }
}
console.log(`ok    all ${mathAnswer.length + 1} streaming maths prefixes render`);

// --- pathological input ------------------------------------------------------
check('lone backtick', renderMd('a ` b'), 'a ` b');
check('empty', renderMd(''), '');
check('only a fence', renderMd('```'), '<pre');
check('lone dollar sign', renderMd('a $ b'), 'a $ b');
check('empty maths is left alone', renderMd('$$'), '$');
check('unclosed display maths does not throw', renderMd('$$x = '), '<code');

console.log(failed ? `\n${failed} FAILED` : '\nall passed');
process.exit(failed ? 1 : 0);
