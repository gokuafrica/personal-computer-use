// Unit tests for electron/ipc-trust.js (plain Node, no Electron).
// Covers the two exact review-repro URLs (dev + packaged app.asar layout),
// percent-encoding variants, and every rejection class the review required.
// Synthetic usernames/paths only; no real key material anywhere.
import assert from 'node:assert';
import path from 'path';
import { fileURLToPath } from 'url';
import { trustedRendererPath } from './ipc-trust.js';

const IS_WIN = process.platform === 'win32';
const HERE = path.dirname(fileURLToPath(import.meta.url));
let failures = 0;

function check(name, actual, expected) {
  const ok = actual === expected;
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name} (got ${actual})`);
  if (!ok) failures += 1;
}

// ---------- renderer dirs under test ----------

// Dev layout: electron/renderer in this repo.
const DEV_RENDERER_DIR = path.resolve(HERE, 'renderer');

// Packaged layout, synthetic user: renderer dir inside app.asar resources.
// asar paths are plain strings for path purposes.
const PACKAGED_RENDERER_DIR = 'C:\\Users\\TestUser\\AppData\\Local\\Programs\\Personal Computer Use\\resources\\app.asar\\renderer';

// ---------- trusted: true ----------

// Review URL 1 (dev): exact URL from SECURITY-REVIEW.md.
check(
  'dev review URL trusted',
  trustedRendererPath(
    'file:///C:/Users/Anwesh%20Mohapatra/OneDrive/Documents/Everything/repositories/personal-computer-use/security-fixes/electron/renderer/settings.html',
    DEV_RENDERER_DIR
  ),
  true
);

// Review URL 2 (packaged app.asar): exact URL from SECURITY-REVIEW.md with a
// synthetic home dir.
check(
  'packaged app.asar review URL trusted',
  trustedRendererPath(
    'file:///C:/Users/TestUser/AppData/Local/Programs/Personal%20Computer%20Use/resources/app.asar/renderer/settings.html',
    PACKAGED_RENDERER_DIR
  ),
  true
);

// Percent-encoded spaces must decode via fileURLToPath, not stay literal %20.
check(
  'percent-encoded space dev URL trusted',
  trustedRendererPath(
    encodeURI(
      'file:///C:/Users/Anwesh Mohapatra/OneDrive/Documents/Everything/repositories/personal-computer-use/security-fixes/electron/renderer/settings.html'
    ),
    DEV_RENDERER_DIR
  ),
  true
);

check(
  'percent-encoded space packaged URL trusted',
  trustedRendererPath(
    encodeURI(
      'file:///C:/Users/TestUser/AppData/Local/Programs/Personal Computer Use/resources/app.asar/renderer/settings.html'
    ),
    PACKAGED_RENDERER_DIR
  ),
  true
);

// Other renderer pages must also pass (instruction bar, status card, overlay).
for (const page of ['instruction-bar.html', 'status-card.html', 'agent-overlay.html']) {
  check(
    `renderer page trusted: ${page}`,
    trustedRendererPath(
      'file:///C:/Users/TestUser/AppData/Local/Programs/Personal%20Computer%20Use/resources/app.asar/renderer/' + page,
      PACKAGED_RENDERER_DIR
    ),
    true
  );
}

// ---------- trusted: false ----------

check('http URL rejected', trustedRendererPath('http://127.0.0.1:8765/renderer/settings.html', DEV_RENDERER_DIR), false);
check('https URL rejected', trustedRendererPath('https://evil.example/settings.html', DEV_RENDERER_DIR), false);
check('javascript: URL rejected', trustedRendererPath('javascript:alert(1)', DEV_RENDERER_DIR), false);
check('chrome: URL rejected', trustedRendererPath('chrome://settings', DEV_RENDERER_DIR), false);
check('data: URL rejected', trustedRendererPath('data:text/html,hi', DEV_RENDERER_DIR), false);
check('ws: URL rejected', trustedRendererPath('ws://127.0.0.1/renderer', DEV_RENDERER_DIR), false);

// File URL outside renderer: packaged asar root (settings.html at asar root,
// not in renderer/).
check(
  'file URL at asar root rejected',
  trustedRendererPath(
    'file:///C:/Users/TestUser/AppData/Local/Programs/Personal%20Computer%20Use/resources/app.asar/settings.html',
    PACKAGED_RENDERER_DIR
  ),
  false
);

// File URL in a completely different directory.
check(
  'file URL other dir rejected',
  trustedRendererPath(
    'file:///C:/Users/TestUser/Documents/secrets/settings.html',
    PACKAGED_RENDERER_DIR
  ),
  false
);

// Sibling-directory escape (renderer2 vs renderer).
check(
  'sibling renderer-ish dir rejected',
  trustedRendererPath(
    'file:///C:/Users/TestUser/AppData/Local/Programs/Personal%20Computer%20Use/resources/app.asar/renderer2/settings.html',
    PACKAGED_RENDERER_DIR
  ),
  false
);

// Traversal: renderer/../../secrets must be rejected.
check(
  'traversal to secrets rejected',
  trustedRendererPath(
    'file:///C:/Users/TestUser/AppData/Local/Programs/Personal%20Computer%20Use/resources/app.asar/renderer/../../secrets/x.html',
    PACKAGED_RENDERER_DIR
  ),
  false
);

// Traversal encoded (percent-encodings do not bypass the containment check).
check(
  'encoded traversal rejected',
  trustedRendererPath(
    'file:///C:/Users/TestUser/AppData/Local/Programs/Personal%20Computer%20Use/resources/app.asar/renderer/%2e%2e/%2e%2e/secrets/x.html',
    PACKAGED_RENDERER_DIR
  ),
  false
);

// Renderer dir itself (empty relative) is not a page.
check(
  'renderer dir itself rejected',
  trustedRendererPath(
    'file:///C:/Users/TestUser/AppData/Local/Programs/Personal%20Computer%20Use/resources/app.asar/renderer',
    PACKAGED_RENDERER_DIR
  ),
  false
);

// Missing / empty / garbage input.
check('missing URL rejected', trustedRendererPath(undefined, DEV_RENDERER_DIR), false);
check('empty URL rejected', trustedRendererPath('', DEV_RENDERER_DIR), false);
check('garbage string rejected', trustedRendererPath('not a url at all', DEV_RENDERER_DIR), false);
check('plain path string rejected', trustedRendererPath('C:\\some\\path\\settings.html', DEV_RENDERER_DIR), false);

// Missing/garbage rendererDir must fail closed too.
check(
  'missing rendererDir rejected',
  trustedRendererPath('file:///C:/x/renderer/settings.html', undefined),
  false
);
check(
  'empty rendererDir rejected',
  trustedRendererPath('file:///C:/x/renderer/settings.html', ''),
  false
);

// Malformed file URL that fileURLToPath cannot parse (file:///C:/ with an
// invalid percent escape).
check(
  'invalid percent escape rejected',
  trustedRendererPath('file:///C:/Users/TestUser/renderer/%zz/settings.html', PACKAGED_RENDERER_DIR),
  false
);

// Non-file URL that still carries renderer in its path (scheme smuggling).
check(
  'http with renderer path rejected',
  trustedRendererPath(
    'http://localhost/resources/app.asar/renderer/settings.html',
    PACKAGED_RENDERER_DIR
  ),
  false
);

// ---------- platform sanity ----------
if (IS_WIN) {
  // On Windows the dev path really resolves under the user profile; make sure
  // the dev renderer dir sanity-checks against itself using a plain path form.
  const asFileUrl = 'file:///' + encodeURI(DEV_RENDERER_DIR.replace(/\\/g, '/')) + '/settings.html';
  check('win dev round-trip trusted', trustedRendererPath(asFileUrl, DEV_RENDERER_DIR), true);
} else {
  const url = 'file://' + encodeURI(DEV_RENDERER_DIR) + '/settings.html';
  check('posix dev round-trip trusted', trustedRendererPath(url, DEV_RENDERER_DIR), true);
}

console.log(failures === 0 ? 'IPC-TRUST PASS' : `IPC-TRUST FAIL (${failures})`);
if (failures > 0) process.exit(1);
