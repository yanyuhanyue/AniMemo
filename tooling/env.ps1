$projectRoot = Split-Path -Parent $PSScriptRoot
$localRoot = Join-Path $projectRoot '.local'
foreach ($relative in @('tmp', 'cache/npm', 'cache/go-build', 'cache/go-mod', 'cache/go', 'logs', 'output', 'bin', 'tools', 'data')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $localRoot $relative) | Out-Null
}
$env:NPM_CONFIG_CACHE = Join-Path $localRoot 'cache/npm'
$env:GOCACHE = Join-Path $localRoot 'cache/go-build'
$env:GOMODCACHE = Join-Path $localRoot 'cache/go-mod'
$env:GOPATH = Join-Path $localRoot 'cache/go'
$env:GOENV = 'off'
$env:GOTOOLCHAIN = 'local'
$env:GOTELEMETRY = 'off'
$env:TEMP = Join-Path $localRoot 'tmp'
$env:TMP = $env:TEMP
$env:TMPDIR = $env:TEMP
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $localRoot 'cache/browsers'
# Preserve the host's Docker credentials and proxy defaults.
$localGo = Join-Path $localRoot 'tools/go/bin'
if (Test-Path (Join-Path $localGo 'go.exe')) { $env:PATH = "$localGo;$env:PATH" }
