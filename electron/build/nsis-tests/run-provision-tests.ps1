#requires -Version 5.1
# Standalone makensis tests for electron/build/provision-macro.nsh and
# electron/build/installer.nsh (T1-T11).
#
# Constraints honored: zero UAC (every test .nsi is RequestExecutionLevel user
# + SilentInstall silent), everything compiled to and run from %TEMP% only,
# the built Setup is never run, the user's installed app / %APPDATA%\personal-
# computer-use / the electron-builder updater cache are never touched
# (installer.nsh is only COMPILED in T6, never executed), no git operations,
# and only synthetic (random, generated here) keys are embedded in test
# artifacts - the real electron/build/seed-define.nsh is never placed on the
# NSIS include path of any test compile.
#
# Usage: pwsh -File run-provision-tests.ps1 [-KeepTemp]

param([switch]$KeepTemp)

$ErrorActionPreference = 'Stop'
$script:passCount = 0
$script:failCount = 0

function Assert([bool]$Cond, [string]$Label) {
  if ($Cond) {
    $script:passCount++
    Write-Host "    PASS: $Label"
  } else {
    $script:failCount++
    Write-Host "    FAIL: $Label"
  }
}

# --- locate toolchain ------------------------------------------------------
$nsisRoot = Get-ChildItem "$env:LOCALAPPDATA\electron-builder\Cache\nsis" -Directory -ErrorAction SilentlyContinue |
  Where-Object Name -match '^nsis-3\.' | Sort-Object Name -Descending | Select-Object -First 1
if (-not $nsisRoot) { throw "makensis not found under $env:LOCALAPPDATA\electron-builder\Cache\nsis (fallback hint: npx electron-builder --help)" }
$makensis = Join-Path $nsisRoot.FullName 'makensis.exe'
if (-not (Test-Path $makensis)) { $makensis = Join-Path $nsisRoot.FullName 'Bin\makensis.exe' }
if (-not (Test-Path $makensis)) { throw "makensis.exe missing in $($nsisRoot.FullName)" }
Write-Host "makensis: $makensis"

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "python not on PATH" }

$buildDir   = Split-Path -Parent $PSScriptRoot          # electron/build
$backendDir = Join-Path (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))) 'backend'

# Synthetic key: 48 random alphanumerics (never the real seed).
function New-SyntheticKey {
  $alphabet = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
  $bytes = [byte[]]::new(48)
  [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
  -join ($bytes | ForEach-Object { $alphabet[$_ % $alphabet.Length] })
}

# Compile a .nsi with makensis (working dir = the .nsi's own temp dir, so all
# relative !include resolution stays inside the sandbox).
function Invoke-Makensis([string]$NsiPath) {
  $work = Split-Path -Parent $NsiPath
  Push-Location $work
  try {
    $out = & $makensis /NOCD /V2 $NsiPath 2>&1
    $code = $LASTEXITCODE
  } finally { Pop-Location }
  [pscustomobject]@{ ExitCode = $code; Output = ($out | Out-String).TrimEnd() }
}

# Run a compiled silent test exe (RequestExecutionLevel user, no elevation).
function Invoke-TestExe([string]$ExePath) {
  $p = Start-Process -FilePath $ExePath -Wait -PassThru
  $p.ExitCode
}

# Verify the provision.json envelope validates (schema, blob sha256) and its
# blob decrypts, via backend.secret_store, to the synthetic key passed through
# the environment (never printed, never on a command line).
function Test-EnvelopeDecrypt([string]$EnvelopePath, [string]$Key) {
  $code = @'
import base64, hashlib, json, os, sys
sys.path.insert(0, r"__BACKEND__")
import secret_store
env = json.load(open(r"__ENV__", "r", encoding="utf-8"))
if env.get("schema") != 1:
    print("BAD_SCHEMA")
elif hashlib.sha256(base64.b64decode(env["blob"], validate=True)).hexdigest() != env["blob_sha256"].lower():
    print("SHA_MISMATCH")
else:
    got = secret_store.unprotect_bytes(env["blob"])
    want = os.environ.get("PCU_TEST_KEY", "").encode("utf-8")
    if got is None:
        print("DECRYPT_FAILED")
    elif got == want:
        print("DECRYPT_OK")
    else:
        print("DECRYPT_MISMATCH")
'@
  $code = $code.Replace('__BACKEND__', $backendDir).Replace('__ENV__', $EnvelopePath)
  $env:PCU_TEST_KEY = $Key
  try { [string](python -I -c $code 2>&1) } finally { Remove-Item Env:PCU_TEST_KEY -ErrorAction SilentlyContinue }
}

# Write a legacy-format provision.blob (raw DPAPI bytes of a SYNTHETIC key)
# for pre-upgrade leftovers, via backend.secret_store (key via env, never
# printed, never on a command line).
function New-LegacyBlobFile([string]$BlobPath, [string]$Key) {
  $code = @'
import base64, os, sys
sys.path.insert(0, r"__BACKEND__")
import secret_store
open(r"__BLOB__", "wb").write(base64.b64decode(secret_store.protect(os.environ.get("PCU_TEST_KEY", ""))))
'@
  $code = $code.Replace('__BACKEND__', $backendDir).Replace('__BLOB__', $BlobPath)
  $env:PCU_TEST_KEY = $Key
  try { $null = python -I -c $code 2>&1 } finally { Remove-Item Env:PCU_TEST_KEY -ErrorAction SilentlyContinue }
}

# Relative file names under a dir (for "nothing left behind" assertions).
function Get-RelFiles([string]$Dir) {
  if (-not (Test-Path $Dir)) { return @() }
  @(Get-ChildItem $Dir -Recurse -File | ForEach-Object { $_.FullName.Substring($Dir.Length + 1) })
}

function Read-Result([string]$Path) {
  if (Test-Path $Path) { (Get-Content $Path -Raw).Trim() } else { '<missing>' }
}

# Write a test .nsi (placeholders replaced literally, PS sees no $ escapes).
function New-TestNsi([string]$Path, [hashtable]$V) {
  $dir = Split-Path -Parent $Path
  Copy-Item (Join-Path $sandbox 'provision-macro.nsh') (Join-Path $dir 'provision-macro.nsh') -Force
  $tpl = @'
RequestExecutionLevel user
SilentInstall silent
!define PCU_SEED_KEY "__KEY__"
!define PCU_SEED_KEY_VERSION __VERSION__
OutFile "__EXE__"
!include "provision-macro.nsh"
Section
  !insertmacro PCU_DPAPI_PROVISION_TO "__TARGET__" "${PCU_SEED_KEY}" "${PCU_SEED_KEY_VERSION}"
  FileOpen $0 "__RESULT__" w
  FileWrite $0 $PCU_DPAPI_PROVISION_RESULT
  FileClose $0
SectionEnd
'@
  $tpl = $tpl.Replace('__KEY__', $V.Key).Replace('__VERSION__', [string]$V.Version)
  $tpl = $tpl.Replace('__EXE__', $V.Exe).Replace('__TARGET__', $V.Target).Replace('__RESULT__', $V.Result)
  Set-Content -LiteralPath $Path -Value $tpl -Encoding UTF8
}

# --- sandbox ---------------------------------------------------------------
$sandbox = Join-Path ([IO.Path]::GetTempPath()) "pcu-nsis-tests-$([guid]::NewGuid().ToString('N').Substring(0,8))"
New-Item -ItemType Directory -Path $sandbox | Out-Null
Write-Host "sandbox: $sandbox"

try {
  # Byte-identical sandbox copies of the files under test; the REAL
  # electron/build dir is never on any include path, so no test can ever
  # resolve the real (secret-bearing) seed-define.nsh.
  Copy-Item (Join-Path $buildDir 'provision-macro.nsh') (Join-Path $sandbox 'provision-macro.nsh')
  $hashReal      = (Get-FileHash (Join-Path $buildDir 'provision-macro.nsh')).Hash
  $hashSandboxed = (Get-FileHash (Join-Path $sandbox 'provision-macro.nsh')).Hash
  Assert ($hashReal -eq $hashSandboxed) 'sandboxed provision-macro.nsh is byte-identical to electron/build/provision-macro.nsh'

  # ================= T1: happy path regression =================
  Write-Host 'T1 happy path: macro succeeds, envelope exists and validates, NO legacy two-file artifacts'
  $t1 = Join-Path $sandbox 't1'; New-Item -ItemType Directory $t1 | Out-Null
  $key1 = New-SyntheticKey
  $v1 = @{
    Key = $key1; Version = 7
    Target = (Join-Path $t1 'pcu'); Exe = (Join-Path $t1 't1.exe'); Result = (Join-Path $t1 't1.result')
  }
  New-TestNsi (Join-Path $t1 't1.nsi') $v1
  $c1 = Invoke-Makensis (Join-Path $t1 't1.nsi')
  Assert ($c1.ExitCode -eq 0) 'T1 .nsi compiles (makensis exit 0)'
  if ($c1.ExitCode -ne 0) { Write-Host $c1.Output }
  $null = Invoke-TestExe $v1.Exe
  Assert ((Read-Result $v1.Result) -eq 'ok') 'T1 macro result is "ok"'
  Assert (Test-Path (Join-Path $v1.Target 'provision.json')) 'T1 provision.json envelope exists'
  Assert (-not ((Test-Path (Join-Path $v1.Target 'provision.blob')) -or (Test-Path (Join-Path $v1.Target 'provision.meta.json')))) 'T1 NO provision.blob / provision.meta.json written'
  Assert (-not ((Test-Path (Join-Path $v1.Target 'provision.json.tmp')) -or (Test-Path (Join-Path $v1.Target 'provision.blob.tmp')) -or (Test-Path (Join-Path $v1.Target 'provision.meta.json.tmp')))) 'T1 no .tmp leftover'
  $env1 = Get-Content (Join-Path $v1.Target 'provision.json') -Raw | ConvertFrom-Json
  Assert ($env1.schema -eq 1 -and $env1.keyVersion -eq 7) "T1 envelope schema=1 keyVersion=7 (got schema=$($env1.schema) keyVersion=$($env1.keyVersion))"
  $dec1 = Test-EnvelopeDecrypt (Join-Path $v1.Target 'provision.json') $key1
  Assert ($dec1 -eq 'DECRYPT_OK') "T1 envelope blob validates (sha256) and decrypts to synthetic key ($dec1)"

  # ================= T2: output dir creation fails =================
  Write-Host 'T2 dir creation fails: target sits under a path blocked by a plain FILE'
  $t2 = Join-Path $sandbox 't2'; New-Item -ItemType Directory (Join-Path $t2 'root') | Out-Null
  $key2 = New-SyntheticKey
  Set-Content (Join-Path $t2 'root\blocked') 'scaffolding: a file where the target parent dir would go' | Out-Null
  $v2 = @{
    Key = $key2; Version = 7
    Target = (Join-Path $t2 'root\blocked\pcu'); Exe = (Join-Path $t2 't2.exe'); Result = (Join-Path $t2 't2.result')
  }
  New-TestNsi (Join-Path $t2 't2.nsi') $v2
  $c2 = Invoke-Makensis (Join-Path $t2 't2.nsi')
  Assert ($c2.ExitCode -eq 0) 'T2 .nsi compiles (makensis exit 0)'
  if ($c2.ExitCode -ne 0) { Write-Host $c2.Output }
  $null = Invoke-TestExe $v2.Exe
  Assert ((Read-Result $v2.Result) -eq 'fail') 'T2 macro result is "fail"'
  $left2 = @(Get-RelFiles (Join-Path $t2 'root'))
  Assert ($left2.Count -eq 1 -and $left2[0] -eq 'blocked') "T2 nothing created under target root (files: $($left2 -join ','))"

  # ================= T3: envelope temp write fails =================
  Write-Host 'T3 envelope temp write fails: provision.json.tmp pre-created as a DIRECTORY'
  $t3 = Join-Path $sandbox 't3'; New-Item -ItemType Directory (Join-Path $t3 'pcu') | Out-Null
  $key3 = New-SyntheticKey
  New-Item -ItemType Directory (Join-Path $t3 'pcu\provision.json.tmp') | Out-Null
  $v3 = @{
    Key = $key3; Version = 7
    Target = (Join-Path $t3 'pcu'); Exe = (Join-Path $t3 't3.exe'); Result = (Join-Path $t3 't3.result')
  }
  New-TestNsi (Join-Path $t3 't3.nsi') $v3
  $c3 = Invoke-Makensis (Join-Path $t3 't3.nsi')
  Assert ($c3.ExitCode -eq 0) 'T3 .nsi compiles (makensis exit 0)'
  if ($c3.ExitCode -ne 0) { Write-Host $c3.Output }
  $null = Invoke-TestExe $v3.Exe
  Assert ((Read-Result $v3.Result) -eq 'fail') 'T3 macro result is "fail"'
  Remove-Item (Join-Path $t3 'pcu\provision.json.tmp') -Force   # clear injected scaffolding
  $left3 = @(Get-RelFiles (Join-Path $t3 'pcu'))
  Assert ($left3.Count -eq 0) "T3 no partial finals and no .tmp leftovers (files: $($left3 -join ','))"

  # ================= T4: single commit rename fails =================
  Write-Host 'T4 commit rename fails: provision.json (FINAL name) pre-created as a DIRECTORY'
  Write-Host '   (simulates the one failure mode left after collapsing to a single rename)'
  $t4 = Join-Path $sandbox 't4'; New-Item -ItemType Directory (Join-Path $t4 'pcu') | Out-Null
  $key4 = New-SyntheticKey
  New-Item -ItemType Directory (Join-Path $t4 'pcu\provision.json') | Out-Null
  $v4 = @{
    Key = $key4; Version = 7
    Target = (Join-Path $t4 'pcu'); Exe = (Join-Path $t4 't4.exe'); Result = (Join-Path $t4 't4.result')
  }
  New-TestNsi (Join-Path $t4 't4.nsi') $v4
  $c4 = Invoke-Makensis (Join-Path $t4 't4.nsi')
  Assert ($c4.ExitCode -eq 0) 'T4 .nsi compiles (makensis exit 0)'
  if ($c4.ExitCode -ne 0) { Write-Host $c4.Output }
  $null = Invoke-TestExe $v4.Exe
  Assert ((Read-Result $v4.Result) -eq 'fail') 'T4 macro result is "fail"'
  Assert (-not (Test-Path (Join-Path $v4.Target 'provision.json.tmp'))) 'T4 envelope .tmp (staged by this attempt) was cleaned up'
  Remove-Item (Join-Path $t4 'pcu\provision.json') -Force   # clear injected scaffolding
  $left4 = @(Get-RelFiles (Join-Path $t4 'pcu'))
  Assert ($left4.Count -eq 0) "T4 no partial finals and no .tmp leftovers (files: $($left4 -join ','))"

  # ================= T5: retry semantics / re-entry =================
  Write-Host 'T5 failure then success then re-run over existing finals (retry + re-entry safe)'
  $t5 = Join-Path $sandbox 't5'; New-Item -ItemType Directory (Join-Path $t5 'root') | Out-Null
  $key5 = New-SyntheticKey
  Set-Content (Join-Path $t5 'root\blocked') 'scaffolding: unwritable-target attempt 1' | Out-Null
  $v5a = @{
    Key = $key5; Version = 7
    Target = (Join-Path $t5 'root\blocked\pcu'); Exe = (Join-Path $t5 't5a.exe'); Result = (Join-Path $t5 't5a.result')
  }
  New-TestNsi (Join-Path $t5 't5a.nsi') $v5a
  $c5a = Invoke-Makensis (Join-Path $t5 't5a.nsi')
  Assert ($c5a.ExitCode -eq 0) 'T5a .nsi compiles (makensis exit 0)'
  if ($c5a.ExitCode -ne 0) { Write-Host $c5a.Output }
  $null = Invoke-TestExe $v5a.Exe
  Assert ((Read-Result $v5a.Result) -eq 'fail') 'T5a first (failing) attempt reports "fail"'
  $left5a = @(Get-RelFiles (Join-Path $t5 'root'))
  Assert ($left5a.Count -eq 1 -and $left5a[0] -eq 'blocked') "T5a failing attempt cleaned everything (files: $($left5a -join ','))"

  Remove-Item (Join-Path $t5 'root\blocked') -Force   # "make the path writable" between runs
  $v5b = @{
    Key = $key5; Version = 7
    Target = (Join-Path $t5 'root\pcu'); Exe = (Join-Path $t5 't5b.exe'); Result = (Join-Path $t5 't5b.result')
  }
  New-TestNsi (Join-Path $t5 't5b.nsi') $v5b
  $c5b = Invoke-Makensis (Join-Path $t5 't5b.nsi')
  Assert ($c5b.ExitCode -eq 0) 'T5b .nsi compiles (makensis exit 0)'
  if ($c5b.ExitCode -ne 0) { Write-Host $c5b.Output }
  $null = Invoke-TestExe $v5b.Exe
  Assert ((Read-Result $v5b.Result) -eq 'ok') 'T5b second (writable) attempt reports "ok"'
  $dec5b = Test-EnvelopeDecrypt (Join-Path $v5b.Target 'provision.json') $key5
  Assert ($dec5b -eq 'DECRYPT_OK') "T5b envelope blob validates and decrypts to synthetic key ($dec5b)"

  $v5c = @{
    Key = $key5; Version = 7
    Target = (Join-Path $t5 'root\pcu'); Exe = (Join-Path $t5 't5c.exe'); Result = (Join-Path $t5 't5c.result')
  }
  New-TestNsi (Join-Path $t5 't5c.nsi') $v5c
  $c5c = Invoke-Makensis (Join-Path $t5 't5c.nsi')
  Assert ($c5c.ExitCode -eq 0) 'T5c .nsi compiles (makensis exit 0)'
  if ($c5c.ExitCode -ne 0) { Write-Host $c5c.Output }
  $null = Invoke-TestExe $v5c.Exe
  Assert ((Read-Result $v5c.Result) -eq 'ok') 'T5c re-run over existing finals reports "ok" (re-entry safe)'
  $dec5c = Test-EnvelopeDecrypt (Join-Path $v5c.Target 'provision.json') $key5
  Assert ($dec5c -eq 'DECRYPT_OK') "T5c re-provisioned envelope decrypts to synthetic key ($dec5c)"
  Assert ((Test-Path (Join-Path $t5 'provision.json.tmp')) -eq $false -and (Test-Path (Join-Path $v5c.Target 'provision.json.tmp')) -eq $false) 'T5c no .tmp after re-entry'

  # ================= T6: full installer.nsh compiles standalone =================
  Write-Host 'T6 full installer.nsh (byte-identical sandbox copy) compiles standalone; uninstall hook compiles; NOT executed'
  $t6 = Join-Path $sandbox 't6'; New-Item -ItemType Directory $t6 | Out-Null
  Copy-Item (Join-Path $sandbox 'provision-macro.nsh') (Join-Path $t6 'provision-macro.nsh')
  Copy-Item (Join-Path $buildDir 'installer.nsh') (Join-Path $t6 'installer.nsh')
  $hIReal = (Get-FileHash (Join-Path $buildDir 'installer.nsh')).Hash
  $hISb   = (Get-FileHash (Join-Path $t6 'installer.nsh')).Hash
  Assert ($hIReal -eq $hISb) 'sandboxed installer.nsh is byte-identical to electron/build/installer.nsh'
  # Synthetic stand-in for the gitignored seed-define.nsh (generated key only).
  Set-Content (Join-Path $t6 'seed-define.nsh') (@"
# synthetic test seed generated by run-provision-tests.ps1 (never the real key)
!define PCU_SEED_KEY "$(New-SyntheticKey)"
!define PCU_SEED_KEY_VERSION 3
!define PCU_SEED_KEY_SCHEMA 1
"@) | Out-Null
  $key6 = (Select-String -Path (Join-Path $t6 'seed-define.nsh') -Pattern '"(.+)"').Matches[0].Groups[1].Value
  $nsi6 = @'
RequestExecutionLevel user
SilentInstall silent
# mirror of electron-builder's structure: LogicLib + installMode var come from
# the template/multiUser.nsh in a real build; APP_PACKAGE_NAME/APP_PRODUCT_
# FILENAME/PCU_* defines come from the template and the generated seed define.
!include "LogicLib.nsh"
Var installMode
!define APP_PACKAGE_NAME "personal-computer-use"
!define APP_PRODUCT_FILENAME "Personal Computer Use"
!include "installer.nsh"
OutFile "__EXE__"
Section "install"
  !insertmacro customInstall
SectionEnd
Section "un.install"
  !insertmacro customUnInstall
SectionEnd
'@
  $nsi6 = $nsi6.Replace('__EXE__', (Join-Path $t6 't6.exe'))
  Set-Content (Join-Path $t6 't6.nsi') $nsi6 -Encoding UTF8
  $c6 = Invoke-Makensis (Join-Path $t6 't6.nsi')
  Assert ($c6.ExitCode -eq 0) 'T6 installer.nsh standalone compile: customInstall + customUnInstall sections, makensis exit 0'
  if ($c6.ExitCode -ne 0) { Write-Host $c6.Output }
  Write-Host '    T6 output exe intentionally NOT run (would touch real %LOCALAPPDATA% updater cache / %APPDATA%)'

  # ================= T7: staleness guard fails the compile on a stale seed =================
  Write-Host 'T7 installer.nsh guard hard-errors when seed-define lacks the schema sentinel'
  $t7 = Join-Path $sandbox 't7'; New-Item -ItemType Directory $t7 | Out-Null
  Copy-Item (Join-Path $sandbox 'provision-macro.nsh') (Join-Path $t7 'provision-macro.nsh')
  Copy-Item (Join-Path $buildDir 'installer.nsh') (Join-Path $t7 'installer.nsh')
  Set-Content (Join-Path $t7 'seed-define.nsh') (@"
# STALE stand-in: written by an older generator - no PCU_SEED_KEY_SCHEMA sentinel
!define PCU_SEED_KEY "$(New-SyntheticKey)"
!define PCU_SEED_KEY_VERSION 2
"@) | Out-Null
  Set-Content (Join-Path $t7 't7.nsi') $nsi6.Replace((Join-Path $t6 't6.exe'), (Join-Path $t7 't7.exe')) -Encoding UTF8
  $c7 = Invoke-Makensis (Join-Path $t7 't7.nsi')
  Assert ($c7.ExitCode -ne 0) 'T7 makensis FAILS on stale seed-define (missing PCU_SEED_KEY_SCHEMA)'
  Assert ($c7.Output -match 'PCU_SEED_KEY') 'T7 failure message names the seed guard error'
  Write-Host '    T7 verifies the staleness guard aborts before any key can be embedded'

  # ================= T8: hard-kill leftovers are cleaned =================
  Write-Host 'T8 prior hard-kill simulation: pre-placed .tmp garbage is cleaned and the next provisioning attempt still succeeds'
  $t8 = Join-Path $sandbox 't8'; New-Item -ItemType Directory (Join-Path $t8 'pcu') | Out-Null
  $key8 = New-SyntheticKey
  Set-Content (Join-Path $t8 'pcu\provision.json.tmp') '{"schema":1,"blob":"killed-mid-write' | Out-Null
  Set-Content (Join-Path $t8 'pcu\provision.blob.tmp') 'legacy-kill-leftover' | Out-Null
  Set-Content (Join-Path $t8 'pcu\provision.meta.json.tmp') '{"keyVersion":' | Out-Null
  $v8 = @{
    Key = $key8; Version = 9
    Target = (Join-Path $t8 'pcu'); Exe = (Join-Path $t8 't8.exe'); Result = (Join-Path $t8 't8.result')
  }
  New-TestNsi (Join-Path $t8 't8.nsi') $v8
  $c8 = Invoke-Makensis (Join-Path $t8 't8.nsi')
  Assert ($c8.ExitCode -eq 0) 'T8 .nsi compiles (makensis exit 0)'
  if ($c8.ExitCode -ne 0) { Write-Host $c8.Output }
  $null = Invoke-TestExe $v8.Exe
  Assert ((Read-Result $v8.Result) -eq 'ok') 'T8 provisioning after a simulated hard kill reports "ok"'
  Assert (Test-Path (Join-Path $v8.Target 'provision.json')) 'T8 provision.json envelope written'
  Assert (-not (Test-Path (Join-Path $v8.Target 'provision.json.tmp'))) 'T8 envelope .tmp leftover removed'
  Assert (-not ((Test-Path (Join-Path $v8.Target 'provision.blob.tmp')) -or (Test-Path (Join-Path $v8.Target 'provision.meta.json.tmp')))) 'T8 legacy .tmp leftovers removed'
  $dec8 = Test-EnvelopeDecrypt (Join-Path $v8.Target 'provision.json') $key8
  Assert ($dec8 -eq 'DECRYPT_OK') "T8 envelope decrypts to the fresh synthetic key ($dec8)"

  # ================= T9: no legacy two-file artifacts, upgrade cleanup =================
  Write-Host 'T9 provisioning writes NO provision.blob/provision.meta.json; pre-placed legacy pair from an older installer is removed'
  $t9 = Join-Path $sandbox 't9'; New-Item -ItemType Directory (Join-Path $t9 'pcu') | Out-Null
  $key9 = New-SyntheticKey          # NEW key this installer provisions
  $key9old = New-SyntheticKey       # OLD key the legacy pair carries
  New-LegacyBlobFile (Join-Path $t9 'pcu\provision.blob') $key9old
  Assert (Test-Path (Join-Path $t9 'pcu\provision.blob')) 'T9 legacy provision.blob pre-placed (synthetic old key)'
  Set-Content (Join-Path $t9 'pcu\provision.meta.json') '{"keyVersion": 2, "keySource": "bundled"}' | Out-Null
  $v9 = @{
    Key = $key9; Version = 9
    Target = (Join-Path $t9 'pcu'); Exe = (Join-Path $t9 't9.exe'); Result = (Join-Path $t9 't9.result')
  }
  New-TestNsi (Join-Path $t9 't9.nsi') $v9
  $c9 = Invoke-Makensis (Join-Path $t9 't9.nsi')
  Assert ($c9.ExitCode -eq 0) 'T9 .nsi compiles (makensis exit 0)'
  if ($c9.ExitCode -ne 0) { Write-Host $c9.Output }
  $null = Invoke-TestExe $v9.Exe
  Assert ((Read-Result $v9.Result) -eq 'ok') 'T9 macro result is "ok"'
  Assert (Test-Path (Join-Path $v9.Target 'provision.json')) 'T9 provision.json envelope written'
  Assert (-not ((Test-Path (Join-Path $v9.Target 'provision.blob')) -or (Test-Path (Join-Path $v9.Target 'provision.meta.json')))) 'T9 legacy provision.blob and provision.meta.json removed (upgrade cleanup)'
  Assert (-not ((Test-Path (Join-Path $v9.Target 'provision.blob.tmp')) -or (Test-Path (Join-Path $v9.Target 'provision.meta.json.tmp')) -or (Test-Path (Join-Path $v9.Target 'provision.json.tmp')))) 'T9 no .tmp leftovers'
  $dec9 = Test-EnvelopeDecrypt (Join-Path $v9.Target 'provision.json') $key9
  Assert ($dec9 -eq 'DECRYPT_OK') "T9 envelope carries the NEW synthetic key ($dec9)"

  # ================= T10/T11 shared helpers: run-level flow tests ============
  # Both tests compile a synthetic installer that !includes the sandboxed
  # installer.nsh (with a synthetic seed-define.nsh, as in T6) and inserts
  # PCU_INSTALL_PROVISION_FLOW directly with a SANDBOX appdata root - so the
  # real %APPDATA%, the electron-builder updater cache and the installed app
  # are never touched, while the exe still exercises installer.nsh's ACTUAL
  # provisioning flow (attempt + retry, failure marker, SetErrorLevel) at run
  # level, including the real process exit code.
  function New-InstallerIncludeDir([string]$Dir) {
    New-Item -ItemType Directory $Dir | Out-Null
    Copy-Item (Join-Path $sandbox 'provision-macro.nsh') (Join-Path $Dir 'provision-macro.nsh')
    Copy-Item (Join-Path $buildDir 'installer.nsh') (Join-Path $Dir 'installer.nsh')
    Set-Content (Join-Path $Dir 'seed-define.nsh') (@"
# synthetic test seed generated by run-provision-tests.ps1 (never the real key)
!define PCU_SEED_KEY "$(New-SyntheticKey)"
!define PCU_SEED_KEY_VERSION 3
!define PCU_SEED_KEY_SCHEMA 1
"@) | Out-Null
    (Select-String -Path (Join-Path $Dir 'seed-define.nsh') -Pattern '"(.+)"').Matches[0].Groups[1].Value
  }
  function New-FlowTestNsi([string]$Path, [string]$Exe, [string]$AppDataRoot, [string]$PkgName) {
    $tpl = @'
RequestExecutionLevel user
SilentInstall silent
!include "LogicLib.nsh"
!define APP_PACKAGE_NAME "__PKG__"
!define APP_PRODUCT_FILENAME "__PKG__"
!include "installer.nsh"
OutFile "__EXE__"
Section "install"
  !insertmacro PCU_INSTALL_PROVISION_FLOW "__APPDATA__"
SectionEnd
'@
    $tpl = $tpl.Replace('__PKG__', $PkgName).Replace('__EXE__', $Exe).Replace('__APPDATA__', $AppDataRoot)
    Set-Content -LiteralPath $Path -Value $tpl -Encoding UTF8
  }

  # ================= T10: failed provisioning is observable ==================
  Write-Host 'T10 failed provisioning (after retry): silent process exit code is the distinct nonzero constant and the non-secret flag file exists'
  $t10 = Join-Path $sandbox 't10'
  $key10 = New-InstallerIncludeDir $t10
  $pkg10 = 'pcu-nsis-test-t10'
  $appdata10 = Join-Path $t10 'appdata'
  $target10 = Join-Path $appdata10 $pkg10
  New-Item -ItemType Directory $target10 | Out-Null
  # Deterministic provisioning failure with the target dir PRESENT (so the
  # flag file is writable): provision.json.tmp pre-created as a DIRECTORY
  # (same scaffolding trick as T3) - both attempts fail on the temp write.
  New-Item -ItemType Directory (Join-Path $target10 'provision.json.tmp') | Out-Null
  New-FlowTestNsi (Join-Path $t10 't10.nsi') (Join-Path $t10 't10.exe') $appdata10 $pkg10
  $c10 = Invoke-Makensis (Join-Path $t10 't10.nsi')
  Assert ($c10.ExitCode -eq 0) 'T10 .nsi compiles (makensis exit 0)'
  if ($c10.ExitCode -ne 0) { Write-Host $c10.Output }
  $exit10 = Invoke-TestExe (Join-Path $t10 't10.exe')
  Assert ($exit10 -eq 3) "T10 silent installer process exit code is the distinct provisioning-failure code 3 (got $exit10)"
  Remove-Item (Join-Path $target10 'provision.json.tmp') -Force   # clear injected scaffolding
  $flag10 = Join-Path $target10 'provisioning-failed.flag'
  Assert (Test-Path $flag10) 'T10 provisioning-failed.flag marker exists after failed provisioning'
  $flagContent10 = Get-Content $flag10 -Raw
  Assert ($flagContent10.Trim() -eq 'provisioning failed at install time') "T10 flag content is the fixed one-line reason (got: $($flagContent10.Trim()))"
  Assert (-not $flagContent10.Contains($key10)) 'T10 flag contains NO synthetic key material'
  $left10 = @(Get-RelFiles $target10)
  Assert ($left10.Count -eq 1 -and $left10[0] -eq 'provisioning-failed.flag') "T10 no envelope and no .tmp leftovers - the flag is the only file (files: $($left10 -join ','))"

  # ================= T11: success path exit 0 + clear-on-repair ==============
  Write-Host 'T11 successful provisioning: exit code stays 0 and a stale failure flag from an earlier broken install is deleted (clear-on-repair)'
  $t11 = Join-Path $sandbox 't11'
  $key11 = New-InstallerIncludeDir $t11
  $pkg11 = 'pcu-nsis-test-t11'
  $appdata11 = Join-Path $t11 'appdata'
  $target11 = Join-Path $appdata11 $pkg11
  New-Item -ItemType Directory $target11 | Out-Null
  # Pre-place a STALE failure marker as if a previous broken install left it.
  Set-Content (Join-Path $target11 'provisioning-failed.flag') 'provisioning failed at install time' | Out-Null
  New-FlowTestNsi (Join-Path $t11 't11.nsi') (Join-Path $t11 't11.exe') $appdata11 $pkg11
  $c11 = Invoke-Makensis (Join-Path $t11 't11.nsi')
  Assert ($c11.ExitCode -eq 0) 'T11 .nsi compiles (makensis exit 0)'
  if ($c11.ExitCode -ne 0) { Write-Host $c11.Output }
  $exit11 = Invoke-TestExe (Join-Path $t11 't11.exe')
  Assert ($exit11 -eq 0) "T11 successful silent installer process exit code is 0 (got $exit11)"
  Assert (-not (Test-Path (Join-Path $target11 'provisioning-failed.flag'))) 'T11 stale provisioning-failed.flag deleted by the successful (repaired) install'
  Assert (Test-Path (Join-Path $target11 'provision.json')) 'T11 provision.json envelope written'
  $dec11 = Test-EnvelopeDecrypt (Join-Path $target11 'provision.json') $key11
  Assert ($dec11 -eq 'DECRYPT_OK') "T11 envelope decrypts to the synthetic key ($dec11)"
  Assert (-not (Test-Path (Join-Path $target11 'provision.json.tmp'))) 'T11 no .tmp leftover'

  # ================= summary =================
  Write-Host ''
  Write-Host "SUMMARY: $($script:passCount) passed, $($script:failCount) failed"
  if ($script:failCount -gt 0) { exit 1 }
  exit 0
} finally {
  if (-not $KeepTemp) {
    Remove-Item $sandbox -Recurse -Force -ErrorAction SilentlyContinue
  } else {
    Write-Host "sandbox kept: $sandbox"
  }
}

