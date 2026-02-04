#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PandoraFMS Discovery (.disco) - VMware vCenter via pyVmomi (VIM/SOAP) - Summary & Table

- Collect inventory (hosts, VMs, datastores) and key capacity data
- Collect performance counters via PerformanceManager for VMs & Hosts:
    cpu.usage.average (%)
    mem.usage.average (%)
    net.usage.average (KBps)
    disk.usage.average (KBps)

Output: Pandora agent XML written into --outdir as *.data
"""

import argparse
import csv
import os
import re
import ssl
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---- Optional dependency
try:
    from pyVim.connect import SmartConnect, Disconnect
    from pyVmomi import vim
except Exception as e:
    print("ERROR: pyVmomi is not installed. Install with: pip3 install pyvmomi", file=sys.stderr)
    print(f"DETAIL: {e}", file=sys.stderr)
    sys.exit(5)

def now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")

def xml_escape(s) -> str:
    if s is None:
        return ""
    s = str(s)
    return (s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))

def mk_module(
    name: str,
    value,
    mtype: str = "async_data",
    unit: Optional[str] = None,
    group: Optional[str] = None,
    description: Optional[str] = None,
    # string thresholds (regex)
    str_warning: Optional[str] = None,
    str_critical: Optional[str] = None,
    # numeric thresholds
    min_warning: Optional[str] = None,
    max_warning: Optional[str] = None,
    min_critical: Optional[str] = None,
    max_critical: Optional[str] = None,
):
    parts = ["<module>",
             f"<name>{xml_escape(name)}</name>",
             f"<type>{xml_escape(mtype)}</type>",
             f"<data>{xml_escape(value)}</data>"]
    if unit:
        parts.append(f"<unit>{xml_escape(unit)}</unit>")
    if group:
        parts.append(f"<module_group>{xml_escape(group)}</module_group>")
    if description:
        parts.append(f"<description>{xml_escape(description)}</description>")

    # string thresholds (regex)
    if str_warning is not None:
        parts.append(f"<str_warning>{xml_escape(str_warning)}</str_warning>")
    if str_critical is not None:
        parts.append(f"<str_critical>{xml_escape(str_critical)}</str_critical>")

    # numeric thresholds
    if min_warning is not None:
        parts.append(f"<min_warning>{xml_escape(min_warning)}</min_warning>")
    if max_warning is not None:
        parts.append(f"<max_warning>{xml_escape(max_warning)}</max_warning>")
    if min_critical is not None:
        parts.append(f"<min_critical>{xml_escape(min_critical)}</min_critical>")
    if max_critical is not None:
        parts.append(f"<max_critical>{xml_escape(max_critical)}</max_critical>")

    parts.append("</module>")
    return "\n".join(parts)

def write_agent_xml(agent: str, group: str, alias: str, address: str, os_name: str, modules: List[str]) -> str:
    # NOTE:
    # - Some Pandora console versions show strange "ARRAY(0x...)" values in the agent IP list
    #   if <address> is provided as an element. Using address="..." attribute is more robust.
    header = [
        f'<agent_data agent_name="{xml_escape(agent)}" address="{xml_escape(address)}">',
        f"<group>{xml_escape(group)}</group>",
        f"<alias>{xml_escape(alias)}</alias>",
        f"<os_name>{xml_escape(os_name)}</os_name>",
    ]
    footer = ["</agent_data>"]
    return "\n".join(header + modules + footer) + "\n"


def connect_vcenter(host: str, user: str, password: str, insecure: bool):
    # Force insecure TLS to avoid CERTIFICATE_VERIFY_FAILED on self-signed/unknown CA environments.
    # If you want strict verification, replace this with a verified context and install CA certs on the Pandora server.
    ctx = ssl._create_unverified_context()
    si = SmartConnect(host=host, user=user, pwd=password, port=443, sslContext=ctx)
    return si


def build_counter_maps(perf) -> Tuple[Dict[str, int], Dict[int, str], Dict[int, str]]:
    """
    Returns:
      - fullName -> counterId
      - counterId -> fullName
      - counterId -> unitKey (e.g., 'percent', 'kiloBytesPerSecond')
    fullName format: group.name.rollupType  (e.g., cpu.usage.average)
    """
    f2id: Dict[str, int] = {}
    id2f: Dict[int, str] = {}
    id2unit: Dict[int, str] = {}
    for c in perf.perfCounter:
        full = f"{c.groupInfo.key}.{c.nameInfo.key}.{c.rollupType}"
        f2id[full] = c.key
        id2f[c.key] = full
        try:
            id2unit[c.key] = c.unitInfo.key
        except Exception:
            id2unit[c.key] = ""
    return f2id, id2f, id2unit

def get_interval_id(perf, entity) -> int:
    try:
        s = perf.QueryPerfProviderSummary(entity=entity)
        rr = int(getattr(s, "refreshRate", 0) or 0)
        if rr > 0:
            return rr
    except Exception:
        pass
    return 20

def filter_supported_metric_ids(perf, entity, interval_id: int, metric_ids: List["vim.PerformanceManager.MetricId"]):
    """
    Filters metric_ids to those actually available for this entity+interval.
    This prevents InvalidArgument/NoSuchCounter errors on some environments.
    """
    try:
        avail = perf.QueryAvailablePerfMetric(entity=entity, intervalId=interval_id) or []
        supported = {m.counterId for m in avail}
        return [m for m in metric_ids if m.counterId in supported]
    except Exception:
        # If QueryAvailablePerfMetric is blocked by permission, fall back to original list.
        return metric_ids

def query_latest(perf, entities: List, metric_ids: List["vim.PerformanceManager.MetricId"], interval_id: int, chunk_size: int = 40):
    """
    Returns: moid -> {counterId -> raw_value_last_sample}
    """
    out: Dict[str, Dict[int, int]] = {}
    if not entities or not metric_ids:
        return out

    query_fn = getattr(perf, "QueryStats", None) or getattr(perf, "QueryPerf", None)
    if query_fn is None:
        raise RuntimeError("PerformanceManager has no QueryStats/QueryPerf method")

    for i in range(0, len(entities), chunk_size):
        batch = entities[i:i+chunk_size]
        specs = []
        for ent in batch:
            specs.append(vim.PerformanceManager.QuerySpec(
                entity=ent,
                metricId=metric_ids,
                intervalId=interval_id,
                maxSample=1
            ))
        try:
            res = query_fn(querySpec=specs)
        except TypeError:
            res = query_fn(specs)

        if not res:
            continue

        for em in res:
            ent = getattr(em, "entity", None)
            if ent is None:
                continue
            moid = getattr(ent, "_moId", str(ent))
            out.setdefault(moid, {})
            for series in getattr(em, "value", []) or []:
                cid = series.id.counterId
                vals = getattr(series, "value", []) or []
                if not vals:
                    continue
                out[moid][cid] = vals[-1]
    return out

def scale_value(raw: int, unit_key: str):
    if raw is None:
        return None
    if unit_key == "percent":
        return float(raw) / 100.0
    return raw

def format_bytes_gib(b: int) -> float:
    return float(b) / (1024.0 ** 3)


def fmt_uptime(seconds: int) -> str:
    try:
        seconds = int(seconds)
    except Exception:
        return ""
    if seconds < 0:
        seconds = 0
    days = seconds // 86400
    rem = seconds % 86400
    hh = rem // 3600
    rem %= 3600
    mm = rem // 60
    ss = rem % 60
    return f"{days:02d},{hh:02d}:{mm:02d}:{ss:02d}"


def datastore_type_label(ds) -> str:
    """Return a human readable datastore type label (e.g. 'VMFS 6', 'NFS NFS41')."""
    try:
        summ = ds.summary
        t = (getattr(summ, "type", "") or "").strip()
    except Exception:
        t = ""
    t_upper = t.upper()

    # VMFS
    if t_upper == "VMFS":
        try:
            info = ds.info
            vmfs = getattr(info, "vmfs", None)
            maj = getattr(vmfs, "majorVersion", None)
            if maj is not None:
                return f"VMFS {maj}"
        except Exception:
            pass
        return "VMFS"

    # NAS / NFS
    if t_upper in ("NFS", "NFS41", "NAS"):
        try:
            info = ds.info
            nas = getattr(info, "nas", None)
            nas_type = getattr(nas, "type", None)
            if nas_type:
                return f"NFS {nas_type}"
        except Exception:
            pass
        return "NFS"

    return t if t else "UNKNOWN"


def make_ds_hostmount_table(rows: list, max_rows: int = 200) -> str:
    """Render a plain text table for datastore host mount status."""
    headers = ["Host", "Mounted", "Connected", "AccessMode", "MountPoint"]
    out_lines = []
    w_host = 30
    w_mount = 45
    w_acc = 10

    out_lines.append(
        f"{headers[0]:<{w_host}} | {headers[1]:<7} | {headers[2]:<9} | {headers[3]:<{w_acc}} | {headers[4]:<{w_mount}}"
    )
    out_lines.append("-" * (w_host + 3 + 7 + 3 + 9 + 3 + w_acc + 3 + w_mount))

    try:
        rows = sorted(rows, key=lambda r: str(r.get("host", "")))
    except Exception:
        pass

    for r in rows[:max_rows]:
        host = str(r.get("host", ""))[:w_host]
        mounted = "Yes" if r.get("mounted") else "No"
        connected = "Yes" if r.get("connected") else "No"
        access = str(r.get("access", ""))[:w_acc]
        path = str(r.get("path", ""))[:w_mount]
        out_lines.append(f"{host:<{w_host}} | {mounted:<7} | {connected:<9} | {access:<{w_acc}} | {path:<{w_mount}}")

    return "\n".join(out_lines)


def make_vm_summary_table(rows, max_rows: int = 300) -> str:
    """
    rows: list of dict with keys:
      name, vmid, power, vcpu, memgib,
      cpu_pct, mem_pct, disk_kbps, rx_kbps, tx_kbps
    Returns monospace-friendly table text.
    """
    rows = sorted(rows, key=lambda r: (r.get("name","") or "").lower())
    total_rows = len(rows)
    truncated = False
    if max_rows and total_rows > max_rows:
        rows = rows[:max_rows]
        truncated = True

    headers = ["Name","VM-ID","Power","vCPU","MemGiB","CPU%","Mem%","DiskKBps","RxKBps","TxKBps"]

    w_name = max(20, min(40, max((len(str(r.get("name",""))) for r in rows), default=20)))
    w_vmid = 10
    w_power = 12
    w_vcpu = 5
    w_mem = 6
    w_cpu = 6
    w_memuse = 6
    w_disk = 8
    w_rx = 7
    w_tx = 7

    def pad(s, w):
        s = str(s)
        if len(s) > w:
            return s[:w-1] + "…"
        return s + " "*(w-len(s))

    line = (
        pad(headers[0], w_name) + " | " +
        pad(headers[1], w_vmid) + " | " +
        pad(headers[2], w_power) + " | " +
        pad(headers[3], w_vcpu) + " | " +
        pad(headers[4], w_mem) + " | " +
        pad(headers[5], w_cpu) + " | " +
        pad(headers[6], w_memuse) + " | " +
        pad(headers[7], w_disk) + " | " +
        pad(headers[8], w_rx) + " | " +
        pad(headers[9], w_tx)
    )
    sep = "-"*len(line)

    out = [line, sep]
    for r in rows:
        out.append(
            pad(r.get("name",""), w_name) + " | " +
            pad(r.get("vmid",""), w_vmid) + " | " +
            pad(r.get("power",""), w_power) + " | " +
            pad(r.get("vcpu",""), w_vcpu) + " | " +
            pad(r.get("memgib",""), w_mem) + " | " +
            pad(r.get("cpu_pct",""), w_cpu) + " | " +
            pad(r.get("mem_pct",""), w_memuse) + " | " +
            pad(r.get("disk_kbps",""), w_disk) + " | " +
            pad(r.get("rx_kbps",""), w_rx) + " | " +
            pad(r.get("tx_kbps",""), w_tx)
        )

    if truncated:
        out.append("")
        out.append(f"... truncated to first {max_rows} VMs (total={total_rows})")
    return "\n".join(out)

def safe_name(s: str) -> str:
    return (s or "").replace("\n", " ").replace("\r", " ").strip()

def collect_view(content, vimtype):
    view = content.viewManager.CreateContainerView(content.rootFolder, [vimtype], True)
    try:
        return list(view.view)
    finally:
        try:
            view.Destroy()
        except Exception:
            pass

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-H", "--host", required=True)
    ap.add_argument("-u", "--user", required=True)
    ap.add_argument("-p", "--password", required=True)
    ap.add_argument("-A", "--agent", default=None)
    ap.add_argument("-g", "--group", default="VMware")
    ap.add_argument("--alias", default=None)
    ap.add_argument("--address", default=None)
    ap.add_argument("--outdir", default="/var/spool/pandora/data_in")
    ap.add_argument("--run-log", default=None)
    ap.add_argument("--status-file", default=None)
    ap.add_argument("--insecure", default="true", help="true/false (disable TLS verify)")
    ap.add_argument("--vm-regex", default=".*", help="filter VM names (regex)")
    ap.add_argument("--host-regex", default=".*", help="filter Host names (regex)")
    ap.add_argument("--max-vms", type=int, default=200)
    ap.add_argument("--max-hosts", type=int, default=100)
    ap.add_argument("--max-datastores", type=int, default=200)
    ap.add_argument("--list-counters", action="store_true", help="print available counters and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    insecure = str(args.insecure).lower() in ("1", "true", "yes", "y", "on")

    agent = args.agent or f"VC-{args.host}"
    alias = args.alias or agent
    address = args.host  # always use vCenter host from -H
    started = time.time()

    vm_re = re.compile(args.vm_regex)
    host_re = re.compile(args.host_regex)

    try:
        si = connect_vcenter(args.host, args.user, args.password, insecure=insecure)
    except Exception as e:
        msg = f"ERROR: connect/login failed: {e}"
        if args.status_file:
            Path(args.status_file).write_text(f"{now_ts()} {msg}\n", encoding="utf-8")
        print(msg, file=sys.stderr)
        sys.exit(2)

    modules: List[str] = []
    counters = {"vms": 0, "hosts": 0, "datastores": 0}

    try:
        content = si.RetrieveContent()
        perf = content.perfManager

        f2id, id2f, id2unit = build_counter_maps(perf)

        if args.list_counters:
            for full in sorted(f2id.keys()):
                if full.startswith(("cpu.", "mem.", "net.", "disk.", "datastore.")):
                    cid = f2id[full]
                    unit = id2unit.get(cid, "")
                    print(f"{full}\t(counterId={cid}, unit={unit})")
            return

        hosts_all = [h for h in collect_view(content, vim.HostSystem) if host_re.search(h.name or "")]
        vms_all = [v for v in collect_view(content, vim.VirtualMachine) if vm_re.search(v.name or "")]
        dss_all = collect_view(content, vim.Datastore)

        hosts = hosts_all[: max(0, args.max_hosts)]
        vms = vms_all[: max(0, args.max_vms)]
        dss = dss_all[: max(0, args.max_datastores)]

        counters["hosts"] = len(hosts_all)
        counters["vms"] = len(vms_all)
        counters["datastores"] = len(dss_all)

        modules.append(mk_module("vCenter:Endpoint", f"https://{args.host}/sdk", "async_string", group="Information"))
        modules.append(mk_module("Inventory:HostsTotal", counters["hosts"], "async_data", group="Information"))
        modules.append(mk_module("Inventory:VMsTotal", counters["vms"], "async_data", group="Information"))
        modules.append(mk_module("Inventory:DatastoresTotal", counters["datastores"], "async_data", group="Information"))
        # Datastores (capacity / free / used% + type + host mount status)
        for ds in dss:
            try:
                summ = ds.summary
                cap = int(getattr(summ, "capacity", 0) or 0)
                free = int(getattr(summ, "freeSpace", 0) or 0)
                used = max(cap - free, 0)
                pct = (used / cap * 100.0) if cap > 0 else 0.0

                ds_name_raw = getattr(summ, "name", None) or getattr(ds, "name", None) or getattr(ds, "_moId", "datastore")
                name = safe_name(ds_name_raw)

                g = "Datastore"
                modules.append(mk_module(f"DS:{name}:CapacityGiB", format_bytes_gib(cap), "async_data", unit="GiB", group=g))
                modules.append(mk_module(f"DS:{name}:FreeGiB", format_bytes_gib(free), "async_data", unit="GiB", group=g))
                modules.append(mk_module(f"DS:{name}:UsedPercent", round(pct, 2), "async_data", unit="%", group=g))

                # Type label (e.g. VMFS 6 / NFS NFS41)
                dtype = datastore_type_label(ds)
                modules.append(mk_module(f"DS:{name}:Type", dtype, "async_string", group=g))

                # Shared datastore flag
                shared = 1 if getattr(summ, "multipleHostAccess", False) else 0
                modules.append(mk_module(f"DS:{name}:Shared", shared, "async_data", group=g, description="1=shared, 0=local"))

                # Per-host mount status
                host_mounts = getattr(ds, "host", None) or []
                hm_rows = []
                for hm in host_mounts:
                    try:
                        host = hm.key
                        hname = getattr(host, "name", None) or getattr(host, "_moId", "host")
                        mi = hm.mountInfo
                        mounted = 1 if getattr(mi, "mounted", False) else 0
                        connected = 1 if getattr(mi, "accessible", False) else 0
                        access_mode = getattr(mi, "accessMode", "") or ""
                        path = getattr(mi, "path", "") or ""

                        hm_rows.append(
                            {"host": hname, "mounted": bool(mounted), "connected": bool(connected), "access": access_mode, "path": path}
                        )

                        hn = safe_name(hname)
                        g2 = "Datastore Host Mount"
                        modules.append(mk_module(f"DS:{name}:Host:{hn}:Mounted", mounted, "async_data", group=g2))
                        modules.append(mk_module(f"DS:{name}:Host:{hn}:Connected", connected, "async_data", group=g2))
                    except Exception:
                        continue

                if hm_rows:
                    table_txt = make_ds_hostmount_table(hm_rows)
                    modules.append(mk_module(f"DS:{name}:HostMountTable", table_txt, "async_string", group=g))
            except Exception:
                continue




# Host perf counters (kept)
        wanted_host = ["cpu.usage.average", "mem.usage.average", "net.usage.average", "disk.usage.average"]
        # Host perf
        if hosts:
            interval_host = get_interval_id(perf, hosts[0])
            host_metric_ids = [vim.PerformanceManager.MetricId(counterId=f2id[w], instance="") for w in wanted_host if w in f2id]
            host_metric_ids = filter_supported_metric_ids(perf, hosts[0], interval_host, host_metric_ids)
            host_perf = query_latest(perf, hosts, host_metric_ids, interval_host, chunk_size=35)
        else:
            host_perf = {}

        # Hosts modules
        for h in hosts:
            moid = getattr(h, "_moId", "")
            hname = safe_name(h.name or moid)
            g_inv = "Host Esxi"
            g_perf = "Perf Host"

            try:
                cs = str(h.runtime.connectionState)
            except Exception:
                cs = "unknown"
            modules.append(mk_module(f"Host:{hname} ({moid}):ConnectionState", cs, "async_string", group=g_inv,
                                     str_critical=".*(disconnected|notResponding).*"))

            pdata = host_perf.get(moid, {})
            for cid, raw in pdata.items():
                full = id2f.get(cid, str(cid))
                unit = id2unit.get(cid, "")
                val = scale_value(raw, unit)
                if unit == "percent":
                    modules.append(mk_module(f"Host:{hname} ({moid}):{full}", f"{val:.2f}", "async_data", unit="%", group=g_perf,
                                             min_warning="80", min_critical="90"))
                elif unit == "kiloBytesPerSecond":
                    modules.append(mk_module(f"Host:{hname} ({moid}):{full}", val, "async_data", unit="KBps", group=g_perf))
                else:
                    modules.append(mk_module(f"Host:{hname} ({moid}):{full}", val, "async_data", group=g_perf))
        # VM perf counters for table (CPU/Mem/Disk/Net RX/TX)
        wanted_vm = ["cpu.usage.average", "mem.usage.average", "disk.usage.average", "net.received.average", "net.transmitted.average"]
        vm_perf = {}
        vm_counter_ids = {}
        if vms_all:
            vms_on_for_perf = []
            for v in vms_all:
                try:
                    if str(v.runtime.powerState) == "poweredOn":
                        vms_on_for_perf.append(v)
                except Exception:
                    continue

            if vms_on_for_perf:
                interval_vm = get_interval_id(perf, vms_on_for_perf[0])
                vm_metric_ids = []
                for w in wanted_vm:
                    if w in f2id:
                        cid = f2id[w]
                        vm_counter_ids[w] = cid
                        vm_metric_ids.append(vim.PerformanceManager.MetricId(counterId=cid, instance=""))
                vm_metric_ids = filter_supported_metric_ids(perf, vms_on_for_perf[0], interval_vm, vm_metric_ids)
                vm_perf = query_latest(perf, vms_on_for_perf, vm_metric_ids, interval_vm, chunk_size=25)
        # VMs summary (counts + totals + one table module)
        vm_rows = []
        vm_total = 0
        vm_on = 0
        vm_off = 0
        vm_total_vcpu = 0
        vm_total_mem_gib = 0.0
        vm_total_cpu_used_mhz = 0
        vm_total_mem_used_gib = 0.0

        # Per-host aggregation
        host_vm = {}  # host_moid -> dict counts/totals
        host_vm_rows = {}  # host_moid -> list of VM row dicts (for per-host tables)

        for v in vms_all:  # use all matched VMs (not limited) for summary
            vm_total += 1
            moid = getattr(v, "_moId", "")
            vname = safe_name(v.name or moid)
            vmid = moid

            # power
            try:
                pwr = str(v.runtime.powerState)
            except Exception:
                pwr = "unknown"
            if "poweredOn" in pwr:
                vm_on += 1
            elif "poweredOff" in pwr:
                vm_off += 1

            # config
            try:
                vcpu = int(v.config.hardware.numCPU)
            except Exception:
                vcpu = 0
            try:
                mem_gib = float(v.config.hardware.memoryMB) / 1024.0
            except Exception:
                mem_gib = 0.0

            vm_total_vcpu += vcpu
            vm_total_mem_gib += mem_gib

            # quickStats usage (MHz/MB) - best effort
            try:
                cpu_used = int(getattr(v.summary.quickStats, "overallCpuUsage", 0) or 0)  # MHz
            except Exception:
                cpu_used = 0
            try:
                mem_used_mb = int(getattr(v.summary.quickStats, "guestMemoryUsage", 0) or 0)  # MB
            except Exception:
                mem_used_mb = 0
            vm_total_cpu_used_mhz += cpu_used
            vm_total_mem_used_gib += (mem_used_mb / 1024.0)

            # host mapping
            host_moid = ""
            host_name = ""
            try:
                h = v.runtime.host
                if h:
                    host_moid = getattr(h, "_moId", "")
                    host_name = safe_name(getattr(h, "name", "") or host_moid)
            except Exception:
                pass

            if host_moid:
                agg = host_vm.setdefault(host_moid, {
                    "name": host_name or host_moid,
                    "total": 0, "on": 0, "off": 0,
                    "vcpu": 0, "mem_gib": 0.0,
                    "cpu_used_mhz": 0, "mem_used_gib": 0.0
                })
                agg["total"] += 1
                if "poweredOn" in pwr:
                    agg["on"] += 1
                elif "poweredOff" in pwr:
                    agg["off"] += 1
                agg["vcpu"] += vcpu
                agg["mem_gib"] += mem_gib
                agg["cpu_used_mhz"] += cpu_used
                agg["mem_used_gib"] += (mem_used_mb / 1024.0)

            pdata = vm_perf.get(moid, {})
            def _get_metric(full: str) -> str:
                cid = vm_counter_ids.get(full)
                if not cid:
                    return ""
                raw = pdata.get(cid)
                if raw is None:
                    return ""
                unit = id2unit.get(cid, "")
                val = scale_value(raw, unit)
                if unit == "percent":
                    return f"{val:.2f}"
                try:
                    return f"{int(val)}"
                except Exception:
                    return str(val)

            cpu_pct = _get_metric("cpu.usage.average")
            mem_pct = _get_metric("mem.usage.average")
            disk_kbps = _get_metric("disk.usage.average")
            rx_kbps = _get_metric("net.received.average")
            tx_kbps = _get_metric("net.transmitted.average")

            vm_rows.append({
                "name": vname,
                "vmid": vmid,
                "power": pwr.upper().replace("POWERED", "POWERED_"),
                "vcpu": vcpu,
                "memgib": f"{mem_gib:.2f}",
                "cpu_pct": cpu_pct,
                "mem_pct": mem_pct,
                "disk_kbps": disk_kbps,
                "rx_kbps": rx_kbps,
                "tx_kbps": tx_kbps,
            })
            if host_moid:
                host_vm_rows.setdefault(host_moid, []).append(vm_rows[-1])

        # Host summary counts (UP/DOWN) using hosts_all
        host_total = 0
        host_up = 0
        host_down = 0
        for h in hosts_all:
            host_total += 1
            try:
                cs = str(h.runtime.connectionState).lower()
            except Exception:
                cs = "unknown"
            if cs in ("connected",):
                host_up += 1
            elif cs in ("disconnected", "notresponding"):
                host_down += 1

        # ===== VCenter summary modules =====
        g_vc = "VCenter"
        modules.append(mk_module("Hosts:Total", host_total, "async_data", group=g_vc))
        modules.append(mk_module("Hosts:Up", host_up, "async_data", group=g_vc))
        modules.append(mk_module("Hosts:Down", host_down, "async_data", group=g_vc))

        modules.append(mk_module("VMs:Total", vm_total, "async_data", group=g_vc))
        modules.append(mk_module("VMs:PoweredOn", vm_on, "async_data", group=g_vc))
        modules.append(mk_module("VMs:PoweredOff", vm_off, "async_data", group=g_vc))
        modules.append(mk_module("VMs:TotalvCPU", vm_total_vcpu, "async_data", group=g_vc))
        modules.append(mk_module("VMs:TotalMemGiB", f"{vm_total_mem_gib:.2f}", "async_data", unit="GiB", group=g_vc))
        modules.append(mk_module("VMs:TotalCPUUsedMHz", vm_total_cpu_used_mhz, "async_data", unit="MHz", group=g_vc))
        modules.append(mk_module("VMs:TotalMemUsedGiB", f"{vm_total_mem_used_gib:.2f}", "async_data", unit="GiB", group=g_vc))

        # Per-host VM modules
        g_hs = "Host Summary"
        for h in hosts_all:
            hmoid = getattr(h, "_moId", "")
            hname = safe_name(h.name or hmoid)
            agg = host_vm.get(hmoid, {"total":0,"on":0,"off":0,"vcpu":0,"mem_gib":0.0,"cpu_used_mhz":0,"mem_used_gib":0.0})
            modules.append(mk_module(f"Host:{hname} ({hmoid}):VMsTotal", agg["total"], "async_data", group=g_hs))
            modules.append(mk_module(f"Host:{hname} ({hmoid}):VMsUp", agg["on"], "async_data", group=g_hs))
            modules.append(mk_module(f"Host:{hname} ({hmoid}):VMsDown", agg["off"], "async_data", group=g_hs))
            modules.append(mk_module(f"Host:{hname} ({hmoid}):VMsTotalvCPU", agg["vcpu"], "async_data", group=g_hs))
            modules.append(mk_module(f"Host:{hname} ({hmoid}):VMsTotalMemGiB", f"{agg['mem_gib']:.2f}", "async_data", unit="GiB", group=g_hs))
            modules.append(mk_module(f"Host:{hname} ({hmoid}):VMsCPUUsedMHz", agg["cpu_used_mhz"], "async_data", unit="MHz", group=g_hs))
            modules.append(mk_module(f"Host:{hname} ({hmoid}):VMsMemUsedGiB", f"{agg['mem_used_gib']:.2f}", "async_data", unit="GiB", group=g_hs))

        # VM summary table module (single module)
        table_txt = make_vm_summary_table(vm_rows, max_rows=500)
        modules.append(mk_module("VM:SummaryTable", table_txt, "async_string", group=g_vc))

        # Per-ESXi-host VM summary tables (one table module per host)
        # This is useful to see per-host VM utilization in a single view, without creating per-VM modules.
        for hmoid, agg in sorted(host_vm.items(), key=lambda kv: (kv[1].get("name","") or "").lower()):
            hname = agg.get("name") or hmoid
            rows = host_vm_rows.get(hmoid, [])
            if not rows:
                continue
            table_txt = make_vm_summary_table(rows, max_rows=500)
            modules.append(mk_module(f"Host:{hname} ({hmoid}):VMsSummaryTable", table_txt, "async_string", group=g_hs))

        xml_txt = write_agent_xml(agent, args.group, alias, address, "VMware vCenter", modules)

        Path(args.outdir).mkdir(parents=True, exist_ok=True)
        safe = agent.replace("/", "_").replace(":", "_").replace(" ", "_")
        out = Path(args.outdir) / f"{safe}_{int(time.time())}.data"
        out.write_text(xml_txt, encoding="utf-8")

        if args.status_file:
            Path(args.status_file).write_text(f"{now_ts()} OK wrote {out}\n", encoding="utf-8")

        if args.run_log:
            new = not os.path.exists(args.run_log)
            with open(args.run_log, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["timestamp", "vcenter", "agent", "result", "duration_ms", "output", "note"])
                note = f"hosts={counters['hosts']} (sent={len(hosts)}); vms={counters['vms']} (sent={len(vms)}); datastores={counters['datastores']} (sent={len(dss)})"
                w.writerow([now_ts(), args.host, agent, "OK", int((time.time() - started) * 1000), str(out), note])

        print(f"OK: wrote {out}")

    except Exception as e:
        msg = f"ERROR during collect: {e}"
        if args.status_file:
            Path(args.status_file).write_text(f"{now_ts()} {msg}\n", encoding="utf-8")
        print(msg, file=sys.stderr)
        sys.exit(3)
    finally:
        try:
            Disconnect(si)
        except Exception:
            pass

if __name__ == "__main__":
    main()
