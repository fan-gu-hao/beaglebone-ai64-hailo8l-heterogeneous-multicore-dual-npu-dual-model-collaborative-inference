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

ROOT = Path.home() / 'ti/bbai64-bringup/pose-mobilenetv2-v1'
BASE_SHA = 'fc4f835f0c17197cd957ee414deb2374de07e44a928d8de22ad05deb5c655a2e'

def need(ok, message):
    if not ok:
        raise RuntimeError(message)

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def patch_source(old):
    marker = 'uint16_t bb_pose_dispatch('
    need(old.count(marker) == 1,
         '找不到唯一的舊版通訊處理函式。')

    engine = old[:old.index(marker)]
    engine, count = re.subn(
        r'static int bb_test\s*\(\s*void\s*\)',
        'static int bb_run(const uint8_t *input)',
        engine
    )
    need(count == 1, '找不到唯一的單張推論函式。')
    need(engine.count('bb_pose_test_input') == 1,
         '原有影像輸入位置不符。')

    engine = engine.replace('bb_pose_test_input', 'input')
    need('bb_records[i].size<0' not in engine.replace(' ', ''),
         '來源仍包含先前已修正的無號比較。')

    return engine + '#include "bb_pose_transport_v2.inc"\n'

def main():
    need(platform.machine() == 'x86_64',
         '請在 Ubuntu 筆電執行。')

    state = json.loads(
        (ROOT / 'reports/c71-runtime-work.json').read_text()
    )
    work, pkg, sdk, package = [
        Path(state[k]).resolve()
        for k in ('directory', 'pdk', 'sdk', 'model_package')
    ]

    need(work.parent == (ROOT / 'runtime').resolve()
         and work.name.startswith('c71.'),
         '工作目錄不符。')
    need(pkg == work / 'pdk/packages',
         '不是本次開發套件副本。')

    guard = (work / '.tidl-link.lock').open('a')
    fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)

    old_report = ROOT / 'reports/c71-pose-selftest-build.json'
    previous = json.loads(old_report.read_text())
    need(
        previous.get('build_passed') is True
        and previous['sha256'] == BASE_SHA
        and sha(Path(previous['firmware'])) == BASE_SHA,
        '已通過硬體測試的第一版韌體或紀錄不符。'
    )

    src = pkg / 'ti/drv/ipc/examples/common/src'
    native = ROOT / 'bb_pose_service.c'
    copied = src / native.name
    include = ROOT / 'bb_pose_transport_v2.inc'

    need(include.is_file()
         and 'C71_TIDL_POSE_V2' in include.read_text(),
         '請先建立新版通訊程式。')
    need(native.read_bytes() == copied.read_bytes(),
         '兩份推論來源不一致。')

    backup = work / 'source-before-live-v2'
    baseline = backup / native.name

    if not baseline.exists():
        need('C71_TIDL_POSE_V1' in native.read_text(),
             '目前來源不是第一版。')
        backup.mkdir(exist_ok=True)
        shutil.copy2(native, baseline)
        shutil.copy2(old_report, backup / old_report.name)
        shutil.copy2(
            src / 'bb_pose_assets.h',
            backup / 'bb_pose_assets.h'
        )

    revised = patch_source(baseline.read_text())
    need(native.read_text() in (baseline.read_text(), revised),
         '來源已有其他修改，停止覆寫。')

    for key, name in [
        ('network', 'network.bin'),
        ('io', 'io.bin'),
        ('test_input', 'test_input_rgb_chw_u8.bin'),
    ]:
        need(sha(package / name) == previous['asset_sha256'][key],
             '模型套件已改變：' + name)

    need(
        (src / 'bb_pose_assets.h').read_bytes()
        == (backup / 'bb_pose_assets.h').read_bytes(),
        '模型介面標頭已改變。'
    )

    report_path = ROOT / 'reports/c71-pose-v2-build.json'
    report_path.write_text('{"build_passed": false}\n')

    shutil.copy2(include, src / include.name)
    native.write_text(revised)
    copied.write_text(revised)

    spec = importlib.util.spec_from_file_location(
        'link_build', ROOT / 'build_c71_tidl_link.py'
    )
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)

    cgt = sdk / 'ti-cgt-c7000_1.4.2.LTS'
    env = os.environ.copy()
    env['C7X_C_DIR'] = str(cgt / 'include') + ';' + str(cgt / 'lib')

    command = [
        'make', '-C', str(pkg / 'ti/build'),
        f'PDK_INSTALL_PATH={pkg}',
        f'SDK_INSTALL_PATH={sdk}',
        f'TOOLS_INSTALL_PATH={sdk}',
        f'C7X_GEN_INSTALL_PATH={cgt}',
        f'BIOS_INSTALL_PATH={sdk / "bios_6_83_02_07"}',
        f'XDC_INSTALL_PATH={sdk / "xdctools_3_61_04_40_core"}',
        'BOARD=j721e_evm',
        'SOC=j721e',
        'CORE=c7x_1',
        'BUILD_OS_TYPE=tirtos',
        'BUILD_PROFILE=release',
        'ENDIAN=little',
        '-j4',
        'ipc_echo_test_tirtos',
    ]

    print('開始建置外部影像版本；沿用既有模型與記憶體配置。',
          flush=True)
    build.run(
        command, work, env,
        work / 'logs/pose-v2-build.log'
    )

    elf = (
        pkg / 'ti/binary/ipc_echo_test_tirtos/bin/j721e_evm'
        / 'ipc_echo_test_tirtos_c7x_1_release.xe71'
    )
    data = elf.read_bytes()

    need(
        data[:6] == b'\x7fELF\x02\x01'
        and struct.unpack_from('<HH', data, 16) == (2, 145),
        '不是 C71 執行檔。'
    )
    need(b'C71_TIDL_POSE_V2' in data,
         '執行檔沒有包含新版通訊程式。')

    phoff = struct.unpack_from('<Q', data, 32)[0]
    phsize, phnum = struct.unpack_from('<HH', data, 54)
    segments = []

    for i in range(phnum):
        p = struct.unpack_from(
            '<IIQQQQQQ', data, phoff + i * phsize
        )
        if p[0] != 1 or p[6] == 0:
            continue
        a, b = p[4], p[4] + p[6]
        need(
            any(lo <= a < b <= hi for lo, hi in (
                (0xA8100000, 0xA9000000),
                (0xAC000000, 0xB4000000),
            )),
            f'載入區段越界：0x{a:X}～0x{b:X}'
        )
        need(p[5] <= p[6] and p[2] + p[5] <= len(data),
             '執行檔區段不完整。')
        segments.append({'start': a, 'end': b})

    shoff = struct.unpack_from('<Q', data, 40)[0]
    shsize, shnum, stridx = struct.unpack_from('<HHH', data, 58)
    sections = [
        struct.unpack_from('<IIQQQQIIQQ', data, shoff + i * shsize)
        for i in range(shnum)
    ]
    s = sections[stridx]
    names = data[s[4]:s[4]+s[5]]
    table = {
        names[s[0]:].split(b'\0', 1)[0].decode(): s
        for s in sections
    }

    need(table['.vecs'][3] == 0xA8400000
         and table['.vecs'][5] <= 0x4000,
         '向量表位置不符。')
    need(table['.resource_table'][3] == 0xA8100000,
         '資源表位置不符。')
    need(table['.bb_pose_heap'][5] == 96 * 1024**2,
         '推論記憶體池大小不符。')

    need(
        table['.bb_pose_assets'][5] >= sum(
            (package / name).stat().st_size
            for name in (
                'network.bin', 'io.bin',
                'test_input_rgb_chw_u8.bin',
            )
        ),
        '模型與內建測試圖片未完整連入。'
    )

    for name in ('.bb_pose_assets', '.bb_pose_heap'):
        s = table[name]
        need(0xAC000000 <= s[3] < s[3]+s[5] <= 0xB4000000,
             name + ' 越界。')
        print(f'{name}：0x{s[3]:X}～0x{s[3]+s[5]:X}')

    entry = struct.unpack_from('<Q', data, 24)[0]
    need(0xA8100000 <= entry < 0xA9000000
         and entry % 0x200000 == 0,
         '入口位址不符。')

    output = work / 'c71-pose-v2.xe71'
    shutil.copy2(elf, output)

    report = dict(previous)
    report.update(
        firmware=str(output),
        sha256=sha(output),
        segments=segments,
        hardware_verified=False,
        protocol='C71_TIDL_POSE_V2',
        baseline_sha256=BASE_SHA,
        source_sha256=sha(native),
        transport_sha256=sha(include),
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    )

    print('\nPASS：外部影像接收與關節回傳韌體建置完成。')
    print('韌體：', output)
    print('SHA256：', report['sha256'])
    print('第一版韌體與報告保留；新版尚未在板子執行。')

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        raise SystemExit('\n停止：' + str(exc))
