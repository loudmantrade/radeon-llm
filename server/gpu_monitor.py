#!/usr/bin/python3
"""Read-only live AMD GPU statistics from the macOS IORegistry."""
import argparse
import csv
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parent.parent
FIELDS={'gpu_util_percent':'Device Utilization %','vram_in_use_mib':'inUseVidMemoryBytes',
        'vram_free_mib':'vramFreeBytes','temperature_c':'Temperature(C)',
        'power_w':'Total Power(W)','core_mhz':'Core Clock(MHz)','memory_mhz':'Memory Clock(MHz)'}


def read_stats():
    result=subprocess.run(['/usr/sbin/ioreg','-r','-d','1','-c','IOAccelerator','-l','-w','0'],
                          capture_output=True,text=True,timeout=5)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'IORegistry query failed')
    current=None
    devices={}
    for line in result.stdout.splitlines():
        header=re.match(r'^\+-o\s+(\S+).*?id\s+(0x[0-9a-fA-F]+)',line)
        if header:current=(header.group(1),header.group(2))
        if not current or not current[0].startswith('AMDRadeon'):
            continue
        if not re.search(r'"PerformanceStatistics"\s*=',line):
            continue
        raw={key:float(value) for key,value in re.findall(r'"([^"\n]+)"\s*=\s*(-?\d+(?:\.\d+)?)',line)}
        row={'timestamp':datetime.now().astimezone().isoformat(timespec='seconds'),
             'device':current[0],'registry_id':current[1]}
        for field,key in FIELDS.items():
            value=raw.get(key)
            if value is None and field=='vram_in_use_mib':value=raw.get('vramUsedBytes')
            if value is not None and field.endswith('_mib'):value/=2**20
            row[field]=value
        devices[current[1]]=row
    if not devices:raise RuntimeError('AMD accelerator statistics unavailable')
    return list(devices.values())


def number(value,unit=''):
    return 'N/A' if value is None else '%.1f%s'%(value,unit)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--interval',type=float,default=1)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--samples',type=int)
    parser.add_argument('--json',action='store_true')
    parser.add_argument('--csv',nargs='?',const=str(ROOT/'logs/gpu-monitor.csv'))
    options=parser.parse_args()
    if options.interval<=0 or (options.samples is not None and options.samples<1):
        parser.error('interval and sample count must be positive')
    limit=1 if options.once else options.samples
    output=None
    writer=None
    try:
        if options.csv:
            path=Path(options.csv).expanduser()
            path.parent.mkdir(parents=True,exist_ok=True)
            header=not path.exists() or path.stat().st_size==0
            output=path.open('a',newline='')
            writer=csv.DictWriter(output,fieldnames=['timestamp','device','registry_id']+list(FIELDS))
            if header:writer.writeheader()
        count=0
        while limit is None or count<limit:
            started=time.monotonic()
            try:
                rows=read_stats()
                if options.json:
                    print(json.dumps(rows),flush=True)
                else:
                    if sys.stdout.isatty() and not options.once:print('\033[H\033[2J',end='')
                    print('Radeon GPU | '+rows[0]['timestamp'])
                    for row in rows:
                        print('Load: %s   VRAM in use: %s   VRAM free: %s'%
                              (number(row['gpu_util_percent'],'%'),number(row['vram_in_use_mib'],' MiB'),number(row['vram_free_mib'],' MiB')))
                        print('Temperature: %s   Driver power: %s   Core: %s   Memory clock: %s'%
                              (number(row['temperature_c'],' C'),number(row['power_w'],' W'),
                               number(row['core_mhz'],' MHz'),number(row['memory_mhz'],' MHz')))
                    print('Whole GPU, including display/apps. Driver counters; used + free may omit reserved VRAM.')
                    if not options.once:print('Ctrl+C to stop.' + (' CSV: '+str(path) if options.csv else ''))
                    sys.stdout.flush()
                if writer:
                    writer.writerows(rows);output.flush()
            except (OSError,RuntimeError,subprocess.TimeoutExpired) as exc:
                if options.once:raise SystemExit(str(exc))
                print('GPU statistics unavailable: '+str(exc),flush=True)
            count+=1
            if limit is None or count<limit:
                time.sleep(max(0,options.interval-(time.monotonic()-started)))
    except KeyboardInterrupt:
        pass
    finally:
        if output:output.close()


if __name__=='__main__':main()
