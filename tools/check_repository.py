"""Read-only integrity/syntax/link checks; never import board applications."""
from pathlib import Path
import hashlib
import json
import re
import sys

ROOT=Path(__file__).resolve().parents[1]
errors=[]
manifest=json.loads((ROOT/'provenance/source-manifest.json').read_text())
for item in manifest['files']:
    p=ROOT/item['path']
    if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=item['sha256']:
        errors.append('Source integrity mismatch: '+item['path'])
python_count=0
for p in ROOT.rglob('*.py'):
    if '.git' in p.parts:continue
    python_count+=1
    try:compile(p.read_text(encoding='utf-8'),str(p),'exec')
    except Exception as exc:errors.append(str(exc))
for p in ROOT.rglob('*.json'):
    try:json.loads(p.read_text(encoding='utf-8'))
    except Exception as exc:errors.append(f'{p.relative_to(ROOT)}: {exc}')
for p in ROOT.rglob('*.md'):
    for target in re.findall(r'\]\(([^\s)]+)\)',p.read_text()):
        if '://' in target or target.startswith('#'):continue
        local=target.split('#',1)[0]
        if local and not (p.parent/local).exists():
            errors.append(f'Broken link in {p.relative_to(ROOT)}: {target}')
for p in ROOT.rglob('*'):
    if not p.is_file() or '.git' in p.parts:continue
    if p.suffix.lower() in {'.hef','.har','.pth','.pt','.bin','.elf','.xe71','.whl','.mp4','.jpg','.jpeg','.png','.npy'}:
        errors.append('Unexpected binary/personal asset: '+str(p.relative_to(ROOT)))
if errors:
    print('\n'.join(errors));sys.exit(1)
print(f'PASS: {len(manifest["files"])} preserved source paths, {python_count} Python files, JSON and local document links.')
print('No board inference, firmware build or model accuracy test was performed by this check.')
