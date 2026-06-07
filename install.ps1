param(
    [switch]$NoSelfElevate,
    [switch]$SkipChocolateyFallback
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "== $Message ==" -ForegroundColor Cyan
}

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-Command {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Invoke-Logged {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )
    Write-Host "> $FilePath $($Arguments -join ' ')"
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath exited with code $LASTEXITCODE"
    }
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Install-WithWinget {
    param(
        [string]$Id,
        [string]$Name
    )
    if (-not (Test-Command "winget")) {
        return $false
    }
    Write-Step "Installing $Name with winget"
    Invoke-Logged "winget" @(
        "install",
        "--exact",
        "--id", $Id,
        "--accept-package-agreements",
        "--accept-source-agreements",
        "--silent"
    )
    Refresh-Path
    return $true
}

function Ensure-Chocolatey {
    if (Test-Command "choco") {
        return $true
    }
    if ($SkipChocolateyFallback) {
        return $false
    }
    Write-Step "Installing Chocolatey fallback"
    Set-ExecutionPolicy Bypass -Scope Process -Force
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
    Invoke-Expression ((New-Object System.Net.WebClient).DownloadString("https://community.chocolatey.org/install.ps1"))
    Refresh-Path
    return (Test-Command "choco")
}

function Install-WithChocolatey {
    param(
        [string]$Package,
        [string]$Name
    )
    if (-not (Ensure-Chocolatey)) {
        return $false
    }
    Write-Step "Installing $Name with Chocolatey"
    Invoke-Logged "choco" @("install", $Package, "-y", "--no-progress")
    Refresh-Path
    return $true
}

function Test-Python314 {
    if (Test-Command "py") {
        & py -3.14 --version *> $null
        if ($LASTEXITCODE -eq 0) {
            return $true
        }
    }
    if (Test-Command "python") {
        $versionOutput = (& python --version 2>&1) -join " "
        if ($versionOutput -match "Python\s+(\d+)\.(\d+)") {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 14)) {
                return $true
            }
        }
    }
    return $false
}

function Ensure-Python314 {
    if (Test-Python314) {
        Write-Host "Python 3.14+ already found." -ForegroundColor Green
        return
    }
    $installed = $false
    try {
        $installed = Install-WithWinget -Id "Python.Python.3.14" -Name "Python 3.14"
    }
    catch {
        Write-Warning "winget install failed for Python 3.14: $($_.Exception.Message)"
    }
    if (-not $installed) {
        $installed = Install-WithChocolatey -Package "python" -Name "Python 3.14"
    }
    Refresh-Path
    if (-not (Test-Python314)) {
        throw "Python 3.14+ is still not available. Open a new PowerShell window or install Python 3.14 manually."
    }
}

function Ensure-Tool {
    param(
        [string]$CommandName,
        [string]$WingetId,
        [string]$ChocoPackage,
        [string]$DisplayName
    )
    if (Test-Command $CommandName) {
        Write-Host "$DisplayName already found: $((Get-Command $CommandName).Source)" -ForegroundColor Green
        return
    }
    $installed = $false
    try {
        $installed = Install-WithWinget -Id $WingetId -Name $DisplayName
    }
    catch {
        Write-Warning "winget install failed for ${DisplayName}: $($_.Exception.Message)"
    }
    if (-not $installed) {
        $installed = Install-WithChocolatey -Package $ChocoPackage -Name $DisplayName
    }
    if (-not $installed) {
        throw "Could not install $DisplayName. Install it manually and rerun this script."
    }
}

function Get-PythonCommand {
    if (Test-Command "py") {
        & py -3.14 --version *> $null
        if ($LASTEXITCODE -eq 0) {
            return @("py", "-3.14")
        }
    }
    if (Test-Command "python") {
        $versionOutput = (& python --version 2>&1) -join " "
        if ($versionOutput -match "Python\s+(\d+)\.(\d+)") {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 14)) {
                return @("python")
            }
        }
    }
    return $null
}

if (-not $IsWindows -and $PSVersionTable.PSEdition -eq "Core") {
    throw "This bootstrapper is for Windows."
}

if (-not (Test-Administrator) -and -not $NoSelfElevate) {
    Write-Step "Requesting administrator approval"
    $argsList = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", "`"$PSCommandPath`"",
        "-NoSelfElevate"
    )
    if ($SkipChocolateyFallback) {
        $argsList += "-SkipChocolateyFallback"
    }
    $process = Start-Process -FilePath "powershell" -ArgumentList $argsList -Verb RunAs -Wait -PassThru
    exit $process.ExitCode
}

Write-Step "Movie Trailer Downloader dependency bootstrap"

Ensure-Python314
Ensure-Tool -CommandName "ffmpeg" -WingetId "Gyan.FFmpeg" -ChocoPackage "ffmpeg" -DisplayName "FFmpeg"
Ensure-Tool -CommandName "deno" -WingetId "DenoLand.Deno" -ChocoPackage "deno" -DisplayName "Deno"

Refresh-Path
$pythonCommand = Get-PythonCommand
if (-not $pythonCommand) {
    throw "Python was installed, but is not available in this shell yet. Open a new PowerShell window and rerun this script."
}

Write-Step "Installing Python package dependencies"
$pythonExe = $pythonCommand[0]
$pythonArgs = @()
if ($pythonCommand.Count -gt 1) {
    $pythonArgs = $pythonCommand[1..($pythonCommand.Count - 1)]
}

Invoke-Logged $pythonExe ($pythonArgs + @("-m", "pip", "install", "-U", "pip"))
Invoke-Logged $pythonExe ($pythonArgs + @("-m", "pip", "install", "-U", "yt-dlp[default]"))

Write-Step "Dependency check"
Invoke-Logged $pythonExe ($pythonArgs + @((Join-Path $PSScriptRoot "movie_trailer_downloader.py"), "--check-deps"))

Write-Host ""
Write-Host "Done. You can start the GUI with:" -ForegroundColor Green
Write-Host "  python `"$PSScriptRoot\movie_trailer_downloader.py`""
