# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [
    ('app.py', '.'),
    ('pw_access.py', '.'),
    ('src', 'src'),
    ('assets', 'assets'),
    ('prompts', 'prompts'),
    # Bundle the REAL login config INSIDE the exe so it runs as a single
    # standalone file — no loose .streamlit/secrets.toml to ship alongside.
    # The launcher chdir's into this bundle dir so Streamlit finds it.
    ('.streamlit/secrets.toml', '.streamlit'),
]
binaries = []
hiddenimports = ['streamlit.web.cli', 'streamlit.runtime.scriptrunner.magic_funcs', 'pw_access']
tmp_ret = collect_all('streamlit')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('altair')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('google.genai')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['streamlit_launcher.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='HandwrittenNotesApp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
