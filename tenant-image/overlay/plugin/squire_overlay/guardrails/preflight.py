"""Resource preflight / admission (acceptance item 4).

Before admitting a new interactive-browser launch, read host pressure and refuse with a
structured reason instead of adding another Chromium to a host already at load 22 on 2 cores.
Readings are a plain dataclass so tests inject values; ``read_host`` is the only live reader.

Isolation of browser workers (CPU/memory away from the messaging gateway) needs systemd and is
therefore shipped as an ARTIFACT (``render_systemd_dropin``) for the operator to install at
deploy time — this module never writes outside HERMES_HOME.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

LOAD_PER_CPU_LIMIT = 3.0      # load1 > 3× cores → refuse
MIN_AVAIL_MB = 512
MAX_SWAP_USED_PCT = 75
DEFAULT_MAX_CHROMIUM = 1
STALE_DAEMON_S = 6 * 3600   # a browser_harness daemon older than this with no owner is an orphan


@dataclass
class Readings:
    load1: float
    ncpu: int
    avail_mb: int
    swap_used_pct: float
    chromium_procs: int
    harness_daemons: int = 0
    stale_harness_daemons: int = 0

    def as_dict(self) -> dict:
        return {"load1": round(self.load1, 2), "ncpu": self.ncpu, "avail_mb": self.avail_mb,
                "swap_used_pct": round(self.swap_used_pct, 1), "chromium_procs": self.chromium_procs,
                "harness_daemons": self.harness_daemons, "stale_harness_daemons": self.stale_harness_daemons}


@dataclass
class Admission:
    allowed: bool
    reason: str
    message: str = ""
    metrics: dict = field(default_factory=dict)


def container_available_mb(root: Path = Path("/sys/fs/cgroup")) -> Optional[int]:
    """Remaining cgroup-v2 memory, rather than the host's free RAM."""
    try:
        maximum = (root / "memory.max").read_text().strip()
        if maximum == "max": return None
        return max(0, (int(maximum) - int((root / "memory.current").read_text())) // (1024 * 1024))
    except (OSError, ValueError): return None


def read_host() -> Readings:
    ncpu = os.cpu_count() or 1
    try:
        load1 = os.getloadavg()[0]
    except (OSError, AttributeError):
        load1 = 0.0
    avail_mb, swap_pct, chromium, daemons, stale = 0, 0.0, 0, 0, 0
    try:
        import psutil
        vm = psutil.virtual_memory()
        avail_mb = int(vm.available / (1024 * 1024))
        sm = psutil.swap_memory()
        swap_pct = float(sm.percent) if sm.total else 0.0
        now = time.time()
        for p in psutil.process_iter(["name", "cmdline", "create_time"]):
            try:
                name = (p.info.get("name") or "").lower()
                cmd = " ".join(p.info.get("cmdline") or []).lower()
                if "browser_harness.daemon" in cmd:
                    daemons += 1
                    if now - float(p.info.get("create_time") or now) > STALE_DAEMON_S:
                        stale += 1
                    continue
                # Browser MAIN processes only: skip --type= helpers (zygote/renderer/gpu/utility)
                # and crashpad handlers, which are per-browser side-cars not extra browsers.
                if ("chrom" in name or "chrom" in cmd) and "--type=" not in cmd and "crashpad" not in name \
                        and "crashpad_handler" not in cmd:
                    chromium += 1
            except Exception:
                continue
    except Exception:
        pass
    remaining = container_available_mb()
    if remaining is not None:
        # Zero must remain a refusal: admit's historical 0 means "unknown".
        avail_mb = max(1, min(avail_mb, remaining) if avail_mb else remaining)
    return Readings(load1=load1, ncpu=ncpu, avail_mb=avail_mb, swap_used_pct=swap_pct, chromium_procs=chromium,
                    harness_daemons=daemons, stale_harness_daemons=stale)


def admit(r: Readings, *, max_chromium: int = DEFAULT_MAX_CHROMIUM) -> Admission:
    m = r.as_dict()
    if r.load1 > LOAD_PER_CPU_LIMIT * max(1, r.ncpu):
        return Admission(False, "host_pressure_load",
                         f"Host load {r.load1:.1f} on {r.ncpu} cores is too high to start another browser; "
                         f"retry when load drops below {LOAD_PER_CPU_LIMIT * max(1, r.ncpu):.0f}.", m)
    if r.avail_mb and r.avail_mb < MIN_AVAIL_MB:
        return Admission(False, "host_pressure_memory",
                         f"Only {r.avail_mb} MB available (<{MIN_AVAIL_MB}); not starting a browser. Retry later.", m)
    if r.swap_used_pct > MAX_SWAP_USED_PCT:
        return Admission(False, "host_pressure_swap",
                         f"Swap {r.swap_used_pct:.0f}% used (>{MAX_SWAP_USED_PCT}%); not starting a browser. Retry later.", m)
    if r.chromium_procs > max_chromium:
        return Admission(False, "host_pressure_chromium_count",
                         f"{r.chromium_procs} browser processes already running (cap {max_chromium}); retry when one exits.", m)
    return Admission(True, "ok", "", m)


# ---- deploy artifact (not applied here) ----------------------------------------------------

DROPIN_NAME = "50-latency-guardrails.conf"


def render_systemd_dropin(*, cpu_weight: int = 100, memory_high: str = "900M", memory_max: str = "1200M") -> str:
    """systemd drop-in for a transient scope that browser workers can be launched under
    (``systemd-run --user --scope --slice=hermes-browser.slice``). Lower CPUWeight than the gateway
    (default 100 vs app.slice default) keeps messaging responsive under contention."""
    return (
        "# Installed by the operator at deploy time (deploy/handoff). NOT applied automatically.\n"
        "# Place at ~/.config/systemd/user/hermes-browser.slice.d/" + DROPIN_NAME + "\n"
        "[Slice]\n"
        f"CPUWeight={cpu_weight}\n"
        f"MemoryHigh={memory_high}\n"
        f"MemoryMax={memory_max}\n"
        "MemorySwapMax=256M\n"
        "[Scope]\n"
        "OOMPolicy=kill\n"
    )


def dropin_install_paths() -> list[Path]:
    return [Path.home() / ".config/systemd/user/hermes-browser.slice.d" / DROPIN_NAME,
            Path("/etc/systemd/system/hermes-browser.slice.d") / DROPIN_NAME]
