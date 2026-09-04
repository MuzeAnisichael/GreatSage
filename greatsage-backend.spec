from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata

root = Path(SPECPATH)
data = [(str(root / 'ui'), 'ui')]
binaries = []
for package in ('faster_whisper', 'ctranslate2', 'tokenizers', 'pyaudiowpatch'):
    data += collect_data_files(package)
    binaries += collect_dynamic_libs(package)
for distribution in ('webrtcvad-wheels', 'huggingface-hub', 'faster-whisper'):
    data += copy_metadata(distribution)

analysis = Analysis(
    [str(root / 'scripts' / 'backend_entry.py')], pathex=[str(root)],
    binaries=binaries, datas=data,
    hookspath=[str(root / 'scripts' / 'hooks')],
    hiddenimports=['pyaudiowpatch', 'webrtcvad', 'win32com.client', 'pythoncom',
                   'pywintypes', 'faster_whisper', 'av', 'uvicorn.logging',
                   'uvicorn.loops.asyncio', 'uvicorn.protocols.http.h11_impl',
                   'uvicorn.protocols.websockets.websockets_impl', 'uvicorn.lifespan.on'],
    excludes=['torch', 'tensorflow', 'jax', 'matplotlib', 'pandas', 'scipy',
              'IPython', 'notebook', 'pytest', 'sympy', 'tkinter', 'transformers',
              'sklearn', 'statsmodels', 'plotly', 'h5py', 'keras', 'tensorboard',
              'cv2', 'onnx', 'onnxruntime', 'torchaudio', 'torchvision'],
    noarchive=False,
)
archive = PYZ(analysis.pure)
executable = EXE(archive, analysis.scripts, [], exclude_binaries=True,
                 name='greatsage-backend', debug=False, strip=False, upx=False, console=True)
bundle = COLLECT(executable, analysis.binaries, analysis.datas, strip=False,
                 upx=False, name='greatsage-backend')
