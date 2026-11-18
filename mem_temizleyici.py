import ctypes
import os
import sys
import threading
import time
import winreg
import tkinter as tk
from tkinter import ttk, messagebox

import psutil
import pystray
from PIL import Image, ImageDraw


PROTECTED = {
    "system", "registry", "smss", "csrss", "wininit", "services", "lsass",
    "winlogon", "dwm", "svchost", "memory compression", "secure system",
    "avp", "avpui", "chrome", "chatgpt", "codex", "explorer",
}

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_SET_QUOTA = 0x0100

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
psapi.EmptyWorkingSet.restype = ctypes.c_int
ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
ntdll.NtSetSystemInformation.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
ntdll.NtSetSystemInformation.restype = ctypes.c_long


def native_memory_cleanup():
    """Use the same documented command family used by Mem Reduct's Native API path.

    Commands: empty working sets, flush modified list, purge standby lists and
    purge low-priority standby lists. Windows may require administrator rights.
    """
    commands = (2, 3, 4, 5)
    success = 0
    for command in commands:
        value = ctypes.c_int(command)
        status = ntdll.NtSetSystemInformation(80, ctypes.byref(value), ctypes.sizeof(value))
        if status >= 0:
            success += 1
    return success


def fmt_mb(value):
    return f"{value / 1024 / 1024:,.0f} MB"


def memory_snapshot():
    vm = psutil.virtual_memory()
    return vm.total, vm.used, vm.available


def clean_working_sets():
    cleaned = 0
    before = sum(p.memory_info().rss for p in psutil.process_iter(["name"]) if p.info["name"])
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info["name"] or "").lower()
            if proc.info["pid"] == os.getpid() or name in PROTECTED:
                continue
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA, False, proc.info["pid"]
            )
            if handle:
                if psapi.EmptyWorkingSet(handle):
                    cleaned += 1
                kernel32.CloseHandle(handle)
        except (psutil.Error, OSError):
            continue
    after = sum(p.memory_info().rss for p in psutil.process_iter(["name"]) if p.info["name"])
    native = native_memory_cleanup()
    return cleaned, max(0, before - after), native


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("RAM Temizleyici")
        self.geometry("338x436")
        self.resizable(False, False)
        self.configure(bg="#171717")
        self.auto_enabled = tk.BooleanVar(value=False)
        self.threshold_gb = tk.DoubleVar(value=24.0)
        self.interval_enabled = tk.BooleanVar(value=False)
        self.interval_minutes = tk.IntVar(value=30)
        self.start_with_windows = tk.BooleanVar(value=False)
        self.start_minimized = tk.BooleanVar(value=False)
        self.always_on_top = tk.BooleanVar(value=False)
        self.region_working = tk.BooleanVar(value=True)
        self.region_standby = tk.BooleanVar(value=True)
        self.region_modified = tk.BooleanVar(value=True)
        self.region_low_priority = tk.BooleanVar(value=True)
        self.clean_lock = threading.Lock()
        self.auto_armed = True
        self.last_clean_time = time.monotonic()
        self.tray = None

        self.bg = "#171717"
        self.fg = "#eeeeee"
        self.muted = "#c7c7c7"
        self.bar = "#282828"
        self.accent = "#67b7ff"

        menubar = tk.Menu(self, tearoff=False, bg="#202020", fg=self.fg,
                          activebackground="#3b3b3b", activeforeground="white")
        file_menu = tk.Menu(menubar, tearoff=False, bg="#202020", fg=self.fg)
        file_menu.add_command(label="Ayarlar...", command=self.show_settings)
        file_menu.add_separator()
        file_menu.add_command(label="Çıkış", command=self.exit_app)
        view_menu = tk.Menu(menubar, tearoff=False, bg="#202020", fg=self.fg)
        view_menu.add_checkbutton(label="Her zaman üstte", variable=self.always_on_top, command=self.apply_view)
        view_menu.add_command(label="Sistem tepsisine küçült", command=self.hide_to_tray)
        settings_menu = tk.Menu(menubar, tearoff=False, bg="#202020", fg=self.fg)
        settings_menu.add_command(label="Genel ve otomatik temizlik...", command=self.show_settings)
        help_menu = tk.Menu(menubar, tearoff=False, bg="#202020", fg=self.fg)
        help_menu.add_command(label="Güncellemeleri kontrol et", command=lambda: self.status.configure(text="Güncelleme kontrolü henüz yapılandırılmadı"))
        help_menu.add_command(label="Hakkında", command=self.show_about)
        menubar.add_cascade(label="Dosya", menu=file_menu)
        menubar.add_cascade(label="Görünüm", menu=view_menu)
        menubar.add_cascade(label="Ayarlar", menu=settings_menu)
        menubar.add_cascade(label="Yardım", menu=help_menu)
        self.config(menu=menubar)

        tk.Label(self, text="RAM Temizleyici", bg=self.bg, fg=self.fg,
                 font=("Segoe UI", 10)).pack(anchor="w", padx=10, pady=(6, 3))
        self.physical = self.section("Fiziksel bellek")
        self.virtual = self.section("Sanal bellek")
        self.system = self.section("Sistem çalışma seti")

        bottom = tk.Frame(self, bg=self.bg)
        bottom.pack(side="bottom", fill="x", padx=12, pady=10)
        self.status = tk.Label(bottom, text="Hazır", bg=self.bg, fg=self.accent, font=("Segoe UI", 8),
                               anchor="w", justify="left", wraplength=300)
        self.status.pack(fill="x", pady=(0, 5))
        clean_menu = tk.Menu(self, tearoff=False, bg="#262626", fg=self.fg)
        clean_menu.add_command(label="Tüm bellek listelerini temizle", command=self.start_clean)
        clean_menu.add_command(label="Sadece işlem çalışma setlerini temizle", command=self.start_clean)
        self.clean_button = tk.Button(bottom, text="Belleği temizle  ▾", bg="#292929", fg=self.fg,
                                      activebackground="#3b3b3b", activeforeground="white", relief="flat",
                                      command=lambda: clean_menu.tk_popup(self.clean_button.winfo_rootx(), self.clean_button.winfo_rooty()-55))
        self.clean_button.pack(anchor="e")
        self.option_frame = None
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.bind("<F2>", lambda event: self.show_settings())
        self.create_tray()
        self.refresh()
        self.auto_check()

    def refresh(self):
        total, used, available = memory_snapshot()
        swap = psutil.swap_memory()
        process_ws = sum((p.info["memory_info"].rss for p in psutil.process_iter(["memory_info"])
                          if p.info.get("memory_info")), 0)
        self.update_section(self.physical, used, available, total)
        self.update_section(self.virtual, swap.used, swap.free, swap.total)
        self.update_section(self.system, process_ws, max(0, total - process_ws), total)
        self.after(2000, self.refresh)

    def section(self, title):
        frame = tk.Frame(self, bg=self.bg, highlightthickness=1, highlightbackground="#424242")
        frame.pack(fill="x", padx=12, pady=(0, 6))
        tk.Label(frame, text=title, bg=self.bar, fg=self.fg, anchor="w", font=("Segoe UI", 8)).pack(fill="x")
        values = tk.Frame(frame, bg=self.bg)
        values.pack(fill="x", pady=4)
        chart = tk.Canvas(values, width=58, height=58, bg=self.bg, highlightthickness=0)
        chart.pack(side="left", padx=(8, 4))
        labels = {}
        for key, text in (("used", "Kullanım"), ("free", "Boşta"), ("total", "Toplam")):
            labels[key] = tk.Label(values, text=f"{text}: -", bg=self.bg, fg=self.fg, anchor="e", font=("Segoe UI", 8))
            labels[key].pack(anchor="e", padx=10)
        labels["chart"] = chart
        return labels

    def update_section(self, section, used, free, total):
        pct = (used / total * 100) if total else 0
        section["used"].configure(text=f"Kullanım    {pct:,.3f}%   {fmt_mb(used)}")
        section["free"].configure(text=f"Boşta       {fmt_mb(free)}")
        section["total"].configure(text=f"Toplam      {fmt_mb(total)}")
        chart = section["chart"]
        chart.delete("all")
        pad = 5
        extent = max(0.1, min(359.9, pct * 3.6))
        chart.create_arc(pad, pad, 58-pad, 58-pad, start=90, extent=-extent,
                         fill="#4aa3df", outline="#4aa3df")
        chart.create_arc(pad, pad, 58-pad, 58-pad, start=90-extent, extent=-(360-extent),
                         fill="#3a3a3a", outline="#3a3a3a")
        chart.create_oval(18, 18, 40, 40, fill=self.bg, outline=self.bg)
        chart.create_text(29, 29, text=f"{pct:.0f}%", fill=self.fg, font=("Segoe UI", 7, "bold"))

    def show_settings(self):
        if self.option_frame is not None and self.option_frame.winfo_exists():
            self.option_frame.destroy()
        self.option_frame = tk.Frame(self, bg=self.bg, highlightthickness=1, highlightbackground="#555555")
        self.option_frame.place(x=5, y=58, relwidth=0.97, relheight=0.80)
        header = tk.Frame(self.option_frame, bg=self.bar)
        header.pack(fill="x")
        tk.Label(header, text="Ayarlar", bg=self.bar, fg=self.fg, font=("Segoe UI", 9, "bold")).pack(side="left", padx=8, pady=5)
        tk.Button(header, text="Geri", command=self.option_frame.destroy, bg=self.bar, fg=self.fg,
                  activebackground="#444444", activeforeground="white", relief="flat").pack(side="right", padx=4)
        notebook = ttk.Notebook(self.option_frame)
        notebook.pack(fill="both", expand=True, padx=4, pady=4)
        general = tk.Frame(notebook, bg=self.bg)
        cleaning = tk.Frame(notebook, bg=self.bg)
        appearance = tk.Frame(notebook, bg=self.bg)
        tray = tk.Frame(notebook, bg=self.bg)
        notebook.add(general, text="Genel")
        notebook.add(cleaning, text="Bellek temizleme")
        notebook.add(appearance, text="Görünüm")
        notebook.add(tray, text="Tepsi simgesi")
        self.check(general, "Windows ile birlikte başlat", self.start_with_windows, 0, self.apply_startup)
        self.check(general, "Başlangıçta tepsiye küçült", self.start_minimized, 1)
        self.check(general, "Otomatik temizlemeyi etkinleştir", self.auto_enabled, 2)
        tk.Label(cleaning, text="Temizlenecek bellek bölgeleri", bg=self.bg, fg=self.fg).pack(anchor="w", padx=12, pady=8)
        self.check(cleaning, "İşlem çalışma kümeleri", self.region_working, 1)
        self.check(cleaning, "Bekleyen sayfa listeleri", self.region_standby, 2)
        self.check(cleaning, "Değiştirilmiş sayfa listeleri", self.region_modified, 3)
        self.check(cleaning, "Düşük öncelikli bekleyen liste", self.region_low_priority, 4)
        tk.Label(cleaning, text="Kullanılan RAM eşiği (GB):", bg=self.bg, fg=self.fg).pack(anchor="w", padx=12, pady=(12, 0))
        ttk.Spinbox(cleaning, from_=1, to=64, increment=0.5, width=8, textvariable=self.threshold_gb).pack(anchor="w", padx=12)
        self.check(cleaning, "Belirli aralıklarla temizle", self.interval_enabled, 6)
        tk.Label(cleaning, text="Aralık (dakika):", bg=self.bg, fg=self.fg).pack(anchor="w", padx=12)
        ttk.Spinbox(cleaning, from_=1, to=1440, width=8, textvariable=self.interval_minutes).pack(anchor="w", padx=12)
        self.check(appearance, "Ana pencereyi her zaman üstte tut", self.always_on_top, 0, self.apply_view)
        tk.Label(appearance, text="Koyu tema etkin", bg=self.bg, fg=self.fg).pack(anchor="w", padx=12, pady=10)
        tk.Label(tray, text="Tepsi simgesi otomatik temizleme durumunu gösterir.", bg=self.bg, fg=self.fg, wraplength=360).pack(anchor="w", padx=12, pady=12)

    def check(self, parent, text, variable, row, command=None):
        cb = tk.Checkbutton(parent, text=text, variable=variable, command=command, bg=self.bg, fg=self.fg,
                            selectcolor="#333333", activebackground=self.bg, activeforeground=self.fg)
        cb.pack(anchor="w", padx=12, pady=3)

    def apply_view(self):
        self.attributes("-topmost", bool(self.always_on_top.get()))

    def apply_startup(self):
        name = "RAMTemizleyici"
        command = f'"{sys.executable}" --tray'
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
            if self.start_with_windows.get():
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
            self.status.configure(text="Başlangıç ayarı kaydedildi")
        except OSError as exc:
            self.status.configure(text=f"Başlangıç ayarı kaydedilemedi: {exc}")

    def show_about(self):
        messagebox.showinfo("Hakkında", "RAM Temizleyici\nMem Reduct benzeri Windows bellek aracı\nSürüm 1.0")

    def auto_check(self):
        if self.auto_enabled.get() or self.interval_enabled.get():
            total, used, _ = memory_snapshot()
            threshold = max(1.0, float(self.threshold_gb.get())) * 1024 * 1024 * 1024
            interval_due = self.interval_enabled.get() and (time.monotonic() - self.last_clean_time >= max(1, self.interval_minutes.get()) * 60)
            if ((self.auto_enabled.get() and used >= threshold and self.auto_armed) or interval_due):
                self.auto_armed = False
                self.start_clean(automatic=True)
                self.last_clean_time = time.monotonic()
            elif used < threshold * 0.90:
                self.auto_armed = True
        self.after(5000, self.auto_check)

    def start_clean(self, automatic=False):
        if self.clean_lock.locked():
            return
        self.status.configure(text="Otomatik temizleniyor..." if automatic else "Temizleniyor...")
        threading.Thread(target=self.clean, args=(automatic,), daemon=True).start()

    def clean(self, automatic=False):
        with self.clean_lock:
            count, released, native = clean_working_sets()
        suffix = "sistem listeleri temizlendi" if native else "yönetici izni olmadan güvenli mod"
        prefix = "Otomatik temizlik tamamlandı" if automatic else "Tamamlandı"
        self.last_clean_time = time.monotonic()
        self.after(0, lambda: self.status.configure(text=f"{prefix}: {count} işlem optimize edildi, yaklaşık {fmt_mb(released)} boşaltıldı ({suffix})"))

    def create_tray(self):
        image = Image.new("RGB", (64, 64), "#2563eb")
        draw = ImageDraw.Draw(image)
        draw.rectangle((14, 14, 50, 50), outline="white", width=4)
        menu = pystray.Menu(
            pystray.MenuItem("RAM Temizleyici'yi göster", self.show_from_tray),
            pystray.MenuItem("Şimdi temizle", lambda icon, item: self.start_clean()),
            pystray.MenuItem("Çıkış", self.exit_app),
        )
        self.tray = pystray.Icon("RAMTemizleyici", image, "RAM Temizleyici", menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    def hide_to_tray(self):
        self.withdraw()

    def show_from_tray(self, icon=None, item=None):
        self.after(0, self.deiconify)

    def exit_app(self, icon=None, item=None):
        if self.tray:
            self.tray.stop()
        self.after(0, self.destroy)


if __name__ == "__main__":
    application = App()
    if "--tray" in sys.argv:
        application.after(300, application.hide_to_tray)
    application.mainloop()
