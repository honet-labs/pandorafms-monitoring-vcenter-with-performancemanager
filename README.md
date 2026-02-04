# PandoraFMS – VMware vCenter Summary (pyVmomi)

Package: **pandorafms.vmware_vcenter_pyvmomi**  
Purpose: Collect **inventory + performance summary** for:
- vCenter (VM totals)
- ESXi hosts (per-host summary + per-host VM summary table)
- Datastores (capacity/free/used% + type/shared + mount/connectivity per host)

This DISCO uses **pyVmomi** (vSphere SOAP API) and **PerformanceManager** counters.

## Requirements
- Pandora discovery node with:
  - `python3`
  - `pyVmomi` installed (`pip install pyvmomi`)
- Network access to vCenter `https://<host>:443/`
- Credentials with permission to read inventory + performance counters.

## Configuration (macros)
From `discovery_definition.ini`:

- `_host_` – vCenter Host/IP
- `_user_` – SSO username (e.g. `administrator@vsphere.local`)
- `_password_` – SSO password

## Outputs in Pandora
The script writes **agent XML** into Pandora discovery incoming directory. The agent:
- Uses vCenter host/IP as `<ip>` (agent address)
- Creates many modules under groups:
  - `vCenter`, `VMs`, `Hosts`, per-host groups (`Host:<host>`), `Datastore`, `DS:<datastore>`

Key highlights:
- `VM:SummaryTable` – one table listing all VMs + utilization (CPU/Mem/Disk/Net).
- `Host:<host>:VMs:SummaryTable` – one table per ESXi host listing its VMs + utilization.
- Datastore modules include capacity/free/used%, type/shared, and per-host mount/connectivity flags.

## Notes on “utilization”
Utilization is derived from vSphere **PerformanceManager** counters and is sampled over a short window:
- VMs: CPU%, Mem%, DiskKBps, RxKBps, TxKBps
- Hosts: CPU%, Mem%, DiskKBps, NetKBps

(Exact counters are listed in `METRICS_*.md`.)

## Troubleshooting
- If performance columns are always `0`, ensure:
  - VMs are powered on
  - account has permission to query performance counters
  - vCenter has performance data retention enabled
- If host tables are empty, check if VMs are mapped to hosts in `runtime.host`.
- Verify the discovery task interval matches your expectations (default 300s).
