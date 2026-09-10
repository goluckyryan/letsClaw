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
const { esc, renderMd } = new Function(src.slice(start, end) + '\nreturn {esc, renderMd};')();

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

// --- pathological input ------------------------------------------------------
check('lone backtick', renderMd('a ` b'), 'a ` b');
check('empty', renderMd(''), '');
check('only a fence', renderMd('```'), '<pre');

console.log(failed ? `\n${failed} FAILED` : '\nall passed');
process.exit(failed ? 1 : 0);
