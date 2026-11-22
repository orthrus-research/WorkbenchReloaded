"""Per-process audio peaks (Windows): how loud each process plays, from the system's own audio session meters.

Every process that plays sound has an audio session on the default output device; its meter gives the peak sample of
the last interval, 0.0 (silence) to 1.0 (full scale). ``Sampler`` reads the meters of chosen processes a few times a
second in a thread and keeps each one's highest and mean peak and how often it was heard, so a session can show that
a client made sound (or stayed silent) without listening to it. Standard library only: the Core Audio interfaces are
called through their vtables.
"""

from __future__ import annotations

import sys
import threading
import time
import uuid
from dataclasses import dataclass, field


def available() -> bool:
    return sys.platform == "win32"


if available():
    import ctypes as C
    from ctypes import wintypes as W

    class GUID(C.Structure):
        _fields_ = [("Data1", W.DWORD), ("Data2", W.WORD), ("Data3", W.WORD), ("Data4", C.c_ubyte * 8)]

        @classmethod
        def of(cls, text: str) -> GUID:
            value = uuid.UUID(text)
            guid = cls()
            guid.Data1, guid.Data2, guid.Data3 = value.fields[0], value.fields[1], value.fields[2]
            guid.Data4[:] = value.bytes[8:]
            return guid

    CLSID_MMDeviceEnumerator = GUID.of("BCDE0395-E52F-467C-8E3D-C4579291692E")
    IID_IMMDeviceEnumerator = GUID.of("A95664D2-9614-4F35-A746-DE8DB63617E6")
    IID_IAudioSessionManager2 = GUID.of("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
    IID_IAudioSessionControl2 = GUID.of("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")
    IID_IAudioMeterInformation = GUID.of("C02216F6-8C67-4B5B-9D00-D008E73E0064")
    CLSCTX_ALL = 0x17
    E_RENDER, E_MULTIMEDIA = 0, 1
    _ole = C.WinDLL("ole32")
    _ole.CoInitializeEx.argtypes, _ole.CoInitializeEx.restype = [C.c_void_p, W.DWORD], C.c_long
    _ole.CoCreateInstance.argtypes = [C.POINTER(GUID), C.c_void_p, W.DWORD, C.POINTER(GUID), C.POINTER(C.c_void_p)]
    _ole.CoCreateInstance.restype = C.c_long

    PV, PG = C.POINTER(C.c_void_p), C.POINTER(GUID)

    def _call(interface: C.c_void_p, index: int, restype, argtypes: list, *args):
        """Method ``index`` of a COM interface pointer, through its vtable, with the given argument types."""
        vtable = C.cast(C.cast(interface, PV)[0], PV)
        return C.WINFUNCTYPE(restype, C.c_void_p, *argtypes)(vtable[index])(interface, *args)

    def _release(interface):
        if interface:
            _call(interface, 2, C.c_ulong, [])

    def _query(interface, iid: GUID):
        out = C.c_void_p()
        hr = _call(interface, 0, C.c_long, [PG, PV], C.byref(iid), C.byref(out))
        return out if hr == 0 else None


def read_peaks(pids: set[int] | None = None) -> dict[int, float]:
    """The current peak of each process playing on the default output device (only ``pids`` when given)."""
    if not available():
        raise OSError("audio meters are read on Windows")
    _ole.CoInitializeEx(None, 0)  # multithreaded apartment; an earlier call from this thread is fine
    enumerator, device, manager, sessions = C.c_void_p(), C.c_void_p(), C.c_void_p(), C.c_void_p()
    peaks: dict[int, float] = {}
    try:
        if _ole.CoCreateInstance(C.byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL, C.byref(IID_IMMDeviceEnumerator),
                                 C.byref(enumerator)) != 0:
            raise OSError("no audio device enumerator")
        if _call(enumerator, 4, C.c_long, [C.c_int, C.c_int, PV], E_RENDER, E_MULTIMEDIA, C.byref(device)) != 0:
            return peaks  # no output device: nothing plays
        if _call(device, 3, C.c_long, [PG, W.DWORD, C.c_void_p, PV], C.byref(IID_IAudioSessionManager2), CLSCTX_ALL,
                 None, C.byref(manager)) != 0:
            raise OSError("the output device has no session manager")
        if _call(manager, 5, C.c_long, [PV], C.byref(sessions)) != 0:
            raise OSError("cannot list audio sessions")
        count = C.c_int()
        _call(sessions, 3, C.c_long, [C.POINTER(C.c_int)], C.byref(count))
        for index in range(count.value):
            control = C.c_void_p()
            if _call(sessions, 4, C.c_long, [C.c_int, PV], index, C.byref(control)) != 0:
                continue
            second = _query(control, IID_IAudioSessionControl2)
            meter = _query(control, IID_IAudioMeterInformation)
            try:
                pid = W.DWORD()
                if second is None or meter is None or _call(second, 14, C.c_long, [C.POINTER(W.DWORD)],
                                                            C.byref(pid)) != 0:
                    continue
                if pids is not None and pid.value not in pids:
                    continue
                peak = C.c_float()
                if _call(meter, 3, C.c_long, [C.POINTER(C.c_float)], C.byref(peak)) == 0:
                    peaks[pid.value] = max(peaks.get(pid.value, 0.0), float(peak.value))
            finally:
                _release(meter)
                _release(second)
                _release(control)
    finally:
        for interface in (sessions, manager, device, enumerator):
            _release(interface)
    return peaks


@dataclass
class Heard:
    samples: int = 0
    heard: int = 0        # samples above the threshold
    total: float = 0.0
    highest: float = 0.0

    def record(self) -> dict:
        return {"samples": self.samples, "heard": self.heard, "peak_max": round(self.highest, 4),
                "peak_mean": round(self.total / self.samples, 4) if self.samples else 0.0}


@dataclass
class Sampler:
    """Reads the chosen processes' peaks every ``every`` seconds in a thread until stopped."""
    pids: dict[int, str]                  # process id -> the name it is reported under
    every: float = 0.25
    threshold: float = 0.001
    heard: dict[str, Heard] = field(default_factory=dict)
    error: str | None = None

    def __post_init__(self):
        self.heard = {name: Heard() for name in self.pids.values()}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="audio-meter", daemon=True)

    def start(self) -> Sampler:
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.wait(self.every):
            try:
                peaks = read_peaks(set(self.pids))
            except OSError as error:
                self.error = str(error)
                return
            for pid, name in self.pids.items():
                value, entry = peaks.get(pid, 0.0), self.heard[name]
                entry.samples += 1
                entry.total += value
                entry.highest = max(entry.highest, value)
                entry.heard += value > self.threshold

    def stop(self) -> dict[str, dict]:
        self._stop.set()
        self._thread.join(5)
        return {name: entry.record() for name, entry in self.heard.items()}
