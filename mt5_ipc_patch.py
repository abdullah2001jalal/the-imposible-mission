#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
In-memory IAT patch for the MetaTrader5 Python package's IPC handshake.

WHY THIS EXISTS
---------------
mt5.initialize() fails on fresh / CI terminals with (-10005, 'IPC timeout'):
the compiled extension (_core*.pyd) polls the terminal over a named pipe with
opcode 0x6d ("status query") and waits for state 2 (STATUS_READY). A terminal
that has just been installed/started with no subscribed symbol can sit in
state 1 (STATUS_INITIALIZING) indefinitely -- subscribing a symbol normally
happens only AFTER initialize() returns, so client and terminal wait on each
other forever. The deadlock lives in MetaQuotes' own extension and also
occurs on native Windows (documented by the wine-mt5-ipc-patch project and
MQL5 forum threads 517169/517620; initialize succeeds there only after an
account was logged in and a symbol/chart was subscribed).

WHAT THIS DOES
--------------
Hooks CreateFileW/ReadFile in the extension's Import Address Table and, for
the specific 12-byte opcode-0x6d status reply only, rewrites the status byte
from 0x01 (STATUS_INITIALIZING) to 0x02 (STATUS_READY). No file on disk is
modified; all other traffic and opcodes are untouched. IAT RVAs are derived
at runtime with pefile, with verified hardcoded fallbacks (identical for
_MetaTrader5 5.0.6231 / _core.cp311-win_amd64.pyd and the reference 6230 pyd).

Import this module BEFORE `import MetaTrader5`, then call apply().
"""

import ctypes
from ctypes import wintypes
import glob
import os
import sys

# Verified via pefile against MetaTrader5 5.0.6231 _core.cp311-win_amd64.pyd
# (identical to the offsets derived from build 6230's pyd by the reference
# wine-mt5-ipc-patch project).
FALLBACK_IAT_RVAS = {"CreateFileW": 0x140B8, "WriteFile": 0x140F0, "ReadFile": 0x140F8}

applied = False
status = "not applied (apply() never ran)"


def _dbg(msg: str) -> None:
    """Crash-proof breadcrumb to stderr (survives partial process death)."""
    try:
        sys.stderr.write(f"[ipc-patch] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _pyd_path() -> str:
    """Locate the on-disk _core*.pyd of the installed MetaTrader5 package."""
    try:
        import MetaTrader5 as _mt5
        pkg_dir = os.path.dirname(os.path.abspath(_mt5.__file__))
        hits = sorted(glob.glob(os.path.join(pkg_dir, "_core*.pyd")))
        if hits:
            return hits[0]
    except Exception:
        pass
    for site in list(sys.path):
        if site and os.path.isdir(site):
            hits = glob.glob(os.path.join(site, "MetaTrader5", "_core*.pyd"))
            if hits:
                return hits[0]
    return ""


def _iat_rvas(pyd: str) -> dict:
    """Resolve IAT slot RVAs for CreateFileW/WriteFile/ReadFile via pefile."""
    try:
        import pefile
        pe = pefile.PE(pyd, fast_load=True)
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]]
        )
        # NOTE: pefile's imp.address is ImageBase + IAT-slot RVA (a full VA),
        # not a bare RVA -- normalize it, and keep only in-image values.
        image_base = int(pe.OPTIONAL_HEADER.ImageBase)
        size_of_image = int(pe.OPTIONAL_HEADER.SizeOfImage)
        rvas = {}
        for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
            for imp in entry.imports:
                name = (imp.name or b"").decode("ascii", "replace")
                if name in FALLBACK_IAT_RVAS and name not in rvas:
                    addr = int(imp.address)
                    rva = addr - image_base if addr >= image_base else addr
                    if 0 <= rva < size_of_image:
                        rvas[name] = rva
        if {"CreateFileW", "ReadFile"} <= set(rvas):
            return rvas
    except Exception:
        pass
    return dict(FALLBACK_IAT_RVAS)


def apply() -> bool:
    """Install the IAT hooks. Idempotent; safe to call more than once."""
    global applied, status
    if applied:
        return True
    try:
        _dbg("apply() called; importing MetaTrader5...")
        import MetaTrader5 as _mt5  # noqa: F401 -- ensures the pyd is loaded
        _dbg("MetaTrader5 import OK")

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_mod = kernel32.GetModuleHandleW
        get_mod.argtypes = [wintypes.LPCWSTR]
        get_mod.restype = wintypes.HMODULE

        pyd = _pyd_path()
        h_mod = None
        if pyd:
            h_mod = get_mod(os.path.basename(pyd)) or get_mod(pyd)
        if not h_mod:
            h_mod = get_mod("_core")
        if not h_mod:
            status = "patch FAILED: _core*.pyd module handle not found"
            return False

        rvas = _iat_rvas(pyd)
        _dbg(
            f"pyd={os.path.basename(pyd) if pyd else '?'} handle=0x{int(h_mod):x} "
            f"rvas={{ {', '.join(f'{k}: 0x{v:x}' for k, v in sorted(rvas.items()))} }}"
        )

        virtual_protect = kernel32.VirtualProtect
        virtual_protect.argtypes = [
            ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        virtual_protect.restype = wintypes.BOOL
        PAGE_EXECUTE_READWRITE = 0x40

        READ_PROTO = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
        )
        CREATE_PROTO = ctypes.WINFUNCTYPE(
            wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )

        def slot(rva: int) -> ctypes.c_uint64:
            return ctypes.c_uint64.from_address(h_mod + rva)

        real_CreateFileW = CREATE_PROTO(slot(rvas["CreateFileW"]).value)
        real_ReadFile = READ_PROTO(slot(rvas["ReadFile"]).value)

        invalid_handle = ctypes.c_void_p(-1).value
        pipe_handles = set()

        def my_CreateFileW(name, access, share, sec, disp, flags, template):
            h = real_CreateFileW(name, access, share, sec, disp, flags, template)
            try:
                if name and "pipe" in str(name).lower() and h and h != invalid_handle:
                    pipe_handles.add(h)
            except Exception:
                pass
            return h

        def my_ReadFile(handle, buf, count, read, overlapped):
            res = real_ReadFile(handle, buf, count, read, overlapped)
            try:
                if res and handle in pipe_handles and read and read.contents.value == 12:
                    data = (ctypes.c_uint8 * 12).from_address(buf)
                    if data[0] == 0x6D and data[8] != 0x02:
                        data[8] = 0x02  # STATUS_INITIALIZING -> STATUS_READY
            except Exception:
                pass
            return res

        cb_CreateFileW = CREATE_PROTO(my_CreateFileW)
        cb_ReadFile = READ_PROTO(my_ReadFile)
        # Keep the callback objects alive for the process lifetime; ctypes
        # callbacks get garbage collected otherwise (crashes the hook).
        apply._cbs = [cb_CreateFileW, cb_ReadFile]

        old_prot = wintypes.DWORD()
        for name, cb in (("CreateFileW", cb_CreateFileW), ("ReadFile", cb_ReadFile)):
            p = h_mod + rvas[name]
            if not virtual_protect(p, 8, PAGE_EXECUTE_READWRITE, ctypes.byref(old_prot)):
                status = f"patch FAILED: VirtualProtect({name}) error {ctypes.get_last_error()}"
                _dbg(status)
                return False
            slot(rvas[name]).value = ctypes.cast(cb, ctypes.c_void_p).value
            virtual_protect(p, 8, old_prot.value, ctypes.byref(old_prot))
            _dbg(f"hook installed: {name}")

        applied = True
        pyd_name = os.path.basename(pyd) if pyd else "_core*.pyd"
        rva_text = ", ".join(f"{k}=0x{v:x}" for k, v in sorted(rvas.items()))
        status = f"ACTIVE (hooked {pyd_name}; IAT RVAs: {rva_text})"
        return True
    except Exception as exc:
        status = f"patch FAILED: {type(exc).__name__}: {exc}"
        return False
