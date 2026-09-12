// Tests for electron/scripts/gen-seed-define.mjs (and the packaging-cleanup
// hook it pairs with, scripts/cleanup-seed-define.mjs).
// Uses a TEMP synthetic default-config fixture — NEVER the real seed file:
// the script and the cleanup hook are copied into a temp scripts/ layout so
// their inputs and output (build/seed-define.nsh) resolve inside the temp
// fixture tree. Synthetic keys only; the real seed and real key are never
// printed, and the real electron/build directory is left exactly as it was
// found (the one real-seed check below compares in memory only and restores
// the prior state afterwards).
import assert from 'node:assert';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { spawnSync } from 'child_process';
import { fileURLToPath, pathToFileURL } from 'url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SCRIPT = path.join(HERE, 'gen-seed-define.mjs');
const CLEANUP = path.join(HERE, 'cleanup-seed-define.mjs');

let failures = 0;
function check(name, ok, detail) {
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ' (' + detail + ')' : ''}`);
  if (!ok) failures += 1;
}

function makeFixture(seed) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pcu-seed-define-'));
  fs.mkdirSync(path.join(dir, 'scripts'), { recursive: true });
  fs.mkdirSync(path.join(dir, 'build'), { recursive: true });
  fs.copyFileSync(SCRIPT, path.join(dir, 'scripts', 'gen-seed-define.mjs'));
  fs.copyFileSync(CLEANUP, path.join(dir, 'scripts', 'cleanup-seed-define.mjs'));
  if (seed !== null) {
    fs.writeFileSync(path.join(dir, 'default-config.json'), JSON.stringify(seed), 'utf8');
  }
  const outPath = path.join(dir, 'build', 'seed-define.nsh');
  return {
    dir,
    outPath,
    run() {
      const out = spawnSync(process.execPath, [path.join(dir, 'scripts', 'gen-seed-define.mjs')], {
        encoding: 'utf8',
        cwd: dir
      });
      return {
        status: out.status,
        stdout: out.stdout,
        stderr: out.stderr,
        content: fs.existsSync(outPath) ? fs.readFileSync(outPath, 'utf8') : null
      };
    },
    writeSeed(next) {
      fs.writeFileSync(path.join(dir, 'default-config.json'), JSON.stringify(next), 'utf8');
    },
    dispose() {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  };
}

function withFixture(seed, fn) {
  const fixture = makeFixture(seed);
  try {
    return fn(fixture);
  } finally {
    fixture.dispose();
  }
}

// Happy path: section key + keyVersion, plus the staleness sentinel.
withFixture({
  provider: 'openai',
  openai: { api_key: 'sk-synthetic-gen-seed-0123456789abcdef' },
  keyVersion: 3
}, f => {
  const r = f.run();
  check('section key: exit 0', r.status === 0, r.stderr.trim());
  check('section key: PCU_SEED_KEY define present', r.content !== null && r.content.includes('!define PCU_SEED_KEY "sk-synthetic-gen-seed-0123456789abcdef"'));
  check('section key: keyVersion honored', r.content.includes('!define PCU_SEED_KEY_VERSION 3'));
  check('guard sentinel PCU_SEED_KEY_SCHEMA present', r.content.includes('!define PCU_SEED_KEY_SCHEMA 1'));
  check('section key: key never printed', !r.stdout.includes('sk-synthetic-gen-seed'));
});

// Stale file is overwritten with fresh, deterministic content.
withFixture({ openai: { api_key: 'sk-synthetic-gen-seed-stale-old' }, keyVersion: 1 }, f => {
  let r = f.run();
  check('stale overwrite: first generation exit 0', r.status === 0, r.stderr.trim());
  f.writeSeed({ openai: { api_key: 'sk-synthetic-gen-seed-fresh-new' }, keyVersion: 9 });
  r = f.run();
  check('stale overwrite: regeneration exit 0', r.status === 0, r.stderr.trim());
  check('stale overwrite: stale key gone', r.content !== null && !r.content.includes('sk-synthetic-gen-seed-stale-old'));
  check('stale overwrite: fresh key present', r.content.includes('!define PCU_SEED_KEY "sk-synthetic-gen-seed-fresh-new"'));
  check('stale overwrite: version refreshed', r.content.includes('!define PCU_SEED_KEY_VERSION 9'));
  f.writeSeed({ openai: { api_key: 'sk-synthetic-gen-seed-fresh-new' }, keyVersion: 9 });
  const again = f.run();
  check('stale overwrite: regeneration is deterministic', again.status === 0 && again.content === r.content, again.stderr.trim());
});

// Cleanup hook (electron-builder afterAllArtifactBuild) deletes the file.
async function testCleanupHook() {
  const f = makeFixture({ openai: { api_key: 'sk-synthetic-gen-seed-cleanup' } });
  try {
    fs.writeFileSync(f.outPath, '# synthetic stand-in\n!define PCU_SEED_KEY "sk-synthetic-gen-seed-cleanup"\n', 'utf8');
    // Same resolution electron-builder uses: dynamic import of the .mjs copy.
    const cleanupPath = path.join(f.dir, 'scripts', 'cleanup-seed-define.mjs');
    const mod = await import(pathToFileURL(cleanupPath).href);
    check('cleanup module exports named hook', typeof mod.afterAllArtifactBuild === 'function' && typeof mod.removeSeedDefine === 'function');
    check('cleanup hook deletes generated file', mod.removeSeedDefine() === true && !fs.existsSync(f.outPath));
    check('cleanup hook idempotent when absent', mod.removeSeedDefine() === false);
    fs.writeFileSync(f.outPath, '# synthetic stand-in\n', 'utf8');
    const artifacts = await mod.afterAllArtifactBuild({});
    check('afterAllArtifactBuild returns [] and deletes file', Array.isArray(artifacts) && artifacts.length === 0 && !fs.existsSync(f.outPath));
    // default export mirrors electron-builder's named-export fallback.
    check('cleanup default export callable', typeof mod.default === 'function');
  } finally {
    f.dispose();
  }
}
await testCleanupHook();

// Anthropic section preferred over legacy top-level field.
withFixture({
  apiKey: 'sk-synthetic-gen-seed-legacy',
  anthropic: { api_key: 'sk-synthetic-gen-seed-anthropic' }
}, f => {
  const r = f.run();
  check('anthropic section wins over legacy apiKey', r.content.includes('"sk-synthetic-gen-seed-anthropic"'));
});

// beforePack hook contract: the module must be importable WITHOUT generating
// (import side-effect free) and must expose the named beforePack export that
// app-builder-lib resolves (util/resolve.js named-export preference), which
// generates on call.
async function testBeforePackHookModule() {
  const f = makeFixture({ openai: { api_key: 'sk-synthetic-gen-seed-hook' }, keyVersion: 5 });
  try {
    const mod = await import(pathToFileURL(path.join(f.dir, 'scripts', 'gen-seed-define.mjs')).href);
    check('gen module exports named beforePack hook', typeof mod.beforePack === 'function');
    check('gen module exports default beforePack', typeof mod.default === 'function' && mod.default === mod.beforePack);
    check('gen module exports generateSeedDefine', typeof mod.generateSeedDefine === 'function');
    check('importing the module does NOT generate on its own', !fs.existsSync(f.outPath), 'build/seed-define.nsh must only appear when the hook runs');
    await mod.beforePack({});
    check('beforePack hook generates the seed define', fs.existsSync(f.outPath) && fs.readFileSync(f.outPath, 'utf8').includes('!define PCU_SEED_KEY "sk-synthetic-gen-seed-hook"'));
    check('beforePack hook: version define present', fs.readFileSync(f.outPath, 'utf8').includes('!define PCU_SEED_KEY_VERSION 5'));
  } finally {
    f.dispose();
  }
}
await testBeforePackHookModule();

// Cwd-independence: the script must resolve every path from its own module
// location, so electron-builder can run it with any cwd. Run the fixture copy
// with cwd pointed at an UNRELATED temp directory and expect a correct file
// inside the fixture tree (never in the unrelated cwd).
withFixture({ openai: { api_key: 'sk-synthetic-gen-seed-cwdindep' }, keyVersion: 2 }, f => {
  const alienCwd = fs.mkdtempSync(path.join(os.tmpdir(), 'pcu-seed-define-cwd-'));
  try {
    const out = spawnSync(process.execPath, [path.join(f.dir, 'scripts', 'gen-seed-define.mjs')], {
      encoding: 'utf8',
      cwd: alienCwd
    });
    check('cwd-independence: exit 0 with unrelated cwd', out.status === 0, (out.stderr || '').trim());
    const content = fs.existsSync(f.outPath) ? fs.readFileSync(f.outPath, 'utf8') : null;
    check('cwd-independence: output written inside the module-owned tree', content !== null);
    check('cwd-independence: correct synthetic key define', content !== null && content.includes('!define PCU_SEED_KEY "sk-synthetic-gen-seed-cwdindep"'));
    check('cwd-independence: nothing written into the unrelated cwd', fs.readdirSync(alienCwd, { recursive: true }).length === 0, 'cwd must stay untouched');
  } finally {
    fs.rmSync(alienCwd, { recursive: true, force: true });
  }
});

// Static wiring checks: the regeneration hook must be registered so EVERY
// electron-builder invocation (npm or direct) regenerates before packing.
{
  const pkg = JSON.parse(fs.readFileSync(path.join(HERE, '..', 'package.json'), 'utf8'));
  const nsisInclude = pkg.build && pkg.build.nsis && pkg.build.nsis.include;
  check('nsis.include points directly at build/installer.nsh (no shadowed wrapper)', nsisInclude === 'build/installer.nsh');
  check('build.beforePack registered to regenerate the seed define', pkg.build && pkg.build.beforePack === './scripts/gen-seed-define.mjs');
  check('cleanup hook still registered as afterAllArtifactBuild', pkg.build && pkg.build.afterAllArtifactBuild === './scripts/cleanup-seed-define.mjs');
  check('predist still regenerates for npm ergonomics', pkg.scripts && pkg.scripts.predist === 'node scripts/gen-seed-define.mjs');
  const genSource = fs.readFileSync(SCRIPT, 'utf8');
  const genCode = genSource.split('\n').filter(line => !line.trim().startsWith('//')).join('\n');
  check('gen script is cwd-independent (no process.cwd use in code)', !genCode.includes('process.cwd'));
  check('gen script resolves paths from its own module location', genSource.includes('import.meta.url'));
  check('gen script still exposes CLI exit-1 on failure', genSource.includes('process.exit(1)'));
  const installerNsh = fs.readFileSync(path.join(HERE, '..', 'build', 'installer.nsh'), 'utf8');
  check('installer.nsh includes seed-define.nsh itself', installerNsh.includes('!include "seed-define.nsh"'));
  check('installer.nsh hard-errors when sentinel defines are absent', installerNsh.includes('!error') && installerNsh.includes('PCU_SEED_KEY_SCHEMA') && installerNsh.includes('PCU_SEED_KEY') && installerNsh.includes('PCU_SEED_KEY_VERSION'));
  check('no shadowing wrapper remains in scripts/', !fs.existsSync(path.join(HERE, 'installer-with-seed-guard.nsh')));
  // compile-level proof of the guard lives in electron/build/nsis-tests
  // run-provision-tests.ps1 (T7): stale seed-define must fail makensis.
}

// Real-seed freshness proof (NO printing, NO lasting side effects): run the
// REAL script — with cwd inside %TEMP% to also prove real-script
// cwd-independence — and compare the generated PCU_SEED_KEY define against
// the canonical seed read into memory with the same selection rules. The
// pre-existing state of electron/build/seed-define.nsh is restored exactly.
function selectKeyLikeGenerator(seed) {
  for (const section of ['openai', 'anthropic', 'openai_compat']) {
    const sub = seed[section];
    if (sub && typeof sub === 'object' && typeof sub.api_key === 'string' && sub.api_key.trim() !== '') {
      return sub.api_key.trim();
    }
  }
  if (typeof seed.apiKey === 'string' && seed.apiKey.trim() !== '') return seed.apiKey.trim();
  return null;
}
function escapeNsisLikeGenerator(value) {
  return value.replace(/\$/g, '$$$$').replace(/"/g, '$\\"');
}
function testRealSeedRegeneration() {
  const seedPath = path.join(HERE, '..', 'default-config.json');
  const outPath = path.join(HERE, '..', 'build', 'seed-define.nsh');
  let seed = null;
  try {
    seed = JSON.parse(fs.readFileSync(seedPath, 'utf8'));
  } catch {
    seed = null;
  }
  const expectedKey = seed ? selectKeyLikeGenerator(seed) : null;
  if (!expectedKey) {
    check('real seed regen matches canonical seed (in-memory)', true, 'skipped: no canonical seed/key in this checkout');
    return;
  }
  const expectedVersion = typeof seed.keyVersion === 'number' && Number.isFinite(seed.keyVersion) ? seed.keyVersion : 1;
  const priorContent = fs.existsSync(outPath) ? fs.readFileSync(outPath, 'utf8') : null;
  const alienCwd = fs.mkdtempSync(path.join(os.tmpdir(), 'pcu-seed-define-real-'));
  try {
    const out = spawnSync(process.execPath, [SCRIPT], { encoding: 'utf8', cwd: alienCwd });
    check('real seed regen: exit 0 with %TEMP% cwd', out.status === 0, (out.stderr || '').trim());
    // stdout may only carry length/version metadata, never the key itself.
    check('real seed regen: key never printed to stdout', !out.stdout.includes(expectedKey) && !(out.stderr || '').includes(expectedKey));
    const content = fs.existsSync(outPath) ? fs.readFileSync(outPath, 'utf8') : null;
    check('real seed regen: file generated', content !== null);
    const match = content !== null ? content.match(/^!define PCU_SEED_KEY "(.*)"$/m) : null;
    // Key value compared in memory only — on mismatch the expected value is
    // never echoed, only the fact of the mismatch.
    check('real seed regen: PCU_SEED_KEY equals canonical seed value read in memory', match !== null && match[1] === escapeNsisLikeGenerator(expectedKey));
    check('real seed regen: version define matches seed keyVersion', content !== null && content.includes(`!define PCU_SEED_KEY_VERSION ${expectedVersion}`));
    check('real seed regen: schema sentinel present', content !== null && content.includes('!define PCU_SEED_KEY_SCHEMA 1'));
    check('real seed regen: define names complete', content !== null && content.includes('!define PCU_SEED_KEY "') && content.includes('!define PCU_SEED_KEY_VERSION') && content.includes('!define PCU_SEED_KEY_SCHEMA'));
  } finally {
    if (priorContent !== null) {
      fs.mkdirSync(path.dirname(outPath), { recursive: true });
      fs.writeFileSync(outPath, priorContent, 'utf8');
    } else {
      fs.rmSync(outPath, { force: true });
    }
    fs.rmSync(alienCwd, { recursive: true, force: true });
  }
}
testRealSeedRegeneration();

// Default keyVersion = 1 when absent.
withFixture({ openai: { api_key: 'sk-synthetic-gen-seed-defaultver' } }, f => {
  const r = f.run();
  check('default keyVersion is 1', r.content.includes('!define PCU_SEED_KEY_VERSION 1'));
});

// NSIS escaping: $ -> $$, " -> $\"
withFixture({ openai: { api_key: 'sk-synthetic"a$b' } }, f => {
  const r = f.run();
  check('NSIS escaping of $ and "', r.content.includes('!define PCU_SEED_KEY "sk-synthetic$\\"a$$b"'));
});

// Empty seed: refuses (exit 1), nothing generated.
withFixture({ openai: { model: 'x' } }, f => {
  const r = f.run();
  check('empty seed refused with exit 1', r.status === 1);
  check('empty seed: no define written', r.content === null);
  check('empty seed: no blank define sneaked out', !(r.content || '').includes('PCU_SEED_KEY'));
});

// Missing seed file: refuses.
withFixture(null, f => {
  const r = f.run();
  check('missing seed refused with exit 1', r.status === 1);
});

// Non-string / whitespace keys ignored.
withFixture({ openai: { api_key: '   ' }, anthropic: { api_key: 'sk-synthetic-gen-seed-whitespace' } }, f => {
  const r = f.run();
  check('whitespace key skipped, next section used', r.content.includes('"sk-synthetic-gen-seed-whitespace"'));
});

console.log(failures === 0 ? 'GEN-SEED-DEFINE PASS' : `GEN-SEED-DEFINE FAIL (${failures})`);
if (failures > 0) process.exit(1);
