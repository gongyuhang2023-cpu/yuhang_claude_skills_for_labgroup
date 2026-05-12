"""
Windows audio device switching for MeetingMind virtual cable support.

Uses pycaw for device enumeration and IPolicyConfig COM interface for
changing the default audio output device.

Workflow:
  switcher = AudioDeviceSwitcher()
  switcher.switch_to("CABLE Input")   # before recording
  # ... record via WASAPI loopback (captures at full volume) ...
  switcher.restore()                   # after recording
"""

import ctypes

import comtypes
from comtypes import GUID, COMMETHOD, HRESULT
from pycaw.pycaw import AudioUtilities, IMMDeviceEnumerator, EDataFlow, ERole
from pycaw.constants import CLSID_MMDeviceEnumerator


CLSID_PolicyConfigClient = GUID('{870af99c-171d-4f9e-af0d-e63df40c2bc9}')

E_CONSOLE = 0
E_MULTIMEDIA = 1


class IPolicyConfig(comtypes.IUnknown):
    """Undocumented but stable COM interface for changing default audio device.
    Available since Windows Vista, works on Windows 10/11."""
    _iid_ = GUID('{f8679f50-850a-41cf-9c72-430f290290c8}')


IPolicyConfig._methods_ = [
    COMMETHOD([], HRESULT, 'GetMixFormat',
              (['in'], ctypes.c_wchar_p),
              (['out'], ctypes.POINTER(ctypes.c_void_p))),
    COMMETHOD([], HRESULT, 'GetDeviceFormat',
              (['in'], ctypes.c_wchar_p),
              (['in'], ctypes.c_int),
              (['out'], ctypes.POINTER(ctypes.c_void_p))),
    COMMETHOD([], HRESULT, 'ResetDeviceFormat',
              (['in'], ctypes.c_wchar_p)),
    COMMETHOD([], HRESULT, 'SetDeviceFormat',
              (['in'], ctypes.c_wchar_p),
              (['in'], ctypes.c_void_p),
              (['in'], ctypes.c_void_p)),
    COMMETHOD([], HRESULT, 'GetProcessingPeriod',
              (['in'], ctypes.c_wchar_p),
              (['in'], ctypes.c_int),
              (['out'], ctypes.POINTER(ctypes.c_longlong)),
              (['out'], ctypes.POINTER(ctypes.c_longlong))),
    COMMETHOD([], HRESULT, 'SetProcessingPeriod',
              (['in'], ctypes.c_wchar_p),
              (['in'], ctypes.POINTER(ctypes.c_longlong))),
    COMMETHOD([], HRESULT, 'GetShareMode',
              (['in'], ctypes.c_wchar_p),
              (['out'], ctypes.POINTER(ctypes.c_void_p))),
    COMMETHOD([], HRESULT, 'SetShareMode',
              (['in'], ctypes.c_wchar_p),
              (['in'], ctypes.c_void_p)),
    COMMETHOD([], HRESULT, 'GetPropertyValue',
              (['in'], ctypes.c_wchar_p),
              (['in'], ctypes.c_int),
              (['in'], ctypes.c_void_p),
              (['out'], ctypes.POINTER(ctypes.c_void_p))),
    COMMETHOD([], HRESULT, 'SetPropertyValue',
              (['in'], ctypes.c_wchar_p),
              (['in'], ctypes.c_int),
              (['in'], ctypes.c_void_p),
              (['in'], ctypes.c_void_p)),
    COMMETHOD([], HRESULT, 'SetDefaultEndpoint',
              (['in'], ctypes.c_wchar_p),
              (['in'], ctypes.c_uint)),
    COMMETHOD([], HRESULT, 'SetEndpointVisibility',
              (['in'], ctypes.c_wchar_p),
              (['in'], ctypes.c_int)),
]


class AudioDeviceSwitcher:
    """Manages default audio output device switching for virtual cable support."""

    def __init__(self):
        self._original_id: str | None = None
        self._original_name: str | None = None

    def list_output_devices(self) -> list[dict]:
        return [
            {'id': d.id, 'name': d.FriendlyName}
            for d in AudioUtilities.GetAllDevices()
            if d.id and d.FriendlyName
        ]

    def get_default_output_id(self) -> str:
        enumerator = comtypes.CoCreateInstance(
            CLSID_MMDeviceEnumerator,
            IMMDeviceEnumerator,
            comtypes.CLSCTX_INPROC_SERVER,
        )
        endpoint = enumerator.GetDefaultAudioEndpoint(
            EDataFlow.eRender.value, ERole.eMultimedia.value
        )
        return endpoint.GetId()

    def set_default_output(self, device_id: str):
        policy = comtypes.CoCreateInstance(
            CLSID_PolicyConfigClient,
            IPolicyConfig,
            comtypes.CLSCTX_ALL,
        )
        policy.SetDefaultEndpoint(device_id, E_CONSOLE)
        policy.SetDefaultEndpoint(device_id, E_MULTIMEDIA)

    def find_device(self, keyword: str) -> dict | None:
        kw = keyword.lower()
        for d in self.list_output_devices():
            if kw in d['name'].lower():
                return d
        return None

    def switch_to(self, keyword: str) -> bool:
        """Switch default output to device matching keyword. Saves original for restore()."""
        target = self.find_device(keyword)
        if not target:
            print(f"  [Audio] Device matching '{keyword}' not found")
            return False

        current_id = self.get_default_output_id()
        current = self.find_device_by_id(current_id)
        self._original_id = current_id
        self._original_name = current['name'] if current else 'unknown'

        if current_id == target['id']:
            print(f"  [Audio] Already using '{target['name']}'")
            return True

        self.set_default_output(target['id'])
        print(f"  [Audio] Switched: {self._original_name} -> {target['name']}")
        return True

    def find_device_by_id(self, device_id: str) -> dict | None:
        for d in self.list_output_devices():
            if d['id'] == device_id:
                return d
        return None

    def restore(self):
        """Restore the original default output device saved by switch_to()."""
        if self._original_id is None:
            return
        try:
            self.set_default_output(self._original_id)
            print(f"  [Audio] Restored: -> {self._original_name}")
        except Exception as e:
            print(f"  [Audio] Failed to restore device: {e}")
        finally:
            self._original_id = None
            self._original_name = None
