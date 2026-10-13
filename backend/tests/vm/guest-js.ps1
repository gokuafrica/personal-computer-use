$ErrorActionPreference='Stop'
$install=Join-Path $env:LOCALAPPDATA 'Programs\personal-computer-use'
$tests=Join-Path $env:USERPROFILE 'Downloads\pcu-js-tests'
Expand-Archive -LiteralPath "$env:USERPROFILE\Downloads\js-tests.zip" -DestinationPath $tests -Force
$env:ELECTRON_RUN_AS_NODE='1'
$env:PCU_VM_INSTALL=$install
$env:PCU_VM_TESTS=$tests
$extract=@'
const fs=require('fs'), path=require('path');
const root=process.env.PCU_VM_TESTS, archive=path.join(process.env.PCU_VM_INSTALL,'resources','app.asar');
for(const name of ['main.js','ipc-trust.js','secrets-filter.js','subscription-setup.js','renderer/instruction-bar.js']) {
 const target=path.join(root,name);fs.mkdirSync(path.dirname(target),{recursive:true});
 fs.writeFileSync(target,fs.readFileSync(path.join(archive,name)));
}
'@
$extract | Set-Content "$tests\extract.cjs"
$extractProcess=Start-Process -FilePath "$install\Personal Computer Use.exe" -ArgumentList "$tests\extract.cjs" -WindowStyle Hidden -PassThru -Wait -RedirectStandardOutput "$tests\extract.out" -RedirectStandardError "$tests\extract.err"
if($extractProcess.ExitCode -ne 0){throw 'Installed ASAR extraction failed'}
$results=@()
foreach($name in @('ipc_trust_test.mjs','secrets_filter_test.mjs','runtime-audit-test.mjs','subscription-setup-test.mjs')){
 $testProcess=Start-Process -FilePath "$install\Personal Computer Use.exe" -ArgumentList "$tests\$name" -WindowStyle Hidden -PassThru -Wait -RedirectStandardOutput "$tests\$name.log" -RedirectStandardError "$tests\$name.err"
 $code=$testProcess.ExitCode
 $results+=@{suite=$name;exitCode=$code;installedModules=$true}
}
$results | ConvertTo-Json | Set-Content "$env:USERPROFILE\Downloads\pcu-vm-evidence\javascript-tests.json"
$results | ConvertTo-Json
if(@($results | Where-Object {$_.exitCode -ne 0}).Count){throw 'JavaScript suite failed'}
