import time
import os
import sys
import shutil
import importlib
from datetime import datetime
from blip.build import Scheduler, Builder, Check, all_checks
import argparse

parser = argparse.ArgumentParser("blip")
subparsers = parser.add_subparsers(dest="cmd", help="Commands")
check_parser = subparsers.add_parser("check", help="Verify the design")
check_parser.add_argument("checks", nargs="*")
check_parser.add_argument("--list", action="store_true", default=False)
argv = parser.parse_args(sys.argv[1:])

if argv.cmd == "check":

    check_files = [
        "rtl.dvi.tmds",
        "rtl.pll",
        "rtl.ecp5.pll",
        "rtl.ecp5.io",
        "util.dvi_timing",
    ]

    def use_check(check: Check) -> bool:
        if not argv.checks: return True
        return any((check.name + ".").startswith(c + ".") for c in argv.checks)

    for f in check_files:
        should_load = not argv.checks
        for check in argv.checks:
            length = min(len(check), len(f))
            if check[:length] == f[:length]:
                should_load = True
                break
        if should_load:
            importlib.import_module("blip." + f)

    if argv.list:
        for check in all_checks:
            if not use_check(check): continue
            print(check.name)
        sys.exit(0)

    now = datetime.now()
    timestamp = now.strftime("%Y_%m_%d_%H_%M_%S")
    temp_dir = os.path.join("temp", timestamp)
    build_dir = "build"

    scheduler = Scheduler(max_threads=os.cpu_count())
    builder = Builder(scheduler, build_dir=temp_dir)

    begin_sec = time.time()

    num_executed = False
    for check in all_checks:
        if not use_check(check): continue
        print(check.name + "...", flush=True)
        builder.set_prefix(check.prefix)
        check.func(builder)
        scheduler.update()
        num_executed += 1

    while not scheduler.finished():
        scheduler.update()
        time.sleep(0.1)
    
    os.makedirs(build_dir, exist_ok=True)
    if os.path.exists(temp_dir):
        print(f"Copying '{temp_dir}' to '{build_dir}'")
        shutil.copytree(temp_dir, build_dir, dirs_exist_ok=True)

    end_sec = time.time()
    num_sec = end_sec - begin_sec
    print(f"Finished {num_executed} checks in {num_sec:.1f} seconds ({scheduler.max_threads} threads).")
