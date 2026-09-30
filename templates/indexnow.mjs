/**
 * IndexNow submission template from the seo-tune skill.
 * Set HOST, and PUBLIC_DIR if the static folder isn't ../public relative to
 * this script (Next.js: public/, Vite: public/, Astro: public/). Generate the
 * key with `openssl rand -hex 16` and write it (no newline) to
 * <PUBLIC_DIR>/<key>.txt. If the site is not at a domain root, adjust.
 *
 * IndexNow submission — tells Bing (and Yandex, Naver, Seznam, Yep) which
 * pages changed. Submission does not guarantee crawling or indexing.
 * Bing's index also feeds ChatGPT search, Copilot, and DuckDuckGo. Google
 * does not use IndexNow; it still relies on the sitemap and GSC.
 *
 * Run AFTER a deploy is live and `node scripts/smoke.mjs` passes:
 *
 *   node scripts/indexnow.mjs                    # sitemap URLs with lastmod = today
 *   node scripts/indexnow.mjs --since 2026-09-24 # lastmod on or after a date
 *   node scripts/indexnow.mjs --all              # every sitemap URL (use rarely)
 *   node scripts/indexnow.mjs /pricing /features # specific paths
 *   add --dry-run to any of the above to list URLs without submitting
 *
 * It reads the LIVE sitemap, so the lastmod dates are the ones in
 * the sitemap generator at the deployed commit — bump those honestly and this
 * submits only what really changed. Resubmitting unchanged URLs repeatedly
 * is unnecessary; respect rate limits and back off on HTTP 429.
 *
 * The key is public by design: search engines verify ownership by fetching
 * <PUBLIC_DIR>/<key>.txt from HOST. Zero dependencies.
 */

import { readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const HOST = 'example.com'; // TODO: canonical host, no scheme
const BASE = `https://${HOST}`;
const ENDPOINT = 'https://api.indexnow.org/indexnow';

const PUBLIC_DIR = '../public/'; // TODO: static folder served at the site root
const publicDir = fileURLToPath(new URL(PUBLIC_DIR, import.meta.url));
const keyFiles = readdirSync(publicDir).filter((f) => /^[a-f0-9]{32}\.txt$/.test(f));
if (keyFiles.length !== 1) {
  console.error(`Expected exactly one IndexNow key file in public/, found ${keyFiles.length}`);
  process.exit(1);
}
const KEY = keyFiles[0].replace(/\.txt$/, '');
const KEY_LOCATION = `${BASE}/${KEY}.txt`;

const args = process.argv.slice(2);
const dryRun = args.includes('--dry-run');
const all = args.includes('--all');
const sinceIndex = args.indexOf('--since');
const since = sinceIndex >= 0 ? args[sinceIndex + 1] : null;
const paths = args.filter((a) => a.startsWith('/'));

// Local calendar date, matching how the sitemap writes lastmod dates.
const today = new Date().toLocaleDateString('en-CA');

if (since && !/^\d{4}-\d{2}-\d{2}$/.test(since)) {
  console.error('--since needs a YYYY-MM-DD date');
  process.exit(1);
}

let urls;
if (paths.length > 0) {
  urls = paths.map((p) => BASE + p);
} else {
  const res = await fetch(`${BASE}/sitemap.xml`);
  if (!res.ok) {
    console.error(`Could not fetch live sitemap: HTTP ${res.status}`);
    process.exit(1);
  }
  const xml = await res.text();
  const entries = [...xml.matchAll(/<url>\s*<loc>([^<]+)<\/loc>\s*<lastmod>([^<]+)<\/lastmod>/g)]
    .map(([, loc, lastmod]) => ({ loc, lastmod }));
  const cutoff = since ?? today;
  urls = entries.filter((e) => all || e.lastmod >= cutoff).map((e) => e.loc);
}

if (urls.length === 0) {
  console.log(`No sitemap URLs with lastmod on or after ${since ?? today}. Nothing to submit.`);
  process.exit(0);
}

console.log(`${urls.length} URL(s):`);
for (const url of urls) console.log(`  ${url}`);

if (dryRun) {
  console.log('\n--dry-run: nothing submitted.');
  process.exit(0);
}

// A 403 from IndexNow just means "key file not found", which is confusing
// when the real cause is that the deploy with the key isn't live yet.
const keyRes = await fetch(KEY_LOCATION);
const keyBody = keyRes.ok ? (await keyRes.text()).trim() : '';
if (keyBody !== KEY) {
  console.error(`Key file not live at ${KEY_LOCATION} (HTTP ${keyRes.status}). Deploy first.`);
  process.exit(1);
}

const res = await fetch(ENDPOINT, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json; charset=utf-8' },
  body: JSON.stringify({ host: HOST, key: KEY, keyLocation: KEY_LOCATION, urlList: urls }),
});

const MEANING = {
  200: 'submitted',
  202: 'accepted; key validation pending (normal on first use)',
  400: 'bad request',
  403: 'key not valid for this host',
  422: 'URLs do not belong to the host',
  429: 'too many requests; treated as possible spam, back off',
};

const detail = MEANING[res.status] ?? (await res.text()).slice(0, 200);
if (res.status === 200 || res.status === 202) {
  console.log(`\nIndexNow: HTTP ${res.status}, ${detail}`);
} else {
  console.error(`\nIndexNow: HTTP ${res.status}, ${detail}`);
  process.exit(1);
}
