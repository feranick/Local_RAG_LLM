#!/usr/bin/env python3
"""
platform_probe.py — work out what this machine can actually run, once, and write
it where every other script can read it.

The stack was first built for an NVIDIA DGX Spark, where "how much model fits" was
a constant (~110 GB of unified memory). On a general workstation it isn't, so that
number is measured instead of assumed, and everything else keys off it:

  * setup_local_rag.sh   offers only models that fit
  * manage_models.py     flags tags that are too big for THIS machine
  * llm_stack_healthcheck.sh reports what was detected

What it reads (Linux + NVIDIA):

  arch / kernel   uname
  CPU             /proc/cpuinfo  (model name, core count)
  RAM             /proc/meminfo  (MemTotal)
  GPU             nvidia-smi --query-gpu=name,memory.total,driver_version
  docker GPU      `docker info` runtimes (no image pull)

The derived budget, USABLE_MEM_GB:

  unified memory (Spark / Grace, VRAM ~= RAM)   85% of RAM
  discrete GPU                                  VRAM - 1 GB headroom
  no GPU (CPU inference)                         60% of RAM, and it will be slow

Usage:
    python3 platform_probe.py                 # human-readable summary
    python3 platform_probe.py --json          # machine-readable
    python3 platform_probe.py --shell         # KEY=value, for `eval`/`source`
    python3 platform_probe.py --write         # save hardware.conf (see below)
    python3 platform_probe.py --write PATH    # ...somewhere specific

The written file is shell-sourceable *and* trivially parsed by Python, so bash and
Python agree on one set of numbers. Edit it freely — hand-edited values win, which
is the point: detection can be wrong, and you should be able to overrule it.

Search order when other tools look for it:
    $RAG_HW_CONF
    ./hardware.conf
    <this script's directory>/hardware.conf
    ~/.config/local_rag/hardware.conf
"""

__version__ = "2026.08.03.2"

import os
import re
import sys
import json
import time
import shutil
import pathlib
import platform
import subprocess

CONF_NAME = "hardware.conf"
DEFAULT_CONF = pathlib.Path.home() / ".config" / "local_rag" / CONF_NAME

# Fractions used to turn installed memory into a model budget. Deliberately
# conservative: the OS, the containers and the KV cache all need room too.
UNIFIED_FRACTION = 0.85
CPU_FRACTION = 0.60
DISCRETE_HEADROOM_GB = 1.0

# Used only when nothing can be detected at all (no /proc, no nvidia-smi).
FALLBACK_USABLE_GB = 8.0


def _run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return ""


def cpu_info():
    name, cores = "", 0
    try:
        txt = pathlib.Path("/proc/cpuinfo").read_text(errors="ignore")
        m = re.search(r"^model name\s*:\s*(.+)$", txt, re.M)
        if not m:      # ARM parts often report differently
            m = re.search(r"^(?:Model|CPU part|Hardware)\s*:\s*(.+)$", txt, re.M)
        name = m.group(1).strip() if m else platform.processor()
        cores = len(re.findall(r"^processor\s*:", txt, re.M))
    except OSError:
        name = platform.processor()
    return name or "unknown", cores or (os.cpu_count() or 0)


def ram_gb():
    try:
        txt = pathlib.Path("/proc/meminfo").read_text(errors="ignore")
        m = re.search(r"^MemTotal:\s+(\d+)\s+kB", txt, re.M)
        if m:
            return int(m.group(1)) / (1024 * 1024)
    except OSError:
        pass
    return 0.0


def gpus():
    """[{name, vram_gb, driver}] from nvidia-smi; empty list if none/absent."""
    if not shutil.which("nvidia-smi"):
        return []
    out = _run(["nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits"])
    found = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            vram = float(parts[1]) / 1024.0      # nvidia-smi reports MiB
        except ValueError:
            vram = 0.0
        found.append({"name": parts[0], "vram_gb": round(vram, 1),
                      "driver": parts[2] if len(parts) > 2 else ""})
    return found


def docker_gpu():
    """(docker_present, gpu_runtime_available) — no image is pulled."""
    if not shutil.which("docker"):
        return False, False
    info = _run(["docker", "info"]) or _run(["sudo", "docker", "info"])
    if not info:
        return False, False
    low = info.lower()
    return True, ("nvidia" in low or "cdi" in low)


def probe():
    """Everything we can learn, plus the derived budget."""
    cpu, cores = cpu_info()
    ram = ram_gb()
    gs = gpus()
    vram = sum(g["vram_gb"] for g in gs)
    docker_present, docker_gpu_ok = docker_gpu()

    # Unified memory (DGX Spark GB10, Grace, Jetson/Orin): the GPU reports
    # essentially the same pool as the system, so treating VRAM and RAM as separate
    # budgets would double-count. Two independent signals, either is enough:
    #   * the part is a known unified-memory design
    #   * VRAM and RAM are within a quarter of each other
    # The ratio must be BOUNDED ON BOTH SIDES: a 24 GB card in a 16 GB workstation
    # has VRAM > RAM and is still very much discrete.
    ratio = (vram / ram) if ram > 0 else 0.0
    name_hint = any(k in (";".join(g["name"] for g in gs)).upper()
                    for k in ("GB10", "GRACE", "ORIN", "THOR", "JETSON", "IGP"))
    tegra = pathlib.Path("/etc/nv_tegra_release").exists()
    unified = bool(gs) and (name_hint or tegra or (0.8 <= ratio <= 1.25))

    if unified:
        kind, usable = "unified", ram * UNIFIED_FRACTION
    elif gs:
        kind, usable = "discrete", max(0.0, vram - DISCRETE_HEADROOM_GB)
    elif ram > 0:
        kind, usable = "cpu-only", ram * CPU_FRACTION
    else:
        kind, usable = "unknown", FALLBACK_USABLE_GB

    env = os.environ.get("RAG_USABLE_MEM_GB")
    overridden = False
    if env:
        try:
            usable, overridden = float(env), True
        except ValueError:
            pass

    return {
        "probe_version": __version__,
        "probed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "cpu": cpu,
        "cores": cores,
        "ram_gb": round(ram, 1),
        "gpu_count": len(gs),
        "gpu_names": "; ".join(g["name"] for g in gs),
        "vram_gb": round(vram, 1),
        "driver": gs[0]["driver"] if gs else "",
        "memory_kind": kind,
        "usable_mem_gb": round(usable, 1),
        "usable_overridden": overridden,
        "docker": docker_present,
        "docker_gpu": docker_gpu_ok,
        "fingerprint": fingerprint(platform.machine(), ram, gs),
        "gpus": gs,
    }


def fingerprint(arch, ram, gs):
    """Cheap identity of the hardware, to notice a stale hardware.conf."""
    return "|".join([arch, f"{round(ram)}G", f"{len(gs)}x",
                     (gs[0]["name"] if gs else "nogpu")])


# ----------------------------- the config file ---------------------------
def conf_candidates():
    here = pathlib.Path(__file__).resolve().parent
    out = []
    if os.environ.get("RAG_HW_CONF"):
        out.append(pathlib.Path(os.environ["RAG_HW_CONF"]).expanduser())
    out += [pathlib.Path.cwd() / CONF_NAME, here / CONF_NAME,
            here.parent / CONF_NAME, DEFAULT_CONF]
    return out


def find_conf():
    for c in conf_candidates():
        if c.is_file():
            return c
    return None


def load_conf(path=None):
    """Parse hardware.conf into a dict of lower-case keys, or {} if absent."""
    p = pathlib.Path(path).expanduser() if path else find_conf()
    if not p or not p.is_file():
        return {}
    out = {}
    for raw in p.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().upper()
        if k.startswith("RAG_"):
            k = k[4:]
        out[k.lower()] = v.split("#")[0].strip().strip('"').strip("'")
    out["_path"] = str(p)
    return out


def shell_lines(d):
    keys = ("os", "arch", "cpu", "cores", "ram_gb", "gpu_count", "gpu_names",
            "vram_gb", "driver", "memory_kind", "usable_mem_gb", "docker",
            "docker_gpu", "fingerprint", "probed_at", "probe_version")
    lines = []
    for k in keys:
        v = d.get(k, "")
        if isinstance(v, bool):
            v = "yes" if v else "no"
        lines.append(f'RAG_{k.upper()}="{v}"')
    return lines


def write_conf(dest=None):
    d = probe()
    p = pathlib.Path(dest).expanduser() if dest else DEFAULT_CONF
    p.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join([
        "# hardware.conf — what this machine can run.",
        f"# Written by platform_probe.py {__version__} on {d['probed_at']}.",
        "#",
        "# Shell-sourceable and Python-parseable; every script in this project reads",
        "# it instead of assuming a particular machine.",
        "#",
        "# HAND EDITS WIN. If detection got something wrong, or you want to reserve",
        "# memory for other work, change RAG_USABLE_MEM_GB and leave a note — the",
        "# probe never overwrites this file unless you re-run it with --write.",
        "#",
        "# Re-run after changing hardware:  python3 platform_probe.py --write",
        "",
        *shell_lines(d),
        "",
    ])
    p.write_text(body)
    return p, d


def usable_mem_gb(default=FALLBACK_USABLE_GB):
    """The number other scripts should use. Order: env > hardware.conf > live probe.

    Deliberately tolerant: a single script copied to another machine with no
    config file and no probe still gets a sane answer instead of an exception.
    """
    env = os.environ.get("RAG_USABLE_MEM_GB")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    conf = load_conf()
    if conf.get("usable_mem_gb"):
        try:
            return float(conf["usable_mem_gb"])
        except ValueError:
            pass
    try:
        return float(probe()["usable_mem_gb"])
    except Exception:
        return default


def stale_warning():
    """A message if hardware.conf no longer matches this machine, else ''."""
    conf = load_conf()
    if not conf.get("fingerprint"):
        return ""
    live = probe()
    if conf["fingerprint"] != live["fingerprint"]:
        return (f"hardware.conf describes different hardware "
                f"({conf['fingerprint']} vs {live['fingerprint']}) — "
                f"re-run: python3 platform_probe.py --write {conf.get('_path','')}")
    return ""


# ----------------------------- CLI ---------------------------------------
def human(d):
    G, B, Y, X = "\033[32m", "\033[1m", "\033[33m", "\033[0m"
    print(f"\n{B}Detected hardware{X}")
    print(f"  os / arch     : {d['os']}  ({d['arch']})")
    print(f"  cpu           : {d['cpu']}  ({d['cores']} threads)")
    print(f"  ram           : {d['ram_gb']:.1f} GB")
    if d["gpu_count"]:
        print(f"  gpu           : {d['gpu_names']}  "
              f"({d['vram_gb']:.1f} GB VRAM, driver {d['driver']})")
    else:
        print("  gpu           : none detected (nvidia-smi not found or no device)")
    print(f"  memory model  : {d['memory_kind']}")
    print(f"  docker        : {'yes' if d['docker'] else 'no'}"
          f"   gpu passthrough: {'yes' if d['docker_gpu'] else 'no'}")
    print(f"\n{B}Usable for models{X}: {G}{d['usable_mem_gb']:.1f} GB{X}"
          + ("   (from RAG_USABLE_MEM_GB)" if d["usable_overridden"] else ""))
    if d["memory_kind"] == "unified":
        print(f"  unified memory: {UNIFIED_FRACTION:.0%} of {d['ram_gb']:.0f} GB RAM, "
              "shared with the OS")
    elif d["memory_kind"] == "discrete":
        print(f"  discrete GPU: VRAM minus {DISCRETE_HEADROOM_GB:.0f} GB headroom. "
              "Bigger models still load by spilling to system RAM, but slowly.")
        if d["gpu_count"] > 1:
            print(f"  {Y}note: that is the SUM over {d['gpu_count']} GPUs. A single "
                  f"model only uses it all if it can be split across them; the "
                  f"largest single-GPU model here is ~"
                  f"{max(g['vram_gb'] for g in d['gpus']) - DISCRETE_HEADROOM_GB:.0f} GB.{X}")
    elif d["memory_kind"] == "cpu-only":
        print(f"  {Y}no GPU: {CPU_FRACTION:.0%} of RAM, and generation will be very "
              f"slow. Prefer models under ~8 GB.{X}")
    if not d["docker_gpu"] and d["gpu_count"]:
        print(f"  {Y}docker has no GPU runtime — containers will run on CPU. "
              f"Install nvidia-container-toolkit.{X}")
    warn = stale_warning()
    if warn:
        print(f"  {Y}! {warn}{X}")
    print()


def main():
    args = sys.argv[1:]
    if "--version" in args:
        print(f"platform_probe.py {__version__}")
        return
    if "--json" in args:
        print(json.dumps(probe(), indent=2))
        return
    if "--shell" in args:
        print("\n".join(shell_lines(probe())))
        return
    if "--write" in args:
        i = args.index("--write")
        dest = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("-") else None
        p, d = write_conf(dest)
        print(f"wrote {p}")
        print(f"  usable for models: {d['usable_mem_gb']:.1f} GB "
              f"({d['memory_kind']})")
        print("  edit that file to override anything the probe got wrong")
        return
    if "--help" in args or "-h" in args:
        print(__doc__)
        return
    human(probe())


if __name__ == "__main__":
    main()
