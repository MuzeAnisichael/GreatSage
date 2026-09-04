"""webrtcvad-wheels provides the import name webrtcvad on Windows."""
from PyInstaller.utils.hooks import copy_metadata

hiddenimports = ['_webrtcvad']
datas = copy_metadata('webrtcvad-wheels')
