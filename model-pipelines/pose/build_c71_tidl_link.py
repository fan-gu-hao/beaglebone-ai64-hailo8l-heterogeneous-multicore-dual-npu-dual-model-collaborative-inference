from pathlib import Path
import fcntl
import hashlib
import json
import os
import platform
import shutil
import struct
import subprocess
import time

ROOT = Path.home() / 'ti/bbai64-bringup/pose-mobilenetv2-v1'

def need(ok, message):
    if not ok:
        raise RuntimeError(message)

def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    )
    temporary.replace(path)

def run(args, cwd, env, log):
    print('\n執行：', ' '.join(str(x) for x in args[:3]), flush=True)
    print('完整紀錄：', log, flush=True)
    with log.open('w') as stream:
        process = subprocess.Popen(
            args, cwd=cwd, env=env,
            stdout=stream, stderr=subprocess.STDOUT
        )
        try:
            while True:
                try:
                    code = process.wait(timeout=20)
                    break
                except subprocess.TimeoutExpired:
                    print('仍在建置，請等待……', flush=True)
        except BaseException:
            process.terminate()
            process.wait()
            raise
    if code:
        print('\n'.join(
            log.read_text(errors='replace').splitlines()[-100:]
        ))
    need(code == 0, f'建置失敗，停止；紀錄：{log}')

def main():
    need(platform.machine() == 'x86_64',
         '請在 Ubuntu 筆電執行。')

    state_path = ROOT / 'reports/c71-runtime-work.json'
    state = json.loads(state_path.read_text())
    work = Path(state['directory']).resolve()
    sdk = Path(state['sdk']).resolve()
    tidl = Path(state['tidl']).resolve()
    pkg = Path(state['pdk']).resolve()

    need(work.parent == (ROOT / 'runtime').resolve()
         and work.name.startswith('c71.'),
         '工作目錄不符。')
    need(pkg == work / 'pdk/packages',
         '開發套件不是本次隔離副本。')

    guard = (work / '.tidl-link.lock').open('a')
    fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)

    save(ROOT / 'reports/c71-tidl-link-report.json', {
        'tidl_link_passed': False,
        'runtime_complete': False,
        'hardware_verified': False
    })

    cgt = sdk / 'ti-cgt-c7000_1.4.2.LTS'
    compiler = cgt / 'bin/cl7x'
    source = work / 'src/bb_pose_tidl_api.c'
    includes = [
        cgt / 'include',
        tidl / 'inc',
        sdk / 'ivision',
        sdk / 'xdais_7_24_00_04/packages',
        sdk / 'xdctools_3_61_04_40_core/packages'
    ]

    need((cgt / 'include/string.h').is_file(),
         'C71 編譯器缺少 string.h。')
    need(compiler.is_file() and source.is_file(),
         '缺少編譯器或既有介面來源。')
    for folder in includes:
        need(folder.is_dir(), f'缺少標頭目錄：{folder}')
    need('bb_pose_tidl_defaults' in source.read_text(),
         '介面來源與預期不同。')

    env = os.environ.copy()
    env['C7X_C_DIR'] = (
        str(cgt / 'include') + ';' + str(cgt / 'lib')
    )
    logs = work / 'logs'
    logs.mkdir(exist_ok=True)

    state['api_compile_passed'] = False
    save(state_path, state)

    run(
        [str(compiler), '--compile_only', '--c99',
         '--obj_directory=' + str(work / 'src')]
        + ['--include_path=' + str(p) for p in includes]
        + [str(source)],
        work, env, logs / 'c71-api-fixed.log'
    )

    obj = work / 'src/bb_pose_tidl_api.obj'
    need(obj.is_file(), '未產生 C71 目的檔。')
    obj_data = obj.read_bytes()
    need(
        obj_data[:6] == b'\x7fELF\x02\x01'
        and struct.unpack_from('<H', obj_data, 18)[0] == 145,
        '目的檔不是 C71 的 64 位元 ELF。'
    )

    state['api_compile_passed'] = True
    state.pop('api_compile_error', None)
    save(state_path, state)
    print('PASS：標頭路徑修正，C71 介面編譯通過。', flush=True)

    makefile = pkg / 'ti/drv/ipc/examples/common/makefile.mk'
    linker = (
        pkg / 'ti/drv/ipc/examples/common/j721e'
        / 'linker_c7x_c7x_1_sysbios.lds'
    )

    context = ROOT / 'reports/c71-runtime-link-context.txt'
    context_files = [
        linker,
        tidl / 'test/src/concerto_common.mak',
        tidl / 'inc/itidl_rt.h'
    ]
    for pattern in ('*tidl_rt*.c', '*tidl_rt*.cpp', '*tidl_rt*.h'):
        context_files.extend(tidl.rglob(pattern))

    with context.open('w') as stream:
        stream.write(
            json.dumps(state, ensure_ascii=False, indent=2) + '\n'
        )
        for path in dict.fromkeys(context_files):
            stream.write(f'\n=== FILE: {path} ===\n')
            if path.is_file():
                stream.write(
                    path.read_text(errors='replace') + '\n'
                )
            else:
                stream.write('不存在\n')
    print('後續執行介面來源：', context, flush=True)

    libraries = [
        tidl / 'lib/dsp/algo/release' / name
        for name in (
            'tidl_algo.lib',
            'tidl_obj_algo.lib',
            'tidl_priv_algo.lib',
            'tidl_custom.lib'
        )
    ]
    libraries += [
        sdk / 'mmalib_02_01_00_07/lib/release' / name
        for name in (
            'common_C7100.lib',
            'mmalib_C7100.lib',
            'mmalib_cn_C7100.lib'
        )
    ]

    for name in ('udma.ae71', 'dmautils.ae71'):
        relative = (
            Path('ti/drv/udma/lib/j721e/c7x_1/release') / name
        )
        candidates = [pkg / relative]
        candidates += [
            p / 'packages' / relative
            for p in sdk.glob('pdk*') if p.is_dir()
        ]
        found = next(
            (p for p in candidates if p.is_file()), None
        )
        need(found is not None, f'找不到 C71 函式庫：{name}')
        libraries.append(found)

    for path in libraries:
        need(path.is_file(), f'缺少函式庫：{path}')
    need(makefile.is_file() and linker.is_file(),
         '找不到既有建置或連結設定。')
    for path in libraries + includes:
        need(not any(c.isspace() for c in str(path)),
             '建置路徑含空白，停止。')

    begin = '# BEGIN BB_POSE_TIDL_LINK_V1'
    end = '# END BB_POSE_TIDL_LINK_V1'
    original = makefile.read_text()

    if begin in original:
        need(original.count(begin) == original.count(end) == 1,
             '既有建置區段不完整。')
        a = original.index(begin)
        b = original.index(end) + len(end)
        original = original[:a] + original[b:]

    anchor = 'include $(MAKERULEDIR)/common.mk'
    need(original.count(anchor) == 1,
         '找不到唯一的建置插入位置。')

    backup = makefile.with_name(
        makefile.name + '.before-pose-tidl'
    )
    if not backup.exists():
        shutil.copy2(makefile, backup)

    shutil.copy2(
        source,
        pkg / 'ti/drv/ipc/examples/common/src' / source.name
    )

    block = '\n'.join([
        begin,
        'ifeq ($(CORE),c7x_1)',
        'ifeq ($(BUILD_OS_TYPE),tirtos)',
        'SRCS_COMMON += bb_pose_tidl_api.c',
        'INCDIR += ' + ' '.join(str(p) for p in includes),
        'LNKFLAGS_LOCAL_c7x_1 += --reread_libs '
        '--undef_sym=TIDL_VISION_FXNS '
        '--undef_sym=bb_pose_tidl_defaults',
        'LNKFLAGS_LOCAL_c7x_1 += ' + ' '.join(
            '--library=' + str(p) for p in libraries
        ),
        'endif',
        'endif',
        end,
        ''
    ])
    makefile.write_text(
        original.replace(anchor, block + '\n' + anchor)
    )

    options = [
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
        'ENDIAN=little'
    ]
    command = ['make', '-C', str(pkg / 'ti/build')] + options

    run(
        command + ['ipc_echo_test_tirtos_clean'],
        work, env, logs / 'tidl-link-clean.log'
    )
    start = time.time()
    run(
        command + ['-j4', 'ipc_echo_test_tirtos'],
        work, env, logs / 'tidl-link-build.log'
    )

    folder = (
        pkg / 'ti/binary/ipc_echo_test_tirtos/bin/j721e_evm'
    )
    candidates = [
        p for p in folder.glob('*c7x_1*release*.xe71')
        if 'strip' not in p.name
        and p.stat().st_mtime >= start - 2
    ]
    need(len(candidates) == 1,
         f'未找到唯一的新韌體：{candidates}')

    elf = candidates[0]
    data = elf.read_bytes()
    need(
        data[:6] == b'\x7fELF\x02\x01'
        and struct.unpack_from('<HH', data, 16) == (2, 145),
        '韌體 ELF 格式不符。'
    )

    phoff = struct.unpack_from('<Q', data, 32)[0]
    phsize, phnum = struct.unpack_from('<HH', data, 54)
    shoff = struct.unpack_from('<Q', data, 40)[0]
    shsize, shnum, stridx = struct.unpack_from('<HHH', data, 58)
    sections = [
        struct.unpack_from('<IIQQQQIIQQ', data, shoff + i * shsize)
        for i in range(shnum)
    ]

    def contents(section):
        return data[section[4]:section[4] + section[5]]

    def name(table, offset):
        return table[offset:].split(b'\0', 1)[0].decode(
            errors='replace'
        )

    names = contents(sections[stridx])
    resource = [
        s for s in sections
        if name(names, s[0]) == '.resource_table'
    ]
    need(len(resource) == 1,
         '未找到唯一的遠端核心資源表。')

    defined = set()
    for section in sections:
        if section[1] == 2:
            strings = contents(sections[section[6]])
            for offset in range(
                section[4],
                section[4] + section[5],
                section[9]
            ):
                symbol = struct.unpack_from(
                    '<IBBHQQ', data, offset
                )
                if symbol[3] != 0:
                    defined.add(name(strings, symbol[0]))

    need(
        {'TIDL_VISION_FXNS', 'bb_pose_tidl_defaults'} <= defined,
        '產物未包含實際的 TIDL 介面，停止。'
    )

    print('\n=== ELF 載入範圍 ===')
    segments = []
    for i in range(phnum):
        p = struct.unpack_from(
            '<IIQQQQQQ', data, phoff + i * phsize
        )
        if p[0] == 1 and p[6]:
            segments.append({
                'start': p[4],
                'end': p[4] + p[6]
            })
            print(f'0x{p[4]:08X} ～ 0x{p[4] + p[6]:08X}')

    output = work / 'c71-tidl-link-check.xe71'
    shutil.copy2(elf, output)

    fits_ddr = all(
        0xA8100000 <= p['start'] < p['end'] <= 0xA9000000
        for p in segments
    )
    report = {
        'firmware': str(output),
        'sha256': hashlib.sha256(data).hexdigest(),
        'api_compile_passed': True,
        'tidl_link_passed': True,
        'resource_table_address': resource[0][3],
        'segments': segments,
        'fits_existing_c71_ddr': fits_ddr,
        'runtime_complete': False,
        'model_loaded': False,
        'hardware_verified': False
    }
    save(ROOT / 'reports/c71-tidl-link-report.json', report)

    print('\nPASS：C71 韌體已連結 TIDL 介面與其相依函式庫。')
    print('韌體：', output)
    print('大小：', round(len(data) / 1024**2, 2), 'MiB')
    print('資源表：', hex(resource[0][3]))
    print('目前 ELF 區段均在既有 C71 DDR 保留區：', fits_ddr)
    print('SHA256：', report['sha256'])
    print('這是連結檢查版本，尚未加入模型載入與推論服務；尚未部署。')
    print('後續來源紀錄：', context)

if __name__ == '__main__':
    try:
        main()
    except (
        OSError, ValueError, KeyError,
        RuntimeError, struct.error
    ) as exc:
        raise SystemExit(f'\n停止：{exc}')
