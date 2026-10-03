<?php
/*
 * Start, stop, and restart the Blender MCP server from the tester page.
 *
 *   GET  server-control.php?action=status
 *   POST server-control.php?action=start|restart|stop
 *        (POST also needs the header "X-Requested-With: server-control")
 *
 * Every response is JSON: { ok, message?, error?, status }.
 *
 * The server runs exactly as it does by hand ("python blender-mcp-server.py"
 * from this folder), but it is created through WMI (Win32_Process.Create)
 * rather than as a child of Apache.  That way it inherits none of Apache's
 * sockets or handles, the request returns at once, and the server keeps
 * running when Apache restarts.  Its console is hidden; stdout and stderr go
 * to a log file that the page shows.  Only requests from this computer are
 * accepted.
 */

declare(strict_types=1);

ini_set('display_errors', '0');   // a PHP warning must not corrupt the JSON

// ---- configuration ---------------------------------------------------------
const MCP_HOST      = '127.0.0.1';
const MCP_PORT      = 8765;                     // passed to the server as --mcp-port
const SERVER_SCRIPT = 'blender-mcp-server.py';  // relative to this folder
const PYTHON_EXE    = '';                       // '' = first python.exe on Apache's PATH
const STOP_TIMEOUT  = 10;                       // seconds to wait for the port to close
const START_GRACE   = 60;                       // seconds a launch still counts as "starting"

define('STATE_DIR', sys_get_temp_dir() . DIRECTORY_SEPARATOR . 'blender-mcp-control');
define('STATE_FILE', STATE_DIR . DIRECTORY_SEPARATOR . 'state.json');
define('LOCK_FILE', STATE_DIR . DIRECTORY_SEPARATOR . 'control.lock');
define('LOG_FILE', STATE_DIR . DIRECTORY_SEPARATOR . 'blender-mcp-server.log');
define('PREV_LOG_FILE', STATE_DIR . DIRECTORY_SEPARATOR . 'blender-mcp-server.prev.log');

final class ControlError extends RuntimeException {}

// ---- request handling ------------------------------------------------------
if (!in_array($_SERVER['REMOTE_ADDR'] ?? '', ['127.0.0.1', '::1'], true)) {
    respond(403, ['ok' => false, 'error' => 'Server control is only available from this computer.']);
}

$action = (string)($_GET['action'] ?? 'status');

if ($action === 'status') {
    respond(200, ['ok' => true, 'status' => status_snapshot()]);
}
if (!in_array($action, ['start', 'restart', 'stop'], true)) {
    respond(400, ['ok' => false, 'error' => 'Unknown action: ' . $action]);
}
// Another website cannot add a custom header without a CORS preflight, which
// this script never approves, so only this page can drive the actions.
if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST'
    || ($_SERVER['HTTP_X_REQUESTED_WITH'] ?? '') !== 'server-control') {
    respond(405, ['ok' => false, 'error' => 'Use POST with the header X-Requested-With: server-control.']);
}

ignore_user_abort(true);
set_time_limit(60);

if (!is_dir(STATE_DIR) && !@mkdir(STATE_DIR, 0777, true)) {
    respond(500, ['ok' => false, 'error' => 'Cannot create ' . STATE_DIR]);
}
$lock = fopen(LOCK_FILE, 'c');
flock($lock, LOCK_EX);   // one start/stop at a time; released on exit

try {
    $message = match ($action) {
        'start'   => start_server(),
        'restart' => restart_server(),
        'stop'    => stop_server(),
    };
    respond(200, ['ok' => true, 'message' => $message, 'status' => status_snapshot()]);
} catch (ControlError $e) {
    respond($e->getCode() ?: 500, ['ok' => false, 'error' => $e->getMessage(), 'status' => status_snapshot()]);
}

// ---- actions ---------------------------------------------------------------
function start_server(): string
{
    $status = status_snapshot();
    if ($status['listener']) {
        if (!$status['running']) {
            throw new ControlError(port_busy($status['listener']), 409);
        }
        return 'Already running (PID ' . $status['listener']['pid'] . ').';
    }
    if ($status['starting']) {
        return 'Already starting (PID ' . $status['launched']['pid'] . ').';
    }
    return launch();
}

function restart_server(): string
{
    stop_server();
    return launch();
}

function stop_server(): string
{
    $status = status_snapshot();
    $stopped = null;
    if ($status['listener']) {
        if (!$status['running']) {
            throw new ControlError(port_busy($status['listener']), 409);
        }
        $stopped = $status['listener']['pid'];
    } elseif ($status['launched'] && $status['launched']['alive']
        && is_our_wrapper($status['launched']['pid'])) {
        // Launched but not listening yet.  PIDs are reused, so the cmd.exe
        // wrapper is only killed after its command line proves it is ours.
        $stopped = $status['launched']['pid'];
    }

    if ($stopped !== null) {
        [$code, $out] = run([system32('taskkill.exe'), '/PID', (string)$stopped, '/T', '/F']);
        if ($code !== 0 && $code !== 128) {   // 128: the process already exited
            throw new ControlError("taskkill could not stop PID $stopped: " . trim($out), 500);
        }
    }

    $deadline = microtime(true) + STOP_TIMEOUT;
    while (listener_pid() !== null) {
        if (microtime(true) > $deadline) {
            throw new ControlError('Port ' . MCP_PORT . ' is still in use ' . STOP_TIMEOUT . ' s after stopping the server.', 500);
        }
        usleep(250000);
    }
    @unlink(STATE_FILE);
    return $stopped === null ? 'The server was not running.' : "Stopped PID $stopped.";
}

function launch(): string
{
    $python = python_exe();
    if ($python === '') {
        throw new ControlError("python.exe was not found on Apache's PATH. Set PYTHON_EXE in server-control.php.", 500);
    }
    if (!is_file(__DIR__ . DIRECTORY_SEPARATOR . SERVER_SCRIPT)) {
        throw new ControlError(SERVER_SCRIPT . ' was not found in ' . __DIR__ . '.', 500);
    }

    if (is_file(LOG_FILE)) {
        @rename(LOG_FILE, PREV_LOG_FILE);
    }
    @file_put_contents(LOG_FILE, sprintf(
        "[server-control] %s  %s %s --mcp-port %d\r\n",
        date('Y-m-d H:i:s'), $python, SERVER_SCRIPT, MCP_PORT
    ));

    // /s makes cmd strip only the outer quotes, so the quoted paths survive.
    // Python writes cp1252 when redirected, which fails on non-ASCII output;
    // PYTHONIOENCODING keeps the log in UTF-8, and -u keeps it live.
    $command = sprintf(
        '"%s" /d /s /c "set "PYTHONIOENCODING=utf-8" & "%s" -u "%s" --mcp-port %d >> "%s" 2>&1"',
        system32('cmd.exe'), $python, SERVER_SCRIPT, MCP_PORT, LOG_FILE
    );
    [, $out] = powershell(
        "\$ErrorActionPreference = 'Stop'\n" .
        "\$startup = New-CimInstance -CimClass (Get-CimClass Win32_ProcessStartup) -ClientOnly -Property @{ ShowWindow = [uint16]0 }\n" .
        '$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ ' .
        'CommandLine = ' . ps_quote($command) . '; ' .
        'CurrentDirectory = ' . ps_quote(__DIR__) . '; ' .
        "ProcessStartupInformation = \$startup }\n" .
        "'launched {0} {1}' -f \$r.ReturnValue, \$r.ProcessId\n"
    );
    if (!preg_match('/launched (\d+) (\d+)/', $out, $m)) {
        throw new ControlError('Could not launch the server through WMI: ' . trim($out), 500);
    }
    if ($m[1] !== '0') {
        throw new ControlError('Win32_Process.Create failed with code ' . $m[1] . '.', 500);
    }

    $pid = (int)$m[2];
    file_put_contents(STATE_FILE, json_encode(['pid' => $pid, 'started_at' => time(), 'python' => $python]));
    return "Started PID $pid.";
}

// ---- status ----------------------------------------------------------------
function status_snapshot(): array
{
    $pid = listener_pid();
    $images = process_images();
    $listener = $pid === null ? null : ['pid' => $pid, 'image' => $images[$pid] ?? null];

    $launched = null;
    $state = read_state();
    if ($state) {
        $wrapper = (int)($state['pid'] ?? 0);
        $launched = [
            'pid'    => $wrapper,
            'alive'  => strcasecmp($images[$wrapper] ?? '', 'cmd.exe') === 0,
            'age'    => time() - (int)($state['started_at'] ?? 0),
            'python' => (string)($state['python'] ?? ''),
        ];
    }

    return [
        'running'  => $listener !== null && is_python($listener['image']),
        'starting' => $listener === null && $launched && $launched['alive'] && $launched['age'] < START_GRACE,
        'listener' => $listener,
        'launched' => $launched,
        'port'     => MCP_PORT,
        'endpoint' => sprintf('http://%s:%d/mcp', MCP_HOST, MCP_PORT),
        'script'   => __DIR__ . DIRECTORY_SEPARATOR . SERVER_SCRIPT,
        'log_file' => LOG_FILE,
        'log_tail' => log_tail(40),
    ];
}

/** PID of whatever is listening on MCP_PORT, or null. */
function listener_pid(): ?int
{
    [, $out] = run([system32('netstat.exe'), '-ano', '-p', 'TCP']);
    $pattern = '/^\s*TCP\s+\S+:' . MCP_PORT . '\s+\S+\s+LISTENING\s+(\d+)\s*$/mi';
    return preg_match($pattern, $out, $m) ? (int)$m[1] : null;
}

/** Map of PID => image name for every running process. */
function process_images(): array
{
    [, $out] = run([system32('tasklist.exe'), '/FO', 'CSV', '/NH']);
    $images = [];
    foreach (preg_split('/\R/', $out) as $line) {
        $row = str_getcsv($line);
        if (count($row) >= 2 && ctype_digit($row[1])) {
            $images[(int)$row[1]] = $row[0];
        }
    }
    return $images;
}

function is_python(?string $image): bool
{
    return $image !== null && preg_match('/^(python|pythonw|py)[\d.]*\.exe$/i', $image) === 1;
}

function is_our_wrapper(int $pid): bool
{
    [, $out] = powershell("(Get-CimInstance Win32_Process -Filter 'ProcessId=$pid').CommandLine\n");
    return str_contains($out, LOG_FILE);
}

function port_busy(array $listener): string
{
    return sprintf(
        'Port %d is in use by %s (PID %d), which is not the MCP server. Close that program first.',
        MCP_PORT, $listener['image'] ?? 'an unknown process', $listener['pid']
    );
}

function read_state(): ?array
{
    $raw = is_file(STATE_FILE) ? @file_get_contents(STATE_FILE) : false;
    $state = $raw === false ? null : json_decode($raw, true);
    return is_array($state) ? $state : null;
}

function log_tail(int $lines): string
{
    clearstatcache(true, LOG_FILE);
    $size = is_file(LOG_FILE) ? filesize(LOG_FILE) : 0;
    $fh = $size ? @fopen(LOG_FILE, 'rb') : false;
    if (!$fh) {
        return '';
    }
    $chunk = 16384;
    fseek($fh, max(0, $size - $chunk));
    $data = (string)stream_get_contents($fh);
    fclose($fh);
    if ($size > $chunk) {
        $data = substr($data, strpos($data, "\n") + 1);   // drop the partial first line
    }
    return implode("\n", array_slice(preg_split('/\r?\n/', rtrim($data)), -$lines));
}

// ---- process helpers -------------------------------------------------------
function python_exe(): string
{
    if (PYTHON_EXE !== '') {
        return PYTHON_EXE;
    }
    [, $out] = run([system32('where.exe'), 'python']);
    foreach (preg_split('/\R/', trim($out)) as $path) {
        // Skip the Microsoft Store alias, which only opens the Store.
        if ($path !== '' && is_file($path) && stripos($path, '\\WindowsApps\\') === false) {
            return $path;
        }
    }
    return '';
}

function system32(string $exe): string
{
    return (getenv('SystemRoot') ?: 'C:\\Windows') . '\\System32\\' . $exe;
}

/**
 * Run a script in Windows PowerShell.  It is fed on stdin rather than as
 * -EncodedCommand, which security software treats as a web-shell signature.
 * Each statement must fit on one line.
 */
function powershell(string $script): array
{
    return run(
        [system32('WindowsPowerShell\\v1.0\\powershell.exe'), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', '-'],
        "\$ProgressPreference = 'SilentlyContinue'\n" . $script . "\n"
    );
}

function ps_quote(string $value): string
{
    return "'" . str_replace("'", "''", $value) . "'";
}

/** Run a program without a shell and return [exit code, stdout + stderr]. */
function run(array $argv, string $stdin = ''): array
{
    $proc = @proc_open($argv, [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['redirect', 1]], $pipes);
    if (!is_resource($proc)) {
        return [-1, 'Could not run ' . $argv[0]];
    }
    fwrite($pipes[0], $stdin);
    fclose($pipes[0]);
    $output = (string)stream_get_contents($pipes[1]);
    fclose($pipes[1]);
    return [proc_close($proc), $output];
}

function respond(int $code, array $body): never
{
    http_response_code($code);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    echo json_encode($body, JSON_UNESCAPED_SLASHES | JSON_INVALID_UTF8_SUBSTITUTE);
    exit;
}
