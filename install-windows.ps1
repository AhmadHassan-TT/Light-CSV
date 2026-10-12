# Installs CSV Viewer from source for the current user (no admin). Safe to run again.
# Run: powershell -ExecutionPolicy Bypass -File install-windows.ps1
$ErrorActionPreference = 'Stop'

$repo = $PSScriptRoot
$launcher = Join-Path $repo 'csvview.pyw'
$icon = Join-Path $repo 'assets\csv-viewer.ico'
$progId = 'CSVViewer.File'
$classes = 'HKCU:\Software\Classes'
$extensions = @('.csv', '.tsv', '.txt', '.psv', '.dat')
$shortcut = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\CSV Viewer.lnk'
$cmdFile = Join-Path $repo 'csvview.cmd'

# Returns python.exe's full path if the command runs Python 3.9+, else $null (no double quotes in -c: PS 5.1 mangles them)
function Test-Python($command, $arguments) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) { return $null }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $command @arguments -c 'import sys; sys.version_info >= (3, 9) and print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return ([string]($out | Select-Object -Last 1)).Trim() }
    } catch {
    } finally {
        $ErrorActionPreference = $previous
    }
    return $null
}

# Creates a registry key only if missing (New-Item -Force would wipe an existing key)
function Ensure-Key($path) {
    if (-not (Test-Path $path)) { New-Item -Path $path -Force | Out-Null }
}

function Set-Default($path, $value) {
    Ensure-Key $path
    Set-ItemProperty -Path $path -Name '(default)' -Value $value
}

# Find Python 3.9+ (the py launcher first; the Store "python" alias fails this check)
$python = Test-Python 'py' @('-3')
if (-not $python) { $python = Test-Python 'python' @() }
if (-not $python) {
    Write-Host 'Python 3.9 or newer was not found. Install it with:'
    Write-Host '    winget install Python.Python.3.12'
    Write-Host 'or download it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"),'
    Write-Host 'then open a new PowerShell window and run this script again.'
    exit 1
}
Write-Host "Using Python: $python"

$pythonw = Join-Path (Split-Path $python -Parent) 'pythonw.exe'
if (-not (Test-Path $pythonw)) {
    Write-Host "pythonw.exe was not found next to $python"
    exit 1
}

# Install dependencies (Continue so pip's stderr warnings are not fatal)
$ErrorActionPreference = 'Continue'
& $python -m pip install --user --upgrade -r (Join-Path $repo 'requirements.txt')
$pipExit = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($pipExit -ne 0) {
    Write-Host 'pip install failed.'
    exit 1
}

# Start Menu shortcut
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($shortcut)
$link.TargetPath = $pythonw
$link.Arguments = "`"$launcher`""
$link.WorkingDirectory = $repo
$link.IconLocation = "$icon,0"
$link.Description = 'View very large CSV files quickly'
$link.Save()

# ProgID used by "Open with"
$openCommand = "`"$pythonw`" `"$launcher`" `"%1`""
Set-Default "$classes\$progId" 'CSV Viewer file'
Set-ItemProperty -Path "$classes\$progId" -Name 'FriendlyTypeName' -Value 'CSV Viewer file'
Set-Default "$classes\$progId\DefaultIcon" "`"$icon`",0"
Set-Default "$classes\$progId\shell\open\command" $openCommand

# Add to each extension's "Open with" list; the default handler is left alone
foreach ($ext in $extensions) {
    $key = "$classes\$ext\OpenWithProgids"
    Ensure-Key $key
    New-ItemProperty -Path $key -Name $progId -PropertyType String -Value '' -Force | Out-Null
}

# Application entry so "Open with" shows a friendly name
$appKey = "$classes\Applications\csvview.pyw"
Ensure-Key $appKey
Set-ItemProperty -Path $appKey -Name 'FriendlyAppName' -Value 'CSV Viewer'
Set-Default "$appKey\DefaultIcon" "`"$icon`",0"
Set-Default "$appKey\shell\open\command" $openCommand

# Terminal launcher: csvview.cmd file.csv (start returns to the prompt at once)
Set-Content -Path $cmdFile -Encoding ASCII -Value "@start `"`" `"$pythonw`" `"%~dp0csvview.pyw`" %*"

# Tell Explorer the associations changed
if (-not ('CsvViewer.ShellNotify' -as [type])) {
    Add-Type -Namespace CsvViewer -Name ShellNotify -MemberDefinition @'
[DllImport("shell32.dll")]
public static extern void SHChangeNotify(int wEventId, uint uFlags, IntPtr dwItem1, IntPtr dwItem2);
'@
}
[CsvViewer.ShellNotify]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)

Write-Host 'Installed:'
Write-Host "  $shortcut"
Write-Host "  $classes\$progId (Open with for $($extensions -join ', '))"
Write-Host "  $cmdFile"
