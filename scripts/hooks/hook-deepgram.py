"""Deepgram SDK 7.14 lazy exports used by Pipecat's Listen v1 STT adapter.

The SDK resolves these exports with __getattr__/import_module. Their ordinary
imports pull in the remaining models/socket implementation; do not collect all
Deepgram APIs or Pipecat service extras merely to resolve these entry points.
"""
from PyInstaller.utils.hooks import copy_metadata

datas = copy_metadata("deepgram-sdk")
hiddenimports = [
    "deepgram.client",
    "deepgram.listen.client",
    "deepgram.listen.v1.client",
    "deepgram.listen.v1.socket_client",
    "deepgram.listen.v1.types.listen_v1close_stream",
    "deepgram.listen.v1.types.listen_v1finalize",
    "deepgram.listen.v1.types.listen_v1keep_alive",
    "deepgram.listen.v1.types.listen_v1results",
]
