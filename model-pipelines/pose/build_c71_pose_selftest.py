from pathlib import Path
import fcntl
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import struct

ROOT = Path.home() / "ti/bbai64-bringup/pose-mobilenetv2-v1"

def need(ok, message):
    if not ok:
        raise RuntimeError(message)

def replace_one(text, old, new):
    need(text.count(old) == 1, "找不到唯一的修改位置：" + old[:100])
    return text.replace(old, new, 1)

def main():
    need(platform.machine() == "x86_64", "請在 Ubuntu 筆電執行。")

    state = json.loads(
        (ROOT / "reports/c71-runtime-work.json").read_text())
    work, pkg, sdk, tidl, package = [
        Path(state[k]).resolve()
        for k in ("directory", "pdk", "sdk", "tidl", "model_package")
    ]

    need(
        work.parent == (ROOT / "runtime").resolve()
        and work.name.startswith("c71."),
        "工作目錄不符。")
    need(pkg == work / "pdk/packages",
         "必須使用本次開發套件副本。")
    need(package.parent == (ROOT / "deployment").resolve(),
         "模型套件位置不符。")

    guard = (work / ".tidl-link.lock").open("a")
    fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)

    common = pkg / "ti/drv/ipc/examples/common"
    src = common / "src"
    cgt = sdk / "ti-cgt-c7000_1.4.2.LTS"
    native = ROOT / "bb_pose_service.c"

    need(native.is_file(), "請先建立 bb_pose_service.c。")
    need((tidl / "custom/tidl_custom.h").is_file(),
         "找不到 TI 自訂層介面標頭。")
    need((ROOT / "build_c71_tidl_link.py").is_file(),
         "找不到已使用的連結程式。")

    spec = importlib.util.spec_from_file_location(
        "previous_build", ROOT / "build_c71_tidl_link.py")
    previous = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(previous)

    blobs = {
        "network": (package / "network.bin").read_bytes(),
        "io": (package / "io.bin").read_bytes(),
        "test_input":
            (package / "test_input_rgb_chw_u8.bin").read_bytes(),
    }

    need(3235916 < len(blobs["network"]) < 16 * 1024**2,
         "模型大小不符。")
    need(
        len(blobs["io"]) == 37256
        and len(blobs["test_input"]) == 147456,
        "模型介面或測試圖片大小不符。")
    need(blobs["network"] == (work / "network.bin").read_bytes(),
         "模型與前次介面檢查使用的版本不同。")
    need(blobs["io"] == (work / "io.bin").read_bytes(),
         "模型介面與前次版本不同。")

    hashes = {
        k: hashlib.sha256(v).hexdigest()
        for k, v in blobs.items()
    }
    report_path = ROOT / "reports/c71-pose-selftest-build.json"
    report_path.write_text(json.dumps({
        "build_passed": False,
        "hardware_verified": False,
    }) + "\n")

    edits = {}

    path = common / "j721e/linker_c7x_c7x_1_sysbios.lds"
    text = path.read_text()
    need("BB_POSE_VECTORS" in text,
         "尚未套用已成功的向量表配置。")

    if "BB_POSE_DDR" not in text:
        text, count = re.subn(
            r"(\bMEMORY\s*\{)",
            r"\1\n    BB_POSE_DDR (RW) : "
            r"origin = 0xAC000000, length = 0x08000000",
            text, count=1)
        need(count == 1, "找不到連結器 MEMORY。")

        text, count = re.subn(
            r"(\bSECTIONS\s*\{)",
            r"\1\n    .bb_pose_assets : {} > BB_POSE_DDR, ALIGN(128)\n"
            r"    .bb_pose_heap : {} > BB_POSE_DDR, "
            r"type=NOINIT, ALIGN(128)",
            text, count=1)
        need(count == 1, "找不到連結器 SECTIONS。")

    text, count = re.subn(
        r"(ti_sysbios_family_c7x_Cache_l2Size\s*=\s*)"
        r"(32768|65536)(\s*;)",
        r"\g<1>65536\3", text)
    need(count == 1, "找不到唯一的 L2 快取設定。")
    edits[path] = text

    path = src / "main_tirtos.c"
    text = path.read_text()
    if "bb_pose_early();" not in text:
        text = replace_one(
            text,
            "    IpcCfgClecAccessCtrl(FALSE);",
            "    IpcCfgClecAccessCtrl(FALSE);\n"
            "    { extern void bb_pose_early(void); bb_pose_early(); }")
    edits[path] = text

    path = src / "ipc_testsetup.c"
    text = path.read_text()
    if "bb_pose_dispatch(str, len)" not in text:
        text = replace_one(
            text,
            "#include <ti/drv/ipc/examples/common/src/bb_math_v1.h>",
            "#include <stdint.h>\n"
            "extern uint16_t bb_pose_dispatch(char *, uint16_t);")
        text = replace_one(
            text, "bb_compute(str, len)",
            "(myEndPt == ENDPT_CHRDEV ? "
            "bb_pose_dispatch(str, len) : 0)")
        text = replace_one(
            text, "#define MSGSIZE",
            "#undef IPC_TASK_STACKSIZE\n"
            "#define IPC_TASK_STACKSIZE (64U * 1024U)\n\n"
            "#define MSGSIZE")
    edits[path] = text

    path = pkg / "ti/drv/ipc/examples/echo_test/ipc_echo_test.c"
    text = path.read_text()
    pattern = (
        r"(#ifdef BUILD_C7X_1\s+"
        r"uint32_t selfProcId = IPC_C7X_1;\s+"
        r"uint32_t remoteProc\[\]\s*=\s*\{)"
        r"[^}]+(\};)"
    )
    text, count = re.subn(
        pattern, r"\1\n    IPC_MPU1_0\n\2", text)
    need(count == 1, "無法唯一設定 C71 通訊對象。")

    if "#undef IPC_TASK_STACKSIZE" not in text:
        text = replace_one(
            text, "#define NUM_RESPONDER_TASKS",
            "#undef IPC_TASK_STACKSIZE\n"
            "#define IPC_TASK_STACKSIZE (64U * 1024U)\n\n"
            "#define NUM_RESPONDER_TASKS")
    edits[path] = text

    path = common / "makefile.mk"
    text = path.read_text()
    need("BB_POSE_TIDL_LINK_V1" in text,
         "找不到前次已連結的 TIDL 函式庫設定。")

    begin = "# BEGIN BB_POSE_SELFTEST_V1"
    end = "# END BB_POSE_SELFTEST_V1"
    if begin in text:
        need(text.count(begin) == text.count(end) == 1,
             "既有區段不完整。")
        a = text.index(begin)
        b = text.index(end) + len(end)
        text = text[:a] + text[b:]

    block = "\n".join([
        begin,
        "ifeq ($(CORE),c7x_1)",
        "SRCS_COMMON += bb_pose_service.c bb_pose_assets.c",
        "INCDIR += " + str(tidl / "custom"),
        "LNKFLAGS_LOCAL_c7x_1 += "
        "--undef_sym=bb_pose_dispatch --undef_sym=bb_pose_early",
        "endif",
        end,
        "",
    ])
    text = replace_one(
        text, "include $(MAKERULEDIR)/common.mk",
        block + "\ninclude $(MAKERULEDIR)/common.mk")
    edits[path] = text

    for path, text in edits.items():
        backup = path.with_name(
            path.name + ".before-pose-selftest-v1")
        if not backup.exists():
            shutil.copy2(path, backup)
        path.write_text(text)

    shutil.copy2(native, src / native.name)

    header = [
        "#ifndef BB_POSE_ASSETS_H",
        "#define BB_POSE_ASSETS_H",
        "#include <stdint.h>",
    ]

    print("正在封裝模型及已驗證的單張輸入……", flush=True)
    with (src / "bb_pose_assets.c").open("w") as out:
        out.write('#include "bb_pose_assets.h"\n')
        for name, data in blobs.items():
            symbol = "bb_pose_" + name
            macro = "BB_POSE_" + name.upper() + "_SIZE"
            header += [
                f"#define {macro} {len(data)}U",
                f"extern const uint8_t {symbol}[{macro}];",
            ]
            out.write(
                f'#pragma DATA_SECTION({symbol}, ".bb_pose_assets")\n'
                f"#pragma DATA_ALIGN({symbol}, 128)\n"
                f"const uint8_t {symbol}[{macro}] = {{\n")
            for offset in range(0, len(data), 32):
                out.write(",".join(
                    "0x%02x" % v
                    for v in data[offset:offset + 32]
                ) + ",\n")
            out.write("};\n")

    header += [
        '#define BB_POSE_NETWORK_SHA "' + hashes["network"] + '"',
        "#endif",
        "",
    ]
    (src / "bb_pose_assets.h").write_text("\n".join(header))

    env = os.environ.copy()
    env["C7X_C_DIR"] = (
        str(cgt / "include") + ";" + str(cgt / "lib"))

    options = [
        f"PDK_INSTALL_PATH={pkg}",
        f"SDK_INSTALL_PATH={sdk}",
        f"TOOLS_INSTALL_PATH={sdk}",
        f"C7X_GEN_INSTALL_PATH={cgt}",
        f'BIOS_INSTALL_PATH={sdk / "bios_6_83_02_07"}',
        f'XDC_INSTALL_PATH={sdk / "xdctools_3_61_04_40_core"}',
        "BOARD=j721e_evm",
        "SOC=j721e",
        "CORE=c7x_1",
        "BUILD_OS_TYPE=tirtos",
        "BUILD_PROFILE=release",
        "ENDIAN=little",
    ]
    command = ["make", "-C", str(pkg / "ti/build")] + options

    previous.run(
        command + ["ipc_echo_test_tirtos_clean"],
        work, env, work / "logs/pose-selftest-clean.log")
    previous.run(
        command + ["-j4", "ipc_echo_test_tirtos"],
        work, env, work / "logs/pose-selftest-build.log")

    elf = (
        pkg / "ti/binary/ipc_echo_test_tirtos/bin/j721e_evm"
        / "ipc_echo_test_tirtos_c7x_1_release.xe71")
    data = elf.read_bytes()

    need(
        data[:6] == b"\x7fELF\x02\x01"
        and struct.unpack_from("<HH", data, 16) == (2, 145),
        "不是 C71 ELF。")

    phoff = struct.unpack_from("<Q", data, 32)[0]
    phsize, phnum = struct.unpack_from("<HH", data, 54)
    segments = []

    for i in range(phnum):
        p = struct.unpack_from(
            "<IIQQQQQQ", data, phoff + i * phsize)
        if p[0] != 1 or p[6] == 0:
            continue

        a, b = p[4], p[4] + p[6]
        need(
            any(lo <= a < b <= hi for lo, hi in (
                (0xA8100000, 0xA9000000),
                (0xAC000000, 0xB4000000),
            )),
            "ELF 載入區段超出規劃範圍。")
        need(
            p[5] <= p[6] and p[2] + p[5] <= len(data),
            "ELF 檔案區段不完整。")
        segments.append({"start": a, "end": b})

    need(
        any(p["start"] >= 0xAC000000 for p in segments),
        "沒有模型與工作記憶體區段。")

    shoff = struct.unpack_from("<Q", data, 40)[0]
    shsize, shnum, stridx = struct.unpack_from("<HHH", data, 58)
    sections = [
        struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shsize)
        for i in range(shnum)
    ]
    strings = sections[stridx]
    names = data[strings[4]:strings[4] + strings[5]]
    section_map = {
        names[s[0]:].split(b"\0", 1)[0].decode(): s
        for s in sections
    }

    need(
        section_map[".vecs"][3] == 0xA8400000
        and section_map[".vecs"][5] <= 0x4000,
        "向量表位置不符。")
    need(
        section_map[".resource_table"][3] == 0xA8100000,
        "資源表位置不符。")
    need(
        section_map[".bb_pose_heap"][5] >= 96 * 1024**2,
        "推論記憶體池大小不符。")

    for key in (".bb_pose_assets", ".bb_pose_heap"):
        sec = section_map[key]
        need(
            0xAC000000 <= sec[3] < sec[3] + sec[5] <= 0xB4000000,
            "新增區段越界：" + key)

    entry = struct.unpack_from("<Q", data, 24)[0]
    need(
        0xA8100000 <= entry < 0xA9000000
        and entry % 0x200000 == 0,
        "C71 入口位址或對齊不符。")

    assets = section_map[".bb_pose_assets"]
    need(
        assets[5] >= sum(map(len, blobs.values())),
        "模型與測試圖片未完整連入。")

    output = work / "c71-pose-selftest.xe71"
    shutil.copy2(elf, output)

    report = {
        "build_passed": True,
        "hardware_verified": False,
        "firmware": str(output),
        "sha256": hashlib.sha256(data).hexdigest(),
        "model_sha256": hashes["network"],
        "asset_sha256": hashes,
        "segments": segments,
        "flowCtrl": 0,
        "required_extra_ddr": {
            "start": 0xAC000000,
            "size": 0x08000000,
        },
        "required_msmc": {
            "start": 0x70020000,
            "size": 0x007C8000,
        },
        "requires_board_memory_setup": True,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    print("\nPASS：單張姿態推論韌體已完成建置。")
    print("韌體：", output)
    print("SHA256：", report["sha256"])
    print("模型、驗證圖片、TI 推論呼叫及輸出讀回介面均已連入。")
    print("新增 DDR 規劃：0xAC000000～0xB4000000，128 MiB。")
    print("尚未部署；須先核對板子的記憶體配置並完成保留設定。")

if __name__ == "__main__":
    try:
        main()
    except (
        OSError, RuntimeError, ValueError, KeyError, struct.error
    ) as exc:
        raise SystemExit("\n停止：" + str(exc))
