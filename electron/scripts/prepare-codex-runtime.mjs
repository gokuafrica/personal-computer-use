// Package only the pinned Windows runtime and its license, never user auth/config.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const electronDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export function beforePack() {
  const packageRoot = path.dirname(require.resolve('@openai/codex-win32-x64/package.json'));
  const source = path.join(packageRoot, 'vendor', 'x86_64-pc-windows-msvc', 'bin', 'codex.exe');
  const destination = path.join(electronDir, 'codex-runtime');
  fs.mkdirSync(destination, { recursive: true });
  fs.copyFileSync(source, path.join(destination, 'codex.exe'));
  const codexRoot = path.dirname(require.resolve('@openai/codex/package.json'));
  fs.copyFileSync(path.join(electronDir, 'build', 'CODEX-LICENSE'), path.join(destination, 'LICENSE'));
  fs.writeFileSync(path.join(destination, 'version.txt'), JSON.parse(fs.readFileSync(path.join(codexRoot, 'package.json'))).version);
  console.log('[codex-runtime] Staged pinned Codex executable and license; no credentials.');
}
export default beforePack;
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) beforePack();
