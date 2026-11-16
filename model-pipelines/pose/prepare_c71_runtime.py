from pathlib import Path
import json
import platform
import shutil
import struct
import subprocess
import tempfile

BASE = Path.home() / "ti/bbai64-bringup"
ROOT = BASE / "pose-mobilenetv2-v1"
SDK = (
    BASE / "sdk-08.00.00.12"
    / "ti-processor-sdk-rtos-j721e-evm-08_00_00_12"
)
TIDL = SDK / "tidl_j7_08_00_00_10/ti_dl"

def need(ok, message):
    if not ok:
        raise RuntimeError(message)

def run(args, log):
    result = subprocess.run(
        args, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True, errors="replace"
    )
    log.write_text(result.stdout, encoding="utf-8")
    if result.returncode:
        print(result.stdout[-10000:], flush=True)
        raise RuntimeError(f"指令執行失敗，完整紀錄：{log}")
    return result.stdout

def main():
    need(platform.machine() == "x86_64", "請在 Ubuntu 筆電執行。")
    cc = shutil.which("gcc")
    need(cc, "找不到 gcc。")

    marker = BASE / "ipc-compute-latest"
    if marker.is_symlink():
        original = marker.resolve()
    else:
        original = Path(
            marker.read_text().strip().strip("'\"")
        ).expanduser()
    if not original.is_absolute():
        original = BASE / original

    source = original / "pdk/packages"
    need(
        (source / "ti/build/makefile").is_file(),
        "找不到先前的 PDK。"
    )

    cl7x = SDK / "ti-cgt-c7000_1.4.2.LTS/bin/cl7x"
    header = TIDL / "inc/itidl_ti.h"
    need(cl7x.is_file(), "找不到 C71x 編譯器。")
    need(header.is_file(), "找不到目前 SDK 的 TIDL 介面標頭。")

    package = json.loads(
        (ROOT / "reports/pose-package.json").read_text()
    )
    bundle = Path(package["directory"])
    need(
        (bundle / "network.bin").is_file()
        and (bundle / "io.bin").is_file(),
        "找不到剛才準備的模型資料。"
    )

    parent = ROOT / "runtime"
    parent.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="c71.", dir=parent))
    for name in ("pdk", "src", "logs"):
        (work / name).mkdir()

    print("建立 C71x 工作目錄：", work, flush=True)
    print("正在複製既有開發環境……", flush=True)
    subprocess.run(
        ["cp", "-a", "--reflink=auto",
         str(source), str(work / "pdk/packages")],
        check=True
    )
    for name in (
        "network.bin", "io.bin",
        "preprocess.json", "manifest.json"
    ):
        shutil.copy2(bundle / name, work / name)

    state = {
        "directory": str(work),
        "sdk": str(SDK),
        "tidl": str(TIDL),
        "pdk": str(work / "pdk/packages"),
        "original_ipc": str(original),
        "model_package": str(bundle),
        "target_core": "c7x_1",
        "remoteproc_name": "64800000.dsp",
        "api_compile_passed": False,
        "hardware_verified": False,
    }
    state_path = ROOT / "reports/c71-runtime-work.json"
    state_path.write_text(json.dumps(state, indent=2) + "\n")

    inspector = work / "src/read_model.c"
    inspector.write_text(r'''
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include "itidl_ti.h"

static void load(const char *name, void *p, size_t size)
{
    FILE *f = fopen(name, "rb");
    if (!f || fread(p, 1, size, f) != size) {
        fprintf(stderr, "Cannot read model header: %s\n", name);
        exit(2);
    }
    fclose(f);
}

int main(int argc, char **argv)
{
    if (argc != 3) return 2;

    sTIDL_Network_t *n = calloc(1, sizeof(*n));
    sTIDL_IOBufDesc_t *d = calloc(1, sizeof(*d));
    if (!n || !d) return 2;

    load(argv[1], n, sizeof(*n));
    load(argv[2], d, sizeof(*d));

    if (n->netVersion != TIDL_NET_VERSION ||
        d->numInputBuf != 1 || d->numOutputBuf != 1) {
        fprintf(stderr,
                "Version or tensor count mismatch: %x %d %d\n",
                n->netVersion, d->numInputBuf, d->numOutputBuf);
        return 3;
    }

    printf("{\n\"net_version\":\"0x%08x\",\"layers\":%d,"
           "\"dataflow_offset\":%d,\"network_header_bytes\":%zu,"
           "\"io_header_bytes\":%zu,\n",
           n->netVersion, n->numLayers, n->dataFlowInfo,
           sizeof(*n), sizeof(*d));

    printf("\"input\":{\"shape\":[%d,%d,%d,%d],\"type\":%d,"
           "\"layout\":%d,\"pitch\":%d,\"pad_ltrb\":[%d,%d,%d,%d],"
           "\"scale\":%.9g,\"zero_point\":%d,\"data_id\":%d},\n",
           d->inNumBatches[0], d->inNumChannels[0],
           d->inHeight[0], d->inWidth[0],
           d->inElementType[0], d->inLayout[0],
           d->inChannelPitch[0],
           d->inPadL[0], d->inPadT[0],
           d->inPadR[0], d->inPadB[0],
           d->inTensorScale[0], d->inZeroPoint[0],
           d->inDataId[0]);

    printf("\"output\":{\"shape\":[%d,%d,%d,%d],\"type\":%d,"
           "\"layout\":%d,\"pitch\":%d,\"pad_ltrb\":[%d,%d,%d,%d],"
           "\"scale\":%.9g,\"zero_point\":%d,\"data_id\":%d}}\n",
           d->outNumBatches[0], d->outNumChannels[0],
           d->outHeight[0], d->outWidth[0],
           d->outElementType[0], d->outLayout[0],
           d->outChannelPitch[0],
           d->outPadL[0], d->outPadT[0],
           d->outPadR[0], d->outPadB[0],
           d->outTensorScale[0], d->outZeroPoint[0],
           d->outDataId[0]);

    free(n);
    free(d);
    return 0;
}
''', encoding="utf-8")

    exe = work / "read_model"
    run(
        [cc, "-std=c99", "-O2", "-DTIDL_IMPORT_TOOL",
         "-I" + str(header.parent),
         str(inspector), "-o", str(exe)],
        work / "logs/metadata-build.log"
    )
    text = run(
        [str(exe), str(work / "network.bin"),
         str(work / "io.bin")],
        work / "logs/metadata.log"
    )
    metadata = json.loads(text)
    state["model_interface"] = metadata

    print("\n=== 直接讀取模型介面 ===\n" + text, flush=True)
    need(
        metadata["dataflow_offset"] > 0,
        "模型缺少硬體資料流資訊。"
    )

    api = work / "src/bb_pose_tidl_api.c"
    api.write_text(r'''
#include <string.h>
#include "itidl_ti.h"

const IVISION_Fxns *bb_pose_tidl_functions(void)
{
    return &TIDL_VISION_FXNS;
}

int bb_pose_tidl_defaults(TIDL_CreateParams *p,
                         sTIDL_Network_t *network,
                         void *udma)
{
    int status;
    memset(p, 0, sizeof(*p));
    status = TIDL_createParamsInit(p);
    p->net = network;
    p->udmaDrvObj = udma;
    return status;
}
''', encoding="utf-8")

    ivision = sorted((SDK / "ivision").rglob("ivision.h"))
    need(ivision, "找不到 ivision.h。")
    include = [
        header.parent,
        ivision[0].parent,
        SDK / "xdais_7_24_00_04/packages",
        SDK / "xdctools_3_61_04_40_core/packages",
    ]

    try:
        run(
            [str(cl7x), "--compile_only", "--c99",
             "--obj_directory=" + str(work / "src")]
            + ["--include_path=" + str(p) for p in include]
            + [str(api)],
            work / "logs/c71-api-build.log"
        )
        obj = work / "src/bb_pose_tidl_api.obj"
        need(obj.is_file(), "未產生 C71x 目的檔。")
        elf = obj.read_bytes()
        need(
            elf[:6] == b"\x7fELF\x02\x01"
            and struct.unpack_from("<H", elf, 18)[0] == 145,
            "目的檔的架構不是預期的 C71x。"
        )
        state["api_compile_passed"] = True
        print("PASS：TIDL 介面已編譯成 C71x 目的檔。", flush=True)
    except RuntimeError as exc:
        state["api_compile_error"] = str(exc)
        print("介面編譯未通過；仍繼續整理來源紀錄。", flush=True)

    state_path.write_text(json.dumps(state, indent=2) + "\n")

    selected = set()

    def add(path):
        if path.is_file():
            selected.add(path)

    for path in (
        TIDL / "inc/itidl_ti.h",
        TIDL / "test/src/tidl_tb.c",
        TIDL / "test/src/tidl_tb_utils.c",
        TIDL / "test/src/tidl_tb_utils.h",
        TIDL / "test/src/dsp_sdk/main.c",
        TIDL / "test/src/dsp_sdk/linker.cmd",
        TIDL / "test/src/dsp_sdk/linker_mem_map.cmd",
        TIDL / "test/linker_dsp.cmd",
        TIDL / "test/Makefile",
        TIDL / "test/makefile",
        work / "pdk/packages/ti/drv/ipc/ipc_component.mk",
    ):
        add(path)

    for folder in (
        TIDL / "test/src/dsp_sdk",
        TIDL / "makerules",
        work / "pdk/packages/ti/drv/ipc/examples/common",
        work / "pdk/packages/ti/drv/ipc/examples/echo_test",
    ):
        for p in folder.rglob("*"):
            if (
                p.suffix in (
                    ".c", ".h", ".cfg", ".bld",
                    ".cmd", ".mk", ".mak"
                )
                or p.name.lower() == "makefile"
            ):
                add(p)

    for folder in (
        SDK / "vision_apps",
        work / "pdk/packages/ti/build",
    ):
        for p in folder.rglob("*"):
            if p.name in (
                "app_init.c", "app_mem_map.h", "vx_tidl_target.c"
            ) or (
                "c7" in str(p.relative_to(folder)).lower()
                and p.suffix in (
                    ".cfg", ".cmd", ".bld", ".mak", ".h", ".c"
                )
            ):
                add(p)

    for path in ivision:
        add(path)
    for path in (work / "logs").glob("*.log"):
        add(path)

    report = ROOT / "reports/c71-runtime-context.txt"
    with report.open("w", encoding="utf-8") as output:
        output.write(
            "=== WORKSPACE ===\n"
            + json.dumps(state, indent=2) + "\n"
        )
        for p in sorted(selected):
            if p.stat().st_size > 2 * 1024**2:
                output.write(f"\n=== LARGE FILE: {p} ===\n")
                continue
            output.write(f"\n=== FILE: {p} ===\n")
            output.write(p.read_text(errors="replace") + "\n")

    print(
        "\nPASS：工作目錄、模型介面與 SDK 來源紀錄準備完成。",
        flush=True
    )
    print("來源檔數量：", len(selected), flush=True)
    print("來源紀錄：", report, flush=True)
    print(
        "大小：", round(report.stat().st_size / 1024),
        "KiB", flush=True
    )
    print(
        "目前完成的是介面編譯；還沒有連結或啟動 TIDL 韌體。",
        flush=True
    )

if __name__ == "__main__":
    try:
        main()
    except (
        OSError, RuntimeError, ValueError, KeyError,
        subprocess.CalledProcessError
    ) as exc:
        raise SystemExit(f"\n停止：{exc}")
