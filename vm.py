#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI STV VM Bot — Windows VM tạm thời qua GitHub Actions.
Dùng GitHub + Tailscale key admin tập trung. Người dùng không cần nhập key.
Tạo Repo Public trên tài khoản admin -> chạy workflow -> tự xóa repo khi xong.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import ssl

try:
    import certifi
    _has_certifi = True
except ImportError:
    _has_certifi = False
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

CONFIG_FILE: Path = Path(os.getenv("CONFIG_FILE", str(Path(__file__).parent / "bot_config.json")))
DATA_DIR: Path = Path(os.getenv("DATA_DIR", str(Path(__file__).parent / "vm_bot_data")))
VN_TZ = timezone(timedelta(hours=7), name="Asia/Ho_Chi_Minh")

BRAND_NAME: str = "AI STV"
WORKFLOW_FILENAME: str = "rdp.yml"
WORKFLOW_NAME: str = "AI STV Windows VM"
WORKFLOW_PATH: str = f".github/workflows/{WORKFLOW_FILENAME}"
VM_WINDOWS_USER: str = "AISTV"
TAILSCALE_HOSTNAME: str = "STV-VM"

VPS_WORKFLOW_FILENAME: str = "vps.yml"
VPS_WORKFLOW_NAME: str = "AI STV Ubuntu VPS (sshx)"
VPS_WORKFLOW_PATH: str = f".github/workflows/{VPS_WORKFLOW_FILENAME}"
VPS_DURATION_HOURS: list[int] = [1, 2, 3, 4, 5, 6]
VPS_SERVERS: list[str] = ["ubuntu-latest", "ubuntu-22.04"]
VPS_SERVER_NAMES: dict[str, str] = {
    "ubuntu-latest": "Ubuntu Latest",
    "ubuntu-22.04": "Ubuntu 22.04",
}
VPS_SERVER_RUNS_ON: dict[str, str] = {
    "ubuntu-latest": "ubuntu-latest",
    "ubuntu-22.04": "ubuntu-22.04",
}
def _vps_server_name(key: str) -> str:
    return VPS_SERVER_NAMES.get(key, key)

WINDOWS_RUNNERS: list[str] = ["windows-latest", "windows-2025", "windows-2022"]
WINDOWS_RUNNER_NAMES: dict[str, str] = {
    "windows-latest": "windows-latest",
    "windows-2025": "Windows Server 2025",
    "windows-2022": "Windows Server 2022",
    "windows-2019": "Windows Server 2019",
}
def _runner_name(r: str) -> str:
    return WINDOWS_RUNNER_NAMES.get(r, r)
DURATION_MINUTES: list[int] = [15, 30, 45, 60, 90, 120, 180, 240, 300, 360]

GITHUB_RETRY_MAX: int = 5
GITHUB_RETRY_BASE_SEC: float = 1.5
MAX_ERROR_NOTIFY_CHARS: int = 1500
EXPIRY_WARNING_CHANNEL_NAME: str = "virtual-machine-notification"
SEVER_WINDOWS_CHANNEL_NAME: str = "sever-windows"
MIN_PANEL_INTERVAL_SEC: float = 15.0
RATE_LIMIT_BACKOFF_SEC: int = 300
RATE_LIMIT_WARN_THRESHOLD: int = 150
GITHUB_USER_AGENT: str = "AISTV-VM-Bot/1.0"
# Bump khi sửa workflow/script -> bot tự đẩy lại file mới vào repo worker khi khởi động
WORKFLOW_VERSION: int = 5
# Scheduler local chạy mỗi 30s để xử lý hết hạn/cảnh báo — KHÔNG gọi GitHub API (webhook event-driven)
SCHEDULER_INTERVAL_SEC: float = 30.0
# Nếu máy đang "starting" quá lâu mà webhook không bao giờ đến -> báo fail (an toàn lưới)
STUCK_START_TIMEOUT_SEC: int = 600

_lang_cache: dict[int, str] = {}

def _find_channel_by_name(guild: discord.Guild, target: str) -> Optional[discord.TextChannel]:
    target_lower = target.lower().strip()
    for ch in guild.text_channels:
        name_lower = ch.name.lower().strip()
        if name_lower == target_lower:
            return ch
        if name_lower.endswith(target_lower) or target_lower in name_lower:
            return ch
    return None

def _load_config() -> tuple[str, int, frozenset[int], int, str, str, str, str]:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    owner_raw = os.getenv("DISCORD_OWNER_ID", "").strip()
    owner = int(owner_raw) if owner_raw.isdigit() else 0
    admin_ids: set[int] = {owner} if owner else set()
    allowed_guild_id = 0
    admin_github_token = os.getenv("ADMIN_GITHUB_TOKEN", "").strip()
    admin_tailscale_key = os.getenv("ADMIN_TAILSCALE_KEY", "").strip()
    workflow_repo = os.getenv("GITHUB_REPO", "").strip()
    workflow_owner = os.getenv("GITHUB_OWNER", "").strip()

    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if not token:
                token = str(data.get("bot_token", "")).strip()
            if not owner:
                owner = int(data.get("owner_id", 0))
                if owner:
                    admin_ids.add(owner)
            for raw in data.get("admin_ids", []):
                try:
                    admin_ids.add(int(raw))
                except (TypeError, ValueError):
                    pass
            allowed_raw = str(data.get("allowed_guild_id", 0)).strip()
            if allowed_raw.lstrip("-").isdigit():
                allowed_guild_id = int(allowed_raw)
            if not admin_github_token:
                admin_github_token = str(data.get("admin_github_token", "")).strip()
            if not admin_tailscale_key:
                admin_tailscale_key = str(data.get("admin_tailscale_key", "")).strip()
            if not workflow_repo:
                workflow_repo = str(data.get("github_repo", "")).strip()
            if not workflow_owner:
                workflow_owner = str(data.get("github_owner", "")).strip()
        except (json.JSONDecodeError, OSError):
            pass

    allowed_raw = os.getenv("ALLOWED_GUILD_ID", "").strip()
    if allowed_raw.lstrip("-").isdigit():
        allowed_guild_id = int(allowed_raw)

    if not workflow_repo:
        workflow_repo = "aistv-vm-worker"

    return token, owner, frozenset(admin_ids), allowed_guild_id, admin_github_token, admin_tailscale_key, workflow_repo, workflow_owner

BOT_TOKEN, OWNER_ID, ADMIN_IDS, ALLOWED_GUILD_ID, ADMIN_GITHUB_TOKEN, ADMIN_TAILSCALE_KEY, WORKFLOW_REPO, WORKFLOW_OWNER = _load_config()

BOT_WEBHOOK_URL: str = os.getenv("BOT_WEBHOOK_URL", "").strip()
BOT_WEBHOOK_SECRET: str = os.getenv("BOT_WEBHOOK_SECRET", "").strip()
if CONFIG_FILE.exists():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as _f:
            _cfg = json.load(_f)
        if not BOT_WEBHOOK_URL:
            BOT_WEBHOOK_URL = str(_cfg.get("bot_webhook_url", "")).strip()
        if not BOT_WEBHOOK_SECRET:
            BOT_WEBHOOK_SECRET = str(_cfg.get("bot_webhook_secret", "")).strip()
    except (json.JSONDecodeError, OSError):
        pass
BOT_WEBHOOK_PATH: str = "/api/vm-ready"
if not BOT_WEBHOOK_URL:
    _auto_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip() or os.getenv("RAILWAY_TCP_PROXY_DOMAIN", "").strip()
    if _auto_domain:
        BOT_WEBHOOK_URL = f"https://{_auto_domain}{BOT_WEBHOOK_PATH}"
        log = logging.getLogger("vm_bot")
        log.info("Auto-detected BOT_WEBHOOK_URL: %s", BOT_WEBHOOK_URL)

def _load_optional_secret(key: str) -> str:
    """Đọc secret phụ từ env hoặc bot_config.json (không bắt buộc)."""
    val = os.getenv(key, "").strip()
    if not val and CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                val = str(json.load(f).get(key.lower(), "")).strip()
        except (json.JSONDecodeError, OSError):
            pass
    return val

# Secrets tùy chọn — workflow GitHub dùng để gửi Discord embed + upload clip Driveway
DISCORD_WEBHOOK_URL: str = _load_optional_secret("DISCORD_WEBHOOK_URL")
DISCORD_USER_ID: str = _load_optional_secret("DISCORD_USER_ID")
DRIVEWAY_UPLOAD_URL: str = _load_optional_secret("DRIVEWAY_UPLOAD_URL")
DRIVEWAY_API_KEY: str = _load_optional_secret("DRIVEWAY_API_KEY")

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

VIETNAMESE_MAP: dict[str, str] = {
    "à": "a", "á": "a", "ả": "a", "ã": "a", "ạ": "a",
    "ă": "a", "ằ": "a", "ắ": "a", "ẳ": "a", "ẵ": "a", "ặ": "a",
    "â": "a", "ầ": "a", "ấ": "a", "ẩ": "a", "ẫ": "a", "ậ": "a",
    "đ": "d",
    "è": "e", "é": "e", "ẻ": "e", "ẽ": "e", "ẹ": "e",
    "ê": "e", "ề": "e", "ế": "e", "ể": "e", "ễ": "e", "ệ": "e",
    "ì": "i", "í": "i", "ỉ": "i", "ĩ": "i", "ị": "i",
    "ò": "o", "ó": "o", "ỏ": "o", "õ": "o", "ọ": "o",
    "ô": "o", "ồ": "o", "ố": "o", "ổ": "o", "ỗ": "o", "ộ": "o",
    "ơ": "o", "ờ": "o", "ớ": "o", "ở": "o", "ỡ": "o", "ợ": "o",
    "ù": "u", "ú": "u", "ủ": "u", "ũ": "u", "ụ": "u",
    "ư": "u", "ừ": "u", "ứ": "u", "ử": "u", "ữ": "u", "ự": "u",
    "ỳ": "y", "ý": "y", "ỷ": "y", "ỹ": "y", "ỵ": "y",
}

def _sanitize_repo_name(name: str) -> str:
    name = name.strip().lower()
    result: list[str] = []
    for ch in name:
        result.append(VIETNAMESE_MAP.get(ch, ch))
    name = "".join(result)
    name = re.sub(r"[^a-z0-9_.-]", "-", name)
    name = re.sub(r"-+", "-", name)
    name = name.strip("-._")
    if not name:
        name = "vm-repo"
    return name[:100]

class VMStatus(str, Enum):
    OFFLINE = "offline"
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"
    EXPIRED = "expired"
    STOPPING = "stopping"

STATUS_EMOJI = {
    VMStatus.RUNNING: "🟢",
    VMStatus.STARTING: "🟡",
    VMStatus.OFFLINE: "🔴",
    VMStatus.EXPIRED: "⌛",
    VMStatus.FAILED: "❌",
    VMStatus.STOPPING: "🟡",
}

STATUS_COLOR = {
    VMStatus.RUNNING: 0x2ECC71,
    VMStatus.STARTING: 0xF1C40F,
    VMStatus.OFFLINE: 0x95A5A6,
    VMStatus.EXPIRED: 0xE67E22,
    VMStatus.FAILED: 0xE74C3C,
    VMStatus.STOPPING: 0xF39C12,
}

ACTIVE_VM_STATUSES: frozenset[str] = frozenset({
    VMStatus.STARTING.value,
    VMStatus.RUNNING.value,
    VMStatus.STOPPING.value,
})

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None

def _to_vn(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(VN_TZ)

def _fmt_dt(iso: str, lang: str = "vi") -> str:
    dt = _parse_iso(iso)
    if not dt:
        return "—"
    if lang == "en":
        us_east = timezone(timedelta(hours=-5), name="US/Eastern")
        return dt.astimezone(us_east).strftime("%m/%d/%Y %I:%M %p") + " (US/Eastern)"
    return _to_vn(dt).strftime("%d/%m/%Y %H:%M") + " (VN)"

def _fmt_remaining(iso: str, lang: str = "vi") -> str:
    dt = _parse_iso(iso)
    if not dt:
        return "—"
    now = datetime.now(timezone.utc)
    delta = dt - now
    if delta.total_seconds() <= 0:
        return "Expired" if lang == "en" else "Đã hết hạn"
    mins = int(delta.total_seconds() // 60)
    secs = int(delta.total_seconds() % 60)
    if mins >= 60:
        return f"{mins // 60}h {mins % 60}m {'remaining' if lang == 'en' else 'còn lại'}"
    if mins > 0:
        return f"{mins}m {secs}s {'remaining' if lang == 'en' else 'còn lại'}"
    return f"{secs}s {'remaining' if lang == 'en' else 'còn lại'}"

def _fmt_duration(minutes: int, lang: str = "vi") -> str:
    if minutes < 60:
        return f"{minutes} {'min' if lang == 'en' else 'phút'}"
    h = minutes // 60
    m = minutes % 60
    if lang == "en":
        return f"{h}h" + (f" {m}m" if m else "")
    return f"{h} giờ" + (f" {m}p" if m else "")

def _t(lang: str, en: str, vi: str) -> str:
    return vi if lang == "vi" else en

def _mask_secret(value: str, visible: int = 4) -> str:
    if not value:
        return "(empty)"
    if len(value) <= visible * 2:
        return "***"
    return f"{value[:visible]}...{value[-visible:]}"

def _safe_error_text(exc: BaseException, *, include_trace: bool = False) -> str:
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) if include_trace else str(exc)
    patterns = [
        r"ghp_[A-Za-z0-9_]{20,}",
        r"github_pat_[A-Za-z0-9_]{20,}",
        r"tskey-[A-Za-z0-9_-]+",
        r"gsk_[A-Za-z0-9_-]+",
        r"Bot\s+[A-Za-z0-9._-]+",
        r"Bearer\s+[A-Za-z0-9._-]+",
    ]
    for pat in patterns:
        text = re.sub(pat, "***REDACTED***", text, flags=re.IGNORECASE)
    return text[-MAX_ERROR_NOTIFY_CHARS:]

def _valid_repo_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name or "")) and not name.startswith(".")

def _provision_ps1_body() -> str:
    return r"""
$ErrorActionPreference = 'Continue'
$tsExe = "$env:ProgramFiles\Tailscale\tailscale.exe"
function Install-NetworkPkg {
  if (Test-Path -LiteralPath $tsExe) { Write-Host "Network: da cai san"; return $true }
  $exe = Join-Path $env:RUNNER_TEMP "tailscale-setup.exe"
  try {
    Invoke-WebRequest -Uri "https://pkgs.tailscale.com/stable/tailscale-setup-full-1.98.4.exe" -OutFile $exe -UseBasicParsing -TimeoutSec 120
    if (Test-Path -LiteralPath $exe) {
      $p = Start-Process -FilePath $exe -ArgumentList @("/quiet") -Wait -PassThru -NoNewWindow
      for ($w = 0; $w -lt 20; $w++) { if (Test-Path -LiteralPath $tsExe) { break }; Start-Sleep -Seconds 2 }
    }
  } catch { Write-Host "Network EXE loi: $($_.Exception.Message)" }
  if (Test-Path -LiteralPath $tsExe) { return $true }
  return $false
}
Install-NetworkPkg | Out-Null
$null = New-Item -Path 'C:\AISTV' -ItemType Directory -Force -ErrorAction SilentlyContinue
if (Test-Path -LiteralPath $tsExe) {
  $ip = 'pending'
  for ($t = 0; $t -lt 18; $t++) {
    try {
      $j = & $tsExe status --json 2>$null | ConvertFrom-Json
      if ($j.Self.Online -eq $true -and $j.Self.TailscaleIPs) {
        foreach ($a in $j.Self.TailscaleIPs) { $s = "$a".Trim(); if ($s -match '^\d{1,3}(\.\d{1,3}){3}$') { $ip = $s; break } }
      }
    } catch {}
    if ($ip -ne 'pending') { break }
    Start-Sleep -Seconds 5
  }
} else {
  $ip = (Invoke-RestMethod -Uri 'https://api.ipify.org?format=json' -TimeoutSec 30).ip
}
$password = -join ((48..57)+(65..90)+(97..122) | Get-Random -Count 52 | ForEach-Object { [char]$_ })
$vmUser = 'AISTV'
$sec = ConvertTo-SecureString $password -AsPlainText -Force
if (Get-LocalUser -Name $vmUser -ErrorAction SilentlyContinue) {
  Set-LocalUser -Name $vmUser -Password $sec -PasswordNeverExpires $true -ErrorAction SilentlyContinue
} else {
  New-LocalUser -Name $vmUser -Password $sec -FullName 'AI STV User' -PasswordNeverExpires -ErrorAction SilentlyContinue
}
$lu = Get-LocalUser -Name $vmUser -ErrorAction SilentlyContinue
if ($lu) { $lu | Enable-LocalUser -ErrorAction SilentlyContinue }
Add-LocalGroupMember -Group 'Administrators' -Member $vmUser -ErrorAction SilentlyContinue
Add-LocalGroupMember -Group 'Remote Desktop Users' -Member $vmUser -ErrorAction SilentlyContinue
Set-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name 'fDenyTSConnections' -Value 0
Enable-NetFirewallRule -DisplayGroup 'Remote Desktop' | Out-Null
# Set up wallpaper & account picture
$ws2 = if ($env:GITHUB_WORKSPACE) { $env:GITHUB_WORKSPACE } else { (Get-Location).Path }
$wpSrc = Join-Path $ws2 "img19.png"
$avSrc = Join-Path $ws2 "user.png"
$lsSrc = Join-Path $ws2 "img0.png"
$wpDst = "C:\AISTV\wallpaper.png"
$avDst = "C:\AISTV\user.png"
$lsDst = "C:\AISTV\lockscreen.png"
if (Test-Path -LiteralPath $wpSrc) { Copy-Item -LiteralPath $wpSrc -Destination $wpDst -Force; Write-Host "Wallpaper image copied" }
if (Test-Path -LiteralPath $avSrc) { Copy-Item -LiteralPath $avSrc -Destination $avDst -Force; Write-Host "Avatar image copied" }
if (Test-Path -LiteralPath $lsSrc) { Copy-Item -LiteralPath $lsSrc -Destination $lsDst -Force; Write-Host "Lockscreen image copied" }
if (Test-Path -LiteralPath $wpDst) {
  try {
    & reg.exe load "HKU\_AISTV_DEF" "C:\Users\Default\NTUSER.DAT" 2>$null
    $null = New-Item -Path "HKU:\_AISTV_DEF\Control Panel\Desktop" -Force -ErrorAction SilentlyContinue
    Set-ItemProperty -Path "HKU:\_AISTV_DEF\Control Panel\Desktop" -Name 'Wallpaper' -Value $wpDst -Force -ErrorAction SilentlyContinue
    Set-ItemProperty -Path "HKU:\_AISTV_DEF\Control Panel\Desktop" -Name 'WallpaperStyle' -Value '10' -Force -ErrorAction SilentlyContinue
    Set-ItemProperty -Path "HKU:\_AISTV_DEF\Control Panel\Desktop" -Name 'TileWallpaper' -Value '0' -Force -ErrorAction SilentlyContinue
    & reg.exe unload "HKU\_AISTV_DEF" 2>$null
  } catch { Write-Host "Wallpaper reg error: $($_.Exception.Message)" }
  try {
    $null = New-Item -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization" -Force -ErrorAction SilentlyContinue
    Set-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization" -Name 'DesktopWallpaper' -Value $wpDst -Force -ErrorAction SilentlyContinue
    Set-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization" -Name 'DesktopWallpaperStyle' -Value '10' -Force -ErrorAction SilentlyContinue
  } catch { Write-Host "Wallpaper policy error: $($_.Exception.Message)" }
}
if (Test-Path -LiteralPath $avDst) {
  try {
    $sid = (Get-LocalUser -Name $vmUser -ErrorAction SilentlyContinue).SID.Value
    if ($sid) {
      $null = New-Item -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AccountPicture\Users\$sid" -Force -ErrorAction SilentlyContinue
      Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AccountPicture\Users\$sid" -Name 'Image128' -Value $avDst -Force -ErrorAction SilentlyContinue
      Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AccountPicture\Users\$sid" -Name 'Image240' -Value $avDst -Force -ErrorAction SilentlyContinue
      Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AccountPicture\Users\$sid" -Name 'Image48' -Value $avDst -Force -ErrorAction SilentlyContinue
      Write-Host "Account picture set for SID $sid"
    }
  } catch { Write-Host "Avatar reg error: $($_.Exception.Message)" }
}

$runId = if ($env:GITHUB_RUN_ID) { $env:GITHUB_RUN_ID } else { '0' }
$tsHostname = "STV-VM-$runId"
Write-Host "Hostname: $tsHostname"
Write-Host "IP: $ip"
Write-Host "Username: $vmUser"
Write-Host "Password: $password"
$ws = if ($env:GITHUB_WORKSPACE) { $env:GITHUB_WORKSPACE } else { (Get-Location).Path }
$credsPath = Join-Path $ws 'vm-creds.json'
@{ hostname = $tsHostname; ip = $ip; username = $vmUser; password = $password; login = $vmUser; instance_id = $env:INSTANCE_ID; discord_id = $env:DISCORD_ID; kind = 'windows'; run_id = $runId } | ConvertTo-Json | Set-Content -LiteralPath $credsPath -Encoding utf8
if (-not (Test-Path -LiteralPath $credsPath)) { Write-Error "vm-creds.json not created"; exit 1 }
exit 0
"""

def _keepalive_ps1_body() -> str:
    return r"""
$ErrorActionPreference = 'SilentlyContinue'
$mins = [int]$env:VM_DURATION_MINUTES
if ($mins -lt 1) { $mins = 30 }
$deadline = (Get-Date).AddMinutes($mins)
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Seconds 30
}
exit 0
"""

def _vps_sh_body() -> str:
    return r"""#!/usr/bin/env bash
set -u
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

# Keep-Alive chong bi ngat ket noi do idle
export SSHX_KEEP_ALIVE=true

# Cai sshx (neu chua co) va cho phep sudo tim thay no
if ! command -v sshx >/dev/null 2>&1; then
  curl -sSf https://sshx.io/get | sh
  export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
fi
SSHX_BIN="$(command -v sshx || true)"
if [ -n "$SSHX_BIN" ] && ! sudo sh -c 'command -v sshx' >/dev/null 2>&1; then
  sudo cp "$SSHX_BIN" /usr/local/bin/sshx 2>/dev/null || true
fi

LOG_DIR="$HOME/sshx_logs"
mkdir -p "$LOG_DIR"
chmod 777 "$LOG_DIR" 2>/dev/null || true
OUT="$LOG_DIR/sshx.out"
: > "$OUT"

# Khoi dong sshx nen, setsid de khong bi GitHub kill khi step doi step khac
RUN_ID="${GITHUB_RUN_ID:-0}"
MINS="${DURATION_MINUTES:-60}"
DEADLINE=$(( $(date +%s) + MINS * 60 ))
setsid nohup sudo script -q -e -c "SSHX_KEEP_ALIVE=true sshx" "$LOG_DIR/sshx.pty" >> "$OUT" 2>&1 &
SSHX_PID=$!

# Doi URL toi da ~60s, ghi file + bao webhook NGAY trong step nay (link con song)
URL=""
for i in $(seq 1 30); do
  URL="$(sed -r 's/\x1B\[[0-9;]*[mK]//g' "$OUT" 2>/dev/null \
    | grep -oE 'https://sshx\.io/[^[:space:]"]+' | tail -n 1 || true)"
  if [ -n "$URL" ]; then
    echo "SSHX_URL: $URL"
    export SSHX_LINK="$URL"
    printf '{"sshx_url":"%s","username":"root","hostname":"root","instance_id":"%s","discord_id":"%s","kind":"vps","run_id":"%s"}\n' \
      "$URL" "${INSTANCE_ID:-1}" "${DISCORD_ID:-}" "$RUN_ID" > ssh_url.txt
    if [ -n "${BOT_WEBHOOK_URL:-}" ]; then
      curl -sS -X POST "${BOT_WEBHOOK_URL}" \
        -H "Content-Type: application/json" \
        -H "X-Bot-Secret: ${BOT_WEBHOOK_SECRET:-}" \
        --data-binary @ssh_url.txt \
        && echo "Webhook sent OK" || echo "Webhook failed"
    fi
    # Discord embed trực tiếp từ GitHub Action (event-driven, không poll)
    if [ -n "${DISCORD_WEBHOOK_URL:-}" ] && command -v python3 >/dev/null 2>&1; then
      python3 - <<'PY' && curl -sS -X POST "${DISCORD_WEBHOOK_URL}" -H "Content-Type: application/json" --data-binary @discord-vps-payload.json >/dev/null 2>&1 || echo "Discord notify failed"
import json, os, datetime
uid = os.environ.get("DISCORD_USER_ID", "").strip()
mention = f"<@{uid}>" if uid else ""
ssh_url = os.environ.get("SSHX_LINK", "").strip()
payload = {
    "content": mention,
    "embeds": [{
        "title": "🚀 MÁY ẢO / VPS ĐÃ KHỞI TẠO THÀNH CÔNG!",
        "color": 0x2ECC71,
        "fields": [
            {"name": "🌐 Link Terminal (sshx)", "value": f"{ssh_url}", "inline": False},
            {"name": "🔑 Tài khoản", "value": "`AISTV` (root · sudo)", "inline": True},
        ],
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }],
}
json.dump(payload, open("discord-vps-payload.json", "w", encoding="utf-8"), ensure_ascii=False)
PY
    fi
    break
  fi
  sleep 2
done
echo "SSHX_URL: ${URL:-pending}"

# Giu session song den deadline NGAY TRONG step nay (step chay full thoi luong)
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  if ! kill -0 "$SSHX_PID" 2>/dev/null; then
    echo "sshx died, restarting..."
    setsid nohup sudo script -q -e -c "SSHX_KEEP_ALIVE=true sshx" "$LOG_DIR/sshx.pty" >> "$OUT" 2>&1 &
    SSHX_PID=$!
  fi
  sleep 60
done
echo "Session finished after ${MINS} minutes."
exit 0
"""

def _notify_vm_ready_script() -> str:
    """Gửi webhook /api/vm-ready về bot khi máy xong (kèm clip_url) hoặc khi thất bại."""
    return r"""#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Notify bot (Railway) that a VM instance is ready or failed - Webhook Event-Driven.
# Doc: reads vm-creds.json (neu co) + env CLIP_URL, POST toi BOT_WEBHOOK_URL.
# Neu thieu vm-creds.json -> gui payload status=failed de bot bao user ngay lap tuc.
import json
import os
import sys
import urllib.request

def main() -> int:
    url = os.environ.get("BOT_WEBHOOK_URL", "").strip()
    if not url:
        return 0
    if os.path.exists("vm-creds.json"):
        with open("vm-creds.json", encoding="utf-8") as f:
            data = json.load(f)
        clip = os.environ.get("CLIP_URL", "").strip()
        if clip:
            data["clip_url"] = clip
        data.setdefault("status", "ready")
    else:
        data = {
            "discord_id": os.environ.get("DISCORD_ID", "").strip(),
            "instance_id": os.environ.get("INSTANCE_ID", "1").strip(),
            "kind": "windows",
            "run_id": os.environ.get("GITHUB_RUN_ID", "0").strip(),
            "status": "failed",
        }
    headers = {"Content-Type": "application/json"}
    secret = os.environ.get("BOT_WEBHOOK_SECRET", "").strip()
    if secret:
        headers["X-Bot-Secret"] = secret
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    print("POST bot webhook status=%s" % data.get("status"))
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            print("bot webhook response status=%s" % resp.status)
    except Exception as e:
        print("bot webhook failed: %s" % e)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
"""

def _notify_discord_script() -> str:
    """Gửi embed '🚀 MÁY ẢO / VPS ĐÃ KHỞI TẠO THÀNH CÔNG!' thẳng về Discord từ workflow."""
    return r"""#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Post rich Discord embed (MAY AO / VPS DA KHOI TAO THANH CONG) qua DISCORD_WEBHOOK_URL.
# Doc: reads vm-creds.json + env CLIP_URL (link clip Driveway).
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

def main() -> int:
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url or not os.path.exists("vm-creds.json"):
        return 0
    with open("vm-creds.json", encoding="utf-8") as f:
        data = json.load(f)
    clip = os.environ.get("CLIP_URL", "").strip()
    uid = os.environ.get("DISCORD_USER_ID", "").strip()
    fields = [
        {"name": "🌐 IP/Host", "value": "`%s`" % (data.get("ip") or data.get("hostname") or "pending"), "inline": True},
        {"name": "🔑 Tài khoản", "value": "`%s` | `%s`" % (data.get("login") or data.get("username") or "-", data.get("password") or "-"), "inline": True},
    ]
    if clip:
        fields.append({"name": "🎬 Link Clip (Driveway)", "value": clip, "inline": False})
    payload = {
        "content": "<@%s>" % uid if uid else "",
        "embeds": [{
            "title": "🚀 MÁY ẢO / VPS ĐÃ KHỞI TẠO THÀNH CÔNG!",
            "color": 0x2ECC71,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            print("discord webhook response status=%s" % resp.status)
    except Exception as e:
        print("discord webhook failed: %s" % e)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
"""

def _keepalive_sh_body() -> str:
    return r"""#!/usr/bin/env bash
set -u
MINS="${DURATION_MINUTES:-60}"
DEADLINE=$(( $(date +%s) + MINS * 60 ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  sleep 60
done
echo "Session finished after ${MINS} minutes."
exit 0
"""

def build_github_workflow() -> str:
    yaml_template = r"""# Auto-generated — %BRAND_NAME%
# Worker: Windows VM (RDP + Tailscale). Triggered via repository_dispatch.
name: %WORKFLOW_NAME%
on:
  repository_dispatch:
    types: [start-vm]
jobs:
  aistv-vm:
    strategy:
      fail-fast: false
      max-parallel: 10
      matrix:
        include: ${{ github.event.client_payload.matrix }}
    runs-on: ${{ matrix.runner }}
    timeout-minutes: 360
    steps:
      - uses: actions/checkout@v4
      - name: Connect Tailscale
        shell: pwsh
        env:
          TAILSCALE_AUTH_KEY: ${{ secrets.TAILSCALE_AUTH_KEY }}
          INSTANCE_ID: ${{ matrix.instance_id }}
        run: |
          $tsExe = "$env:ProgramFiles\Tailscale\tailscale.exe"
          if (-not (Test-Path -LiteralPath $tsExe)) {
            $exe = Join-Path $env:RUNNER_TEMP "tailscale-setup.exe"
            Invoke-WebRequest -Uri "https://pkgs.tailscale.com/stable/tailscale-setup-full-1.98.4.exe" -OutFile $exe -UseBasicParsing -TimeoutSec 120
            Start-Process -FilePath $exe -ArgumentList @("/quiet") -Wait -PassThru -NoNewWindow
            Start-Sleep -Seconds 5
          }
          $tsRunId = if ($env:GITHUB_RUN_ID) { $env:GITHUB_RUN_ID } else { '0' }
          & $tsExe up --authkey=$env:TAILSCALE_AUTH_KEY --hostname=STV-VM-$tsRunId --accept-routes=false --accept-dns=false --timeout=90s 2>&1
      - name: Provision VM
        timeout-minutes: 25
        shell: pwsh
        env:
          INSTANCE_ID: ${{ matrix.instance_id }}
          VM_DURATION_MINUTES: ${{ github.event.client_payload.duration }}
          DISCORD_ID: ${{ github.event.client_payload.session }}
        run: |
          & ./scripts/provision.ps1
      - name: Upload VM credentials
        uses: actions/upload-artifact@v4
        with:
          name: aistv-creds-${{ matrix.instance_id }}
          path: vm-creds.json
          if-no-files-found: warn
          retention-days: 1
      - name: Upload clip to Driveway (optional, pluggable)
        id: upload_clip
        if: always()
        shell: bash
        env:
          DRIVEWAY_UPLOAD_URL: ${{ secrets.DRIVEWAY_UPLOAD_URL }}
          DRIVEWAY_API_KEY: ${{ secrets.DRIVEWAY_API_KEY }}
        run: |
          echo "clip_url=" >> "$GITHUB_OUTPUT"
          CLIP_FILE=""
          for f in session_record.mp4 clip.mp4 "$GITHUB_WORKSPACE/session_record.mp4" "$GITHUB_WORKSPACE/clip.mp4"; do
            if [ -f "$f" ]; then CLIP_FILE="$f"; break; fi
          done
          if [ -z "$CLIP_FILE" ] || [ -z "${DRIVEWAY_UPLOAD_URL:-}" ]; then
            echo "skip clip: no clip file found or DRIVEWAY_UPLOAD_URL not configured"
            exit 0
          fi
          AUTH=()
          if [ -n "${DRIVEWAY_API_KEY:-}" ]; then AUTH=(-H "Authorization: Bearer ${DRIVEWAY_API_KEY}"); fi
          RESP=$(curl -sS -m 120 -F "file=@${CLIP_FILE}" "${AUTH[@]}" "${DRIVEWAY_UPLOAD_URL}" || true)
          echo "driveway response: ${RESP}"
          export CLIP_RESULT="${RESP}"
          CLIP_URL=$(printf '%s' "${CLIP_RESULT}" | sed -n 's/.*"url"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)
          if [ -z "$CLIP_URL" ]; then
            CLIP_URL=$(printf '%s' "${CLIP_RESULT}" | sed -n 's/.*"link"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)
          fi
          if [ -n "$CLIP_URL" ]; then
            echo "clip_url=$CLIP_URL" >> "$GITHUB_OUTPUT"
          fi
      - name: Notify Bot via Webhook (event-driven, 0 polling)
        if: always()
        shell: bash
        env:
          BOT_WEBHOOK_URL: ${{ secrets.BOT_WEBHOOK_URL }}
          BOT_WEBHOOK_SECRET: ${{ secrets.BOT_WEBHOOK_SECRET }}
          DISCORD_ID: ${{ github.event.client_payload.session }}
          INSTANCE_ID: ${{ matrix.instance_id }}
          CLIP_URL: ${{ steps.upload_clip.outputs.clip_url }}
        run: |
          python scripts/notify_vm_ready.py || echo "Bot webhook notify failed"
      - name: Notify Discord — MÁY ẢO / VPS ĐÃ KHỞI TẠO THÀNH CÔNG
        if: always()
        shell: bash
        env:
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
          DISCORD_USER_ID: ${{ secrets.DISCORD_USER_ID }}
          CLIP_URL: ${{ steps.upload_clip.outputs.clip_url }}
        run: |
          python scripts/notify_discord.py || echo "Discord notify failed"
      - name: Keep-Alive
        shell: pwsh
        env:
          INSTANCE_ID: ${{ matrix.instance_id }}
          VM_DURATION_MINUTES: ${{ github.event.client_payload.duration }}
        run: |
          & ./scripts/keepalive.ps1
"""
    return (yaml_template
        .replace("%BRAND_NAME%", BRAND_NAME)
        .replace("%WORKFLOW_NAME%", WORKFLOW_NAME)
    )

def build_vps_workflow() -> str:
    yaml_template = r"""# Auto-generated — %BRAND_NAME%
# Worker: Ubuntu terminal (sshx). Triggered via repository_dispatch.
name: %VPS_WORKFLOW_NAME%
on:
  repository_dispatch:
    types: [start-vps]
jobs:
  launch-terminal:
    strategy:
      fail-fast: false
      matrix:
        include: ${{ github.event.client_payload.matrix }}
    runs-on: ${{ matrix.runner }}
    timeout-minutes: 360
    steps:
      - uses: actions/checkout@v4
      - name: Setup Environment & Start sshx (giu session suot thoi gian)
        timeout-minutes: 360
        shell: bash
        env:
          DURATION_MINUTES: ${{ github.event.client_payload.duration }}
          DISCORD_ID: ${{ github.event.client_payload.session }}
          INSTANCE_ID: ${{ matrix.instance_id }}
          BOT_WEBHOOK_URL: ${{ secrets.BOT_WEBHOOK_URL }}
          BOT_WEBHOOK_SECRET: ${{ secrets.BOT_WEBHOOK_SECRET }}
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
          DISCORD_USER_ID: ${{ secrets.DISCORD_USER_ID }}
        run: |
          bash scripts/vps.sh
      - name: Upload SSH URL
        uses: actions/upload-artifact@v4
        with:
          name: aistv-ssh-${{ matrix.instance_id }}
          path: ssh_url.txt
          if-no-files-found: warn
          retention-days: 1
"""
    return (yaml_template
        .replace("%BRAND_NAME%", BRAND_NAME)
        .replace("%VPS_WORKFLOW_NAME%", VPS_WORKFLOW_NAME)
    )

# ── Data Models ──────────────────────────────────────────────────────

@dataclass
class VMInstance:
    instance_id: str
    run_id: Optional[int] = None
    job_id: Optional[int] = None
    pipeline_id: Optional[int] = None
    status: str = VMStatus.STARTING.value
    workflow_status: str = "queued"
    hostname: str = ""
    ip: str = ""
    username: str = ""
    password: str = ""

@dataclass
class VMRecord:
    discord_id: int
    owner_id: int
    repo: str
    github_token: str
    workflow_name: str
    kind: str = "windows"
    runner: str = "windows-latest"
    runners: list[str] = field(default_factory=list)
    duration_minutes: int = 60
    machine_count: int = 1
    instances: list = field(default_factory=list)
    run_id: Optional[int] = None
    status: str = VMStatus.OFFLINE.value
    workflow_status: str = "unknown"
    created_at: str = field(default_factory=_utc_now_iso)
    ready_at: str = ""
    expires_at: str = ""
    ip: str = ""
    username: str = ""
    password: str = ""
    message_id: Optional[int] = None
    channel_id: Optional[int] = None
    guild_id: Optional[int] = None
    expiry_notified: bool = False
    notified_milestones: list = field(default_factory=list)
    credentials_sent: bool = False
    dm_ready_count: int = 0
    last_real_ip_count: int = 0
    last_panel_update: float = 0.0
    last_panel_status: str = ""

    def __post_init__(self) -> None:
        if not self.expires_at:
            created = _parse_iso(self.created_at)
            if created:
                self.expires_at = (created + timedelta(minutes=self.duration_minutes)).isoformat()

def _instance_has_password(inst: dict) -> bool:
    return bool((inst.get("password") or "").strip())

def _instance_has_creds(inst: dict, kind: str = "windows") -> bool:
    if kind == "vps":
        u = (inst.get("sshx_url") or "").strip()
        return bool(u) and u != "pending"
    return _instance_has_password(inst)

def _vm_is_active(vm: VMRecord) -> bool:
    for inst in vm.instances or []:
        st = inst.get("status") if isinstance(inst, dict) else getattr(inst, "status", "")
        if st in ACTIVE_VM_STATUSES:
            return True
    return vm.status in ACTIVE_VM_STATUSES

def _count_active_windows(vms: list[VMRecord]) -> int:
    n = 0
    for vm in vms:
        for inst in vm.instances or []:
            st = inst.get("status", "") if isinstance(inst, dict) else ""
            if st in (VMStatus.STARTING.value, VMStatus.RUNNING.value):
                n += 1
        if not vm.instances and vm.status in (VMStatus.STARTING.value, VMStatus.RUNNING.value):
            n += 1
    return n

def _vm_has_creds(vm: VMRecord) -> bool:
    for inst in vm.instances or []:
        if _instance_has_creds(inst, vm.kind):
            return True
    return bool((vm.password or "").strip())

def _ensure_expiry_clock(vm: VMRecord) -> bool:
    if vm.status != VMStatus.RUNNING.value or not _vm_has_creds(vm):
        return False
    if vm.ready_at:
        return False
    now = datetime.now(timezone.utc)
    vm.ready_at = now.isoformat()
    exp = _parse_iso(vm.expires_at)
    if not exp or exp <= now:
        vm.expires_at = (now + timedelta(minutes=vm.duration_minutes)).isoformat()
    return True

# ── JSON Storage ─────────────────────────────────────────────────────

class JsonStore:
    def __init__(self, path: Path, default: Any):
        self.path = path
        self.default = default
        self._lock = asyncio.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write_sync(default)

    def _read_sync(self) -> Any:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return json.loads(json.dumps(self.default))

    def _write_sync(self, data: Any) -> None:
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp.replace(self.path)

    async def load(self) -> Any:
        async with self._lock:
            return await asyncio.to_thread(self._read_sync)

    async def save(self, data: Any) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write_sync, data)

def _vm_key(user_id: int, kind: str = "windows") -> str:
    if kind == "vps":
        return f"{user_id}:vps"
    return str(user_id)

class DataManager:
    def __init__(self, base: Path):
        self.users = JsonStore(base / "users.json", {})
        self.vms = JsonStore(base / "vms.json", {})
        self.blacklist = JsonStore(base / "blacklist.json", [])
        self.meta = JsonStore(base / "meta.json", {})
        self._lock = asyncio.Lock()

    async def get_meta(self) -> dict:
        data = await self.meta.load()
        return data if isinstance(data, dict) else {}

    async def set_meta(self, **kwargs) -> None:
        data = await self.meta.load()
        if not isinstance(data, dict):
            data = {}
        data.update(kwargs)
        await self.meta.save(data)

    async def get_user_lang(self, user_id: int) -> str:
        data = await self.users.load()
        rec = data.get(str(user_id), {})
        if isinstance(rec, dict):
            return rec.get("lang", "")
        return ""

    async def set_user_lang(self, user_id: int, lang: str) -> None:
        async with self._lock:
            data = await self.users.load()
            rec = data.get(str(user_id), {})
            if not isinstance(rec, dict):
                rec = {}
            rec["lang"] = lang
            data[str(user_id)] = rec
            await self.users.save(data)

    async def get_vm(self, user_id: int, kind: Optional[str] = None) -> Optional[VMRecord]:
        data = await self.vms.load()
        if kind == "vps":
            rec = data.get(f"{user_id}:vps")
            if not rec:
                legacy = data.get(str(user_id))
                if legacy and isinstance(legacy, dict) and legacy.get("kind") == "vps":
                    rec = legacy
            return VMRecord(**rec) if rec else None
        if kind == "windows":
            rec = data.get(str(user_id))
            if rec and isinstance(rec, dict) and rec.get("kind") == "vps":
                return None
            return VMRecord(**rec) if rec else None
        # Bất kỳ loại nào — ưu tiên Windows, fallback VPS
        rec = data.get(str(user_id))
        if rec and isinstance(rec, dict):
            return VMRecord(**rec)
        rec = data.get(f"{user_id}:vps")
        return VMRecord(**rec) if rec else None

    async def set_vm(self, rec: VMRecord) -> None:
        data = await self.vms.load()
        key = _vm_key(rec.discord_id, rec.kind)
        data[key] = asdict(rec)
        plain = str(rec.discord_id)
        if key != plain:
            legacy = data.get(plain)
            if isinstance(legacy, dict) and legacy.get("kind") == "vps":
                data.pop(plain, None)
        await self.vms.save(data)

    async def delete_vm(self, user_id: int, kind: str = "windows") -> None:
        data = await self.vms.load()
        data.pop(_vm_key(user_id, kind), None)
        data.pop(str(user_id), None)
        await self.vms.save(data)

    async def all_vms(self) -> list[VMRecord]:
        data = await self.vms.load()
        new_data: dict[str, Any] = {}
        records: list[VMRecord] = []
        changed = False
        for key, rec in data.items():
            if not isinstance(rec, dict):
                continue
            kind = rec.get("kind") or "windows"
            uid = str(key).split(":")[0]
            proper = _vm_key(int(uid) if uid.lstrip("-").isdigit() else 0, kind)
            if proper != key:
                changed = True
            new_data[proper] = rec
            records.append(VMRecord(**rec))
        if changed:
            await self.vms.save(new_data)
        return records

    async def is_blacklisted(self, user_id: int) -> bool:
        bl = await self.blacklist.load()
        if not isinstance(bl, dict):
            return user_id in bl or str(user_id) in bl
        entry = bl.get(str(user_id))
        if entry is None:
            return False
        if isinstance(entry, dict):
            until = _parse_iso(entry.get("until", ""))
            if until and until <= datetime.now(timezone.utc):
                async with self._lock:
                    bl.pop(str(user_id), None)
                    await self.blacklist.save(bl)
                return False
        return True

    async def add_blacklist(self, user_id: int, days: int = 0, reason: str = "") -> None:
        async with self._lock:
            bl = await self.blacklist.load()
            if not isinstance(bl, dict):
                bl = {}
            until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat() if days > 0 else ""
            bl[str(user_id)] = {"until": until, "reason": reason, "at": _utc_now_iso()}
            await self.blacklist.save(bl)

    async def remove_blacklist(self, user_id: int) -> bool:
        async with self._lock:
            bl = await self.blacklist.load()
            changed = False
            if isinstance(bl, dict):
                if str(user_id) in bl:
                    bl.pop(str(user_id), None)
                    changed = True
            else:
                if user_id in bl:
                    bl.remove(user_id)
                    changed = True
                if str(user_id) in bl:
                    bl.remove(str(user_id))
                    changed = True
            if changed:
                await self.blacklist.save(bl)
            return changed

    async def blacklist_remaining(self, user_id: int) -> str:
        bl = await self.blacklist.load()
        if not isinstance(bl, dict):
            return ""
        entry = bl.get(str(user_id))
        if not isinstance(entry, dict):
            return ""
        until = _parse_iso(entry.get("until", ""))
        if not until:
            return ""
        delta = until - datetime.now(timezone.utc)
        if delta.total_seconds() <= 0:
            return ""
        hours = int(delta.total_seconds() // 3600)
        mins = int((delta.total_seconds() % 3600) // 60)
        if hours:
            return f"{hours}h {mins}m"
        return f"{mins}m"

    async def stats_summary(self) -> dict:
        vms = await self.all_vms()
        users_data = await self.users.load()
        bl = await self.blacklist.load()
        now = datetime.now(timezone.utc)
        active_vms = 0
        total_instances = 0
        running_instances = 0
        status_counts: dict[str, int] = {}
        for vm in vms:
            if vm.instances:
                for inst in vm.instances:
                    total_instances += 1
                    st = inst.get("status", "")
                    status_counts[st] = status_counts.get(st, 0) + 1
                    if st in (VMStatus.RUNNING.value, VMStatus.STARTING.value):
                        active_vms += 1
                    if st == VMStatus.RUNNING.value:
                        running_instances += 1
            else:
                total_instances += 1
                st = vm.status
                status_counts[st] = status_counts.get(st, 0) + 1
                if st in (VMStatus.RUNNING.value, VMStatus.STARTING.value):
                    active_vms += 1
                if st == VMStatus.RUNNING.value:
                    running_instances += 1
        registered_users = sum(1 for v in users_data.values() if isinstance(v, dict) and v.get("lang"))
        return {
            "active_vms": active_vms,
            "total_instances": total_instances,
            "running_instances": running_instances,
            "status_counts": status_counts,
            "registered_users": registered_users,
            "blacklisted": len(bl),
            "total_vm_records": len(vms),
        }

# ── GitHub API Client ────────────────────────────────────────────────

class GitHubAPIError(Exception):
    def __init__(self, status: int, message: str, reset_at: Optional[float] = None):
        self.status = status
        self.message = message
        self.reset_at = reset_at
        super().__init__(f"GitHub API {status}: {message}")

def _is_rate_limited(e: Exception) -> bool:
    msg = str(getattr(e, "message", e)).lower()
    return getattr(e, "status", 0) in (403, 429) and ("rate limit" in msg or "secondary rate" in msg)

def _rate_limit_msg(lang: str) -> str:
    return _t(
        lang,
        " GitHub is currently at its request limit (API busy). Please wait **~10–60 minutes** and try again.",
        " GitHub đang hết lượt gọi API (hệ thống bận). Vui lòng đợi **khoảng 10–60 phút** rồi thử lại.",
    )

class GitHubClient:
    BASE = "https://api.github.com"

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self._etag_cache: dict[str, tuple[str, Any]] = {}

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": GITHUB_USER_AGENT,
        }

    async def _request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        json_body: Any = None,
        params: Optional[dict] = None,
        raw: bool = False,
    ) -> Any:
        url = path if path.startswith("http") else f"{self.BASE}{path}"
        cache_key = url
        if params:
            qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            cache_key = f"{url}?{qs}"
        last_err: Optional[Exception] = None
        for attempt in range(1, GITHUB_RETRY_MAX + 1):
            try:
                headers = self._headers(token)
                cached: Optional[tuple[str, Any]] = None
                if method == "GET" and not raw:
                    cached = self._etag_cache.get(cache_key)
                    if cached:
                        headers["If-None-Match"] = cached[0]
                async with self.session.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status == 304 and cached is not None:
                        return cached[1]
                    text = await resp.text()
                    reset_at = None
                    if resp.status in (403, 429):
                        reset_hdr = resp.headers.get("X-RateLimit-Reset")
                        if reset_hdr:
                            try:
                                reset_at = float(reset_hdr)
                            except ValueError:
                                reset_at = None
                        if reset_at is None:
                            retry_after = resp.headers.get("Retry-After")
                            if retry_after:
                                try:
                                    reset_at = time.time() + float(retry_after)
                                except ValueError:
                                    reset_at = None
                    if resp.status in (401, 403):
                        raise GitHubAPIError(resp.status, text[:300], reset_at=reset_at)
                    if resp.status == 404:
                        raise GitHubAPIError(404, text[:300])
                    if resp.status == 422 and "already exists" in text.lower():
                        return {"exists": True}
                    if resp.status >= 400:
                        raise GitHubAPIError(resp.status, text[:300], reset_at=reset_at)
                    if raw:
                        return text
                    if resp.status == 204 or not text:
                        return {}
                    data = json.loads(text)
                    if method == "GET":
                        etag = resp.headers.get("ETag")
                        if etag:
                            self._etag_cache[cache_key] = (etag, data)
                    self._check_rate_remaining(resp)
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError, GitHubAPIError) as e:
                last_err = e
                if isinstance(e, GitHubAPIError) and e.status in (401, 403, 404):
                    raise
                wait = GITHUB_RETRY_BASE_SEC * (2 ** (attempt - 1))
                await asyncio.sleep(wait)
        raise last_err or GitHubAPIError(0, "Unknown error")

    def _check_rate_remaining(self, resp: aiohttp.ClientResponse) -> None:
        raw = resp.headers.get("X-RateLimit-Remaining")
        try:
            remaining = int(raw) if raw else None
        except ValueError:
            remaining = None
        if remaining is None:
            return
        if remaining <= 10:
            logging.getLogger("vm_bot").warning(
                "GitHub API gan can request — con %s, se tam dung polling neu cham 403/429", remaining
            )
        elif remaining <= RATE_LIMIT_WARN_THRESHOLD:
            logging.getLogger("vm_bot").warning("GitHub API remaining: %s requests", remaining)

    async def verify_token(self, token: str) -> dict:
        return await self._request("GET", "/user", token)

    async def get_user_login(self, token: str) -> str:
        user = await self.verify_token(token)
        return user["login"]

    async def repo_exists(self, token: str, owner: str, repo: str) -> bool:
        try:
            await self._request("GET", f"/repos/{owner}/{repo}", token)
            return True
        except GitHubAPIError as e:
            if e.status == 404:
                return False
            raise

    async def create_repo(self, token: str, repo: str, private: bool = False) -> dict:
        return await self._request(
            "POST",
            "/user/repos",
            token,
            json_body={"name": repo, "private": private, "auto_init": True},
        )

    async def delete_repo(self, token: str, owner: str, repo: str) -> bool:
        try:
            await self._request("DELETE", f"/repos/{owner}/{repo}", token)
            return True
        except GitHubAPIError as e:
            logging.getLogger("vm_bot").warning("delete_repo: %s", e)
            return False

    async def enable_actions(self, token: str, owner: str, repo: str) -> None:
        try:
            await self._request(
                "PUT",
                f"/repos/{owner}/{repo}/actions/permissions",
                token,
                json_body={"enabled": True, "allowed_actions": "all"},
            )
        except GitHubAPIError as e:
            logging.getLogger("vm_bot").debug("enable_actions: %s", e)

    async def put_file(self, token: str, owner: str, repo: str, path: str, content: str, message: str) -> None:
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        existing_sha = None
        try:
            f = await self._request("GET", f"/repos/{owner}/{repo}/contents/{path}", token)
            existing_sha = f.get("sha")
        except GitHubAPIError as e:
            if e.status != 404:
                raise
        body: dict[str, Any] = {"message": message, "content": encoded}
        if existing_sha:
            body["sha"] = existing_sha
        await self._request("PUT", f"/repos/{owner}/{repo}/contents/{path}", token, json_body=body)

    async def put_binary_file(self, token: str, owner: str, repo: str, path: str, b64_content: str, message: str) -> None:
        existing_sha = None
        try:
            f = await self._request("GET", f"/repos/{owner}/{repo}/contents/{path}", token)
            existing_sha = f.get("sha")
        except GitHubAPIError as e:
            if e.status != 404:
                raise
        body: dict[str, Any] = {"message": message, "content": b64_content}
        if existing_sha:
            body["sha"] = existing_sha
        await self._request("PUT", f"/repos/{owner}/{repo}/contents/{path}", token, json_body=body)

    async def delete_file(self, token: str, owner: str, repo: str, path: str) -> bool:
        try:
            f = await self._request("GET", f"/repos/{owner}/{repo}/contents/{path}", token)
            sha = f.get("sha")
            if not sha:
                return False
            await self._request(
                "DELETE",
                f"/repos/{owner}/{repo}/contents/{path}",
                token,
                json_body={"message": "Remove workflow (security)", "sha": sha},
            )
            return True
        except GitHubAPIError as e:
            if e.status == 404:
                return False
            logging.getLogger("vm_bot").warning("delete_file %s: %s", path, e)
            return False

    async def upsert_secret(self, token: str, owner: str, repo: str, name: str, value: str) -> None:
        from nacl import encoding, public
        key_data = await self._request(
            "GET", f"/repos/{owner}/{repo}/actions/secrets/public-key", token
        )
        pk = public.PublicKey(key_data["key"].encode(), encoding.Base64Encoder())
        sealed = public.SealedBox(pk).encrypt(value.encode())
        encrypted = base64.b64encode(sealed).decode()
        await self._request(
            "PUT",
            f"/repos/{owner}/{repo}/actions/secrets/{name}",
            token,
            json_body={"encrypted_value": encrypted, "key_id": key_data["key_id"]},
        )

    async def get_default_branch(self, token: str, owner: str, repo: str) -> str:
        data = await self._request("GET", f"/repos/{owner}/{repo}", token)
        return data.get("default_branch", "main")

    async def dispatch_workflow(self, token: str, owner: str, repo: str, workflow_file: str, inputs: Optional[dict] = None, ref: Optional[str] = None) -> None:
        branch = ref or await self.get_default_branch(token, owner, repo)
        body: dict[str, Any] = {"ref": branch}
        if inputs:
            body["inputs"] = inputs
        await self._request(
            "POST",
            f"/repos/{owner}/{repo}/actions/workflows/{workflow_file}/dispatches",
            token,
            json_body=body,
        )

    async def dispatch_repository_event(self, token: str, owner: str, repo: str, event_type: str, payload: Optional[dict] = None) -> None:
        body: dict[str, Any] = {"event_type": event_type}
        if payload:
            body["client_payload"] = payload
        await self._request(
            "POST",
            f"/repos/{owner}/{repo}/dispatches",
            token,
            json_body=body,
        )

    async def get_workflow(self, token: str, owner: str, repo: str, workflow_file: str) -> dict:
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/workflows/{workflow_file}", token)

    async def cancel_run(self, token: str, owner: str, repo: str, run_id: int) -> bool:
        try:
            await self._request("POST", f"/repos/{owner}/{repo}/actions/runs/{run_id}/cancel", token)
            return True
        except GitHubAPIError as e:
            if e.status in (409, 422):
                return False
            raise

def _is_real_ip(ip: str) -> bool:
    s = (ip or "").strip().lower()
    return bool(s) and s != "pending"

def _apply_creds_to_instance(inst: dict, creds: dict[str, str]) -> None:
    sshx_url = (creds.get("sshx_url") or "").strip()
    password = (creds.get("password") or "").strip()
    if sshx_url == "pending":
        sshx_url = ""
    if not password and not sshx_url:
        return
    if sshx_url:
        inst["sshx_url"] = sshx_url
        inst["hostname"] = creds.get("hostname", "root")
        inst["username"] = creds.get("username", "root")
        inst["login"] = creds.get("login") or "root"
        inst["status"] = VMStatus.RUNNING.value
        return
    new_ip = (creds.get("ip") or "").strip()
    inst["hostname"] = creds.get("hostname", TAILSCALE_HOSTNAME)
    if _is_real_ip(new_ip):
        inst["ip"] = new_ip
    elif not _is_real_ip(str(inst.get("ip", ""))):
        inst["ip"] = new_ip or "pending"
    inst["username"] = creds.get("username", VM_WINDOWS_USER)
    inst["password"] = password
    login = (creds.get("login") or creds.get("username") or VM_WINDOWS_USER).strip()
    if "\\" in login:
        login = login.split("\\")[-1]
    inst["login"] = login or VM_WINDOWS_USER
    inst["status"] = VMStatus.RUNNING.value

def _apply_webhook_creds(inst: dict, data: dict) -> None:
    clip = str(data.get("clip_url") or data.get("driveway_url") or "").strip()
    if clip:
        inst["clip_url"] = clip
    creds: dict[str, str] = {}
    if (data.get("sshx_url") or "").strip():
        creds["sshx_url"] = str(data["sshx_url"]).strip()
        creds["hostname"] = str(data.get("hostname") or "root")
        creds["username"] = str(data.get("username") or "root")
        creds["login"] = str(data.get("login") or "root")
    else:
        creds["hostname"] = str(data.get("hostname") or TAILSCALE_HOSTNAME)
        creds["ip"] = str(data.get("ip") or "")
        creds["username"] = str(data.get("username") or VM_WINDOWS_USER)
        creds["password"] = str(data.get("password") or "")
        creds["login"] = str(data.get("login") or "")
    _apply_creds_to_instance(inst, creds)

# ── Creating Set ─────────────────────────────────────────────────────

class _CreatingSet:
    def __init__(self):
        self._set: set[str] = set()
        self._lock = asyncio.Lock()

    async def try_add(self, key: str) -> bool:
        async with self._lock:
            if key in self._set:
                return False
            self._set.add(key)
            return True

    async def discard(self, key: str) -> None:
        async with self._lock:
            self._set.discard(key)

# ── UI ───────────────────────────────────────────────────────────────

def build_status_embed(
    vm: Optional[VMRecord],
    gh_login: str = "?",
    global_active: int = 0,
    lang: str = "vi",
) -> discord.Embed:
    if vm is None:
        desc_en = (
            "You don't have a VM yet.\nClick **Create VM** to start.\n\n"
            f"Active machines on system: `{global_active}` sessions"
        )
        desc_vi = (
            "Bạn chưa có VM.\nNhấn **Create VM** để bắt đầu.\n\n"
            f"Máy đang chạy trên hệ thống: `{global_active}` phiên"
        )
        embed = discord.Embed(
            title=f" {BRAND_NAME} — Windows VM",
            description=desc_en if lang == "en" else desc_vi,
            color=0x5865F2,
            timestamp=datetime.now(VN_TZ),
        )
        embed.set_footer(text=_t(lang, f"{BRAND_NAME} · Max 10 machines per session", f"{BRAND_NAME} · Tối đa 10 máy/lần"))
        return embed

    try:
        status = VMStatus(vm.status)
    except ValueError:
        status = VMStatus.OFFLINE

    emoji = STATUS_EMOJI.get(status, "⚪")
    color = STATUS_COLOR.get(status, 0x5865F2)

    embed = discord.Embed(
        title=_t(lang, f"{emoji} VM Status — {status.value.upper()}", f"{emoji} Trạng thái VM — {status.value.upper()}"),
        color=color,
        timestamp=datetime.now(VN_TZ),
    )

    active_n = sum(
        1 for i in (vm.instances or [])
        if (i.get("status") if isinstance(i, dict) else "") in (VMStatus.STARTING.value, VMStatus.RUNNING.value)
    ) or (1 if vm.status in (VMStatus.STARTING.value, VMStatus.RUNNING.value) else 0)

    embed.add_field(name=_t(lang, "Owner", "Chủ sở hữu"), value=f"<@{vm.owner_id}>", inline=True)
    embed.add_field(name=_t(lang, "Platform", "Nền tảng"), value=f"`{BRAND_NAME}`", inline=True)
    embed.add_field(name=_t(lang, "Project", "Dự án"), value=f"`{vm.repo}`", inline=True)
    embed.add_field(name=_t(lang, "Windows Version", "Phiên bản Windows"), value=f"`{_runner_name(vm.runner)}`", inline=True)
    embed.add_field(name=_t(lang, "Duration", "Thời gian"), value=_fmt_duration(vm.duration_minutes, lang=lang), inline=True)
    embed.add_field(name=_t(lang, "Running", "Đang chạy"), value=f"`{active_n}/{vm.machine_count}`", inline=True)
    embed.add_field(name=_t(lang, "Active sessions", "Phiên đang hoạt động"), value=f"`{global_active}`", inline=True)
    embed.add_field(name=_t(lang, "Session", "Phiên"), value=f"`{vm.workflow_name}`", inline=True)
    embed.add_field(name=_t(lang, "Deploy status", "Trạng thái"), value=f"`{vm.workflow_status}`", inline=True)

    if "queued" in vm.workflow_status.lower():
        embed.add_field(
            name=_t(lang, "⏳ Waiting for a cloud server", "⏳ Đang chờ máy chủ dịch vụ"),
            value=_t(lang,
                "The service is busy — the machine is **queued**.\n"
                "If it takes too long, the bot will cancel it and notify you to retry in ~30 minutes.",
                "Dịch vụ đang quá tải — máy của bạn **đang xếp hàng**.\n"
                "Nếu đứng quá lâu, bot sẽ tự hủy và báo bạn tạo lại sau ~30 phút."),
            inline=False,
        )

    # Hiển thị thời gian còn lại (cập nhật mỗi 10 giây)
    if vm.expires_at:
        remaining = _fmt_remaining(vm.expires_at, lang=lang)
        embed.add_field(name=_t(lang, "Time remaining", "Thời gian còn lại"), value=f"**{remaining}**", inline=False)

    if vm.status == VMStatus.RUNNING.value and vm.ip:
        embed.add_field(name="IP", value=f"||`{vm.ip}`||", inline=True)
        embed.add_field(name="User", value=f"||`{vm.username or '-'}`||", inline=True)

    for _inst in (vm.instances or []):
        _clip = str(_inst.get("clip_url") or "").strip()
        if _clip:
            embed.add_field(
                name=_t(lang, "🎬 Experience Clip (Driveway)", "🎬 Link Clip Trải Nghiệm"),
                value=f"[Xem clip tại đây]({_clip})",
                inline=False,
            )
            break

    embed.set_footer(text=_t(lang, "Info sent via DM", "Thông tin gửi qua DM"))
    return embed

def build_vm_info_embed(vm: VMRecord, title_suffix: str = "", lang: str = "vi") -> discord.Embed:
    embed = discord.Embed(
        title=_t(lang, f" {BRAND_NAME} — VM Info{title_suffix}", f" {BRAND_NAME} — Thông tin máy{title_suffix}"),
        description=_t(lang, "**Keep secret** — sent privately.", "**Giữ bí mật** — gửi riêng cho bạn."),
        color=0x5865F2,
        timestamp=datetime.now(VN_TZ),
    )
    instances = vm.instances or []
    if not instances and vm.password:
        instances = [{"hostname": TAILSCALE_HOSTNAME, "ip": vm.ip or "pending", "username": vm.username, "password": vm.password}]
    for i, inst in enumerate(instances, 1):
        if not _instance_has_password(inst):
            continue
        name = inst.get("hostname") or f"{TAILSCALE_HOSTNAME}-{inst.get('instance_id', i)}"
        ip_show = inst.get("ip") or "pending"
        login = inst.get("login") or inst.get("username") or VM_WINDOWS_USER
        if "\\" in str(login):
            login = str(login).split("\\")[-1]
        embed.add_field(
            name=_t(lang, f"Machine {i} — {name}", f"Máy {i} — {name}"),
            value=_t(lang,
                f"**IP:** `{ip_show}`\n**Account:** `{login}`\n**Password:** ||`{inst.get('password', '-')}`||\n*Connect via Remote Desktop (RDP)*",
                f"**IP:** `{ip_show}`\n**Tài khoản:** `{login}`\n**Mật khẩu:** ||`{inst.get('password', '-')}`||\n*Kết nối qua Remote Desktop (RDP)*",
            ),
            inline=False,
        )
        clip = str(inst.get("clip_url") or "").strip()
        if clip:
            embed.add_field(
                name=_t(lang, "🎬 Experience Clip (Driveway)", "🎬 Link Clip Trải Nghiệm"),
                value=f"[Xem clip tại đây]({clip})",
                inline=False,
            )
    embed.add_field(name=_t(lang, "Project", "Dự án"), value=f"`{vm.repo}`", inline=True)
    ts_key = ADMIN_TAILSCALE_KEY or "N/A"
    embed.add_field(
        name=_t(lang, "🔗 Connect via Tailscale", "🔗 Kết nối qua Tailscale"),
        value=_t(lang,
            "Use the auth key below to join your device to the same Tailscale network and RDP into the VM.\n\n"
            "**Step A:** Download & install Tailscale from https://tailscale.com/download\n"
            "**Step B:** Open Tailscale → Settings → Account → ⋮ → Use an Auth Key\n"
            "**Step C:** Paste the key below and connect.\n\n"
            "After connecting, use the IP and password above to RDP in.\n"
            "**🔑 Auth Key:** ||`" + ts_key + "`||",
            "Sử dụng auth key bên dưới để kết nối thiết bị của bạn vào cùng mạng Tailscale "
            "và truy cập VM qua Remote Desktop (RDP).\n\n"
            "**Bước A:** Tải và cài đặt Tailscale từ https://tailscale.com/download\n"
            "**Bước B:** Mở Tailscale → Settings → Account → ⋮ → Use an Auth Key\n"
            "**Bước C:** Dán key bên dưới và kết nối.\n\n"
            "Sau khi kết nối, dùng IP và mật khẩu ở trên để RDP vào VM.\n"
            "**🔑 Auth Key:** ||`" + ts_key + "`||",
        ),
        inline=False,
    )
    embed.set_footer(text=_t(lang, f"{BRAND_NAME} VM Bot", f"{BRAND_NAME} VM Bot"))
    return embed

def build_vps_status_embed(
    vm: Optional[VMRecord],
    gh_login: str = "?",
    global_active: int = 0,
    lang: str = "vi",
) -> discord.Embed:
    if vm is None:
        desc_en = (
            "You don't have a VPS yet.\nClick **Create VPS** to start.\n\n"
            f"Active machines on system: `{global_active}` sessions"
        )
        desc_vi = (
            "Bạn chưa có VPS.\nNhấn **Create VPS** để bắt đầu.\n\n"
            f"Máy đang chạy trên hệ thống: `{global_active}` phiên"
        )
        embed = discord.Embed(
            title=f" {BRAND_NAME} — Ubuntu VPS",
            description=desc_en if lang == "en" else desc_vi,
            color=0x2ECC71,
            timestamp=datetime.now(VN_TZ),
        )
        embed.set_footer(text=_t(lang, f"{BRAND_NAME} · Max 6 hours per session", f"{BRAND_NAME} · Tối đa 6 giờ/lần"))
        return embed

    try:
        status = VMStatus(vm.status)
    except ValueError:
        status = VMStatus.OFFLINE

    emoji = STATUS_EMOJI.get(status, "⚪")
    color = STATUS_COLOR.get(status, 0x2ECC71)

    embed = discord.Embed(
        title=_t(lang, f"{emoji} VPS Status — {status.value.upper()}", f"{emoji} Trạng thái VPS — {status.value.upper()}"),
        color=color,
        timestamp=datetime.now(VN_TZ),
    )

    active_n = sum(
        1 for i in (vm.instances or [])
        if (i.get("status") if isinstance(i, dict) else "") in (VMStatus.STARTING.value, VMStatus.RUNNING.value)
    ) or (1 if vm.status in (VMStatus.STARTING.value, VMStatus.RUNNING.value) else 0)

    embed.add_field(name=_t(lang, "Owner", "Chủ sở hữu"), value=f"<@{vm.owner_id}>", inline=True)
    embed.add_field(name=_t(lang, "Platform", "Nền tảng"), value=f"`{BRAND_NAME}`", inline=True)
    embed.add_field(name=_t(lang, "Project", "Dự án"), value=f"`{vm.repo}`", inline=True)
    os_name = _vps_server_name(vm.runner) if vm.runner in VPS_SERVER_NAMES else "Ubuntu Latest"
    embed.add_field(name=_t(lang, "OS", "Hệ điều hành"), value=f"`{os_name}`", inline=True)
    embed.add_field(name=_t(lang, "Specs", "Cấu hình"), value="`4 vCPU · 16 GB RAM`", inline=True)
    embed.add_field(name=_t(lang, "Duration", "Thời gian"), value=_fmt_duration(vm.duration_minutes, lang=lang), inline=True)
    embed.add_field(name=_t(lang, "Running", "Đang chạy"), value=f"`{active_n}/{vm.machine_count}`", inline=True)
    embed.add_field(name=_t(lang, "Active sessions", "Phiên đang hoạt động"), value=f"`{global_active}`", inline=True)
    embed.add_field(name=_t(lang, "Session", "Phiên"), value=f"`{vm.workflow_name}`", inline=True)
    embed.add_field(name=_t(lang, "Deploy status", "Trạng thái"), value=f"`{vm.workflow_status}`", inline=True)

    if "queued" in vm.workflow_status.lower():
        embed.add_field(
            name=_t(lang, "⏳ Waiting for a cloud server", "⏳ Đang chờ máy chủ dịch vụ"),
            value=_t(lang,
                "The service is busy — the VPS is **queued**.\n"
                "If it takes too long, the bot will cancel it and notify you to retry in ~30 minutes.",
                "Dịch vụ đang quá tải — VPS của bạn **đang xếp hàng**.\n"
                "Nếu đứng quá lâu, bot sẽ tự hủy và báo bạn tạo lại sau ~30 phút."),
            inline=False,
        )

    if vm.expires_at:
        remaining = _fmt_remaining(vm.expires_at, lang=lang)
        embed.add_field(name=_t(lang, "Time remaining", "Thời gian còn lại"), value=f"**{remaining}**", inline=False)

    url = ""
    for inst in vm.instances or []:
        u = inst.get("sshx_url")
        if u:
            url = str(u)
            break
    if vm.status == VMStatus.RUNNING.value and url:
        embed.add_field(
            name=_t(lang, "🔒 Terminal Access", "🔒 Truy cập Terminal"),
            value=_t(lang,
                "Top secret — the SSHX link was sent **privately via DM**.",
                "Tuyệt mật — link SSHX đã được gửi **riêng qua DM**."),
            inline=False,
        )

    for _inst in (vm.instances or []):
        _clip = str(_inst.get("clip_url") or "").strip()
        if _clip:
            embed.add_field(
                name=_t(lang, "🎬 Experience Clip (Driveway)", "🎬 Link Clip Trải Nghiệm"),
                value=f"[Xem clip tại đây]({_clip})",
                inline=False,
            )
            break

    embed.set_footer(text=_t(lang, "Info sent via DM", "Thông tin gửi qua DM"))
    return embed

def build_vps_info_embed(vm: VMRecord, title_suffix: str = "", lang: str = "vi") -> discord.Embed:
    embed = discord.Embed(
        title=_t(lang, f" {BRAND_NAME} — Ubuntu VPS Info{title_suffix}", f" {BRAND_NAME} — Thông tin Ubuntu VPS{title_suffix}"),
        description=_t(lang, "**Keep secret** — sent privately.", "**Giữ bí mật** — gửi riêng cho bạn."),
        color=0x2ECC71,
        timestamp=datetime.now(VN_TZ),
    )
    instances = vm.instances or []
    for i, inst in enumerate(instances, 1):
        url = inst.get("sshx_url")
        if not url:
            continue
        embed.add_field(
            name=_t(lang, f"VPS {i} — Interactive Terminal", f"VPS {i} — Terminal tương tác"),
            value=_t(lang,
                f"**Link:** {url}\n"
                f"**User:** `AISTV`\n\n"
                "*Open the link in your browser and click **+** to open a terminal.*\n"
                "The terminal runs on **Ubuntu LTS** with root access (`sudo`).",
                f"**Link:** {url}\n"
                f"**User:** `AISTV`\n\n"
                "*Mở link trong trình duyệt và bấm **+** để mở terminal.*\n"
                "Terminal chạy trên **Ubuntu LTS** với quyền root (`sudo`)."),
            inline=False,
        )
        clip = str(inst.get("clip_url") or "").strip()
        if clip:
            embed.add_field(
                name=_t(lang, "🎬 Experience Clip (Driveway)", "🎬 Link Clip Trải Nghiệm"),
                value=f"[Xem clip tại đây]({clip})",
                inline=False,
            )
    embed.add_field(
        name=_t(lang, "System Specs", "Thông số hệ thống"),
        value=(
            "**OS:** `Ubuntu LTS x86_64`\n"
            "**Host:** `Virtual Machine (Hyper-V)`\n"
            "**CPU:** `Intel Xeon Platinum 8573C (4)`\n"
            "**RAM:** `≈ 16 GB`\n"
            "**Shell:** `bash 5.2` · **Terminal:** `sshx`"
        ),
        inline=False,
    )
    embed.add_field(name=_t(lang, "Project", "Dự án"), value=f"`{vm.repo}`", inline=True)
    embed.add_field(name=_t(lang, "Duration", "Thời gian"), value=_fmt_duration(vm.duration_minutes, lang=lang), inline=True)
    embed.add_field(name=_t(lang, "Expires", "Hết hạn"), value=_fmt_dt(vm.expires_at, lang=lang), inline=True)
    embed.set_footer(text=_t(lang, f"{BRAND_NAME} VM Bot", f"{BRAND_NAME} VM Bot"))
    return embed

def build_welcome_embed(guild: Optional[discord.Guild], lang: str = "vi") -> discord.Embed:
    server_name = guild.name if guild else BRAND_NAME
    embed = discord.Embed(
        title=_t(lang, f" 👋 Welcome to {server_name}!", f" 👋 Chào mừng đến với {server_name}!"),
        description=_t(lang,
            "This bot lets you create a temporary **Windows VM** or **Ubuntu VPS** "
            "powered by cloud servers.\n\n"
            "### Getting started\n"
            "1. `/lang` — choose your language (`vi` / `en`)\n"
            "2. `/vm` — create a temporary Windows VM\n"
            "3. `/vps` — create a temporary Ubuntu VPS (1–6 hours)\n\n"
            "### Other commands\n"
            "`/status` — your machine status · `/rules` — rules · `/ping` — latency",
            "Bot này cho phép bạn tạo **Windows VM** hoặc **Ubuntu VPS** tạm thời "
            "chạy bằng máy chủ dịch vụ.\n\n"
            "### Bắt đầu\n"
            "1. `/lang` — chọn ngôn ngữ (`vi` / `en`)\n"
            "2. `/vm` — tạo Windows VM tạm thời\n"
            "3. `/vps` — tạo Ubuntu VPS tạm thời (1–6 giờ)\n\n"
            "### Lệnh khác\n"
            "`/status` — trạng thái máy · `/rules` — luật · `/ping` — độ trễ"),
        color=0x5865F2,
        timestamp=datetime.now(VN_TZ),
    )
    embed.set_footer(text=f"{BRAND_NAME} VM Bot")
    return embed

# ── Create Wizard ────────────────────────────────────────────────────

class CreateWizardView(discord.ui.View):
    def __init__(self, bot: "VMBot", *, lang: str = "vi"):
        super().__init__(timeout=300)
        self.bot = bot
        self.lang = lang
        self.runners: list[str] = ["windows-latest"]
        duration_opts = [
            discord.SelectOption(
                label=f"{m} {_t(lang, 'min', 'phút')}" if m < 60 else _fmt_duration(m, lang=lang),
                value=str(m)
            )
            for m in DURATION_MINUTES
        ]
        dur_sel = discord.ui.Select(
            placeholder=_t(lang, "Duration (15min - 6h)", "Thời gian chạy (15p - 6h)"),
            options=duration_opts, row=0,
        )
        dur_sel.callback = self._on_duration
        self.add_item(dur_sel)

        runner_opts = [discord.SelectOption(label=_runner_name(r), value=r) for r in WINDOWS_RUNNERS]
        runner_sel = discord.ui.Select(
            placeholder=_t(lang, "Windows version", "Phiên bản Windows"),
            options=runner_opts, row=1,
        )
        runner_sel.callback = self._on_runner
        self.add_item(runner_sel)

        btn = discord.ui.Button(
            label=_t(lang, "Create VM", "Tạo VM"),
            style=discord.ButtonStyle.green, row=2,
        )
        btn.callback = self._on_create
        self.add_item(btn)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.error("CreateWizardView error: %s", error, exc_info=error)
        try:
            await interaction.response.send_message(_t(self.lang, " An error occurred: ", " Đã xảy ra lỗi: ") + _safe_error_text(error), ephemeral=True)
        except Exception:
            pass

    async def _on_duration(self, interaction: discord.Interaction) -> None:
        self.duration = int(interaction.data["values"][0])
        await interaction.response.defer()

    async def _on_runner(self, interaction: discord.Interaction) -> None:
        self.runners = [interaction.data["values"][0]]
        await interaction.response.defer()

    async def _on_create(self, interaction: discord.Interaction) -> None:
        user_id = interaction.user.id
        lang = self.lang

        if not ADMIN_GITHUB_TOKEN or not ADMIN_TAILSCALE_KEY:
            await interaction.response.send_message(
                _t(lang,
                   " System error: admin keys not configured. Contact admin.",
                   " Lỗi hệ thống: admin keys chưa được cấu hình. Liên hệ admin."),
                ephemeral=True,
            )
            return

        if await self.bot.data.is_blacklisted(user_id):
            remaining = await self.bot.data.blacklist_remaining(user_id)
            await interaction.response.send_message(
                _t(lang,
                   " You have been blacklisted." + (f" Time left: **{remaining}**." if remaining else ""),
                   " Bạn đã bị cấm sử dụng bot." + (f" Thời gian còn lại: **{remaining}**." if remaining else "")),
                ephemeral=True,
            )
            return

        existing = await self.bot.data.get_vm(user_id, kind="windows")
        if existing and _vm_is_active(existing):
            await interaction.response.defer(ephemeral=True)
            await self.bot.stop_vm(existing)
            await self.bot.data.delete_vm(user_id, kind="windows")
        else:
            await interaction.response.defer(thinking=True)

        repo = WORKFLOW_REPO
        item = {
            "interaction": interaction,
            "user_id": user_id,
            "token": ADMIN_GITHUB_TOKEN,
            "tailscale_key": ADMIN_TAILSCALE_KEY,
            "repo": repo,
            "duration": self.duration,
            "machine_count": 1,
            "runners": self.runners,
            "lang": lang,
        }

        embed = discord.Embed(
            title=_t(lang, "Creating your VM...", "Đang tạo máy ảo của bạn..."),
            description=_t(lang,
                "System is initializing, please wait...",
                "Hệ thống đang khởi tạo, vui lòng chờ..."),
            color=0x2ECC71,
        )
        await interaction.followup.send(embed=embed)

        task = asyncio.create_task(self.bot._process_create(item))
        task.add_done_callback(lambda t: logging.getLogger("vm_bot").error("Create failed", exc_info=t.exception()) if t.exception() else None)

class CreateVPSWizardView(discord.ui.View):
    def __init__(self, bot: "VMBot", *, lang: str = "vi"):
        super().__init__(timeout=300)
        self.bot = bot
        self.lang = lang
        self.duration = 1
        self.server = "ubuntu-22.04"

        duration_opts = [
            discord.SelectOption(
                label=f"{h} giờ" if lang == "vi" else (f"{h} hour" if h == 1 else f"{h} hours"),
                value=str(h)
            )
            for h in VPS_DURATION_HOURS
        ]
        dur_sel = discord.ui.Select(
            placeholder=_t(lang, "Duration (1h - 6h)", "Thời gian chạy (1h - 6h)"),
            options=duration_opts, row=0,
        )
        dur_sel.callback = self._on_duration
        self.add_item(dur_sel)

        btn = discord.ui.Button(
            label=_t(lang, "Create VPS", "Tạo VPS"),
            style=discord.ButtonStyle.green, row=1,
        )
        btn.callback = self._on_create
        self.add_item(btn)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.error("CreateVPSWizardView error: %s", error, exc_info=error)
        try:
            await interaction.response.send_message(_t(self.lang, " An error occurred: ", " Đã xảy ra lỗi: ") + _safe_error_text(error), ephemeral=True)
        except Exception:
            pass

    async def _on_duration(self, interaction: discord.Interaction) -> None:
        self.duration = int(interaction.data["values"][0])
        await interaction.response.defer()

    async def _on_create(self, interaction: discord.Interaction) -> None:
        user_id = interaction.user.id
        lang = self.lang

        if not ADMIN_GITHUB_TOKEN:
            await interaction.response.send_message(
                _t(lang,
                   " System error: admin keys not configured. Contact admin.",
                   " Lỗi hệ thống: admin keys chưa được cấu hình. Liên hệ admin."),
                ephemeral=True,
            )
            return

        if await self.bot.data.is_blacklisted(user_id):
            remaining = await self.bot.data.blacklist_remaining(user_id)
            await interaction.response.send_message(
                _t(lang,
                   " You have been blacklisted." + (f" Time left: **{remaining}**." if remaining else ""),
                   " Bạn đã bị cấm sử dụng bot." + (f" Thời gian còn lại: **{remaining}**." if remaining else "")),
                ephemeral=True,
            )
            return

        existing = await self.bot.data.get_vm(user_id, kind="vps")
        if existing and _vm_is_active(existing):
            await interaction.response.defer(ephemeral=True)
            await self.bot.stop_vm(existing)
            await self.bot.data.delete_vm(user_id, kind="vps")
        else:
            await interaction.response.defer(thinking=True)

        repo = WORKFLOW_REPO
        item = {
            "interaction": interaction,
            "user_id": user_id,
            "token": ADMIN_GITHUB_TOKEN,
            "repo": repo,
            "duration": self.duration * 60,
            "machine_count": 1,
            "server": self.server,
            "lang": lang,
        }

        embed = discord.Embed(
            title=_t(lang, "Creating your Ubuntu VPS...", "Đang tạo Ubuntu VPS của bạn..."),
            description=_t(lang,
                "System is initializing, please wait...",
                "Hệ thống đang khởi tạo, vui lòng chờ..."),
            color=0x2ECC71,
        )
        await interaction.followup.send(embed=embed)

        task = asyncio.create_task(self.bot._process_create_vps(item))
        task.add_done_callback(lambda t: logging.getLogger("vm_bot").error("Create VPS failed", exc_info=t.exception()) if t.exception() else None)

# ── VM Panel View ────────────────────────────────────────────────────

class VMPanelView(discord.ui.View):
    def __init__(self, bot: "VMBot", *, lang: str = "vi"):
        super().__init__(timeout=None)
        self.bot = bot
        self.lang = lang
        b_create = discord.ui.Button(
            label=_t(lang, "Create VM", "Tạo VM"),
            style=discord.ButtonStyle.green, emoji="\U0001F680", custom_id="vm:create", row=0,
        )
        b_stop = discord.ui.Button(
            label=_t(lang, "Stop VM", "Dừng VM"),
            style=discord.ButtonStyle.secondary, emoji="\U0001F6D1", custom_id="vm:stop", row=0,
        )
        b_del = discord.ui.Button(
            label=_t(lang, "Delete VM", "Xóa VM"),
            style=discord.ButtonStyle.danger, emoji="\U0001F5D1", custom_id="vm:delete", row=1,
        )
        for _btn, _cb in (
            (b_create, self.btn_create),
            (b_stop, self.btn_stop),
            (b_del, self.btn_delete),
        ):
            async def _handler(interaction: discord.Interaction, _btn=_btn, _cb=_cb):
                await _cb(interaction, _btn)

            _btn.callback = _handler
            self.add_item(_btn)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.error("VMPanelView error: custom_id=%s error=%s", item.custom_id if isinstance(item, discord.ui.Button) else "?", error, exc_info=error)
        try:
            await interaction.response.send_message(f" An error occurred: {_safe_error_text(error)}", ephemeral=True)
        except Exception:
            pass

    async def _owner_check(self, interaction: discord.Interaction, vm: Optional[VMRecord], action: str = "Stop") -> bool:
        uid = interaction.user.id
        if vm is None:
            await interaction.response.send_message(
                _t(await _get_lang(uid, self.bot.data), " You don't have a VM.", " Bạn chưa có VM."),
                ephemeral=True,
            )
            return False
        if interaction.user.id != vm.owner_id:
            lang = await _get_lang(uid, self.bot.data)
            try:
                await interaction.response.send_message(
                    _t(lang, " You don't have permission.", " Bạn không có quyền."),
                    ephemeral=True,
                )
            except (discord.NotFound, discord.InteractionResponded):
                pass
            if not is_admin(uid):
                asyncio.create_task(self.bot.punish_abuse(uid, interaction.guild_id, vm.owner_id, action=action))
            return False
        return True

    async def btn_create(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        user_id = interaction.user.id
        lang = await _get_lang(user_id, self.bot.data)

        if not ADMIN_TAILSCALE_KEY:
            try:
                await interaction.response.send_message(
                    _t(lang, " System not configured. Contact admin.", " Hệ thống chưa cấu hình. Liên hệ admin."),
                    ephemeral=True,
                )
            except (discord.NotFound, discord.InteractionResponded):
                pass
            return

        if await self.bot.data.is_blacklisted(user_id):
            remaining = await self.bot.data.blacklist_remaining(user_id)
            try:
                await interaction.response.send_message(
                    _t(lang,
                       " You have been blacklisted." + (f" Time left: **{remaining}**." if remaining else ""),
                       " Bạn đã bị cấm sử dụng bot." + (f" Thời gian còn lại: **{remaining}**." if remaining else "")),
                    ephemeral=True,
                )
            except (discord.NotFound, discord.InteractionResponded):
                pass
            return

        existing = await self.bot.data.get_vm(user_id, kind="windows")
        if existing and _vm_is_active(existing):
            try:
                await interaction.response.defer(ephemeral=True)
            except (discord.NotFound, discord.InteractionResponded):
                return
            await self.bot.stop_vm(existing)
            await self.bot.data.delete_vm(user_id, kind="windows")
            embed = discord.Embed(
                title=_t(lang, f" {BRAND_NAME} — VM Configuration", f" {BRAND_NAME} — Cấu hình VM"),
                description=_t(lang,
                    "Choose duration and Windows version below.",
                    "Chọn thời gian và phiên bản Windows bên dưới."),
                color=0x2ECC71,
            )
            try:
                await interaction.followup.send(embed=embed, view=CreateWizardView(self.bot, lang=lang), ephemeral=True)
            except discord.HTTPException:
                pass
            return

        embed = discord.Embed(
            title=_t(lang, f" {BRAND_NAME} — VM Configuration", f" {BRAND_NAME} — Cấu hình VM"),
            description=_t(lang,
                "Choose duration and Windows version below.",
                "Chọn thời gian và phiên bản Windows bên dưới."),
            color=0x2ECC71,
        )
        try:
            await interaction.response.send_message(embed=embed, view=CreateWizardView(self.bot, lang=lang), ephemeral=True)
        except (discord.NotFound, discord.InteractionResponded):
            pass

    async def btn_stop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        vm = await self.bot.data.get_vm(interaction.user.id, kind="windows")
        if not await self._owner_check(interaction, vm, action="Stop"):
            return
        await interaction.response.defer(thinking=True)
        try:
            msg = await self.bot.stop_vm(vm, lang=lang)
            await interaction.followup.send(msg)
        except Exception as e:
            await interaction.followup.send(f" Error: {_safe_error_text(e)}")

    async def btn_delete(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        vm = await self.bot.data.get_vm(interaction.user.id, kind="windows")
        if not await self._owner_check(interaction, vm, action="Delete"):
            return
        await interaction.response.defer(thinking=True)
        try:
            msg = await self.bot.delete_vm(vm, lang=await _get_lang(interaction.user.id, self.bot.data))
            await interaction.followup.send(msg)
        except Exception as e:
            await self.bot.data.delete_vm(interaction.user.id, kind="windows")
            await interaction.followup.send(f" Deleted record. Error: {_safe_error_text(e)}")

class VPSPanelView(discord.ui.View):
    def __init__(self, bot: "VMBot", *, lang: str = "vi"):
        super().__init__(timeout=None)
        self.bot = bot
        self.lang = lang
        b_create = discord.ui.Button(
            label=_t(lang, "Create VPS", "Tạo VPS"),
            style=discord.ButtonStyle.green, emoji="\U0001F680", custom_id="vps:create", row=0,
        )
        b_stop = discord.ui.Button(
            label=_t(lang, "Stop VPS", "Dừng VPS"),
            style=discord.ButtonStyle.secondary, emoji="\U0001F6D1", custom_id="vps:stop", row=0,
        )
        for _btn, _cb in (
            (b_create, self.btn_create),
            (b_stop, self.btn_stop),
        ):
            async def _handler(interaction: discord.Interaction, _btn=_btn, _cb=_cb):
                await _cb(interaction, _btn)

            _btn.callback = _handler
            self.add_item(_btn)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.error("VPSPanelView error: custom_id=%s error=%s", item.custom_id if isinstance(item, discord.ui.Button) else "?", error, exc_info=error)
        try:
            await interaction.response.send_message(f" An error occurred: {_safe_error_text(error)}", ephemeral=True)
        except Exception:
            pass

    async def _owner_check(self, interaction: discord.Interaction, vm: Optional[VMRecord], action: str = "Stop") -> bool:
        uid = interaction.user.id
        if vm is None:
            await interaction.response.send_message(
                _t(await _get_lang(uid, self.bot.data), " You don't have a VPS.", " Bạn chưa có VPS."),
                ephemeral=True,
            )
            return False
        if interaction.user.id != vm.owner_id:
            lang = await _get_lang(uid, self.bot.data)
            try:
                await interaction.response.send_message(
                    _t(lang, " You don't have permission.", " Bạn không có quyền."),
                    ephemeral=True,
                )
            except (discord.NotFound, discord.InteractionResponded):
                pass
            if not is_admin(uid):
                asyncio.create_task(self.bot.punish_abuse(uid, interaction.guild_id, vm.owner_id, action=action))
            return False
        return True

    async def btn_create(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        user_id = interaction.user.id
        lang = await _get_lang(user_id, self.bot.data)

        if not ADMIN_GITHUB_TOKEN:
            try:
                await interaction.response.send_message(
                    _t(lang, " System not configured. Contact admin.", " Hệ thống chưa cấu hình. Liên hệ admin."),
                    ephemeral=True,
                )
            except (discord.NotFound, discord.InteractionResponded):
                pass
            return

        if await self.bot.data.is_blacklisted(user_id):
            remaining = await self.bot.data.blacklist_remaining(user_id)
            try:
                await interaction.response.send_message(
                    _t(lang,
                       " You have been blacklisted." + (f" Time left: **{remaining}**." if remaining else ""),
                       " Bạn đã bị cấm sử dụng bot." + (f" Thời gian còn lại: **{remaining}**." if remaining else "")),
                    ephemeral=True,
                )
            except (discord.NotFound, discord.InteractionResponded):
                pass
            return

        existing = await self.bot.data.get_vm(user_id, kind="vps")
        if existing and _vm_is_active(existing):
            try:
                await interaction.response.defer(ephemeral=True)
            except (discord.NotFound, discord.InteractionResponded):
                return
            await self.bot.stop_vm(existing)
            await self.bot.data.delete_vm(user_id, kind="vps")
            embed = discord.Embed(
                title=_t(lang, f" {BRAND_NAME} — VPS Configuration", f" {BRAND_NAME} — Cấu hình VPS"),
                description=_t(lang,
                    "Choose duration below.",
                    "Chọn thời gian chạy bên dưới."),
                color=0x2ECC71,
            )
            try:
                await interaction.followup.send(embed=embed, view=CreateVPSWizardView(self.bot, lang=lang), ephemeral=True)
            except discord.HTTPException:
                pass
            return

        embed = discord.Embed(
            title=_t(lang, f" {BRAND_NAME} — VPS Configuration", f" {BRAND_NAME} — Cấu hình VPS"),
            description=_t(lang,
                "Choose duration below.",
                "Chọn thời gian chạy bên dưới."),
            color=0x2ECC71,
        )
        try:
            await interaction.response.send_message(embed=embed, view=CreateVPSWizardView(self.bot, lang=lang), ephemeral=True)
        except (discord.NotFound, discord.InteractionResponded):
            pass

    async def btn_stop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        vm = await self.bot.data.get_vm(interaction.user.id, kind="vps")
        if not await self._owner_check(interaction, vm, action="Stop"):
            return
        await interaction.response.defer(thinking=True)
        try:
            msg = await self.bot.stop_vm(vm, lang=await _get_lang(interaction.user.id, self.bot.data))
            await interaction.followup.send(msg)
        except Exception as e:
            await interaction.followup.send(f" Error: {_safe_error_text(e)}")

# ── Language Cache ───────────────────────────────────────────────────

async def _get_lang(user_id: int, data: DataManager) -> str:
    if user_id not in _lang_cache:
        _lang_cache[user_id] = await data.get_user_lang(user_id)
    return _lang_cache[user_id]

async def _require_lang(interaction: discord.Interaction, data: DataManager) -> Optional[bool]:
    uid = interaction.user.id
    if is_admin(uid):
        return True
    lang = await data.get_user_lang(uid)
    if lang:
        return True
    embed = discord.Embed(
        title=" Language Required / Yêu cầu chọn ngôn ngữ",
        description=(
            " Please set your language first using `/lang`.\n"
            " Vui lòng chọn ngôn ngữ bằng lệnh `/lang` trước.\n\n"
            "`/lang en` — English\n"
            "`/lang vi` — Tiếng Việt"
        ),
        color=0xE74C3C,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
    return False

async def _guild_only(interaction: discord.Interaction) -> bool:
    if interaction.guild is not None:
        return True
    await interaction.response.send_message(
        " Lệnh này chỉ hoạt động trong **server**.", ephemeral=True
    )
    return False
# ── Local Scheduler (Webhook Event-Driven: KHÔNG gọi GitHub API) ─────

class LocalScheduler:
    """Thay thế WorkflowMonitor (poll GitHub API 10s/lần/máy -> vượt rate limit).

    Chỉ xử lý dữ liệu local: hết hạn, cảnh báo sắp hết hạn, máy kẹt 'starting',
    cập nhật panel Discord. 0 request GitHub — mọi trạng thái máy đến từ webhook
    /api/vm-ready do chính GitHub Actions bắn về."""

    def __init__(self, bot: "VMBot"):
        self.bot = bot
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            try:
                vms = await self.bot.data.all_vms()
                for vm in vms:
                    try:
                        await self._check_local(vm)
                    except Exception as e:
                        log.exception("Scheduler check user=%s: %s", vm.discord_id, e)
            except Exception as e:
                log.exception("Scheduler loop: %s", e)
            await asyncio.sleep(SCHEDULER_INTERVAL_SEC)

    async def _check_local(self, vm: VMRecord) -> None:
        now = datetime.now(timezone.utc)
        exp = _parse_iso(vm.expires_at)

        # Máy đang "starting" quá lâu mà webhook chưa từng bắn về -> báo fail (lưới an toàn)
        if vm.status == VMStatus.STARTING.value and not _vm_has_creds(vm):
            created = _parse_iso(vm.created_at)
            if created and (now - created).total_seconds() >= STUCK_START_TIMEOUT_SEC:
                await self._fail_stuck_start(vm)
                return

        if vm.status in (VMStatus.STARTING.value, VMStatus.RUNNING.value):
            if _ensure_expiry_clock(vm):
                await self.bot.data.set_vm(vm)
            if not exp:
                await self._refresh_panel(vm)
                return
            total_seconds = (exp - now).total_seconds()
            minutes_left = int(total_seconds // 60)
            if total_seconds <= 0:
                vm.status = VMStatus.EXPIRED.value
                for inst in vm.instances or []:
                    inst["status"] = VMStatus.EXPIRED.value
                if not vm.expiry_notified:
                    vm.expiry_notified = True
                    await self.bot.data.set_vm(vm)
                    await self.bot._notify_expiry(vm)
                await self.bot.data.delete_vm(vm.discord_id, vm.kind)
                log.info("VM het han — da xoa record user=%s", vm.discord_id)
                return
            await self._maybe_warn(vm, minutes_left)

        await self._refresh_panel(vm)

    async def _maybe_warn(self, vm: VMRecord, minutes_left: int) -> None:
        """Cảnh báo hết hạn theo mốc thời gian (logic local, không cần GitHub)."""
        duration = vm.duration_minutes
        should_warn = False
        warn_attr = ""
        milestone = 0
        if duration == 15 and minutes_left <= 5 and minutes_left > 2:
            should_warn, warn_attr, milestone = True, "warn_5m", 5
        elif duration == 15 and minutes_left <= 2:
            should_warn, warn_attr, milestone = True, "warn_2m", 2
        elif duration == 30 and minutes_left <= 10 and minutes_left > 5:
            should_warn, warn_attr, milestone = True, "warn_10m", 10
        elif duration == 30 and minutes_left <= 5 and minutes_left > 2:
            should_warn, warn_attr, milestone = True, "warn_5m", 5
        elif duration == 30 and minutes_left <= 2:
            should_warn, warn_attr, milestone = True, "warn_2m", 2
        elif duration == 45 and minutes_left <= 15 and minutes_left > 10:
            should_warn, warn_attr, milestone = True, "warn_15m", 15
        elif duration == 45 and minutes_left <= 10 and minutes_left > 5:
            should_warn, warn_attr, milestone = True, "warn_10m", 10
        elif duration == 45 and minutes_left <= 5 and minutes_left > 2:
            should_warn, warn_attr, milestone = True, "warn_5m", 5
        elif duration == 45 and minutes_left <= 2:
            should_warn, warn_attr, milestone = True, "warn_2m", 2
        elif duration >= 60:
            if minutes_left <= 20 and minutes_left > 10:
                should_warn, warn_attr, milestone = True, "warn_20m", 20
            elif minutes_left <= 10 and minutes_left > 5:
                should_warn, warn_attr, milestone = True, "warn_10m", 10
            elif minutes_left <= 5 and minutes_left > 2:
                should_warn, warn_attr, milestone = True, "warn_5m", 5
            elif minutes_left <= 2:
                should_warn, warn_attr, milestone = True, "warn_2m", 2
        if not (should_warn and milestone):
            return
        if milestone in vm.notified_milestones:
            return
        vm.notified_milestones.append(milestone)
        channel = await self.bot.get_expiry_warning_channel(vm)
        if channel:
            try:
                kind_label = "VPS" if vm.kind == "vps" else "MÁY ẢO"
                machine_word = "VPS" if vm.kind == "vps" else "máy ảo"
                machine_en = "VPS" if vm.kind == "vps" else "machine"
                lang = await self.bot.data.get_user_lang(vm.discord_id)
                embed = discord.Embed(
                    title=_t(
                        lang,
                        f"⚠️ {kind_label} SESSION EXPIRING SOON",
                        f"⚠️ PHIÊN {kind_label} SẮP HẾT HẠN",
                    ),
                    description=_t(
                        lang,
                        (
                            f"Hi <@{vm.discord_id}>, your {machine_en} is about to run out of time.\n\n"
                            f"⏱️ Total session time: `{_fmt_duration(vm.duration_minutes, lang='en')}`\n"
                            f"⏳ Time remaining: **About {minutes_left} minutes**\n"
                            f"⏰ Expires at: {_fmt_dt(vm.expires_at, lang='en')}"
                        ),
                        (
                            f"Chào <@{vm.discord_id}>, {machine_word} của bạn sắp hết thời gian duy trì.\n\n"
                            f"⏱️ Tổng thời gian tạo máy: `{_fmt_duration(vm.duration_minutes)}`\n"
                            f"⏳ Thời gian còn lại: **Khoảng {minutes_left} phút**\n"
                            f"⏰ Hết hạn lúc: {_fmt_dt(vm.expires_at)}"
                        ),
                    ),
                    color=0xE67E22,
                    timestamp=datetime.now(timezone.utc)
                )
                embed.set_footer(
                    text=_t(lang,
                        f"{BRAND_NAME} · Save your personal data now!",
                        f"{BRAND_NAME} · Hãy sao lưu dữ liệu cá nhân ngay!")
                )
                await channel.send(
                    content=_t(lang,
                        f"⚠️ <@{vm.discord_id}>! Your {machine_en} session is expiring soon.",
                        f"⚠️ <@{vm.discord_id}> ơi! Phiên {machine_word} của bạn sắp hết hạn."),
                    embed=embed,
                )
                await self.bot.data.set_vm(vm)
                log.info("Sent expiration warning (%s) to channel for user=%s", warn_attr, vm.discord_id)
            except Exception as err:
                log.error("Lỗi gửi cảnh báo hết hạn cho user=%s: %s", vm.discord_id, _safe_error_text(err))
        await self.bot._notify_expiry_warning(vm, minutes_left)
        await self.bot.data.set_vm(vm)

    async def _fail_stuck_start(self, vm: VMRecord) -> None:
        """Webhook không về sau STUCK_START_TIMEOUT_SEC -> thông báo + xóa record."""
        log.warning("STUCK START: user=%s kind=%s — webhook chưa về, hủy record", vm.discord_id, vm.kind)
        await self.bot._notify_vm_failed(vm)
        await self.bot.data.delete_vm(vm.discord_id, vm.kind)

    async def _refresh_panel(self, vm: VMRecord) -> None:
        if not vm.message_id or not vm.channel_id:
            return
        now_ts = time.time()
        status_key = f"{vm.status}|{vm.workflow_status}|{vm.ip}"
        should_update = (
            status_key != vm.last_panel_status
            or (now_ts - vm.last_panel_update) >= MIN_PANEL_INTERVAL_SEC
        )
        if not should_update:
            return
        vm.last_panel_update = now_ts
        vm.last_panel_status = status_key
        try:
            channel = self.bot.get_channel(vm.channel_id) or await self.bot.fetch_channel(vm.channel_id)
            msg = await channel.fetch_message(vm.message_id)
            login = self.bot.gh_user_cache.get(vm.discord_id, "?")
            ga = _count_active_windows(await self.bot.data.all_vms())
            lang = await self.bot.data.get_user_lang(vm.discord_id)
            if vm.kind == "vps":
                embed = build_vps_status_embed(vm, login, global_active=ga, lang=lang)
                view: discord.ui.View = VPSPanelView(self.bot, lang=lang)
            else:
                embed = build_status_embed(vm, login, global_active=ga, lang=lang)
                view = VMPanelView(self.bot, lang=lang)
            await msg.edit(embed=embed, view=view)
        except discord.HTTPException as e:
            if e.status == 429:
                log.warning("Rate limited editing panel for user=%s, will skip next cycles", vm.discord_id)
                vm.last_panel_update = now_ts + 30
            elif e.status == 404:
                vm.message_id = None
        except Exception:
            pass
# ── Bot Class ────────────────────────────────────────────────────────

class VMBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.data = DataManager(DATA_DIR)
        self.session: Optional[aiohttp.ClientSession] = None
        self.gh: Optional[GitHubClient] = None
        self.scheduler = LocalScheduler(self)
        self._creating_set = _CreatingSet()
        self.gh_user_cache: dict[int, str] = {}
        self._repo_ready = False
        self._rate_limit_until = 0.0

    async def _gh_owner(self, gh: "GitHubClient") -> str:
        if WORKFLOW_OWNER:
            return WORKFLOW_OWNER
        owner = self.gh_user_cache.get("_admin_")
        if not owner:
            try:
                meta = await self.data.get_meta()
                owner = meta.get("admin_login", "")
            except Exception:
                owner = ""
        if not owner:
            owner = await gh.get_user_login(ADMIN_GITHUB_TOKEN)
            self.gh_user_cache["_admin_"] = owner
            try:
                await self.data.set_meta(admin_login=owner)
            except Exception:
                pass
        return owner

    def _rate_limited(self) -> bool:
        return time.time() < self._rate_limit_until

    def _mark_rate_limited(self, reset_at: Optional[float] = None) -> None:
        now = time.time()
        if reset_at and reset_at > now:
            until = reset_at + 5
        else:
            until = now + RATE_LIMIT_BACKOFF_SEC
        if until > self._rate_limit_until:
            self._rate_limit_until = until
            wait_s = int(until - now)
            log.warning("GitHub rate limit hit — tạm dừng gọi API %s giây", wait_s)

    async def _ensure_fixed_repo(self) -> None:
        """Đảm bảo 1 repo worker cố định (không spawn/delete repo rác) đã sẵn sàng."""
        gh = self.gh
        if not gh or not ADMIN_GITHUB_TOKEN or self._repo_ready:
            return
        try:
            meta = await self.data.get_meta()
        except Exception:
            meta = {}
        if self._rate_limited():
            return
        owner = await self._gh_owner(gh)
        repo = WORKFLOW_REPO
        # Cache trên đĩa: đã provision đúng version trước đó -> 0 request
        if (
            meta.get("repo_ready") == repo
            and meta.get("repo_owner") == owner
            and meta.get("repo_ready_v") == WORKFLOW_VERSION
        ):
            self._repo_ready = True
            log.info("Fixed worker repo ready (cached v%d): %s/%s", WORKFLOW_VERSION, owner, repo)
            return
        if await gh.repo_exists(ADMIN_GITHUB_TOKEN, owner, repo):
            try:
                await gh.get_workflow(ADMIN_GITHUB_TOKEN, owner, repo, WORKFLOW_FILENAME)
                await gh.get_workflow(ADMIN_GITHUB_TOKEN, owner, repo, VPS_WORKFLOW_FILENAME)
            except GitHubAPIError as e:
                if e.status not in (404,):
                    raise
            await gh.enable_actions(ADMIN_GITHUB_TOKEN, owner, repo)
        else:
            await gh.create_repo(ADMIN_GITHUB_TOKEN, repo, private=False)
            await asyncio.sleep(2)
            await gh.enable_actions(ADMIN_GITHUB_TOKEN, owner, repo)

        # Version thay đổi -> đẩy lại workflow + script mới nhất (idempotent, PUT có sha)
        await gh.put_file(ADMIN_GITHUB_TOKEN, owner, repo, WORKFLOW_PATH, build_github_workflow(), "Add Windows VM workflow")
        await gh.put_file(ADMIN_GITHUB_TOKEN, owner, repo, VPS_WORKFLOW_PATH, build_vps_workflow(), "Add Ubuntu VPS workflow")

        # Scripts — plain text, không base64/obfuscation
        await gh.put_file(ADMIN_GITHUB_TOKEN, owner, repo, "scripts/provision.ps1", _provision_ps1_body(), "Add provision script")
        await gh.put_file(ADMIN_GITHUB_TOKEN, owner, repo, "scripts/keepalive.ps1", _keepalive_ps1_body(), "Add keep-alive script")
        await gh.put_file(ADMIN_GITHUB_TOKEN, owner, repo, "scripts/vps.sh", _vps_sh_body(), "Add VPS setup script")
        await gh.put_file(ADMIN_GITHUB_TOKEN, owner, repo, "scripts/keepalive.sh", _keepalive_sh_body(), "Add VPS keep-alive script")
        await gh.put_file(ADMIN_GITHUB_TOKEN, owner, repo, "scripts/notify_vm_ready.py", _notify_vm_ready_script(), "Add bot webhook notify script")
        await gh.put_file(ADMIN_GITHUB_TOKEN, owner, repo, "scripts/notify_discord.py", _notify_discord_script(), "Add Discord notify script")

        # Secret duy nhất: Tailscale auth key (không còn GH_TOKEN_DELETE)
        if ADMIN_TAILSCALE_KEY:
            await gh.upsert_secret(ADMIN_GITHUB_TOKEN, owner, repo, "TAILSCALE_AUTH_KEY", ADMIN_TAILSCALE_KEY.strip())

        # Webhook để GitHub Actions bắn creds về Bot (loại bỏ nghẽn polling)
        if BOT_WEBHOOK_URL:
            await gh.upsert_secret(ADMIN_GITHUB_TOKEN, owner, repo, "BOT_WEBHOOK_URL", BOT_WEBHOOK_URL.strip())
        if BOT_WEBHOOK_SECRET:
            await gh.upsert_secret(ADMIN_GITHUB_TOKEN, owner, repo, "BOT_WEBHOOK_SECRET", BOT_WEBHOOK_SECRET.strip())
        await self._ensure_extra_secrets(gh, owner, repo)

        # Assets hình ảnh (một lần)
        base_dir = Path(__file__).parent
        for fname in ("img0.png", "img19.png", "user.png"):
            fpath = base_dir / fname
            if fpath.exists():
                with open(fpath, "rb") as f:
                    b64_data = base64.b64encode(f.read()).decode("ascii")
                await gh.put_binary_file(ADMIN_GITHUB_TOKEN, owner, repo, fname, b64_data, f"Add {fname}")

        readme = (
            f"# {BRAND_NAME} VM Worker\n\n"
            "GitHub Actions workers for temporary cloud sessions:\n"
            "- `start-vm` — Windows VM (Remote Desktop + Tailscale)\n"
            "- `start-vps` — Ubuntu interactive terminal (sshx)\n\n"
            "Workflows trigger via `repository_dispatch` with a `matrix` + `duration` "
            "client payload. All setup scripts are plain-text and live in `scripts/`."
        )
        await gh.put_file(ADMIN_GITHUB_TOKEN, owner, repo, "README.md", readme, "Add README.md")

        self._repo_ready = True
        await self.data.set_meta(repo_ready=repo, repo_owner=owner, repo_ready_v=WORKFLOW_VERSION)
        log.info("Fixed worker repo ready v%d: %s/%s", WORKFLOW_VERSION, owner, repo)

    async def _ensure_extra_secrets(self, gh: "GitHubClient", owner: str, repo: str) -> None:
        """Push các secret tùy chọn cho workflow: Discord embed + Driveway clip upload."""
        secrets = {
            "DISCORD_WEBHOOK_URL": DISCORD_WEBHOOK_URL,
            "DISCORD_USER_ID": DISCORD_USER_ID,
            "DRIVEWAY_UPLOAD_URL": DRIVEWAY_UPLOAD_URL,
            "DRIVEWAY_API_KEY": DRIVEWAY_API_KEY,
        }
        for name, value in secrets.items():
            if not value:
                continue
            try:
                await gh.upsert_secret(ADMIN_GITHUB_TOKEN, owner, repo, name, value.strip())
                log.info("Pushed repo secret: %s", name)
            except Exception as e:
                log.warning("Không push được secret %s: %s", name, _safe_error_text(e))

    async def setup_hook(self) -> None:
        ssl_ctx = ssl.create_default_context(cafile=certifi.where()) if _has_certifi else ssl.create_default_context()
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        self.session = aiohttp.ClientSession(connector=connector)
        self.gh = GitHubClient(self.session)
        # Pre-cache admin GitHub login + repo-ready state (tránh đốt request khi đã setup)
        try:
            meta = await self.data.get_meta()
        except Exception:
            meta = {}
        if meta.get("admin_login"):
            self.gh_user_cache["_admin_"] = meta["admin_login"]
        elif ADMIN_GITHUB_TOKEN and self.gh:
            try:
                admin_data = await self.gh.verify_token(ADMIN_GITHUB_TOKEN)
                login = admin_data["login"]
                self.gh_user_cache["_admin_"] = login
                await self.data.set_meta(admin_login=login)
                log.info("Admin GitHub login cached: %s", login)
            except Exception as e:
                log.warning("Could not cache admin GitHub login: %s", e)
        try:
            await self._ensure_fixed_repo()
        except GitHubAPIError as e:
            if _is_rate_limited(e):
                self._mark_rate_limited(getattr(e, "reset_at", None))
            log.warning("Fixed worker repo setup failed (sẽ thử lại khi tạo máy): %s", e)
        except Exception as e:
            log.warning("Fixed worker repo setup failed (sẽ thử lại khi tạo máy): %s", e)
        self.scheduler.start()
        self.add_view(VMPanelView(self))
        self.add_view(VPSPanelView(self))
        await self.tree.sync()
        logging.getLogger("vm_bot").info("Slash commands synced")

    async def close(self) -> None:
        await self.scheduler.stop()
        if self.session:
            await self.session.close()
        await super().close()

    async def _process_create(self, item: dict) -> None:
        uid = item["user_id"]
        if not await self._creating_set.try_add(f"w:{uid}"):
            return
        try:
            await self._do_create_github(item)
        finally:
            await self._creating_set.discard(f"w:{uid}")

    async def _do_create_github(self, item: dict) -> None:
        interaction: discord.Interaction = item["interaction"]
        user_id: int = item["user_id"]
        duration: int = min(int(item["duration"]), 355)
        machine_count: int = int(item["machine_count"])
        runners: list[str] = item.get("runners", ["windows-latest"])
        lang: str = item.get("lang", "vi")
        gh = self.gh
        assert gh is not None

        async def notify(msg: str) -> None:
            try:
                await interaction.followup.send(msg)
            except discord.HTTPException:
                pass

        try:
            await self._ensure_fixed_repo()
            owner = await self._gh_owner(gh)
            repo = WORKFLOW_REPO

            matrix = []
            for i in range(1, machine_count + 1):
                r = runners[i - 1] if i - 1 < len(runners) else runners[-1]
                matrix.append({"instance_id": i, "runner": r})
            # Webhook Event-Driven: 1 request DUY NHẤT (repository_dispatch).
            # KHÔNG poll list_runs — run_id sẽ được điền tự động khi GitHub Actions
            # bắn webhook /api/vm-ready về (payload có github.run_id).
            await gh.dispatch_repository_event(
                ADMIN_GITHUB_TOKEN,
                owner,
                repo,
                "start-vm",
                {"matrix": matrix, "duration": duration, "session": str(user_id)},
            )
            log.info("Dispatched start-vm user=%s machines=%d duration=%d (1 GitHub request)", user_id, machine_count, duration)

            instances = [{"instance_id": str(i + 1), "run_id": None, "status": VMStatus.STARTING.value} for i in range(machine_count)]

            vm = VMRecord(
                discord_id=user_id,
                owner_id=user_id,
                repo=repo,
                github_token=ADMIN_GITHUB_TOKEN,
                workflow_name=WORKFLOW_NAME,
                runner=runners[0],
                runners=runners,
                duration_minutes=duration,
                machine_count=machine_count,
                instances=instances,
                run_id=None,
                status=VMStatus.STARTING.value,
                workflow_status="queued",
                expires_at=(datetime.now(timezone.utc) + timedelta(minutes=duration)).isoformat(),
                guild_id=interaction.guild_id,
            )
            await self.data.set_vm(vm)

            all_vms = await self.data.all_vms()
            embed = build_status_embed(vm, owner, global_active=_count_active_windows(all_vms), lang=lang)
            try:
                msg = await interaction.followup.send(embed=embed, view=VMPanelView(self, lang=lang), wait=True)
                vm.message_id = msg.id
                vm.channel_id = msg.channel.id if msg.channel else None
                await self.data.set_vm(vm)
            except discord.HTTPException:
                pass

            await notify(
                _t(lang,
                   " ⏳ Request sent! Your machine is being configured.\nCredentials and the clip link will be sent automatically when ready.",
                   " ⏳ Yêu cầu đã được gửi! Máy đang khởi tạo.\nSau khi xong, thông số đăng nhập và Link Clip sẽ tự động gửi về đây.")
            )

        except GitHubAPIError as e:
            if e.status == 0 and "PyNaCl" in str(e.message):
                await notify(" Missing PyNaCl. Install: `pip install pynacl`")
            elif _is_rate_limited(e):
                self._mark_rate_limited(getattr(e, "reset_at", None))
                await notify(f"⚠️ {_rate_limit_msg(lang)}")
            else:
                await notify(_t(lang, " Setup failed. Please try again.", " Thiết lập thất bại. Vui lòng thử lại."))
        except Exception as e:
            await notify(f" Error: {_safe_error_text(e)}")

    async def _process_create_vps(self, item: dict) -> None:
        uid = item["user_id"]
        if not await self._creating_set.try_add(f"v:{uid}"):
            return
        try:
            await self._do_create_vps_github(item)
        finally:
            await self._creating_set.discard(f"v:{uid}")

    async def _do_create_vps_github(self, item: dict) -> None:
        interaction: discord.Interaction = item["interaction"]
        user_id: int = item["user_id"]
        duration: int = min(int(item["duration"]), 355)
        lang: str = item.get("lang", "vi")
        gh = self.gh
        assert gh is not None

        async def notify(msg: str) -> None:
            try:
                await interaction.followup.send(msg)
            except discord.HTTPException:
                pass

        try:
            await self._ensure_fixed_repo()
            owner = await self._gh_owner(gh)
            repo = WORKFLOW_REPO
            server = item.get("server", "ubuntu-22.04")

            # Webhook Event-Driven: 1 request DUY NHẤT (repository_dispatch).
            # KHÔNG poll list_runs — run_id đến từ webhook /api/vm-ready.
            await gh.dispatch_repository_event(
                ADMIN_GITHUB_TOKEN,
                owner,
                repo,
                "start-vps",
                {"matrix": [{"instance_id": "1", "runner": VPS_SERVER_RUNS_ON.get(server, "ubuntu-latest")}], "duration": duration, "session": str(user_id)},
            )
            log.info("Dispatched start-vps user=%s duration=%d (1 GitHub request)", user_id, duration)

            instances = [{"instance_id": "1", "run_id": None, "status": VMStatus.STARTING.value}]

            vm = VMRecord(
                discord_id=user_id,
                owner_id=user_id,
                repo=repo,
                github_token=ADMIN_GITHUB_TOKEN,
                workflow_name=VPS_WORKFLOW_NAME,
                kind="vps",
                runner=server,
                duration_minutes=duration,
                machine_count=1,
                instances=instances,
                run_id=None,
                status=VMStatus.STARTING.value,
                workflow_status="queued",
                expires_at=(datetime.now(timezone.utc) + timedelta(minutes=duration)).isoformat(),
                guild_id=interaction.guild_id,
            )
            await self.data.set_vm(vm)

            all_vms = await self.data.all_vms()
            embed = build_vps_status_embed(vm, owner, global_active=_count_active_windows(all_vms), lang=lang)
            try:
                msg = await interaction.followup.send(embed=embed, view=VPSPanelView(self, lang=lang), wait=True)
                vm.message_id = msg.id
                vm.channel_id = msg.channel.id if msg.channel else None
                await self.data.set_vm(vm)
            except discord.HTTPException:
                pass

            await notify(
                _t(lang,
                   " ⏳ Request sent! Your Ubuntu VPS is being configured.\nTerminal link will be sent automatically when ready.",
                   " ⏳ Yêu cầu đã được gửi! Ubuntu VPS đang khởi tạo.\nLink terminal sẽ tự động gửi về đây khi sẵn sàng.")
            )

        except GitHubAPIError as e:
            if e.status == 0 and "PyNaCl" in str(e.message):
                await notify(" Missing PyNaCl. Install: `pip install pynacl`")
            elif _is_rate_limited(e):
                self._mark_rate_limited(getattr(e, "reset_at", None))
                await notify(f"⚠️ {_rate_limit_msg(lang)}")
            else:
                await notify(_t(lang, " Setup failed. Please try again.", " Thiết lập thất bại. Vui lòng thử lại."))
        except Exception as e:
            await notify(f" Error: {_safe_error_text(e)}")

    async def stop_vm(self, vm: VMRecord, lang: str = "vi") -> str:
        vm.status = VMStatus.STOPPING.value
        try:
            await asyncio.wait_for(self.data.set_vm(vm), timeout=10)
        except asyncio.TimeoutError:
            pass
        gh = self.gh
        assert gh is not None
        cancelled = False
        try:
            if not ADMIN_GITHUB_TOKEN:
                return _t(lang, " Cannot stop — admin token not configured.", " Không thể dừng — admin token chưa cấu hình.")
            owner = await asyncio.wait_for(self._gh_owner(gh), timeout=10)
            if vm.run_id:
                cancelled = await asyncio.wait_for(
                    gh.cancel_run(ADMIN_GITHUB_TOKEN, owner, vm.repo, vm.run_id), timeout=10
                )
        except (asyncio.TimeoutError, Exception):
            pass
        for inst in vm.instances or []:
            inst["status"] = VMStatus.OFFLINE.value
        vm.status = VMStatus.OFFLINE.value
        try:
            await asyncio.wait_for(self.data.set_vm(vm), timeout=10)
        except asyncio.TimeoutError:
            pass
        if vm.kind == "vps":
            return _t(lang, " Stopped VPS session.", " Đã dừng phiên VPS.") if cancelled else _t(lang, " VPS session ended.", " Phiên VPS đã kết thúc.")
        return _t(lang, " Stopped VM session.", " Đã dừng phiên máy ảo.") if cancelled else _t(lang, " VM session ended.", " Phiên máy ảo đã kết thúc.")

    async def delete_vm(self, vm: VMRecord, lang: str = "vi") -> str:
        msg = await self.stop_vm(vm, lang=lang)
        await self.data.delete_vm(vm.discord_id, vm.kind)
        return msg + _t(lang, "\n Deleted VM from system.", "\n Đã xóa máy khỏi hệ thống.")

    async def send_vm_dm(self, vm: VMRecord, title_suffix: str = "", lang: str = "vi") -> bool:
        user = self.get_user(vm.discord_id) or await self.fetch_user(vm.discord_id)
        if not user:
            return False
        ready = [i for i in (vm.instances or []) if _instance_has_creds(i, vm.kind)]
        if not ready:
            return False
        if vm.kind == "vps":
            embed = build_vps_info_embed(vm, title_suffix=title_suffix, lang=lang)
            content = _t(lang,
                f" **{BRAND_NAME}** — info for your Ubuntu VPS session:",
                f" **{BRAND_NAME}** — thông tin phiên Ubuntu VPS của bạn:")
        else:
            embed = build_vm_info_embed(vm, title_suffix=title_suffix, lang=lang)
            content = _t(lang,
                f" **{BRAND_NAME}** — info for **{len(ready)}** machine(s):",
                f" **{BRAND_NAME}** — thông tin **{len(ready)}** máy của bạn:")
        try:
            await user.send(content=content, embed=embed)
            log.info("DM sent to user=%s kind=%s (%d machine(s))", vm.discord_id, vm.kind, len(ready))
            return True
        except discord.Forbidden:
            log.warning("DM blocked for user=%s — will send private info to channel fallback", vm.discord_id)
        except discord.HTTPException as e:
            if e.status == 429:
                log.warning("Rate limited sending DM to user=%s, will retry next cycle", vm.discord_id)
                await asyncio.sleep(5)
            else:
                pass
        if vm.channel_id:
            try:
                channel = self.get_channel(vm.channel_id) or await self.fetch_channel(vm.channel_id)
                channel_embed = build_vps_info_embed(vm, title_suffix=title_suffix, lang=lang) if vm.kind == "vps" else build_vm_info_embed(vm, title_suffix=title_suffix, lang=lang)
                await channel.send(
                    content=_t(lang,
                        f"<@{vm.discord_id}> Bot cannot DM you — private info sent here instead. Enable **Allow DMs from server members**.",
                        f"<@{vm.discord_id}> Bot không thể nhắn DM — gửi thông tin riêng tại đây thay thế. Hãy bật **Cho phép nhận tin nhắn riêng từ thành viên**."),
                    embed=channel_embed,
                )
                log.info("DM blocked — fallback info sent to channel for user=%s", vm.discord_id)
                return True
            except Exception as err:
                log.warning("Channel fallback failed for user=%s: %s", vm.discord_id, _safe_error_text(err))
        return False

    async def _notify_expiry(self, vm: VMRecord) -> None:
        user = self.get_user(vm.discord_id) or await self.fetch_user(vm.discord_id)
        if not user:
            return
        try:
            lang = await self.data.get_user_lang(vm.discord_id)
            mins = vm.duration_minutes
            run_hint = f" (run `{vm.run_id}`)" if vm.run_id else ""
            exp_txt = _fmt_dt(vm.expires_at, lang=lang) if vm.expires_at else _fmt_duration(mins)
            cmd_hint = "/vm" if vm.kind != "vps" else "/vps"
            machine_word = "VPS" if vm.kind == "vps" else "Virtual machine"
            machine_vi = "VPS" if vm.kind == "vps" else "máy ảo"
            await user.send(_t(lang,
                f"⌛ **{machine_word} session ended**\n"
                f"Duration: **{_fmt_duration(mins)}** · Expired: **{exp_txt}**{run_hint}\n\n"
                f"The machine has been removed from the system. Use `{cmd_hint}` to create a new session if needed.",
                f"⌛ **Phiên {machine_vi} đã kết thúc**\n"
                f"Thời hạn: **{_fmt_duration(mins)}** · Hết lúc: **{exp_txt}**{run_hint}\n\n"
                f"Máy đã được gỡ khỏi hệ thống. Dùng `{cmd_hint}` để tạo phiên mới nếu cần.",
            ))
        except discord.HTTPException:
            pass

    async def _notify_expiry_warning(self, vm: VMRecord, minutes_left: int = 5) -> None:
        user = self.get_user(vm.discord_id) or await self.fetch_user(vm.discord_id)
        if not user:
            return
        try:
            lang = await self.data.get_user_lang(vm.discord_id)
            exp_txt = _fmt_dt(vm.expires_at, lang=lang) if vm.expires_at else "sắp hết hạn"
            remaining = _fmt_remaining(vm.expires_at, lang=lang) if vm.expires_at else f"{minutes_left} phút"
            icon = "⚠️" if minutes_left < 10 else "⏰"
            machine_word = "VPS" if vm.kind == "vps" else "virtual machine"
            machine_vi = "VPS" if vm.kind == "vps" else "máy ảo"
            await user.send(_t(lang,
                f"{icon} **{machine_word} session expiring soon**\n"
                f"Duration: **{exp_txt}**\n"
                f"⏳ **Time remaining:** {remaining}\n\n"
                "The machine will be automatically removed when time runs out.",
                f"{icon} **Phiên {machine_vi} sắp hết hạn**\n"
                f"Thời hạn: **{exp_txt}**\n"
                f"⏳ **Thời gian còn lại:** {remaining}\n\n"
                "Máy sẽ tự động gỡ khi hết thời gian.",
            ))
        except discord.HTTPException:
            pass


    async def get_expiry_warning_channel(self, vm: VMRecord) -> Optional[discord.abc.Messageable]:
        if vm.guild_id:
            guild = self.get_guild(vm.guild_id)
            if guild:
                channel = _find_channel_by_name(guild, EXPIRY_WARNING_CHANNEL_NAME)
                if channel:
                    return channel
                channel = _find_channel_by_name(guild, SEVER_WINDOWS_CHANNEL_NAME)
                if channel:
                    return channel
        for guild in self.guilds:
            channel = _find_channel_by_name(guild, EXPIRY_WARNING_CHANNEL_NAME)
            if channel:
                return channel
            channel = _find_channel_by_name(guild, SEVER_WINDOWS_CHANNEL_NAME)
            if channel:
                return channel
        if vm.channel_id:
            try:
                channel = self.get_channel(vm.channel_id) or await self.fetch_channel(vm.channel_id)
                if isinstance(channel, (discord.TextChannel, discord.Thread)):
                    return channel
            except discord.HTTPException:
                pass
        return None

    async def notify_admin_error(self, context: str, exc: BaseException, *, user_id: Optional[int] = None) -> None:
        if not ADMIN_IDS:
            return
        detail = _safe_error_text(exc, include_trace=True)
        msg = f" **{BRAND_NAME} Bot Error**\n**Context:** `{context}`\n**User:** `{user_id or '-'}`\n```py\n{detail}\n```"
        for admin_id in ADMIN_IDS:
            try:
                user = self.get_user(admin_id) or await self.fetch_user(admin_id)
                await user.send(msg[:2000])
            except discord.HTTPException:
                continue

    async def punish_abuse(self, offender_id: int, guild_id: Optional[int], target_owner_id: int, action: str = "Stop") -> None:
        """Ai cố tình stop/delete máy của người khác -> cấm bot 3 ngày + thông báo toàn server."""
        await self.data.add_blacklist(offender_id, days=3, reason=f"Attempted {action} of another user's machine")
        log.warning("ABUSE DETECTED: user=%s attempted %s of machine owned by %s (guild=%s)", offender_id, action, target_owner_id, guild_id)
        try:
            offender = self.get_user(offender_id) or await self.fetch_user(offender_id)
            if offender:
                until = datetime.now(timezone.utc) + timedelta(days=3)
                await offender.send(
                    "⛔ **CẢNH BÁO — LẠM DỤNG MÁY ẢO**\n"
                    f"Bạn vừa cố gắng **{action.lower()}** máy của người khác (chủ máy: <@{target_owner_id}>).\n"
                    "Đây là hành vi **nghiêm cấm** theo luật máy ảo AI STV.\n\n"
                    f"Bạn đã bị **cấm sử dụng bot trong 3 ngày** (đến <t:{int(until.timestamp())}:F>).\n"
                    "⚠️ Vi phạm lần tiếp theo sẽ bị **ban vĩnh viễn**."
                )
        except discord.HTTPException:
            pass
        await self.announce_abuse(offender_id, target_owner_id, action, guild_id)

    async def announce_abuse(self, offender_id: int, target_owner_id: int, action: str, guild_id: Optional[int]) -> None:
        embed = discord.Embed(
            title="⛔ CẢNH BÁO VI PHẠM LUẬT — TOÀN SERVER",
            description=(
                f"Thành viên **<@{offender_id}>** vừa cố gắng **{action.lower()}** máy của người khác "
                f"(chủ máy: **<@{target_owner_id}>**).\n\n"
                "Hành vi này **bị nghiêm cấm** theo luật máy ảo AI STV:\n"
                "• Mỗi người chỉ được dùng **máy của chính mình**.\n"
                "• Cấm stop/delete máy của người khác.\n\n"
                f"⛔ **<@{offender_id}>** đã bị **cấm sử dụng bot trong 3 ngày**.\n"
                "⚠️ Lần vi phạm tiếp theo sẽ bị **ban vĩnh viễn**."
            ),
            color=0xE74C3C,
            timestamp=datetime.now(VN_TZ),
        )
        embed.set_footer(text=f"{BRAND_NAME} · Thông báo toàn bộ thành viên")
        sent = 0
        if guild_id:
            guild = self.get_guild(guild_id)
            if guild:
                for ch in guild.text_channels:
                    perms = ch.permissions_for(guild.me)
                    if not (perms.read_messages and perms.send_messages):
                        continue
                    try:
                        await ch.send(content=f"<@{offender_id}>", embed=embed)
                        sent += 1
                        if sent >= 10:
                            break
                    except discord.HTTPException:
                        continue
        if not sent:
            vm = await self.data.get_vm(target_owner_id)
            if vm and vm.channel_id:
                try:
                    channel = self.get_channel(vm.channel_id) or await self.fetch_channel(vm.channel_id)
                    await channel.send(content=f"<@{offender_id}>", embed=embed)
                except discord.HTTPException:
                    pass

    async def refresh_panel_message(self, vm: VMRecord, global_active: int = 0) -> None:
        if not vm.channel_id or not vm.message_id:
            return
        now_ts = time.time()
        if (now_ts - vm.last_panel_update) < MIN_PANEL_INTERVAL_SEC:
            return
        vm.last_panel_update = now_ts
        try:
            channel = self.get_channel(vm.channel_id) or await self.fetch_channel(vm.channel_id)
            msg = await channel.fetch_message(vm.message_id)
            login = self.gh_user_cache.get(vm.discord_id, "?")
            lang = await self.data.get_user_lang(vm.discord_id)
            if vm.kind == "vps":
                embed = build_vps_status_embed(vm, login, global_active=global_active, lang=lang)
                view: discord.ui.View = VPSPanelView(self, lang=lang)
            else:
                embed = build_status_embed(vm, login, global_active=global_active, lang=lang)
                view = VMPanelView(self, lang=lang)
            await msg.edit(embed=embed, view=view)
        except discord.HTTPException as e:
            if e.status == 429:
                log.warning("Rate limited in refresh_panel_message for user=%s", vm.discord_id)
                vm.last_panel_update = now_ts + 30
            elif e.status == 404:
                vm.message_id = None
        except Exception:
            pass

    async def _maybe_send_ready_dm(self, vm: VMRecord) -> None:
        """Gửi DM thông tin máy ngay khi vừa có creds (webhook hoặc polling). Chống gửi lặp."""
        ready = [i for i in (vm.instances or []) if _instance_has_creds(i, vm.kind)]
        if not ready:
            return
        send_dm = False
        if vm.kind == "vps":
            if len(ready) > vm.dm_ready_count:
                vm.dm_ready_count = len(ready)
                send_dm = True
        else:
            real_ip_count = sum(1 for i in ready if _is_real_ip(str(i.get("ip", ""))))
            if real_ip_count > vm.last_real_ip_count:
                vm.last_real_ip_count = real_ip_count
                send_dm = True
        if not send_dm:
            return
        if vm.kind != "vps":
            first = ready[0]
            vm.ip = first.get("ip", "")
            vm.username = first.get("username", VM_WINDOWS_USER)
            vm.password = first.get("password", "")
        lang = await self.data.get_user_lang(vm.discord_id)
        ok = await self.send_vm_dm(vm, lang=lang)
        if ok:
            vm.credentials_sent = True

    async def _process_webhook_ready(self, data: dict) -> None:
        """Nhận creds từ GitHub Actions (POST /api/vm-ready) — cập nhật record + DM tức thì.

        Webhook Event-Driven: GitHub Actions tự bắn khi máy tạo xong (payload gồm run_id,
        IP/pass hoặc sshx_url, và clip_url Driveway). Bot KHÔNG poll GitHub nữa."""
        try:
            discord_id = int(str(data.get("discord_id") or "").strip())
        except (TypeError, ValueError):
            return
        kind = str(data.get("kind") or "windows").lower()
        instance_id = str(data.get("instance_id") or "1")
        status = str(data.get("status") or "ready").lower()
        vm = await self.data.get_vm(discord_id, kind=kind)
        if not vm:
            log.warning("Webhook vm-ready: không tìm thấy record user=%s kind=%s", discord_id, kind)
            return

        run_id_str = str(data.get("run_id") or "").strip()
        if run_id_str:
            try:
                run_id_int = int(run_id_str)
            except (TypeError, ValueError):
                run_id_int = None
            if run_id_int is not None:
                if vm.run_id is None:
                    vm.run_id = run_id_int
                elif str(vm.run_id) != run_id_str:
                    log.warning("Webhook run_id mismatch: user=%s got=%s want=%s", discord_id, run_id_str, vm.run_id)
                    return

        inst = next((i for i in vm.instances if str(i.get("instance_id")) == instance_id), None)
        if not inst:
            inst = {"instance_id": instance_id, "run_id": vm.run_id}
            vm.instances.append(inst)
        if run_id_str:
            inst["run_id"] = run_id_str

        if status in ("failed", "failure", "error", "cancelled", "canceled"):
            log.warning("Webhook vm-ready FAILED user=%s kind=%s instance=%s run=%s", discord_id, kind, instance_id, vm.run_id)
            inst["status"] = VMStatus.FAILED.value
            vm.status = VMStatus.FAILED.value
            await self.data.set_vm(vm)
            await self._notify_vm_failed(vm)
            await self.data.delete_vm(discord_id, kind)
            return

        if inst.get("status") != VMStatus.RUNNING.value:
            inst["status"] = VMStatus.RUNNING.value
        if data.get("clip_url") or data.get("driveway_url"):
            inst["clip_url"] = str(data.get("clip_url") or data.get("driveway_url") or "").strip()
        _apply_webhook_creds(inst, data)
        if all(_instance_has_creds(i, vm.kind) for i in vm.instances):
            vm.status = VMStatus.RUNNING.value
        _ensure_expiry_clock(vm)
        await self.data.set_vm(vm)
        await self._maybe_send_ready_dm(vm)
        await self.data.set_vm(vm)
        log.info("Webhook vm-ready user=%s kind=%s instance=%s run=%s clip=%s", discord_id, kind, instance_id, vm.run_id, bool(inst.get("clip_url")))
        asyncio.create_task(self.refresh_panel_message(vm))

    async def _notify_vm_failed(self, vm: VMRecord) -> None:
        """Báo user khi máy khởi tạo thất bại (do webhook failure hoặc kẹt starting quá lâu)."""
        kind_label = "VPS" if vm.kind == "vps" else "MÁY ẢO"
        machine_word = "VPS" if vm.kind == "vps" else "máy ảo"
        lang = await self.data.get_user_lang(vm.discord_id)
        embed = discord.Embed(
            title=_t(lang, f"🚫 {kind_label} START FAILED", f"🚫 KHỞI TẠO {kind_label} THẤT BẠI"),
            description=_t(lang,
                f"Sorry <@{vm.discord_id}>, your {machine_word} **failed to start**.\n\n"
                f"⏳ Please **try again in a few minutes** using `/{'vps' if vm.kind == 'vps' else 'vm'}`.",
                f"Xin lỗi <@{vm.discord_id}>, {machine_word} của bạn **khởi tạo thất bại**.\n\n"
                f"⏳ Vui lòng **thử lại sau vài phút** bằng lệnh `/{'vps' if vm.kind == 'vps' else 'vm'}`."),
            color=0xE74C3C,
        )
        user = self.get_user(vm.discord_id) or await self.fetch_user(vm.discord_id)
        try:
            if user:
                await user.send(content=f"🚫 **{BRAND_NAME}** — {kind_label} failed to start.", embed=embed)
        except discord.HTTPException:
            pass
        if vm.channel_id:
            try:
                channel = self.get_channel(vm.channel_id) or await self.fetch_channel(vm.channel_id)
                await channel.send(content=f"🚫 <@{vm.discord_id}>", embed=embed)
            except Exception:
                pass

# ── Logging Setup ────────────────────────────────────────────────────

def _setup_logging() -> logging.Logger:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except (AttributeError, OSError):
                pass
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%H:%M:%S"))
    file_handler = logging.FileHandler(DATA_DIR / "bot.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    for logger_name in ("vm_bot", "discord", "discord.ui", "discord.http"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.addHandler(file_handler)
        logger.setLevel(logging.INFO)
    return logging.getLogger("vm_bot")

log = _setup_logging()

# ── Bot Instance ─────────────────────────────────────────────────────

bot = VMBot()

# ── Events ───────────────────────────────────────────────────────────

@bot.event
async def on_ready() -> None:
    log.info(f"Bot logged in as {bot.user} (ID: {bot.user.id})")
    log.info(f"Owner ID: {OWNER_ID or '(not set)'}")
    log.info(f"Admin IDs: {', '.join(str(i) for i in sorted(ADMIN_IDS)) or '(none)'}")
    log.info("No user token cache needed — using admin keys for all operations")

@bot.event
async def on_member_join(member: discord.Member) -> None:
    if member.bot:
        return
    try:
        embed = build_welcome_embed(member.guild)
        await member.send(embed=embed)
        log.info("Welcome DM sent to new member %s", member.id)
    except discord.HTTPException:
        pass

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    log.exception("Slash command error: %s", error)
    if isinstance(error.__cause__, discord.NotFound):
        return
    await bot.notify_admin_error(f"slash:{getattr(interaction.command, 'name', 'unknown')}", error, user_id=interaction.user.id if interaction.user else None)
    msg = " Command encountered an error."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg)
        else:
            await interaction.response.send_message(msg)
    except discord.HTTPException:
        pass

# ── Slash Commands ───────────────────────────────────────────────────

@bot.tree.command(name="vm", description="AI STV — Quản lý Windows VM tạm thời")
async def cmd_vm(interaction: discord.Interaction) -> None:
    if not await _guild_only(interaction):
        return
    if not await _require_lang(interaction, bot.data):
        return
    if ALLOWED_GUILD_ID and interaction.guild_id != ALLOWED_GUILD_ID:
        try:
            await interaction.response.send_message(
                " `/vm` only works in the main server.", ephemeral=True
            )
        except (discord.NotFound, discord.InteractionResponded):
            pass
        return
    try:
        await interaction.response.defer()
    except (discord.NotFound, discord.InteractionResponded):
        return
    user_id = interaction.user.id
    vm = await bot.data.get_vm(user_id, kind="windows")
    login = "?"
    if bot.gh and ADMIN_GITHUB_TOKEN:
        try:
            login = bot.gh_user_cache.get("_admin_") or await bot.gh.get_user_login(ADMIN_GITHUB_TOKEN)
            if not bot.gh_user_cache.get("_admin_"):
                bot.gh_user_cache["_admin_"] = login
        except GitHubAPIError:
            login = bot.gh_user_cache.get(user_id, "?")
    all_vms = await bot.data.all_vms()
    ga = _count_active_windows(all_vms)
    lang = await _get_lang(user_id, bot.data)
    embed = build_status_embed(vm, login, global_active=ga, lang=lang)
    view = VMPanelView(bot, lang=lang)
    try:
        await interaction.followup.send(embed=embed, view=view)
    except discord.HTTPException as e:
        log.error("Failed to send /vm panel: %s", e)

@bot.tree.command(name="status", description="Xem trạng thái VM của bạn")
async def cmd_status(interaction: discord.Interaction) -> None:
    if not await _require_lang(interaction, bot.data):
        return
    try:
        await interaction.response.defer()
    except (discord.NotFound, discord.InteractionResponded):
        return
    user_id = interaction.user.id
    vm = await bot.data.get_vm(user_id)
    login = "?"
    if bot.gh and ADMIN_GITHUB_TOKEN:
        try:
            login = bot.gh_user_cache.get("_admin_") or await bot.gh.get_user_login(ADMIN_GITHUB_TOKEN)
            if not bot.gh_user_cache.get("_admin_"):
                bot.gh_user_cache["_admin_"] = login
        except GitHubAPIError:
            login = bot.gh_user_cache.get(user_id, "?")
    all_vms = await bot.data.all_vms()
    ga = _count_active_windows(all_vms)
    lang = await _get_lang(user_id, bot.data)
    if vm and vm.kind == "vps":
        embed = build_vps_status_embed(vm, login, global_active=ga, lang=lang)
    else:
        embed = build_status_embed(vm, login, global_active=ga, lang=lang)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="vps", description="AI STV — Tạo Ubuntu VPS tạm thời qua sshx (1-6 giờ)")
async def cmd_vps(interaction: discord.Interaction) -> None:
    if not await _guild_only(interaction):
        return
    if not await _require_lang(interaction, bot.data):
        return
    if ALLOWED_GUILD_ID and interaction.guild_id != ALLOWED_GUILD_ID:
        try:
            await interaction.response.send_message(
                " `/vps` only works in the main server.", ephemeral=True
            )
        except (discord.NotFound, discord.InteractionResponded):
            pass
        return
    try:
        await interaction.response.defer()
    except (discord.NotFound, discord.InteractionResponded):
        return
    user_id = interaction.user.id
    vm = await bot.data.get_vm(user_id, kind="vps")
    login = "?"
    if bot.gh and ADMIN_GITHUB_TOKEN:
        try:
            login = bot.gh_user_cache.get("_admin_") or await bot.gh.get_user_login(ADMIN_GITHUB_TOKEN)
            if not bot.gh_user_cache.get("_admin_"):
                bot.gh_user_cache["_admin_"] = login
        except GitHubAPIError:
            login = bot.gh_user_cache.get(user_id, "?")
    all_vms = await bot.data.all_vms()
    ga = _count_active_windows(all_vms)
    lang = await _get_lang(user_id, bot.data)
    embed = build_vps_status_embed(vm, login, global_active=ga, lang=lang)
    try:
        await interaction.followup.send(embed=embed, view=VPSPanelView(bot, lang=lang))
    except discord.HTTPException as e:
        log.error("Failed to send /vps panel: %s", e)

@bot.tree.command(name="lang", description="Set your language / Chọn ngôn ngữ")
@app_commands.describe(language="en = English, vi = Tiếng Việt")
@app_commands.choices(language=[
    app_commands.Choice(name="English", value="en"),
    app_commands.Choice(name="Tiếng Việt", value="vi"),
])
async def cmd_lang(interaction: discord.Interaction, language: str) -> None:
    await bot.data.set_user_lang(interaction.user.id, language)
    _lang_cache[interaction.user.id] = language
    if language == "en":
        embed = discord.Embed(
            title=" Language Set",
            description="Your language has been set to **English**.",
            color=0x2ECC71,
        )
    else:
        embed = discord.Embed(
            title=" Đã chọn ngôn ngữ",
            description="Ngôn ngữ của bạn đã được đặt thành **Tiếng Việt**.",
            color=0x2ECC71,
        )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="id", description="Xem Discord ID của bạn")
async def cmd_id(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(f" Your Discord ID: **`{interaction.user.id}`**")

@bot.tree.command(name="ping", description="Kiểm tra độ trễ của bot")
async def cmd_ping(interaction: discord.Interaction) -> None:
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f" Pong! Latency: `{latency}ms`")

@bot.tree.command(name="welcome", description="Chào mừng & hướng dẫn sử dụng bot")
async def cmd_welcome(interaction: discord.Interaction) -> None:
    lang = await _get_lang(interaction.user.id, bot.data)
    embed = build_welcome_embed(interaction.guild, lang)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="userinfo", description="Thông tin chi tiết về user")
@app_commands.describe(user="User cần xem (để trống = bạn)")
async def cmd_userinfo(interaction: discord.Interaction, user: Optional[discord.User] = None) -> None:
    await interaction.response.defer()
    target = user or interaction.user
    embed = discord.Embed(title=f" {target.display_name}", color=target.accent_color or 0x5865F2, timestamp=datetime.now(VN_TZ))
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="ID", value=f"`{target.id}`", inline=True)
    embed.add_field(name="Name", value=f"**{target}**", inline=True)
    embed.add_field(name="Bot", value=" Yes" if target.bot else " No", inline=True)
    embed.add_field(name="Created", value=target.created_at.strftime("%d/%m/%Y %H:%M"), inline=True)
    if interaction.guild:
        member = interaction.guild.get_member(target.id)
        if member:
            embed.add_field(name="Joined", value=member.joined_at.strftime("%d/%m/%Y %H:%M") if member.joined_at else "-", inline=True)
            roles = [r.mention for r in member.roles if r != interaction.guild.default_role][:10]
            if roles:
                embed.add_field(name=f"Roles ({len(roles)})", value=" ".join(roles), inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="rules", description="Xem luật máy ảo AI STV")
async def cmd_rules(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title=" L U Ậ T   M Á Y   Ả O — AI STV",
        description=(
            "### 1. HÀNH VI CẤM (BAN)\n"
            "- **Cấm Đào Coin:** Không treo công cụ đào tiền ảo.\n"
            "- **Cấm Tấn Công Mạng:** Không DDOS, quét cổng, phát tán mã độc.\n"
            "- **Cấm Spam:** Không chạy bot spam, tạo tài khoản hàng loạt.\n\n"
            "### 2. QUY ĐỊNH TÀI NGUYÊN\n"
            "- Mỗi người chỉ được **1 máy** tại 1 thời điểm.\n"
            "- Tối đa **6 giờ/phiên**.\n\n"
            "### 3. XỬ PHẠT\n"
            "- Vi phạm nhẹ: +1 strike. Đủ 5 strike => ban.\n"
            "- Vi phạm nặng (Đào coin, DDOS): Ban ngay lập tức."
        ),
        color=0xE74C3C,
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="admin", description="[Admin] Dashboard thống kê hệ thống")
async def cmd_admin(interaction: discord.Interaction) -> None:
    if not await _guild_only(interaction):
        return
    if not (OWNER_ID and interaction.user.id == OWNER_ID):
        await interaction.response.send_message(" Owner only.", ephemeral=True)
        return
    await interaction.response.defer()
    stats = await bot.data.stats_summary()
    embed = discord.Embed(title=f" {BRAND_NAME} — Admin Dashboard", color=0x5865F2, timestamp=datetime.now(VN_TZ))
    embed.add_field(name="Active VMs", value=f"**{stats['active_vms']}**", inline=True)
    embed.add_field(name="Running Instances", value=f"**{stats['running_instances']}**", inline=True)
    embed.add_field(name="Total Records", value=f"**{stats['total_vm_records']}**", inline=True)
    embed.add_field(name="Registered Users", value=f"**{stats['registered_users']}**", inline=True)
    embed.add_field(name="Blacklisted", value=f"**{stats['blacklisted']}**", inline=True)
    await interaction.followup.send(embed=embed)

# ── Main ─────────────────────────────────────────────────────────────

def _start_health_server() -> None:
    """Serve GET /, /health and POST /api/vm-ready on $PORT (Railway)."""
    try:
        import threading
        from aiohttp import web

        async def _handler(_request: web.Request) -> web.Response:
            return web.Response(text="ok", content_type="text/plain")

        async def _vm_ready_handler(request: web.Request) -> web.Response:
            if not BOT_WEBHOOK_SECRET:
                return web.Response(status=503, text="webhook not configured")
            if request.headers.get("X-Bot-Secret", "") != BOT_WEBHOOK_SECRET:
                return web.Response(status=403, text="forbidden")
            try:
                data = await request.json()
            except Exception:
                return web.Response(status=400, text="bad json")
            if not isinstance(data, dict):
                return web.Response(status=400, text="bad json")
            failed_status = str(data.get("status") or "").lower() in ("failed", "failure", "error", "cancelled", "canceled")
            if not data.get("sshx_url") and not data.get("password") and not failed_status:
                return web.Response(status=200, text="ignored")
            try:
                fut = asyncio.run_coroutine_threadsafe(bot._process_webhook_ready(data), bot.loop)
                await asyncio.wait_for(asyncio.wrap_future(fut), timeout=25)
            except Exception as err:
                log.warning("Webhook processing error: %s", _safe_error_text(err))
                return web.Response(status=500, text="error")
            return web.Response(status=200, text="ok")

        async def _serve() -> None:
            app = web.Application()
            app.router.add_get("/", _handler)
            app.router.add_get("/health", _handler)
            app.router.add_post(BOT_WEBHOOK_PATH, _vm_ready_handler)
            runner = web.AppRunner(app)
            await runner.setup()
            port = int(os.getenv("PORT", "8080"))
            site = web.TCPSite(runner, "0.0.0.0", port)
            await site.start()
            log.info("Health server started on port %s (webhook: %s)", port, BOT_WEBHOOK_PATH)

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_serve())
            except Exception as e:
                log.warning("Health server error: %s", e)
            loop.run_forever()

        threading.Thread(target=_run, daemon=True).start()
    except Exception as e:
        log.warning("Health server not started: %s", e)

def main() -> None:
    _start_health_server()
    if not BOT_TOKEN:
        log.error("Missing bot token. Set DISCORD_BOT_TOKEN env or bot_config.json")
        sys.exit(1)
    try:
        bot.run(BOT_TOKEN, log_handler=None)
    except discord.LoginFailure:
        log.error("Invalid Discord token (401).")
        sys.exit(1)

if __name__ == "__main__":
    main()