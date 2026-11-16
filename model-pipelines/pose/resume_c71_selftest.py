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

def need(ok, message):
    if not ok:
        raise RuntimeError(message)

def main():
    need(platform.machine() == 'x86_64', '請在 Ubuntu 筆電執行。')
    state = json.loads((ROOT / 'reports/c71-runtime-work.json').read_text())
    work, pkg, sdk, package = [
        Path(state[k]).resolve()
        for k in ('directory', 'pdk', 'sdk', 'model_package')
    ]
    need(work.parent == (ROOT / 'runtime').resolve()
         and work.name.startswith('c71.'), '工作目錄不符。')
    need(pkg == work / 'pdk/packages', '不是本次開發套件副本。')

    guard = (work / '.tidl-link.lock').open('a')
    fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)

    common = pkg / 'ti/drv/ipc/examples/common'
    src = common / 'src'
    need('BB_POSE_SELFTEST_V1' in (common / 'makefile.mk').read_text(),
         '找不到前次建立的單張推論設定。')
    need((src / 'bb_pose_assets.c').is_file(), '缺少已封裝的模型。')

    pattern = (
        r'if\s*\(\s*bb_records\[i\]\.size\s*<\s*0\s*\|\|'
        r'\s*bb_records\[i\]\.alignment\s*<\s*0\s*\)'
    )
    replacement = (
        'if ((uintptr_t)bb_records[i].size > (uintptr_t)BB_POOL_BYTES ||\n'
        '            (uintptr_t)bb_records[i].alignment > (uintptr_t)BB_POOL_BYTES)'
    )

    for path in (ROOT / 'bb_pose_service.c', src / 'bb_pose_service.c'):
        old = path.read_text()
        if replacement in old:
            continue
        new, count = re.subn(pattern, replacement, old)
        need(count == 1, '找不到唯一的比較式：' + str(path))
        backup = path.with_name(path.name + '.before-size-fix')
        if not backup.exists():
            shutil.copy2(path, backup)
        path.write_text(new)

    need((ROOT / 'bb_pose_service.c').read_bytes()
         == (src / 'bb_pose_service.c').read_bytes(),
         '兩份推論來源不一致。')

    assets = {
        'network': 'network.bin',
        'io': 'io.bin',
        'test_input': 'test_input_rgb_chw_u8.bin',
    }
    blobs = {
        key: (package / name).read_bytes()
        for key, name in assets.items()
    }
    hashes = {
        key: hashlib.sha256(value).hexdigest()
        for key, value in blobs.items()
    }
    need(blobs['network'] == (work / 'network.bin').read_bytes()
         and blobs['io'] == (work / 'io.bin').read_bytes(),
         '模型版本不符。')
    need(hashes['network'] in (src / 'bb_pose_assets.h').read_text(),
         '已封裝的模型版本不符。')

    report_path = ROOT / 'reports/c71-pose-selftest-build.json'
    report_path.write_text(
        '{"build_passed": false, "hardware_verified": false}\n'
    )

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
        'BOARD=j721e_evm', 'SOC=j721e', 'CORE=c7x_1',
        'BUILD_OS_TYPE=tirtos', 'BUILD_PROFILE=release',
        'ENDIAN=little', '-j4', 'ipc_echo_test_tirtos',
    ]

    print('比較式已修正；保留既有目的檔，繼續建置。', flush=True)
    build.run(
        command, work, env,
        work / 'logs/pose-selftest-resume.log'
    )

    elf = (
        pkg / 'ti/binary/ipc_echo_test_tirtos/bin/j721e_evm'
        / 'ipc_echo_test_tirtos_c7x_1_release.xe71'
    )
    data = elf.read_bytes()
    need(data[:6] == b'\x7fELF\x02\x01'
         and struct.unpack_from('<HH', data, 16) == (2, 145),
         '不是 C71 ELF。')

    phoff = struct.unpack_from('<Q', data, 32)[0]
    phsize, phnum = struct.unpack_from('<HH', data, 54)
    segments = []
    for i in range(phnum):
        p = struct.unpack_from('<IIQQQQQQ', data, phoff + i * phsize)
        if p[0] != 1 or p[6] == 0:
            continue
        a, b = p[4], p[4] + p[6]
        need(any(lo <= a < b <= hi for lo, hi in (
            (0xA8100000, 0xA9000000),
            (0xAC000000, 0xB4000000),
        )), f'載入區段越界：0x{a:X}～0x{b:X}')
        need(p[5] <= p[6] and p[2] + p[5] <= len(data),
             'ELF 區段不完整。')
        segments.append({'start': a, 'end': b})

    shoff = struct.unpack_from('<Q', data, 40)[0]
    shsize, shnum, stridx = struct.unpack_from('<HHH', data, 58)
    sections = [
        struct.unpack_from('<IIQQQQIIQQ', data, shoff + i * shsize)
        for i in range(shnum)
    ]
    s = sections[stridx]
    names = data[s[4]:s[4] + s[5]]
    table = {
        names[s[0]:].split(b'\0', 1)[0].decode(): s
        for s in sections
    }

    need(table['.vecs'][3] == 0xA8400000
         and table['.vecs'][5] <= 0x4000, '向量表位置不符。')
    need(table['.resource_table'][3] == 0xA8100000,
         '資源表位置不符。')
    need(table['.bb_pose_heap'][5] >= 96 * 1024**2,
         '推論記憶體池大小不符。')
    need(table['.bb_pose_assets'][5] >= sum(map(len, blobs.values())),
         '模型封裝不完整。')

    for name in ('.bb_pose_assets', '.bb_pose_heap'):
        s = table[name]
        need(0xAC000000 <= s[3] < s[3] + s[5] <= 0xB4000000,
             name + ' 越界。')
        print(f'{name}：0x{s[3]:X}～0x{s[3]+s[5]:X}')

    entry = struct.unpack_from('<Q', data, 24)[0]
    need(0xA8100000 <= entry < 0xA9000000
         and entry % 0x200000 == 0, '入口位址不符。')

    output = work / 'c71-pose-selftest.xe71'
    shutil.copy2(elf, output)
    report = {
        'build_passed': True,
        'hardware_verified': False,
        'firmware': str(output),
        'sha256': hashlib.sha256(data).hexdigest(),
        'model_sha256': hashes['network'],
        'asset_sha256': hashes,
        'segments': segments,
        'flowCtrl': 0,
        'required_extra_ddr': {
            'start': 0xAC000000, 'size': 0x08000000,
        },
        'required_msmc': {
            'start': 0x70020000, 'size': 0x007C8000,
        },
        'requires_board_memory_setup': True,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    )
    print('\nPASS：單張姿態推論韌體建置與區段檢查完成。')
    print('韌體：', output)
    print('SHA256：', report['sha256'])
    print('尚未部署或執行；下一步設定板端記憶體。')

if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, struct.error) as exc:
        raise SystemExit('\n停止：' + str(exc))
