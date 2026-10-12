# Removes what install-windows.ps1 added. Safe to run again.
# Run: powershell -ExecutionPolicy Bypass -File uninstall-windows.ps1
$ErrorActionPreference = 'Stop'

$repo = $PSScriptRoot
$progId = 'CSVViewer.File'
$classes = 'HKCU:\Software\Classes'
$extensions = @('.csv', '.tsv', '.txt', '.psv', '.dat')

$paths = @(
    (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\CSV Viewer.lnk'),
    "$classes\$progId",
    "$classes\Applications\csvview.pyw",
    (Join-Path $repo 'csvview.cmd')
)
foreach ($path in $paths) {
    if (Test-Path $path) { Remove-Item -Path $path -Recurse -Force }
}

# Only our value is removed; the extension keys stay
foreach ($ext in $extensions) {
    $key = "$classes\$ext\OpenWithProgids"
    if ((Test-Path $key) -and ($null -ne (Get-ItemProperty -Path $key -Name $progId -ErrorAction SilentlyContinue))) {
        Remove-ItemProperty -Path $key -Name $progId
    }
}

# Tell Explorer the associations changed
if (-not ('CsvViewer.ShellNotify' -as [type])) {
    Add-Type -Namespace CsvViewer -Name ShellNotify -MemberDefinition @'
[DllImport("shell32.dll")]
public static extern void SHChangeNotify(int wEventId, uint uFlags, IntPtr dwItem1, IntPtr dwItem2);
'@
}
[CsvViewer.ShellNotify]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)

Write-Host 'CSV Viewer removed. Python and its packages were left installed.'
