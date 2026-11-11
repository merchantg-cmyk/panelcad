# Install PanelCAD for the current Windows user and open the drawing window.
# Python 3.12 is downloaded from python.org only when this PC does not already have 3.11+.
# The venv stays in %USERPROFILE%\.venvs\panelcad, not on Google Drive.
#
# Quiet (no drawing window): Install-PanelCAD.cmd /quiet
# or: powershell -File scripts\Install-PanelCAD.ps1 -Quiet
# or set PANELCAD_INSTALL_QUIET=1 before starting. The .cmd also skips pause.
# PanelCAD's updater uses quiet mode and sets PANELCAD_BACKUP_DONE=1 after it
# has already copied the program folder. This script then does not take a second copy.
# Drawings, %USERPROFILE%\.panelcad (settings, logs, and the user part library), and the venv stay.
param(
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

if (-not $Quiet -and $env:PANELCAD_INSTALL_QUIET -eq "1") {
    $Quiet = $true
}

$PythonVersion = "3.12.10"
$InstallRoot = Join-Path $env:USERPROFILE "PanelCAD"
$VenvRoot = Join-Path $env:USERPROFILE ".venvs\panelcad"
$Example = Join-Path $InstallRoot "examples\dol_starter.pcad.yaml"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "== $Message" -ForegroundColor Cyan
}

function Get-SourceRoot {
    $here = Split-Path -Parent $PSCommandPath
    $parent = Split-Path -Parent $here
    if (Test-Path (Join-Path $here "pyproject.toml")) { return $here }
    if (Test-Path (Join-Path $parent "pyproject.toml")) { return $parent }
    throw "pyproject.toml was not next to this installer. Extract PanelCAD-<version>-setup.zip and run Install-PanelCAD.cmd from that folder (the one that contains pyproject.toml)."
}

function Get-PanelCadVersion([string]$Root) {
    $path = Join-Path $Root "src\panelcad\version.py"
    if (-not (Test-Path $path)) { return "unknown" }
    $line = Select-String -Path $path -Pattern '__version__\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($line -and $line.Matches.Count -gt 0) { return $line.Matches[0].Groups[1].Value }
    return "unknown"
}

function Backup-ExistingInstall([string]$Root, [string]$Python, [string]$Script) {
    # One copy of the program folder, for Help -> Roll back.
    # The updater sets PANELCAD_BACKUP_DONE=1 after it has already made that copy.
    # .panelcad and the venv live outside $Root and are not part of the copy.
    if ($env:PANELCAD_BACKUP_DONE -eq "1") { return $false }
    if (-not (Test-Path -LiteralPath $Root)) { return $false }
    $old = Get-PanelCadVersion $Root
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $config = Join-Path $env:USERPROFILE ".panelcad"
    $dest = Join-Path (Join-Path $config "backups") "PanelCAD-$old-$stamp"
    New-Item -ItemType Directory -Force -Path (Split-Path $dest -Parent) | Out-Null
    Write-Step "Backing up $Root to $dest"
    & $Python $Script --source $Root --dest $dest
    if ($LASTEXITCODE -ne 0) {
        throw "Could not back up the previous install (install_tree.py exit $LASTEXITCODE)."
    }
    $rollback = @{
        previous_version = $old
        backup = $dest
        install_root = $Root
        created = (Get-Date).ToUniversalTime().ToString("o")
    } | ConvertTo-Json
    New-Item -ItemType Directory -Force -Path $config | Out-Null
    Set-Content -LiteralPath (Join-Path $config "rollback.json") -Value $rollback -Encoding UTF8
    Remove-OldBackups $config
    return $true
}

function Remove-OldBackups([string]$Config) {
    # Keep the newest N program-folder backups. The GUI setting is keep_backups (default 3).
    $dir = Join-Path $Config "backups"
    if (-not (Test-Path -LiteralPath $dir)) { return }
    $keep = 3
    $settingsPath = Join-Path $Config "update-settings.json"
    if (Test-Path -LiteralPath $settingsPath) {
        try {
            $settings = Get-Content -Raw -LiteralPath $settingsPath | ConvertFrom-Json
            if ($null -ne $settings.keep_backups) {
                $parsed = [int]$settings.keep_backups
                if ($parsed -ge 1) { $keep = $parsed }
            }
        } catch {
            $keep = 3
        }
    }
    $old = Get-ChildItem -LiteralPath $dir -Directory | Sort-Object LastWriteTime -Descending | Select-Object -Skip $keep
    foreach ($item in $old) {
        Remove-Item -LiteralPath $item.FullName -Recurse -Force
    }
}

function Test-SameProfilePath([string]$Child, [string]$Parent) {
    if ([string]::IsNullOrWhiteSpace($Child) -or [string]::IsNullOrWhiteSpace($Parent)) { return $false }
    try {
        $childFull = [IO.Path]::GetFullPath($Child).TrimEnd('\')
        $parentFull = [IO.Path]::GetFullPath($Parent).TrimEnd('\')
    } catch {
        return $false
    }
    if ($childFull.Equals($parentFull, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    return $childFull.StartsWith($parentFull + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Write-UpdateNotice([string]$Version) {
    $config = Join-Path $env:USERPROFILE ".panelcad"
    New-Item -ItemType Directory -Force -Path $config | Out-Null
    $notice = @{ version = $Version; kind = "update" } | ConvertTo-Json
    Set-Content -LiteralPath (Join-Path $config "update-notice.json") -Value $notice -Encoding UTF8
}

function Test-Python312Plus([string]$Exe) {
    if (-not (Test-Path $Exe)) { return $false }
    $code = "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
    & $Exe -c $code
    return $LASTEXITCODE -eq 0
}

function Find-Python {
    $local = Join-Path $env:LOCALAPPDATA "Programs\Python"
    if (Test-Path $local) {
        $found = Get-ChildItem $local -Filter python.exe -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "Python3(1[1-9]|[2-9][0-9])\\" } |
            Select-Object -ExpandProperty FullName
        foreach ($exe in $found) {
            if (Test-Python312Plus $exe) { return $exe }
        }
    }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        foreach ($tag in @("-3.12", "-3.13", "-3.14", "-3")) {
            $exe = & py $tag -c "import sys; print(sys.executable)" 2>$null
            if ($exe -and (Test-Python312Plus $exe.Trim())) { return $exe.Trim() }
        }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and (Test-Python312Plus $cmd.Source)) { return $cmd.Source }
    return $null
}

function Install-Python {
    $arch = $env:PROCESSOR_ARCHITECTURE
    $suffix = "amd64"
    if ($arch -eq "ARM64") { $suffix = "arm64" }
    $name = "python-$PythonVersion-$suffix.exe"
    $url = "https://www.python.org/ftp/python/$PythonVersion/$name"
    $dest = Join-Path $env:TEMP $name
    Write-Step "Downloading Python $PythonVersion ($suffix) from python.org"
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
    Write-Step "Running the Python installer"
    $args = @(
        "/quiet",
        "InstallAllUsers=0",
        "PrependPath=1",
        "Include_pip=1",
        "Include_launcher=1",
        "Include_test=0",
        "Shortcuts=0"
    )
    $proc = Start-Process -FilePath $dest -ArgumentList $args -Wait -PassThru
    if ($proc.ExitCode -ne 0) {
        throw "The Python installer exited with code $($proc.ExitCode)."
    }
    $installed = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    if (Test-Python312Plus $installed) { return $installed }
    $again = Find-Python
    if ($again) { return $again }
    throw "Python installed, but python.exe was not found under $env:LOCALAPPDATA\Programs\Python."
}

function Test-Tesseract {
    $cmd = Get-Command tesseract -ErrorAction SilentlyContinue
    if ($cmd) { return $true }
    $roots = @()
    if ($env:ProgramFiles) { $roots += $env:ProgramFiles }
    if (${env:ProgramFiles(x86)}) { $roots += ${env:ProgramFiles(x86)} }
    foreach ($root in $roots) {
        $exe = Join-Path $root "Tesseract-OCR\tesseract.exe"
        if (Test-Path $exe) { return $true }
    }
    return $false
}

function Test-BundledTesseract([string]$Source) {
    $exe = Join-Path $Source "resources\ocr\tesseract\tesseract.exe"
    $trained = Join-Path $Source "resources\ocr\tesseract\tessdata\eng.traineddata"
    return (Test-Path -LiteralPath $exe) -and (Test-Path -LiteralPath $trained)
}

function Get-ElectricalLockWheelNames([string]$Source) {
    # The lock names every wheel the offline ONNX Runtime install needs,
    # including flatbuffers, numpy, packaging, and protobuf.
    # Write one string per wheel. An empty result writes nothing, so the
    # caller's @() has Count 0. A nested empty array counts as one item,
    # and Join-Path then refuses to treat that array as a path.
    $lock = Join-Path $Source "packaging\electrical-ocr.lock.json"
    if (-not (Test-Path -LiteralPath $lock)) { return }
    $data = Get-Content -Raw -LiteralPath $lock | ConvertFrom-Json
    foreach ($wheel in @($data.wheels)) {
        $name = [string]$wheel.filename
        if ($name) { $name }
    }
}

function Test-BundledElectrical([string]$Source) {
    $root = Join-Path $Source "resources\ocr\electrical"
    $need = @(
        "models\PP-OCRv5_mobile_det_onnx\inference.onnx",
        "models\PP-OCRv5_mobile_det_onnx\inference.yml",
        "models\en_PP-OCRv5_mobile_rec_onnx\inference.onnx",
        "models\en_PP-OCRv5_mobile_rec_onnx\inference.yml",
        "models\PP-OCRv6_small_det_onnx\inference.onnx",
        "models\PP-OCRv6_small_det_onnx\inference.yml",
        "models\PP-OCRv6_small_rec_onnx\inference.onnx",
        "models\PP-OCRv6_small_rec_onnx\inference.yml",
        "models\PP-LCNet_x1_0_textline_ori_onnx\inference.onnx",
        "models\PP-LCNet_x1_0_textline_ori_onnx\inference.yml",
        "licenses\NOTICE.md"
    )
    foreach ($rel in $need) {
        if (-not (Test-Path -LiteralPath (Join-Path $root $rel))) { return $false }
    }
    $wheels = Join-Path $root "wheels"
    if (-not (Test-Path -LiteralPath $wheels)) { return $false }
    $names = @(Get-ElectricalLockWheelNames $Source)
    if ($names.Count -lt 1) { return $false }
    foreach ($name in $names) {
        if ($name -isnot [string] -or -not $name) { return $false }
        if (-not (Test-Path -LiteralPath (Join-Path $wheels $name))) { return $false }
    }
    return $true
}

function Install-BundledOnnxRuntime([string]$VenvPy, [string]$Source) {
    $wheelDir = Join-Path $Source "resources\ocr\electrical\wheels"
    $spec = & $VenvPy -c "import sys,platform; print(sys.version_info[0]); print(sys.version_info[1]); print(platform.machine())"
    if ($LASTEXITCODE -ne 0) { throw "Could not read the venv Python version." }
    $lines = @($spec) | ForEach-Object { "$_".Trim() } | Where-Object { $_ }
    if ($lines.Count -lt 3) { throw "Could not read the venv Python version." }
    $tag = "cp$($lines[0])$($lines[1])"
    $machine = $lines[2].ToLower()
    $arch = "win_amd64"
    if ($machine -eq "arm64" -or $machine -eq "aarch64") { $arch = "win_arm64" }
    $match = @(Get-ChildItem -LiteralPath $wheelDir -Filter "onnxruntime-*$tag-*$arch.whl" -ErrorAction SilentlyContinue)
    if ($match.Count -lt 1) {
        throw "This setup has no ONNX Runtime wheel for Python $($lines[0]).$($lines[1]) ($arch). The scan reader does not download one."
    }
    Write-Step "Installing the shipped ONNX Runtime ($($match[0].Name))"
    # --find-links is the only place pip may look. The required wheels
    # (flatbuffers, numpy, packaging, protobuf) sit next to this file.
    & $VenvPy -m pip install --disable-pip-version-check --no-index "--only-binary=:all:" --find-links $wheelDir $match[0].FullName
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install the shipped ONNX Runtime wheel. The wheels folder must also contain flatbuffers, numpy, packaging, and protobuf. The scan reader does not download them."
    }
}

function Copy-Source([string]$From, [string]$To, [string]$Python) {
    # scripts/install_tree.py always overwrites, including files whose size
    # and timestamp already match (robocopy /E /IS /IT). It then deletes
    # __pycache__ and stale .pyc files under the install's src, tests, and
    # docs only. It does not mirror-delete drawings or other extra files.
    Write-Step "Copying PanelCAD to $To"
    $script = Join-Path $From "scripts\install_tree.py"
    if (-not (Test-Path -LiteralPath $script)) {
        throw "Missing $script. Extract the whole setup folder and run Install-PanelCAD.cmd from there."
    }
    & $Python $script --source $From --dest $To
    if ($LASTEXITCODE -ne 0) {
        throw "Could not copy the program (install_tree.py exit $LASTEXITCODE)."
    }
}

function Assert-InstalledVersion([string]$Python, [string]$Expected) {
    Write-Step "Checking that Python imports PanelCAD $Expected"
    # Normal import, not python -B. A stale version.pyc must fail this check.
    $raw = & $Python -c "import panelcad; print(panelcad.__version__)"
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw "Could not import panelcad after install (python exit $code)."
    }
    $lines = @($raw) | ForEach-Object { "$_".Trim() } | Where-Object { $_ }
    $reported = $lines | Select-Object -Last 1
    if ($reported -ne $Expected) {
        throw "Version check failed. Installer release: $Expected. Python reports: '$reported'. Program folder: $InstallRoot. PanelCAD is still importing an older copy. Close PanelCAD if it is open, then run Install-PanelCAD.cmd again from the new setup folder."
    }
    Write-Host "Python reports PanelCAD $reported"
}

# Running this file installs PanelCAD. Dot-sourcing only loads the functions.
# return here would leave the caller, so the install body stays inside this if.
if ($MyInvocation.InvocationName -ne '.') {

$source = Get-SourceRoot
$version = Get-PanelCadVersion $source
Write-Host "PanelCAD $version installer"
Write-Host "Source: $source"
Write-Host "This updates $InstallRoot. Drawings you saved and $VenvRoot stay."
Write-Host "Program files are overwritten even when the size and date match."
Write-Host "Settings, logs, and the user part library stay in $env:USERPROFILE\.panelcad."
if ($Quiet) {
    Write-Host "Quiet mode: pause is skipped and the drawing window is not opened."
}

$python = Find-Python
if (-not $python) {
    $python = Install-Python
}
Write-Step "Using $python"
& $python -c "import sys; print(sys.version)"

if (-not (Test-BundledElectrical $source)) {
    throw "This setup is missing the electrical OCR reader (resources\ocr\electrical), including the wheels ONNX Runtime requires. Rebuild the setup zip. A scan does not download weights or ONNX Runtime."
}
Write-Host "Pixel OCR uses the electrical reader shipped in this setup."
Write-Host "The installer does not download OCR models, does not change PATH, and does not run a separate OCR installer."
if (Test-BundledTesseract $source) {
    Write-Host "Tesseract 5 is in this setup only as a fallback. A scan does not need it."
} elseif (Test-Tesseract) {
    Write-Host "A tesseract command is already on this PC. It is used only if the electrical reader cannot load."
}
$treeScript = Join-Path $source "scripts\install_tree.py"
$didBackup = Backup-ExistingInstall $InstallRoot $python $treeScript

Copy-Source $source $InstallRoot $python

Write-Step "Creating the virtual environment"
if (-not (Test-Path (Join-Path $VenvRoot "Scripts\python.exe"))) {
    New-Item -ItemType Directory -Force -Path (Split-Path $VenvRoot -Parent) | Out-Null
    & $python -m venv $VenvRoot
}
$venvPy = Join-Path $VenvRoot "Scripts\python.exe"
if (-not (Test-Path $venvPy)) { throw "venv python was not created at $venvPy" }

Write-Step "Installing PanelCAD and Qt (this is the long step)"
& $venvPy -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
Install-BundledOnnxRuntime $venvPy $source
& $venvPy -m pip install -e $InstallRoot
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
# paddlex asks for opencv-contrib-python. That build carries its own Qt and
# replaces the headless cv2 PanelCAD already uses. Put the headless module
# back. The contrib package name stays installed so the OCR dependency check
# still passes, and import cv2 is the headless build.
& $venvPy -m pip install --force-reinstall --no-deps "opencv-python-headless>=4.9,<5"
if ($LASTEXITCODE -ne 0) { throw "Could not restore opencv-python-headless" }

Assert-InstalledVersion $venvPy $version

$launcher = Join-Path $InstallRoot "PanelCAD.cmd"
@"
@echo off
set QT_QPA_PLATFORM=
cd /d "$InstallRoot"
"$venvPy" -m panelcad gui "$Example"
"@ | Set-Content -Path $launcher -Encoding ASCII

# Only create or update a shortcut that points at THIS install.
# [Environment]::GetFolderPath("Desktop") is the interactive user's desktop, not
# $env:USERPROFILE\Desktop. A side-folder install must not rewrite that .lnk.
# scripts/install_tree.py shortcut_action returns create, update, or leave.
$shortcutPath = ""
$startMenuPath = ""
$iconFile = Join-Path $InstallRoot "src\panelcad\assets\panelcad.ico"
$knownDesktop = [Environment]::GetFolderPath("Desktop")
$profileDesktop = Join-Path $env:USERPROFILE "Desktop"
$desktop = $null
if (Test-SameProfilePath $knownDesktop $env:USERPROFILE) {
    $desktop = $knownDesktop
} elseif (Test-Path -LiteralPath $profileDesktop) {
    $desktop = $profileDesktop
} else {
    Write-Host "Desktop is outside this profile. Not changing PanelCAD.lnk."
}
if ($desktop) {
    $shortcutPath = Join-Path $desktop "PanelCAD.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $existingWork = ""
    $shortcutExists = $false
    if (Test-Path -LiteralPath $shortcutPath) {
        $shortcutExists = $true
        $existingWork = [string]($shell.CreateShortcut($shortcutPath).WorkingDirectory)
    }
    # PowerShell drops an empty string when splatting a native program.
    # "--existing", "" would reach Python as a bare --existing and argparse
    # would exit 2 after the copy and pip install. Omit it when blank.
    $actionArgs = @(
        $treeScript,
        "--shortcut-action",
        "--install-root", $InstallRoot,
        "--desktop", $desktop,
        "--user-profile", $env:USERPROFILE
    )
    if (-not [string]::IsNullOrWhiteSpace($existingWork)) {
        $actionArgs += @("--existing", $existingWork)
    }
    if ($shortcutExists) { $actionArgs += "--shortcut-exists" }
    $action = ""
    try {
        $action = (& $python @actionArgs | Out-String).Trim().ToLower()
    } catch {
        $action = ""
    }
    if ($LASTEXITCODE -ne 0 -or @("create", "update", "leave") -notcontains $action) {
        Write-Host "Could not decide whether to update the desktop shortcut. Leaving it unchanged."
        $LASTEXITCODE = 0
    } elseif ($action -eq "leave") {
        Write-Host "Leaving shortcut $shortcutPath (it points at '$existingWork', not $InstallRoot)."
    } else {
        if (-not (Test-Path -LiteralPath $desktop)) {
            New-Item -ItemType Directory -Force -Path $desktop | Out-Null
        }
        $link = $shell.CreateShortcut($shortcutPath)
        $link.TargetPath = $venvPy
        $link.Arguments = "-m panelcad gui `"$Example`""
        $link.WorkingDirectory = $InstallRoot
        $link.Description = "PanelCAD"
        if (Test-Path -LiteralPath $iconFile) {
            $link.IconLocation = "$iconFile,0"
        }
        $link.Save()

        $programs = [Environment]::GetFolderPath("Programs")
        if ($programs -and (Test-SameProfilePath $programs $env:USERPROFILE)) {
            if (-not (Test-Path -LiteralPath $programs)) {
                New-Item -ItemType Directory -Force -Path $programs | Out-Null
            }
            $startMenuPath = Join-Path $programs "PanelCAD.lnk"
            $start = $shell.CreateShortcut($startMenuPath)
            $start.TargetPath = $venvPy
            $start.Arguments = "-m panelcad gui `"$Example`""
            $start.WorkingDirectory = $InstallRoot
            $start.Description = "PanelCAD"
            if (Test-Path -LiteralPath $iconFile) {
                $start.IconLocation = "$iconFile,0"
            }
            $start.Save()
        }
    }
}

# A quiet first install has nothing to announce. An upgrade that backed up this
# folder, or the updater (quiet, backup already taken), writes "Updated to X".
if ($didBackup -or ($Quiet -and $env:PANELCAD_BACKUP_DONE -eq "1")) {
    Write-UpdateNotice $version
}

if (-not $Quiet) {
    Write-Step "Opening PanelCAD"
    Start-Process -FilePath $venvPy -ArgumentList @("-m", "panelcad", "gui", $Example) -WorkingDirectory $InstallRoot
} else {
    Write-Host ""
    Write-Host "Quiet mode: PanelCAD was not opened."
}
Write-Host ""
Write-Host "Installed PanelCAD $version." -ForegroundColor Green
Write-Host "  Program:  $InstallRoot"
Write-Host "  Python:   $VenvRoot"
Write-Host "  Shortcut: $shortcutPath"
if ($startMenuPath) {
    Write-Host "  Start menu: $startMenuPath"
}
Write-Host ""
Write-Host "Next time, double-click PanelCAD on the desktop."
Write-Host "Open your own .pcad.yaml with File -> Open. Those files were not overwritten."
}
