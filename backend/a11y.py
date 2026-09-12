"""UIA element-level grounding.

Exposes a structured tree of the foreground window's UI Automation elements so
the model can target elements (``click_element``) instead of pixel
coordinates, and can verify typed text landed (verify-after-action).

DPI: the process is already per-monitor-v2 aware (main.py sets it before
importing anything screen-related). This module must NOT touch DPI awareness.

Everything is defensive: any failure returns None (logged once to stdout).
"""

from __future__ import annotations

import sys
from typing import Any

from backend import secrets_filter

_MAX_ELEMENTS = 120
_MAX_DEPTH = 5
_TEXT_ROLE_SUFFIX = "Control"
_WARNED = False


def _warn_once(message: str) -> None:
    global _WARNED
    if not _WARNED:
        _WARNED = True
        print(f"[a11y] {secrets_filter.filter_text(message)}", flush=True)


def _import_uia():
    import uiautomation as auto

    if hasattr(auto, "SetGlobalSearchTimeout"):
        auto.SetGlobalSearchTimeout(1000)
    return auto


def _role(control: Any) -> str:
    try:
        name = str(control.ControlTypeName)
        return name[:-len(_TEXT_ROLE_SUFFIX)] if name.endswith(_TEXT_ROLE_SUFFIX) else name
    except Exception:
        return "Unknown"


def _safe_center(control: Any) -> list[int] | None:
    try:
        rect = control.BoundingRectangle
        left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
        if right > left and bottom > top:
            return [int((left + right) // 2), int((top + bottom) // 2)]
    except Exception:
        pass
    return None


def _safe_value(control: Any) -> str | None:
    try:
        import uiautomation as auto
        value = control.GetPropertyValue(auto.ValuePattern.ValueProperty)
        if isinstance(value, str) and value:
            return value
    except Exception:
        pass
    return None


def _safe_name(control: Any) -> str:
    try:
        return str(control.Name or "")
    except Exception:
        return ""


def _safe_enabled(control: Any) -> bool:
    try:
        return bool(control.IsEnabled)
    except Exception:
        return True


def _collect(root: Any, max_elements: int, max_depth: int) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = []

    def visit(control: Any, depth: int) -> None:
        if len(elements) >= max_elements or depth > max_depth:
            return
        name = _safe_name(control)
        value = _safe_value(control)
        if name or value:
            elements.append({
                "id": len(elements),
                "role": _role(control),
                "name": name,
                "value": value,
                "enabled": _safe_enabled(control),
                "center": _safe_center(control),
            })
        try:
            children = control.GetChildren()
        except Exception:
            children = []
        for child in children:
            visit(child, depth + 1)
            if len(elements) >= max_elements:
                return

    visit(root, 0)
    return elements


def _foreground_window() -> Any | None:
    auto = _import_uia()
    focused = auto.GetFocusedControl()
    if focused is None:
        return None
    window = focused
    while window is not None:
        try:
            if window.ControlTypeName == "WindowControl":
                return window
        except Exception:
            pass
        parent = window.GetParentControl()
        if parent is None or parent.GetRuntimeId() == window.GetRuntimeId():
            break
        window = parent
    return window if window is not None else focused


def _window_title(window: Any) -> str:
    try:
        return str(window.Name or "")
    except Exception:
        return ""


def _window_rect(window: Any) -> list[int] | None:
    try:
        rect = window.BoundingRectangle
        if rect.right > rect.left and rect.bottom > rect.top:
            return [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]
    except Exception:
        pass
    return None


def _elements_for(root: Any, max_elements: int, max_depth: int) -> dict[str, Any] | None:
    try:
        elements = _collect(root, max_elements, max_depth)
        title = _window_title(root)
        rect = _window_rect(root)
        return {"window_title": title, "window_rect": rect, "elements": elements}
    except Exception as exc:
        _warn_once(f"UIA walk failed: {exc}")
        return None


def get_window_elements(
    max_elements: int = _MAX_ELEMENTS, max_depth: int = _MAX_DEPTH
) -> dict[str, Any] | None:
    """UIA element list for the foreground window; None on any failure."""
    try:
        window = _foreground_window()
        if window is None:
            _warn_once("no focused control / foreground window")
            return None
        return _elements_for(window, max_elements, max_depth)
    except Exception as exc:
        _warn_once(f"get_window_elements failed: {exc}")
        return None


def get_desktop_elements(
    max_elements: int = _MAX_ELEMENTS, max_depth: int = _MAX_DEPTH
) -> dict[str, Any] | None:
    """UIA element list rooted at the desktop; fallback when no foreground window."""
    try:
        auto = _import_uia()
        root = auto.GetRootControl()
        if root is None:
            _warn_once("no desktop root control")
            return None
        return _elements_for(root, max_elements, max_depth)
    except Exception as exc:
        _warn_once(f"get_desktop_elements failed: {exc}")
        return None


def read_foreground_text(max_len: int = 2000) -> str | None:
    """Concatenated visible text of the foreground window's text/edit/document
    elements; None on any failure."""
    try:
        auto = _import_uia()
        window = _foreground_window()
        if window is None:
            return None
        parts: list[str] = []

        def visit(control: Any, depth: int) -> None:
            if depth > 8 or sum(len(p) for p in parts) >= max_len:
                return
            try:
                type_name = control.ControlTypeName
            except Exception:
                type_name = ""
            if type_name in ("TextControl", "EditControl", "DocumentControl"):
                text = _safe_value(control)
                if not text:
                    text = _safe_name(control)
                if text:
                    parts.append(text)
                    if sum(len(p) for p in parts) >= max_len:
                        return
            try:
                children = control.GetChildren()
            except Exception:
                return
            for child in children:
                visit(child, depth + 1)

        visit(window, 0)
        return "\n".join(parts)[:max_len] or ""
    except Exception as exc:
        _warn_once(f"read_foreground_text failed: {exc}")
        return None


def get_focused_center() -> tuple[int, int] | None:
    """Physical-pixel center of the FOCUSED control, so the agent pointer can
    glide to the field that is about to receive typed input. None on failure
    or when the focused control has no usable rectangle."""
    try:
        auto = _import_uia()
        control = auto.GetFocusedControl()
        if control is None:
            return None
        return _safe_center(control) or None
    except Exception as exc:
        _warn_once(f"get_focused_center failed: {exc}")
        return None


def read_focused_value(max_len: int = 500) -> str | None:
    """Value/Name text of the FOCUSED control (typically the element that just
    received typed input). Returns None on failure, "" when the control exposes
    no readable value."""
    try:
        auto = _import_uia()
        control = auto.GetFocusedControl()
        if control is None:
            return None
        text = _safe_value(control)
        if not text:
            text = _safe_name(control)
        if not text:
            return ""
        return text[:max_len]
    except Exception as exc:
        _warn_once(f"read_focused_value failed: {exc}")
        return None


def build_model_context(
    a11y_result: dict[str, Any] | None,
    origin_x: int,
    origin_y: int,
    scale_x: float,
    scale_y: float,
    max_elements: int = _MAX_ELEMENTS,
) -> dict[str, Any] | None:
    """Convert a raw a11y result into model-image space for the provider.

    Each element's physical-pixel center becomes ``mx = (cx - origin_x) /
    scale_x`` (and likewise for y), rounded. Elements whose center is None or
    outside the model image are dropped. Returns ``None`` if ``a11y_result`` is
    None so callers can pass the value straight through.
    """
    if a11y_result is None:
        return None
    try:
        elements: list[dict[str, Any]] = []
        for raw in a11y_result.get("elements", []):
            if len(elements) >= max_elements:
                break
            center = raw.get("center")
            if not center or len(center) != 2:
                continue
            cx, cy = center
            mx = round((cx - origin_x) / scale_x) if scale_x else cx
            my = round((cy - origin_y) / scale_y) if scale_y else cy
            if mx < 0 or my < 0:
                continue
            elements.append({
                "id": raw.get("id"),
                "role": raw.get("role"),
                "name": raw.get("name"),
                "value": raw.get("value"),
                "enabled": raw.get("enabled", True),
                "center": [mx, my],
            })
        return {
            "window_title": a11y_result.get("window_title", ""),
            "elements": elements,
        }
    except Exception as exc:
        _warn_once(f"build_model_context failed: {exc}")
        return None


if not sys.platform.startswith("win"):
    _warn_once("uiautomation is Windows-only; a11y disabled on this platform")
