# Security worktree independent review

Date: 2026-09-12  
Reviewed state: uncommitted `security-fixes` worktree based on `be9f4b7c4c8c265daf1cc0a8264326a916304d2a`.

The private installer may contain the dedicated revocable key. The intended family-install result is one DPAPI-protected runtime store, with no installed plaintext seed, updater-cache installer, or key-bearing diagnostics. Repository-bound build inputs are accepted.

## Verdict

**Pass. No new actionable source or packaging blocker remains in this review round.** The three previously bounded issues are fixed in the current source, covered by focused synthetic tests, and present in a rebuilt setup whose application payload matches the reviewed source. The currently installed app remains one build behind this newest artifact but is already in the earlier encrypted runtime state.

## Requested fixes independently verified

### Provisioning failure is observable

`electron/build/installer.nsh:96-135` runs the actual provisioning flow, retries once, writes a fixed non-secret failure marker, and calls `SetErrorLevel 3` after the second failure for both silent and interactive installs. Interactive installs additionally show the warning at lines 124-126. Success removes a stale marker at lines 129-133.

NSIS T10 executes the shared installer flow in an isolated synthetic executable and observes exit code 3, the exact fixed marker, no synthetic key in the marker, and no provision/temp output. T11 observes exit code 0 on success, stale-marker removal, a valid envelope, and no temp file. These tests prove the shared macro flow and process exit status; the final packaged setup's failure branch was not deliberately triggered.

### New provisioning is a single-envelope commit

`electron/build/provision-macro.nsh` produces one `provision.json` containing schema, key version, base64 DPAPI blob, and SHA-256 of the decoded blob. It writes `provision.json.tmp` and promotes it with one final rename. A kill before promotion leaves an ignored temp; after promotion the complete JSON is authoritative. Controlled write/rename failures clean their temporary output.

`backend/config.py:392-447` validates JSON structure, schema, strict base64, and the blob hash before DPAPI unprotect. Malformed/corrupt envelopes are deleted and fail closed; transient DPAPI failures retain the complete envelope for retry. Tests cover corruption, invalid JSON/schema, temp-only interruption residue, successful recovery, and absence of the old two-file output.

The hash detects blob corruption; it is not a cryptographic authentication binding for `keyVersion`. That is sufficient for torn-write integrity in this local same-user design and is not a release finding.

Legacy compatibility remains necessarily weaker: `backend/config.py:450-505` rejects incomplete or undecodable old pairs, but cannot identify a decryptable new blob paired with valid stale metadata because the old format had no binding between them. The prior report's claim that every mixed valid pair is detected was too strong. The new installer cannot create that state, so this is a documented legacy-format limitation rather than a current blocker.

### Every Electron Builder path regenerates the seed define

`electron/package.json:26-27` registers `beforePack` generation and successful-build cleanup. `electron/scripts/gen-seed-define.mjs:63-126` resolves paths from its own module, exposes the named hook, regenerates from the canonical seed, and throws on failure. Thus direct Electron Builder invocation no longer relies on a schema-valid file left by a failed prior build. `predist` remains an ergonomic duplicate for `npm run dist`; `afterAllArtifactBuild` removes the generated file after success.

The focused suite verifies named-hook wiring, cwd independence, canonical seed/version correspondence in memory, failure propagation, cleanup, and guard compilation. The implementation also demonstrated a direct Electron Builder run overwriting a deliberately incomplete old define before NSIS compilation.

## Earlier security fixes retained

- Renderer IPC uses `fileURLToPath` plus canonical containment and exposes summary-only configuration with one-way key operations.
- WebSocket task/key operations require the per-launch token and reject foreign browser origins before upgrade and in the handler.
- Electron never writes plaintext credentials; Python migration, rotation, restore, and provisioning failures preserve the existing credential or retry artifact.
- Typed-action telemetry withholds the text itself; screenshots default off; provider raw dumps default off and redact when enabled.
- Active and bundled credentials are DPAPI blobs inside the same runtime `config.json`. The separate installer envelope is protected staging and is deleted after durable consumption.
- The WebSocket key-operation test now captures live stdout/stderr and includes two working negative controls, so its output-redaction assertion is meaningful.

## Focused verification

No provider call, production credential write, application-process change, installation, or uninstallation was performed. Tests used synthetic keys and isolated temporary directories.

```text
python -m unittest backend.tests.test_security
node electron/scripts/gen_seed_define_test.mjs
pwsh -NoProfile -File electron/build/nsis-tests/run-provision-tests.ps1
```

Results: **Python 70/70 passed** in 3.637 seconds; **GEN-SEED-DEFINE PASS (51 checks)**; **NSIS 57/57 passed**. The unchanged IPC, secrets-filter, backend-authentication, and live WebSocket key-operation suites had already passed; they were not needlessly rerun except where the updated report evidence required it.

## Source, rebuilt artifact, and installed state

### Source

The reviewed implementation is still uncommitted on top of the baseline HEAD. All three changes above are present and pass this review.

### Rebuilt setup

```text
size: 104,381,903 bytes
SHA-256: ada46f4f231f0d4237b0616c83088ec42fcf9ee8ac7c4bf4c750dc3115f239fa
```

The current nested and unpacked ASAR are byte-identical: 603,229 bytes, SHA-256 `f28f96f24af386308382fdd153e806f1f9076aa821ceb63696dbf12aeb3eba5a`. They contain no config/default-config/generated-seed member and have zero exact-key matches in UTF-8 and UTF-16LE scans. Security-critical `main.js`, `ipc-trust.js`, and `secrets-filter.js` match current source hashes. All 19 production backend Python members match current source, including the new `config.py` envelope consumer. Extraction temporary files were removed, and the generated seed define is absent after the build.

### Current installed app

The installed ASAR is the preceding 603,218-byte build, SHA-256 `da4150a32e3ec2d477a7ee9404f7747ef64198a35a73fe32bd12ae3d6fdd4ba4`. Eighteen installed backend files match current source; `config.py` differs because the installed app predates the new envelope consumer. Four omitted files are tests only.

The current runtime config remains encrypted: active and bundled-backup DPAPI values are in the same file, both decrypt in memory to the canonical key, plaintext fields are empty, and no provision staging file, failure marker, updater cache, or generated seed define is present. This is a read-only snapshot, not proof that the newest installer lifecycle has run.

## Limits

Still unverified through the actual final setup: clean-machine install, deliberate packaged-installer provisioning failure, upgrade-in-place to the envelope build, cancellation/crash recovery, and uninstall. BrowserWindow IPC has unit coverage of its trust decision but no full UI round trip. No new full-machine scan was performed.
