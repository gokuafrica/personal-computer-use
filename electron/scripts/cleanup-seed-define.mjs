// electron-builder `afterAllArtifactBuild` hook: deletes the generated
// electron/build/seed-define.nsh (which contains the real seed key) once all
// artifacts are written, so no key-bearing copy survives a completed build in
// the worktree. Registered in electron/package.json as
// build.afterAllArtifactBuild = "./scripts/cleanup-seed-define.mjs";
// app-builder-lib resolves the string via resolveFunction, dynamically imports
// the .mjs and uses the named export matching the hook name (index.js:56-58).
// Also runnable directly (`node scripts/cleanup-seed-define.mjs`, e.g. wired
// later as an npm postdist) as a belt-and-braces cleanup; it never reads or
// prints the key — only the file path.
//
// The hook intentionally does NOT run when the build itself fails (npm
// postdist hooks likewise only fire on success): a failed build may leave the
// file behind, but it then still holds the current key and the next
// `npm run dist` deterministically overwrites it via the predist hook, while a
// bypassed build fails loudly on the missing/stale file (see
// installer-with-seed-guard.nsh).

'use strict';

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SEED_DEFINE_PATH = path.resolve(HERE, '..', 'build', 'seed-define.nsh');

export function removeSeedDefine(targetPath = SEED_DEFINE_PATH) {
  try {
    fs.unlinkSync(targetPath);
    return true;
  } catch (err) {
    if (err && err.code === 'ENOENT') {
      return false;
    }
    throw err;
  }
}

export function afterAllArtifactBuild() {
  const removed = removeSeedDefine();
  console.log(`[seed-define] ${removed ? 'removed' : 'already absent'}: ${SEED_DEFINE_PATH}`);
  // No additional artifacts to publish.
  return [];
}

export default afterAllArtifactBuild;

const SELF_PATH = fileURLToPath(import.meta.url);
const invokedDirectly =
  process.argv[1] != null &&
  path.resolve(process.argv[1]).replace(/\\/g, '/').toLowerCase() === SELF_PATH.replace(/\\/g, '/').toLowerCase();
if (invokedDirectly) {
  const removed = removeSeedDefine();
  console.log(`[seed-define] ${removed ? 'removed' : 'already absent'}: ${SEED_DEFINE_PATH}`);
}
