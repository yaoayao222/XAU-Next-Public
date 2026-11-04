"""Windows local notification adapter. Text is data; it is never interpolated as shell code."""

import base64
import json
import os
import subprocess
from pathlib import Path


class WindowsNotifier:
    def send(self, notification):
        if os.name != "nt":
            return "failed", "当前系统不支持 Windows 本机通知。"
        script = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$notice = [Console]::In.ReadToEnd() | ConvertFrom-Json
$expiry = [DateTimeOffset]::Parse($notice.expires_at)
if ([DateTimeOffset]::UtcNow -ge $expiry) { Write-Output 'expired'; exit 0 }
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$icon = New-Object System.Windows.Forms.NotifyIcon
$script:shown = $false
$icon.add_BalloonTipShown({ $script:shown = $true })
try {
    $icon.Icon = [System.Drawing.SystemIcons]::Information
    $icon.Text = 'XAU Next'
    $icon.Visible = $true
    if ([DateTimeOffset]::UtcNow -ge $expiry) { Write-Output 'expired'; exit 0 }
    $remaining = [Math]::Max(1, [Math]::Min(3000, ($expiry - [DateTimeOffset]::UtcNow).TotalMilliseconds))
    $icon.ShowBalloonTip([int]$remaining, [string]$notice.title, [string]$notice.body, [System.Windows.Forms.ToolTipIcon]::Info)
    $until = [DateTimeOffset]::UtcNow.AddMilliseconds($remaining)
    while ([DateTimeOffset]::UtcNow -lt $until -and [DateTimeOffset]::UtcNow -lt $expiry) {
        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 50
    }
    if ($script:shown) { Write-Output 'shown' } else { Write-Output 'submitted' }
} finally {
    $icon.Visible = $false
    $icon.Dispose()
}
'''
        executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        try:
            result = subprocess.run(
                [str(executable), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                 "-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
                input=json.dumps(notification, ensure_ascii=False).encode("utf-8"),
                capture_output=True, timeout=12, creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
            output = result.stdout.decode("utf-8", errors="replace").strip()
            if result.returncode == 0 and output in {"submitted", "shown"}:
                return "submitted", ("Windows 已返回本机通知显示事件；不代表用户已看到或已成交。" if output == "shown"
                                      else "已提交 Windows 本机通知；未收到显示确认，不代表用户已看到或已成交。")
            if output == "expired":
                return "suppressed", "系统通知发送前已失去时效。"
            if output.startswith("disabled:"):
                return "failed", "Windows 通知已禁用：" + output.split(":", 1)[1]
            return "failed", "Windows 未接受通知：" + result.stderr.decode("utf-8", errors="replace")[:600]
        except subprocess.TimeoutExpired:
            return "unknown", "通知调用超时，是否已显示未知；不会盲目重发。"
        except OSError as error:
            return "failed", "无法调用 Windows 通知：" + str(error)[:500]
