# Metrics mapping – pandorafms.vmware_vcenter_pyvmomi
This document maps **Pandora modules** to their **source metrics** (vSphere properties and PerformanceManager counters).

## Data sources
1) **Inventory / properties** (pyVmomi managed object properties)
- `vim.VirtualMachine` (VM config/runtime/summary)
- `vim.HostSystem` (host runtime/summary/hardware)
- `vim.Datastore` (summary + host mount info)

2) **PerformanceManager counters**
VM counters (per-VM):
- `cpu.usage.average` → CPU %  
- `mem.usage.average` → Memory %  
- `disk.usage.average` → Disk KBps  
- `net.received.average` → RX KBps  
- `net.transmitted.average` → TX KBps  

Host counters (per-host):
- `cpu.usage.average` → CPU %  
- `mem.usage.average` → Memory %  
- `disk.usage.average` → Disk KBps  
- `net.usage.average` → Network KBps  

> Counter names above are resolved via `vim.PerformanceManager.perfCounter` and then queried via `QueryPerf`.

## Module mapping

### vCenter / inventory totals
| Pandora module | Type | Source | Notes |
|---|---|---|---|
| `vCenter:Endpoint` | `async_data_string` | `_host_` macro | stored as text |
| `Inventory:VMsTotal` | `async_data` | count of VMs | from inventory traversal |
| `Inventory:HostsTotal` | `async_data` | count of hosts | from inventory traversal |
| `Inventory:DatastoresTotal` | `async_data` | count of datastores | from inventory traversal |
| `Hosts:Total` | `async_data` | count of hosts | derived from host list |
| `Hosts:Up` | `async_data` | `host.runtime.connectionState` | `connected` counted as up |
| `Hosts:Down` | `async_data` | `host.runtime.connectionState` | non-`connected` counted as down |

### VM totals (aggregate)
| Pandora module | Type | Source | Notes |
|---|---|---|---|
| `VMs:Total` | `async_data` | inventory | total VMs |
| `VMs:PoweredOn` | `async_data` | `vm.runtime.powerState` | `poweredOn` |
| `VMs:PoweredOff` | `async_data` | `vm.runtime.powerState` | `poweredOff` |
| `VMs:TotalvCPU` | `async_data` | `vm.config.hardware.numCPU` | summed across VMs |
| `VMs:TotalMemGiB` | `async_data` | `vm.config.hardware.memoryMB` | summed /1024 |
| `VMs:TotalCPUUsedMHz` | `async_data` | `vm.summary.quickStats.overallCpuUsage` | MHz summed |
| `VMs:TotalMemUsedGiB` | `async_data` | `vm.summary.quickStats.guestMemoryUsage` | MB summed /1024 |

### VM summary table (all VMs)
| Pandora module | Type | Source | Columns |
|---|---|---|---|
| `VM:SummaryTable` | `async_data_string` | inventory + perf counters | `Name, VM-ID, Power, vCPU, MemGiB, CPU%, Mem%, DiskKBps, RxKBps, TxKBps` |

Per-row sources:
- `Name` → `vm.name`
- `VM-ID` → `vm._moId`
- `Power` → `vm.runtime.powerState`
- `vCPU` → `vm.config.hardware.numCPU`
- `MemGiB` → `vm.config.hardware.memoryMB / 1024`
- `CPU%` → `cpu.usage.average`
- `Mem%` → `mem.usage.average`
- `DiskKBps` → `disk.usage.average`
- `RxKBps` → `net.received.average`
- `TxKBps` → `net.transmitted.average`

### Per-host summary (dynamic)
For each ESXi host:

| Pandora module pattern | Type | Source |
|---|---|---|
| `Host:<host>:ConnectionState` | `async_data_string` | `host.runtime.connectionState` |
| `Host:<host>:vCPU` | `async_data` | `host.summary.hardware.numCpuThreads` |
| `Host:<host>:MemGiB` | `async_data` | `host.summary.hardware.memorySize / GiB` |
| `Host:<host>:CPU%` | `async_data` | `cpu.usage.average` |
| `Host:<host>:Mem%` | `async_data` | `mem.usage.average` |
| `Host:<host>:DiskKBps` | `async_data` | `disk.usage.average` |
| `Host:<host>:NetKBps` | `async_data` | `net.usage.average` |
| `Host:<host>:VMsTotal` | `async_data` | VMs mapped to that host |
| `Host:<host>:VMsUp` | `async_data` | VMs with powerState=poweredOn mapped to host |
| `Host:<host>:VMsTotalvCPU` | `async_data` | sum of `vm.config.hardware.numCPU` per host |
| `Host:<host>:VMsTotalMemGiB` | `async_data` | sum of `vm.config.hardware.memoryMB/1024` per host |

### Per-host VM summary table (dynamic)
| Pandora module pattern | Type | Source | Columns |
|---|---|---|---|
| `Host:<host>:VMs:SummaryTable` | `async_data_string` | same as VM table + host filter | `Name, VM-ID, Power, vCPU, MemGiB, CPU%, Mem%, DiskKBps, RxKBps, TxKBps` |

### Datastores (dynamic)
For each datastore:

| Pandora module pattern | Type | Source | Notes |
|---|---|---|---|
| `DS:<ds>:CapacityGiB` | `async_data` | `ds.summary.capacity` | bytes→GiB |
| `DS:<ds>:FreeGiB` | `async_data` | `ds.summary.freeSpace` | bytes→GiB |
| `DS:<ds>:UsedPercent` | `async_data` | computed | `(capacity-free)/capacity*100` |
| `DS:<ds>:Type` | `async_data_string` | `ds.summary.type` | e.g. VMFS, NFS, vsan, vVol |
| `DS:<ds>:Shared` | `async_data` | `ds.summary.multipleHostAccess` | `1` shared, `0` not |
| `DS:<ds>:Host:<host>:Mounted` | `async_data` | `ds.host[*].mountInfo.mounted` | per-host mount flag |
| `DS:<ds>:Host:<host>:Connected` | `async_data` | `ds.host[*].mountInfo.accessible` | per-host accessibility/connected flag |
