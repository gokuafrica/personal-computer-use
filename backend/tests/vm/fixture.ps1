param([string]$Root)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form=New-Object Windows.Forms.Form
$form.Text='Local computer-use test'
$form.StartPosition='Manual'
$form.Location=New-Object Drawing.Point(50,80)
$form.Size=New-Object Drawing.Size(600,380)
$button=New-Object Windows.Forms.Button
$button.Text='Submit test'; $button.AccessibleName='Submit test'
$button.Location=New-Object Drawing.Point(35,40);$button.Size=New-Object Drawing.Size(200,55)
$box=New-Object Windows.Forms.TextBox
$box.AccessibleName='Test text';$box.Location=New-Object Drawing.Point(35,125);$box.Size=New-Object Drawing.Size(500,45)
$box.Font=New-Object Drawing.Font('Segoe UI',18)
$label=New-Object Windows.Forms.Label
$label.Location=New-Object Drawing.Point(35,205);$label.Size=New-Object Drawing.Size(480,60)
$script:clicks=0
function Save-State {
 $label.Text="Clicks: $script:clicks`r`n$($box.Text)"
 $json=@{clicks=$script:clicks;text=$box.Text} | ConvertTo-Json -Compress
 [IO.File]::WriteAllText((Join-Path $Root 'fixture-result.json'),$json,(New-Object Text.UTF8Encoding($false)))
}
$button.add_Click({$script:clicks++;Save-State})
$box.add_TextChanged({Save-State})
$form.Controls.AddRange(@($button,$box,$label))
$form.add_Shown({Save-State;$form.Activate()})
$timer=New-Object Windows.Forms.Timer
$timer.Interval=100
$timer.add_Tick({
 $request=Join-Path $Root 'fixture-reset.json'
 if(Test-Path $request){
   try {
     $nonce=[IO.File]::ReadAllText($request)
     $script:clicks=0;$box.Clear();Save-State
     [IO.File]::Delete($request)
     [IO.File]::WriteAllText((Join-Path $Root 'fixture-ready.txt'),$nonce)
   } catch {}
 }
})
$timer.Start()
[void]$form.ShowDialog()
