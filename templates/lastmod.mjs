/**
 * Sitemap lastmod template from the seo-tune skill.
 *
 * Honest sitemap <lastmod> for a statically built site: the day each page's CONTENT last
 * changed. Without it, lastmod is usually a publish date or missing, and `indexnow.mjs`
 * (which submits sitemap URLs whose lastmod is today) finds nothing after a deploy that
 * changed pages; with a build date on every URL, it resubmits pages that didn't change.
 *
 * Run it right after the static build (chain it into the build script):
 *   node scripts/lastmod.mjs           fingerprint every sitemap page in DIST; a page whose
 *                                      fingerprint changed (or is new) gets today's date in
 *                                      STORE; the dates are written into the built sitemap(s)
 *   node scripts/lastmod.mjs --check   exit 1 while STORE has uncommitted changes (add it to the
 *                                      pre-deploy check, so a deploy always carries its dates)
 *
 * The fingerprint is the page's visible words plus its JSON-LD. Titles, meta descriptions,
 * styles and scripts are left out: a meta-only change doesn't bump lastmod (seo-tune's rule).
 * Commit STORE. The host's build then finds every hash unchanged and keeps the dates; if
 * STORE wasn't committed, the host would re-date changed pages at every build.
 *
 * Set DIST (the build output), STORE (a JSON file in the repo) and SITEMAPS (the built sitemap
 * files holding <url> entries; a sitemap index's children, e.g. Astro's sitemap-0.xml). Pages
 * are looked up as <route>/index.html, then <route>.html. For a server-rendered sitemap
 * (Next.js app/sitemap.ts), read STORE in the sitemap code instead of patching files, and feed
 * this the HTML of a local production build. Zero dependencies; Node 18+.
 */
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import path from 'node:path';

const root = path.resolve(import.meta.dirname, '..'); // the project (or site) folder
const DIST = path.join(root, 'dist'); // Astro: dist; Vite: dist; Eleventy: _site; Hugo: public
const STORE = path.join(root, 'src', 'data', 'lastmod.json');
const SITEMAPS = ['sitemap.xml']; // e.g. ['sitemap-0.xml'] with @astrojs/sitemap

if (process.argv.includes('--check')) {
  const changed = execFileSync('git', ['status', '--porcelain', '--', STORE], { cwd: root, encoding: 'utf8' }).trim();
  if (changed) { console.error(`lastmod: ${path.relative(root, STORE)} has uncommitted changes; commit it with this deploy`); process.exit(1); }
  console.log(`lastmod: ${path.relative(root, STORE)} is committed`);
  process.exit(0);
}

const today = new Date().toLocaleDateString('en-CA'); // the same "today" indexnow.mjs uses
const known = existsSync(STORE) ? JSON.parse(readFileSync(STORE, 'utf8')) : {};
const routeOf = (loc) => new URL(loc).pathname.replace(/\/$/, '') || '/';

function pageFile(route) {
  const tries = route === '/' ? ['index.html'] : [path.join(route, 'index.html'), `${route}.html`];
  const hit = tries.map((t) => path.join(DIST, t)).find((f) => existsSync(f));
  if (!hit) throw new Error(`lastmod: the sitemap lists ${route}, but no built page was found for it`);
  return hit;
}

// a page's content: its JSON-LD and its visible words, without markup, styles or scripts
function content(html) {
  const data = [...html.matchAll(/<script type="application\/ld\+json"[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  const body = (html.split(/<body[^>]*>/)[1] || html)
    .replace(/<(script|style|svg|template|noscript)[\s\S]*?<\/\1>/g, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return JSON.stringify({ data, body });
}

const next = {};
let dated = 0;
const files = SITEMAPS.map((f) => path.join(DIST, f));
for (const file of files) {
  for (const [, loc] of readFileSync(file, 'utf8').matchAll(/<loc>([^<]+)<\/loc>/g)) {
    const route = routeOf(loc);
    const hash = createHash('sha1').update(content(readFileSync(pageFile(route), 'utf8'))).digest('hex').slice(0, 16);
    const was = known[route];
    next[route] = was && was.hash === hash ? was : { hash, date: today };
    if (next[route] !== was) dated++;
  }
}
writeFileSync(STORE, JSON.stringify(next, null, 2) + '\n');

// each page's date after its <loc>, replacing any lastmod the generator wrote
for (const file of files) {
  const xml = readFileSync(file, 'utf8').replace(/\s*<lastmod>[^<]*<\/lastmod>/g, '').replace(/(<loc>([^<]+)<\/loc>)/g, (all, tag, loc) => {
    const date = next[routeOf(loc)]?.date;
    return date ? `${tag}\n    <lastmod>${date}</lastmod>` : tag;
  });
  writeFileSync(file, xml);
}
console.log(`lastmod: ${Object.keys(next).length} pages, ${dated} dated ${today}`);
