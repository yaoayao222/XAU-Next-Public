"""Round trip one disposable SYNTHETIC credential under this app's own namespace."""

import ctypes
import os
from ctypes import wintypes

import pytest
from xau_next.platform.credentials.public import PREFIX, WindowsCredentials
from xau_next.shared.errors import DomainError
from xau_next.shared.values import new_id


def test_other_application_credentials_are_never_read():
    with pytest.raises(DomainError):
        WindowsCredentials().read("OtherApplication/example")


@pytest.mark.skipif(os.name != "nt", reason="Windows Credential Manager requires Windows")
def test_windows_native_credential_roundtrip_and_exact_cleanup():
    store = WindowsCredentials()
    reference = PREFIX + "SYNTHETIC-test/" + new_id()
    try:
        store.write(reference, "SYNTHETIC-disposable-validation-value")
        assert store.configured(reference)
        assert store.read(reference) == "SYNTHETIC-disposable-validation-value"
    finally:
        dll = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        dll.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        dll.CredDeleteW.restype = wintypes.BOOL
        assert reference.startswith(PREFIX + "SYNTHETIC-test/")
        assert dll.CredDeleteW(reference, 1, 0) or ctypes.get_last_error() == 1168
    assert not store.configured(reference)
