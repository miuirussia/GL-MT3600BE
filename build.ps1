# Requires Windows PowerShell 5.1 or PowerShell 7.
# Use the automatic argument array so make flags such as -j1 pass through unchanged.
$ErrorActionPreference = 'Stop'
# Handle native exit codes explicitly, including in PowerShell 7 profiles that enable this preference.
$PSNativeCommandUseErrorActionPreference = $false
$buildArgs = @($args)
$originalUid = $env:BUILD_UID
$originalGid = $env:BUILD_GID

Push-Location -LiteralPath $PSScriptRoot
try {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'Docker with Compose is required. Install and start Docker Desktop with Linux containers.'
    }
    docker compose version > $null
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker Compose is required. Install Docker Desktop with the Compose plugin.'
    }
    $dockerOs = docker info --format '{{.OSType}}'
    if ($LASTEXITCODE -ne 0) {
        throw 'Cannot connect to Docker. Start Docker Desktop and try again.'
    }
    if ($dockerOs -ne 'linux') {
        throw 'The builder requires Linux containers. Switch Docker Desktop to Linux containers.'
    }

    # Windows has no Unix host UID/GID; use the unprivileged container account.
    $env:BUILD_UID = '1000'
    $env:BUILD_GID = '1000'
    New-Item -ItemType Directory -Force -Path artifacts, .keys > $null

    if ($buildArgs.Count -gt 0 -and $buildArgs[0] -eq 'image') {
        docker compose build --pull builder
        exit $LASTEXITCODE
    }
    docker compose build builder
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    if ($buildArgs.Count -eq 0) { $buildArgs = @('build') }
    docker compose run --rm builder @buildArgs
    exit $LASTEXITCODE
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
finally {
    $env:BUILD_UID = $originalUid
    $env:BUILD_GID = $originalGid
    Pop-Location
}
