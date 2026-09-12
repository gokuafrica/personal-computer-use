'use strict';

// Pure trust-decision helper for renderer IPC. Extracted from main.js so it
// can be unit-tested with plain Node (no Electron runtime needed).
//
// What happened before: WHATWG file URLs on Windows have pathname
// "/C:/Users/..."; the old regex (^\\[A-Za-z]:) expected a BACKslash-led
// string, never matched, and path.normalize("\\C:\\...") fell outside the
// renderer directory, rejecting every trusted renderer on Windows.
//
// Correct approach: new URL() -> protocol check -> fileURLToPath() -> strict
// canonical path containment under the renderer directory.

const { fileURLToPath } = require('url');
const path = require('path');

// Allowed renderer page URLs must be file: URLs whose resource path is
// strictly inside the renderer directory. Mirrors app.isPackaged only for
// computing candidate roots; the decision itself never reads the filesystem.
function trustedRendererPath(urlString, rendererDir) {
  if (typeof urlString !== 'string' || urlString === '') return false;
  if (typeof rendererDir !== 'string' || rendererDir === '') return false;
  let url;
  try {
    url = new URL(urlString);
  } catch {
    return false;
  }
  // Only local bundled pages; http(s), javascript:, chrome:, etc. never pass.
  if (url.protocol !== 'file:') return false;
  let filePath;
  try {
    // fileURLToPath handles drive letters ("/C:/..." -> "C:\...") and UNC
    // forms ("//server/share/..." -> "\\server\share\...").
    filePath = fileURLToPath(url);
  } catch {
    return false;
  }
  if (typeof filePath !== 'string' || filePath === '') return false;

  const base = path.resolve(rendererDir);
  const resolved = path.resolve(filePath);

  // path.relative must stay non-empty (rendererDir itself is not a page),
  // must not escape via .. segments, and must not be absolute (platforms
  // where relative() yields an absolute path).
  const rel = path.relative(base, resolved);
  if (rel === '') return false;
  if (rel.startsWith('..') && (rel === '..' || rel.startsWith('..' + path.sep))) return false;
  if (path.isAbsolute(rel)) return false;
  return true;
}

module.exports = { trustedRendererPath };
