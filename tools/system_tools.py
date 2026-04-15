"""
RPi system tools — let the agent inspect and control the host Raspberry Pi.
"""

import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)

_LOG_FILE = os.getenv("LOG_FILE", "agent.log")

# Input schemas for tools that take parameters
GET_AGENT_LOGS_SCHEMA = {
    "type": "object",
    "properties": {
        "n_lines": {
            "type": "integer",
            "description": "Number of log lines to return (default: 50)",
        },
    },
    "required": [],
}

SHUTDOWN_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["reboot", "poweroff"],
            "description": "'reboot' to restart the Raspberry Pi, 'poweroff' to shut it down",
        },
    },
    "required": ["action"],
}


async def get_system_status() -> dict:
    """
    Return current Raspberry Pi system metrics: CPU usage, RAM, disk space,
    CPU temperature, and uptime. Use this when the user asks about system
    performance, free storage, memory, temperature, or how long the device
    has been running.
    """
    cpu_percent = psutil.cpu_percent(interval=0.5)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    boot_ts = psutil.boot_time()
    uptime_seconds = int(datetime.now(tz=timezone.utc).timestamp() - boot_ts)

    hours, remainder = divmod(uptime_seconds, 3600)
    minutes = remainder // 60
    uptime_str = f"{hours}h {minutes}m"

    temp_celsius: float | None = None
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
        temp_celsius = round(int(raw) / 1000, 1)
    except Exception:
        pass

    return {
        "cpu_percent": cpu_percent,
        "ram_used_mb": round(ram.used / 1024 / 1024),
        "ram_total_mb": round(ram.total / 1024 / 1024),
        "ram_percent": ram.percent,
        "disk_used_gb": round(disk.used / 1024 / 1024 / 1024, 1),
        "disk_total_gb": round(disk.total / 1024 / 1024 / 1024, 1),
        "disk_percent": disk.percent,
        "cpu_temp_celsius": temp_celsius,
        "uptime": uptime_str,
    }


async def get_agent_logs(n_lines: int = 50) -> dict:
    """
    Return the last N lines from the agent log file. Use this when the user
    asks to see recent logs, errors, or what the agent has been doing.
    """
    try:
        result = subprocess.run(
            ["tail", "-n", str(n_lines), _LOG_FILE],
            capture_output=True,
            text=True,
            timeout=5,
        )
        lines = result.stdout.strip()
        return {"lines": lines, "file": _LOG_FILE}
    except Exception as e:
        logger.exception("Failed to read log file")
        return {"error": str(e)}


async def restart_agent_service() -> dict:
    """
    Restart the agent systemd service (agent.service). Use this when the user
    asks to restart the agent or when a restart is needed to apply changes.
    Note: restarting terminates this process — no response will follow.
    """
    try:
        logger.info("Restarting agent.service as requested")
        subprocess.Popen(
            ["systemctl", "restart", "agent"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"status": "restart_initiated"}
    except Exception as e:
        logger.exception("Failed to restart agent service")
        return {"error": str(e)}


async def shutdown_raspberry_pi(action: str) -> dict:
    """
    Reboot or power off the Raspberry Pi. Use this only when the user explicitly
    asks to restart or shut down the device. action must be 'reboot' or 'poweroff'.
    """
    if action not in ("reboot", "poweroff"):
        return {"error": f"Unknown action '{action}'. Use 'reboot' or 'poweroff'."}

    command = ["sudo", "/sbin/reboot"] if action == "reboot" else ["sudo", "/sbin/poweroff"]
    logger.info("Executing system %s as requested", action)
    try:
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"status": f"{action}_initiated"}
    except Exception as e:
        logger.exception("Failed to %s", action)
        return {"error": str(e)}
