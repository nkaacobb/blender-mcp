<#
.SYNOPSIS
    Start the Blender MCP server in this PowerShell window.

.DESCRIPTION
    Runs blender-mcp-server.py in the foreground, exactly like typing
    "python blender-mcp-server.py" by hand.  Press Ctrl+C to stop it.
    Any arguments are passed on to the server unchanged.

    If the server is already running (for example, started from the tester
    page), this says so and exits instead of failing to bind the port.  The
    page sees a server started here as running and can stop or restart it.

.EXAMPLE
    .\start-server.ps1

.EXAMPLE
    .\start-server.ps1 --socket-timeout 120 --blender-port 9877
#>

# python.exe to use; put a full path here if "python" on PATH is the wrong one.
$Python = 'python'

$script = Join-Path $PSScriptRoot 'blender-mcp-server.py'

# The port the server will use: 8765 unless --mcp-port / --port is passed.
$port = 8765
for ($i = 0; $i -lt $args.Count; $i++) {
    if ($args[$i] -match '^--(mcp-)?port=(\d+)$') { $port = [int]$Matches[2] }
    elseif ($args[$i] -in '--mcp-port', '--port' -and $i + 1 -lt $args.Count) { $port = [int]$args[$i + 1] }
}

$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    $owner = $listener.OwningProcess
    $name = (Get-Process -Id $owner -ErrorAction SilentlyContinue).ProcessName
    if ($name -match '^(python|pythonw|py)[\d.]*$') {
        Write-Host "The Blender MCP server is already running on port $port (PID $owner)," -ForegroundColor Yellow
        Write-Host "started from the tester page or another window.  Use it as is, or stop it first:" -ForegroundColor Yellow
        Write-Host "    Stop-Process -Id $owner"
    } else {
        Write-Host "Port $port is in use by $name (PID $owner), which is not the MCP server." -ForegroundColor Red
        Write-Host "Close that program, or pick another port with --mcp-port."
    }
    exit 1
}

& $Python $script @args
exit $LASTEXITCODE
