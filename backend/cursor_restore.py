"""Force-restore all native Windows cursors (used by Electron when the backend
died without cleanup and could not restore them itself). Exits immediately."""

import ctypes

ctypes.windll.user32.SystemParametersInfoW(0x0057, 0, None, 0)
