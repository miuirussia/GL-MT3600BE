# Self-contained launcher tests; no Docker daemon or Pester installation required.
$ErrorActionPreference = 'Stop'
$launcher = Join-Path (Split-Path $PSScriptRoot -Parent) 'build.ps1'
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('routerfw tests ' + [Guid]::NewGuid())
$originalUid = $env:BUILD_UID
$originalGid = $env:BUILD_GID
$originalJobs = $env:JOBS
$global:routerfwLauncherTest = @{}

function Assert-Equal($actual, $expected) {
    $actualJson = ConvertTo-Json -InputObject $actual -Compress -Depth 8
    $expectedJson = ConvertTo-Json -InputObject $expected -Compress -Depth 8
    if ($actualJson -cne $expectedJson) {
        throw "Expected $expectedJson; got $actualJson"
    }
}

# Shadow Docker while exercising the actual launcher, including its exit/finally paths.
function docker {
    $command = $args -join ' '
    $global:routerfwLauncherTest.dockerCalls.Add(@($args))
    Assert-Equal (Get-Location).Path $global:routerfwLauncherTest.project
    $global:LASTEXITCODE = 0
    if ($command -eq 'compose version') {
        if ($global:routerfwLauncherTest.failureCommand -eq $command) {
            $global:LASTEXITCODE = $global:routerfwLauncherTest.failureCode
        }
        return
    }
    if ($command -eq 'info --format {{.OSType}}') {
        if ($global:routerfwLauncherTest.failureCommand -eq 'info') {
            $global:LASTEXITCODE = 1
            return
        }
        return $global:routerfwLauncherTest.dockerOs
    }
    Assert-Equal $env:BUILD_UID '1000'
    Assert-Equal $env:BUILD_GID '1000'
    Assert-Equal $env:JOBS '8'
    Assert-Equal (Test-Path -LiteralPath (Join-Path $global:routerfwLauncherTest.project 'artifacts') -PathType Container) $true
    Assert-Equal (Test-Path -LiteralPath (Join-Path $global:routerfwLauncherTest.project '.keys') -PathType Container) $true
    if ($command -eq $global:routerfwLauncherTest.failureCommand) {
        $global:LASTEXITCODE = $global:routerfwLauncherTest.failureCode
    }
}

function Test-Launcher {
    param(
        [string[]]$Arguments = @(),
        [string]$FailureCommand = '',
        [int]$FailureCode = 0,
        [string]$DockerOs = 'linux',
        [int]$ExpectedCode = 0,
        [object[]]$ExpectedCalls
    )
    $global:routerfwLauncherTest.failureCommand = $FailureCommand
    $global:routerfwLauncherTest.failureCode = $FailureCode
    $global:routerfwLauncherTest.dockerOs = $DockerOs
    $global:routerfwLauncherTest.dockerCalls = [Collections.Generic.List[object]]::new()
    $env:BUILD_UID = '2345'
    $env:BUILD_GID = '6789'
    $env:JOBS = '8'
    $before = (Get-Location).Path
    & (Join-Path $global:routerfwLauncherTest.project 'build.ps1') @Arguments
    Assert-Equal $LASTEXITCODE $ExpectedCode
    Assert-Equal (Get-Location).Path $before
    Assert-Equal $env:BUILD_UID '2345'
    Assert-Equal $env:BUILD_GID '6789'
    Assert-Equal @($global:routerfwLauncherTest.dockerCalls.ToArray()) $ExpectedCalls
}

try {
    # A path with spaces catches accidental path splitting and caller-CWD assumptions.
    $global:routerfwLauncherTest.project = (New-Item -ItemType Directory -Path (Join-Path $testRoot 'project with spaces')).FullName
    Copy-Item -LiteralPath $launcher -Destination $global:routerfwLauncherTest.project
    Push-Location -LiteralPath $testRoot
    try {
        $preflight = @(@('compose', 'version'), @('info', '--format', '{{.OSType}}'))
        $build = @('compose', 'build', 'builder')
        Test-Launcher -ExpectedCalls ($preflight + @($build, @('compose', 'run', '--rm', 'builder', 'build')))
        foreach ($command in @('prepare', 'configure', 'download', 'menuconfig', 'shell', 'reset-sources')) {
            Test-Launcher -Arguments @($command) -ExpectedCalls (
                $preflight + @($build, @('compose', 'run', '--rm', 'builder', $command)))
        }
        $makeArgs = @('make', '-j1', 'package/htop/compile', 'V=s', 'VALUE=two words')
        Test-Launcher -Arguments $makeArgs -ExpectedCalls (
            $preflight + @($build, (@('compose', 'run', '--rm', 'builder') + $makeArgs)))
        $image = @('compose', 'build', '--pull', 'builder')
        Test-Launcher -Arguments @('image') -ExpectedCalls ($preflight + @(,$image))
        Test-Launcher -Arguments @('image') -FailureCommand 'compose build --pull builder' -FailureCode 23 `
            -ExpectedCode 23 -ExpectedCalls ($preflight + @(,$image))
        Test-Launcher -FailureCommand 'compose build builder' -FailureCode 17 `
            -ExpectedCode 17 -ExpectedCalls ($preflight + @(,$build))
        Test-Launcher -FailureCommand 'compose run --rm builder build' -FailureCode 42 `
            -ExpectedCode 42 -ExpectedCalls ($preflight + @($build, @('compose', 'run', '--rm', 'builder', 'build')))
        Test-Launcher -DockerOs 'windows' -ExpectedCode 1 -ExpectedCalls $preflight
        Test-Launcher -FailureCommand 'info' -ExpectedCode 1 -ExpectedCalls $preflight
        Test-Launcher -FailureCommand 'compose version' -FailureCode 1 -ExpectedCode 1 `
            -ExpectedCalls @(,@('compose', 'version'))

        $env:BUILD_UID = $null
        $env:BUILD_GID = $null
        $global:routerfwLauncherTest.failureCommand = ''
        & (Join-Path $global:routerfwLauncherTest.project 'build.ps1') prepare
        Assert-Equal $LASTEXITCODE 0
        Assert-Equal ([string]::IsNullOrEmpty($env:BUILD_UID)) $true
        Assert-Equal ([string]::IsNullOrEmpty($env:BUILD_GID)) $true
    }
    finally {
        Pop-Location
    }
    Write-Host 'PowerShell launcher tests passed.'
}
finally {
    $env:BUILD_UID = $originalUid
    $env:BUILD_GID = $originalGid
    $env:JOBS = $originalJobs
    Remove-Item -LiteralPath $testRoot -Recurse -Force
    Remove-Variable routerfwLauncherTest -Scope Global
}
