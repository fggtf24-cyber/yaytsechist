"""Яйцечист — оптимизация как в AntiLag (github.com/AmbitiousPilots/AntiLag),
очистка диска как в Razer Cortex System Booster, очистка памяти как в Mem Reduct.

    python gameboost.py             — окно программы (сама запросит права администратора)
    python gameboost.py --scan      — только посчитать мусор, ничего не удаляя
    python gameboost.py --selftest  — самопроверка
    python gameboost.py --update    — обновиться с GitHub без окна
    python gameboost.py --agent     — фоновый агент (таймер 0.5 мс + автоочистка RAM), запускается планировщиком
"""
import csv, ctypes, fnmatch, glob, hashlib, heapq, json, os, re, shutil, stat, subprocess, sys, tempfile, threading, time, urllib.request, winreg
from array import array
from ctypes import byref, sizeof, wintypes
from pathlib import Path

STATE = Path(os.environ["LOCALAPPDATA"]) / "GameBoost" / "state.json"
AUTO_LOG = STATE.with_name("auto.json")  # когда агент чистил в последний раз; отдельно, чтобы не спорить с окном за state.json
FROZEN = getattr(sys, "frozen", False)  # запущено как Яйцечист.exe (PyInstaller)
ASSETS = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
EXE = sys.executable if FROZEN else sys.executable.replace("python.exe", "pythonw.exe")
SCRIPT = "" if FROZEN else f'"{os.path.abspath(__file__)}" '  # аргументы перед флагами
AGENT_ARGS = SCRIPT + "--agent"
GUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", re.I)
HIVES = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}

# Всё ниже — ровно то, что делает 68WAntiLagApp.exe v0.0.3α (сверено по декомпилированному коду).
# План: копия активного плана, переименованная, с этими значениями от сети и от батареи.
POWER_TWEAKS = [
    ("SUB_PROCESSOR", "CPMINCORES", "100"),  # без парковки ядер
    ("SUB_PROCESSOR", "CPMINCORES1", "100"),  # то же для E-ядер
    ("2a737441-1930-4402-8d77-b2bebba308a3", "48e6b7a6-50f5-4782-a5d4-53bb8f07e226", "0"),  # USB не засыпает
    ("0d7dbae2-4294-402a-ba8e-26777e8488cd", "309dce9b-bef4-4119-9921-a851fb12f0f4", "1"),  # слайд-шоу обоев на паузе
    ("02f815b5-a5cf-4c84-bf20-649d1f75d3d8", "4c793e7d-a264-42e1-87d3-7a0d2f523ccd", "1"),  # таймер JavaScript: макс.
    ("SUB_SLEEP", "STANDBYIDLE", "0"),  # не уходить в сон
    ("19cbb8fa-5279-450e-9fac-8a3d5fedd0c1", "12bbebe6-58d6-4636-95bb-3217ef867c1a", "0"),  # Wi-Fi без экономии
    ("SUB_PROCESSOR", "PERFBOOSTMODE", "2"),  # турбобуст: агрессивный
    ("SUB_PROCESSOR", "PROCTHROTTLEMIN", "100"),  # процессор не сбрасывает частоту
    ("SUB_PROCESSOR", "PROCTHROTTLEMAX", "100"),
    ("SUB_PROCESSOR", "PROCTHROTTLEMIN1", "100"),  # то же для E-ядер
    ("SUB_PROCESSOR", "PROCTHROTTLEMAX1", "100"),
    ("SUB_PROCESSOR", "75b0ae3f-bce0-45a7-8c89-c9611c25e100", "0"),  # макс. частота: без ограничения
    ("SUB_PROCESSOR", "DISTRIBUTEUTIL", "0"),
    ("SUB_PCIEXPRESS", "ASPM", "0"),  # PCIe без энергосбережения
    ("SUB_DISK", "DISKIDLE", "0"),  # диск не отключается
    ("SUB_PROCESSOR", "SYSCOOLPOL", "1"),  # активное охлаждение
    ("SUB_NONE", "245d8541-3943-4422-b025-13a784f679b7", "1"),  # тип схемы: производительность
]
UNHIDE = ("CPMINCORES", "75b0ae3f-bce0-45a7-8c89-c9611c25e100", "PERFBOOSTMODE")  # показать в панели питания
REG_TWEAKS = [
    ("HKLM", r"SYSTEM\CurrentControlSet\Control\PriorityControl", "Win32PrioritySeparation", 42),
    ("HKLM", r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "HwSchMode", 2),  # аппаратное планирование GPU
    ("HKCU", r"Software\Microsoft\GameBar", "AllowAutoGameMode", 0),  # игровой режим Windows — выкл.
    ("HKCU", r"Software\Microsoft\GameBar", "AutoGameModeEnabled", 0),
    ("HKCU", r"System\GameConfigStore", "GameDVR_Enabled", 0),  # Game DVR — выкл.
    ("HKCU", r"System\GameConfigStore", "GameDVR_FSEBehaviorMode", 2),
    ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\GameDVR", "AppCaptureEnabled", 0),
    ("HKLM", r"SOFTWARE\Microsoft\PolicyManager\default\ApplicationManagement\AllowGameDVR", "value", 0),
    ("HKLM", r"SOFTWARE\Microsoft\PolicyManager\current\device\ApplicationManagement", "AllowGameDVR", 0),
    # Сверх AntiLag: с Win10 2004 таймер не глобальный — этим ключом Win11 снова применяет его ко всем процессам
    ("HKLM", r"SYSTEM\CurrentControlSet\Control\Session Manager\kernel", "GlobalTimerResolutionRequests", 1),
]
BCD = {"useplatformtick": "yes", "disabledynamictick": "yes", "useplatformclock": None}  # None — удалить

ntdll, k32 = ctypes.WinDLL("ntdll"), ctypes.WinDLL("kernel32")
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
k32.FlushFileBuffers.argtypes = k32.CloseHandle.argtypes = (wintypes.HANDLE,)
k32.GetCurrentProcess.restype = wintypes.HANDLE
k32.SetProcessInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)


def run(*args):
    r = subprocess.run(args, capture_output=True, text=True, encoding="oem", errors="replace",
                       creationflags=subprocess.CREATE_NO_WINDOW)
    return r.returncode, r.stdout + r.stderr


def ps(cmd):
    return run("powershell", "-NoProfile", "-NonInteractive", "-Command", cmd)


def guid(*args):
    code, out = run(*args)
    m = GUID.search(out)
    return m.group(0) if code == 0 and m else None


def reg_get(hive, path, name):
    try:
        with winreg.OpenKey(HIVES[hive], path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def reg_set(hive, path, name, value):
    """value=None — удалить параметр; int — DWORD; str — строка."""
    access = winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY
    try:
        if value is None:
            with winreg.OpenKey(HIVES[hive], path, 0, access) as k:
                winreg.DeleteValue(k, name)
        else:
            kind = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
            with winreg.CreateKeyEx(HIVES[hive], path, 0, access) as k:
                winreg.SetValueEx(k, name, 0, kind, value)
    except FileNotFoundError:
        pass


def parse_bcd(text):
    """{параметр: 'yes'/'no'} из вывода `bcdedit /enum {current}` (только наши параметры)."""
    found = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower() in BCD:
            found[parts[0].lower()] = "yes" if parts[1].lower() in ("yes", "да") else "no"
    return found


def bcd_apply(values):
    for name in BCD:
        if values.get(name):
            run("bcdedit", "/set", name, values[name])
        else:
            run("bcdedit", "/deletevalue", name)


# ---------- оптимизация (как AntiLag) ----------

class PowerThrottling(ctypes.Structure):
    _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]


def set_timer(on):
    """NtSetTimerResolution: 5000 * 100 нс = 0.5 мс. Держится, пока жив процесс."""
    if on:
        # Win11 игнорирует таймер невидимых процессов (наш агент в трее как раз такой).
        # Отказ от этого по документации SetProcessInformation: IGNORE_TIMER_RESOLUTION (4) = выкл.
        pt = PowerThrottling(1, 4, 0)
        k32.SetProcessInformation(k32.GetCurrentProcess(), 4, byref(pt), sizeof(pt))  # ProcessPowerThrottling
    cur = wintypes.ULONG()
    ntdll.NtSetTimerResolution(5000, on, byref(cur))
    return cur.value / 10000


def query_timer():
    lo, hi, cur = wintypes.ULONG(), wintypes.ULONG(), wintypes.ULONG()
    ntdll.NtQueryTimerResolution(byref(lo), byref(hi), byref(cur))
    return cur.value / 10000


def plan_name():
    m = re.search(r"\((.+)\)\s*$", run("powercfg", "/getactivescheme")[1].strip())
    return m.group(1) if m else "—"


def enable():
    """Применяет всё, что делает AntiLag, и запоминает прежние значения для отката."""
    st = {}
    orig = guid("powercfg", "/getactivescheme")
    new = orig and guid("powercfg", "-duplicatescheme", orig)
    if new:
        run("powercfg", "-changename", new, "Яйцечист", "Копия плана AntiLag Supreme Performance")
        for s in UNHIDE:
            run("powercfg", "-attributes", "SUB_PROCESSOR", s, "-ATTRIB_HIDE")
        for sub, setting, value in POWER_TWEAKS:
            for mode in ("/setacvalueindex", "/setdcvalueindex"):
                run("powercfg", mode, new, sub, setting, value)
        run("powercfg", "/setactive", new)
        st["power"] = [orig, new]
    # AntiLag бэкапит часть этих значений не из того ключа реестра — у нас каждое берётся из своего
    st["reg"] = [[h, p, n, reg_get(h, p, n)] for h, p, n, _ in REG_TWEAKS]
    for h, p, n, v in REG_TWEAKS:
        reg_set(h, p, n, v)
    code, out = run("bcdedit", "/enum", "{current}")
    if code == 0:
        st["bcd"] = parse_bcd(out)
        bcd_apply(BCD)
    return st


def disable(st):
    if p := st.get("power"):
        run("powercfg", "/setactive", p[0])
        run("powercfg", "-delete", p[1])
        for s in UNHIDE:
            run("powercfg", "-attributes", "SUB_PROCESSOR", s, "+ATTRIB_HIDE")
    for h, p, n, prev in st.get("reg", []):
        reg_set(h, p, n, prev)
    if "bcd" in st:
        bcd_apply(st["bcd"])


def wanted(st):
    """Нужен ли фоновый агент. tray_on — держать его всегда, даже без оптимизации и автоочистки."""
    return bool(st["applied"] or st["auto_on"] or st["int_on"] or st["tray_on"])


def sync_agent(st):
    """Фоновый агент живёт в Планировщике: старт при входе, с правами админа, без лимита времени."""
    if wanted(st):
        a, exe = AGENT_ARGS.replace("'", "''"), EXE.replace("'", "''")
        ps(f"$t=Get-ScheduledTask GameBoost -EA 0; if(-not $t -or $t.Actions[0].Execute -ne '{exe}' -or $t.Actions[0].Arguments -ne '{a}'){{"
           "$s=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0;"
           f"Register-ScheduledTask GameBoost -Action (New-ScheduledTaskAction -Execute '{exe}' -Argument '{a}') "
           "-Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) -Settings $s -RunLevel Highest -Force|Out-Null};"
           "Start-ScheduledTask GameBoost")
    else:
        ps("Unregister-ScheduledTask GameBoost -Confirm:$false -EA 0")  # сам агент завершится, увидев state.json


def agent_running():
    return ps("(Get-ScheduledTask GameBoost -EA 0).State")[1].strip() == "Running"


def open_gui():
    subprocess.Popen(f'"{EXE}" {SCRIPT}', creationflags=subprocess.DETACHED_PROCESS)


def exit_label():
    """Таймер 0.5 мс живёт, пока жив агент: только при включённой оптимизации выход меняет отклик."""
    return tr("Выход (таймер 0.5 мс отключится)", "Exit (0.5 ms timer will stop)") if load()["applied"] else tr("Выход", "Exit")


def auto_clean(mask):
    AUTO_LOG.write_text(json.dumps({"time": time.time(), "freed": clean_ram(mask)}), "utf-8")


def kill_other_agents():
    """Exe из PyInstaller — это два процесса: оболочка и сама программа. Планировщик, останавливая задачу, завершает
    только оболочку, а программа живёт дальше со своим значком в трее. Поэтому новый агент сам убирает прежние."""
    if FROZEN:
        name = os.path.basename(sys.executable).replace("'", "''")
        ps(f"Get-CimInstance Win32_Process|?{{$_.Name -like '{name}*' -and $_.CommandLine -like '*--agent*' -and "
           f"$_.ProcessId -notin {os.getpid()},{os.getppid()}}}|%{{Stop-Process -Id $_.ProcessId -Force -EA 0}}")


def agent():
    """Фон: держит таймер, чистит RAM по условиям, показывает значок в трее (как AntiLag)."""
    import pystray
    from PIL import Image

    kill_other_agents()
    menu = pystray.Menu(
        pystray.MenuItem(lambda item: tr("Открыть Яйцечист", "Open Yaytsechist"), lambda: open_gui(), default=True),
        pystray.MenuItem(lambda item: tr("Очистить память", "Clean memory"), lambda: clean_ram(load()["ram_mask"])),
        pystray.MenuItem(lambda item: exit_label(), lambda icon: icon.stop()))
    icon = pystray.Icon("GameBoost", Image.open(ASSETS / "icon.ico"), "Яйцечист", menu)

    def loop(icon):
        icon.visible = True
        st, timer, last, last_int = load(), False, 0.0, time.time()
        while wanted(st):
            if bool(st["applied"]) != timer:
                timer = bool(st["applied"])
                set_timer(timer)
                icon.update_menu()
            now, m = time.time(), mem()
            mask = st["ram_mask"] & ~FREEZES  # как Mem Reduct: авто — без «подвисающих» областей
            if st["auto_on"] and m["load"] >= st["auto_pct"] and now - last >= 30:
                auto_clean(mask)
                last = now
            if st["int_on"] and now - last_int >= st["int_min"] * 60:
                auto_clean(mask)
                last = last_int = now
            icon.title = tr(f"Яйцечист · таймер {query_timer():.2f} мс · RAM {m['load']}%", f"Yaytsechist · timer {query_timer():.2f} ms · RAM {m['load']}%")
            time.sleep(5)
            st = load(st)
        icon.stop()

    icon.run(setup=loop)  # loop стартует, когда значок готов, — иначе ранний stop() вешает run()


# ---------- очистка памяти (как Mem Reduct) ----------

# (флаг, название, пометка) — флаги и порядок как в Mem Reduct
RAM_AREAS = [
    (0x01, "Память запущенных программ", "рабочие наборы"),
    (0x02, "Файловый кэш системы", "безопасно"),
    (0x80, "Записать кэш файлов на диск", "безопасно"),
    (0x10, "Изменённые страницы", "может подвисать"),
    (0x08, "Резервная память (standby)", "может подвисать"),
    (0x04, "Резервная память, низкий приоритет", "безопасно"),
    (0x40, "Кэш реестра", "Windows 8.1+"),
    (0x20, "Объединение одинаковых страниц", "Windows 10+"),
]
RAM_DEFAULT, FREEZES = 0xE7, 0x18  # REDUCT_MASK_DEFAULT и REDUCT_MASK_FREEZES из Mem Reduct


class MemStatus(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD)] + \
               [(n, ctypes.c_ulonglong) for n in ("TotalPhys", "AvailPhys", "TotalPageFile", "AvailPageFile",
                                                  "TotalVirtual", "AvailVirtual", "AvailExtendedVirtual")]


class PerfInfo(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD)] + [(n, ctypes.c_size_t) for n in (
        "CommitTotal", "CommitLimit", "CommitPeak", "PhysicalTotal", "PhysicalAvailable", "SystemCache",
        "KernelTotal", "KernelPaged", "KernelNonpaged", "PageSize")] + \
        [(n, wintypes.DWORD) for n in ("HandleCount", "ProcessCount", "ThreadCount")]


class FileCacheInfo(ctypes.Structure):
    _fields_ = [("CurrentSize", ctypes.c_size_t), ("PeakSize", ctypes.c_size_t), ("PageFaultCount", ctypes.c_ulong),
                ("MinimumWorkingSet", ctypes.c_size_t), ("MaximumWorkingSet", ctypes.c_size_t),
                ("CurrentSizeIncludingTransitionInPages", ctypes.c_size_t),
                ("PeakSizeIncludingTransitionInPages", ctypes.c_size_t),
                ("TransitionRePurposeCount", ctypes.c_ulong), ("Flags", ctypes.c_ulong)]


class CombineInfo(ctypes.Structure):
    _fields_ = [("Handle", wintypes.HANDLE), ("PagesCombined", ctypes.c_size_t), ("Flags", ctypes.c_ulong)]


def mem():
    m, p = MemStatus(sizeof(MemStatus)), PerfInfo(sizeof(PerfInfo))
    k32.GlobalMemoryStatusEx(byref(m))
    k32.K32GetPerformanceInfo(byref(p), p.cb)
    return {"load": m.dwMemoryLoad, "used": m.TotalPhys - m.AvailPhys, "total": m.TotalPhys,
            "virt": round(100 * p.CommitTotal / p.CommitLimit), "cache": p.SystemCache * p.PageSize}


def flush_volumes():
    drives = k32.GetLogicalDrives()
    for i in range(26):
        d = chr(65 + i)
        if drives >> i & 1 and k32.GetDriveTypeW(f"{d}:\\") == 3:  # DRIVE_FIXED
            h = k32.CreateFileW(f"\\\\.\\{d}:", 0xC0000000, 3, None, 3, 0, None)
            if h and h != ctypes.c_void_p(-1).value:
                k32.FlushFileBuffers(h)
                k32.CloseHandle(h)


def clean_ram(mask):
    """Те же вызовы NtSetSystemInformation, что в Mem Reduct. Возвращает освобождённые байты."""
    before, was = mem()["used"], ctypes.c_bool()
    for priv in (13, 5):  # SeProfileSingleProcess, SeIncreaseQuota
        ntdll.RtlAdjustPrivilege(priv, True, False, byref(was))

    def mem_list(cmd):  # SystemMemoryListInformation
        c = ctypes.c_int(cmd)
        ntdll.NtSetSystemInformation(80, byref(c), sizeof(c))

    if mask & 0x01:
        mem_list(2)  # MemoryEmptyWorkingSets
    if mask & 0x02:
        fc = FileCacheInfo()
        fc.MinimumWorkingSet = fc.MaximumWorkingSet = ctypes.c_size_t(-1).value
        ntdll.NtSetSystemInformation(81, byref(fc), sizeof(fc))  # SystemFileCacheInformationEx
    if mask & 0x80:
        flush_volumes()
    if mask & 0x10:
        mem_list(3)  # MemoryFlushModifiedList
    if mask & 0x08:
        mem_list(4)  # MemoryPurgeStandbyList
    if mask & 0x04:
        mem_list(5)  # MemoryPurgeLowPriorityStandbyList
    if mask & 0x40:
        ntdll.NtSetSystemInformation(155, None, 0)  # SystemRegistryReconciliationInformation
    if mask & 0x20:
        ci = CombineInfo()
        ntdll.NtSetSystemInformation(130, byref(ci), sizeof(ci))  # SystemCombinePhysicalMemoryInformation
    return max(0, before - mem()["used"])


# ---------- очистка диска (как Razer Cortex System Booster) ----------

L, R = os.environ["LOCALAPPDATA"], os.environ["APPDATA"]
W = os.environ.get("SystemRoot", r"C:\Windows")
DAY = 86400
BROWSERS = [L + r"\Google\Chrome\User Data", L + r"\Microsoft\Edge\User Data",
            L + r"\Yandex\YandexBrowser\User Data", L + r"\BraveSoftware\Brave-Browser\User Data"]
# (название, шаблоны папок, мин. возраст файла в сек., включено по умолчанию, пометка)
CATEGORIES = [
    ("Временные файлы", [os.environ["TEMP"]], DAY, True, "старше суток"),
    ("Temp Windows", [W + r"\Temp"], DAY, True, "старше суток"),
    ("Windows Update", [W + r"\SoftwareDistribution\Download"], 0, True, "кэш загрузок"),
    ("Отчёты об ошибках", [r"C:\ProgramData\Microsoft\Windows\WER\ReportArchive",
                           r"C:\ProgramData\Microsoft\Windows\WER\ReportQueue",
                           L + r"\CrashDumps", W + r"\Minidump", W + r"\LiveKernelReports"], 0, True, "и дампы"),
    ("Браузеры", [b + rf"\*\{c}" for b in BROWSERS for c in ("Cache", "Code Cache", "GPUCache")]
     + [L + r"\Mozilla\Firefox\Profiles\*\cache2", L + r"\Opera Software\Opera*\Cache"], 0, True, "закройте их"),
    ("Steam / Epic / Discord", [L + r"\Steam\htmlcache", L + r"\EpicGamesLauncher\Saved\webcache*",
                                R + r"\discord\Cache", R + r"\discord\Code Cache"], 0, True, "веб-кэш"),
    ("Кэш шейдеров", [L + r"\D3DSCache", L + r"\NVIDIA\DXCache", L + r"\NVIDIA\GLCache",
                      L + r"\AMD\DxCache", L + r"\AMD\DxcCache", L + r"\AMD\VkCache",
                      R + r"\NVIDIA\ComputeCache", r"C:\ProgramData\NVIDIA Corporation\NV_Cache"],
     0, False, "только если игры глючат"),
    ("Корзина", None, 0, False, "навсегда"),
]
# Что чистит сам AntiLag при включении (шаг «Clearing shader cache...»)
NV_CACHE = ("Кэш NVIDIA", [R + r"\NVIDIA\ComputeCache", r"C:\ProgramData\NVIDIA Corporation\NV_Cache"], 0)


# Что чистить внутри найденных мест. Только папки, которые создаёт сам Adobe: под кэш можно указать любую папку,
# например рабочую, поэтому остальное её содержимое не трогаем.
ADOBE_CACHES = ("Media Cache Files", "Media Cache", "Peak Files", "*Disk Cache*")


def ae_prefs_folders(text):
    """Папки кэша из настроек After Effects: "Folder 7" = "путь" в разделе Disk Cache Controls.
    Номер меняется от версии к версии. Длинные значения разбиты на куски с переносом строки, не-ASCII символы записаны байтами в hex."""
    found = []
    for section in re.findall(r'\["Disk Cache Controls"\](.*?)(?=^\s*\[|\Z)', text, re.M | re.S):
        for value in re.findall(r'"Folder \d+"\s*=\s*((?:"[^"]*"|[0-9A-Fa-f]{2,}|\\\s*)+)', section):
            raw = b"".join(lit.encode("utf-8") if lit else bytes.fromhex(hexa)
                           for lit, hexa in re.findall(r'"([^"]*)"|([0-9A-Fa-f]{2,})', value))
            found.append(raw.decode("utf-8", "replace"))
    return found


def cache_patterns(roots):
    found = {}
    for root in filter(None, roots):
        root = os.path.normpath(root)
        if any(fnmatch.fnmatch(os.path.basename(root).lower(), n.lower()) for n in ADOBE_CACHES):
            paths = [glob.escape(root)]  # указана сама папка кэша
        else:
            paths = [os.path.join(glob.escape(root), n) for n in ADOBE_CACHES]
        found.update((os.path.normcase(p), p) for p in paths)  # одна папка не должна посчитаться дважды
    return sorted(found.values())


def ae_cache():
    """Где кэш у этого пользователя: настройки всех версий AE, реестр Adobe и стандартная папка. None — AE не установлен."""
    prefs = glob.glob(glob.escape(R) + r"\Adobe\After Effects\*\*Prefs.txt")
    if not prefs:
        return None
    roots = [R + r"\Adobe\Common"]
    for f in prefs:
        try:
            roots += ae_prefs_folders(Path(f).read_text("utf-8", "replace"))
        except OSError:
            pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Adobe") as k:
            for name in (winreg.EnumKey(k, i) for i in range(winreg.QueryInfoKey(k)[0])):
                if name.startswith("Common"):
                    roots += [reg_get("HKCU", rf"Software\Adobe\{name}\Media Cache", v) for v in ("FolderPath", "DatabasePath")]
    except OSError:
        pass
    return cache_patterns(roots)


if ae := ae_cache():
    CATEGORIES.append(("Кэш After Effects", ae, 0, False, "закройте Adobe"))  # в конце: state.json хранит галочки по порядку


class RBInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]


def _is_link(p):
    return os.path.islink(p) or Path(p).is_junction()


def _walk(cat):
    """Обходит папки категории, НЕ заходя в симлинки/junction (чтобы не удалить чужое)."""
    for pat in cat[1]:
        for root in glob.glob(pat):
            for d, subdirs, names in os.walk(root):
                subdirs[:] = [s for s in subdirs if not _is_link(os.path.join(d, s))]
                yield root, d, names


def _old_files(cat):
    cutoff = time.time() - cat[2]
    for _, d, names in _walk(cat):
        for n in names:
            p = os.path.join(d, n)
            try:
                s = os.lstat(p)
                if s.st_mtime <= cutoff:
                    yield p, s.st_size
            except OSError:
                pass


def scan(cat):
    if cat[1] is None:
        i = RBInfo(sizeof(RBInfo))
        ctypes.windll.shell32.SHQueryRecycleBinW(None, byref(i))
        return i.i64Size
    return sum(size for _, size in _old_files(cat))


def clean(cat):
    """Удаляет файлы; занятые программами пропускает. Возвращает освобождённые байты."""
    if cat[1] is None:
        size = scan(cat)
        ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 7)  # без подтверждения/звука
        return size
    freed = 0
    for p, size in list(_old_files(cat)):
        try:
            os.chmod(p, stat.S_IWRITE)
            os.remove(p)
            freed += size
        except OSError:
            pass
    dirs = [d for root, d, _ in _walk(cat) if d != root]
    for d in reversed(dirs):
        try:
            os.rmdir(d)  # только пустые
        except OSError:
            pass
    return freed


def human(n):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit in ("Б", "КБ") else f"{n:.1f} {unit}"
        n /= 1024


# ---------- что занимает место (как WizTree / WinDirStat) ----------

# Типы для цветов схемы. Всё, чего нет в списке, — «Другое» (индекс len(KINDS))
KINDS = [("Видео", "mp4 mkv avi mov wmv webm m4v ts flv mpg mpeg"),
         ("Музыка", "mp3 flac wav ogg m4a aac opus wma"),
         ("Картинки", "jpg jpeg png gif webp bmp tif tiff heic psd raw svg ico"),
         ("Архивы и образы", "zip rar 7z tar gz xz iso img vhd vhdx wim cab pak"),
         ("Программы", "exe dll sys msi msp ocx drv"),
         ("Документы", "pdf doc docx xls xlsx ppt pptx txt md csv epub")]
KIND_OF = {e: i for i, (_, exts) in enumerate(KINDS) for e in exts.split()}
CLOUD = 0x1000 | 0x40000 | 0x400000  # OFFLINE, RECALL_ON_OPEN, RECALL_ON_DATA_ACCESS: файл в облаке, на диске не лежит
TOP_FILES, LIST_LIMIT = 500, 400
SAVE_LOCK = threading.Lock()
UPDATE = None  # что нашла последняя проверка обновлений
TREE = None  # последнее сканирование; не атрибут Api, иначе pywebview полезет обходить всё дерево


def ext_of(name):
    return name.rpartition(".")[2].lower() if "." in name else ""


def kind(name):
    return KIND_OF.get(ext_of(name), len(KINDS))


class Dir:
    """Папка. Файлы лежат не объектами, а одной строкой и массивами: иначе миллионы файлов съедят гигабайты памяти."""
    __slots__ = ("name", "parent", "dirs", "fnames", "fsizes", "ftimes", "size", "count", "mtime", "kinds")

    def __init__(self, name, parent, mtime=0):
        self.name, self.parent, self.dirs, self.mtime = name, parent, [], mtime
        self.fnames, self.fsizes, self.ftimes, self.size, self.count = "", array("Q"), array("I"), 0, 0
        self.kinds = array("Q", [0] * (len(KINDS) + 1))  # байт по типам: чем красить папку на схеме

    def files(self):
        return zip(self.fnames.split("\0"), self.fsizes, self.ftimes) if self.fsizes else ()

    def path(self):
        parts, d = [], self
        while d:
            parts.append(d.name)
            d = d.parent
        return os.path.join(*reversed(parts))


class Scan:
    def __init__(self, path):
        self.path, self.root, self.exts, self.top = path, None, {}, []
        self.running, self.cancel, self.files, self.bytes, self.denied, self.current = True, False, 0, 0, 0, path


def tree_walk(t):
    """Обходит папки, НЕ заходя в симлинки/junction: иначе одни и те же файлы посчитаются дважды."""
    try:
        root = Dir(t.path, None)
        order, stack = [root], [(root, t.path)]
        while stack and not t.cancel:
            d, path = stack.pop()
            t.current = path
            names = []
            try:
                with os.scandir(path) as it:
                    for e in it:
                        try:
                            st = e.stat(follow_symlinks=False)
                            when = min(max(int(st.st_mtime), 0), 0xFFFFFFFF)
                            if e.is_symlink() or e.is_junction():
                                continue
                            if e.is_dir(follow_symlinks=False):
                                c = Dir(e.name, d, when)
                                d.dirs.append(c)
                                order.append(c)
                                stack.append((c, e.path))
                                continue
                            size = 0 if st.st_file_attributes & CLOUD else st.st_size
                        except OSError:
                            t.denied += 1
                            continue
                        names.append(e.name)
                        d.fsizes.append(size)
                        d.ftimes.append(when)
                        d.size += size
                        t.files += 1
                        t.bytes += size
                        d.kinds[kind(e.name)] += size
                        x = t.exts.setdefault(ext_of(e.name), [0, 0])
                        x[0] += size
                        x[1] += 1
                        if len(t.top) < TOP_FILES:
                            heapq.heappush(t.top, (size, e.path, when))
                        elif size > t.top[0][0]:
                            heapq.heappushpop(t.top, (size, e.path, when))
            except OSError:
                t.denied += 1  # System Volume Information и подобное
            d.fnames, d.count = "\0".join(names), len(names)
        for d in reversed(order[1:]):  # дети всегда позже родителей, поэтому суммы поднимаются за один проход
            d.parent.size += d.size
            d.parent.count += d.count
            for i, v in enumerate(d.kinds):
                d.parent.kinds[i] += v
        if not t.cancel:
            t.root = root
    finally:
        t.running = False


def tree_node(t, path):
    d = t.root
    rel = os.path.relpath(path, t.path)
    for part in () if rel == "." else rel.split(os.sep):
        d = next((c for c in d.dirs if c.name == part), None)
        if d is None:
            return None
    return d


def tree_list(d):
    dirs = [{"name": c.name, "size": c.size, "count": c.count, "mtime": c.mtime, "dir": True} for c in d.dirs]
    files = [{"name": n, "size": s, "count": 1, "mtime": m, "dir": False} for n, s, m in d.files()]
    items = dirs + files
    return {"path": d.path(), "size": d.size, "count": d.count, "total": len(items),
            "items": heapq.nlargest(LIST_LIMIT, items, key=lambda i: i["size"])}


def tree_map(d, floor, depth):
    """Схема: [имя, размер, тип, дети]. Тип: 0.. — KINDS, -1 папка, -2-k — мелочь, в которой больше всего типа k."""
    kids, left = [], list(d.kinds)
    for c in d.dirs:
        if c.size >= floor:
            kids.append([c.name, c.size, -1, tree_map(c, floor if depth else float("inf"), depth - 1)])
            left = [a - b for a, b in zip(left, c.kinds)]
    for n, s, _ in d.files():
        if s >= floor:
            kids.append([n, s, kind(n), []])
            left[kind(n)] -= s
    rest = d.size - sum(k[1] for k in kids)
    if rest > 0:
        kids.append(["Мелкие файлы", rest, -2 - left.index(max(left)), []])
    return kids


def tree_search(t, query, limit=300):
    q, found, stack = query.casefold(), [], [t.root]
    while stack:
        d = stack.pop()
        stack += d.dirs
        hits = [(s, n, m) for n, s, m in d.files() if q in n.casefold()]
        if hits:
            base = d.path()
            found += [(s, os.path.join(base, n), m) for s, n, m in hits]
        if q in d.name.casefold() and d.parent:
            found.append((d.size, d.path(), d.mtime))
    return heapq.nlargest(limit, found)


def tree_forget(t, path):
    """Убирает удалённое из дерева, чтобы не сканировать диск заново."""
    parent = tree_node(t, os.path.dirname(path))
    name = os.path.basename(path)
    if parent is None:
        return 0
    gone = next((c for c in parent.dirs if c.name == name), None)
    if gone:
        parent.dirs.remove(gone)
        size, count, kinds, stack = gone.size, gone.count, gone.kinds, [gone]
        while stack:
            d = stack.pop()
            stack += d.dirs
            for n, s, _ in d.files():
                x = t.exts[ext_of(n)]
                x[0] -= s
                x[1] -= 1
    else:
        rows = list(parent.files())
        i = next((i for i, r in enumerate(rows) if r[0] == name), None)
        if i is None:
            return 0
        size, count = rows.pop(i)[1], 1
        kinds = [size if i == kind(name) else 0 for i in range(len(KINDS) + 1)]
        x = t.exts[ext_of(name)]
        x[0] -= size
        x[1] -= 1
        parent.fnames = "\0".join(r[0] for r in rows)
        parent.fsizes, parent.ftimes = array("Q", (r[1] for r in rows)), array("I", (r[2] for r in rows))
    d = parent
    while d:
        d.size -= size
        d.count -= count
        for i, v in enumerate(kinds):
            d.kinds[i] -= v
        d = d.parent
    t.top = [x for x in t.top if x[1] != path and not x[1].startswith(path + os.sep)]
    heapq.heapify(t.top)
    return size


def protected(path):
    """Корень диска, Windows целиком и сами системные папки в корзину не отправляем."""
    p = os.path.normcase(os.path.abspath(path))
    if p == os.path.splitdrive(p)[0] + os.sep:
        return True
    win = os.path.normcase(W)
    if p == win or p.startswith(win + os.sep):
        return True
    keep = [os.environ.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData", "USERPROFILE")]
    return any(k and (p == os.path.normcase(k) or os.path.normcase(k).startswith(p + os.sep)) for k in keep)


class ShFileOp(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", ctypes.c_void_p), ("pTo", ctypes.c_void_p),
                ("fFlags", wintypes.WORD), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_void_p)]


def recycle(path):
    """В корзину. Если у диска её нет (флешка), Windows сама предупредит, что файл удалится навсегда."""
    buf = ctypes.create_unicode_buffer(path + "\0")  # список путей заканчивается двумя нулями
    op = ShFileOp(None, 3, ctypes.addressof(buf), None, 0x40 | 0x10 | 0x4000)  # DELETE; ALLOWUNDO, NOCONFIRMATION, WANTNUKEWARNING
    return ctypes.windll.shell32.SHFileOperationW(byref(op)) == 0 and not op.fAnyOperationsAborted and not os.path.lexists(path)


# ---------- состояние и окно ----------

DEFAULTS = {"applied": None, "cats": [c[3] for c in CATEGORIES], "ram_mask": RAM_DEFAULT,
            "auto_on": False, "auto_pct": 90, "int_on": False, "int_min": 30, "tray_on": False, "lang": ""}


def load(fallback=None):
    try:
        st = {**DEFAULTS, **json.loads(STATE.read_text("utf-8-sig"))}  # -sig: файл мог сохранить блокнот
        st["cats"] += DEFAULTS["cats"][len(st["cats"]):]  # появились новые пункты очистки
        return st
    except FileNotFoundError:
        return dict(DEFAULTS)
    except (OSError, ValueError):  # файл как раз перезаписывается — агенту лучше взять прошлое состояние
        return fallback or dict(DEFAULTS)


def save(data):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, STATE)


# ---------- обновление и язык ----------

VERSION = "1.0.4"
REPO, ASSET = "fggtf24-cyber/yaytsechist", "Yaytsechist.exe"
TITLES = {"ru": "Яйцечист", "en": "Yaytsechist"}


def lang_of(st):
    """Язык из настроек, а если его не выбирали — как у Windows."""
    return st["lang"] or ("ru" if k32.GetUserDefaultUILanguage() & 0x3FF == 0x19 else "en")


def tr(ru, en):
    return en if lang_of(load()) == "en" else ru


def make_shortcut(lnk, exe):
    """Windows ищет программы по ярлыкам в «Пуске»: без него «яйцечист» не найдётся, файл-то называется латиницей."""
    q = lambda s: s.replace("'", "''")
    ps(f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{q(lnk)}');"
       f"if($s.TargetPath -ne '{q(exe)}'){{$s.TargetPath='{q(exe)}';$s.WorkingDirectory='{q(os.path.dirname(exe))}';$s.Save()}}")


def find_window():
    return next(filter(None, (ctypes.windll.user32.FindWindowW(None, t) for t in TITLES.values())), 0)


def ver(text):
    return tuple(int(x) for x in re.findall(r"\d+", text)[:3])


def latest_release():
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                 headers={"User-Agent": "Yaytsechist", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        rel = json.load(r)
    asset = next(a for a in rel["assets"] if a["name"] == ASSET)
    return {"version": rel["tag_name"].lstrip("v"), "url": asset["browser_download_url"], "size": asset["size"],
            "sha256": (asset.get("digest") or "").removeprefix("sha256:")}


def update_download(info, dest):
    """Скачивает новый exe и сверяет размер и SHA-256 из релиза. Битый файл не оставляет."""
    h = hashlib.sha256()
    try:
        with urllib.request.urlopen(info["url"], timeout=30) as r, open(dest, "wb") as f:
            while chunk := r.read(1 << 20):
                h.update(chunk)
                f.write(chunk)
        if os.path.getsize(dest) != info["size"] or (info["sha256"] and h.hexdigest() != info["sha256"]):
            raise ValueError("файл обновления повреждён")
    except Exception:
        if os.path.exists(dest):
            os.remove(dest)
        raise


def swap_exe(new, exe):
    """Запущенный exe нельзя перезаписать, но можно переименовать. Если подмена сорвалась, старый возвращается на место."""
    old = exe + ".old"
    if os.path.exists(old):
        os.remove(old)
    os.replace(exe, old)
    try:
        os.replace(new, exe)
    except OSError:
        os.replace(old, exe)
        raise


def update_apply(info, restart=True):
    exe = sys.executable
    update_download(info, exe + ".new")
    swap_exe(exe + ".new", exe)
    if restart:
        ps("if(Get-ScheduledTask GameBoost -EA 0){Stop-ScheduledTask GameBoost -EA 0;Start-ScheduledTask GameBoost}")
        # без RESET новый процесс решит, что он дочерний, и возьмёт временную папку старого, которая сейчас удалится
        subprocess.Popen([exe, "--updated"], env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
                         creationflags=subprocess.DETACHED_PROCESS)
        os._exit(0)


class Api:
    """Методы, которые вызывает интерфейс (ui.html) через pywebview."""

    def get(self):
        st = load()
        return {**self.sysinfo(), "applied": bool(st["applied"]), "agent": agent_running(),
                "cats": [{"name": c[0], "note": c[4], "on": on} for c, on in zip(CATEGORIES, st["cats"])],
                "areas": [{"flag": f, "name": n, "note": t} for f, n, t in RAM_AREAS],
                "kinds": [k[0] for k in KINDS] + ["Другое"], "ext_kinds": KIND_OF, "lang": lang_of(st), "version": VERSION,
                "mask": st["ram_mask"], **{k: st[k] for k in ("auto_on", "auto_pct", "int_on", "int_min", "tray_on")}}

    def sysinfo(self):
        return {"timer": query_timer(), "plan": plan_name()}

    def agent(self):
        return agent_running()

    def auto_last(self):
        try:
            return json.loads(AUTO_LOG.read_text("utf-8"))
        except (OSError, ValueError):
            return None

    def mem(self):
        return mem()

    def put(self, key, value):
        if key not in DEFAULTS or key == "applied":
            return
        with SAVE_LOCK:  # pywebview вызывает методы из разных потоков: две записи подряд не должны затереть друг друга
            st = load()
            st[key] = value
            save(st)
        if key in ("auto_on", "int_on", "tray_on"):
            sync_agent(st)
        if key == "lang":
            import webview
            webview.windows[0].set_title(TITLES[value])

    def scan(self):
        return [scan(c) for c in CATEGORIES]

    def clean_disk(self):
        return sum(clean(c) for c, on in zip(CATEGORIES, load()["cats"]) if on)

    def clean_ram(self):
        return clean_ram(load()["ram_mask"])

    def antilag(self):
        st, nv = load(), 0
        if st["applied"]:
            disable(st["applied"])
            st["applied"], ok = None, [True, True]
        else:
            st["applied"] = enable()
            ok = ["power" in st["applied"], "bcd" in st["applied"]]
            nv = clean(NV_CACHE)
        save(st)
        sync_agent(st)
        running = agent_running()
        return {"on": bool(st["applied"]), "ok": ok + [running == wanted(st)], "agent": running, "nv": nv}

    def drives(self):
        found, mask = [], k32.GetLogicalDrives()
        for i in range(26):
            p = f"{chr(65 + i)}:\\"
            if mask >> i & 1 and k32.GetDriveTypeW(p) in (2, 3):  # съёмный, жёсткий
                try:
                    u = shutil.disk_usage(p)
                    found.append({"path": p, "total": u.total, "free": u.free})
                except OSError:
                    pass  # кардридер без карты
        return found

    def tree_pick(self):
        import webview
        r = webview.windows[0].create_file_dialog(webview.FileDialog.FOLDER)
        return r[0] if r else None

    def tree_start(self, path):
        global TREE
        if not (TREE and TREE.running):
            TREE = Scan(path)
            threading.Thread(target=tree_walk, args=(TREE,), daemon=True).start()

    def tree_cancel(self):
        TREE.cancel = True

    def tree_progress(self):
        t = TREE
        return {"running": t.running, "files": t.files, "bytes": t.bytes, "current": t.current, "ready": bool(t.root)}

    def tree_info(self):
        t, u = TREE, shutil.disk_usage(TREE.path)
        drive = t.path == os.path.splitdrive(t.path)[0] + os.sep
        return {"path": t.path, "size": t.root.size, "count": t.root.count, "free": u.free, "total": u.total, "denied": t.denied,
                "hidden": max(0, u.total - u.free - t.root.size) if drive else 0}  # системное и закрытое от сканера

    def tree_dir(self, path):
        d = tree_node(TREE, path)
        return tree_list(d) if d else None

    def tree_map(self, path):
        d = tree_node(TREE, path)
        return tree_map(d, d.size / 700, 3) if d else []

    def tree_files(self, query=""):
        rows = tree_search(TREE, query) if query else heapq.nlargest(300, TREE.top)
        return [{"path": p, "size": s, "mtime": m} for s, p, m in rows]

    def tree_types(self):
        kinds = [{"name": n, "size": 0, "count": 0} for n, _ in KINDS] + [{"name": "Другое", "size": 0, "count": 0}]
        for e, (size, count) in TREE.exts.items():
            k = kinds[KIND_OF.get(e, len(KINDS))]
            k["size"] += size
            k["count"] += count
        top = heapq.nlargest(60, TREE.exts.items(), key=lambda x: x[1][0])
        return {"kinds": kinds, "exts": [{"ext": e, "size": v[0], "count": v[1], "kind": KIND_OF.get(e, len(KINDS))}
                                         for e, v in top if v[1]]}

    def tree_reveal(self, path):
        subprocess.Popen(f'explorer /select,"{path}"')

    def tree_delete(self, path):
        if protected(path):
            return {"ok": False, "error": "Это системная папка, её удалять нельзя"}
        if not recycle(path):
            return {"ok": False, "error": "Не получилось: файл занят или удаление отменено"}
        return {"ok": True, "freed": tree_forget(TREE, path)}

    def tree_export(self):
        import webview
        r = webview.windows[0].create_file_dialog(webview.FileDialog.SAVE, save_filename="место-на-диске.csv")
        if not r:
            return None
        out = r if isinstance(r, str) else r[0]
        with open(out, "w", encoding="utf-8-sig", newline="") as f:  # -sig: иначе Excel покажет кракозябры
            w = csv.writer(f, delimiter=";")
            w.writerow(["Папка", "Байт", "Файлов", "Изменена"])
            stack = [TREE.root]
            while stack:
                d = stack.pop()
                stack += d.dirs
                w.writerow([d.path(), d.size, d.count, time.strftime("%Y-%m-%d", time.localtime(d.mtime))])
        return out

    def update_check(self):
        """Новая версия: {"version": ...}, уже последняя: {"latest": True}, нет сети: None."""
        global UPDATE
        if not FROZEN:
            return {"latest": True}  # из исходников обновлять нечего
        try:
            UPDATE = latest_release()
        except Exception:
            return None
        return {"version": UPDATE["version"]} if ver(UPDATE["version"]) > ver(VERSION) else {"latest": True}

    def update_install(self):
        try:
            update_apply(UPDATE)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reboot(self):
        run("shutdown", "/r", "/t", "5")


def gui(check=False):
    """check=True — открыть окно, убедиться, что интерфейс получил данные от Python, и выйти (код 0/1)."""
    import webview
    if "--updated" in sys.argv:
        time.sleep(2)  # старая версия ещё закрывается и держит мьютекс окна
    if FROZEN and os.path.exists(sys.executable + ".old"):
        try:
            os.remove(sys.executable + ".old")
        except OSError:
            pass  # ещё занят фоновым агентом, удалится при следующем запуске
    ctypes.WinDLL("kernel32", use_last_error=True).CreateMutexW(None, False, "GameBoostWindow")
    if ctypes.get_last_error() == 183 and not check:  # ERROR_ALREADY_EXISTS — окно уже открыто, показать его
        u32 = ctypes.windll.user32
        if h := find_window():
            u32.ShowWindow(h, 9)  # SW_RESTORE
            u32.SetForegroundWindow(h)
        return
    w = webview.create_window(TITLES[lang_of(load())], str(ASSETS / "ui.html"), js_api=Api(),
                              width=1120, height=720, min_size=(900, 600), background_color="#262624")

    def titlebar():
        """Заголовок в цвет левой панели. Цвета — 0xBBGGRR, работает с Windows 11; на Windows 10 остаётся системный."""
        w.events.shown.wait(10)
        h = find_window()
        # тёмные значки кнопок, рамка, фон заголовка, текст заголовка
        for attr, value in ((20, 1), (34, 0x1D1E1F), (35, 0x1D1E1F), (36, 0xF5F9FA)):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(h, attr, byref(ctypes.c_int(value)), 4)

    def probe():
        for _ in range(60):
            time.sleep(0.5)
            try:
                if w.evaluate_js("typeof S != 'undefined' && !!S && document.querySelectorAll('#cats .row').length > 0"):
                    os._exit(0)
            except Exception:
                pass
        os._exit(1)

    def started():
        if FROZEN:
            programs = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            threading.Thread(target=make_shortcut, args=(str(programs / f"{TITLES[lang_of(load())]}.lnk"), sys.executable), daemon=True).start()
        titlebar()
        if check:
            probe()

    webview.start(started)


# ---------- самопроверка ----------

def selftest():
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
        old, new = os.path.join(tmp, "old.tmp"), os.path.join(tmp, "new.tmp")
        os.makedirs(os.path.join(tmp, "sub"))
        deep = os.path.join(tmp, "sub", "deep.tmp")
        precious = os.path.join(outside, "precious.txt")
        for p in (old, new, deep, precious):
            Path(p).write_text("x" * 100)
        for p in (old, deep, precious):
            os.utime(p, (time.time() - 2 * DAY,) * 2)
        subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(tmp, "link"), outside], capture_output=True)
        cat = ("test", [tmp], DAY, True)
        assert scan(cat) == 200, scan(cat)
        assert clean(cat) == 200
        assert not os.path.exists(old) and not os.path.exists(os.path.join(tmp, "sub")), "старое не удалено"
        assert os.path.exists(new), "удалён свежий файл"
        assert os.path.exists(precious), "очистка прошла через junction!"
    assert not wanted(DEFAULTS) and wanted({**DEFAULTS, "tray_on": True})
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
        os.makedirs(os.path.join(tmp, "a", "b"))
        Path(tmp, "a", "b", "film.mp4").write_bytes(b"x" * 1000)
        Path(tmp, "a", "note.txt").write_bytes(b"x" * 10)
        Path(outside, "big.bin").write_bytes(b"x" * 5000)
        subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(tmp, "link"), outside], capture_output=True)
        t = Scan(tmp)
        tree_walk(t)
        assert (t.root.size, t.root.count) == (1010, 2), "сканер посчитал не то или прошёл через junction"
        assert tree_node(t, os.path.join(tmp, "a", "b")).size == 1000
        assert [k[0] for k in tree_map(t.root, 0, 6)] == ["a"] and tree_list(t.root)["items"][0]["name"] == "a"
        assert tree_search(t, "FILM")[0][0] == 1000 and t.exts["mp4"] == [1000, 1]
        assert tree_forget(t, os.path.join(tmp, "a", "b")) == 1000 and t.root.size == 10 and t.exts["mp4"] == [0, 0]
        assert not t.top[0][1].endswith("film.mp4") and tree_forget(t, os.path.join(tmp, "a", "note.txt")) == 10
        assert t.root.size == 0 and t.root.count == 0 and not protected(tmp)
    assert protected("C:\\") and protected(W) and protected(W + r"\System32\x.dll") and protected(os.environ["ProgramFiles"])
    assert tree_map(Dir("x", None), 1, 3) == [] and kind("a.MKV") == 0 and kind("нет расширения") == len(KINDS)
    assert ver("v1.10.0") > ver("1.9.9") and not ver("1.0.0") > ver(VERSION) and lang_of({"lang": "en"}) == "en"
    with tempfile.TemporaryDirectory() as tmp:
        src, dest, exe = (os.path.join(tmp, n) for n in ("src.bin", "new.bin", "app.exe"))
        Path(src).write_bytes(b"new")
        Path(exe).write_bytes(b"old")
        good = {"url": Path(src).as_uri(), "size": 3, "sha256": hashlib.sha256(b"new").hexdigest()}
        try:
            update_download({**good, "sha256": "0" * 64}, dest)
            assert False, "принят файл с чужой контрольной суммой"
        except ValueError:
            assert not os.path.exists(dest), "битое обновление осталось на диске"
        update_download(good, dest)
        swap_exe(dest, exe)
        assert Path(exe).read_bytes() == b"new" and Path(exe + ".old").read_bytes() == b"old" and not os.path.exists(dest)
    prefs = '["Disk Cache Controls"]\n\t"Enabled 2" = "1"\n\t"Folder 7" = "D:\\Кэш"\n\t"Max Size 3" = "94"\n\n["Other"]\n\t"Folder 1" = "C:\\no"\n'
    assert ae_prefs_folders(prefs) == ["D:\\Кэш"], ae_prefs_folders(prefs)
    hexed = '["Disk Cache Controls"]\n\t"Folder 9" = "E:\\"D09AD18D"\\a"\\\n\t\t"bc"\n\t"Max Size 3" = "94"\n'
    assert ae_prefs_folders(hexed) == ["E:\\Кэ\\abc"], ae_prefs_folders(hexed)
    assert ae_prefs_folders('["Disk Cache Controls"]\n\t"Enabled 2" = "0"\n') == []
    pats = cache_patterns(["D:\\Работа", "D:\\Работа\\", "E:\\X\\Media Cache Files", None, "F:\\"])
    assert "D:\\Работа\\Media Cache" in pats and "E:\\X\\Media Cache Files" in pats and "F:\\Peak Files" in pats
    assert len(pats) == 9 and "D:\\Работа" not in pats, pats
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "Media Cache Files"))
        Path(tmp, "Media Cache Files", "a.cfa").write_bytes(b"x" * 7)
        Path(tmp, "project.aep").write_bytes(b"x" * 100)
        cat = ("ae", cache_patterns([tmp]), 0, False)
        assert scan(cat) == 7 and clean(cat) == 7
        assert os.path.exists(os.path.join(tmp, "project.aep")), "очистка кэша удалила файл пользователя"
    with tempfile.TemporaryDirectory() as tmp:
        lnk = os.path.join(tmp, "Яйцечист.lnk")
        make_shortcut(lnk, r"C:\Windows\notepad.exe")
        assert os.path.exists(lnk), "ярлык с русским именем не создался"
    assert human(1536) == "2 КБ" and human(5 * 1024 ** 3) == "5.0 ГБ"
    bcd = "identifier              {current}\nuseplatformtick         Yes\ndisabledynamictick      Да\nnx  OptIn\n"
    assert parse_bcd(bcd) == {"useplatformtick": "yes", "disabledynamictick": "yes"}, parse_bcd(bcd)
    assert all(len(t) == 3 for t in POWER_TWEAKS) and all(h in HIVES for h, *_ in REG_TWEAKS)
    assert 0.4 < set_timer(True) <= 1.0 and query_timer() <= 1.0, "таймер не встал"
    set_timer(False)
    m = mem()
    assert 0 < m["load"] <= 100 and 0 < m["virt"] <= 100 and m["total"] > 0, m
    assert clean_ram(RAM_DEFAULT) >= 0  # без админа Windows отклонит вызовы — проверяем, что не падает
    print("selftest OK", m)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--scan" in sys.argv:
        for cat in CATEGORIES:
            print(f"{human(scan(cat)):>10}  {cat[0]}")
    elif "--agent" in sys.argv:
        agent()
    elif "--uicheck" in sys.argv:
        gui(check=True)
    elif "--update" in sys.argv:  # обновиться без окна и перезапуска; код 0 — файл заменён
        info = latest_release()
        if ver(info["version"]) <= ver(VERSION):
            sys.exit(2)
        update_apply(info, restart=False)
    elif not ctypes.windll.shell32.IsUserAnAdmin():
        ctypes.windll.shell32.ShellExecuteW(None, "runas", EXE, SCRIPT, None, 1)
    else:
        gui()
