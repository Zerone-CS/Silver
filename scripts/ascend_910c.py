#!/usr/bin/env python3
"""Docker deployment and observation for a single device on an Ascend 910C host."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
from dataclasses import dataclass
import time
from urllib.request import Request, urlopen

SYSTEM_MOUNTS = {
    "/usr/local/Ascend/driver": "/usr/local/Ascend/driver",
    "/usr/local/dcmi": "/usr/local/dcmi",
    "/usr/local/bin/npu-smi": "/usr/local/bin/npu-smi",
}
MODEL_FILES = (
    "config.json", "configuration.json", "generation_config.json", "model.safetensors",
    "tokenizer.json", "tokenizer_config.json", "vocab.json",
)
PROFILER = {"profiler": "torch", "torch_profiler_dir": "/profiles", "torch_profiler_with_stack": False,
            "ignore_frontend": True, "torch_profiler_use_gzip": False}


@dataclass(frozen=True)
class SiteConfig:
    image: str
    model_dir: Path
    model: str
    port: int
    card: int
    die: int
    chip: int
    sibling_dies: tuple
    packages: dict
    lock_dir: Path

    @classmethod
    def load(cls, path):
        path = Path(path)
        doc = read(path)
        expected = {"schema_version", "image", "model_dir", "model", "port", "card",
                    "die", "chip", "sibling_dies", "packages", "lock_dir"}
        require(set(doc) == expected and doc["schema_version"] == 1, "Invalid site configuration")
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", doc["image"]), "Pin the Docker image by image ID")
        require(re.fullmatch(r"[a-zA-Z0-9_.-]+", doc["model"]), "Invalid served model name")
        require(type(doc["port"]) is int and 1024 <= doc["port"] <= 65535, "Invalid local port")
        require(all(type(doc[k]) is int and doc[k] >= 0 for k in ("card", "die", "chip")),
                "Invalid physical device")
        siblings = tuple(doc["sibling_dies"])
        require(doc["chip"] in (0, 1) and len(siblings) == 1 and
                all(type(die) is int and die >= 0 and die != doc["die"] for die in siblings),
                "Supply the other die on the 910C card")
        require(set(doc["packages"]) == {"vllm", "vllm-ascend", "torch", "torch-npu"}
                and all(isinstance(v, str) and v for v in doc["packages"].values()),
                "Pin the installed package versions")
        paths = (Path(doc["model_dir"]), Path(doc["lock_dir"]))
        require(all(p.is_absolute() and ":" not in str(p) for p in paths), "Supply absolute host paths")
        return cls(doc["image"], paths[0], doc["model"], doc["port"], doc["card"],
                   doc["die"], doc["chip"], siblings, doc["packages"], paths[1])

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def docker_devices(self):
        return [f"/dev/davinci{self.die}", "/dev/davinci_manager", "/dev/devmm_svm", "/dev/hisi_hdc"]

    @property
    def lock_paths(self):
        return [self.lock_dir / f"inferopt-910c-device{die}.lock"
                for die in sorted((self.die, *self.sibling_dies))]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read(path):
    return json.loads(path.read_bytes())


def write(path, value):
    with path.open("x", encoding="utf-8") as f:
        f.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def utc():
    return datetime.now(timezone.utc).isoformat()


def cmd(*args, timeout=60):
    result = subprocess.run(list(map(str, args)), text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} exited {result.returncode}: {result.stderr[-1400:]}")
    return result.stdout


def inspect(cid):
    result = json.loads(cmd("docker", "inspect", cid))
    require(len(result) == 1 and result[0]["Id"] == cid, "Docker identity mismatch")
    return result[0]


def optional_container(name):
    ids = cmd("docker", "ps", "-aq", "--no-trunc", "--filter", f"name=^/{name}$").split()
    require(len(ids) <= 1, "More than one container matched reserved name")
    return inspect(ids[0]) if ids else None


def visible_devices(value):
    devices = set()
    for part in value.split(","):
        match = re.fullmatch(r"(\d{1,3})(?:-(\d{1,3}))?", part.strip())
        if match:
            first = int(match[1])
            last = int(match[2] or match[1])
            devices.update(range(first, last + 1))
    return devices


def verify_container_reservation(settings):
    # The workload can reserve a mount namespace before opening its device.
    # npu-smi and fuser can both report idle in that condition.
    ids = cmd("docker", "ps", "-q", "--no-trunc").split()
    containers = json.loads(cmd("docker", "inspect", *ids)) if ids else []
    device = f"/dev/davinci{settings.die}"
    reservations = []
    for item in containers:
        if not item["State"]["Running"]:
            continue
        config = item["HostConfig"]
        sources = {entry["PathOnHost"] for entry in config.get("Devices") or []}
        sources.update(mount["Source"] for mount in item.get("Mounts") or [])
        asc = next((entry.partition("=")[2] for entry in (item["Config"].get("Env") or [])
                    if entry.startswith("ASCEND_VISIBLE_DEVICES=")), "")
        if device in sources or (config.get("Runtime") == "ascend" and
                                 (asc == "all" or settings.die in visible_devices(asc))):
            reservations.append(item["Name"])
    require(not reservations, f"Device {settings.die} is reserved by a live Docker container: {', '.join(reservations)}")


def http(base_url, path, payload=None, timeout=5):
    raw = json.dumps(payload).encode() if payload is not None else None
    req = Request(base_url + path, data=raw, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=timeout) as response:
        return json.load(response)


def card_snapshot(settings, owned_pids, running):
    card = settings.card
    npu = cmd("npu-smi", "info")
    health = cmd("npu-smi", "info", "-t", "health", "-i", card)
    require(len(re.findall(r"^\s*Health\s*:\s*OK\s*$", health, re.M)) == 3,
            f"Card {card} health does not confirm two healthy dies and MCU")
    require("Process id" in npu, "No NPU process table")
    process_table = npu[npu.index("Process id"):]
    processes = {int(p): int(chip)
                 for chip, p in re.findall(rf"^\|\s*{card}\s+(\d+)\s*\|\s*(\d+)\s*\|", process_table, re.M)}
    idle = bool(re.search(rf"No running processes found in NPU\s+{card}\b", process_table))
    require(processes or idle, f"Card {card} missing from NPU process table")
    if running:
        require(processes and set(processes).issubset(owned_pids) and set(processes.values()) == {settings.chip},
                f"Card {card} has a foreign process, a process on the sibling die, or no live owned engine: {processes}")
    else:
        require(not processes and idle, f"Card {card} is already in use: {processes}")
        fuser = subprocess.run(["fuser", *[f"/dev/davinci{die}" for die in (settings.die, *settings.sibling_dies)]],
                               capture_output=True, text=True, timeout=12)
        require(fuser.returncode == 1 and not (fuser.stdout + fuser.stderr).strip(),
                f"Card device file is in use or fuser failed: {fuser.stdout}{fuser.stderr}")
    return {"captured_at": utc(), "npu_smi": npu, "health": health,
            "processes_on_card": processes, "docker_host_pids": sorted(owned_pids)}


class Site:
    def __init__(self, root, settings, template=None):
        self.root = Path(root).resolve(strict=True)
        self.template = Path(template or (self.root / "cases/no-think.jinja")).resolve(strict=True)
        require(":" not in str(self.template) and ":" not in str(self.root), "Invalid Docker mount path")
        self.settings = settings
        self.model = settings.model
        self.base_url = settings.base_url
        self.name = "inferopt-910c-" + hashlib.sha256(str(self.root).encode()).hexdigest()[:16]
        self.owner = hashlib.sha256(str(self.root).encode()).hexdigest()

    def initialize(self):
        settings = self.settings
        require(optional_container(self.name) is None, "Site already has a container")
        verify_container_reservation(settings)
        card = card_snapshot(self.settings, set(), False)
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", settings.port))
        images = json.loads(cmd("docker", "image", "inspect", settings.image))
        require(len(images) == 1 and images[0]["Id"] == settings.image, "Pinned image missing")
        self.root.joinpath("deployments").mkdir(exist_ok=True)
        write(self.root / "preflight.json", {"host": socket.gethostname(), "card": card,
              "container_name": self.name, "image_id": settings.image, "repo_digests": images[0].get("RepoDigests")})
        env = {"host": socket.gethostname(), "physical_device": settings.die, "card": settings.card,
               "image_id": settings.image, "model": settings.model, "packages": settings.packages,
               "model_hashes": {name: sha(settings.model_dir / name) for name in MODEL_FILES},
               "client_python": sys.version, "npu_smi": next((line.strip() for line in card["npu_smi"].splitlines()
                                                            if "npu-smi" in line), None)}
        write(self.root / "environment.json", env)
        common = ["serve", "/model", "--served-model-name", settings.model, "--host", "0.0.0.0", "--port", "8000",
                  "--enforce-eager", "--dtype", "bfloat16", "--max-model-len", "512",
                  "--gpu-memory-utilization", "0.30", "--no-enable-prefix-caching",
                  "--chat-template", "/site/no-think.jinja",
                  "--profiler-config", json.dumps(PROFILER, sort_keys=True, separators=(",", ":"))]
        for side, n in (("A", 4), ("B", 8)):
            write(self.config(side), {"image_id": settings.image, "argv": [*common, "--max-num-seqs", str(n)],
                                     "environment": {"OMP_NUM_THREADS": "4"},
                                     "chat_template_sha256": sha(self.template)})

    def config(self, side):
        require(re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,45}", side), "Invalid configuration key")
        return self.root / f"config-{side}.json"

    @contextmanager
    def deployment(self, side, label):
        require(re.fullmatch(r"[a-zA-Z0-9_-]+", label), "Invalid deployment label")
        settings = self.settings
        config = self.config(side)
        directory = self.root / "deployments" / label
        directory.mkdir()
        profile = directory / "profiles"
        profile.mkdir()
        write(directory / "prelaunch-card.json", card_snapshot(self.settings, set(), False))
        require(optional_container(self.name) is None, "Refusing to replace existing container")
        plan = read(config)
        mounts = {**SYSTEM_MOUNTS, "/model": str(settings.model_dir), "/site/no-think.jinja": str(self.template)}
        argv = ["docker", "run", "--pull=never", "-d", "--name", self.name, "--label",
                f"inferopt.owner={self.owner}", "--label", f"inferopt.deployment={label}",
                "--shm-size", "4g", "-p", f"127.0.0.1:{settings.port}:8000"]
        for device in settings.docker_devices:
            argv.extend(["--device", device])
        for target, src in mounts.items():
            argv.extend(["-v", f"{src}:{target}:ro"])
        argv.extend(["-v", f"{profile}:/profiles", "-e", "OMP_NUM_THREADS=4", "--entrypoint", "vllm", settings.image, *plan["argv"]])
        write(directory / "launch.json", {"time": utc(), "command": argv, "side": side})
        try:
            cid = cmd(*argv, timeout=90).strip()
            require(re.fullmatch("[0-9a-f]{64}", cid), "Invalid container ID")
            write(directory / "container.json", {"id": cid, "side": side, "label": label, "name": self.name})
            live = Live(self, directory, cid)
            live.qualify()
            yield live
        finally:
            self.stop(label, directory)

    def stop(self, label, directory):
        item = optional_container(self.name)
        if item is None:
            return
        require(item["Config"]["Labels"].get("inferopt.owner") == self.owner
                and item["Config"]["Labels"].get("inferopt.deployment") == label,
                "Refusing to stop container unrelated to this deployment")
        marker = directory / "container.json"
        cid = item["Id"]
        require(not marker.exists() or read(marker)["id"] == cid, "Container ID changed")
        log = directory / "service.log"
        logs_rc = None
        logs_error = None
        try:
            with log.open("ab") as stream:
                logs_rc = subprocess.run(["docker", "logs", "--timestamps", cid], stdout=stream,
                                         stderr=subprocess.STDOUT, timeout=30).returncode
        except (OSError, subprocess.TimeoutExpired) as error:
            logs_error = str(error)
        if item["State"]["Running"]:
            cmd("docker", "stop", "-t", "30", cid, timeout=60)
        stopped = inspect(cid)["State"]
        require(not stopped["Running"], "Owned test container failed to stop")
        cmd("docker", "rm", cid)
        receipt = {"captured_at": utc(), "id": cid, "stopped": stopped, "logs_returncode": logs_rc,
                   "log_capture_error": logs_error}
        try:
            receipt["card"] = card_snapshot(self.settings, set(), False)
        except Exception as error:
            receipt["card_snapshot_error"] = str(error)
            raise
        finally:
            write(directory / "released.json", receipt)


class Live:
    def __init__(self, site, directory, cid=None):
        self.site = site
        self.directory = Path(directory).resolve(strict=True)
        require(self.directory.parent == site.root / "deployments", "Unexpected deployment directory")
        doc = read(self.directory / "container.json")
        self.cid = cid or doc["id"]
        self.label = doc["label"]
        self.side = doc["side"]
        self.settings = site.settings
        self.config = site.config(self.side)
        self.evidence = self.directory / "config-evidence.json"
        self.export_root = self.directory / "profiles"

    def details(self):
        item = inspect(self.cid)
        require(item["State"]["Running"] and item["State"]["Pid"] > 0, "Owned container is not running")
        require(item["Config"]["Labels"].get("inferopt.owner") == self.site.owner
                and item["Config"]["Labels"].get("inferopt.deployment") == self.label
                and item["Name"] == "/" + self.site.name, "Running container has foreign labels or name")
        require(item["Path"].endswith("vllm"), "Container entrypoint is unexpected")
        require([x["PathOnHost"] for x in item["HostConfig"]["Devices"]] == self.settings.docker_devices, "Container devices changed")
        port = item["NetworkSettings"]["Ports"].get("8000/tcp", [])
        require(port == [{"HostIp": "127.0.0.1", "HostPort": str(self.settings.port)}], "Container binding changed")
        devices = item["HostConfig"]["Devices"]
        require(not item["HostConfig"]["Privileged"] and all(x["PathInContainer"] == x["PathOnHost"] for x in devices),
                "Unexpected container privileges")
        mounts = {x["Destination"]: x for x in item["Mounts"]}
        for dest, path in {"/model": self.settings.model_dir, "/site/no-think.jinja": self.site.template}.items():
            require(not mounts[dest]["RW"] and Path(mounts[dest]["Source"]).resolve() == path.resolve(),
                    f"Deployment mount mismatch for {dest}")
        effective = {"image_id": item["Image"], "argv": item["Config"]["Cmd"],
                     "environment": {"OMP_NUM_THREADS": next(x.split("=", 1)[1] for x in item["Config"]["Env"]
                                                             if x.startswith("OMP_NUM_THREADS="))},
                     "chat_template_sha256": sha(self.site.template)}
        require(effective == read(self.config), "Live launch configuration differs from frozen plan")
        ident = {"schema_version": 1, "instance_id": f"{self.cid}:{item['State']['StartedAt']}:{item['State']['Pid']}",
                 "base_url": self.site.base_url, "model": self.site.model, "effective_config": effective, "implementation_sha256": None}
        return ident

    def assert_isolated(self, label=None):
        ident = self.details()
        processes = cmd("docker", "top", self.cid, "-eo", "pid,ppid,lstart,args")
        pids = {int(line.split()[0]) for line in processes.splitlines()[1:] if line.strip()}
        require(bool(pids), "Docker process list unexpectedly empty")
        result = {"observation": ident, "docker_top": processes, **card_snapshot(self.settings, pids, True)}
        if label is not None:
            require(re.fullmatch(r"[a-zA-Z0-9_-]+", label), "Invalid snapshot label")
            write(self.directory / f"device-{label}.json", result)
        return result

    def observe(self):
        result = self.assert_isolated()
        require(any(item["id"] == self.site.model and item.get("max_model_len", 512) == 512
                    for item in http(self.site.base_url, "/models")["data"]), "Endpoint does not expose the expected model")
        return result["observation"]

    def qualify(self):
        end = time.monotonic() + 300
        while time.monotonic() < end:
            state = inspect(self.cid)["State"]
            require(state["Running"], f"Service stopped before readiness: {state}")
            try:
                models = http(self.site.base_url, "/models")
                if any(item["id"] == self.site.model for item in models["data"]):
                    break
            except OSError:
                pass
            time.sleep(2)
        else:
            raise RuntimeError("Timed out waiting for Qwen service")
        observation = self.observe()
        result = subprocess.run(["docker", "logs", self.cid], capture_output=True, text=True, timeout=30, check=True)
        logs = result.stdout + result.stderr
        with (self.directory / "startup.log").open("x") as stream:
            stream.write(logs)
        # Verify the API startup's parsed arguments and the initialized engine flags.
        argv = read(self.config)["argv"]
        seq = argv[argv.index("--max-num-seqs") + 1]
        prefix = "enable_prefix_caching=True" if "--enable-prefix-caching" in read(self.config)["argv"] else "enable_prefix_caching=False"
        for marker in (f"'max_num_seqs': {seq}", prefix, "enforce_eager=True",
                       "dtype=torch.bfloat16", "/site/no-think.jinja"):
            require(marker in logs, f"Startup log lacks verified configuration marker: {marker}")
        require("Profiler started" not in logs, "Profiler started before diagnostic bracket")
        code = ("import importlib.metadata as m,json;print(json.dumps({n:m.version(n)"
                " for n in ('vllm','vllm-ascend','torch','torch-npu')}))")
        packages = json.loads(cmd("docker", "exec", self.cid, "python3", "-c", code).splitlines()[-1])
        require(packages == self.settings.packages, "Unexpected package versions in pinned image")
        write(self.directory / "packages.json", packages)
        write(self.directory / "models.json", models)
        write(self.directory / "observation.json", observation)
        write(self.evidence, {"verified": True, "scope": "Live container ID, image digest, argv, template mount, parsed startup settings and model endpoint",
                              "observation": "observation.json",
                              "evidence": ["observation.json", "startup.log", "models.json", "packages.json"]})

    def warmup(self, dataset, concurrency, rounds=1):
        jobs = [json.loads(line) for line in Path(dataset).read_text().splitlines() if line.strip()]
        require(jobs and concurrency >= 1 and rounds >= 1, "Invalid warmup workload")
        results = []
        def infer(job):
            reply = http(self.site.base_url, "/chat/completions", {"model": self.site.model, "stream": False, "temperature": 0,
                         "seed": 42, "max_tokens": job["output_tokens"],
                         "messages": [{"role": "user", "content": job["prompt_text"]}]}, timeout=80)
            require(reply.get("model") == self.site.model and reply.get("choices"), "Warmup did not complete")
            return {"request_id": job["request_id"], "response_id": reply["id"], "usage": reply.get("usage"),
                    "finish_reason": reply["choices"][0].get("finish_reason")}
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for _ in range(rounds):
                results.append(list(pool.map(infer, jobs[:concurrency])))
        write(self.directory / f"warmup-{time.time_ns()}.json", {"dataset": str(dataset), "concurrency": concurrency,
                                                              "captured_at": utc(), "results": results})
        return results

