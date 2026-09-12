import fs from 'fs';
import os from 'os';
import path from 'path';

// Token location mirrors electron/main.js: the backend writes runtime_token
// into the config dir (PCU_CONFIG_DIR env, %APPDATA%\<productName or name>
// when packaged, repo root in dev) at startup and deletes it at shutdown.
export function runtimeTokenPath() {
  const envDir = (process.env.PCU_CONFIG_DIR || '').trim();
  if (envDir) return path.join(envDir, 'runtime_token');
  const appData = process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming');
  const candidates = [
    path.join(appData, 'Personal Computer Use', 'runtime_token'),
    path.join(appData, 'personal-computer-use', 'runtime_token'),
    path.join(process.cwd(), 'runtime_token')
  ];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return candidates[candidates.length - 1];
}

export function readRuntimeToken() {
  const tokenPath = runtimeTokenPath();
  let token = '';
  try {
    token = fs.readFileSync(tokenPath, 'utf8').trim();
  } catch {
    throw new Error(
      `No runtime token at ${tokenPath}. Start the app (or the backend) first — ` +
      'the backend writes the token there at startup and removes it at shutdown.'
    );
  }
  if (!token) {
    throw new Error(`Runtime token file at ${tokenPath} is empty.`);
  }
  return token;
}

export function authHeaders() {
  return { 'X-PCU-Token': readRuntimeToken() };
}
