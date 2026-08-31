# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 S.huaizhong & LaoXia(ark-code-latest)
"""
全局屏幕文本搜索 + 输入记录插件
- Ctrl+Alt+F : 截取屏幕 → OCR → 覆盖层搜索高亮
- Ctrl+Alt+X : 打开输入记录（含剪贴板项）
- Ctrl+Alt+N : 打开内置轻量文本编辑器
- 托盘图标   : 左键 OCR；右键菜单可打开输入记录 / 新建文本 / 退出

输入记录规则（n天保留，按本地 0 点滚动清理）：
- 键盘输入：一坨接在一起，不分行
- 特殊键 / 快捷键：写成 [*退格] [*Enter] [*Ctrl+C] 之类的标签（`[*` 前缀避免与源代码里的普通 `[xxx]` 语法混淆）
- IME 中文/词组：走 UI Automation Value diff，记录 IME 提交后的最终文本
  （拼音码 nihao1 不进日志）
- 剪贴板：文本项作为分隔块夹在中间
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox

# ---- DPI 感知：确保 PIL 截图与 Tk 坐标一致 ----
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

from PIL import ImageGrab, Image, ImageDraw, ImageTk
import numpy as np

try:
    from rapidocr_onnxruntime import RapidOCR
    _RAPIDOCR_AVAILABLE = True
except Exception as _e_rapid:
    RapidOCR = None
    _RAPIDOCR_AVAILABLE = False
import winocr
import keyboard

HOTKEY = "ctrl+alt+f"
ESC_HOTKEY = "esc"
KEYLOG_HOTKEY = "ctrl+alt+x"
NEW_TEXT_HOTKEY = "ctrl+alt+n"
TEXT_EDITOR_FONT_SIZE = 14
TEXT_EDITOR_WRAP = "word"
TEXT_EDITOR_WIDTH = 600
TEXT_EDITOR_HEIGHT = 600
TEXT_EDITOR_RIGHT = 2500
TEXT_EDITOR_Y = 50
TEXT_EDITOR_REFERENCE_HEIGHT = 1440
TEXT_EDITOR_SCROLL_OPACITY = 0.70
OCR_LANG = "zh-CN"
OCR_TIMEOUT_SEC = 15
TRANSPARENT_COLOR = "#010203"
HIGHLIGHT_ACTIVE = "#ff5c5c"
HIGHLIGHT_OUTLINE = "#ffd93d"

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KEYLOG_DIR = os.path.join(_SCRIPT_DIR, "memory", "keylog")
KEYLOG_FILE = os.path.join(KEYLOG_DIR, "log.jsonl")
KEYLOG_CONFIG_FILE = os.path.join(KEYLOG_DIR, "config.json")
KEYLOG_RETENTION_DAYS_DEFAULT = 3
KEYLOG_RETENTION_CHOICES = (3, 7, 15)

# 输入记录面板：分段加载参数
# - 打开时先只装最后 2000 字符（按 rec 边界向前拼，避免切在剪贴板块中间）
# - 视口滚到顶部后，每次追加 ~1000 字符
KEYLOG_INITIAL_CHARS = 2000
KEYLOG_CHUNK_CHARS = 1000
KEYLOG_PAGE_MAX_RECORDS = 8000
KEYLOG_COPY_MAX_CHARS = 2_000_000
CLIPBOARD_MAX_CHARS = 16_384

# 中日韩 IME 语言 ID（HKL 低 16 位）
_CJK_IME_LANG_IDS = {0x0804, 0x0404, 0x0411, 0x0412}


# ============================================================
#                    i18n（中/英 双语）
# ============================================================
LANG_CONFIG_FILE = os.path.join(_SCRIPT_DIR, "memory", "keylog", "lang.json")


def _atomic_write_json(path, data):
    """Write JSON beside the destination and atomically replace the target."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise

_I18N = {
    "zh": {
        # 托盘
        "tray_tooltip": "屏幕 OCR + 输入记录\n左键: 识别 · 右键: 菜单",
        "tray_ocr": "识别屏幕 (Ctrl+Alt+F)",
        "tray_keylog": "输入记录 (Ctrl+Alt+X)",
        "tray_new_text": "新建文本文档 (Ctrl+Alt+N)",
        "tray_language": "选择语言",
        "tray_lang_zh": "中文",
        "tray_lang_en": "English",
        "tray_quit": "退出",
        # 内置文本编辑器
        "text_editor_untitled": "无标题",
        "text_editor_suffix": "文本文档",
        "text_editor_file": "文件",
        "text_editor_save": "保存",
        "text_editor_save_as": "另存为…",
        "text_editor_close": "关闭",
        "text_editor_chars": "字数: {count}",
        "text_editor_save_error_title": "保存失败",
        "text_editor_save_error": "无法保存文件:\n{err}",
        "text_editor_close_title": "保存更改",
        "text_editor_close_msg": "文档尚未保存，是否先保存更改？",
        "text_editor_filetype": "文本文档",
        "text_editor_all_files": "所有文件",
        # 输入记录面板
        "win_title": "输入记录（Ctrl+Alt+X）  @S.HZ & XIA",
        "header_title": "键盘输入+剪贴板",
        "btn_reload": "🔄 刷新",
        "btn_copy_all": "📋 全选复制",
        "btn_clear": "🧹 清空",
        "cb_autostart": "🚀 开机启动(管理员)",
        "cb_autostart_user": "🚩 开机启动(普通)",
        "tooltip_autostart_admin": "高权限启动：需 UAC，全功能（含键盘钩子）",
        "tooltip_autostart_user": "普通启动：无需 UAC，仅 OCR/剪贴板/输入监听，不安装全局钩子层面提升",
        "cb_keylog": "⌨️ 键盘记录",
        "cb_clipboard": "📋 剪贴板监听",
        "cb_uia": "🆎 中文 IME 捕捉",
        "privacy_hint": "隐私开关：默认全关。仅本机保存，不上传。",
        "tray_status_prefix": "录制",
        "tray_status_off": "录制: 全关",
        "tray_status_on": "录制: {parts}",
        "label_retention": "保留:",
        "days_suffix": "天",
        "loading": "加载中…",
        "count_full": "共 {total} 条 · {chars} 字符（已全部加载）",
        "count_paged": "已显示 {loaded} 条 / {chars} 字符（↑ 滚动加载更早）",
        "copy_too_large": "记录过大（超过 {limit} 字符），请分段选择复制",
        "toast_autostart_install_fail": "开机启动安装失败",
        "toast_autostart_remove_fail": "删除开机启动失败（需管理员）",
        "toast_uac_fail": "启动 UAC 失败或被拒绝",
        # 确认框
        "dlg_autostart_title": "确认开机自启",
        "dlg_autostart_msg": "开机自启需要以管理员权限重启本软件，\n确定后会弹 UAC 并完成安装。",
        "btn_cancel": "取消",
        "btn_confirm_admin": "确定（以管理员重启）",
        "dlg_clear_title": "确认清空",
        "dlg_clear_msg": "确定要清空当前所有输入记录吗？\n此操作不可恢复。",
        # OCR / overlay
        "ocr_loading": "🔍 正在识别屏幕文本...",
        "ocr_screencap_fail": "截屏失败: {err}",
        "ocr_no_text": "未识别到任何文本",
        "ocr_timeout": "OCR 超时（>{sec}s），已放弃",
        "ocr_fail": "OCR 失败: {err}",
        "overlay_placeholder": "🔍",
        "overlay_hint": "Enter 下一个 · Shift+Enter 上一个 · 点左侧「选词」进入选词 · Esc 关闭",
        "overlay_no_match": "0 匹配",
        "overlay_zero_status": "0/0",
        "overlay_select_tip": "选词模式：拖拽选择文本 · 再点「退出选词」或按 Esc 退出",
        "overlay_select_btn_tooltip": "选词模式：直接拖拽选文本",
        "btn_select": "选词",
        "btn_select_exit": "退出选词",
        "btn_rescan": "重新识别",
        "sel_btn_copy": "📋 复制选中 ({n})",
        "sel_btn_close": "✖",
        "sel_toast_copied": "已复制 {n} 字",
        "sel_toast_empty": "选区内无文字",
        "startup_line": "[screen_search] 已启动: OCR={ocr} · KeyLog={kl} · NewText={nt}",
    },
    "en": {
        "tray_tooltip": "Screen OCR + Input History\nLeft: OCR · Right: menu",
        "tray_ocr": "Recognize Screen (Ctrl+Alt+F)",
        "tray_keylog": "Input History (Ctrl+Alt+X)",
        "tray_new_text": "New Text Document (Ctrl+Alt+N)",
        "tray_language": "Language",
        "tray_lang_zh": "中文",
        "tray_lang_en": "English",
        "tray_quit": "Quit",
        # Built-in text editor
        "text_editor_untitled": "Untitled",
        "text_editor_suffix": "Text Document",
        "text_editor_file": "File",
        "text_editor_save": "Save",
        "text_editor_save_as": "Save As…",
        "text_editor_close": "Close",
        "text_editor_chars": "Characters: {count}",
        "text_editor_save_error_title": "Save Failed",
        "text_editor_save_error": "Could not save the file:\n{err}",
        "text_editor_close_title": "Save Changes",
        "text_editor_close_msg": "This document has unsaved changes. Save them first?",
        "text_editor_filetype": "Text documents",
        "text_editor_all_files": "All files",
        "win_title": "Input History (Ctrl+Alt+X)  @S.HZ & XIA",
        "header_title": "Keyboard + Clipboard",
        "btn_reload": "🔄 Reload",
        "btn_copy_all": "📋 Copy All",
        "btn_clear": "🧹 Clear",
        "cb_autostart": "🚀 Start with Windows (Admin)",
        "cb_autostart_user": "🚩 Start with Windows (User)",
        "tooltip_autostart_admin": "Admin autostart: needs UAC; full features (global keyboard hook).",
        "tooltip_autostart_user": "User autostart: no UAC; OCR/clipboard/input only, without elevated global hook.",
        "cb_keylog": "⌨️ Keyboard log",
        "cb_clipboard": "📋 Clipboard watch",
        "cb_uia": "🆎 Chinese IME capture",
        "privacy_hint": "Privacy switches: OFF by default. Local file only, never uploaded.",
        "tray_status_prefix": "Recording",
        "tray_status_off": "Recording: OFF",
        "tray_status_on": "Recording: {parts}",
        "label_retention": "Keep:",
        "days_suffix": "d",
        "loading": "Loading…",
        "count_full": "{total} entries · {chars} chars (fully loaded)",
        "count_paged": "Shown {loaded} entries / {chars} chars (↑ scroll to load older)",
        "copy_too_large": "History exceeds {limit} chars; copy it in smaller selections",
        "toast_autostart_install_fail": "Failed to install autostart",
        "toast_autostart_remove_fail": "Failed to remove autostart (needs admin)",
        "toast_uac_fail": "UAC failed or was declined",
        "dlg_autostart_title": "Confirm Autostart",
        "dlg_autostart_msg": "Autostart requires restarting this app as administrator.\nClick OK to trigger UAC and install the task.",
        "btn_cancel": "Cancel",
        "btn_confirm_admin": "OK (restart as admin)",
        "dlg_clear_title": "Confirm Clear",
        "dlg_clear_msg": "Clear all current input history?\nThis cannot be undone.",
        "ocr_loading": "🔍 Recognizing screen text...",
        "ocr_screencap_fail": "Screenshot failed: {err}",
        "ocr_no_text": "No text recognized",
        "ocr_timeout": "OCR timed out (>{sec}s), giving up",
        "ocr_fail": "OCR failed: {err}",
        "overlay_placeholder": "🔍",
        "overlay_hint": "Enter next · Shift+Enter prev · Click \"Select\" on the left to enter select mode · Esc to close",
        "overlay_no_match": "0 matches",
        "overlay_zero_status": "0/0",
        "overlay_select_tip": "Select mode: drag to select text · click \"Exit Select\" or press Esc to exit",
        "overlay_select_btn_tooltip": "Select mode: drag to select text directly",
        "btn_select": "Select",
        "btn_select_exit": "Exit Select",
        "btn_rescan": "Rescan",
        "sel_btn_copy": "📋 Copy selection ({n})",
        "sel_btn_close": "✖",
        "sel_toast_copied": "Copied {n} chars",
        "sel_toast_empty": "No text in selection",
        "startup_line": "[screen_search] started: OCR={ocr} · KeyLog={kl} · NewText={nt}",
    },
}


def _load_lang():
    try:
        with open(LANG_CONFIG_FILE, "r", encoding="utf-8") as f:
            v = json.load(f).get("lang", "zh")
            return v if v in _I18N else "zh"
    except Exception:
        return "zh"


def _save_lang(lang):
    try:
        _atomic_write_json(LANG_CONFIG_FILE, {"lang": lang})
    except Exception:
        pass


LANG = _load_lang()

_lang_listeners = []  # 切语言时需刷新的回调


def t(key, **kw):
    txt = _I18N.get(LANG, _I18N["zh"]).get(key)
    if txt is None:
        txt = _I18N["zh"].get(key, key)
    if kw:
        try:
            return txt.format(**kw)
        except Exception:
            return txt
    return txt


def set_lang(new_lang):
    global LANG
    if new_lang not in _I18N or new_lang == LANG:
        return
    LANG = new_lang
    _save_lang(new_lang)
    for cb in list(_lang_listeners):
        try:
            cb()
        except Exception:
            pass


def register_lang_listener(cb):
    _lang_listeners.append(cb)


def unregister_lang_listener(cb):
    try:
        _lang_listeners.remove(cb)
    except ValueError:
        pass


# ============================================================
#                   工具：IME 状态检测
# ============================================================
def _get_ime_active():
    """判断前景窗口是否处于 CJK IME 输入模式（开状态）"""
    try:
        user32 = ctypes.windll.user32
        imm32 = ctypes.windll.imm32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        tid = user32.GetWindowThreadProcessId(hwnd, None)
        hkl = user32.GetKeyboardLayout(tid)
        lang = hkl & 0xFFFF
        if lang not in _CJK_IME_LANG_IDS:
            return False
        himc = imm32.ImmGetContext(hwnd)
        if not himc:
            return False
        try:
            status = imm32.ImmGetOpenStatus(himc)
        finally:
            imm32.ImmReleaseContext(hwnd, himc)
        return bool(status)
    except Exception:
        return False


# ============================================================
#                     KeyLog 存储层
# ============================================================
class KeyLogStore:
    """输入记录持久化。使用 JSONL 追加；按本地日 0 点保留最近 N 天。"""

    def __init__(self, path=KEYLOG_FILE, config_path=KEYLOG_CONFIG_FILE):
        self.path = path
        self.config_path = config_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._last_cleanup_day = None
        cfg = self._load_config()
        self.retention_days = cfg.get("retention_days", KEYLOG_RETENTION_DAYS_DEFAULT)
        # 隐私 opt-in：所有片面默认关闭，需长官在输入记录面板里手动启用
        self.keylog_enabled = bool(cfg.get("keylog_enabled", False))
        self.clipboard_enabled = bool(cfg.get("clipboard_enabled", False))
        self.uia_enabled = bool(cfg.get("uia_enabled", False))

    # ---- config 读写：保留老字段，新字段渐进式追加 ----
    def _load_config(self):
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    d = int(raw.get("retention_days", KEYLOG_RETENTION_DAYS_DEFAULT))
                    if d not in KEYLOG_RETENTION_CHOICES:
                        d = KEYLOG_RETENTION_DAYS_DEFAULT
                    return {
                        "retention_days": d,
                        "keylog_enabled": bool(raw.get("keylog_enabled", False)),
                        "clipboard_enabled": bool(raw.get("clipboard_enabled", False)),
                        "uia_enabled": bool(raw.get("uia_enabled", False)),
                    }
        except Exception:
            pass
        return {
            "retention_days": KEYLOG_RETENTION_DAYS_DEFAULT,
            "keylog_enabled": False,
            "clipboard_enabled": False,
            "uia_enabled": False,
        }

    def _save_config(self):
        try:
            data = {
                "retention_days": self.retention_days,
                "keylog_enabled": self.keylog_enabled,
                "clipboard_enabled": self.clipboard_enabled,
                "uia_enabled": self.uia_enabled,
            }
            _atomic_write_json(self.config_path, data)
        except Exception:
            pass

    def set_keylog_enabled(self, on):
        self.keylog_enabled = bool(on)
        self._save_config()

    def set_clipboard_enabled(self, on):
        self.clipboard_enabled = bool(on)
        self._save_config()

    def set_uia_enabled(self, on):
        self.uia_enabled = bool(on)
        self._save_config()

    def _load_retention(self):
        # 向后兼容保留（未使用）
        return self._load_config().get("retention_days", KEYLOG_RETENTION_DAYS_DEFAULT)

    def set_retention_days(self, days):
        if days not in KEYLOG_RETENTION_CHOICES:
            return
        self.retention_days = days
        self._save_config()
        # 立即按新窗口整理
        threading.Thread(target=self.cleanup, daemon=True).start()

    def clear_all(self):
        """清空当前所有输入记录"""
        with self._lock:
            try:
                if os.path.exists(self.path):
                    os.remove(self.path)
            except Exception:
                pass

    def _cutoff_ts(self):
        # 保留：从今日 0 点往前 retention_days 天起算
        now = datetime.now()
        today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = today0 - timedelta(days=self.retention_days)
        return cutoff.timestamp()

    def append(self, kind, text):
        if not text and kind != "clip":
            return
        # opt-in 闸门：默认关闭，需在输入记录面板里手动启用
        if kind in ("key", "ime") and not self.keylog_enabled:
            return
        if kind == "ime" and not self.uia_enabled:
            return
        if kind == "clip" and not self.clipboard_enabled:
            return
        rec = {"ts": time.time(), "kind": kind, "text": text}
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8", newline="") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception:
                pass
        # 每小时最多整理一次（按日切）
        today = datetime.now().date()
        if self._last_cleanup_day != today:
            self._last_cleanup_day = today
            threading.Thread(target=self.cleanup, daemon=True).start()

    def cleanup(self):
        """就地重写日志文件，剔除过期记录。"""
        cutoff = self._cutoff_ts()
        with self._lock:
            if not os.path.exists(self.path):
                return
            tmp = None
            kept = 0
            try:
                fd, tmp = tempfile.mkstemp(
                    prefix=os.path.basename(self.path) + ".cleanup.",
                    suffix=".tmp",
                    dir=os.path.dirname(os.path.abspath(self.path)),
                )
                os.close(fd)
                with open(self.path, "r", encoding="utf-8") as fi, \
                     open(tmp, "w", encoding="utf-8", newline="") as fo:
                    for line in fi:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except Exception:
                            continue
                        if r.get("ts", 0) >= cutoff:
                            fo.write(json.dumps(r, ensure_ascii=False) + "\n")
                            kept += 1
                os.replace(tmp, self.path)
            except Exception:
                try:
                    if tmp and os.path.exists(tmp):
                        os.remove(tmp)
                except Exception:
                    pass

    @staticmethod
    def _approx_record_len(rec):
        text = rec.get("text", "") or ""
        if rec.get("kind") == "clip":
            return len(text) + 60
        if rec.get("kind") == "key" and text.startswith("[*") and text.endswith("]"):
            return 0
        return len(text)

    def load_page_before(self, end_offset=None, target_chars=KEYLOG_CHUNK_CHARS,
                         max_records=KEYLOG_PAGE_MAX_RECORDS):
        """Read one bounded page ending before *end_offset* without loading all JSONL."""
        cutoff = self._cutoff_ts()
        with self._lock:
            if not os.path.exists(self.path):
                return [], 0, False
            try:
                with open(self.path, "rb") as f:
                    file_size = f.seek(0, os.SEEK_END)
                    end = file_size if end_offset is None else max(0, min(int(end_offset), file_size))
                    pos = end
                    data = b""
                    parsed = []
                    while pos > 0:
                        start = max(0, pos - 65536)
                        f.seek(start)
                        data = f.read(pos - start) + data
                        pos = start
                        lines = data.splitlines(keepends=True)
                        if pos > 0 and lines:
                            lines = lines[1:]
                        parsed = []
                        newest_ts = None
                        cursor = len(data) - sum(len(x) for x in lines)
                        for raw in lines:
                            line_start = pos + cursor
                            cursor += len(raw)
                            try:
                                rec = json.loads(raw.decode("utf-8").strip())
                            except Exception:
                                continue
                            rec_ts = rec.get("ts", 0)
                            newest_ts = rec_ts if newest_ts is None else max(newest_ts, rec_ts)
                            if rec_ts >= cutoff:
                                parsed.append((line_start, rec))
                        if newest_ts is not None and newest_ts < cutoff:
                            return [], 0, False
                        visible = 0
                        used = 0
                        for _, rec in reversed(parsed):
                            visible += self._approx_record_len(rec)
                            used += 1
                            if visible >= target_chars or used >= max_records:
                                break
                        if visible >= target_chars or used >= max_records or pos == 0:
                            break
                    selected = []
                    visible = 0
                    for line_start, rec in reversed(parsed):
                        if selected and (visible >= target_chars or len(selected) >= max_records):
                            break
                        selected.append((line_start, rec))
                        visible += self._approx_record_len(rec)
                    selected.reverse()
                    if not selected:
                        return [], 0, False
                    before = selected[0][0]
                    return [rec for _, rec in selected], before, before > 0
            except Exception:
                return [], 0, False

    @staticmethod
    def format_records(recs):
        """
        - 连续 key/ime → 首尾相接一坨字符串
        - clip → 单独一段
        - 同一 token 连续 >= 5 次 → 压缩为 TOKEN*n（包括单字符 token，
          如 aaaaa → a*5）
        """
        RUN_THRESHOLD = 5

        def flush_buf(tokens):
            if not tokens:
                return ""
            out = []
            i = 0
            n = len(tokens)
            while i < n:
                j = i + 1
                while j < n and tokens[j] == tokens[i]:
                    j += 1
                run = j - i
                tok = tokens[i]
                if run >= RUN_THRESHOLD:
                    out.append(f"{tok}*{run}")
                else:
                    out.append(tok * run)
                i = j
            return "".join(out)

        out_parts = []
        buf = []
        for r in recs:
            kind = r.get("kind")
            text = r.get("text", "") or ""
            if kind in ("key", "ime"):
                buf.append(text)
            elif kind == "clip":
                if buf:
                    out_parts.append(flush_buf(buf))
                    buf = []
                ts = r.get("ts", 0)
                try:
                    if LANG == "en":
                        # 英文标准：月日时分秒 (e.g. Jul 12 22:20:33)
                        hh = datetime.fromtimestamp(ts).strftime("%b %d %H:%M:%S")
                    else:
                        hh = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")
                except Exception:
                    hh = "?"
                clip_label = "Clipboard" if LANG == "en" else "剪贴板"
                out_parts.append(
                    f"\n\n───[{clip_label} {hh}]───\n{text}\n───[/{clip_label}]───\n\n"
                )
        if buf:
            out_parts.append(flush_buf(buf))
        return "".join(out_parts)

    def build_copy_text(self, max_chars=KEYLOG_COPY_MAX_CHARS):
        """Stream JSONL into bounded copy text without retaining record objects."""
        cutoff = self._cutoff_ts()
        parts = []
        char_count = 0
        run_token = None
        run_count = 0

        def emit(value):
            nonlocal char_count
            if not value:
                return
            char_count += len(value)
            if char_count > max_chars:
                raise ValueError("copy text too large")
            parts.append(value)

        def flush_run():
            nonlocal run_token, run_count
            if run_token is None:
                return
            if run_count >= 5:
                emit(f"{run_token}*{run_count}")
            else:
                emit(run_token * run_count)
            run_token = None
            run_count = 0

        with self._lock:
            if not os.path.exists(self.path):
                return ""
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("ts", 0) < cutoff:
                        continue
                    kind = rec.get("kind")
                    text = rec.get("text", "") or ""
                    if kind in ("key", "ime"):
                        if kind == "key" and text.startswith("[*") and text.endswith("]"):
                            continue
                        if text == run_token:
                            run_count += 1
                        else:
                            flush_run()
                            run_token = text
                            run_count = 1
                    elif kind == "clip":
                        flush_run()
                        try:
                            stamp = datetime.fromtimestamp(rec.get("ts", 0)).strftime(
                                "%b %d %H:%M:%S" if LANG == "en" else "%m-%d %H:%M:%S"
                            )
                        except Exception:
                            stamp = "?"
                        label = "Clipboard" if LANG == "en" else "剪贴板"
                        emit(f"\n\n───[{label} {stamp}]───\n{text}\n───[/{label}]───\n\n")
                flush_run()
        return "".join(parts)


# ============================================================
#              KeyHooker（键盘 + 快捷键 → key 事件）
# ============================================================
# 特殊键映射：keyboard 库返回的 name → 标签
# 注意：所有标签统一 `[*xxx]` 前缀，避免和源代码/终端里的普通 `[xxx]` 语法碰撞
_SPECIAL_KEY_LABELS = {
    "backspace": "[*退格]",
    "delete": "[*Del]",
    "enter": "[*Enter]",
    "tab": "[*Tab]",
    "esc": "[*Esc]",
    "space": " ",
    "up": "[*↑]",
    "down": "[*↓]",
    "left": "[*←]",
    "right": "[*→]",
    "home": "[*Home]",
    "end": "[*End]",
    "page up": "[*PgUp]",
    "page down": "[*PgDn]",
    "insert": "[*Ins]",
    "print screen": "[*PrtSc]",
    "caps lock": "[*CapsLock]",
    "num lock": "[*NumLock]",
    "scroll lock": "[*ScrLk]",
    "pause": "[*Pause]",
    "menu": "[*Menu]",
}
# 修饰键集合（判断快捷键组合）
_MOD_KEYS = {
    "ctrl", "left ctrl", "right ctrl",
    "alt", "left alt", "right alt", "alt gr",
    "shift", "left shift", "right shift",
    "windows", "left windows", "right windows",
    "cmd", "left cmd", "right cmd",
}


def _is_cjk(s):
    """列中的字符全部为 CJK / 中日韩全角标点时返回 True。空串返回 False。"""
    return bool(s) and all(
        '\u3000' <= c <= '\u9fff' or
        '\uff00' <= c <= '\uffef' or
        '\u3400' <= c <= '\u4dbf'
        for c in s
    )


def _canonical_mod(name):
    n = name.lower()
    if n in ("ctrl", "left ctrl", "right ctrl"):
        return "ctrl"
    if n in ("alt", "left alt", "right alt", "alt gr"):
        return "alt"
    if n in ("shift", "left shift", "right shift"):
        return "shift"
    if n in ("windows", "left windows", "right windows", "cmd",
             "left cmd", "right cmd"):
        return "win"
    return None


class KeyHooker:
    ASCII_FLUSH_DELAY = 1.5   # IME 关闭时字母/数字入 log 的延迟（真英文输入）
    IME_DROP_TIMEOUT = 8.0    # IME 活跃时入 buf 的字符，超时未被 UIA discard 则丢弃（拼音码）
    IME_STATE_CACHE_SEC = 0.25

    def __init__(self, store, app):
        self.store = store
        self.app = app
        self._active_mods = set()
        # buf entries: (ts, ch, ime_active_at_press)
        self._ascii_buf = []
        self._ascii_lock = threading.Lock()
        self._flush_stop = threading.Event()
        self._flush_thread = None
        self._ime_cache = False
        self._ime_cache_ts = 0.0
        # 单调递增的按键计数器：供 UIAWatcher 判断“真的有人在敊键”（只统计非修饰键的 down）
        self._press_count = 0
        self._last_press_monotonic = 0.0

    def get_press_count(self):
        return self._press_count

    def had_recent_press(self, max_age=3.0):
        return (time.monotonic() - self._last_press_monotonic) <= max_age

    def _cached_ime_state(self):
        now = time.time()
        if now - self._ime_cache_ts < self.IME_STATE_CACHE_SEC:
            return self._ime_cache
        try:
            self._ime_cache = _get_ime_active()
        except Exception:
            self._ime_cache = False
        self._ime_cache_ts = now
        return self._ime_cache

    def start(self):
        keyboard.hook(self._on_event, suppress=False)
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()

    def stop(self):
        self._flush_stop.set()
        # 退出时将缓冲全部刷入
        self._flush_pending(force=True)

    def discard_ascii_buffer(self):
        """UIA 捕获到 IME 提交时调用：丢弃尚未刷入的拼音码"""
        with self._ascii_lock:
            self._ascii_buf.clear()

    def _flush_pending(self, force=False):
        now = time.time()
        to_flush = []
        with self._ascii_lock:
            keep = []
            for item in self._ascii_buf:
                ts, ch, ime_on = item
                age = now - ts
                if force:
                    # 退出/需要强制 flush：非 IME 写入，IME 时丢弃
                    if not ime_on:
                        to_flush.append((ts, ch))
                    continue
                if ime_on:
                    # IME 活跃期待到 UIA discard；超时则当拼音码丢弃
                    if age >= self.IME_DROP_TIMEOUT:
                        continue  # drop
                    keep.append(item)
                else:
                    if age >= self.ASCII_FLUSH_DELAY:
                        to_flush.append((ts, ch))
                    else:
                        keep.append(item)
            self._ascii_buf = keep
        for ts, ch in to_flush:
            self.store.append("key", ch)

    def _flush_loop(self):
        while not self._flush_stop.wait(0.2):
            try:
                self._flush_pending(force=False)
            except Exception:
                pass

    def _on_event(self, event):
        if not self.store.keylog_enabled:
            with self._ascii_lock:
                self._ascii_buf.clear()
            self._active_mods.clear()
            return
        try:
            name = (event.name or "").lower()
        except Exception:
            return
        etype = event.event_type

        # 维护修饰键状态
        mod = _canonical_mod(name)
        if mod is not None:
            if etype == "down":
                self._active_mods.add(mod)
            elif etype == "up":
                self._active_mods.discard(mod)
            return

        if etype != "down":
            return

        # 非修饰键的 keydown → 计数 +1（供 UIAWatcher 作“输入人证”）
        self._press_count += 1
        self._last_press_monotonic = time.monotonic()

        # 任何非 ascii 事件到达：先把 buffer 里的字母/数字刷到磁盘，保证时序
        # （仅当下一行将写入非 ascii 标签时才需要）
        modifier_no_shift = {m for m in self._active_mods if m != "shift"}

        # 快捷键组合：Ctrl / Alt / Win 与主键一起按下
        if modifier_no_shift:
            key_repr = self._pretty_main_key(name)
            if key_repr is None:
                return
            parts = []
            if "ctrl" in self._active_mods:
                parts.append("Ctrl")
            if "alt" in self._active_mods:
                parts.append("Alt")
            if "shift" in self._active_mods:
                parts.append("Shift")
            if "win" in self._active_mods:
                parts.append("Win")
            parts.append(key_repr)
            self._flush_pending(force=True)
            self.store.append("key", "[*" + "+".join(parts) + "]")
            return

        # 特殊键标签
        if name in _SPECIAL_KEY_LABELS:
            self._flush_pending(force=True)
            self.store.append("key", _SPECIAL_KEY_LABELS[name])
            return

        # 功能键 F1-F24
        if re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", name):
            self._flush_pending(force=True)
            self.store.append("key", f"[*{name.upper()}]")
            return

        # 打印字符
        ch = event.name
        if ch and len(ch) == 1 and ch.isprintable():
            if ch.isalpha() and "shift" in self._active_mods:
                ch = ch.upper()
            if ch.isalpha() or ch.isdigit():
                # 可能是 IME 拼音码/选词：先入缓冲，同时记录此刻 IME 状态
                ime_on = self._cached_ime_state()
                with self._ascii_lock:
                    self._ascii_buf.append((time.time(), ch, ime_on))
                return
            # 标点/符号——直接写入，先 flush 保证序
            self._flush_pending(force=True)
            self.store.append("key", ch)
            return
        # 其他未识别键 —— 忽略

    def _pretty_main_key(self, name):
        if not name:
            return None
        if name in _SPECIAL_KEY_LABELS:
            # 用去掉方括号 + 前缀 * 的可读名
            return _SPECIAL_KEY_LABELS[name].strip("[]*") or name
        if re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", name):
            return name.upper()
        if len(name) == 1:
            return name.upper() if name.isalpha() else name
        # 例如 "num 0" 之类
        return name


# ============================================================
#          ClipboardWatcher（轮询 sequence number）
# ============================================================
# 全局只有一个 ClipboardWatcher（由 ScreenSearchApp 创建），
# 任意位置要写剪贴板时，先调 suppress_clipboard(text) 告诉它 "这块内容不要记入日志"。
_CLIP_WATCHER_REF = None


def suppress_clipboard(text):
    """告诉全局剪贴板监听器下一次自己写入的 text 不要记入日志。未启动时静默忽略。"""
    w = _CLIP_WATCHER_REF
    if w is not None:
        try:
            w.suppress_next(text)
        except Exception:
            pass


class ClipboardWatcher(threading.Thread):
    CF_UNICODETEXT = 13

    def __init__(self, store):
        super().__init__(daemon=True)
        self.store = store
        self._stop = threading.Event()
        self._last_seq = None
        # 自记录抑制：当本程序自己写入剪贴板（复制日志/选区）时，先记下预期文本，
        # 下一次 sequence 变化如果内容匹配则丢弃，避免 “复制全部” 后又把自己再记一次。
        self._suppress_lock = threading.Lock()
        self._suppress_texts = []  # list[tuple[float, str]]：(expires_at, text)
        # 64 位下 HANDLE/LPVOID 必须显式声明，否则被截为 32 位 0
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        u32.OpenClipboard.argtypes = [wt.HWND]
        u32.OpenClipboard.restype = wt.BOOL
        u32.CloseClipboard.argtypes = []
        u32.CloseClipboard.restype = wt.BOOL
        u32.GetClipboardData.argtypes = [wt.UINT]
        u32.GetClipboardData.restype = wt.HANDLE
        u32.GetClipboardSequenceNumber.argtypes = []
        u32.GetClipboardSequenceNumber.restype = wt.DWORD
        k32.GlobalLock.argtypes = [wt.HANDLE]
        k32.GlobalLock.restype = ctypes.c_void_p
        k32.GlobalUnlock.argtypes = [wt.HANDLE]
        k32.GlobalUnlock.restype = wt.BOOL
        k32.GlobalSize.argtypes = [wt.HANDLE]
        k32.GlobalSize.restype = ctypes.c_size_t

    def stop(self):
        self._stop.set()

    def suppress_next(self, text):
        """告诉 watcher：接下来会有一次剪贴板变化，内容就是 text，不要记入日志。
        有效期 3 秒，避免卡住真人后续复制。"""
        if not text:
            return
        # 归一化：Windows 剪贴板往返可能把 \n 换成 \r\n，比较时统一去 \r
        expected = text.replace("\r\n", "\n").replace("\r", "\n")
        now = time.monotonic()
        with self._suppress_lock:
            self._suppress_texts = [(t, s) for (t, s) in self._suppress_texts if t > now]
            self._suppress_texts.append((now + 3.0, expected))

    def _consume_suppress(self, text):
        if not text:
            return False
        # 归一化：与 suppress_next 一致，去掉 \r 后再比较
        norm = text.replace("\r\n", "\n").replace("\r", "\n")
        now = time.monotonic()
        with self._suppress_lock:
            new_list = []
            hit = False
            for expires, expected in self._suppress_texts:
                if expires <= now:
                    continue
                if not hit and expected == norm:
                    hit = True
                    continue
                new_list.append((expires, expected))
            self._suppress_texts = new_list
            return hit

    def _read_text(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        opened = False
        try:
            if not user32.OpenClipboard(None):
                return None
            opened = True
            handle = user32.GetClipboardData(self.CF_UNICODETEXT)
            if not handle:
                return None
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return None
            try:
                size_bytes = int(kernel32.GlobalSize(handle))
                if size_bytes <= 0:
                    return None
                max_chars = min(
                    CLIPBOARD_MAX_CHARS,
                    max(0, size_bytes // ctypes.sizeof(ctypes.c_wchar)),
                )
                text = ctypes.wstring_at(ptr, max_chars).split("\x00", 1)[0]
            finally:
                kernel32.GlobalUnlock(handle)
            return text
        except Exception:
            return None
        finally:
            if opened:
                try:
                    user32.CloseClipboard()
                except Exception:
                    pass

    def run(self):
        user32 = ctypes.windll.user32
        try:
            self._last_seq = user32.GetClipboardSequenceNumber()
        except Exception:
            self._last_seq = 0
        while not self._stop.is_set():
            try:
                seq = user32.GetClipboardSequenceNumber()
                if seq != self._last_seq:
                    self._last_seq = seq
                    if not self.store.clipboard_enabled:
                        self._stop.wait(0.25)
                        continue
                    text = self._read_text()
                    if text:
                        # 本程序自己写的剪贴板（复制日志/选区）不记入，避免自循环
                        if self._consume_suppress(text):
                            continue
                        if len(text) >= CLIPBOARD_MAX_CHARS:
                            text = text[:CLIPBOARD_MAX_CHARS] + "…（截断）"
                        self.store.append("clip", text)
            except Exception:
                pass
            self._stop.wait(0.25)


# ============================================================
#      UIAWatcher（焦点控件 Value diff → 抓 IME 中文提交）
# ============================================================
# UIA 依赖探测（comtypes + uiautomation），惰性缓存一次
_UIA_AVAILABLE = None


def uia_available():
    global _UIA_AVAILABLE
    if _UIA_AVAILABLE is not None:
        return _UIA_AVAILABLE
    try:
        import comtypes  # noqa: F401
        import uiautomation  # noqa: F401
        _UIA_AVAILABLE = True
    except Exception:
        _UIA_AVAILABLE = False
    return _UIA_AVAILABLE


class UIAWatcher(threading.Thread):
    """
    低频轮询前台焦点控件的 Value / Text，若新增内容里包含 CJK 段落，
    把这些 CJK 段追加到日志。避开把普通英文重复记录。
    """
    CJK_RE = re.compile(
        r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002ffff"
        r"\u3040-\u309f\u30a0-\u30ff\uff00-\uffef]+"
    )

    def __init__(self, store, key_hooker=None):
        super().__init__(daemon=True)
        self.store = store
        self.key_hooker = key_hooker
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def _get_value(self, ctrl):
        # 尝试多种模式取当前文本
        try:
            vp = ctrl.GetValuePattern()
            v = vp.Value
            if v is not None:
                return v
        except Exception:
            pass
        try:
            tp = ctrl.GetTextPattern()
            v = tp.DocumentRange.GetText(-1)
            if v is not None:
                return v
        except Exception:
            pass
        try:
            lp = ctrl.GetLegacyIAccessiblePattern()
            v = lp.Value
            if v is not None:
                return v
        except Exception:
            pass
        return None

    def _get_placeholder(self, ctrl):
        """尝试拿控件的 placeholder / help text（若有）。"""
        try:
            v = getattr(ctrl, "HelpText", None)
            if v:
                return v
        except Exception:
            pass
        return None

    def run(self):
        # COM 初始化（STA）
        try:
            import comtypes
            try:
                comtypes.CoInitializeEx(0)  # 0 = STA
            except Exception:
                pass
        except Exception:
            pass

        try:
            import uiautomation as auto
        except Exception:
            return

        last_ctrl_key = None
        last_value = ""
        last_press_count = 0
        # 以控件 RuntimeId 为键的最近提交去重：{(rid, seg_text): expires_at}
        recent_commits = {}
        get_press = self.key_hooker.get_press_count if self.key_hooker else (lambda: 0)
        had_recent_press = self.key_hooker.had_recent_press if self.key_hooker else (lambda max_age=3.0: False)

        while not self._stop.is_set():
            if not (self.store.keylog_enabled and self.store.uia_enabled):
                last_ctrl_key = None
                last_value = ""
                last_press_count = get_press()
                recent_commits.clear()
                self._stop.wait(0.4)
                continue
            try:
                ctrl = auto.GetFocusedControl()
                if ctrl is None:
                    self._stop.wait(0.4)
                    continue
                try:
                    # RuntimeId 是 UIA 对控件实例的唯一标识（tuple[int]），
                    # 比 hwnd/name 更稳：同一控件在编辑时 Name 会变，但 RuntimeId 不变。
                    rid = None
                    try:
                        rid = tuple(ctrl.GetRuntimeId() or ())
                    except Exception:
                        rid = None
                    ck = rid if rid else (
                        ctrl.ControlTypeName,
                        ctrl.AutomationId or "",
                        ctrl.Name or "",
                        ctrl.NativeWindowHandle,
                    )
                except Exception:
                    ck = None
                val = self._get_value(ctrl) or ""

                if ck != last_ctrl_key:
                    last_ctrl_key = ck
                    last_value = val
                    last_press_count = get_press()
                    self._stop.wait(0.35)
                    continue

                if val != last_value:
                    cur_press = get_press()
                    press_advanced = cur_press > last_press_count
                    # Programmatic updates and incoming messages must not be logged.
                    # A short recent-key window still permits mouse-selected IME candidates.
                    if not press_advanced and not had_recent_press(3.0):
                        last_value = val
                        last_press_count = cur_press
                        self._stop.wait(0.35)
                        continue

                    # 找共同前缀（增量 diff）
                    n = 0
                    m = min(len(val), len(last_value))
                    while n < m and val[n] == last_value[n]:
                        n += 1
                    # [2026-07-13 diag] 二号嫌犯 len<rebase 暂时关闭。
                    # 本意：网页自动填充撤销回退 val 变短，不当新输入。
                    # 副作用：IME 上屏 "baizheshichen"(13)→"白折时辰"(4)，
                    # 4<13 直接 rebase，"白折时辰"永远不进 log。这是本次主凶。
                    # if len(val) < len(last_value):
                    #     last_value = val
                    #     last_press_count = cur_press
                    #     self._stop.wait(0.35)
                    #     continue
                    added = val[n:]
                    last_value = val
                    last_press_count = cur_press

                    if added:
                        # 防御：新增内容恰好等于 placeholder / HelpText → 略过
                        placeholder = self._get_placeholder(ctrl)
                        if placeholder and added.strip() == placeholder.strip():
                            self._stop.wait(0.35)
                            continue

                        # 只吸收其中的 CJK / 假名 / 全角段
                        cjk_segs = self.CJK_RE.findall(added)
                        # 只要新增内容非纯拉丁字母数字（含 CJK/假名/全角标点/emoji）
                        # 都当作 IME 提交信号，丢掉 ascii buf 里的拼音码
                        has_non_ascii = any(ord(c) > 127 for c in added)
                        if (cjk_segs or has_non_ascii) and self.key_hooker is not None:
                            try:
                                self.key_hooker.discard_ascii_buffer()
                            except Exception:
                                pass
                        for seg in cjk_segs:
                            if not seg:
                                continue
                            # 去重：同一控件上 2 秒内重复提交同一段 CJK 文本 → 丢弃
                            rid_key = (ck, seg)
                            now = time.monotonic()
                            # 清理过期
                            recent_commits = {k: exp for k, exp in recent_commits.items() if exp > now}
                            if rid_key in recent_commits:
                                continue
                            recent_commits[rid_key] = now + 2.0
                            self.store.append("ime", seg)
            except Exception:
                pass
            self._stop.wait(0.35)


# ============================================================
#                 内置文本编辑器
# ============================================================
def _count_document_characters(content):
    """统计非空白字符，作为中英文通用的字数。"""
    return sum(1 for char in content if not char.isspace())


def _write_text_document(path, content):
    """以 UTF-8 保存文本文档。"""
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)


def _scaled_editor_metrics(screen_height):
    """以 2560×1440 为基准，按右上角锚点等比缩放窗口。"""
    scale = max(0.25, float(screen_height) / TEXT_EDITOR_REFERENCE_HEIGHT)
    width = max(1, round(TEXT_EDITOR_WIDTH * scale))
    height = max(1, round(TEXT_EDITOR_HEIGHT * scale))
    right = round(TEXT_EDITOR_RIGHT * scale)
    return {
        "scale": scale,
        "right": right,
        "x": right - width,
        "y": round(TEXT_EDITOR_Y * scale),
        "width": width,
        "height": height,
    }


def _scaled_editor_geometry(virtual_bounds, screen_height):
    """计算缩放后的 Tk geometry，严格保留指定的虚拟桌面坐标。"""
    del virtual_bounds  # 坐标可指向当前会话不可见的右侧副屏，不做单屏夹取。
    metrics = _scaled_editor_metrics(screen_height)
    return (
        f"{metrics['width']}x{metrics['height']}"
        f"{metrics['x']:+d}{metrics['y']:+d}",
        metrics["scale"],
    )


def _scroll_thumb_color(opacity=TEXT_EDITOR_SCROLL_OPACITY):
    """将深灰滑块按指定不透明度与白色背景预混合。"""
    opacity = min(1.0, max(0.0, float(opacity)))
    channel = round(90 * opacity + 255 * (1.0 - opacity))
    return f"#{channel:02x}{channel:02x}{channel:02x}"


class _DWM_MARGINS(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth", ctypes.c_int),
        ("cxRightWidth", ctypes.c_int),
        ("cyTopHeight", ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int),
    ]


def _window_handle(win):
    """返回 Tk 顶层窗口对应的原生 Windows 外框句柄。"""
    win.update_idletasks()
    frame = win.wm_frame()
    return int(frame, 16) if isinstance(frame, str) else int(frame)


def _enable_native_window_shadow(win, dwmapi=None, hwnd=None):
    """要求 DWM 按当前原生窗口样式绘制非客户区和系统阴影。"""
    try:
        hwnd = _window_handle(win) if hwnd is None else int(hwnd)
        if not hwnd:
            return False
        if dwmapi is None:
            dwmapi = ctypes.WinDLL("dwmapi")

        dwmapi.DwmSetWindowAttribute.argtypes = [
            wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD,
        ]
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
        dwmapi.DwmExtendFrameIntoClientArea.argtypes = [
            wt.HWND, ctypes.POINTER(_DWM_MARGINS),
        ]
        dwmapi.DwmExtendFrameIntoClientArea.restype = ctypes.c_long

        # DWMWA_NCRENDERING_POLICY=2, DWMNCRP_ENABLED=2
        policy = ctypes.c_int(2)
        policy_result = dwmapi.DwmSetWindowAttribute(
            hwnd, 2, ctypes.byref(policy), ctypes.sizeof(policy)
        )
        margins = _DWM_MARGINS(1, 1, 1, 1)
        frame_result = dwmapi.DwmExtendFrameIntoClientArea(
            hwnd, ctypes.byref(margins)
        )
        return policy_result >= 0 and frame_result >= 0
    except Exception:
        return False


def _configure_native_frameless_window(win, user32=None, dwmapi=None):
    """保留 DWM 原生框架能力，同时把 Tk 客户区扩展成无标题栏外观。

    返回值必须由调用方持有：其中的 Python WndProc 回调在 HWND 生命周期内
    不能被垃圾回收。
    """
    try:
        hwnd = _window_handle(win)
        if not hwnd:
            return None
        if user32 is None:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
        if dwmapi is None:
            dwmapi = ctypes.WinDLL("dwmapi")

        get_window_long = user32.GetWindowLongW
        get_window_long.argtypes = [wt.HWND, ctypes.c_int]
        get_window_long.restype = ctypes.c_long
        set_window_long_ptr = user32.SetWindowLongPtrW
        set_window_long_ptr.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
        set_window_long_ptr.restype = ctypes.c_void_p
        call_window_proc = user32.CallWindowProcW
        call_window_proc.argtypes = [
            ctypes.c_void_p, wt.HWND, wt.UINT, ctypes.c_size_t,
            ctypes.c_ssize_t,
        ]
        call_window_proc.restype = ctypes.c_ssize_t
        set_window_pos = user32.SetWindowPos
        set_window_pos.argtypes = [
            wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wt.UINT,
        ]
        set_window_pos.restype = wt.BOOL

        # 保留标准顶层窗口样式，让 DWM 将它视为可绘制原生阴影的窗口。
        gwl_style = -16
        gwlp_wndproc = -4
        ws_caption = 0x00C00000
        ws_thickframe = 0x00040000
        ws_sysmenu = 0x00080000
        ws_minimizebox = 0x00020000
        style = int(get_window_long(hwnd, gwl_style))
        native_style = (
            style | ws_caption | ws_thickframe | ws_sysmenu | ws_minimizebox
        )
        user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
        user32.SetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW(hwnd, gwl_style, native_style)

        wm_nccalcsize = 0x0083
        wm_ncaactivate = 0x0086
        wndproc_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wt.HWND, wt.UINT,
            ctypes.c_size_t, ctypes.c_ssize_t,
        )
        original_wndproc = ctypes.c_void_p()

        @wndproc_type
        def frameless_wndproc(message_hwnd, message, wparam, lparam):
            try:
                if message == wm_nccalcsize and wparam:
                    # 整个窗口矩形都交给 Tk 绘制；原生样式仍留给 DWM 生成阴影。
                    return 0
                if message == wm_ncaactivate:
                    # 阻止激活状态变化触发标准标题栏重绘。
                    return 1
                return call_window_proc(
                    original_wndproc, message_hwnd, message, wparam, lparam
                )
            except Exception:
                return call_window_proc(
                    original_wndproc, message_hwnd, message, wparam, lparam
                )

        callback_ptr = ctypes.cast(frameless_wndproc, ctypes.c_void_p)
        previous = set_window_long_ptr(hwnd, gwlp_wndproc, callback_ptr)
        previous_value = (
            previous.value if isinstance(previous, ctypes.c_void_p) else int(previous)
        )
        if not previous_value:
            return None
        original_wndproc.value = previous_value

        # SWP_FRAMECHANGED 使新样式立即重新计算；不移动、不缩放、不抢焦点。
        swp_flags = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020
        set_window_pos(hwnd, 0, 0, 0, 0, 0, swp_flags)
        if not _enable_native_window_shadow(win, dwmapi, hwnd=hwnd):
            set_window_long_ptr(
                hwnd, gwlp_wndproc, ctypes.c_void_p(original_wndproc.value)
            )
            return None
        return {
            "hwnd": hwnd,
            "wndproc": frameless_wndproc,
            "original_wndproc": original_wndproc,
        }
    except Exception:
        return None


def _set_native_window_bounds(hwnd, metrics, user32=None):
    """按外框尺寸精确定位 HWND，避免 Tk 把隐藏边框再次计入 geometry。"""
    try:
        if user32 is None:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetWindowPos.argtypes = [
            wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wt.UINT,
        ]
        user32.SetWindowPos.restype = wt.BOOL
        # SWP_NOZORDER | SWP_NOACTIVATE
        return bool(user32.SetWindowPos(
            int(hwnd), 0,
            int(metrics["x"]), int(metrics["y"]),
            int(metrics["width"]), int(metrics["height"]),
            0x0004 | 0x0010,
        ))
    except Exception:
        return False


def _virtual_screen_metrics(win):
    """返回虚拟桌面边界和主屏物理高度。"""
    try:
        user32 = ctypes.windll.user32
        left = int(user32.GetSystemMetrics(76))
        top = int(user32.GetSystemMetrics(77))
        width = int(user32.GetSystemMetrics(78))
        height = int(user32.GetSystemMetrics(79))
        primary_height = int(user32.GetSystemMetrics(1))
        if width <= 0 or height <= 0 or primary_height <= 0:
            raise OSError("GetSystemMetrics failed")
        return (left, top, left + width, top + height), primary_height
    except Exception:
        width = win.winfo_screenwidth()
        height = win.winfo_screenheight()
        return (0, 0, width, height), height


class TextDocumentWindow:
    """轻量文本编辑器：自动换行、14 号字、字数统计与保存。"""

    def __init__(self, root, on_closed=None):
        self.file_path = None
        self._dirty = False
        self._on_closed = on_closed
        self._closed = False
        self._native_frame = None

        self.win = tk.Toplevel(root)
        # 在原生框架完成配置前保持隐藏，避免标准标题栏短暂闪现。
        self.win.withdraw()
        virtual_bounds, screen_height = _virtual_screen_metrics(self.win)
        geometry, self._ui_scale = _scaled_editor_geometry(
            virtual_bounds, screen_height
        )
        self._editor_metrics = _scaled_editor_metrics(screen_height)
        self._editor_geometry = geometry
        self.win.geometry(geometry)
        self.win.protocol("WM_DELETE_WINDOW", self._close)
        self.win.configure(bg="#ffffff")

        top_height = max(30, round(38 * self._ui_scale))
        self.top_bar = tk.Frame(
            self.win, bg="#f3f3f3", height=top_height, bd=0,
            highlightthickness=0,
        )
        self.top_bar.pack(fill="x", side="top")
        self.top_bar.pack_propagate(False)

        self._file_button = tk.Menubutton(
            self.top_bar,
            bg="#f3f3f3",
            fg="#202020",
            activebackground="#e2e2e2",
            activeforeground="#202020",
            relief="flat",
            bd=0,
            font=("Microsoft YaHei UI", 10),
            padx=max(9, round(12 * self._ui_scale)),
        )
        self._file_button.pack(side="left", fill="y")

        self._close_button = tk.Button(
            self.top_bar,
            text="×",
            command=self._close,
            bg="#f3f3f3",
            fg="#333333",
            activebackground="#e81123",
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            font=("Segoe UI", 13),
            width=3,
        )
        self._close_button.pack(side="right", fill="y")

        self.count_label = tk.Label(
            self.top_bar,
            anchor="e",
            bg="#f3f3f3",
            fg="#555555",
            font=("Microsoft YaHei UI", 9),
            padx=max(8, round(10 * self._ui_scale)),
        )
        self.count_label.pack(side="right", fill="y")

        for drag_widget in (self.top_bar, self.count_label):
            drag_widget.bind("<ButtonPress-1>", self._start_drag)
            drag_widget.bind("<B1-Motion>", self._drag_window)

        body = tk.Frame(self.win, bg="#ffffff")
        body.pack(fill="both", expand=True)

        self.text = tk.Text(
            body,
            wrap=TEXT_EDITOR_WRAP,
            undo=True,
            maxundo=-1,
            font=("Microsoft YaHei UI", TEXT_EDITOR_FONT_SIZE),
            bg="#ffffff",
            fg="#202020",
            insertbackground="#202020",
            selectbackground="#b7d7ff",
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=max(10, round(14 * self._ui_scale)),
            pady=max(8, round(12 * self._ui_scale)),
            yscrollcommand=self._on_text_scroll,
        )
        self.text.pack(fill="both", expand=True)

        self._scroll_width = max(7, round(10 * self._ui_scale))
        self._scroll_range = (0.0, 1.0)
        self._scroll_thumb = (0, 0)
        self._scroll_drag_offset = None
        self.scroll_overlay = tk.Canvas(
            body,
            width=self._scroll_width,
            bg="#ffffff",
            bd=0,
            highlightthickness=0,
            cursor="sb_v_double_arrow",
        )
        self.scroll_overlay.bind("<Configure>", self._redraw_scroll_overlay)
        self.scroll_overlay.bind("<ButtonPress-1>", self._scrollbar_press)
        self.scroll_overlay.bind("<B1-Motion>", self._scrollbar_drag)
        self.scroll_overlay.bind(
            "<ButtonRelease-1>", lambda _event: setattr(
                self, "_scroll_drag_offset", None
            )
        )
        self.scroll_overlay.bind("<MouseWheel>", self._scrollbar_wheel)

        self.text.bind("<<Modified>>", self._on_modified)
        self.win.bind("<Control-s>", self._save_shortcut)
        self.win.bind("<Control-Shift-s>", self._save_as_shortcut)
        self.win.bind("<Control-Shift-S>", self._save_as_shortcut)

        self._lang_listener = self._refresh_language
        register_lang_listener(self._lang_listener)
        self._refresh_language()
        self.text.edit_modified(False)
        self._update_count()

        # 创建时短暂置顶以确保可见，随后恢复普通窗口层级。
        try:
            self.win.attributes("-topmost", True)
            self.win.after_idle(self._show_with_native_frame)
        except Exception:
            self.win.deiconify()
            self.text.focus_set()

    def _build_menu(self):
        old_menu = getattr(self, "_file_menu", None)
        file_menu = tk.Menu(self._file_button, tearoff=False)
        file_menu.add_command(
            label=t("text_editor_save"), accelerator="Ctrl+S", command=self._save
        )
        file_menu.add_command(
            label=t("text_editor_save_as"),
            accelerator="Ctrl+Shift+S",
            command=self._save_as,
        )
        file_menu.add_separator()
        file_menu.add_command(label=t("text_editor_close"), command=self._close)
        self._file_button.config(text=t("text_editor_file"), menu=file_menu)
        self._file_menu = file_menu
        if old_menu is not None:
            try:
                old_menu.destroy()
            except Exception:
                pass

    def _start_drag(self, event):
        self._drag_offset = (
            event.x_root - self.win.winfo_x(),
            event.y_root - self.win.winfo_y(),
        )

    def _drag_window(self, event):
        try:
            offset_x, offset_y = self._drag_offset
            x = event.x_root - offset_x
            y = event.y_root - offset_y
            self.win.geometry(f"{x:+d}{y:+d}")
        except Exception:
            pass

    def _focus_editor(self):
        try:
            self.win.lift()
            self.win.focus_force()
            self.text.focus_set()
        except Exception:
            pass

    def _on_text_scroll(self, first, last):
        first = float(first)
        last = float(last)
        self._scroll_range = (first, last)
        overlay = getattr(self, "scroll_overlay", None)
        if overlay is None:
            return
        if first <= 0.0 and last >= 1.0:
            overlay.place_forget()
            return
        overlay.place(
            relx=1.0,
            x=-max(3, round(4 * self._ui_scale)),
            y=max(4, round(6 * self._ui_scale)),
            anchor="ne",
            width=self._scroll_width,
            relheight=1.0,
            height=-max(8, round(12 * self._ui_scale)),
        )
        overlay.tk.call("raise", overlay._w)
        overlay.after_idle(self._redraw_scroll_overlay)

    def _redraw_scroll_overlay(self, _event=None):
        overlay = getattr(self, "scroll_overlay", None)
        if overlay is None or not overlay.winfo_ismapped():
            return
        first, last = self._scroll_range
        height = max(1, overlay.winfo_height())
        width = max(1, overlay.winfo_width())
        thumb_top = round(first * height)
        thumb_bottom = round(last * height)
        min_thumb = max(24, round(30 * self._ui_scale))
        if thumb_bottom - thumb_top < min_thumb:
            thumb_bottom = min(height, thumb_top + min_thumb)
            thumb_top = max(0, thumb_bottom - min_thumb)
        self._scroll_thumb = (thumb_top, thumb_bottom)
        overlay.delete("all")
        overlay.create_rectangle(
            0,
            thumb_top,
            width,
            thumb_bottom,
            fill=_scroll_thumb_color(),
            outline="",
        )

    def _scrollbar_press(self, event):
        top, bottom = self._scroll_thumb
        if top <= event.y <= bottom:
            self._scroll_drag_offset = event.y - top
        else:
            self._scroll_drag_offset = max(0, (bottom - top) // 2)
            self._scrollbar_drag(event)

    def _scrollbar_drag(self, event):
        if self._scroll_drag_offset is None:
            return
        height = max(1, self.scroll_overlay.winfo_height())
        thumb_height = max(1, self._scroll_thumb[1] - self._scroll_thumb[0])
        max_top = max(1, height - thumb_height)
        thumb_top = min(max(0, event.y - self._scroll_drag_offset), max_top)
        self.text.yview_moveto(thumb_top / height)

    def _scrollbar_wheel(self, event):
        units = -1 if event.delta > 0 else 1
        self.text.yview_scroll(units * 3, "units")
        return "break"

    def _focus_and_release_topmost(self):
        try:
            self.win.attributes("-topmost", False)
            self._focus_editor()
        except Exception:
            pass

    def _show_with_native_frame(self):
        """配置原生无框外观后再显示，映射后重申 DWM 阴影策略。"""
        try:
            # Tk 会在 withdrawn -> normal 时重建外层 HWND；必须先映射，再对子类化
            # 最终句柄。先透明显示可避免标准标题栏在这一步短暂闪现。
            self.win.attributes("-alpha", 0.0)
            self.win.deiconify()
            # 映射消息处理完后 wm_frame() 才会从内部 Tk 子窗口切换为真正
            # 的顶层包装 HWND。
            self.win.after(20, self._finish_native_frame_setup)
        except Exception:
            self.win.deiconify()
            self._focus_and_release_topmost()

    def _finish_native_frame_setup(self):
        try:
            self._native_frame = _configure_native_frameless_window(self.win)
            if self._native_frame:
                _set_native_window_bounds(
                    self._native_frame["hwnd"], self._editor_metrics
                )
            else:
                self.win.geometry(self._editor_geometry)
            self.win.attributes("-alpha", 1.0)
            self.win.after(
                20,
                lambda: _enable_native_window_shadow(
                    self.win,
                    hwnd=(self._native_frame or {}).get("hwnd"),
                ),
            )
            self.win.after(120, self._focus_and_release_topmost)
        except Exception:
            self.win.attributes("-alpha", 1.0)
            self._focus_and_release_topmost()

    def _document_name(self):
        if self.file_path:
            return os.path.basename(self.file_path)
        return t("text_editor_untitled")

    def _update_title(self):
        dirty = "*" if self._dirty else ""
        self.win.title(
            f"{dirty}{self._document_name()} - {t('text_editor_suffix')}"
        )

    def _content(self):
        return self.text.get("1.0", "end-1c")

    def _update_count(self):
        count = _count_document_characters(self._content())
        self.count_label.config(text=t("text_editor_chars", count=count))

    def _on_modified(self, _event=None):
        if not self.text.edit_modified():
            return
        self._dirty = True
        self.text.edit_modified(False)
        self._update_title()
        self._update_count()

    def _save_shortcut(self, _event=None):
        self._save()
        return "break"

    def _save_as_shortcut(self, _event=None):
        self._save_as()
        return "break"

    def _save(self):
        if not self.file_path:
            return self._save_as()
        try:
            _write_text_document(self.file_path, self._content())
        except OSError as exc:
            messagebox.showerror(
                t("text_editor_save_error_title"),
                t("text_editor_save_error", err=exc),
                parent=self.win,
            )
            return False
        self._dirty = False
        self._update_title()
        return True

    def _save_as(self):
        path = filedialog.asksaveasfilename(
            parent=self.win,
            title=t("text_editor_save_as"),
            defaultextension=".txt",
            filetypes=[
                (t("text_editor_filetype"), "*.txt"),
                (t("text_editor_all_files"), "*.*"),
            ],
        )
        if not path:
            return False
        previous_path = self.file_path
        self.file_path = path
        if self._save():
            return True
        self.file_path = previous_path
        self._update_title()
        return False

    def _refresh_language(self):
        try:
            if not self.win.winfo_exists():
                return
            self._build_menu()
            self._update_title()
            self._update_count()
        except Exception:
            pass

    def _close(self):
        if self._closed:
            return
        if self._dirty:
            choice = messagebox.askyesnocancel(
                t("text_editor_close_title"),
                t("text_editor_close_msg"),
                parent=self.win,
            )
            if choice is None:
                return
            if choice and not self._save():
                return

        self._closed = True
        try:
            unregister_lang_listener(self._lang_listener)
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass
        finally:
            if self._on_closed is not None:
                self._on_closed()


# ============================================================
#              KeyLog Viewer（Tk 只读窗口）
# ============================================================
class KeyLogViewer:
    def __init__(self, root, store):
        self.store = store
        self._app = None            # 可选：既存在就用于开关切换后重创 watcher
        # 分段加载状态
        self._recs = []            # 仅保留已经分页读取并显示的记录
        self._before_offset = None # 下一页从该 JSONL 字节偏移之前读取
        self._has_older = False
        self._loading = False
        self._sb = None
        self.win = tk.Toplevel(root)
        self.win.title(t("win_title"))
        self.win.geometry("900x600")
        self.win.attributes("-topmost", True)

        # 两行工具栏：第一行➔标题 + 计数文本，第二行➔功能按钮
        top = tk.Frame(self.win, bg="#1e1e1e")
        top.pack(fill="x")

        # ---- 第一行：标题 + 计数 ----
        row2 = tk.Frame(top, bg="#1e1e1e")
        row2.pack(fill="x")

        self._header_label = tk.Label(
            row2, text=t("header_title"),
            fg="#4a90e2", bg="#1e1e1e",
            font=("Microsoft YaHei", 11, "bold"), padx=10, pady=4,
        )
        self._header_label.pack(side="left")

        self.count_label = tk.Label(
            row2, text="", fg="#aaaaaa", bg="#1e1e1e",
            font=("Consolas", 10), padx=10, pady=4,
        )
        self.count_label.pack(side="left")

        # ---- 第二行：功能按钮 ----
        row1 = tk.Frame(top, bg="#1e1e1e")
        row1.pack(fill="x")

        self._btn_reload = tk.Button(
            row1, text=t("btn_reload"), command=self._reload,
            bg="#2b2b2b", fg="white", relief="flat",
            font=("Microsoft YaHei", 9), padx=10,
        )
        self._btn_reload.pack(side="left", padx=(8, 4), pady=4)

        self._btn_copy = tk.Button(
            row1, text=t("btn_copy_all"), command=self._copy_all,
            bg="#2b2b2b", fg="white", relief="flat",
            font=("Microsoft YaHei", 9), padx=10,
        )
        self._btn_copy.pack(side="left", padx=4, pady=4)

        self._btn_clear = tk.Button(
            row1, text=t("btn_clear"), command=self._clear_all,
            bg="#5a2323", fg="#ffdcdc", relief="flat",
            font=("Microsoft YaHei", 9, "bold"), padx=10,
        )
        self._btn_clear.pack(side="left", padx=4, pady=4)

        # 开机自启：两个复选框（普通 / 管理员）
        self._autostart_user_var = tk.IntVar(value=1 if autostart_exists(privileged=False) else 0)
        self._autostart_user_cb = tk.Checkbutton(
            row1, text=t("cb_autostart_user"),
            variable=self._autostart_user_var,
            command=self._on_autostart_user_toggle,
            bg="#1e1e1e", fg="#e6e6e6",
            activebackground="#1e1e1e", activeforeground="white",
            selectcolor="#1e1e1e", relief="flat", bd=0,
            font=("Microsoft YaHei", 9), padx=8, pady=2,
            highlightthickness=0,
        )
        self._autostart_user_cb.pack(side="left", padx=(10, 4), pady=4)

        self._autostart_var = tk.IntVar(value=1 if autostart_exists(privileged=True) else 0)
        self._autostart_cb = tk.Checkbutton(
            row1, text=t("cb_autostart"),
            variable=self._autostart_var,
            command=self._on_autostart_toggle,
            bg="#1e1e1e", fg="#e6e6e6",
            activebackground="#1e1e1e", activeforeground="white",
            selectcolor="#1e1e1e", relief="flat", bd=0,
            font=("Microsoft YaHei", 9), padx=8, pady=2,
            highlightthickness=0,
        )
        self._autostart_cb.pack(side="left", padx=(10, 4), pady=4)

        # 保留天数下拉，同一行右侧
        self._retention_var = tk.StringVar(value=f"{self.store.retention_days} {t('days_suffix')}")
        self._retention_menu = tk.OptionMenu(
            row1, self._retention_var,
            *[f"{d} {t('days_suffix')}" for d in KEYLOG_RETENTION_CHOICES],
            command=self._on_retention_changed,
        )
        self._retention_menu.configure(
            bg="#2b2b2b", fg="white", activebackground="#3a3a3a",
            activeforeground="white", relief="flat", highlightthickness=0,
            font=("Microsoft YaHei", 9), padx=6,
        )
        try:
            self._retention_menu["menu"].configure(
                bg="#2b2b2b", fg="white", activebackground="#4a90e2",
                activeforeground="white",
            )
        except Exception:
            pass
        self._retention_menu.pack(side="right", padx=(2, 8), pady=4)
        self._retention_label = tk.Label(
            row1, text=t("label_retention"), fg="#aaaaaa", bg="#1e1e1e",
            font=("Microsoft YaHei", 9),
        )
        self._retention_label.pack(side="right", padx=(6, 0))

        # ---- 第三行：隐私开关（默认全关）----
        row3 = tk.Frame(top, bg="#1e1e1e")
        row3.pack(fill="x")

        self._keylog_var = tk.IntVar(value=1 if self.store.keylog_enabled else 0)
        self._keylog_cb = tk.Checkbutton(
            row3, text=t("cb_keylog"),
            variable=self._keylog_var,
            command=self._on_keylog_toggle,
            bg="#1e1e1e", fg="#e6e6e6",
            activebackground="#1e1e1e", activeforeground="white",
            selectcolor="#1e1e1e", relief="flat", bd=0,
            font=("Microsoft YaHei", 9), padx=8, pady=2,
            highlightthickness=0,
        )
        self._keylog_cb.pack(side="left", padx=(10, 4), pady=4)

        self._clipboard_var = tk.IntVar(value=1 if self.store.clipboard_enabled else 0)
        self._clipboard_cb = tk.Checkbutton(
            row3, text=t("cb_clipboard"),
            variable=self._clipboard_var,
            command=self._on_clipboard_toggle,
            bg="#1e1e1e", fg="#e6e6e6",
            activebackground="#1e1e1e", activeforeground="white",
            selectcolor="#1e1e1e", relief="flat", bd=0,
            font=("Microsoft YaHei", 9), padx=8, pady=2,
            highlightthickness=0,
        )
        self._clipboard_cb.pack(side="left", padx=4, pady=4)

        self._uia_var = tk.IntVar(value=1 if self.store.uia_enabled else 0)
        self._uia_cb = tk.Checkbutton(
            row3, text=t("cb_uia"),
            variable=self._uia_var,
            command=self._on_uia_toggle,
            bg="#1e1e1e", fg="#e6e6e6",
            activebackground="#1e1e1e", activeforeground="white",
            selectcolor="#1e1e1e", relief="flat", bd=0,
            font=("Microsoft YaHei", 9), padx=8, pady=2,
            highlightthickness=0,
        )
        self._uia_cb.pack(side="left", padx=4, pady=4)
        # 若 comtypes/uiautomation 未安装，该开关自动置灰
        if not uia_available():
            try:
                self._uia_var.set(0)
                self.store.set_uia_enabled(False)
                self._uia_cb.config(state="disabled", fg="#666666")
            except Exception:
                pass

        self._privacy_hint = tk.Label(
            row3, text=t("privacy_hint"),
            fg="#888888", bg="#1e1e1e",
            font=("Microsoft YaHei", 8), padx=10,
        )
        self._privacy_hint.pack(side="left", padx=(6, 0))

        text_frame = tk.Frame(self.win, bg="#111111")
        text_frame.pack(fill="both", expand=True)

        self.text = tk.Text(
            text_frame, wrap="word",
            bg="#111111", fg="#e6e6e6",
            insertbackground="white",
            font=("Consolas", 11), bd=0, padx=10, pady=10,
        )
        sb = tk.Scrollbar(text_frame, command=self._on_sb_command)
        self._sb = sb
        # 只同步滑块位置，不在回调里触发加载（避免 yscrollcommand 自触发循环）
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        # 主动动作才拉新：滑轮 / PgUp / ↑ / Home / Ctrl+Home / 拖滑块
        self.text.bind("<MouseWheel>", self._on_mouse_wheel, add="+")
        self.text.bind("<Button-4>", lambda e: self._maybe_load_older_from_action(), add="+")
        for k in ("<Prior>", "<Up>", "<Home>", "<Control-Home>", "<KeyPress-Up>", "<KeyPress-Prior>"):
            self.text.bind(k, self._on_key_scroll_up, add="+")

        self.text.tag_configure("clip", foreground="#ffd93d")
        # 特殊键标签（[*Tab] [*Shift+X] [*↑] [*Ctrl+C] 等，含后接 *N）：置灰，复制时剪除
        self.text.tag_configure("special", foreground="#555555")
        # 复制拦截：拖选任意区域，但 Ctrl+C / 右键复制 时剥除 special 部分
        self.text.bind("<Control-c>", self._on_copy)
        self.text.bind("<Control-C>", self._on_copy)
        self.text.bind("<Control-Insert>", self._on_copy)
        # <<Copy>> 事件可能由右键菜单触发
        self.text.bind("<<Copy>>", self._on_copy)
        self.text.bind("<Escape>", lambda e: self._close())
        self.win.bind("<Escape>", lambda e: self._close())
        self.win.protocol("WM_DELETE_WINDOW", self._close)

        self._reload()

        # 语言切换监听：重上标题/按钮/下拉/计数
        self._lang_listener = self._refresh_lang
        register_lang_listener(self._lang_listener)

        # 抢焦点
        try:
            self.win.after(30, self.win.focus_force)
        except Exception:
            pass

    # ---------- 分段加载核心 ----------
    def _apply_tags_range(self, start_index, end_index):
        """给 Text 里 [start_index, end_index) 区间打 clip / special 高亮标签。"""
        body = self.text.get(start_index, end_index)
        if not body:
            return
        clip_ranges = []
        for mm in re.finditer(
            r"───\[(?:剪贴板|Clipboard) [^\]]+\]───\n.*?\n───\[/(?:剪贴板|Clipboard)\]───",
            body, flags=re.DOTALL,
        ):
            s = f"{start_index}+{mm.start()}c"
            e = f"{start_index}+{mm.end()}c"
            self.text.tag_add("clip", s, e)
            clip_ranges.append((mm.start(), mm.end()))

        def _in_clip(pos):
            for s, e in clip_ranges:
                if s <= pos < e:
                    return True
            return False

        for mm in re.finditer(r"\[\*[^\]\n]+\](?:\*\d+)?", body):
            if _in_clip(mm.start()):
                continue
            s = f"{start_index}+{mm.start()}c"
            e = f"{start_index}+{mm.end()}c"
            self.text.tag_add("special", s, e)

    def _update_count_label(self):
        loaded_recs = len(self._recs)
        # 已加载字符数
        try:
            loaded_chars = int(self.text.count("1.0", "end-1c", "chars")[0])
        except Exception:
            loaded_chars = 0
        if not self._has_older:
            self.count_label.config(
                text=t("count_full", total=loaded_recs, chars=loaded_chars),
                fg="#aaaaaa",
            )
        else:
            self.count_label.config(
                text=t("count_paged", loaded=loaded_recs, chars=loaded_chars),
                fg="#aaaaaa",
            )

    def _reload(self):
        """全量重建（刷新按钮 / 清空 / 切换保留天数时调用）。
        recs 从磁盘慢（可能十上 MB），放后台线程读，完了回主线程插 Text。
        只装尾部 KEYLOG_INITIAL_CHARS 字符。"""
        # 先拉一个加载代 token，干掉旧回调（避免快速连点刷新/切天数时旧线程追到新 UI）
        gen = getattr(self, "_reload_gen", 0) + 1
        self._reload_gen = gen
        # 上锁、清空 Text，先在 count_label 上提示加载中
        self._loading = True
        try:
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
        except Exception:
            pass
        try:
            self.count_label.config(text=t("loading"))
        except Exception:
            pass

        def _worker():
            try:
                recs, before, has_older = self.store.load_page_before(
                    end_offset=None,
                    target_chars=KEYLOG_INITIAL_CHARS,
                )
            except Exception:
                recs, before, has_older = [], 0, False
            def _apply():
                # 旧代回调：已被新 _reload 覆盖
                if getattr(self, "_reload_gen", 0) != gen:
                    return
                try:
                    if not self.win.winfo_exists():
                        return
                except Exception:
                    return
                self._recs = recs
                self._before_offset = before
                self._has_older = has_older
                try:
                    segment = KeyLogStore.format_records(recs)
                    self.text.configure(state="normal")
                    self.text.insert("end", segment)
                    self._apply_tags_range("1.0", "end-1c")
                finally:
                    self._loading = False

                def _pin_bottom():
                    try:
                        if not self.win.winfo_exists():
                            return
                        self.text.yview_moveto(1.0)
                        self.text.see("end")
                    except Exception:
                        pass
                try:
                    self.win.update_idletasks()
                    _pin_bottom()
                    self.win.after(30, _pin_bottom)
                except Exception:
                    pass
                self._update_count_label()

            try:
                self.win.after(0, _apply)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _on_autostart_toggle(self):
        """开机自启(管理员)：与普通档互斥。"""
        if self._autostart_var.get() == 1:
            # 先卸载普通档（若存在）
            if autostart_exists(privileged=False):
                autostart_remove(privileged=False)
            try:
                self._autostart_user_var.set(0)
            except Exception:
                pass
            # 已经是管理员 + 任务已存在：什么都不用做
            if is_admin() and autostart_exists(privileged=True):
                return
            # 已经是管理员 但任务不在 → 直接装不需重启
            if is_admin():
                ok = autostart_install(privileged=True)
                if not ok:
                    self._autostart_var.set(0)
                    self._toast_err(t("toast_autostart_install_fail"))
                return
            # 非管理员 → 弹确认框
            self._show_autostart_confirm()
        else:
            # 取消勾选 → 删除任务
            if autostart_exists(privileged=True):
                ok = autostart_remove(privileged=True)
                if not ok:
                    # 删除失败可能是无权限 → 保持勾选，提示用户
                    self._autostart_var.set(1)
                    self._toast_err(t("toast_autostart_remove_fail"))

    def _on_autostart_user_toggle(self):
        """开机自启(普通)：与管理员档互斥。"""
        if self._autostart_user_var.get() == 1:
            # 先卸载管理员档（若存在）
            if autostart_exists(privileged=True):
                ok_rm = autostart_remove(privileged=True)
                if not ok_rm:
                    # 无权限删除管理员档（当前是普通用户） → 保持原状态
                    self._autostart_user_var.set(0)
                    self._toast_err(t("toast_autostart_remove_fail"))
                    return
            try:
                self._autostart_var.set(0)
            except Exception:
                pass
            if autostart_exists(privileged=False):
                return
            ok = autostart_install(privileged=False)
            if not ok:
                self._autostart_user_var.set(0)
                self._toast_err(t("toast_autostart_install_fail"))
        else:
            if autostart_exists(privileged=False):
                ok = autostart_remove(privileged=False)
                if not ok:
                    self._autostart_user_var.set(1)
                    self._toast_err(t("toast_autostart_remove_fail"))

    def _show_autostart_confirm(self):
        """弹层确认框：以管理员重启以安装开机自启。"""
        dlg = tk.Toplevel(self.win)
        dlg.title(t("dlg_autostart_title"))
        dlg.configure(bg="#1e1e1e")
        dlg.attributes("-topmost", True)
        dlg.transient(self.win)
        try:
            dlg.grab_set()
        except Exception:
            pass
        # 尺寸 & 居中
        w, h = 460, 200
        try:
            self.win.update_idletasks()
            px = self.win.winfo_rootx()
            py = self.win.winfo_rooty()
            pw = self.win.winfo_width()
            ph = self.win.winfo_height()
            x = px + (pw - w) // 2
            y = py + (ph - h) // 2
            dlg.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            dlg.geometry(f"{w}x{h}")

        tk.Label(
            dlg,
            text=t("dlg_autostart_msg"),
            fg="#e6e6e6", bg="#1e1e1e",
            font=("Microsoft YaHei", 10), justify="left",
            padx=20, pady=20,
        ).pack(fill="x", expand=False)

        btn_frame = tk.Frame(dlg, bg="#1e1e1e")
        btn_frame.pack(fill="x", padx=20, pady=(0, 16))

        cancelled = {"v": True}  # 默认弹框关闭 = 取消

        def do_cancel():
            cancelled["v"] = True
            dlg.destroy()

        def do_confirm():
            cancelled["v"] = False
            dlg.destroy()

        tk.Button(
            btn_frame, text=t("btn_cancel"), command=do_cancel,
            bg="#2b2b2b", fg="white", relief="flat",
            font=("Microsoft YaHei", 10), padx=16, pady=4,
        ).pack(side="right", padx=(6, 0))

        tk.Button(
            btn_frame, text=t("btn_confirm_admin"), command=do_confirm,
            bg="#2d6cdf", fg="white", relief="flat",
            font=("Microsoft YaHei", 10, "bold"), padx=16, pady=4,
        ).pack(side="right")

        dlg.protocol("WM_DELETE_WINDOW", do_cancel)
        dlg.bind("<Escape>", lambda e: do_cancel())

        # 阻塞等弹框关闭
        self.win.wait_window(dlg)

        if cancelled["v"]:
            # 取消 / 关闭 → 取消勾选，当前面板可正常关闭
            self._autostart_var.set(0)
            return

        # 确定 → 启动管理员重启；新进程带 --set-autostart 完成安装
        ok = relaunch_as_admin(extra_args=["--set-autostart"])
        if ok:
            # UAC 已拉起新进程，退出当前普通权限实例
            try:
                self.win.destroy()
            except Exception:
                pass
            os._exit(0)
        else:
            # 用户在 UAC 上点了“否” / 失败
            self._autostart_var.set(0)
            self._toast_err(t("toast_uac_fail"))

    def _toast_err(self, msg):
        try:
            self.count_label.config(text=msg, fg="#ff8888")
            self.win.after(2500, lambda: self._update_count_label())
        except Exception:
            pass

    def _refresh_lang(self):
        try:
            if not self.win.winfo_exists():
                return
        except Exception:
            return
        try:
            self.win.title(t("win_title"))
            self._header_label.config(text=t("header_title"))
            self._btn_reload.config(text=t("btn_reload"))
            self._btn_copy.config(text=t("btn_copy_all"))
            self._btn_clear.config(text=t("btn_clear"))
            self._autostart_cb.config(text=t("cb_autostart"))
            self._autostart_user_cb.config(text=t("cb_autostart_user"))
            self._keylog_cb.config(text=t("cb_keylog"))
            self._clipboard_cb.config(text=t("cb_clipboard"))
            self._uia_cb.config(text=t("cb_uia"))
            self._privacy_hint.config(text=t("privacy_hint"))
            self._retention_label.config(text=t("label_retention"))
            self._retention_var.set(f"{self.store.retention_days} {t('days_suffix')}")
            menu = self._retention_menu["menu"]
            menu.delete(0, "end")
            for d in KEYLOG_RETENTION_CHOICES:
                label = f"{d} {t('days_suffix')}"
                menu.add_command(
                    label=label,
                    command=lambda v=label: (self._retention_var.set(v), self._on_retention_changed(v)),
                )
            self._update_count_label()
            # 重新渲染 Text：里面的“剪贴板”/日期格式依赖 LANG
            try:
                self._reload()
            except Exception:
                pass
        except Exception:
            pass

    def _close(self):
        """关闭面板：先自增 generation 干掉任何后台回调，再 destroy。"""
        try:
            unregister_lang_listener(self._lang_listener)
        except Exception:
            pass
        try:
            self._reload_gen = getattr(self, "_reload_gen", 0) + 1
        except Exception:
            pass
        self._loading = True
        try:
            self.win.destroy()
        except Exception:
            pass

    # ---------- 主动滚动动作 → 拉新 ----------
    def _on_sb_command(self, *args):
        """代理滑块拖拽/箭头点击：先执行实际滚动，再判断方向。"""
        try:
            cur_first = float(self.text.yview()[0])
        except Exception:
            cur_first = 1.0
        self.text.yview(*args)
        going_up = False
        try:
            if args and args[0] == "scroll":
                # ("scroll", n, "units"|"pages")
                n = int(float(args[1]))
                if n < 0:
                    going_up = True
            elif args and args[0] == "moveto":
                target = float(args[1])
                if target < cur_first:
                    going_up = True
        except Exception:
            pass
        if going_up:
            self._maybe_load_older_from_action()

    def _on_mouse_wheel(self, event):
        # Windows: delta 为 120 的倍数，正 = 上翻
        try:
            up = event.delta > 0
        except Exception:
            up = False
        if up:
            self._maybe_load_older_from_action()
        # 不拦截，Tk 默认滑动行为照走

    def _on_key_scroll_up(self, event):
        self._maybe_load_older_from_action()
        # 不 return "break"

    def _maybe_load_older_from_action(self):
        """用户主动向上滚动 → 视口已在顶部且还有更早内容 → 拉一段。
        关键：只在人滑时才运行，yview/see 引起的位置变化不会自己触发自己。"""
        if self._loading or not self._has_older:
            return
        try:
            first = float(self.text.yview()[0])
        except Exception:
            first = 1.0
        if first > 0.02:
            return
        # 使用 after_idle 而不是直接调，避免在 Tk 输入事件处理中途修改内容
        self.win.after_idle(self._load_older)

    def _release_loading(self):
        self._loading = False

    def _load_older(self, target_chars=KEYLOG_CHUNK_CHARS, initial=False):
        """Read and prepend one bounded page from disk."""
        if self._loading or not self._has_older:
            return
        self._loading = True
        end_offset = self._before_offset

        def _worker():
            recs, before, has_older = self.store.load_page_before(
                end_offset=end_offset,
                target_chars=target_chars,
            )

            def _apply():
                try:
                    if not self.win.winfo_exists():
                        return
                    segment = KeyLogStore.format_records(recs)
                    if segment:
                        anchor = "__olderload_anchor__"
                        self.text.mark_set(anchor, "@0,0")
                        self.text.insert("1.0", segment)
                        self._apply_tags_range("1.0", f"1.0+{len(segment)}c")
                        try:
                            self.text.yview(f"{anchor} linestart")
                        except Exception:
                            pass
                        self.text.mark_unset(anchor)
                        self._recs = recs + self._recs
                    self._before_offset = before
                    self._has_older = has_older
                    self._update_count_label()
                finally:
                    self.win.after(120, self._release_loading)

            try:
                self.win.after(0, _apply)
            except Exception:
                self._loading = False

        threading.Thread(target=_worker, daemon=True).start()

    def _selection_text_without_special(self):
        """返回选区文本，剪除所有 special tag 范围里的字符。"""
        try:
            sel_first = self.text.index("sel.first")
            sel_last = self.text.index("sel.last")
        except tk.TclError:
            return None
        raw = self.text.get(sel_first, sel_last)
        if not raw:
            return ""
        # 收集 special 与 sel 的交集，映射回 raw 里的偏移区间
        ranges = self.text.tag_ranges("special")
        if not ranges:
            return raw
        cuts = []  # (start_offset, end_offset) in raw
        # 累计相对偏移：用 count -chars 求两个 index 的字符距离
        def _rel(idx):
            try:
                n = self.text.count(sel_first, idx, "chars")
                if n is None:
                    return 0
                if isinstance(n, tuple):
                    n = n[0]
                return int(n)
            except Exception:
                return 0
        raw_len = len(raw)
        for i in range(0, len(ranges), 2):
            r_start = str(ranges[i])
            r_end = str(ranges[i + 1])
            if self.text.compare(r_end, "<=", sel_first):
                continue
            if self.text.compare(r_start, ">=", sel_last):
                continue
            clip_s = r_start if self.text.compare(r_start, ">", sel_first) else sel_first
            clip_e = r_end if self.text.compare(r_end, "<", sel_last) else sel_last
            s_off = max(0, min(raw_len, _rel(clip_s)))
            e_off = max(0, min(raw_len, _rel(clip_e)))
            if e_off > s_off:
                cuts.append((s_off, e_off))
        if not cuts:
            return raw
        # 合并区间并剪除
        cuts.sort()
        merged = [cuts[0]]
        for s, e in cuts[1:]:
            if s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        out = []
        prev = 0
        for s, e in merged:
            if s > prev:
                out.append(raw[prev:s])
            prev = e
        if prev < raw_len:
            out.append(raw[prev:])
        return "".join(out)

    def _on_copy(self, event=None):
        text = self._selection_text_without_special()
        if text is None:
            return  # 无选区，走默认行为
        try:
            suppress_clipboard(text)
            self.win.clipboard_clear()
            self.win.clipboard_append(text)
        except Exception:
            pass
        return "break"  # 拦截默认复制行为，避免把带 special 的原文写回

    def _copy_all(self):
        """Stream all retained records into a bounded clipboard payload."""
        try:
            body = self.store.build_copy_text(KEYLOG_COPY_MAX_CHARS)
            suppress_clipboard(body)
            self.win.clipboard_clear()
            self.win.clipboard_append(body)
        except ValueError:
            self._toast_err(t("copy_too_large", limit=KEYLOG_COPY_MAX_CHARS))
        except Exception:
            pass

    def _clear_all(self):
        import tkinter.messagebox as mb
        ok = mb.askyesno(
            t("dlg_clear_title"),
            t("dlg_clear_msg"),
            parent=self.win,
        )
        if not ok:
            return
        self.store.clear_all()
        self._reload()

    def _on_retention_changed(self, val):
        try:
            days = int(str(val).split()[0])
        except Exception:
            return
        if days not in KEYLOG_RETENTION_CHOICES:
            return
        self.store.set_retention_days(days)
        self._reload()

    # ---------- 隐私开关 ----------
    def _on_keylog_toggle(self):
        on = bool(self._keylog_var.get())
        self.store.set_keylog_enabled(on)
        self._notify_privacy_change()

    def _on_clipboard_toggle(self):
        on = bool(self._clipboard_var.get())
        self.store.set_clipboard_enabled(on)
        self._notify_privacy_change()

    def _on_uia_toggle(self):
        on = bool(self._uia_var.get())
        self.store.set_uia_enabled(on)
        self._notify_privacy_change()

    def _notify_privacy_change(self):
        # 向 App 回报，令托盘译 tooltip / 图标。App 不存在时静默。
        try:
            app = getattr(self, "_app", None)
            if app is not None and hasattr(app, "_on_privacy_changed"):
                app._on_privacy_changed()
        except Exception:
            pass


# ============================================================
#              托盘图标
# ============================================================
def _build_tray_icon_image(size=64):
    """按目标像素尺寸直接绘制一张只有横线的空白练习纸。"""
    size = max(8, int(size))
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    margin_x = max(2, round(size * 0.16))
    margin_y = max(1, round(size * 0.06))
    left, top = margin_x, margin_y
    right, bottom = size - margin_x - 1, size - margin_y - 1
    fold = max(2, round(size * 0.19))
    stroke = 1 if size <= 24 else max(1, round(size / 28))

    paper = [
        (left, top),
        (right - fold, top),
        (right, top + fold),
        (right, bottom),
        (left, bottom),
    ]
    draw.polygon(
        paper,
        fill=(250, 250, 247, 255),
        outline=(72, 93, 112, 255),
        width=stroke,
    )
    # 右上折角只用纸张明暗表示，不引入文字或额外符号。
    draw.polygon(
        [(right - fold, top), (right - fold, top + fold), (right, top + fold)],
        fill=(208, 222, 232, 255),
        outline=(72, 93, 112, 255),
    )

    line_left = left + max(2, round(size * 0.12))
    line_right = right - max(1, round(size * 0.08))
    line_top = top + fold + max(1, round(size * 0.06))
    line_bottom = bottom - max(2, round(size * 0.12))
    line_count = 3 if size <= 20 else 4
    if line_count > 1:
        positions = [
            round(line_top + i * (line_bottom - line_top) / (line_count - 1))
            for i in range(line_count)
        ]
        for y in sorted(set(positions)):
            draw.line(
                [(line_left, y), (line_right, y)],
                fill=(75, 145, 194, 255),
                width=stroke,
            )
    return img


# ============================================================
#         纯 ctypes 系统托盘（零第三方依赖）
#         MIT 可用。基于 Win32 Shell_NotifyIconW + 自建隐藏窗口。
# ============================================================

# Win32 常量
_WM_USER = 0x0400
_WM_TRAYICON = _WM_USER + 1
_WM_DESTROY = 0x0002
_WM_COMMAND = 0x0111
_WM_LBUTTONUP = 0x0202
_WM_RBUTTONUP = 0x0205
_WM_LBUTTONDBLCLK = 0x0203

_NIM_ADD = 0x00000000
_NIM_MODIFY = 0x00000001
_NIM_DELETE = 0x00000002

_NIF_MESSAGE = 0x00000001
_NIF_ICON = 0x00000002
_NIF_TIP = 0x00000004

_TPM_LEFTALIGN = 0x0000
_TPM_RIGHTBUTTON = 0x0002
_TPM_RETURNCMD = 0x0100
_TPM_NONOTIFY = 0x0080

_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x00000010

_SM_CXSMICON = 49
_SM_CYSMICON = 50

_MF_STRING = 0x00000000
_MF_SEPARATOR = 0x00000800
_MF_DEFAULT = 0x00001000

# NOTIFYICONDATA 结构（GUID/State 等高级字段不用）
class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uTimeoutOrVersion", wt.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wt.DWORD),
    ]


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM
        )),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wt.HWND),
        ("message", wt.UINT),
        ("wParam", wt.WPARAM),
        ("lParam", wt.LPARAM),
        ("time", wt.DWORD),
        ("pt", _POINT),
    ]


_user32 = ctypes.windll.user32
_shell32 = ctypes.windll.shell32
_gdi32 = ctypes.windll.gdi32
_kernel32 = ctypes.windll.kernel32

# 函数签名
WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM
)
_user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_user32.DefWindowProcW.restype = ctypes.c_ssize_t
_user32.CreateWindowExW.restype = wt.HWND
_user32.CreatePopupMenu.restype = wt.HMENU
_user32.CreatePopupMenu.argtypes = []
# AppendMenuW: HMENU, UINT uFlags, UINT_PTR uIDNewItem (可能是命令id 或 子菜单的 HMENU指针), LPCWSTR
# 不声明 argtypes 时会把 64-bit HMENU 当作 c_int 传，导致 OverflowError。
_user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_void_p, wt.LPCWSTR]
_user32.AppendMenuW.restype = wt.BOOL
_user32.TrackPopupMenu.argtypes = [
    wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, wt.HWND, ctypes.c_void_p,
]
_user32.TrackPopupMenu.restype = ctypes.c_int
_user32.DestroyMenu.argtypes = [wt.HMENU]
_user32.DestroyMenu.restype = wt.BOOL
_user32.SetForegroundWindow.argtypes = [wt.HWND]
_user32.SetForegroundWindow.restype = wt.BOOL
_user32.LoadImageW.argtypes = [
    wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT,
]
_user32.LoadImageW.restype = wt.HANDLE
_user32.DestroyIcon.argtypes = [wt.HICON]
_user32.DestroyIcon.restype = wt.BOOL
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetSystemMetrics.restype = ctypes.c_int
if hasattr(_user32, "GetDpiForWindow"):
    _user32.GetDpiForWindow.argtypes = [wt.HWND]
    _user32.GetDpiForWindow.restype = wt.UINT
if hasattr(_user32, "GetSystemMetricsForDpi"):
    _user32.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, wt.UINT]
    _user32.GetSystemMetricsForDpi.restype = ctypes.c_int
_shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]
_shell32.Shell_NotifyIconW.restype = wt.BOOL


def _get_tray_icon_size(hwnd=None):
    """返回当前托盘窗口 DPI 对应的小图标像素尺寸。"""
    dpi = 96
    try:
        if hwnd and hasattr(_user32, "GetDpiForWindow"):
            dpi = int(_user32.GetDpiForWindow(hwnd)) or 96
    except Exception:
        dpi = 96

    try:
        if hasattr(_user32, "GetSystemMetricsForDpi"):
            width = _user32.GetSystemMetricsForDpi(_SM_CXSMICON, dpi)
            height = _user32.GetSystemMetricsForDpi(_SM_CYSMICON, dpi)
        else:
            width = _user32.GetSystemMetrics(_SM_CXSMICON)
            height = _user32.GetSystemMetrics(_SM_CYSMICON)
    except Exception:
        width = height = 16
    return max(1, int(width or 16)), max(1, int(height or 16))


def _pil_to_hicon(pil_img, width, height):
    """把各目标尺寸独立绘制为 .ico 并用 LoadImageW 载入。
    注意：**保留 .ico 文件到 stop 时才删**，因为 Windows shell 在
    重绘 tooltip / 切换 DPI 时可能会根据 HICON 重新读取图标数据。"""
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".ico", prefix="screen_search_tray_")
    os.close(fd)
    try:
        # 每一帧都按原生像素独立绘制，禁止 Pillow 从 64x64 自动缩小。
        icon_sizes = (16, 20, 24, 32, 40, 48, 64)
        frames = [_build_tray_icon_image(icon_size) for icon_size in icon_sizes]
        frames[-1].save(
            path,
            format="ICO",
            sizes=[(icon_size, icon_size) for icon_size in icon_sizes],
            append_images=frames[:-1],
        )
        # 必须明确请求 DPI 对应尺寸；0, 0 会固定选中多尺寸 ICO 的第一帧（16x16）。
        hicon = _user32.LoadImageW(
            None, path, _IMAGE_ICON, width, height,
            _LR_LOADFROMFILE,
        )
        if not hicon:
            try:
                os.remove(path)
            except Exception:
                pass
            return None, None
        return hicon, path
    except Exception:
        try:
            os.remove(path)
        except Exception:
            pass
        return None, None


class NativeTray:
    """纯 ctypes 系统托盘图标。使用 GetMessage 阻塞循环，适合在后台线程跑。"""

    def __init__(self, tooltip, on_left_click, on_menu_items, on_quit):
        """
        tooltip: str
        on_left_click: () -> None  左键单击
        on_menu_items: [item, ...]，item = 
            (None, None)                           # 分隔线
            (text, callback)                       # 普通项
            (text, [sub_item, ...])                # 子菜单，递归相同结构
            (text, callback, {"checked": bool})    # 带 ✓ 或 • 标记的项
        on_quit: () -> None
        """
        self.tooltip = tooltip
        self.on_left_click = on_left_click
        self._menu_spec = on_menu_items  # 保留原始 spec，每次 弹菜单时重新展开
        self._quit_cb = on_quit
        self._QUIT_ID = 999
        # 运行时 cmd_id → callback 映射，弹菜单时重建
        self._cmd_map = {}

        self.hwnd = None
        self.hicon = None
        self._icon_path = None
        self._nid = None
        self._class_name = f"NativeTray_{id(self)}"
        # 一定要用 self 保持 WNDPROC 不被 GC
        self._wndproc = WNDPROC(self._wnd_proc)
        self._running = False

    def set_menu(self, on_menu_items):
        """运行时更新菜单结构（例如语言切换后）。"""
        self._menu_spec = on_menu_items

    def set_tooltip(self, tooltip):
        self.tooltip = tooltip or ""
        if self._nid is not None:
            try:
                self._nid.szTip = self.tooltip[:127]
                # 只提交 tooltip，避免 Explorer 每次状态更新都重新栅格化图标。
                update = _NOTIFYICONDATAW()
                update.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
                update.hWnd = self._nid.hWnd
                update.uID = self._nid.uID
                update.uFlags = _NIF_TIP
                update.szTip = self.tooltip[:127]
                _shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(update))
            except Exception:
                pass

    def _register_class(self):
        wc = _WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = self._wndproc
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = _kernel32.GetModuleHandleW(None)
        wc.hIcon = None
        wc.hCursor = None
        wc.hbrBackground = None
        wc.lpszMenuName = None
        wc.lpszClassName = self._class_name
        atom = _user32.RegisterClassW(ctypes.byref(wc))
        # explorer 重启时会广播 TaskbarCreated 消息，收到后重新注册图标
        try:
            self._TaskbarCreated = _user32.RegisterWindowMessageW("TaskbarCreated")
        except Exception:
            self._TaskbarCreated = 0
        return atom

    def _create_hidden_window(self):
        # 使用普通顶层窗口（不显示）而不是 HWND_MESSAGE：
        # HWND_MESSAGE 在部分 Windows shell 上会被认为不合法的托盘图标宿主，
        # explorer 定期清理后导致图标悬停后消失。
        WS_OVERLAPPED = 0x00000000
        self.hwnd = _user32.CreateWindowExW(
            0,
            self._class_name,
            "NativeTrayHidden",
            WS_OVERLAPPED,
            0, 0, 0, 0,
            None, None,
            _kernel32.GetModuleHandleW(None),
            None,
        )
        # 不显示窗口，它只当消息阵的靠山
        try:
            _user32.ShowWindow(self.hwnd, 0)  # SW_HIDE
        except Exception:
            pass
        return self.hwnd

    def _add_icon(self, pil_image):
        icon_width, icon_height = _get_tray_icon_size(self.hwnd)
        self.hicon, self._icon_path = _pil_to_hicon(
            pil_image, icon_width, icon_height
        )
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
        nid.uCallbackMessage = _WM_TRAYICON
        nid.hIcon = self.hicon or 0
        # szTip 限 127 字符（含结尾符）
        nid.szTip = (self.tooltip or "")[:127]
        # 持有引用防 GC
        self._nid = nid
        ok = _shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(nid))
        return bool(ok)

    def _remove_icon(self):
        if self._nid is not None:
            try:
                _shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(self._nid))
            except Exception:
                pass
            self._nid = None
        if self.hicon:
            try:
                _user32.DestroyIcon(self.hicon)
            except Exception:
                pass
            self.hicon = None
        if self._icon_path and os.path.exists(self._icon_path):
            try:
                os.remove(self._icon_path)
            except Exception:
                pass
            self._icon_path = None

    def _build_menu(self, spec, cid_counter):
        """递归建 HMENU；返回 hmenu。cid_counter 是 [next_id] 列表包装（供递归字自增）。"""
        hmenu = _user32.CreatePopupMenu()
        for entry in spec:
            if entry[0] is None:
                _user32.AppendMenuW(hmenu, _MF_SEPARATOR, 0, None)
                continue
            text, second = entry[0], entry[1]
            flags = _MF_STRING
            checked = False
            if len(entry) >= 3 and isinstance(entry[2], dict):
                checked = bool(entry[2].get("checked", False))
            if isinstance(second, list):
                # 子菜单：递归先建
                sub = self._build_menu(second, cid_counter)
                MF_POPUP = 0x00000010
                if checked:
                    text = "\u2713 " + text
                _user32.AppendMenuW(hmenu, flags | MF_POPUP, sub, text)
            else:
                cb = second
                cmd_id = cid_counter[0]
                cid_counter[0] += 1
                self._cmd_map[cmd_id] = cb
                if checked:
                    text = "\u2022 " + text
                _user32.AppendMenuW(hmenu, flags, cmd_id, text)
        return hmenu

    def _show_menu(self):
        # 重建 cmd_map，避免跨次注入旧表
        self._cmd_map = {}
        cid_counter = [100]
        hmenu = self._build_menu(self._menu_spec, cid_counter)
        # 分隔符 + 退出
        _user32.AppendMenuW(hmenu, _MF_SEPARATOR, 0, None)
        _user32.AppendMenuW(hmenu, _MF_STRING, self._QUIT_ID, t("tray_quit"))

        # 获取鼠标位置
        pt = _POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        # Windows 要求 tray menu 前先 SetForegroundWindow
        _user32.SetForegroundWindow(self.hwnd)
        cmd = _user32.TrackPopupMenu(
            hmenu,
            _TPM_LEFTALIGN | _TPM_RIGHTBUTTON | _TPM_RETURNCMD | _TPM_NONOTIFY,
            pt.x, pt.y, 0, self.hwnd, None,
        )
        _user32.DestroyMenu(hmenu)
        if cmd == self._QUIT_ID:
            try:
                if self._quit_cb:
                    self._quit_cb()
            except Exception:
                pass
            return
        if cmd > 0:
            cb = self._cmd_map.get(cmd)
            if cb:
                try:
                    cb()
                except Exception:
                    pass

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        # explorer.exe 重启后广播的自愈消息：重新注册托盘图标
        if getattr(self, "_TaskbarCreated", 0) and msg == self._TaskbarCreated:
            try:
                if self._nid is not None:
                    _shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(self._nid))
            except Exception:
                pass
            return 0
        if msg == _WM_TRAYICON:
            # lParam 低字节＝鼠标消息
            m = lparam & 0xFFFF
            if m == _WM_LBUTTONUP:
                try:
                    if self.on_left_click:
                        self.on_left_click()
                except Exception:
                    pass
            elif m == _WM_RBUTTONUP:
                self._show_menu()
            return 0
        if msg == _WM_DESTROY:
            _user32.PostQuitMessage(0)
            return 0
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # -------- 对外 API --------
    def run(self, pil_image):
        """阅 重。阻塞当前线程直到 stop。应在后台线程调用。"""
        self._register_class()
        self._create_hidden_window()
        self._add_icon(pil_image)
        self._running = True
        msg = _MSG()
        while self._running:
            r = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r == 0 or r == -1:
                break
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))
        self._remove_icon()
        try:
            _user32.DestroyWindow(self.hwnd)
        except Exception:
            pass
        try:
            _user32.UnregisterClassW(self._class_name, _kernel32.GetModuleHandleW(None))
        except Exception:
            pass

    def stop(self):
        """从外部线程安全调用：发 WM_QUIT 给托盘线程。"""
        self._running = False
        if self.hwnd:
            try:
                # 向托盘窗口发 WM_CLOSE / WM_DESTROY 让 GetMessage 退出
                _user32.PostMessageW(self.hwnd, _WM_DESTROY, 0, 0)
            except Exception:
                pass


# ============================================================
#                 主应用
# ============================================================
class ScreenSearchApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.overlay = None
        self.event_queue = queue.Queue()
        self._busy = False
        self._splash = None
        self._ocr_thread = None
        self._ocr_result = None
        self._ocr_deadline = 0.0
        self._ocr_generation = 0        # 防竞态：每次启动拍 +1，旧线程回写前需校验
        self._ocr_lock = threading.Lock()  # 确保 RapidOCR 实例不被两个线程同时调用
        self._ocr_stuck_threads = {}    # engine name -> timed-out worker (bounded)
        self._ocr_active_engine = None
        self._tray_icon = None
        self._tray_thread = None
        self._quit_requested = False
        self._keylog_viewer = None
        self._text_editors = set()
        self._rapid_ocr = None            # RapidOCR 实例（启动时初始化并常驻）

        # 启动后台预热 RapidOCR（首次约 1-2s 加载，不阻主循环）
        if _RAPIDOCR_AVAILABLE:
            def _preload_rapid():
                try:
                    self._rapid_ocr = RapidOCR()
                except Exception as e:
                    print(f"[RapidOCR] init failed, fallback to winocr: {e}")
                    self._rapid_ocr = None
            threading.Thread(target=_preload_rapid, daemon=True).start()

        # 输入记录
        self.keylog_store = KeyLogStore()
        # 启动时清一次过期
        threading.Thread(target=self.keylog_store.cleanup, daemon=True).start()

        # 主快捷键 + 输入记录快捷键 + 新建文本快捷键
        keyboard.add_hotkey(HOTKEY, self._on_hotkey_ocr)
        keyboard.add_hotkey(KEYLOG_HOTKEY, self._on_hotkey_keylog)
        keyboard.add_hotkey(NEW_TEXT_HOTKEY, self._on_hotkey_new_text)
        keyboard.on_press_key(ESC_HOTKEY, self._on_esc, suppress=False)

        # 键盘钩子（记录输入）— 必须放在 add_hotkey 之后以避免冲突
        self.key_hooker = KeyHooker(self.keylog_store, self)
        self.key_hooker.start()

        # 剪贴板监听
        self.clip_watcher = ClipboardWatcher(self.keylog_store)
        global _CLIP_WATCHER_REF
        _CLIP_WATCHER_REF = self.clip_watcher
        self.clip_watcher.start()

        # UIA IME 中文捕获，传入 key_hooker 以便提交时清拼音缓冲
        self.uia_watcher = UIAWatcher(self.keylog_store, self.key_hooker)
        self.uia_watcher.start()

        self.root.after(50, self._pump_events)

        # 监听 overlay 发起的重新识别事件
        self.root.bind("<<ScreenSearchRescan>>", lambda e: self._on_rescan_request())

    def _privacy_status_text(self):
        parts = []
        if self.keylog_store.keylog_enabled:
            parts.append("K")
        if self.keylog_store.clipboard_enabled:
            parts.append("C")
        if self.keylog_store.uia_enabled:
            parts.append("IME")
        if not parts:
            return t("tray_status_off")
        return t("tray_status_on", parts="+".join(parts))

    def _compose_tray_tooltip(self):
        return t("tray_tooltip") + "\n" + self._privacy_status_text()

    def _on_privacy_changed(self):
        # 刷 tray tooltip（录制状态）
        try:
            if self._tray_icon is not None:
                self._tray_icon.set_tooltip(self._compose_tray_tooltip())
        except Exception:
            pass

    def _on_rescan_request(self):
        """overlay “重新识别”按钮触发→ 归到制作镜头，重新截屏 OCR。"""
        # overlay 已在 _on_rescan_click 里 close；这里确保旧引用释放
        try:
            if self.overlay is not None and not self.overlay.alive:
                self.overlay = None
        except Exception:
            self.overlay = None
        if self._busy:
            return
        self._start_capture()

    # ---------- 事件回调 ----------
    def _on_hotkey_ocr(self):
        self.event_queue.put("toggle_ocr")

    def _on_hotkey_keylog(self):
        self.event_queue.put("show_keylog")

    def _on_hotkey_new_text(self):
        self.event_queue.put("new_text")

    def _on_esc(self, event=None):
        if self.overlay is not None and self.overlay.alive:
            self.event_queue.put("close_overlay")

    def _on_tray_click(self, icon=None, item=None):
        self.event_queue.put("toggle_ocr")

    def _on_tray_keylog(self, icon=None, item=None):
        self.event_queue.put("show_keylog")

    def _on_tray_quit(self, icon=None, item=None):
        self.event_queue.put("quit")

    def _pump_events(self):
        try:
            while True:
                ev = self.event_queue.get_nowait()
                if ev == "toggle_ocr":
                    self._toggle_ocr()
                elif ev == "close_overlay":
                    if self.overlay is not None and self.overlay.alive:
                        self.overlay.close()
                        self.overlay = None
                elif ev == "show_keylog":
                    self._show_keylog()
                elif ev == "new_text":
                    self._open_new_text_document()
                elif ev == "quit":
                    self._shutdown()
                    return
        except queue.Empty:
            pass
        self.root.after(50, self._pump_events)

    def _open_new_text_document(self):
        """打开一个独立的内置文本编辑器窗口。"""
        editor = None

        def _on_closed():
            self._text_editors.discard(editor)

        editor = TextDocumentWindow(self.root, on_closed=_on_closed)
        self._text_editors.add(editor)

    def _show_keylog(self):
        # 若已存在则前置
        if self._keylog_viewer is not None:
            try:
                if self._keylog_viewer.win.winfo_exists():
                    self._keylog_viewer.win.lift()
                    self._keylog_viewer._reload()
                    try:
                        self._keylog_viewer.win.focus_force()
                    except Exception:
                        pass
                    return
            except Exception:
                pass
        self._keylog_viewer = KeyLogViewer(self.root, self.keylog_store)
        try:
            self._keylog_viewer._app = self
        except Exception:
            pass

    def _build_tray_menu_spec(self, switch_lang):
        return [
            (t("tray_ocr"), lambda: self.event_queue.put("toggle_ocr")),
            (t("tray_keylog"), lambda: self.event_queue.put("show_keylog")),
            (t("tray_new_text"), lambda: self.event_queue.put("new_text")),
            (None, None),
            (t("tray_language"), [
                (t("tray_lang_zh"), switch_lang("zh"), {"checked": LANG == "zh"}),
                (t("tray_lang_en"), switch_lang("en"), {"checked": LANG == "en"}),
            ]),
        ]

    def _start_tray(self):
        image = _build_tray_icon_image(64)

        def _switch_lang(new_lang):
            def _do():
                # 从托盘线程切回主线程再改 UI（Tk 不支持跨线程）
                try:
                    self.root.after(0, lambda: set_lang(new_lang))
                except Exception:
                    set_lang(new_lang)
            return _do

        def _build_menu_spec():
            return self._build_tray_menu_spec(_switch_lang)

        self._tray_icon = NativeTray(
            tooltip=self._compose_tray_tooltip(),
            on_left_click=lambda: self.event_queue.put("toggle_ocr"),
            on_menu_items=_build_menu_spec(),
            on_quit=lambda: self.event_queue.put("quit"),
        )

        # 语言切换时→重建菜单 & tooltip
        def _on_lang_change():
            try:
                if self._tray_icon is not None:
                    self._tray_icon.set_menu(_build_menu_spec())
                    self._tray_icon.set_tooltip(self._compose_tray_tooltip())
            except Exception:
                pass

        register_lang_listener(_on_lang_change)
        self._tray_lang_listener = _on_lang_change

        self._tray_thread = threading.Thread(
            target=self._tray_icon.run, args=(image,), daemon=True
        )
        self._tray_thread.start()

    def _shutdown(self):
        self._quit_requested = True
        try:
            if self.overlay is not None and self.overlay.alive:
                self.overlay.close()
        except Exception:
            pass
        try:
            if self._splash is not None:
                self._splash.destroy()
        except Exception:
            pass
        try:
            self.clip_watcher.stop()
        except Exception:
            pass
        try:
            self.uia_watcher.stop()
        except Exception:
            pass
        try:
            if self._tray_icon is not None:
                self._tray_icon.stop()
        except Exception:
            pass
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        try:
            self.root.quit()
        except Exception:
            pass

    # ---------- OCR 主流程（沿用原逻辑） ----------
    def _toggle_ocr(self):
        if self._busy:
            return
        if self.overlay is not None and self.overlay.alive:
            self.overlay.close()
            self.overlay = None
            return
        self._start_capture()

    def _start_capture(self):
        self._ocr_stuck_threads = {
            name: worker for name, worker in self._ocr_stuck_threads.items()
            if worker is not None and worker.is_alive()
        }
        rapid_ready = (
            getattr(self, "_rapid_ocr", None) is not None
            and "rapid" not in self._ocr_stuck_threads
        )
        active_engine = "rapid" if rapid_ready else "winocr"
        if active_engine in self._ocr_stuck_threads:
            self._toast(t("ocr_timeout", sec=OCR_TIMEOUT_SEC))
            return
        self._busy = True
        try:
            img = ImageGrab.grab(all_screens=True)
        except Exception as e:
            self._busy = False
            self._toast(t("ocr_screencap_fail", err=e))
            return

        user32 = ctypes.windll.user32
        vx = user32.GetSystemMetrics(76)
        vy = user32.GetSystemMetrics(77)
        vw = user32.GetSystemMetrics(78)
        vh = user32.GetSystemMetrics(79)

        self._splash = tk.Toplevel(self.root)
        self._splash.overrideredirect(True)
        self._splash.attributes("-topmost", True)
        self._splash.configure(bg="#222222")
        tk.Label(
            self._splash, text=t("ocr_loading"),
            fg="white", bg="#222222",
            font=("Microsoft YaHei", 14, "bold"),
            padx=20, pady=10,
        ).pack()
        self._splash.update_idletasks()
        sw = self._splash.winfo_screenwidth()
        sh = self._splash.winfo_screenheight()
        self._splash.geometry(f"+{sw//2-120}+{sh//2-40}")

        self._ocr_result = {}
        self._ocr_deadline = time.monotonic() + OCR_TIMEOUT_SEC
        # 防竞态：每次启动产生一个新 generation，旧线程回写前先比对
        self._ocr_generation += 1
        gen = self._ocr_generation
        result = self._ocr_result

        # 优先用 RapidOCR（PP-OCRv4），不可用时回退到 winocr
        engine = self._rapid_ocr if active_engine == "rapid" else None
        rgb_img = img.convert("RGB") if engine is not None else None
        self._ocr_active_engine = active_engine

        ocr_lock = self._ocr_lock

        def do_ocr():
            try:
                if engine is not None:
                    with ocr_lock:
                        # 二次确认 generation：拿到锁时已作废则直接退出
                        if gen != self._ocr_generation:
                            return
                        arr = np.array(rgb_img)  # allocate only after acquiring the engine lock
                        out, elapse = engine(arr)
                    if gen != self._ocr_generation:
                        return  # 结果已无主，丢弃
                    result["rapid"] = out
                else:
                    r = winocr.recognize_pil_sync(img, OCR_LANG)
                    if gen != self._ocr_generation:
                        return
                    result["result"] = r
            except Exception as e:
                if gen == self._ocr_generation:
                    result["error"] = e

        self._ocr_thread = threading.Thread(target=do_ocr, daemon=True)
        self._ocr_thread.start()

        self._ocr_ctx = {"vx": vx, "vy": vy, "vw": vw, "vh": vh, "img": img}
        self.root.after(30, self._poll_ocr)

    def _poll_ocr(self):
        if not self._busy:
            return
        if self._ocr_thread and self._ocr_thread.is_alive():
            if time.monotonic() > self._ocr_deadline:
                # 超时：提升 generation，旧线程无法再写回任何结果
                self._ocr_generation += 1
                if self._ocr_thread is not None and self._ocr_thread.is_alive():
                    self._ocr_stuck_threads[self._ocr_active_engine] = self._ocr_thread
                self._finish_capture(timeout=True)
                return
            self.root.after(30, self._poll_ocr)
            return
        self._finish_capture(timeout=False)

    def _finish_capture(self, timeout=False):
        try:
            if self._splash is not None:
                self._splash.destroy()
        except Exception:
            pass
        self._splash = None

        try:
            if timeout:
                self._toast(t("ocr_timeout", sec=OCR_TIMEOUT_SEC))
                return

            if "error" in self._ocr_result:
                self._toast(t("ocr_fail", err=self._ocr_result['error']))
                return

            words = []
            lines_index = []

            if "rapid" in self._ocr_result:
                # ---- RapidOCR 输出解析 ----
                # 格式：[[box(4x2), text, score], ...] 或 None
                out = self._ocr_result["rapid"] or []
                items = []
                for entry in out:
                    try:
                        box, text, score = entry[0], entry[1], entry[2]
                        if not text:
                            continue
                        xs = [p[0] for p in box]
                        ys = [p[1] for p in box]
                        x0 = min(xs); x1 = max(xs)
                        y0 = min(ys); y1 = max(ys)
                        items.append({
                            "text": str(text),
                            "x": float(x0),
                            "y": float(y0),
                            "w": float(x1 - x0),
                            "h": float(y1 - y0),
                            "cy": float((y0 + y1) / 2),
                        })
                    except Exception:
                        continue
                items.sort(key=lambda it: (it["cy"], it["x"]))

                for line_idx, it in enumerate(items):
                    line_chars = []
                    line_word_ids = []
                    text = it["text"]
                    n = len(text)
                    if n == 0:
                        continue
                    # 基于行高模拟物理字宽从左端递推（不拉满 bbox）。
                    # 适合 RapidOCR 行 bbox 内 padding + 可能丢失的空格情景：
                    # → 只要行 bbox 左边对齐首字，后面字就能自行堆叠到正确位置，不会因行末 padding 而相对右飘。
                    line_h = max(1.0, float(it["h"]))
                    cjk_w = line_h * 0.98
                    ascii_w = line_h * 0.55
                    space_w = line_h * 0.35
                    def _phys_w(c):
                        if _is_cjk(c):
                            return cjk_w
                        if c == " ":
                            return space_w
                        return ascii_w
                    widths = [_phys_w(c) for c in text]
                    pad_x = min(6.0, line_h * 0.15)
                    x_start = it["x"] + pad_x
                    avail = max(1.0, it["w"] - 2.0 * pad_x)
                    total = sum(widths) or 1.0
                    # 只在物理宽推尼无可避免时才压缩，且下限 0.7、上限 1.2（避免离谱拉伸）
                    if total > avail:
                        scale = max(0.7, avail / total)
                        widths = [w * scale for w in widths]
                    elif total < avail * 0.6:
                        # OCR 行 ‘可能把它粘得很长’时适当拉一点
                        scale = min(1.2, avail / total)
                        widths = [w * scale for w in widths]
                    # 可见 bbox 居中收缩 22%，防止小偏移时物理跨到邻字
                    VIS_SHRINK = 0.78
                    cursor = x_start
                    for i, ch in enumerate(text):
                        cell_left = cursor
                        cell_w = widths[i]
                        cursor += cell_w
                        cx = cell_left + cell_w * 0.5
                        vis_w = cell_w * VIS_SHRINK
                        vis_x = cx - vis_w * 0.5
                        word_idx = len(words)
                        words.append({
                            "text": ch,
                            "x": vis_x,
                            "y": it["y"],
                            "w": vis_w,
                            "h": it["h"],
                            "line_id": line_idx,
                            "pos_in_line": i,
                        })
                        line_word_ids.append(word_idx)
                        line_chars.append((ch, word_idx))
                    line_text = "".join(c for c, _ in line_chars)
                    char_to_word = [wi for _, wi in line_chars]
                    lines_index.append({
                        "text": line_text,
                        "char_to_word": char_to_word,
                        "word_ids": line_word_ids,
                    })
            else:
                # ---- winocr 回退分支（原逻辑）----
                result = self._ocr_result.get("result") or {}

                for line in result.get("lines", []):
                    line_chars = []
                    line_word_ids = []
                    line_idx = len(lines_index)
                    word_list = line.get("words", []) or []
                    for i, w in enumerate(word_list):
                        br = w.get("bounding_rect") or {}
                        word_idx = len(words)
                        words.append({
                            "text": w.get("text", ""),
                            "x": float(br.get("x", 0)),
                            "y": float(br.get("y", 0)),
                            "w": float(br.get("width", 0)),
                            "h": float(br.get("height", 0)),
                            "line_id": line_idx,
                            "pos_in_line": i,
                        })
                        line_word_ids.append(word_idx)
                        w_text = w.get("text", "") or ""
                        for ch in w_text:
                            line_chars.append((ch, word_idx))
                        if i < len(word_list) - 1:
                            nxt_txt = word_list[i + 1].get("text", "") or ""
                            if _is_cjk(w_text) and _is_cjk(nxt_txt):
                                pass
                            else:
                                line_chars.append((" ", -1))
                    line_text = "".join(c for c, _ in line_chars)
                    char_to_word = [wi for _, wi in line_chars]
                    lines_index.append({
                        "text": line_text,
                        "char_to_word": char_to_word,
                        "word_ids": line_word_ids,
                    })

            if not words:
                self._toast(t("ocr_no_text"))
                return

            ctx = self._ocr_ctx
            self.overlay = OverlayWindow(
                self.root, words, lines_index,
                ctx["vx"], ctx["vy"], ctx["vw"], ctx["vh"],
                bg_image=self._ocr_ctx.get("img"),
            )
        finally:
            self._busy = False
            self._ocr_thread = None
            self._ocr_result = None
            self._ocr_ctx = None
            self._ocr_active_engine = None

    def _toast(self, text, ms=1500):
        try:
            top = tk.Toplevel(self.root)
            top.overrideredirect(True)
            top.attributes("-topmost", True)
            top.configure(bg="#333333")
            tk.Label(
                top, text=text, fg="white", bg="#333333",
                font=("Microsoft YaHei", 12), padx=20, pady=10,
            ).pack()
            top.update_idletasks()
            sw = top.winfo_screenwidth()
            sh = top.winfo_screenheight()
            top.geometry(f"+{sw//2 - top.winfo_width()//2}+{sh//2 - top.winfo_height()//2}")

            def _safe_destroy():
                try:
                    top.destroy()
                except Exception:
                    pass
            top.after(ms, _safe_destroy)
        except Exception:
            pass

    def run(self):
        self._start_tray()
        try:
            self.root.mainloop()
        finally:
            self._busy = False
            try:
                self.clip_watcher.stop()
            except Exception:
                pass
            try:
                self.uia_watcher.stop()
            except Exception:
                pass
            try:
                if self._tray_icon is not None:
                    self._tray_icon.stop()
            except Exception:
                pass
            try:
                keyboard.unhook_all()
            except Exception:
                pass


# ============================================================
#                    OverlayWindow（原样保留）
# ============================================================
class OverlayWindow:
    def __init__(self, root, words, lines_index, vx, vy, vw, vh, bg_image=None):
        self.root = root
        self.words = words
        self.lines_index = lines_index
        self.vx = vx
        self.vy = vy
        self.vw = vw
        self.vh = vh
        self._bg_pil = bg_image        # 选词模式下铺的原截图（PIL Image）
        self._bg_photo = None          # ImageTk.PhotoImage 引用，避免被 GC
        self._bg_canvas_item = None    # canvas image item id
        self.matches = []
        self.match_boxes = []
        self.active_index = -1
        self.alive = True

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.geometry(f"{vw}x{vh}+{vx}+{vy}")
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-transparentcolor", TRANSPARENT_COLOR)
        except Exception:
            pass
        self.win.configure(bg=TRANSPARENT_COLOR)

        self.canvas = tk.Canvas(
            self.win, width=vw, height=vh,
            bg=TRANSPARENT_COLOR, highlightthickness=0, bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        self.bar = tk.Frame(self.win, bg="#1e1e1e", bd=0, highlightthickness=2,
                            highlightbackground="#4a90e2", highlightcolor="#4a90e2")
        self.bar.place(relx=0.5, y=20, anchor="n")

        # 选词模式开关（在搜索栏最左侧，替代 Shift 拖选的老逻辑）
        self._select_mode_on = False
        self.select_btn = tk.Button(
            self.bar, text=t("btn_select"), command=self._toggle_select_mode,
            bg="#2b2b2b", fg="#e0e0e0",
            activebackground="#3a3a3a", activeforeground="white",
            bd=0, relief="flat",
            font=("Microsoft YaHei", 10, "bold"),
            padx=12, pady=6, cursor="hand2",
        )
        self.select_btn.pack(side="left", padx=(6, 4), pady=6)

        # 重新识别按钮：页面变化时手动重扫 OCR
        self.rescan_btn = tk.Button(
            self.bar, text=t("btn_rescan"), command=self._on_rescan_click,
            bg="#2b2b2b", fg="#e0e0e0",
            activebackground="#3a3a3a", activeforeground="white",
            bd=0, relief="flat",
            font=("Microsoft YaHei", 10, "bold"),
            padx=12, pady=6, cursor="hand2",
        )
        self.rescan_btn.pack(side="left", padx=(0, 4), pady=6)

        tk.Label(self.bar, text="🔍", bg="#1e1e1e", fg="#4a90e2",
                 font=("Segoe UI Emoji", 14), padx=8).pack(side="left")

        self.entry_var = tk.StringVar()
        self.entry = tk.Entry(
            self.bar, textvariable=self.entry_var,
            bg="#1e1e1e", fg="white", insertbackground="white",
            font=("Microsoft YaHei", 13), width=32, bd=0, relief="flat",
        )
        self.entry.pack(side="left", padx=(0, 4), pady=8)
        self.entry_var.trace_add("write", lambda *a: self._on_query_changed())

        self.status = tk.Label(
            self.bar, text=t("overlay_zero_status"), bg="#1e1e1e", fg="#aaaaaa",
            font=("Consolas", 11), padx=10,
        )
        self.status.pack(side="left")

        tk.Label(
            self.bar, text=t("overlay_hint"),
            bg="#1e1e1e", fg="#666666", font=("Microsoft YaHei", 9), padx=10,
        ).pack(side="left")

        # 右侧 × 关闭按钮：手动关闭 overlay
        self.close_btn = tk.Button(
            self.bar, text="✖", command=self._on_close_click,
            bg="#2b2b2b", fg="#e0e0e0",
            activebackground="#c94040", activeforeground="white",
            bd=0, relief="flat",
            font=("Segoe UI", 11, "bold"),
            padx=12, pady=6, cursor="hand2",
        )
        self.close_btn.pack(side="right", padx=(4, 6), pady=6)

        self.entry.bind("<Escape>", lambda e: self._on_escape())
        self.entry.bind("<Return>", lambda e: self._jump(+1))
        self.entry.bind("<Shift-Return>", lambda e: self._jump(-1))
        self.entry.bind("<F3>", lambda e: self._jump(+1))
        self.entry.bind("<Shift-F3>", lambda e: self._jump(-1))
        self.entry.bind("<Down>", lambda e: self._jump(+1))
        self.entry.bind("<Up>", lambda e: self._jump(-1))
        self.win.bind("<Escape>", lambda e: self._on_escape())

        self.canvas.bind("<Button-3>", lambda e: self.close())
        # 文本选择（仅 Shift+左键在已识别 word 内触发）
        # 不影响原“点空白关闭 / 点匹配框跳转”
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        # 选择状态
        self._sel_mode = None             # None | "click" | "text-select"
        self._sel_anchor_wid = None       # 文本选择起点 word id
        self._sel_current_wid = None      # 当前终点 word id
        self._sel_word_ids = []           # 当前选中的 word id 列表（阅读顺序）
        self._sel_hit_boxes = []          # 选中高亮框 item ids
        self._sel_popup = None            # 复制小窗 Toplevel
        self._sel_toast = None            # toast label
        self._sel_cursor_active = False   # 当前是否已将 canvas 光标改为 ibeam

        # 全局 Ctrl+C：选中后按一下就复制
        self.win.bind_all("<Control-c>", self._on_ctrl_c, add="+")
        self.win.bind_all("<Control-C>", self._on_ctrl_c, add="+")

        self.win.focus_force()
        self.entry.focus_set()
        try:
            hwnd = int(self.win.wm_frame(), 16) if self.win.wm_frame() else 0
            if hwnd:
                user32 = ctypes.windll.user32
                cur_thread = ctypes.windll.kernel32.GetCurrentThreadId()
                fg_hwnd = user32.GetForegroundWindow()
                fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None)
                if fg_thread and fg_thread != cur_thread:
                    user32.AttachThreadInput(fg_thread, cur_thread, True)
                    user32.BringWindowToTop(hwnd)
                    user32.SetForegroundWindow(hwnd)
                    user32.AttachThreadInput(fg_thread, cur_thread, False)
                else:
                    user32.BringWindowToTop(hwnd)
                    user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        self.win.after(30, lambda: (self.win.lift(),
                                    self.entry.focus_force()))
        self.win.after(120, lambda: self.entry.focus_force())

    def _on_query_changed(self):
        for group in self.match_boxes:
            for item in group:
                self.canvas.delete(item)
        self.match_boxes.clear()
        self.matches.clear()
        self.active_index = -1

        q = self.entry_var.get().strip()
        if not q:
            self.status.config(text=t("overlay_zero_status"))
            return

        q_lower = q.lower()
        for line in self.lines_index:
            text_l = line["text"].lower()
            char_map = line["char_to_word"]
            if not text_l:
                continue
            start = 0
            while True:
                pos = text_l.find(q_lower, start)
                if pos < 0:
                    break
                end = pos + len(q_lower)
                word_ids = []
                seen = set()
                for i in range(pos, end):
                    if i >= len(char_map):
                        break
                    wid = char_map[i]
                    if wid >= 0 and wid not in seen:
                        seen.add(wid)
                        word_ids.append(wid)
                if word_ids:
                    self.matches.append(word_ids)
                    self._draw_match_boxes(word_ids, active=False)
                start = pos + max(1, len(q_lower))

        if self.matches:
            self.active_index = 0
            self._highlight_active()
            self.status.config(text=f"1/{len(self.matches)}")
        else:
            self.status.config(text=t("overlay_no_match"), fg="#ff7676")
            self.status.after(600, lambda: self._safe_status_reset())

    def _safe_status_reset(self):
        try:
            self.status.config(fg="#aaaaaa")
        except Exception:
            pass

    def _draw_match_boxes(self, word_ids, active=False):
        if not word_ids:
            self.match_boxes.append([])
            return []
        outline = HIGHLIGHT_ACTIVE if active else HIGHLIGHT_OUTLINE
        width = 2 if active else 1
        xs1, ys1, xs2, ys2 = [], [], [], []
        for wid in word_ids:
            w = self.words[wid]
            xs1.append(w["x"])
            ys1.append(w["y"])
            xs2.append(w["x"] + w["w"])
            ys2.append(w["y"] + w["h"])
        x1 = min(xs1) - self.vx - 3
        y1 = min(ys1) - self.vy - 3
        x2 = max(xs2) - self.vx + 3
        y2 = max(ys2) - self.vy + 3
        item = self.canvas.create_rectangle(
            x1, y1, x2, y2,
            fill="",
            outline=outline,
            width=width,
        )
        self.match_boxes.append([item])
        return [item]

    def _highlight_active(self):
        for group in self.match_boxes:
            for item in group:
                self.canvas.delete(item)
        self.match_boxes.clear()
        for i, word_ids in enumerate(self.matches):
            self._draw_match_boxes(word_ids, active=(i == self.active_index))
        self.status.config(text=f"{self.active_index+1}/{len(self.matches)}")

    def _jump(self, direction):
        if not self.matches:
            return "break"
        self.active_index = (self.active_index + direction) % len(self.matches)
        self._highlight_active()
        word_ids = self.matches[self.active_index]
        if word_ids:
            w = self.words[word_ids[0]]
            cx = int(w["x"] + w["w"] / 2)
            cy = int(w["y"] + w["h"] / 2)
            try:
                ctypes.windll.user32.SetCursorPos(cx, cy)
            except Exception:
                pass
        return "break"

    # ============================================================
    #             文本选择 (Shift + 左键拖拽选词)
    # ============================================================
    def _word_at(self, x, y):
        """先 canvas 坐标 (x,y) 射中的 word 索引；未命中返回 None。"""
        # 反向遍历：后画的在上
        for wid in range(len(self.words) - 1, -1, -1):
            w = self.words[wid]
            wx = w["x"] - self.vx
            wy = w["y"] - self.vy
            if wx <= x <= wx + w["w"] and wy <= y <= wy + w["h"]:
                return wid
        return None

    def _shift_pressed(self, event):
        # Tk event.state 位 0 = Shift（保留但当前逻辑不再依赖它）
        try:
            return bool(event.state & 0x0001)
        except Exception:
            return False

    # ============================================================
    #        选词模式开关（搜索栏左侧 ✂ 按钮）
    # ============================================================
    def _toggle_select_mode(self):
        if self._select_mode_on:
            self._exit_select_mode()
        else:
            self._enter_select_mode()

    def _on_rescan_click(self):
        """手动重新 OCR：先退出选词模式 + 关闭当前 overlay，再让 App 重新截屏 OCR。
        使用 root.after 避免在回调中摧毁自己时的 Tk 崩溃风险。"""
        try:
            root = self.root
        except Exception:
            root = None
        try:
            if self._select_mode_on:
                self._exit_select_mode()
        except Exception:
            pass
        try:
            self.close()
        except Exception:
            pass
        # 延迟到下一轮事件循环，避免重入 canvas destroy
        if root is not None:
            try:
                root.after(30, lambda: root.event_generate("<<ScreenSearchRescan>>", when="tail"))
            except Exception:
                pass

    def _on_escape(self):
        # 选词模式下 Esc 只退出选词；否则关闭 overlay
        if self._select_mode_on:
            self._exit_select_mode()
            return "break"
        self.close()
        return "break"

    def _on_close_click(self):
        """右侧 × 按钮：无论是否在选词模式一律关闭 overlay。"""
        try:
            if self._select_mode_on:
                self._exit_select_mode()
        except Exception:
            pass
        try:
            self.close()
        except Exception:
            pass

    def _enter_select_mode(self):
        self._select_mode_on = True
        try:
            # 按钮高亮：蓝底白字 + 文字变成“退出选词”
            self.select_btn.config(
                text=t("btn_select_exit"),
                bg="#4a90e2", fg="white",
                activebackground="#3a80d0", activeforeground="white",
            )
        except Exception:
            pass
        # 铺全屏截图背景：整个页面看着被“冻住”
        self._install_screenshot_backdrop()
        # 搜索栏锁住：不抢焦点，避免拖拽时敲键干扰匹配
        try:
            self.entry.config(state="disabled")
        except Exception:
            pass
        # canvas 全局 I-beam 光标，提示可选中
        try:
            self.canvas.config(cursor="xterm")
            self._sel_cursor_active = True
        except Exception:
            pass

    def _exit_select_mode(self):
        self._select_mode_on = False
        try:
            self.select_btn.config(
                text=t("btn_select"),
                bg="#2b2b2b", fg="#e0e0e0",
                activebackground="#3a3a3a", activeforeground="white",
            )
        except Exception:
            pass
        # 接除截图背景 + 清当前选中
        self._remove_screenshot_backdrop()
        self._clear_selection_visuals()
        try:
            self.entry.config(state="normal")
            self.entry.focus_set()
        except Exception:
            pass
        try:
            self.canvas.config(cursor="")
            self._sel_cursor_active = False
        except Exception:
            pass

    def _install_screenshot_backdrop(self):
        """把 OCR 时拍的原截图铺到 canvas 最底层，作为“锁屏”背景。"""
        if self._bg_pil is None or self._bg_canvas_item is not None:
            return
        try:
            img = self._bg_pil
            # ImageGrab.grab(all_screens=True) 已是虚拟屏尺寸（vw×vh），
            # 预防尺寸不符时自适应
            if img.size != (self.vw, self.vh):
                img = img.resize((self.vw, self.vh), Image.LANCZOS)
            self._bg_photo = ImageTk.PhotoImage(img, master=self.win)
            iid = self.canvas.create_image(0, 0, anchor="nw", image=self._bg_photo)
            # 一定要沉到最底，避免遮住匹配框 / 选区框
            self.canvas.tag_lower(iid)
            self._bg_canvas_item = iid
        except Exception:
            self._bg_photo = None
            self._bg_canvas_item = None

    def _remove_screenshot_backdrop(self):
        if self._bg_canvas_item is not None:
            try:
                self.canvas.delete(self._bg_canvas_item)
            except Exception:
                pass
            self._bg_canvas_item = None
        self._bg_photo = None

    def _on_motion(self, event):
        # 拖拽中：更新选择终点
        if self._sel_mode == "text-select":
            wid = self._word_at(event.x, event.y)
            if wid is not None:
                if self._sel_anchor_wid is None:
                    self._sel_anchor_wid = wid
                if wid != self._sel_current_wid:
                    self._sel_current_wid = wid
                    self._update_text_selection()
            return
        # 非拖拽：选词模式下全区光标保持 I-beam；否则默认
        try:
            if self._select_mode_on:
                if not self._sel_cursor_active:
                    self.canvas.config(cursor="xterm")
                    self._sel_cursor_active = True
            else:
                if self._sel_cursor_active:
                    self.canvas.config(cursor="")
                    self._sel_cursor_active = False
        except Exception:
            pass

    def _on_press(self, event):
        wid = self._word_at(event.x, event.y)
        if self._select_mode_on:
            # 选词模式：拖拽选择；不关闭 overlay
            self._clear_selection_visuals()
            self._sel_mode = "text-select"
            self._sel_anchor_wid = wid  # 可能为 None，motion 会履新
            self._sel_current_wid = wid
            if wid is not None:
                self._update_text_selection()
            return "break"
        # 普通左键：记录待定 click，release 时走原逻辑
        self._sel_mode = "click"
        # 不返回 "break"，保留默认行为

    def _on_release(self, event):
        mode = self._sel_mode
        self._sel_mode = None
        if mode == "text-select":
            # 拖拽结束 → 最后一次刷新选择 + 弹小窗
            wid = self._word_at(event.x, event.y)
            if wid is not None:
                if self._sel_anchor_wid is None:
                    self._sel_anchor_wid = wid
                self._sel_current_wid = wid
                self._update_text_selection()
            if self._sel_word_ids:
                self._show_copy_popup(len(self._sel_word_ids))
            return "break"
        # click 或 None → 走原 click 逻辑
        self._on_canvas_click(event)

    def _selected_word_ids_in_range(self, a, b):
        """从 word a 到 word b 按阅读顺序取中间所有 word id。"""
        if a is None or b is None:
            return []
        lo, hi = (a, b) if a <= b else (b, a)
        return list(range(lo, hi + 1))

    def _update_text_selection(self):
        hits = self._selected_word_ids_in_range(self._sel_anchor_wid, self._sel_current_wid)
        self._sel_word_ids = hits
        self._draw_selection_highlight(hits)

    def _hits_to_text(self, hits):
        """按阅读顺序拼回文本。
        利用 line.char_to_word：找到命中 word 对应的字符区间，直接从 line.text 切片。
        既避免 RapidOCR 单字拆分后 query→"q u e r y"，又保留 winocr 英文词间空格。"""
        if not hits:
            return ""
        hit_set = set(hits)
        parts = []
        for line in self.lines_index:
            char_map = line.get("char_to_word") or []
            line_text = line.get("text", "") or ""
            if not char_map:
                continue
            idxs = [i for i, wid in enumerate(char_map) if wid in hit_set]
            if not idxs:
                continue
            c_lo = min(idxs)
            c_hi = max(idxs)
            sub = line_text[c_lo:c_hi + 1]
            if sub:
                parts.append(sub)
        return "\n".join(parts)

    def _draw_selection_highlight(self, hits):
        for iid in self._sel_hit_boxes:
            try:
                self.canvas.delete(iid)
            except Exception:
                pass
        self._sel_hit_boxes.clear()
        for wid in hits:
            w = self.words[wid]
            x1 = w["x"] - self.vx - 1
            y1 = w["y"] - self.vy - 1
            x2 = x1 + w["w"] + 2
            y2 = y1 + w["h"] + 2
            # 黄色实线框——类似文本高亮
            iid = self.canvas.create_rectangle(
                x1, y1, x2, y2,
                outline="#ffd93d", width=2, fill="",
            )
            self._sel_hit_boxes.append(iid)

    def _clear_selection_visuals(self):
        for iid in self._sel_hit_boxes:
            try:
                self.canvas.delete(iid)
            except Exception:
                pass
        self._sel_hit_boxes.clear()
        self._sel_word_ids = []
        self._sel_anchor_wid = None
        self._sel_current_wid = None
        if self._sel_popup is not None:
            try:
                self._sel_popup.destroy()
            except Exception:
                pass
            self._sel_popup = None
        if self._sel_toast is not None:
            try:
                self._sel_toast.destroy()
            except Exception:
                pass
            self._sel_toast = None

    def _show_copy_popup(self, count):
        """在选区右下角弹个小窗，含一个 复制 按钮。"""
        if self._sel_popup is not None:
            try:
                self._sel_popup.destroy()
            except Exception:
                pass
            self._sel_popup = None

        # 选中所有词的包围盒 → popup 默认靠近右下
        if not self._sel_word_ids:
            return
        xs2, ys2 = [], []
        for wid in self._sel_word_ids:
            w = self.words[wid]
            xs2.append(w["x"] + w["w"] - self.vx)
            ys2.append(w["y"] + w["h"] - self.vy)
        anchor_x = max(xs2) + 6
        anchor_y = max(ys2) + 6

        pop = tk.Toplevel(self.win)
        pop.overrideredirect(True)
        pop.attributes("-topmost", True)
        pop.configure(bg="#1e1e1e")
        frm = tk.Frame(pop, bg="#1e1e1e", bd=0, highlightthickness=2,
                       highlightbackground="#4a90e2", highlightcolor="#4a90e2")
        frm.pack(fill="both", expand=True)
        btn = tk.Button(
            frm, text=t("sel_btn_copy", n=count),
            command=self._do_copy_selection,
            bg="#2b2b2b", fg="white", activebackground="#3a5a80",
            activeforeground="white", bd=0, relief="flat",
            font=("Microsoft YaHei", 10), padx=10, pady=4, cursor="hand2",
        )
        btn.pack(side="left", padx=(6, 2), pady=4)
        close_btn = tk.Button(
            frm, text=t("sel_btn_close"),
            command=self._clear_selection_visuals,
            bg="#2b2b2b", fg="#aaaaaa", activebackground="#444444",
            activeforeground="white", bd=0, relief="flat",
            font=("Consolas", 10), padx=6, pady=4, cursor="hand2",
        )
        close_btn.pack(side="left", padx=(0, 6), pady=4)

        # 定位到虚拟屏绝对坐标
        pop.update_idletasks()
        pw = pop.winfo_reqwidth()
        ph = pop.winfo_reqheight()
        # 防出屏（左/上/右/下）
        gx = self.vx + anchor_x
        gy = self.vy + anchor_y
        if gx + pw > self.vx + self.vw:
            gx = self.vx + self.vw - pw - 4
        if gy + ph > self.vy + self.vh:
            gy = self.vy + min(ys2) - ph - 6  # 安到选区上方
        if gx < self.vx:
            gx = self.vx + 4
        if gy < self.vy:
            gy = self.vy + 4
        pop.geometry(f"+{int(gx)}+{int(gy)}")
        self._sel_popup = pop

    def _do_copy_selection(self):
        text = self._hits_to_text(self._sel_word_ids)
        if not text:
            self._show_sel_toast(t("sel_toast_empty"))
            return
        try:
            suppress_clipboard(text)
            self.win.clipboard_clear()
            self.win.clipboard_append(text)
            # 确保写入系统剪贴板（Tk 内部还会在 destroy 时丢丁）
            self.win.update()
        except Exception:
            pass
        n = len(text)
        self._show_sel_toast(t("sel_toast_copied", n=n))

    def _on_ctrl_c(self, event=None):
        # 若搜索框正在编辑且有选中内容 → 让 Entry 自己处理（不拦截）
        try:
            if self.entry.selection_present():
                return None
        except Exception:
            pass
        if self._sel_word_ids:
            self._do_copy_selection()
            return "break"
        return None

    def _show_sel_toast(self, message, ms=1400):
        if self._sel_toast is not None:
            try:
                self._sel_toast.destroy()
            except Exception:
                pass
            self._sel_toast = None
        top = tk.Toplevel(self.win)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg="#1e1e1e")
        lbl = tk.Label(
            top, text=message,
            bg="#1e1e1e", fg="#e0e0e0",
            font=("Microsoft YaHei", 10),
            padx=14, pady=6,
            bd=0, highlightthickness=1, highlightbackground="#4a90e2",
        )
        lbl.pack()
        top.update_idletasks()
        tw = top.winfo_reqwidth()
        th = top.winfo_reqheight()
        gx = self.vx + (self.vw - tw) // 2
        gy = self.vy + self.vh - th - 60
        top.geometry(f"+{int(gx)}+{int(gy)}")
        self._sel_toast = top
        top.after(ms, lambda tt=top: (self._safe_destroy(tt)))

    def _safe_destroy(self, w):
        try:
            w.destroy()
        except Exception:
            pass
        if self._sel_toast is w:
            self._sel_toast = None

    def _on_canvas_click(self, event):
        items = self.canvas.find_overlapping(event.x, event.y, event.x, event.y)
        if not items:
            self.close()
            return
        for i, group in enumerate(self.match_boxes):
            for iid in group:
                if iid in items:
                    self.active_index = i
                    self._highlight_active()
                    return
        self.close()

    def close(self):
        self.alive = False
        # 解除全局 Ctrl+C 绑定，避免影响后续花胶
        try:
            self.win.unbind_all("<Control-c>")
            self.win.unbind_all("<Control-C>")
        except Exception:
            pass
        # 清选择面板
        try:
            self._clear_selection_visuals()
        except Exception:
            pass
        # 选词背景图引用清掉
        try:
            self._remove_screenshot_backdrop()
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass


# ============================================================
#              开机自启（管理员计划任务）
# ============================================================
AUTOSTART_TASK_BASE = "ScreenSearch"
AUTOSTART_TASK_ADMIN_SUFFIX = "_Full"     # 高权限任务（旧行为）
AUTOSTART_TASK_USER_SUFFIX = "_UserOnly"  # 普通用户任务（新增安全档）


def _script_signature():
    """基于脚本路径的 SHA1 前 8 位，避免多份安装互相覆盖。"""
    try:
        import hashlib
        h = hashlib.sha1(os.path.abspath(__file__).encode("utf-8", errors="replace")).hexdigest()
        return "_" + h[:8]
    except Exception:
        return ""


def _task_name(privileged):
    suffix = AUTOSTART_TASK_ADMIN_SUFFIX if privileged else AUTOSTART_TASK_USER_SUFFIX
    return AUTOSTART_TASK_BASE + suffix + _script_signature()


# 向后兼容：旧安装可能写的是不带后缀的 "ScreenSearch"，存在时亦视为已安装
AUTOSTART_TASK_NAME_LEGACY = AUTOSTART_TASK_BASE


def _task_query(task_name):
    try:
        r = subprocess.run(
            ["schtasks", "/Query", "/TN", task_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
        )
        return r.returncode == 0
    except Exception:
        return False


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def autostart_exists(privileged=True):
    """privileged=True 查高权限档（兼容旧名）；False 查普通档。"""
    if _task_query(_task_name(privileged)):
        return True
    if privileged and _task_query(AUTOSTART_TASK_NAME_LEGACY):
        return True
    return False


def autostart_install(privileged=True):
    """创建登录启动任务。privileged=True: 高权限(需当前进程为管理员)；
    False: 普通用户权限(无 /RL HIGHEST，无需 UAC)。成功返回 True。"""
    py = _find_pythonw()
    script = os.path.abspath(__file__)
    tr = f'"{py}" "{script}"'
    tn = _task_name(privileged)
    args = [
        "schtasks", "/Create",
        "/TN", tn,
        "/TR", tr,
        "/SC", "ONLOGON",
        "/F",
    ]
    if privileged:
        args += ["/RL", "HIGHEST"]
    try:
        r = subprocess.run(
            args, capture_output=True, text=True,
            creationflags=0x08000000,
        )
        return r.returncode == 0
    except Exception:
        return False


def autostart_remove(privileged=True):
    """删除指定档任务。高权限档同时尝试清理旧名字任务。"""
    ok = False
    tn = _task_name(privileged)
    try:
        r = subprocess.run(
            ["schtasks", "/Delete", "/TN", tn, "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
        )
        ok = r.returncode == 0
    except Exception:
        ok = False
    if privileged:
        try:
            subprocess.run(
                ["schtasks", "/Delete", "/TN", AUTOSTART_TASK_NAME_LEGACY, "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=0x08000000,
            )
        except Exception:
            pass
    return ok


# 任务管理器显示的可执行名
# 全角 U+FF06 (＆) 避开 cmd/shell 的 & 转义，普通 Unicode 文件名安全
# ⚠ 品牌副本必须与 python314.dll 同目录（真解释器目录，如 pythoncore-3.14-64）
#   Python\bin\ 里的是 py-install shim，不能直接改名（改后加载不到 DLL）
BRANDED_EXE_NAME = "OCR＆Keyboard Record.exe"


def _find_pythonw():
    """定位当前环境的 GUI 解释器（避免开机自启后弹控制台窗口）。
    顺序：
    1) 同目录下的品牌化副本 BRANDED_EXE_NAME（任务管理器友好显示）
    2) 相邻 pythoncore-* 真解释器目录里的品牌副本
    3) 同目录 pythonw.exe
    4) sys.executable 本身（实在找不到 pythonw 时就用 python.exe）"""
    exe = sys.executable
    exe_dir = os.path.dirname(exe)
    branded = os.path.join(exe_dir, BRANDED_EXE_NAME)
    if os.path.exists(branded):
        return branded
    # 若当前跑在 shim 目录（例如 Python\bin），真解释器通常在 Python\pythoncore-*
    try:
        parent = os.path.dirname(exe_dir)
        if parent and os.path.isdir(parent):
            for name in os.listdir(parent):
                if name.lower().startswith("pythoncore-"):
                    cand = os.path.join(parent, name, BRANDED_EXE_NAME)
                    if os.path.exists(cand):
                        return cand
    except Exception:
        pass
    cand = os.path.join(exe_dir, "pythonw.exe")
    if os.path.exists(cand):
        return cand
    return exe


def relaunch_as_admin(extra_args=None):
    """以管理员方式重新启动自己，弹 UAC。成功则返回 True（调用方应 sys.exit）。"""
    py = _find_pythonw()
    script = os.path.abspath(__file__)
    args = [script] + list(extra_args or [])
    params = " ".join(f'"{a}"' for a in args)
    try:
        # ShellExecuteW with 'runas' verb triggers UAC
        hinst = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", py, params, os.path.dirname(script), 1
        )
        # ShellExecuteW returns > 32 on success
        return int(hinst) > 32
    except Exception:
        return False


class SingleInstanceGuard:
    """Per-installation Windows mutex preventing duplicate hooks and log writers."""

    ERROR_ALREADY_EXISTS = 183

    def __init__(self):
        import hashlib
        signature = hashlib.sha256(
            os.path.abspath(__file__).casefold().encode("utf-8", errors="replace")
        ).hexdigest()[:16]
        self.name = f"Local\\OCRKeyboardRecord_{signature}"
        self.handle = None

    def acquire(self):
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
        kernel32.CreateMutexW.restype = wt.HANDLE
        kernel32.CloseHandle.argtypes = [wt.HANDLE]
        kernel32.CloseHandle.restype = wt.BOOL
        kernel32.SetLastError(0)
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            return False
        if kernel32.GetLastError() == self.ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self.handle = handle
        return True

    def release(self):
        if self.handle:
            try:
                ctypes.windll.kernel32.CloseHandle(self.handle)
            finally:
                self.handle = None


def main():
    # 子命令：管理员重启后自动安装开机启动（高权限档）
    if "--set-autostart" in sys.argv:
        if is_admin():
            autostart_install(privileged=True)
        return

    guard = SingleInstanceGuard()
    if not guard.acquire():
        return
    try:
        app = ScreenSearchApp()
        print(t(
            "startup_line",
            ocr=HOTKEY.upper(),
            kl=KEYLOG_HOTKEY.upper(),
            nt=NEW_TEXT_HOTKEY.upper(),
        ))
        try:
            app.run()
        except KeyboardInterrupt:
            pass
    finally:
        guard.release()


if __name__ == "__main__":
    main()
