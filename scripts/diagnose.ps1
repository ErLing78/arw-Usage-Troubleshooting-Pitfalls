# diagnose.ps1
#
# Report what actually works on this machine, before you waste an hour
# discovering it the hard way.
#
# Every probe is isolated in its own try/catch and nothing here aborts on
# failure - a diagnostic that dies because one check failed is useless.
#
# Run it (the scriptblock wrapper is deliberate; a Restricted execution policy
# refuses to run .ps1 files directly):
#
#   & ([scriptblock]::Create((Get-Content -Raw .\diagnose.ps1)))
#   & ([scriptblock]::Create((Get-Content -Raw .\diagnose.ps1))) -OutputPath C:\report.json
#
# The report is written to disk because some hosts do not return stdout.

param(
    [string]$OutputPath = ''
)

# Continue, not Stop: a failed probe must not abort the whole sweep.
$ErrorActionPreference = 'Continue'

$global:diag = [ordered]@{
    script      = 'awesome-restricted-windows / diagnose.ps1'
    timestamp   = (Get-Date).ToString('o')
    machine     = $env:COMPUTERNAME
    user        = $env:USERNAME
}

# -- helper ----------------------------------------------------------------
function Add-Probe {
    param([string]$Group, [string]$Name, [scriptblock]$Body)
    if (-not $global:diag.Contains($Group)) {
        $global:diag[$Group] = [ordered]@{}
    }
    try {
        $value = & $Body
        $global:diag[$Group][$Name] = [ordered]@{ ok = $true; value = $value }
    } catch {
        $global:diag[$Group][$Name] = [ordered]@{
            ok    = $false
            error = $_.Exception.Message
            type  = $_.Exception.GetType().Name
        }
    }
}

# -- 1. host ---------------------------------------------------------------
Add-Probe 'host' 'powershellVersion' { $PSVersionTable.PSVersion.ToString() }
Add-Probe 'host' 'edition' { $PSVersionTable.PSEdition }
Add-Probe 'host' 'is64BitProcess' { [Environment]::Is64BitProcess }
Add-Probe 'host' 'is64BitOperatingSystem' { [Environment]::Is64BitOperatingSystem }
Add-Probe 'host' 'osVersion' { (Get-CimInstance Win32_OperatingSystem).Caption }
Add-Probe 'host' 'clrVersion' { [System.Environment]::Version.ToString() }
Add-Probe 'host' 'culture' { (Get-Culture).Name }

# -- 2. execution policy ---------------------------------------------------
# The list form matters: a machine may be Restricted by machine policy while
# the user scope says otherwise.
Add-Probe 'executionPolicy' 'list' {
    (Get-ExecutionPolicy -List | ForEach-Object { "$($_.Scope)=$($_.ExecutionPolicy)" }) -join '; '
}
Add-Probe 'executionPolicy' 'effective' { (Get-ExecutionPolicy).ToString() }

# -- 3. Add-Type, the common blocker ---------------------------------------
Add-Probe 'assemblyLoading' 'addTypeAssemblyName' {
    Add-Type -AssemblyName System.Drawing -ErrorAction Stop
    'succeeded'
}
Add-Probe 'assemblyLoading' 'addTypeTypeDefinition' {
    Add-Type -TypeDefinition 'public class DiagProbe { public static int N = 1; }' -ErrorAction Stop
    'succeeded'
}
Add-Probe 'assemblyLoading' 'loadWithPartialName' {
    [void][System.Reflection.Assembly]::LoadWithPartialName('System.Drawing')
    'succeeded'
}

# -- 4. LoadFrom, the usual workaround ------------------------------------
$frameworkRoot = if ([Environment]::Is64BitProcess) {
    'C:\Windows\Microsoft.NET\Framework64\v4.0.30319'
} else {
    'C:\Windows\Microsoft.NET\Framework\v4.0.30319'
}
Add-Probe 'assemblyLoading' 'frameworkRoot' { $frameworkRoot }

# Note the variable names here.  PowerShell resolves free variables in a
# scriptblock dynamically, against the *caller's* scope at invocation time, not
# against the scope where the block was written.  So a loop variable called
# $name would be shadowed by Add-Probe's own $Name parameter, and every probe
# would silently read the wrong value.  Prefixing with $asm keeps them distinct.
$assemblies = [ordered]@{
    'System.Drawing'                = 'System.Drawing.dll'
    'System.Windows.Forms'          = 'System.Windows.Forms.dll'
    'System.Runtime.WindowsRuntime' = 'System.Runtime.WindowsRuntime.dll'
    'System.Management'             = 'System.Management.dll'
}

foreach ($asmName in $assemblies.Keys) {
    $asmProbe = 'loadFrom_' + ($asmName -replace '\.', '_')
    $asmPath = Join-Path $frameworkRoot $assemblies[$asmName]
    Add-Probe 'assemblyLoading' $asmProbe {
        if (-not [System.IO.File]::Exists($asmPath)) {
            throw "assembly not present: $asmPath"
        }
        $asm = [System.Reflection.Assembly]::LoadFrom($asmPath)
        "$($asm.GetName().Name) $($asm.GetName().Version)"
    }
}

# -- 5. does the imaging stack actually decode? ---------------------------
Add-Probe 'imaging' 'gdiPlusRoundTrip' {
    $null = [System.Reflection.Assembly]::LoadFrom((Join-Path $frameworkRoot 'System.Drawing.dll'))
    $bmp = New-Object System.Drawing.Bitmap(4, 4)
    $bmp.SetPixel(0, 0, [System.Drawing.Color]::Red)
    $saved = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "diag_$PID.png")
    $bmp.Save($saved, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    $bytes = (Get-Item $saved).Length
    Remove-Item $saved -Force -ErrorAction SilentlyContinue
    "wrote $bytes bytes"
}

# -- 6. external programs --------------------------------------------------
# `where.exe` rather than Get-Command: we care about executables on PATH, not
# about aliases or cmdlets that happen to share a name.
Add-Probe 'programs' 'found' {
    $candidates = @(
        'magick', 'convert', 'ffmpeg', 'gm',
        'python', 'python3', 'py', 'node', 'npm', 'git', 'curl',
        'tesseract', '7z', 'tar', 'robocopy', 'where',
        'pwsh', 'cmd', 'wmic', 'certutil', 'bitsadmin'
    )
    $found = [ordered]@{}
    foreach ($exe in $candidates) {
        $located = & where.exe $exe 2>$null
        if ($LASTEXITCODE -eq 0 -and $located) {
            $found[$exe] = @($located)[0]
        }
    }
    $found
}

Add-Probe 'programs' 'noteConvertIsNotImageMagick' {
    # C:\Windows\System32\convert.exe is the FAT-to-NTFS converter, not
    # ImageMagick.  Scripts that probe for "convert" hit this and then wonder
    # why nothing works.
    $located = & where.exe convert 2>$null
    if ($LASTEXITCODE -eq 0 -and $located) {
        @($located) -join '; '
    } else {
        'not found'
    }
}

# -- 7. Python ------------------------------------------------------------
Add-Probe 'python' 'version' { & python --version 2>&1 | Out-String }
Add-Probe 'python' 'thirdPartyModules' {
    $script = @'
import importlib
names = ["PIL", "numpy", "cv2", "pytesseract", "requests", "win32com", "docx", "openpyxl"]
out = {}
for n in names:
    try:
        importlib.import_module(n)
        out[n] = "importable"
    except Exception as e:
        out[n] = "missing: " + type(e).__name__
print("|".join(f"{k}={v}" for k, v in out.items()))
'@
    $temp = Join-Path ([System.IO.Path]::GetTempPath()) "diag_modules_$PID.py"
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($temp, $script, $enc)
    try {
        & python $temp 2>&1 | Out-String | ForEach-Object { $_.Trim() }
    } finally {
        Remove-Item $temp -Force -ErrorAction SilentlyContinue
    }
}
Add-Probe 'python' 'pipConfig' {
    $config = Join-Path $env:APPDATA 'pip\pip.ini'
    if ([System.IO.File]::Exists($config)) {
        [System.IO.File]::ReadAllText($config).Trim()
    } else {
        'no pip.ini at ' + $config
    }
}

# -- 8. WinRT availability ------------------------------------------------
Add-Probe 'winrt' 'ocrLanguages' {
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType = WindowsRuntime]
    $tags = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages |
        ForEach-Object { $_.LanguageTag }
    if ($tags) { $tags -join ', ' } else { 'none installed' }
}

# -- 9. recycle bin -------------------------------------------------------
Add-Probe 'recycleBin' 'userSid' {
    $sid = ([System.Security.Principal.WindowsIdentity]::GetCurrent()).User.Value
    $sid
}
Add-Probe 'recycleBin' 'pathAccessible' {
    $sid = ([System.Security.Principal.WindowsIdentity]::GetCurrent()).User.Value
    $path = "C:\`$Recycle.Bin\$sid"
    if (-not [System.IO.Directory]::Exists($path)) {
        throw "not found: $path"
    }
    # Deliberately os.listdir-equivalent rather than Get-ChildItem: on a recycle
    # bin holding tens of thousands of entries, enumeration is dramatically
    # faster through the raw .NET call.
    $entries = [System.IO.Directory]::GetFileSystemEntries($path)
    $iFiles = @($entries | Where-Object { [System.IO.Path]::GetFileName($_) -like '$I*' })
    "$($entries.Count) entries, $($iFiles.Count) `$I metadata files"
}

# -- 10. derived capabilities ---------------------------------------------
# Raw probe results are hard to read.  This turns them into the questions
# people actually ask, so the report can be skimmed in five seconds.
function Test-ProbeOk {
    param([string]$Group, [string]$Key)
    $section = $global:diag[$Group]
    if (-not $section) { return $false }
    $entry = $section[$Key]
    if (-not $entry) { return $false }
    return [bool]$entry['ok']
}

$hasDrawing = Test-ProbeOk 'assemblyLoading' 'loadFrom_System_Drawing'
$hasForms = Test-ProbeOk 'assemblyLoading' 'loadFrom_System_Windows_Forms'
$hasGdi = Test-ProbeOk 'imaging' 'gdiPlusRoundTrip'

$global:diag['capabilities'] = [ordered]@{
    # Compile C# on the fly (Add-Type -TypeDefinition)
    addTypeCompile        = (Test-ProbeOk 'assemblyLoading' 'addTypeTypeDefinition')
    # Load an existing framework assembly by full path
    loadFromAssembly      = $hasDrawing
    # Decode JPEG and encode PNG/BMP through GDI+
    gdiPlusImaging        = $hasGdi
    # Capture the screen (needs WinForms + GDI+)
    screenCapture         = ($hasForms -and $hasGdi)
    # Offline OCR through the built-in Windows engine
    offlineOcr            = (Test-ProbeOk 'winrt' 'ocrLanguages')
    # Reach the per-user recycle bin directory
    recycleBinAccess      = (Test-ProbeOk 'recycleBin' 'pathAccessible')
}

# -- 11. summary ----------------------------------------------------------
$blockers = @()
foreach ($group in $global:diag.Keys) {
    if ($group -in @('script', 'timestamp', 'machine', 'user')) { continue }
    $section = $global:diag[$group]
    if ($section -isnot [System.Collections.IDictionary]) { continue }
    foreach ($key in $section.Keys) {
        $entry = $section[$key]
        if ($entry -is [System.Collections.IDictionary] -and $entry.Contains('ok') -and -not $entry['ok']) {
            $blockers += "$group.$key"
        }
    }
}
$global:diag['summary'] = [ordered]@{
    failedProbes = $blockers.Count
    failed       = $blockers
}

# -- write ----------------------------------------------------------------
if (-not $OutputPath) {
    $OutputPath = Join-Path (Get-Location).Path 'diagnose_powershell.json'
}
$json = $global:diag | ConvertTo-Json -Depth 8
$encoding = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($OutputPath, $json, $encoding)

Write-Output "diagnostic written to $OutputPath"
Write-Output "$($blockers.Count) probe(s) failed"
