from nmigen.build import Platform
from nmigen.build.run import BuildPlan
import os
import sys
import subprocess
from typing import Iterable, Optional, Callable, List
from dataclasses import dataclass

@dataclass
class Result:
    ok: bool
    info: str = ""
    exit_code: int = 0

@dataclass
class Check:
    name: str
    prefix: List[str]
    func: Callable

class Task:
    def __init__(self, name: str, id: str, threads: int=1):
        self.name = name
        self.id = id
        self.threads = threads

    def describe(self) -> str:
        return self.name

class ExecTask(Task):
    def __init__(self, name: str, id: str, args: Iterable[str], cwd: str, stdout, stderr, threads: int=1):
        super().__init__(name, id, threads=threads)
        self.args = list(args)
        self.cwd = cwd
        self.stdout = stdout
        self.stderr = stderr
        self.stdout_file = None
        self.stderr_file = None

    def start(self):
        stdout, stderr = self.stdout, self.stderr
        if isinstance(stdout, str):
            self.stdout_file = stdout = open(self.stdout, "w")
        if isinstance(stderr, str):
            self.stderr_file = stderr = open(self.stderr, "w")

        self.proc = subprocess.Popen(self.args, cwd=self.cwd, stdout=stdout, stderr=stderr)

    def poll(self) -> Optional[Result]:
        exit_code = self.proc.poll()
        if exit_code is None: return None

        if self.stdout_file: self.stdout_file.close()
        if self.stderr_file: self.stderr_file.close()

        if exit_code == 0:
            return Result(ok=True)
        else:
            return Result(ok=False, exit_code=exit_code)

    def describe(self) -> str:
        args = " ".join(self.args)
        return f"$ {args}"

class Scheduler:
    def __init__(self, max_threads):
        self.queue = []
        self.active = []
        self.max_threads = max_threads

    def add_task(self, task: Task) -> Task:
        self.queue.append(task)
        return task
    
    def update(self):
        # Poll active task
        still_active = []
        for task in self.active:
            result = task.poll()
            if result is None:
                still_active.append(task)
                continue
            self.on_done(task, result)
        self.active = still_active

        # Schedule new tasks
        while self.queue and sum(a.threads for a in self.active) < self.max_threads:
            task = self.queue.pop(0)
            self.on_start(task)
            task.start()
            self.active.append(task)

    def finished(self) -> bool:
        return not (self.queue or self.active)

    def on_start(self, task: Task):
        print(f"{task.describe()}   ({task.id})", flush=True)

    def on_done(self, task: Task, result: Result):
        if result.ok:
            print(f"{task.id}: OK", flush=True)
        else:
            print(f"{task.id}: FAIL", flush=True)

class Builder:
    def __init__(self, scheduler: Scheduler, build_dir: str):
        self.build_dir = build_dir
        self.prefix = []
        self.scheduler = scheduler
    
    def set_prefix(self, prefix: Iterable[str]):
        self.prefix = list(prefix)
        self.prefix_path = os.path.join(self.build_dir, *self.prefix)
        os.makedirs(self.prefix_path, exist_ok=True)
    
    def begin_check(self, check: Check):
        self.set_prefix(check.prefix)

    def temp_file(self, name: str) -> str:
        return os.path.join(self.prefix_path, name)

    def temp_open(self, name: str) -> str:
        return open(self.temp_file(name), "w")
    
    def temp_exists(self, name: str) -> bool:
        return os.path.exists(self.temp_file(name))

    def exec(self, name: str, exe: str, args: Iterable[str], cwd: Optional[str] = None, threads: int = 1):
        if cwd is None:
            cwd = self.prefix_path
        exe_id = ".".join(self.prefix + [name])
        self.scheduler.add_task(ExecTask(name, exe_id, [exe] + args,
            stdout=os.path.join(self.prefix_path, name + ".out"),
            stderr=os.path.join(self.prefix_path, name + ".err"),
            cwd=cwd,
            threads=threads))
    
    def exec_plan(self, name: str, plan: BuildPlan, cwd: Optional[str] = None):
        if cwd is None:
            cwd = self.prefix_path
        
        plan.execute_local(cwd, run_script=False)

        if sys.platform.startswith("win32"):
            self.exec(name, "cmd", ["/c", f"call {plan.script}.bat"], cwd)
        else:
            self.exec(name, "sh", [f"{plan.script}.sh"], cwd)

all_checks = []

def check(shared=False):
    def inner(fn: Callable) -> Callable:
        name = f"{fn.__module__}.{fn.__name__}"
        if name.startswith("blip."):
            name = name[5:]
        prefix = name.split(".")
        if shared:
            prefix = prefix[:-1]
        check = Check(name, prefix, fn)
        all_checks.append(check)
        return fn
    return inner

def use_asserts(p: Platform):
    return not p
