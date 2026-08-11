# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

datas = [
    ('app.py', '.'),
    ('pw_access.py', '.'),
    ('src', 'src'),
    ('assets', 'assets'),
    ('prompts', 'prompts'),
    ('.streamlit/secrets.toml', '.streamlit'),
]

if Path('.env').exists():
    datas.append(('.env', '.'))

binaries = []
hiddenimports = [
    'pw_access',
    # Google token refresh. `win32crypt` (pywin32) is imported lazily inside
    # pw_auth to DPAPI-encrypt the stored refresh token, so PyInstaller's static
    # analysis cannot see it — without this the packaged build silently falls
    # back to storing the refresh token in plaintext.
    'src.pw_auth',
    'win32crypt',
    'streamlit.web.cli',
    'streamlit.runtime.scriptrunner.magic_funcs',
]

tmp_ret = collect_all('streamlit')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('altair')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('docx')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('fitz')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pptx')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('PIL')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('dotenv')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# NumPy's normal PyInstaller hook already collects its runtime DLLs.  Using
# collect_all('numpy') also bundles NumPy's large test suite and f2py tooling,
# adding minutes and many unnecessary files to the team build.

hiddenimports += [
    'fitz',
    'docx',
    'pptx',
    'PIL',
    'PIL.Image',
    'dotenv',
    'numpy',
    'pythoncom',
    'win32com',
    'win32com.client',
]


a = Analysis(
    ['streamlit_launcher_stable.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'pytest',
        'numpy.tests',
        'numpy.f2py.tests',
        'numpy.fft.tests',
        'numpy.lib.tests',
        'numpy.linalg.tests',
        'PIL.tests',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='HandwrittenNotesAppTeam',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='HandwrittenNotesAppTeam',
)
