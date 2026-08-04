<#
Runs Ask Jenny as a single-instance Windows notification-area application.
It owns the uvicorn child process, polls health for menu state, opens the browser,
and writes child stdout/stderr to predictable log files for troubleshooting.
#>
param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8000,
    [switch]$NoAutoStart
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$runScript = Join-Path $root "run.ps1"
$standardLog = Join-Path $root "tray-server.log"
$errorLog = Join-Path $root "tray-server.err.log"
$iconPath = Join-Path $root "app\static\jenny.ico"
$serverUrl = "http://$HostAddress`:$Port"
$healthUrl = "$serverUrl/api/health"

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()

# A named per-user mutex prevents two tray icons from managing competing servers.
$trayMutex = New-Object System.Threading.Mutex($false, "Local\AskJennyServerTray")
$mutexAcquired = $false
try {
    $mutexAcquired = $trayMutex.WaitOne(0, $false)
} catch [System.Threading.AbandonedMutexException] {
    $mutexAcquired = $true
}

if (-not $mutexAcquired) {
    $browserSessionActive = $false
    try {
        $browserSessionStatus = Invoke-RestMethod `
            -Uri "$serverUrl/api/ui-sessions/active" `
            -Method Get `
            -TimeoutSec 3
        $browserSessionActive = $browserSessionStatus.active -eq $true
    } catch {
        $browserSessionActive = $false
    }

    if (-not $browserSessionActive) {
        Start-Process $serverUrl
    }

    $trayMutex.Dispose()
    exit 0
}

# Script scope lets event-handler closures share process and shutdown state.
$script:serverProcess = $null
$script:lastStatusKey = ""
$script:exiting = $false

$customIconLoaded = Test-Path $iconPath
$applicationIcon = if ($customIconLoaded) {
    New-Object System.Drawing.Icon($iconPath)
} else {
    [System.Drawing.SystemIcons]::Application
}

$notifyIcon = New-Object System.Windows.Forms.NotifyIcon
$notifyIcon.Icon = $applicationIcon
$notifyIcon.Text = "Ask Jenny - Starting"
$notifyIcon.Visible = $true

$menu = New-Object System.Windows.Forms.ContextMenuStrip
$statusItem = New-Object System.Windows.Forms.ToolStripMenuItem
$statusItem.Text = "Status: Starting"
$statusItem.Enabled = $false
$openItem = New-Object System.Windows.Forms.ToolStripMenuItem
$openItem.Text = "Open Ask Jenny"
$startItem = New-Object System.Windows.Forms.ToolStripMenuItem
$startItem.Text = "Start server"
$stopItem = New-Object System.Windows.Forms.ToolStripMenuItem
$stopItem.Text = "Stop server"
$restartItem = New-Object System.Windows.Forms.ToolStripMenuItem
$restartItem.Text = "Restart server"
$logsItem = New-Object System.Windows.Forms.ToolStripMenuItem
$logsItem.Text = "Open server logs"
$exitItem = New-Object System.Windows.Forms.ToolStripMenuItem
$exitItem.Text = "Exit tray and stop server"

[void]$menu.Items.Add($statusItem)
[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
[void]$menu.Items.Add($openItem)
[void]$menu.Items.Add($startItem)
[void]$menu.Items.Add($stopItem)
[void]$menu.Items.Add($restartItem)
[void]$menu.Items.Add($logsItem)
[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
[void]$menu.Items.Add($exitItem)
$notifyIcon.ContextMenuStrip = $menu

# Health and presentation helpers --------------------------------------------
function Get-ServerHealth {
    try {
        return Invoke-RestMethod -Uri $healthUrl -Method Get -TimeoutSec 4
    } catch {
        return $null
    }
}

function Show-TrayNotice {
    param(
        [string]$Title,
        [string]$Message,
        [System.Windows.Forms.ToolTipIcon]$Icon
    )

    $notifyIcon.BalloonTipTitle = $Title
    $notifyIcon.BalloonTipText = $Message
    $notifyIcon.BalloonTipIcon = $Icon
    $notifyIcon.ShowBalloonTip(4000)
}

function Set-TrayStatus {
    param(
        [string]$StatusKey,
        [string]$MenuText,
        [string]$Tooltip,
        [System.Drawing.Icon]$Icon,
        [bool]$ServerRunning
    )

    $statusItem.Text = $MenuText
    $notifyIcon.Text = $Tooltip.Substring(0, [Math]::Min(63, $Tooltip.Length))
    $notifyIcon.Icon = $Icon
    $startItem.Enabled = -not $ServerRunning
    $stopItem.Enabled = $ServerRunning -and $null -ne $script:serverProcess
    $restartItem.Enabled = $true
    $openItem.Enabled = $ServerRunning

    if ($StatusKey -ne $script:lastStatusKey) {
        if ($StatusKey -eq "network-alert") {
            Show-TrayNotice `
                -Title "Ask Jenny network alert" `
                -Message "The server is running, but OpenAI network access is unavailable." `
                -Icon ([System.Windows.Forms.ToolTipIcon]::Warning)
        } elseif ($StatusKey -eq "running" -and $script:lastStatusKey) {
            Show-TrayNotice `
                -Title "Ask Jenny is ready" `
                -Message "The server and OpenAI network connection are healthy." `
                -Icon ([System.Windows.Forms.ToolTipIcon]::Info)
        } elseif ($StatusKey -eq "stopped" -and $script:lastStatusKey -notin @("", "stopped")) {
            Show-TrayNotice `
                -Title "Ask Jenny stopped" `
                -Message "The local application server is not running." `
                -Icon ([System.Windows.Forms.ToolTipIcon]::Warning)
        }
        $script:lastStatusKey = $StatusKey
    }
}

function Update-TrayStatus {
    if ($null -ne $script:serverProcess -and $script:serverProcess.HasExited) {
        $script:serverProcess.Dispose()
        $script:serverProcess = $null
    }

    $health = Get-ServerHealth
    if ($null -eq $health) {
        Set-TrayStatus `
            -StatusKey "stopped" `
            -MenuText "Status: Server stopped" `
            -Tooltip "Ask Jenny - Server stopped" `
            -Icon $applicationIcon `
            -ServerRunning $false
        return
    }

    if ($health.openai_network.reachable -eq $true) {
        Set-TrayStatus `
            -StatusKey "running" `
            -MenuText "Status: Running - Network healthy" `
            -Tooltip "Ask Jenny - Running and online" `
            -Icon $applicationIcon `
            -ServerRunning $true
        return
    }

    Set-TrayStatus `
        -StatusKey "network-alert" `
        -MenuText "Status: Running - Network alert" `
        -Tooltip "Ask Jenny - OpenAI network alert" `
        -Icon ([System.Drawing.SystemIcons]::Warning) `
        -ServerRunning $true
}

# Server lifecycle ------------------------------------------------------------
function Start-Server {
    if ($null -ne (Get-ServerHealth)) {
        Update-TrayStatus
        return
    }
    if ($null -ne $script:serverProcess -and -not $script:serverProcess.HasExited) {
        return
    }

    $statusItem.Text = "Status: Starting server..."
    $notifyIcon.Text = "Ask Jenny - Starting server"
    $notifyIcon.Icon = $applicationIcon
    $startItem.Enabled = $false

    $arguments = (
        "-NoProfile -ExecutionPolicy Bypass -File `"$runScript`" " +
        "-HostAddress `"$HostAddress`" -Port $Port -NoReload"
    )
    $script:serverProcess = Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList $arguments `
        -WorkingDirectory $root `
        -WindowStyle Hidden `
        -RedirectStandardOutput $standardLog `
        -RedirectStandardError $errorLog `
        -PassThru
}

function Stop-Server {
    if ($null -ne $script:serverProcess -and -not $script:serverProcess.HasExited) {
        Start-Process `
            -FilePath "taskkill.exe" `
            -ArgumentList @("/PID", $script:serverProcess.Id, "/T", "/F") `
            -WindowStyle Hidden `
            -Wait | Out-Null
        $script:serverProcess.Dispose()
        $script:serverProcess = $null
    }
    Update-TrayStatus
}

function Restart-Server {
    Stop-Server
    Start-Sleep -Milliseconds 400
    Start-Server
}

function Open-Application {
    if ($null -eq (Get-ServerHealth)) {
        Start-Server
        Show-TrayNotice `
            -Title "Ask Jenny is starting" `
            -Message "The browser will be available at $serverUrl once startup completes." `
            -Icon ([System.Windows.Forms.ToolTipIcon]::Info)
        return
    }
    Start-Process $serverUrl
}

function Open-Logs {
    if (-not (Test-Path $standardLog)) {
        New-Item -ItemType File -Path $standardLog -Force | Out-Null
    }
    Start-Process "notepad.exe" -ArgumentList "`"$standardLog`""
}

$openItem.Add_Click({ Open-Application })
$startItem.Add_Click({ Start-Server })
$stopItem.Add_Click({ Stop-Server })
$restartItem.Add_Click({ Restart-Server })
$logsItem.Add_Click({ Open-Logs })
$notifyIcon.Add_DoubleClick({ Open-Application })

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 5000
$timer.Add_Tick({ Update-TrayStatus })
$timer.Start()

$exitItem.Add_Click({
    $script:exiting = $true
    $timer.Stop()
    Stop-Server
    $notifyIcon.Visible = $false
    [System.Windows.Forms.Application]::Exit()
})

try {
    if (-not $NoAutoStart) {
        Start-Server
    }
    Update-TrayStatus
    [System.Windows.Forms.Application]::Run()
} finally {
    $timer.Stop()
    $timer.Dispose()
    if (-not $script:exiting) {
        Stop-Server
    }
    $notifyIcon.Visible = $false
    $notifyIcon.Dispose()
    if ($customIconLoaded) {
        $applicationIcon.Dispose()
    }
    $menu.Dispose()
    if ($mutexAcquired) {
        $trayMutex.ReleaseMutex()
    }
    $trayMutex.Dispose()
}
