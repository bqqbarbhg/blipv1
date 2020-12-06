from dataclasses import dataclass
from typing import List, Iterable
from blip.build import Builder

@dataclass
class Task:
    name: str
    mode: str
    depth: int
    engines: List[str]
    multiclock: bool = False

def verify(bld: Builder, sby_name: str, il_path: str, task: Task):

    with bld.temp_open(sby_name) as f:
        print("[options]", file=f)
        multiclock = ["off", "on"][task.multiclock]
        print(f"mode {task.mode}", file=f)
        print(f"depth {task.depth}", file=f)
        print(f"multiclock {multiclock}", file=f)
        print("[engines]", file=f)
        print(" ".join(task.engines), file=f)
        print("[script]", file=f)
        print(f"read_ilang {il_path}", file=f)
        print("prep -top top", file=f)
        print("[files]", file=f)
        print(f"{il_path}", file=f)

    bld.exec(task.name, "sby", [sby_name])

def verify_multi(bld: Builder, sby_name: str, il_path: str, tasks: Iterable[Task]):

    with bld.temp_open(sby_name) as f:
        print("[tasks]", file=f)
        for task in tasks:
            print(f"{task.name}", file=f)
        print("[options]", file=f)
        for task in tasks:
            multiclock = ["off", "on"][task.multiclock]
            print(f"{task.name}: mode {task.mode}", file=f)
            print(f"{task.name}: depth {task.depth}", file=f)
            print(f"{task.name}: multiclock {multiclock}", file=f)
        print("[engines]", file=f)
        for task in tasks:
            print(f"{task.name}:" + " ".join(task.engines), file=f)
        print("[script]", file=f)
        print(f"read_ilang {il_path}", file=f)
        print("prep -top top", file=f)
        print("[files]", file=f)
        print(f"{il_path}", file=f)

    bld.exec("sby", "sby", [sby_name])

