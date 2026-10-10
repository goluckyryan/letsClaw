/* Maths rendering, against the real vendored KaTeX.
 *   node web/test_math.mjs
 *
 * test_md.mjs evaluates the markdown section with no KaTeX on the page, so it
 * only ever exercises the fallback. This one loads web/vendor/katex first and
 * asserts on the HTML KaTeX actually emits -- that the extraction feeds it the
 * right source, that display maths is a block and inline maths is not, that a
 * formula it rejects is coloured rather than thrown, and that nothing in a
 * model's output can turn into a live link. */
import { readFileSync } from 'node:fs';

const root = '/home/ryan/letsClaw/web';
// KaTeX's UMD bundle wants a global; give it one.
globalThis.window = globalThis;
new Function(readFileSync(`${root}/vendor/katex/katex.min.js`, 'utf8'))();
const katex = globalThis.katex || globalThis.window.katex;
if (!katex) throw new Error('KaTeX did not define a global');
console.log('ok    vendored katex loaded, version', katex.version);

const src = readFileSync(`${root}/app.js`, 'utf8');
const start = src.indexOf('function esc(s)');
const end = src.indexOf('/* ------------------------------------------------------------------- the log */');
const { renderMd } = new Function('katex', src.slice(start, end) + '\nreturn {renderMd};')(katex);

let failed = 0;
const check = (n, got, want) => {
  const ok = want instanceof RegExp ? want.test(got) : got.includes(want);
  if (!ok) { failed++; console.log(`FAIL  ${n}\n  got:  ${got.slice(0,400)}\n  want: ${want}`); }
  else console.log(`ok    ${n}`);
};
const reject = (n, got, bad) => {
  const hit = bad instanceof RegExp ? bad.test(got) : got.includes(bad);
  if (hit) { failed++; console.log(`FAIL  ${n}\n  got: ${got.slice(0,400)}`); }
  else console.log(`ok    ${n}`);
};

check('inline renders katex html', renderMd('mass $E = mc^2$ here'), 'class="katex"');
reject('inline is not a display block', renderMd('mass $E=mc^2$ here'), /katex-display/);
check('display renders as katex-display', renderMd('$$\\int_0^1 x\\,dx$$'), 'katex-display');
check('mathml emitted (accessibility)', renderMd('$x^2$'), '<math');
check('exponent becomes msup', renderMd('$x^2$'), 'msup');
check('fraction renders', renderMd('$$\\frac{a}{b}$$'), 'frac-line');
check('greek renders', renderMd('$\\alpha + \\beta$'), 'class="katex"');
check('no fallback span when katex present', renderMd('$x^2$'), 'class="katex"');
reject('fallback not used', renderMd('$x^2$'), /math-raw/);

// throwOnError:false -> a bad macro is coloured, not thrown
let bad;
try { bad = renderMd('$\\notarealmacro{x}$'); console.log('ok    bad macro did not throw'); }
catch (e) { failed++; console.log('FAIL  bad macro threw:', e.message); }
check('bad macro coloured with the theme variable', bad || '', 'color:var(--err)');
check('bad macro shows its source', bad || '', '\\notarealmacro');
reject('a good formula is not error-coloured', renderMd('$x^2$'), /color:var\(--err\)/);

// trust:false -> \href must not become a live link
reject('href in maths is not a live link', renderMd('$\\href{javascript:alert(1)}{x}$'), /<a /);

// display block is not inside a <p>
const blk = renderMd('before\n\n$$x=1$$\n\nafter');
reject('katex-display not nested in p', blk, /<p>(?:(?!<\/p>)[\s\S])*katex-display/);

// prose around maths still renders as markdown
const mixed = renderMd('The **rate** was $412.5$ A:\n\n$$I = \\frac{V}{R}$$\n\n- see `run.sh`');
check('bold survives beside maths', mixed, '<strong>rate</strong>');
check('list survives beside maths', mixed, '<li>');
check('code span survives beside maths', mixed, '<code>run.sh</code>');
check('display maths present in mixed', mixed, 'katex-display');

// the original bug, through the real renderer
reject('no em from $a^*b^*$ with katex', renderMd('$a^*b^*$'), /<em>/);

console.log(failed ? `\n${failed} FAILED` : '\nall passed');
process.exit(failed ? 1 : 0);
