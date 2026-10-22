"""
Keeps one authenticated SSH transport open per server and runs a small
read-only probe over it on a timer. Login (password + OTP, if your cluster
asks for one) happens once per session — after that, every poll reuses the
same transport as a new channel, so you are never re-prompted.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field
from typing import Optional

import paramiko

CONNECT_TIMEOUT = 15  # seconds — fail fast on an unreachable host instead of hanging

# Gathers cores, live per-core AND aggregate CPU% (two /proc/stat samples 1s
# apart), memory, GPU stats (if nvidia-smi is present), and a per-process
# snapshot (user, %cpu, %mem, RSS, elapsed seconds) in a single round trip.
# Everything here is read-only and either a standard coreutils command or,
# for GPUs, whatever the vendor tool already installed on that box reports
# — nothing is installed on the remote side.
PROBE_SCRIPT = r"""
CORES=$(nproc)
S1="$(grep '^cpu' /proc/stat)"
sleep 1
S2="$(grep '^cpu' /proc/stat)"
echo "CORES:$CORES"
awk '
  FNR==NR { idle1[FNR]=$5+$6; total1[FNR]=$2+$3+$4+$5+$6+$7+$8; name[FNR]=$1; next }
  {
    idle2=$5+$6; total2=$2+$3+$4+$5+$6+$7+$8;
    dt=total2-total1[FNR]; di=idle2-idle1[FNR];
    pct=(dt>0)?(dt-di)/dt*100:0;
    printf "CORE:%s:%.1f\n", name[FNR], pct;
  }
' <(printf '%s\n' "$S1") <(printf '%s\n' "$S2")
read MT MU MF MS MB MA <<< "$(free -m | awk '/^Mem:/{print $2,$3,$4,$5,$6,$7}')"
echo "MEM_TOTAL:$MT"
echo "MEM_USED:$MU"
echo "MEM_AVAIL:$MA"
if command -v nvidia-smi >/dev/null 2>&1; then
  echo "---GPUS---"
  nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu \
    --format=csv,noheader,nounits 2>/dev/null
  PROC_MEM="$(nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits 2>/dev/null)"
  if [ -n "$PROC_MEM" ]; then
    echo "---GPU_PROC_MEM---"
    echo "$PROC_MEM"
    PIDS=$(echo "$PROC_MEM" | awk -F',' '{gsub(/ /,"",$1); print $1}' | paste -sd, -)
    if [ -n "$PIDS" ]; then
      echo "---GPU_PROC_USER---"
      ps -o pid=,user= -p "$PIDS" 2>/dev/null
    fi
  fi
fi
echo "---USERS---"
ps -eo user:32,pcpu,pmem,rss,etimes --no-headers
""".strip()

PROBE_TIMEOUT = 12  # seconds; script itself takes ~1s (the sleep) plus round trip

# On-demand only (not part of the periodic poll) — df on a network home/scratch
# mount can be slower than the other read-only checks, so it's fetched just
# when the "Disk usage" button is clicked. -P keeps each entry on one line
# (plain "df -h" wraps long device names onto a second line) and the -x flags
# drop virtual filesystems that aren't real storage.
DISK_SCRIPT = r"""
df -kP -x tmpfs -x devtmpfs -x overlay -x squashfs -x proc -x sysfs \
  -x cgroup -x cgroup2 -x tracefs -x debugfs -x rpc_pipefs -x autofs \
  -x fuse.gvfsd-fuse -x efivarfs 2>/dev/null | tail -n +2
""".strip()

DISK_TIMEOUT = 12  # seconds — a stuck network mount shouldn't hang the UI


def _looks_like(prompt: str, *words: str) -> bool:
    p = prompt.lower()
    return any(w in p for w in words)


@dataclass
class ServerConnection:
    name: str
    host: str
    port: int
    username: str
    transport: Optional[paramiko.Transport] = field(default=None, repr=False)
    connected: bool = False
    last_error: Optional[str] = None
    last_stats: Optional[dict] = None

    # -- auth -----------------------------------------------------------
    def _interactive_handler(self, password: str, otp: str):
        def handler(title, instructions, prompt_list):
            answers = []
            for prompt_text, _echo in prompt_list:
                if _looks_like(prompt_text, "verification", "otp", "token", "code", "passcode"):
                    answers.append(otp or "")
                elif _looks_like(prompt_text, "password"):
                    answers.append(password or "")
                else:
                    answers.append("")
            return answers
        return handler

    def connect_sync(self, password: str, otp: str = "") -> None:
        """Runs in a worker thread. Authenticates once; raises on failure."""
        # Open the TCP socket ourselves with an explicit timeout — paramiko's own
        # default can hang far longer than that against an unreachable host.
        sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT)
        transport = paramiko.Transport(sock)
        try:
            transport.start_client(timeout=CONNECT_TIMEOUT)
            try:
                transport.auth_interactive(self.username, self._interactive_handler(password, otp))
            except paramiko.SSHException:
                # Cluster doesn't offer keyboard-interactive (no OTP prompt) — plain password.
                transport.auth_password(self.username, password)
            if not transport.is_authenticated():
                raise paramiko.AuthenticationException("Authentication was not completed")
        except Exception:
            transport.close()
            raise
        transport.set_keepalive(30)
        self.transport = transport
        self.connected = True
        self.last_error = None

    def disconnect_sync(self) -> None:
        if self.transport:
            try:
                self.transport.close()
            except Exception:
                pass
        self.transport = None
        self.connected = False

    # -- polling ----------------------------------------------------------
    def _exec_sync(self, command: str, timeout: int) -> str:
        """Runs in a worker thread over the existing transport (no re-auth)."""
        if not self.transport or not self.transport.is_active():
            raise RuntimeError("not connected")
        chan = self.transport.open_session(timeout=timeout)
        chan.settimeout(timeout)
        chan.exec_command(command)
        out = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if chan.recv_ready():
                out += chan.recv(4096)
                continue
            if chan.exit_status_ready():
                while chan.recv_ready():
                    out += chan.recv(4096)
                break
            time.sleep(0.05)
        chan.close()
        return out.decode("utf-8", errors="ignore")

    def run_probe_sync(self) -> dict:
        return parse_probe_output(self._exec_sync(PROBE_SCRIPT, PROBE_TIMEOUT))

    def run_disk_probe_sync(self) -> list[dict]:
        return parse_disk_output(self._exec_sync(DISK_SCRIPT, DISK_TIMEOUT))


def parse_probe_output(out: str) -> dict:
    stats = {
        "cores": 0,
        "cpu_pct": 0.0,
        "per_core": [],
        "mem_total": 0,
        "mem_used": 0,
        "mem_avail": 0,
        "users": {},
        "gpus": [],
    }
    core_pcts: dict[str, float] = {}
    gpu_proc_mem: dict[str, int] = {}   # pid -> MB
    gpu_proc_user: dict[str, str] = {}  # pid -> username
    section = "top"

    for raw_line in out.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line == "---USERS---":
            section = "users"
            continue
        if line == "---GPUS---":
            section = "gpus"
            continue
        if line == "---GPU_PROC_MEM---":
            section = "gpu_proc_mem"
            continue
        if line == "---GPU_PROC_USER---":
            section = "gpu_proc_user"
            continue

        if section == "top":
            if line.startswith("CORE:"):
                _, core_name, val = line.split(":", 2)
                try:
                    core_pcts[core_name] = float(val)
                except ValueError:
                    pass
                continue
            if ":" in line:
                key, _, val = line.partition(":")
                try:
                    if key == "CORES":
                        stats["cores"] = int(val)
                    elif key == "MEM_TOTAL":
                        stats["mem_total"] = int(val)
                    elif key == "MEM_USED":
                        stats["mem_used"] = int(val)
                    elif key == "MEM_AVAIL":
                        stats["mem_avail"] = int(val)
                except ValueError:
                    pass
            continue

        if section == "gpus":
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 6:
                continue
            try:
                stats["gpus"].append({
                    "index": int(parts[0]),
                    "name": parts[1],
                    "util_pct": float(parts[2]),
                    "mem_used_mb": int(float(parts[3])),
                    "mem_total_mb": int(float(parts[4])),
                    "temp_c": float(parts[5]),
                })
            except ValueError:
                pass
            continue

        if section == "gpu_proc_mem":
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2:
                continue
            try:
                gpu_proc_mem[parts[0]] = int(float(parts[1]))
            except ValueError:
                pass
            continue

        if section == "gpu_proc_user":
            parts = line.split()
            if len(parts) < 2:
                continue
            gpu_proc_user[parts[0]] = parts[1]
            continue

        # section == "users"
        parts = line.split()
        if len(parts) < 5:
            continue
        user, pcpu, pmem, rss_kb, etimes = parts[0], parts[1], parts[2], parts[3], parts[4]
        try:
            pcpu = float(pcpu)
            pmem = float(pmem)
            rss_kb = int(rss_kb)
            etimes = int(etimes)
        except ValueError:
            continue
        u = stats["users"].setdefault(
            user, {"cpu_pct": 0.0, "mem_pct": 0.0, "rss_mb": 0, "max_etimes": 0, "procs": 0, "gpu_mb": 0}
        )
        u["cpu_pct"] += pcpu
        u["mem_pct"] += pmem
        u["rss_mb"] += rss_kb // 1024
        u["max_etimes"] = max(u["max_etimes"], etimes)
        u["procs"] += 1

    # Fold per-process GPU memory into the same per-user rows the CPU/RAM
    # table already uses, joining on PID (nvidia-smi doesn't report usernames).
    for pid, mb in gpu_proc_mem.items():
        user = gpu_proc_user.get(pid)
        if not user:
            continue
        u = stats["users"].setdefault(
            user, {"cpu_pct": 0.0, "mem_pct": 0.0, "rss_mb": 0, "max_etimes": 0, "procs": 0, "gpu_mb": 0}
        )
        u["gpu_mb"] = u.get("gpu_mb", 0) + mb
    for u in stats["users"].values():
        u.setdefault("gpu_mb", 0)

    stats["cpu_pct"] = core_pcts.pop("cpu", 0.0)  # aggregate line, reported separately from /proc/stat

    def _core_index(name: str) -> int:
        try:
            return int(name[3:])  # "cpu0" -> 0, "cpu17" -> 17
        except ValueError:
            return 0

    stats["per_core"] = [core_pcts[k] for k in sorted(core_pcts, key=_core_index)]
    stats["gpus"].sort(key=lambda g: g["index"])
    return stats


def parse_disk_output(out: str) -> list[dict]:
    """Parses `df -kP` output (sizes in 1024-byte blocks) into MB-based rows."""
    disks: list[dict] = []
    for raw_line in out.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 6:
            continue
        filesystem = parts[0]
        mount = " ".join(parts[5:])  # defensive: mount points rarely but can contain spaces
        try:
            total_kb = int(parts[1])
            used_kb = int(parts[2])
            avail_kb = int(parts[3])
        except ValueError:
            continue
        if total_kb <= 0:
            continue
        pct_str = parts[4].rstrip("%")
        try:
            pct = float(pct_str)
        except ValueError:
            pct = (used_kb / total_kb * 100) if total_kb else 0.0
        disks.append({
            "filesystem": filesystem,
            "mount": mount,
            "total_mb": total_kb // 1024,
            "used_mb": used_kb // 1024,
            "avail_mb": avail_kb // 1024,
            "pct": pct,
        })
    disks.sort(key=lambda d: d["mount"])
    return disks
