from pathlib import Path
import fcntl
import json
import os
import shutil
import subprocess
import threading
import time
import uuid

def mlx_bus():
    matches = {}
    for adapter in Path("/sys/bus/i2c/devices").glob("i2c-*"):
        parent = adapter / "device"
        controller = parent.resolve() if parent.exists() else adapter.resolve().parent
        for link in (adapter / "of_node", controller / "of_node"):
            if link.exists() and link.resolve().name == "i2c@2020000":
                node = link.resolve()
                hz = int.from_bytes((node / "clock-frequency").read_bytes(), "big")
                if hz != 100000:
                    raise RuntimeError(f"測溫匯流排時脈不是先前的 100000 Hz：{hz}")
                matches[int(adapter.name.split("-")[1])] = node
    if len(matches) != 1:
        raise RuntimeError("無法唯一找到 P9.19/P9.20 的測溫匯流排。")
    return next(iter(matches))

class SensorFusion:
    def __init__(self, folder):
        self.bus = mlx_bus()
        self.i2cget = shutil.which("i2cget")
        if not self.i2cget:
            raise RuntimeError("找不到已安裝的 i2cget。")

        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.guard = (self.folder / "sensors.lock").open("a")
        try:
            fcntl.flock(self.guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.guard.close()
            raise

        self.run_id = uuid.uuid4().hex
        self.boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        self.mutex = threading.Lock()
        self.stop = threading.Event()
        self.temp = {"valid": False, "error": "尚未取得溫度"}
        self.previous = None
        self.last_publish = 0
        self.last_log = 0
        self.event_id = 0
        self.last_event = None
        self.log = (self.folder / ("sensors-" + self.run_id[:12] + ".jsonl")).open(
            "x", encoding="utf-8", buffering=1
        )

        self.invalidate("zeroing")
        self.worker = threading.Thread(target=self.temperature_loop, daemon=True)
        self.worker.start()
        print(f"溫度來源：/dev/i2c-{self.bus}，位址 0x5A，啟用 PEC")
        print("即時資料：", self.folder / "status.json")

    def read_temperature(self, register):
        result = subprocess.run(
            [self.i2cget, "-y", str(self.bus), "0x5a", hex(register), "wp"],
            capture_output=True, text=True, timeout=1.0
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip()[-200:] or "溫度讀取失敗")
        word = int(result.stdout.strip(), 16)
        if not (0 < word < 0x8000):
            raise RuntimeError("溫度讀值無效或感測器回報錯誤")
        return round(word * 0.02 - 273.15, 2)

    def temperature_loop(self):
        while not self.stop.is_set():
            try:
                ambient = self.read_temperature(0x06)
                object_c = self.read_temperature(0x07)
                value = {
                    "valid": True,
                    "ambient_c": ambient,
                    "object_c": object_c,
                    "host_received_ns": time.monotonic_ns(),
                    "error": None
                }
            except Exception as exc:
                value = {"valid": False, "error": str(exc)}
            with self.mutex:
                self.temp = value
            self.stop.wait(0.5)

    def temperature(self):
        with self.mutex:
            value = dict(self.temp)
        if value.get("valid"):
            age = (time.monotonic_ns() - value["host_received_ns"]) / 1e6
            value["age_ms"] = round(age, 1)
            if age > 2000:
                value = {
                    "valid": False,
                    "error": "溫度資料已逾時",
                    "age_ms": age
                }
        return value

    def publish(self, presence, phase, force_log=False):
        now = time.monotonic_ns()
        record = {
            "schema": 1,
            "run_id": self.run_id,
            "boot_id": self.boot_id,
            "host_monotonic_ns": now,
            "host_unix_ns": time.time_ns(),
            "phase": phase,
            "presence": presence,
            "temperature": self.temperature(),
            "last_event": self.last_event
        }
        text = json.dumps(record, ensure_ascii=False, allow_nan=False)
        temporary = self.folder / ("status." + self.run_id + ".tmp")
        temporary.write_text(text + "\n", encoding="utf-8")
        os.replace(temporary, self.folder / "status.json")

        if force_log or now - self.last_log >= 1_000_000_000:
            self.log.write(text + "\n")
            self.last_log = now

    def invalidate(self, phase):
        self.previous = None
        self.last_event = None
        self.publish({"valid": False, "present": None}, phase, True)

    def update(self, present, delta, seq, r5_us):
        now = time.monotonic_ns()
        changed = self.previous is not None and present != self.previous
        if changed:
            self.event_id += 1
            self.last_event = {
                "id": self.event_id,
                "kind": "load_on" if present else "load_off",
                "host_monotonic_ns": now
            }
            print("事件：", "秤台有物品" if present else "秤台空秤")

        if self.previous is None or changed or now - self.last_publish >= 200_000_000:
            self.publish({
                "valid": True,
                "present": bool(present),
                "delta_counts": delta,
                "r5_seq": seq,
                "r5_time_us": r5_us,
                "host_updated_ns": now
            }, "running", changed)
            self.last_publish = now

        self.previous = present

    def describe(self):
        value = self.temperature()
        if not value.get("valid"):
            return "溫度暫不可用：" + value.get("error", "未知原因")
        return (
            f"視野表面={value['object_c']:.2f} °C"
            f"｜感測器自身={value['ambient_c']:.2f} °C"
        )

    def close(self):
        self.stop.set()
        self.worker.join(timeout=3)
        with self.mutex:
            self.temp = {"valid": False, "error": "感測程式已停止"}
        try:
            self.invalidate("stopped")
        finally:
            self.log.close()
            self.guard.close()
