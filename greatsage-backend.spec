from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata

root = Path(SPECPATH)
data = [(str(root / 'ui'), 'ui'),
        (str(root / 'docs' / 'third-party.md'), 'licenses')]
binaries = []
for package in ('faster_whisper', 'ctranslate2', 'tokenizers', 'pyaudiowpatch'):
    data += collect_data_files(package)
    binaries += collect_dynamic_libs(package)
for distribution in ('webrtcvad-wheels', 'huggingface-hub', 'faster-whisper'):
    data += copy_metadata(distribution)
for package in ('langchain_core', 'langchain_openai', 'langchain_ollama'):
    data += collect_data_files(package)
for distribution in ('langchain-core', 'langchain-openai', 'langchain-ollama', 'langsmith', 'openai', 'ollama'):
    data += copy_metadata(distribution)
for distribution in ('langgraph', 'langgraph-checkpoint', 'langgraph-checkpoint-sqlite'):
    data += copy_metadata(distribution)
for distribution in ('langmem', 'trustcall', 'langchain', 'langchain-anthropic', 'anthropic', 'dydantic'):
    data += copy_metadata(distribution)
for package in ('mcp', 'mcp_types', 'jsonschema_specifications'):
    data += collect_data_files(package)
for distribution in ('mcp', 'mcp-types', 'jsonschema', 'jsonschema-specifications'):
    data += copy_metadata(distribution)
# Pipecat reads its distribution version on import. Keep the metadata/licences
# for the narrow voice import graph, without collecting its bundled ONNX models,
# CLI templates or unrelated service extras. Deepgram's lazy exports have a hook.
for distribution in ('pipecat-ai', 'soxr', 'sentencex', 'loudness'):
    data += copy_metadata(distribution)

analysis = Analysis(
    [str(root / 'scripts' / 'backend_entry.py')], pathex=[str(root)],
    binaries=binaries, datas=data,
    hookspath=[str(root / 'scripts' / 'hooks')],
    hiddenimports=['pyaudiowpatch', 'webrtcvad', 'win32com.client', 'pythoncom', 'tiktoken_ext.openai_public',
                   'pywintypes', 'faster_whisper', 'av', 'uvicorn.logging',
                   'uvicorn.loops.asyncio', 'uvicorn.protocols.http.h11_impl',
                   'uvicorn.protocols.websockets.websockets_impl', 'uvicorn.lifespan.on'],
    # The voice slice keeps WebRTC VAD; Pipecat's optional ONNX SmartTurn/Silero
    # and resampy/scipy paths are not configured or imported by this pipeline.
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
