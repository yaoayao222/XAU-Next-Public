"""Only XAU Next's own Windows generic credentials; never enumerate other apps."""

import ctypes
import os
from ctypes import wintypes

from xau_next.shared.errors import DomainError

PREFIX = "XAU-Next/model/"


class Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentials:
    def _api(self, reference):
        if not reference.startswith(PREFIX) or os.name != "nt":
            raise DomainError("CREDENTIAL_UNAVAILABLE", "请在本机 Windows 设置页重新录入本应用密钥。", 409)
        dll = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        dll.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
        dll.CredWriteW.restype = wintypes.BOOL
        dll.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(Credential)),
        ]
        dll.CredReadW.restype = wintypes.BOOL
        dll.CredFree.argtypes = [ctypes.c_void_p]
        dll.CredFree.restype = None
        return dll

    def write(self, reference, key):
        dll = self._api(reference)
        raw = key.encode("utf-8")
        blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        item = Credential(
            Type=1,
            TargetName=reference,
            CredentialBlobSize=len(raw),
            CredentialBlob=blob,
            Persist=2,
            UserName="XAU Next",
        )
        try:
            if not dll.CredWriteW(ctypes.byref(item), 0):
                raise DomainError("CREDENTIAL_WRITE_FAILED", "Windows 凭据存储写入失败，请重新录入。", 503)
        finally:
            ctypes.memset(blob, 0, len(raw))

    def read(self, reference):
        if not reference:
            raise DomainError("MODEL_KEY_REQUIRED", "请在本机设置页录入模型密钥。", 409)
        dll = self._api(reference)
        pointer = ctypes.POINTER(Credential)()
        if not dll.CredReadW(reference, 1, 0, ctypes.byref(pointer)):
            raise DomainError("MODEL_KEY_REQUIRED", "本机未找到密钥；恢复到其他机器后须重新录入。", 409)
        try:
            item = pointer.contents
            return ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize).decode("utf-8")
        finally:
            dll.CredFree(pointer)

    def configured(self, reference):
        try:
            return bool(self.read(reference))
        except DomainError:
            return False
