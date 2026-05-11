# -*- coding: utf-8 -*-
"""每周工作总结生成器 v2.1"""

import os
import re
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog
import ctypes

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).parent))

try:
    import customtkinter as ctk
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "customtkinter"])
    import customtkinter as ctk

from PIL import Image

from sticky_notes_reader import StickyNotesReader, close_sticky_notes, open_sticky_notes
from ai_summarizer import BilingualSummarizer
from config_manager import ConfigManager

ctk.set_appearance_mode("dark")

# ─── Deep Navy palette ─────────────────────────────────────────
BG_MAIN  = "#1e2d42"
BLUE     = "#4a9ede"
BLUE_HVR = "#6bb5f0"
SUCCESS  = "#62c58e"
ERROR    = "#cf9585"
WARNING  = "#e4b86a"
SEP      = "#2e4260"
TROUGH   = "#283a50"
BTN_SEC  = "#2e4058"
BTN_HVR  = "#385070"
FONT     = "Microsoft YaHei UI"
PROJECT_DIR = Path(__file__).parent


def _set_window_icon(win):
    """Set window + taskbar icon via Win32 API for crisp high-DPI rendering."""
    try:
        ico = PROJECT_DIR / "icon.ico"
        if not ico.exists():
            return
        win.iconbitmap(str(ico))
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010
        LR_DEFAULTSIZE = 0x0040
        WM_SETICON = 0x0080
        ICON_BIG = 1
        ICON_SMALL = 0
        ico_path = str(ico)
        hicon_big = ctypes.windll.user32.LoadImageW(
            0, ico_path, IMAGE_ICON, 256, 256,
            LR_LOADFROMFILE)
        hicon_small = ctypes.windll.user32.LoadImageW(
            0, ico_path, IMAGE_ICON, 32, 32,
            LR_LOADFROMFILE)
        if hicon_big:
            ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
        if hicon_small:
            ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
    except Exception:
        pass


def _clear_white(img):
    img = img.convert("RGBA")
    px = list(img.getdata())
    img.putdata([(r, g, b, 0) if r > 240 and g > 240 and b > 240
                 else (r, g, b, a) for r, g, b, a in px])
    return img


# ═══════════════════════════════════════════════════════════════
#  Settings Dialog
# ═══════════════════════════════════════════════════════════════
class SettingsDialog(ctk.CTkToplevel):

    def __init__(self, parent, cfg, on_save=None):
        super().__init__(parent)
        self.cfg = cfg
        self.on_save = on_save
        self._conf = cfg.load()

        self.title("设置")
        self.geometry("420x520")
        self.resizable(False, False)
        self.configure(fg_color=BG_MAIN)
        self.transient(parent)
        self.grab_set()
        self.after(100, self._center)
        self.after(150, self.lift)

        _set_window_icon(self)

        pad = dict(fill="x", padx=30)

        ctk.CTkLabel(self, text="中文姓名", font=(FONT, 13),
                     anchor="w").pack(**pad, pady=(24, 4))
        self.e_cn = ctk.CTkEntry(self, placeholder_text="请输入中文姓名",
                                 font=(FONT, 13), height=38)
        self.e_cn.pack(**pad)
        if self._conf.get("user_name_cn"):
            self.e_cn.insert(0, self._conf["user_name_cn"])

        ctk.CTkLabel(self, text="英文姓名", font=(FONT, 13),
                     anchor="w").pack(**pad, pady=(12, 4))
        self.e_en = ctk.CTkEntry(self, placeholder_text="可选",
                                 font=(FONT, 13), height=38)
        self.e_en.pack(**pad)
        if self._conf.get("user_name_en"):
            self.e_en.insert(0, self._conf["user_name_en"])

        ctk.CTkLabel(self, text="API Key", font=(FONT, 13),
                     anchor="w").pack(**pad, pady=(12, 4))
        kf = ctk.CTkFrame(self, fg_color="transparent")
        kf.pack(**pad)
        self.e_key = ctk.CTkEntry(kf, placeholder_text="sk-...",
                                  show="•", font=(FONT, 13), height=38)
        self.e_key.pack(side="left", fill="x", expand=True)
        if self._conf.get("api_key"):
            self.e_key.insert(0, self._conf["api_key"])
        self._vis = False
        self._eye = ctk.CTkButton(kf, text="显示", width=50, height=38,
                                  fg_color="transparent", text_color="gray",
                                  hover_color=("gray85", BTN_SEC),
                                  font=(FONT, 11), command=self._toggle)
        self._eye.pack(side="left", padx=(4, 0))

        tf = ctk.CTkFrame(self, fg_color="transparent")
        tf.pack(**pad, pady=(4, 0))
        self.t_btn = ctk.CTkButton(tf, text="测试连接", width=80, height=28,
                                   fg_color="transparent", text_color=BLUE,
                                   hover_color=("gray85", BTN_SEC),
                                   font=(FONT, 11), command=self._test)
        self.t_btn.pack(side="left")
        self.t_lbl = ctk.CTkLabel(tf, text="", font=(FONT, 11))
        self.t_lbl.pack(side="left", padx=(8, 0))

        ctk.CTkLabel(self, text="保存位置", font=(FONT, 13),
                     anchor="w").pack(**pad, pady=(16, 4))
        lf = ctk.CTkFrame(self, fg_color="transparent")
        lf.pack(**pad)
        self._loc = self._conf.get("save_location") or str(Path.home() / "Desktop")
        self.l_lbl = ctk.CTkLabel(lf, text=self._abbr(self._loc), font=(FONT, 11),
                                  text_color="gray", anchor="w")
        self.l_lbl.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(lf, text="...", width=38, height=32,
                      fg_color=("gray85", BTN_SEC),
                      hover_color=("gray75", BTN_HVR),
                      text_color=("gray20", "#8898b5"),
                      command=self._browse).pack(side="left", padx=(4, 0))

        self.err = ctk.CTkLabel(self, text="", font=(FONT, 11), text_color=ERROR)
        self.err.pack(**pad, pady=(12, 0))

        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.pack(**pad, pady=(8, 20))
        ctk.CTkButton(bf, text="保存", width=100, height=40, corner_radius=8,
                      fg_color=BLUE, hover_color=BLUE_HVR,
                      font=(FONT, 13, "bold"), command=self._save
                      ).pack(side="left")
        ctk.CTkButton(bf, text="取消", width=80, height=40, corner_radius=8,
                      fg_color=("gray85", BTN_SEC),
                      hover_color=("gray75", BTN_HVR),
                      text_color=("gray20", "#8898b5"),
                      font=(FONT, 13), command=self.destroy
                      ).pack(side="left", padx=(12, 0))

    def _center(self):
        px, py = self.master.winfo_x(), self.master.winfo_y()
        pW, pH = self.master.winfo_width(), self.master.winfo_height()
        w, h = 420, 520
        self.geometry(f"+{px + (pW - w) // 2}+{py + (pH - h) // 2}")

    @staticmethod
    def _abbr(p):
        s = str(p)
        return s if len(s) < 40 else "…" + s[-37:]

    def _toggle(self):
        self._vis = not self._vis
        self.e_key.configure(show="" if self._vis else "•")
        self._eye.configure(text="隐藏" if self._vis else "显示")

    def _browse(self):
        f = filedialog.askdirectory(initialdir=self._loc)
        if f:
            self._loc = f
            self.l_lbl.configure(text=self._abbr(f))

    def _test(self):
        key = self.e_key.get().strip()
        if not key:
            self.t_lbl.configure(text="请输入 Key", text_color=WARNING)
            return
        self.t_btn.configure(state="disabled")
        self.t_lbl.configure(text="连接中…", text_color="gray")

        def go():
            ok, msg = BilingualSummarizer.test_connection(key)
            self.after(0, lambda: self._test_done(ok, msg))
        threading.Thread(target=go, daemon=True).start()

    def _test_done(self, ok, msg):
        self.t_btn.configure(state="normal")
        if ok:
            self.t_lbl.configure(text="✓ 连接成功", text_color=SUCCESS)
        else:
            m = msg[:30] + "…" if len(msg) > 30 else msg
            self.t_lbl.configure(text=m, text_color=ERROR)

    def _save(self):
        cn = self.e_cn.get().strip()
        en = self.e_en.get().strip()
        key = self.e_key.get().strip()
        if not cn:
            self.err.configure(text="请输入中文姓名")
            return
        if not key:
            self.err.configure(text="请输入 API Key")
            return
        self._conf.update(user_name_cn=cn, user_name_en=en or cn,
                          api_key=key, save_location=self._loc)
        self.cfg.save(self._conf)
        if self.on_save:
            self.on_save()
        self.destroy()


# ═══════════════════════════════════════════════════════════════
#  Main Window
# ═══════════════════════════════════════════════════════════════
class WeeklySummaryApp(ctk.CTk):

    def __init__(self):
        super().__init__()
        self.title("Weekly Summary")
        self.geometry("600x460")
        self.resizable(False, False)
        self.configure(fg_color=BG_MAIN)

        _set_window_icon(self)

        self.cfg_mgr = ConfigManager()
        self.is_running = False
        self.notes_content = ""
        self.week_range = ""
        self.target_notes = {}

        self._build_ui()
        self._center()

        if not self.cfg_mgr.is_configured():
            self.after(400, lambda: self._open_settings(True))
        else:
            self._load_notes()

    def _center(self):
        w, h = 600, 460
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _build_ui(self):
        ctk.CTkButton(
            self, text="⚙", width=32, height=32,
            fg_color="transparent", hover_color=("gray85", BTN_SEC),
            text_color="gray", font=("Segoe UI", 18),
            command=self._open_settings
        ).place(relx=1.0, x=-16, y=12, anchor="ne")

        c = ctk.CTkFrame(self, fg_color="transparent")
        c.place(relx=0.5, rely=0.5, anchor="center")

        self._logo = None
        lp = PROJECT_DIR / "logo.png"
        if lp.exists():
            try:
                img = _clear_white(Image.open(lp))
                ratio = img.width / img.height
                ht = 56
                self._logo = ctk.CTkImage(light_image=img, dark_image=img,
                                          size=(int(ht * ratio), ht))
                ctk.CTkLabel(c, image=self._logo, text="").pack(pady=(0, 8))
            except Exception:
                pass

        ctk.CTkLabel(c, text="Weekly Summary",
                     font=("Segoe UI", 22, "bold")).pack()
        ctk.CTkLabel(c, text="Powered by DeepSeek",
                     font=("Segoe UI", 11), text_color="#7a8fa8").pack(pady=(2, 0))

        ctk.CTkFrame(c, fg_color=("gray80", SEP),
                     height=1, width=120).pack(pady=16)

        self.week_lbl = ctk.CTkLabel(c, text="",
                                     font=(FONT, 18, "bold"), text_color=BLUE)
        self.week_lbl.pack()

        self.status_lbl = ctk.CTkLabel(c, text="请先完成设置",
                                       font=(FONT, 11), text_color="#8b96a8",
                                       wraplength=300)
        self.status_lbl.pack(pady=(4, 0))

        self.progress = ctk.CTkProgressBar(c, width=340, height=4,
                                           progress_color=BLUE,
                                           fg_color=("gray85", TROUGH))
        self.progress.pack(pady=(14, 0))
        self.progress.set(0)

        self.gen_btn = ctk.CTkButton(
            c, text="生成周报", command=self._start_gen,
            width=180, height=40, corner_radius=10,
            fg_color=BLUE, hover_color=BLUE_HVR,
            font=(FONT, 14, "bold"))
        self.gen_btn.pack(pady=(16, 0))

        self.nw_btn = ctk.CTkButton(
            c, text="\U0001f4c5 启动下一周", command=self._next_week,
            width=120, height=32, corner_radius=8,
            fg_color=("gray85", BTN_SEC),
            hover_color=("gray75", BTN_HVR),
            text_color=("gray30", "#8898b5"),
            font=(FONT, 11))

    def _open_settings(self, first_run=False):
        if self.is_running:
            return
        SettingsDialog(self, self.cfg_mgr, on_save=self._load_notes)

    def _set_status(self, text, color="#8898b5"):
        self.status_lbl.configure(text=text, text_color=color)

    def _load_notes(self):
        if not self.cfg_mgr.is_configured():
            self._set_status("请在设置中配置姓名和 API Key", WARNING)
            self.gen_btn.configure(state="disabled", fg_color="gray")
            return
        try:
            reader = StickyNotesReader()
            self.target_notes = reader.get_target_notes()
            self.notes_content = reader.format_notes_for_summary()
            self.week_range = self._detect_week(self.notes_content)
            self.week_lbl.configure(text=self.week_range)
            name = self.cfg_mgr.load().get("user_name_cn", "")
            self._set_status(f"✓ 就绪 · {name}", SUCCESS)
            self.gen_btn.configure(state="normal", fg_color=BLUE)
        except Exception as e:
            msg = str(e)
            if "便笺" in msg or "plum" in msg:
                self._set_status("未找到便笺数据库，请确认已使用 Sticky Notes", ERROR)
            else:
                self._set_status(f"加载失败: {type(e).__name__}", ERROR)
            self.gen_btn.configure(state="disabled", fg_color="gray")

    def _detect_week(self, content):
        for p in [r'(\d{2}-\d{2})\s*~\s*(\d{2}-\d{2})',
                  r'(\d{2}-\d{2})~(\d{2}-\d{2})']:
            ms = re.findall(p, content)
            if ms:
                return f"{ms[-1][0]} ~ {ms[-1][1]}"
        today = datetime.now()
        mon = today - timedelta(days=today.weekday())
        return f"{mon:%m-%d} ~ {(mon + timedelta(6)):%m-%d}"

    def _report_paths(self):
        c = self.cfg_mgr.load()
        d = self.cfg_mgr.get_save_location()
        cn = c.get("user_name_cn", "用户")
        en = c.get("user_name_en", "User")
        return (d / f"周报-{cn}.md",
                d / f"WeeklySummary_{en.replace(' ', '')}.md", cn, en)

    # ── generation ─────────────────────────────────────────
    def _start_gen(self):
        if self.is_running:
            return
        self.is_running = True
        self.gen_btn.configure(state="disabled", fg_color="gray")
        self.nw_btn.pack_forget()
        self.progress.configure(mode="indeterminate")
        self.progress.start()
        threading.Thread(target=self._gen, daemon=True).start()

    def _gen(self):
        try:
            cn_r, en_r, cn_n, en_n = self._report_paths()

            self.after(0, lambda: self._set_status("检查配置…", WARNING))
            key = self.cfg_mgr.get_api_key()
            if not key:
                raise RuntimeError("未配置 API Key")

            self._init_file(cn_r, f"# 周报 - {cn_n}")
            self._init_file(en_r, f"# Weekly Summary - {en_n}")

            self.after(0, lambda: self._set_status("正在生成中文总结…", BLUE))
            sm = BilingualSummarizer(key)
            cn_sum = sm.summarize_chinese(self.notes_content)

            self.after(0, lambda: self._set_status("正在生成英文总结…", BLUE))
            en_sum = sm.summarize_english(self.notes_content)

            self.after(0, lambda: self._set_status("写入文件…", WARNING))
            self._write_report(cn_r, cn_sum, self.week_range, True)
            self._write_report(en_r, en_sum, self.week_range, False)

            p = cn_r
            self.after(0, lambda: self._ok(p))
        except Exception as e:
            msg = str(e)
            self.after(0, lambda: self._fail(msg))

    def _init_file(self, path, header):
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"{header}\n\n> 创建时间: {datetime.now():%Y-%m-%d}\n\n---\n")

    def _write_report(self, path, content, wr, is_cn):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        if is_cn:
            sec = f"## \U0001f4c5 {wr} 周报\n\n> 生成时间: {ts}\n\n{content}\n"
            pat = rf"## \U0001f4c5 {re.escape(wr)} 周报\n.*?(?=\n---\n|$)"
        else:
            sec = f"## \U0001f4c5 {wr} Weekly Report\n\n> Generated: {ts}\n\n{content}\n"
            pat = (rf"## \U0001f4c5 (?:Week of )?{re.escape(wr)}"
                   rf"(?: Weekly Report)?\n.*?(?=\n---\n|$)")
        with open(path, "r", encoding="utf-8") as f:
            old = f.read()
        if re.search(pat, old, re.DOTALL):
            new = re.sub(pat, sec.rstrip(), old, flags=re.DOTALL)
        else:
            m = re.search(r'^(.*?---)\n', old, re.DOTALL)
            if m:
                insert_pos = m.end()
                new = old[:insert_pos] + "\n\n" + sec + "\n---" + old[insert_pos:]
            else:
                new = old + "\n\n---\n\n" + sec
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)

    def _ok(self, path):
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress.set(1.0)
        self._set_status(f"✅ 完成！已保存到 {path.parent.name}/", SUCCESS)
        self.gen_btn.configure(fg_color=SUCCESS, hover_color=SUCCESS,
                               text="✓ 完成")
        self.nw_btn.pack(pady=(10, 0))
        self.is_running = False

    def _fail(self, msg):
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress.set(0)
        self._set_status(f"失败: {msg}", ERROR)
        self.gen_btn.configure(state="normal", fg_color=BLUE,
                               hover_color=BLUE_HVR, text="重试")
        self.is_running = False

    # ── next week ──────────────────────────────────────────
    def _next_week(self):
        try:
            mt = re.match(r'(\d{2})-(\d{2})\s*~\s*(\d{2})-(\d{2})',
                          self.week_range)
            if mt:
                y = datetime.now().year
                try:
                    end = datetime(y, int(mt.group(3)), int(mt.group(4)))
                except ValueError:
                    end = datetime.now()
                ns = end + timedelta(days=1)
                ne = ns + timedelta(days=6)
            else:
                today = datetime.now()
                ns = today + timedelta(days=(7 - today.weekday()))
                ne = ns + timedelta(days=6)

            nr = f"{ns:%m-%d} ~ {ne:%m-%d}"
            reader = StickyNotesReader()
            notes = list(self.target_notes.items())

            close_sticky_notes()

            cnt = 0
            for _, note in notes:
                name = re.sub(r'\d{2}-\d{2}\s*[~～]\s*\d{2}-\d{2}.*', '',
                              note["title"]).strip() or note["title"]
                reader.append_to_note(note["id"], f"\\b {name}\\b0\n\\b {nr}\\b0\n ")
                cnt += 1

            open_sticky_notes()

            self._set_status(
                f"✅ 已初始化 {cnt} 个便笺 → {nr}", SUCCESS)
            self.nw_btn.configure(state="disabled", text="✓ 已初始化")
        except Exception as e:
            self._set_status(f"初始化失败: {e}", ERROR)


def main():
    app = WeeklySummaryApp()
    app.mainloop()


if __name__ == "__main__":
    main()
