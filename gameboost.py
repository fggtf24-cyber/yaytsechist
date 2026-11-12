"""Яйцечист — оптимизация как в AntiLag (github.com/AmbitiousPilots/AntiLag),
очистка диска как в Razer Cortex System Booster, очистка памяти как в Mem Reduct.

    python gameboost.py             — окно программы (сама запросит права администратора)
    python gameboost.py --scan      — только посчитать мусор, ничего не удаляя
    python gameboost.py --selftest  — самопроверка
    python gameboost.py --update    — обновиться с GitHub без окна
    python gameboost.py --agent     — фоновый агент (таймер 0.5 мс + автоочистка RAM), запускается планировщиком
    python gameboost.py --uninstall ПУТЬ — открыть «Удаление программ» с программой этого ярлыка или exe (меню Проводника)
"""
import codecs, csv, ctypes, fnmatch, glob, hashlib, heapq, json, os, re, shutil, stat, subprocess, sys, tempfile, threading, time, urllib.request, winreg
from array import array
from ctypes import byref, sizeof, wintypes
from pathlib import Path

STATE = Path(os.environ["LOCALAPPDATA"]) / "GameBoost" / "state.json"
SHOW_FLAG = STATE.with_name("show")  # просьба к агенту показать окно
AGENT_MUTEX = "GameBoostAgent"
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


powrprof = ctypes.WinDLL("powrprof")
powrprof.PowerGetActiveScheme.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
powrprof.PowerReadFriendlyName.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD))
k32.LocalFree.argtypes = (ctypes.c_void_p,)


def plan_name():
    """Через API, а не текст powercfg: тот идёт в кодировке консоли, и в нерусской Windows кириллица становится «???»."""
    g = ctypes.c_void_p()
    if powrprof.PowerGetActiveScheme(None, byref(g)):
        return "—"
    try:
        size = wintypes.DWORD()
        powrprof.PowerReadFriendlyName(None, g, None, None, None, byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if powrprof.PowerReadFriendlyName(None, g, None, None, buf, byref(size)):
            return "—"
        return ctypes.wstring_at(buf) or "—"
    finally:
        k32.LocalFree(g)


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
    subprocess.Popen(f'"{EXE}" {SCRIPT}--window', env=fresh_env(), creationflags=subprocess.DETACHED_PROCESS)


def agent_alive():
    """Мьютекс создан с правами администратора, поэтому обычному процессу в него «отказано», и это тоже значит «есть»."""
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    return bool(k.OpenMutexW(0x100000, False, AGENT_MUTEX)) or ctypes.get_last_error() == 5  # SYNCHRONIZE, ACCESS_DENIED


def wake_agent():
    """Повторный запуск: окно открывает уже работающий агент, поэтому нет ни второй копии, ни запроса прав."""
    ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY: иначе окно не сможет выйти на передний план
    SHOW_FLAG.touch()


def watch_show():
    while True:
        time.sleep(.3)
        if SHOW_FLAG.exists():
            SHOW_FLAG.unlink(missing_ok=True)
            open_gui()


def exit_label():
    """Таймер 0.5 мс живёт, пока жив агент: только при включённой оптимизации выход меняет отклик."""
    return tr("Выход (таймер 0.5 мс отключится)", "Exit (0.5 ms timer will stop)") if load()["applied"] else tr("Выход", "Exit")


def quit_agent(icon):
    """icon.stop() тут не годится: цикл агента идёт дальше, пока нужен фон, и процесс остаётся жить."""
    icon.visible = False
    os._exit(0)


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
    k32.CreateMutexW(None, False, AGENT_MUTEX)
    SHOW_FLAG.unlink(missing_ok=True)
    menu = pystray.Menu(
        pystray.MenuItem(lambda item: tr("Открыть Яйцечист", "Open Yaytsechist"), lambda: open_gui(), default=True),
        pystray.MenuItem(lambda item: tr("Очистить память", "Clean memory"), lambda: clean_ram(load()["ram_mask"])),
        pystray.MenuItem(lambda item: exit_label(), quit_agent))
    icon = pystray.Icon("GameBoost", Image.open(ASSETS / "icon.ico"), "Яйцечист", menu)

    def loop(icon):
        icon.visible = True
        threading.Thread(target=watch_show, daemon=True).start()
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
    """Где медиакэш Adobe у этого пользователя (он общий у Premiere, After Effects, Media Encoder, Audition):
    настройки всех версий AE, реестр Adobe и стандартная папка Adobe\\Common. None — программ Adobe нет."""
    prefs = glob.glob(glob.escape(R) + r"\Adobe\After Effects\*\*Prefs.txt")
    if not prefs and not os.path.isdir(R + r"\Adobe\Common"):
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
    CATEGORIES.append(("Медиакэш Adobe", ae, 0, True, "закройте Premiere и After Effects"))

SYSTEM_DRIVE = os.environ.get("SystemDrive", "C:") + "\\"


def clean_winold(cat):
    """Windows.old принадлежит системе: сначала права группе «Администраторы», потом удаление.
    icacls /setowner, а не takeown /D Y: у takeown ответ «Y» зависит от языка Windows."""
    before = scan(cat)
    for d in (p for p in cat[1] if os.path.isdir(p)):
        run("icacls", d, "/setowner", "*S-1-5-32-544", "/T", "/C", "/Q")
        run("icacls", d, "/grant", "*S-1-5-32-544:F", "/T", "/C", "/Q")
        run("cmd", "/c", "rd", "/s", "/q", d)  # rd не заходит в junction, удаляет только их самих
    return max(0, before - scan(cat))


def clean_winsxs(cat):
    """Старые версии компонентов в WinSxS. Вручную там удалять нельзя, только DISM; размер известен лишь после."""
    free = shutil.disk_usage(SYSTEM_DRIVE).free
    run("dism", "/Online", "/Cleanup-Image", "/StartComponentCleanup")
    return max(0, shutil.disk_usage(SYSTEM_DRIVE).free - free)


CATEGORIES += [
    ("Предыдущая версия Windows", [SYSTEM_DRIVE + n for n in ("Windows.old", "$Windows.~BT", "$Windows.~WS")], 0, False,
     "откат станет невозможен", clean_winold),
    ("Остатки обновлений Windows", "dism", 0, False, "долго, до 15 минут", clean_winsxs),
]


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
    if isinstance(cat[1], str):
        return -1  # размер заранее не узнать
    if cat[1] is None:
        i = RBInfo(sizeof(RBInfo))
        ctypes.windll.shell32.SHQueryRecycleBinW(None, byref(i))
        return i.i64Size
    return sum(size for _, size in _old_files(cat))


def clean(cat):
    """Удаляет файлы; занятые программами пропускает. Возвращает освобождённые байты."""
    if len(cat) > 5:
        return cat[5](cat)
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


def wipe(path):
    """Удаляет навсегда всё, что получится; занятое пропускает. Ссылки и junction убирает сами по себе,
    не заходя внутрь: иначе удалилось бы содержимое папки, на которую они указывают."""
    def rm(p):
        try:
            os.chmod(p, stat.S_IWRITE)
            os.remove(p)
        except OSError:
            pass

    if os.path.isdir(path) and not _is_link(path):
        for e in os.scandir(path):
            if _is_link(e.path):
                try:
                    os.rmdir(e.path) if e.is_dir() else os.remove(e.path)
                except OSError:
                    pass
            elif e.is_dir(follow_symlinks=False):
                wipe(e.path)
            else:
                rm(e.path)
        try:
            os.rmdir(path)
        except OSError:
            pass
    elif _is_link(path) and os.path.isdir(path):
        os.rmdir(path)
    else:
        rm(path)
    return not os.path.lexists(path)


def tree_refresh(t, path):
    """Пересканирует одну папку и встраивает её в дерево. Возвращает, на сколько она уменьшилась."""
    parent = tree_node(t, os.path.dirname(path))
    if parent is None or not any(c.name == os.path.basename(path) for c in parent.dirs):
        return 0
    was = tree_forget(t, path)
    s = Scan(path)
    tree_walk(s)
    d = s.root
    d.name, d.parent = os.path.basename(path), parent
    parent.dirs.append(d)
    for e, (size, count) in s.exts.items():
        x = t.exts.setdefault(e, [0, 0])
        x[0] += size
        x[1] += count
    t.top = heapq.nlargest(TOP_FILES, t.top + s.top)
    heapq.heapify(t.top)
    p = parent
    while p:
        p.size += d.size
        p.count += d.count
        for i, v in enumerate(d.kinds):
            p.kinds[i] += v
        p = p.parent
    return was - d.size


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


def recycle(path, undo=True):
    """undo — в корзину; если у диска её нет (флешка), Windows сама предупредит, что файл удалится навсегда.
    Без undo — сразу навсегда. Занятые файлы Windows пропустит, тогда часть папки останется."""
    buf = ctypes.create_unicode_buffer(path + "\0")  # список путей заканчивается двумя нулями
    flags = (0x40 | 0x4000 if undo else 0) | 0x10 | 0x400  # ALLOWUNDO, WANTNUKEWARNING; NOCONFIRMATION, NOERRORUI
    op = ShFileOp(None, 3, ctypes.addressof(buf), None, flags)  # FO_DELETE
    return ctypes.windll.shell32.SHFileOperationW(byref(op)) == 0 and not op.fAnyOperationsAborted and not os.path.lexists(path)


# ---------- удаление программ (как Revo Uninstaller) ----------

UNINSTALL = r"Microsoft\Windows\CurrentVersion\Uninstall"
UNINSTALL_VIEWS = [("HKLM", "SOFTWARE"), ("HKLM", r"SOFTWARE\WOW6432Node"), ("HKCU", "Software")]
SOFT = UNINSTALL_VIEWS  # где программы хранят настройки: Software\Программа или Software\Издатель\Программа
UNINSTALL_REQ = STATE.with_name("uninstall")  # путь, по которому нажали «Удалить через Яйцечист»
REG_BACKUP = STATE.with_name("reg-backup")
MENU_KEYS = [rf"Software\Classes\{c}\shell\YaytsechistUninstall" for c in ("lnkfile", "exefile")]
APPS, LEFT, PROC = {}, {}, None
VERSION_NOISE = re.compile(r"\(.*?\)|\b(?:v?\d+(?:\.\d+)+|x64|x86|64-bit|32-bit|amd64|win64|win32)\b", re.I)
CORP = re.compile(r"\b(?:corporation|corp|inc|ltd|llc|gmbh|limited|co|ag|sa)\b", re.I)
# такие имена слишком общие: папку с ними не считаем остатком, даже если совпало
BLOCK = {"microsoft", "windows", "classes", "policies", "wow6432node", "clients", "programs", "program", "common", "commonfiles",
         "temp", "packages", "bin", "app", "apps", "application", "current", "data", "files", "shared", "tools", "launcher",
         "client", "setup", "update", "updater", "cache", "user", "users", "local", "roaming", "default", "games", "game"}


def norm(s):
    return re.sub(r"[\W_]+", "", s.casefold())


def exe_of(cmd):
    m = re.match(r'\s*"([^"]+)"|\s*(.+?\.exe)\b', cmd, re.I)
    return (m.group(1) or m.group(2)) if m else ""


def reg_children(hive, path):
    try:
        with winreg.OpenKey(HIVES[hive], path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            return [winreg.EnumKey(k, i) for i in range(winreg.QueryInfoKey(k)[0])]
    except OSError:
        return []


def key_exists(path):
    hive, _, rest = path.partition("\\")
    try:
        winreg.OpenKey(HIVES[hive], rest, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY).Close()
        return True
    except OSError:
        return False


def installed():
    """Программы из тех же ключей реестра, что читают «Параметры». id — полный путь к ключу программы."""
    found = {}
    for hive, base in UNINSTALL_VIEWS:
        for sub in reg_children(hive, rf"{base}\{UNINSTALL}"):
            key = rf"{hive}\{base}\{UNINSTALL}\{sub}"
            v = {n: reg_get(hive, key.partition("\\")[2], n) for n in ("DisplayName", "UninstallString", "Publisher", "DisplayVersion",
                                                                     "EstimatedSize", "InstallLocation", "DisplayIcon", "SystemComponent",
                                                                     "ParentKeyName", HOOK_VALUE)}
            name, cmd = str(v["DisplayName"] or "").strip(), str(v["UninstallString"] or "").strip()
            if is_hook(cmd) and v[HOOK_VALUE]:
                cmd = v[HOOK_VALUE]  # кнопку «Удалить» перехватили мы — настоящая команда сохранена рядом
            if not name or not cmd or v["SystemComponent"] == 1 or v["ParentKeyName"]:
                continue  # системные компоненты и обновления к программам «Параметры» тоже прячут
            size = v["EstimatedSize"]
            found.setdefault((name, v["DisplayVersion"]), {
                "id": key, "name": name, "publisher": str(v["Publisher"] or "").strip(), "cmd": cmd, "version": str(v["DisplayVersion"] or ""),                "size": size * 1024 if isinstance(size, int) else 0,
                "location": os.path.expandvars(str(v["InstallLocation"] or "")).strip().strip('"').rstrip("\\"),
                "icon": os.path.expandvars(str(v["DisplayIcon"] or "")).split(",")[0].strip().strip('"')})
    APPS.clear()
    APPS.update((a["id"], a) for a in found.values())
    return sorted(APPS.values(), key=lambda a: a["name"].casefold())


# Кнопка «Удалить» в «Параметрах» запускает UninstallString из реестра. Подменяем её на Яйцечист, а настоящую храним рядом.
# MSI-программы с WindowsInstaller=1 «Параметры» удаляют сами, мимо этой строки, — флаг на время снимаем и храним рядом.
HOOK_VALUE, MSI_VALUE = "YaytsechistUninstallString", "YaytsechistWindowsInstaller"


def is_hook(cmd):
    return "--uninstall-key" in str(cmd or "")


def hook(a):
    hive, _, path = a["id"].partition("\\")
    raw = reg_get(hive, path, "UninstallString")
    want = f'"{EXE}" {SCRIPT}--uninstall-key "{a["id"]}"'
    if raw and raw != want:
        if not is_hook(raw):
            reg_set(hive, path, HOOK_VALUE, raw)  # первая подмена или программа обновилась и записала свою строку заново
        reg_set(hive, path, "UninstallString", want)
    if raw and reg_get(hive, path, "WindowsInstaller") == 1:
        reg_set(hive, path, MSI_VALUE, 1)
        reg_set(hive, path, "WindowsInstaller", 0)


def unhook(a):
    """Вернуть как было: настоящую строку на место, нашу копию убрать."""
    hive, _, path = a["id"].partition("\\")
    saved = reg_get(hive, path, HOOK_VALUE)
    if saved and is_hook(reg_get(hive, path, "UninstallString")):
        reg_set(hive, path, "UninstallString", saved)
    if saved:
        reg_set(hive, path, HOOK_VALUE, None)
    if reg_get(hive, path, MSI_VALUE) == 1:
        reg_set(hive, path, "WindowsInstaller", 1)
        reg_set(hive, path, MSI_VALUE, None)


def sync_hooks(on):
    for a in installed():
        try:
            hook(a) if on else unhook(a)
        except OSError:
            pass  # без прав администратора записи в HKLM не изменить — остальные всё равно приводим в порядок


def app_home(a):
    """Папка установки, если ей можно верить: её имя похоже на название или прямо в ней лежит удалитель.
    Бывает, что установщик пишет туда общую папку вроде D:\\Games — её трогать нельзя."""
    loc = a["location"]
    if not loc or protected(loc):
        return ""
    base, full = norm(os.path.basename(loc)), norm(VERSION_NOISE.sub(" ", a["name"]))
    if len(base) < 3 or base in BLOCK or base != full and (base in norm(a["publisher"]) or shared_name(base, a)):
        return ""  # Adobe пишет сюда C:\Program Files\Adobe — папку всех своих программ
    if base in full or full in base:
        return loc
    own = [os.path.dirname(x) for x in (a["icon"], exe_of(os.path.expandvars(a["cmd"]))) if x]
    return loc if any(os.path.normcase(d) == os.path.normcase(loc) for d in own) else ""


def shared_name(n, a):
    """Имя общее, если с него начинается название другой программы или оно входит в имя её издателя (Ample Sound, Arturia)."""
    return any(x["id"] != a["id"] and (norm(x["name"]).startswith(n) or n in norm(x["publisher"])) for x in APPS.values())


def app_names(a):
    """Под какими именами программа заводит свои папки и ключи реестра, и имя издателя."""
    full = norm(VERSION_NOISE.sub(" ", a["name"]))
    pub = norm(CORP.sub(" ", a["publisher"]))
    names = {full}
    if pub and full.startswith(pub):
        names.add(full[len(pub):])  # Google Chrome лежит в Google\Chrome
    base = norm(os.path.basename(app_home(a)))
    if base and (base in full or full in base):  # папку, принятую только из-за удалителя внутри, именем не считаем
        names.add(base)
    return {n for n in names if len(n) >= 3 and n not in BLOCK and (n == full or not shared_name(n, a))}, pub if len(pub) >= 3 and pub not in BLOCK - {"microsoft"} else ""


# ---------- когда программу запускали в последний раз ----------

USERASSIST = r"Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist"
PREFETCH = os.path.join(W, "Prefetch")
KNOWN_FOLDERS = {"{6D809377-6AF0-444B-8957-A3773F02200E}": "ProgramW6432", "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}": "ProgramFiles(x86)",
                 "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}": "LOCALAPPDATA", "{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}": "APPDATA",
                 "{62AB5D82-FDC1-4DC3-A9DD-070D1D495D97}": "ProgramData", "{5E6C858F-0E22-4760-9AFE-EA3317B67173}": "USERPROFILE"}
NOT_MAIN = re.compile(r"(?i)^(?:unins|uninst|update|setup|crash|helper|install|maintenance|elevat|notification)")


def launches(prefetch=PREFETCH):
    """Журналы запусков Windows: UserAssist — полные пути exe и имена ярлыков, запущенных из «Пуска» и Проводника;
    Prefetch — имена exe, запущенных как угодно (папку видит только администратор). Время — в секундах Unix."""
    paths, lnks, exes = {}, {}, {}
    put = lambda d, k, t: d.__setitem__(k, max(d.get(k, 0), t))
    for g in reg_children("HKCU", USERASSIST):
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{USERASSIST}\{g}\Count")
        except OSError:
            continue
        with k:
            for i in range(winreg.QueryInfoKey(k)[1]):
                name, data, _ = winreg.EnumValue(k, i)
                ft = int.from_bytes(data[60:68], "little") if isinstance(data, bytes) and len(data) >= 68 else 0
                if not ft:
                    continue
                t, name = ft / 1e7 - 11644473600, codecs.decode(name, "rot13")  # FILETIME → Unix; имена записаны ROT13
                for guid, env in KNOWN_FOLDERS.items():
                    if name.upper().startswith(guid):
                        name = os.environ.get(env, guid) + name[len(guid):]
                if name.lower().endswith(".lnk"):
                    put(lnks, norm(Path(name).stem), t)
                elif name.lower().endswith(".exe"):
                    put(paths, os.path.normcase(name), t)
    try:
        for e in os.scandir(prefetch):
            if e.name.lower().endswith(".pf"):  # ADOBE PREMIERE PRO.EXE-1A2B3C4D.pf
                put(exes, e.name.rsplit("-", 1)[0].lower(), e.stat().st_mtime)
    except OSError:
        pass
    return paths, lnks, exes


def app_exes(a):
    """Основные exe программы: из значка и из корня её папки, без удалителей и обновлялок."""
    found = {os.path.basename(a["icon"]).lower()} if a["icon"].lower().endswith(".exe") else set()
    if home := app_home(a):
        try:
            found.update(e.name.lower() for e in os.scandir(home) if e.name.lower().endswith(".exe"))
        except OSError:
            pass
    return {n for n in found if not NOT_MAIN.match(n)}


def last_used(apps, runs):
    """a["used"]: когда запускали в последний раз, 0 — Windows об этом не помнит."""
    paths, lnks, exes = runs
    names = {a["id"]: app_exes(a) for a in apps}
    seen = {}
    for s in names.values():
        for n in s:
            seen[n] = seen.get(n, 0) + 1
    for a in apps:
        homes = [os.path.normcase(h) + os.sep for h in (app_home(a), a["icon"] and os.path.dirname(a["icon"])) if h and not protected(h)]
        times = [t for p, t in paths.items() if any(p.startswith(h) for h in homes)]
        times += [lnks[n] for n in app_names(a)[0] if n in lnks]
        times += [exes[n[:29]] for n in names[a["id"]] if seen[n] == 1 and n[:29] in exes]  # Update.exe есть у многих — по нему не судим
        a["used"] = int(max(times, default=0))


def file_roots():
    e = lambda k: os.environ.get(k, "")
    menu = r"Microsoft\Windows\Start Menu\Programs"
    roots = [e("ProgramFiles"), e("ProgramFiles(x86)"), e("ProgramW6432"), e("ProgramData"), R, L, os.path.join(L, "Programs"),
             os.path.join(e("USERPROFILE"), "AppData", "LocalLow"), os.path.join(R, menu), os.path.join(e("ProgramData"), menu),
             os.path.join(e("USERPROFILE"), "Desktop"), os.path.join(e("PUBLIC"), "Desktop")]
    return list({os.path.normcase(r): r for r in roots if r and os.path.isdir(r)}.values())


def dir_size(p):
    total = 0
    for d, subs, names in os.walk(p):
        subs[:] = [s for s in subs if not _is_link(os.path.join(d, s))]
        for n in names:
            try:
                total += os.lstat(os.path.join(d, n)).st_size
            except OSError:
                pass
    return total


def leftovers(a, roots=None, soft=SOFT, adobe_roots=None):
    """Что осталось от программы: папка установки, папки с её именем в Program Files, AppData, ProgramData,
    в «Пуске», ярлыки на рабочем столе, её ключи в реестре и сама запись об установке."""
    roots = file_roots() if roots is None else roots
    names, pub = app_names(a)
    keep = {os.path.normcase(r) for r in roots}
    others = [os.path.normcase(h) for x in APPS.values() if x["id"] != a["id"] and (h := app_home(x))]

    def ok(p):  # не корень, не системное и внутри нет другой установленной программы
        n = os.path.normcase(p)
        return n not in keep and not protected(p) and not any(o == n or o.startswith(n + os.sep) for o in others)

    def ours(entry):
        if entry.is_dir(follow_symlinks=False) and not _is_link(entry.path):
            return norm(entry.name) in names
        return entry.name.lower().endswith(".lnk") and norm(entry.name[:-4]) in names

    found = {}
    if (home := app_home(a)) and os.path.isdir(home) and ok(home):
        found[os.path.normcase(home)] = home
    for root in roots:
        try:
            kids = list(os.scandir(root))
        except OSError:
            continue
        for e in kids:
            if ours(e) and ok(e.path):
                found[os.path.normcase(e.path)] = e.path
            elif pub and norm(e.name) == pub and e.is_dir(follow_symlinks=False) and not _is_link(e.path):
                try:
                    found.update((os.path.normcase(c.path), c.path) for c in os.scandir(e.path) if ours(c) and ok(c.path))
                except OSError:
                    pass
    if fam := adobe_family(a):  # папки только этой версии, общие Adobe\After Effects не трогаем
        found.update((os.path.normcase(p), p) for p, f, tok in adobe_folders(adobe_roots) if f == fam and adobe_match(tok, a) and ok(p))
    paths = sorted(found.values(), key=len)
    paths = [p for i, p in enumerate(paths) if not any(p.lower().startswith(q.lower() + os.sep) for q in paths[:i])]  # вложенное уйдёт с родителем
    items = [sized(p) for p in paths]
    regs = []
    for hive, base in soft:
        for c in reg_children(hive, base):
            if norm(c) in names:
                regs.append(rf"{hive}\{base}\{c}")
            elif pub and norm(c) == pub:
                regs += [rf"{hive}\{base}\{c}\{cc}" for cc in reg_children(hive, rf"{base}\{c}") if norm(cc) in names]
    if key_exists(a["id"]):
        regs.append(a["id"])
    return items + [{"kind": "reg", "path": r, "size": None} for r in dict.fromkeys(regs)]


# Adobe держит настройки по версиям: Adobe\After Effects\26.0, Adobe\Adobe Photoshop 2025, Adobe Illustrator 30 Settings.
# Как Creative Cloud Cleaner Tool: папка версии принадлежит программе, если совпал год из названия или первая цифра версии.
ADOBE_APPS = {"photoshop", "illustrator", "indesign", "incopy", "aftereffects", "premierepro", "premiererush", "mediaencoder",
              "audition", "animate", "dreamweaver", "bridge", "characteranimator", "lightroom", "lightroomclassic", "fresco",
              "substance3dpainter", "substance3ddesigner", "substance3dsampler", "substance3dstager", "dimension", "xd"}
ADOBE_NAMED = re.compile(r"(?i)(?:adobe\s+)?(.+?)\s+((?:19|20)\d\d|\d+(?:\.\d+)*)(?:\s+settings)?(?:\.lnk)?$")


def adobe_family(a):
    """«Adobe After Effects 2026» → aftereffects; не Adobe — пусто."""
    if "adobe" not in a["publisher"].lower():
        return ""
    return norm(re.sub(r"(?i)^adobe\s+|\b(?:19|20)\d\d\b", " ", VERSION_NOISE.sub(" ", a["name"])))


def adobe_match(token, a):
    if re.fullmatch(r"(?:19|20)\d\d", token):
        return token in a["name"]
    return token.split(".")[0] == a.get("version", "").split(".")[0]  # Premiere 26.2 хранит настройки в 26.0


def adobe_folders(roots=None):
    """(путь, программа, версия) для всех папок и ярлыков версий Adobe."""
    if roots is None:
        e, menu = lambda k: os.environ.get(k, ""), r"Microsoft\Windows\Start Menu\Programs"
        roots = [os.path.join(r, "Adobe") for r in (R, L, e("ProgramFiles"), e("ProgramFiles(x86)"), e("ProgramData"),
                                                     os.path.join(e("USERPROFILE"), "Documents"))]
        roots += [os.path.join(R, menu), os.path.join(e("ProgramData"), menu)]
    for root in roots:
        try:
            kids = list(os.scandir(root))
        except OSError:
            continue
        for k in kids:
            if _is_link(k.path):
                continue
            m = ADOBE_NAMED.match(k.name)
            family = norm(re.sub(r"(?i)^adobe\s+", "", k.name))
            if m and norm(m[1]) in ADOBE_APPS and (k.is_dir() or k.name.lower().endswith(".lnk")):
                yield k.path, norm(m[1]), m[2]
            elif family in ADOBE_APPS and k.is_dir():
                try:
                    yield from ((c.path, family, c.name) for c in os.scandir(k.path)
                                if c.is_dir() and not _is_link(c.path) and re.fullmatch(r"\d+(?:\.\d+)*", c.name))
                except OSError:
                    pass


def sized(p):
    return {"kind": "dir" if os.path.isdir(p) else "file", "path": p, "size": dir_size(p) if os.path.isdir(p) else os.path.getsize(p)}


def adobe_leftovers(roots=None):
    """Папки версий Adobe, которых на компьютере больше нет (Photoshop 2025 при установленном 2026)."""
    have = [a for a in APPS.values() if adobe_family(a)]
    return [sized(p) for p, fam, tok in adobe_folders(roots)
            if not protected(p) and not any(adobe_family(a) == fam and adobe_match(tok, a) for a in have)]


def remove_leftover(item, roots):
    """Папки и ярлыки — в корзину, ключ реестра — сперва в .reg-копию, потом удалить."""
    p = item["path"]
    if item["kind"] == "reg":
        REG_BACKUP.mkdir(parents=True, exist_ok=True)
        run("reg", "export", p, str(REG_BACKUP / f"{time.strftime('%Y%m%d-%H%M%S')}-{norm(p)[-40:]}.reg"), "/y", "/reg:64")
        run("reg", "delete", p, "/f", "/reg:64")
        return not key_exists(p)
    if protected(p):
        return False
    gone = not os.path.lexists(p) or recycle(p)
    parent = os.path.dirname(p)
    if gone and os.path.normcase(parent) not in {os.path.normcase(r) for r in roots} and not protected(parent):
        try:
            os.rmdir(parent)  # опустевшая папка издателя
        except OSError:
            pass
    return gone


def uninstall_cmd(cmd):
    """MSI в списке часто записан как «изменить» (/I), а нам нужно удаление (/X)."""
    return re.sub(r"(?i)(msiexec(?:\.exe)?\s+)/I\s*(\{)", r"\1/X\2", os.path.expandvars(cmd))


def resolve_lnk(p):
    if not p.lower().endswith(".lnk"):
        return p
    code, out = ps(f"(New-Object -ComObject WScript.Shell).CreateShortcut('{p.replace(chr(39), chr(39) * 2)}').TargetPath")
    return out.strip() if code == 0 and out.strip() else p


def find_app(path):
    """Какая программа стоит за ярлыком или exe: файл лежит в её папке, а если нет — совпадает имя ярлыка."""
    target, stem = os.path.normcase(resolve_lnk(path)), norm(Path(path).stem)
    by_name = None
    for a in APPS.values():
        homes = [h for h in (app_home(a), a["icon"] and os.path.dirname(a["icon"])) if h and not protected(h)]
        if any(target.startswith(os.path.normcase(h) + os.sep) for h in homes):
            return a["id"]
        if not by_name and stem in app_names(a)[0]:
            by_name = a["id"]
    return by_name


def sync_menu(on):
    """Пункт в меню Проводника для ярлыков и exe. В Windows 11 он под «Показать дополнительные параметры»."""
    for k in MENU_KEYS:
        if on:
            reg_set("HKCU", k, "", tr("Удалить через Яйцечист", "Uninstall with Yaytsechist"))
            reg_set("HKCU", k, "Icon", sys.executable if FROZEN else str(ASSETS / "icon.ico"))
            reg_set("HKCU", k + r"\command", "", f'"{EXE}" {SCRIPT}--uninstall "%1"')
        else:
            for sub in (k + r"\command", k):
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
                except OSError:
                    pass


# ---------- состояние и окно ----------

OLD_ORDER = [c[0] for c in CATEGORIES[:8]] + ["Кэш After Effects"]


def migrate_cats(cats):
    """До 1.1 галочки хранились списком по порядку пунктов: новый пункт сдвинул бы чужие галочки."""
    return dict(zip(OLD_ORDER, cats)) if isinstance(cats, list) else cats


DEFAULTS = {"applied": None, "cats": {c[0]: c[3] for c in CATEGORIES}, "ram_mask": RAM_DEFAULT,
            "auto_on": False, "auto_pct": 90, "int_on": False, "int_min": 30, "tray_on": False, "lang": "", "ask_delete": True, "menu_on": True, "hook_on": False, "ask_left": False}


def load(fallback=None):
    try:
        st = {**DEFAULTS, **json.loads(STATE.read_text("utf-8-sig"))}  # -sig: файл мог сохранить блокнот
        st["cats"] = migrate_cats(st["cats"])
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

VERSION = "1.3.2"
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


def drop_old(exe):
    for old in glob.glob(glob.escape(exe) + ".old*"):
        try:
            os.remove(old)
        except OSError:
            pass  # ещё занят, удалится в следующий раз


def fresh_env():
    """Без RESET новый процесс решит, что он дочерний, и возьмёт временную папку, а с ней и код, запустившей его версии."""
    return {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}


def swap_exe(new, exe):
    """Запущенный exe нельзя перезаписать, но можно переименовать. Если подмена сорвалась, старый возвращается на место."""
    drop_old(exe)
    old = exe + ".old"
    if os.path.exists(old):  # из него ещё работает прежний агент, а такой файл Windows удалить не даст
        old += str(int(time.time()))
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
        subprocess.Popen([exe, "--updated"], env=fresh_env(), creationflags=subprocess.DETACHED_PROCESS)
        os._exit(0)


class Api:
    """Методы, которые вызывает интерфейс (ui.html) через pywebview."""

    def get(self):
        st = load()
        return {**self.sysinfo(), "applied": bool(st["applied"]), "agent": agent_alive(),
                "cats": [{"name": c[0], "note": c[4], "on": st["cats"].get(c[0], c[3])} for c in CATEGORIES],
                "areas": [{"flag": f, "name": n, "note": t} for f, n, t in RAM_AREAS],
                "kinds": [k[0] for k in KINDS] + ["Другое"], "ext_kinds": KIND_OF, "lang": lang_of(st), "version": VERSION,
                "mask": st["ram_mask"], **{k: st[k] for k in ("auto_on", "auto_pct", "int_on", "int_min", "tray_on", "ask_delete", "menu_on", "hook_on", "ask_left")}}

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
        if key == "menu_on":
            sync_menu(value)
        if key == "hook_on":
            sync_hooks(value)
        if key == "lang":
            import webview
            webview.windows[0].set_title(TITLES[value])

    def scan(self):
        return [scan(c) for c in CATEGORIES]

    def clean_disk(self):
        cats = load()["cats"]
        return sum(clean(c) for c in CATEGORIES if cats.get(c[0], c[3]))

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

    def tree_delete(self, path, forever=False):
        if protected(path):
            return {"ok": False, "error": "Это системная папка, её удалять нельзя", "freed": 0}
        if wipe(path) if forever else recycle(path):
            return {"ok": True, "freed": tree_forget(TREE, path)}
        if os.path.isdir(path):  # удалилась только часть: пересчитать папку, чтобы схема не врала
            freed = tree_refresh(TREE, path)
            if freed:
                return {"ok": False, "error": "Часть файлов занята и не удалилась", "freed": freed}
        return {"ok": False, "error": "Не получилось: файл занят или удаление отменено", "freed": 0}

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

    def apps(self):
        sync_hooks(load()["hook_on"])  # реестр всегда как галочка: новые, обновившиеся, отменённые; выключено — вернуть всё
        apps = installed()
        last_used(apps, launches())
        return [{k: a[k] for k in ("id", "name", "publisher", "size", "location", "used")} for a in apps]

    def app_request(self):
        """Нажали «Удалить через Яйцечист» в Проводнике: вывести окно и сказать, какую программу выделить."""
        try:
            path = UNINSTALL_REQ.read_text("utf-8")
            UNINSTALL_REQ.unlink()
        except OSError:
            return None
        u32 = ctypes.windll.user32
        if h := find_window():
            if u32.IsIconic(h):
                u32.ShowWindow(h, 9)  # SW_RESTORE, только если свёрнуто: развёрнутое окно не трогаем
            u32.SetForegroundWindow(h)
        if not APPS:
            installed()
        if path.startswith("key:"):  # нажали «Удалить» в «Параметрах» — сразу спросить про удаление
            key, (hive, _, rest) = path[4:], path[4:].partition("\\")
            name = reg_get(hive, rest, "DisplayName") if hive in HIVES else None
            found = key if key in APPS else next((a["id"] for a in APPS.values() if a["name"] == name), None)
            return {"path": name or key, "id": found, "ask": True}
        return {"path": path, "id": find_app(path)}

    def uninstall_start(self, app_id):
        global PROC
        a = APPS.get(app_id)
        if not a or is_hook(a["cmd"]):
            return tr("Не нашлась команда удаления этой программы", "This program's uninstall command was not found")
        unhook(a)  # своему удалителю программа достаётся с реестром как был; если удаление отменят — перехватим снова
        try:
            PROC = subprocess.Popen(uninstall_cmd(a["cmd"]))
        except (OSError, KeyError) as e:
            return str(e)

    def uninstall_state(self, app_id):
        """Удалители вроде NSIS сразу запускают свою копию и выходят, поэтому конец удаления — это пропавшая запись в реестре."""
        return {"running": bool(PROC and PROC.poll() is None), "gone": not key_exists(app_id)}

    def leftovers(self, app_id):
        global LEFT
        items = leftovers(APPS[app_id])
        LEFT = {i["path"]: i for i in items}
        return items

    def adobe_leftovers(self):
        global LEFT
        installed()
        items = adobe_leftovers()
        LEFT = {i["path"]: i for i in items}
        return items

    def leftovers_delete(self, paths):
        roots, done, freed = file_roots(), 0, 0
        for p in paths:
            if p in LEFT and remove_leftover(LEFT[p], roots):  # удаляем только то, что сами нашли
                done += 1
                freed += LEFT[p]["size"] or 0
        return {"done": done, "failed": len(paths) - done, "freed": freed}


def gui(check=False):
    """check=True — открыть окно, убедиться, что интерфейс получил данные от Python, и выйти (код 0/1)."""
    import webview
    if "--updated" in sys.argv:
        time.sleep(2)  # старая версия ещё закрывается и держит мьютекс окна
    if FROZEN:
        drop_old(sys.executable)
    ctypes.WinDLL("kernel32", use_last_error=True).CreateMutexW(None, False, "GameBoostWindow")
    if ctypes.get_last_error() == 183 and not check:  # ERROR_ALREADY_EXISTS — окно уже открыто, показать его
        u32 = ctypes.windll.user32
        if h := find_window():
            u32.ShowWindow(h, 9)  # SW_RESTORE
            u32.SetForegroundWindow(h)
        return
    w = webview.create_window(TITLES[lang_of(load())], str(ASSETS / "ui.html"), js_api=Api(),
                              width=1120, height=720, min_size=(900, 600), background_color="#191919")

    def titlebar():
        """Заголовок в цвет левой панели. Цвета — 0xBBGGRR, работает с Windows 11; на Windows 10 остаётся системный."""
        w.events.shown.wait(10)
        h = find_window()
        # тёмные значки кнопок, рамка, фон заголовка, текст заголовка
        for attr, value in ((20, 1), (34, 0x202020), (35, 0x202020), (36, 0xD4D4D4)):
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
            threading.Thread(target=lambda: (make_shortcut(str(programs / f"{TITLES[lang_of(load())]}.lnk"), sys.executable),
                                             sync_menu(load()["menu_on"]), sync_hooks(load()["hook_on"])), daemon=True).start()
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
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "d"))
        Path(tmp, "d", "free.bin").write_bytes(b"x" * 300)
        Path(tmp, "d", "busy.mp4").write_bytes(b"x" * 20)
        t = Scan(tmp)
        tree_walk(t)
        with open(os.path.join(tmp, "d", "busy.mp4")):  # занят: Windows не даст удалить
            assert not wipe(os.path.join(tmp, "d"))
        assert not os.path.exists(os.path.join(tmp, "d", "free.bin")), "свободный файл не удалился навсегда"
        assert tree_refresh(t, os.path.join(tmp, "d")) == 300 and t.root.size == 20 and t.exts["mp4"] == [20, 1]
        assert wipe(os.path.join(tmp, "d")) and not os.path.exists(os.path.join(tmp, "d"))
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
        Path(outside, "precious.txt").write_text("x")
        os.makedirs(os.path.join(tmp, "d"))
        subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(tmp, "d", "link"), outside], capture_output=True)
        assert wipe(os.path.join(tmp, "d")) and os.path.exists(os.path.join(outside, "precious.txt")), "удаление прошло через junction!"
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
        Path(dest).write_bytes(b"newer")
        with open(exe + ".old"):  # занят, как файл работающей программы
            swap_exe(dest, exe)
        assert Path(exe).read_bytes() == b"newer" and len(glob.glob(glob.escape(exe) + ".old*")) == 2, "занятый .old сорвал обновление"
        drop_old(exe)
        assert not glob.glob(glob.escape(exe) + ".old*")
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
        os.utime(os.path.join(tmp, "Media Cache Files", "a.cfa"), (time.time() - 60,) * 2)  # time.time() грубее файловых часов
        Path(tmp, "project.aep").write_bytes(b"x" * 100)
        cat = ("ae", cache_patterns([tmp]), 0, False)
        assert scan(cat) == 7 and clean(cat) == 7
        assert os.path.exists(os.path.join(tmp, "project.aep")), "очистка кэша удалила файл пользователя"
    with tempfile.TemporaryDirectory() as tmp:
        lnk = os.path.join(tmp, "Яйцечист.lnk")
        make_shortcut(lnk, r"C:\Windows\notepad.exe")
        assert os.path.exists(lnk), "ярлык с русским именем не создался"
    old = migrate_cats([True] * 8 + [False])
    assert old["Корзина"] and old["Кэш After Effects"] is False and "Предыдущая версия Windows" not in old
    assert migrate_cats({"Корзина": False}) == {"Корзина": False} and scan(CATEGORIES[-1]) == -1
    with tempfile.TemporaryDirectory() as tmp:
        d = os.path.join(tmp, "Windows.old")
        os.makedirs(os.path.join(d, "sub"))
        Path(d, "sub", "a.dll").write_bytes(b"x" * 90)
        os.utime(os.path.join(d, "sub", "a.dll"), (time.time() - 60,) * 2)  # time.time() грубее файловых часов
        os.chmod(os.path.join(d, "sub", "a.dll"), stat.S_IREAD)  # только чтение, как многое в Windows.old
        Path(tmp, "keep.txt").write_text("x")
        assert clean(("w", [d], 0, False, "", clean_winold)) == 90 and not os.path.exists(d) and os.path.exists(os.path.join(tmp, "keep.txt"))
    app = {"id": r"HKCU\Software\нет-такого-ключа", "name": "Foo Bar 1.2.3 (x64)", "publisher": "Acme Inc.", "location": "", "icon": "", "cmd": ""}
    assert app_names(app) == ({"foobar"}, "acme"), app_names(app)
    guid_ = "{12345678-1234-1234-1234-123456789012}"
    assert uninstall_cmd("MsiExec.exe /I" + guid_) == "MsiExec.exe /X" + guid_ and exe_of('"C:\\a b\\u.exe" /S') == "C:\\a b\\u.exe"
    assert exe_of("C:\\a b\\unins000.exe /S") == "C:\\a b\\unins000.exe"
    with tempfile.TemporaryDirectory() as root:
        for d in ("Foo Bar", r"Acme\FooBar", r"Acme\Other\x", "Foo Barn", r"Games\Foo Bar"):
            os.makedirs(os.path.join(root, d))
        Path(root, "Foo Bar.lnk").write_text("x")
        Path(root, "Games", "Foo Bar", "unins000.exe").write_text("x")
        other = {**app, "id": r"HKCU\Software\нет-other", "name": "Other", "location": os.path.join(root, "Acme", "Other")}
        shared = {**app, "id": r"HKCU\Software\нет-shared", "location": os.path.join(root, "Games"), "cmd": os.path.join(root, "Games", "Foo Bar", "unins000.exe")}
        APPS.clear()
        APPS.update((x["id"], x) for x in (app, other, shared))
        found = {i["path"] for i in leftovers(app, roots=[root], soft=[])}
        assert found == {os.path.join(root, p) for p in ("Foo Bar", r"Acme\FooBar", "Foo Bar.lnk")}, found
        assert app_home(shared) == "" and app_home(other) == other["location"], "общую папку Games приняли за папку программы"
        assert not any(i["path"].endswith("Games") for i in leftovers(shared, roots=[root], soft=[])), "предложено удалить общую папку"
        assert find_app(os.path.join(root, "Acme", "Other", "x", "run.exe")) == other["id"] and find_app(r"C:\Foo Bar.exe") == app["id"]
        # Ample Sound: папка с удалителем названа по издателю, в ней и в AppData\Ample Sound живут и другие его плагины
        for d in (r"Ample Sound", r"Plugins\Ample Sound"):
            os.makedirs(os.path.join(root, d))
        Path(root, "Plugins", "Ample Sound", "unins.exe").write_text("x")
        bass = {**app, "id": r"HKCU\Software\нет-bass", "name": "Ample Bass J", "publisher": "Ample Sound Technology Co., Ltd.",
                "location": os.path.join(root, "Plugins", "Ample Sound"), "cmd": os.path.join(root, "Plugins", "Ample Sound", "unins.exe")}
        APPS[bass["id"]] = bass
        assert app_home(bass) == "" and not leftovers(bass, roots=[root], soft=[]), "предложена общая папка издателя"
        APPS.clear()
    test_key = r"HKCU\Software\YaytsechistSelftest"  # подмена кнопки «Удалить» и возврат как было — на своём ключе
    t_app = {"id": test_key}
    reg_set("HKCU", test_key[5:], "UninstallString", '"C:\\x\\unins000.exe" /S')
    try:
        hook(t_app)
        assert is_hook(reg_get("HKCU", test_key[5:], "UninstallString")) and reg_get("HKCU", test_key[5:], HOOK_VALUE) == '"C:\\x\\unins000.exe" /S'
        hook(t_app)  # повторно — копия не затирается нашей же строкой
        reg_set("HKCU", test_key[5:], "UninstallString", '"C:\\x\\new.exe"')  # программа обновилась
        hook(t_app)
        assert reg_get("HKCU", test_key[5:], HOOK_VALUE) == '"C:\\x\\new.exe"', "после обновления программы сохранилась старая команда"
        unhook(t_app)
        assert reg_get("HKCU", test_key[5:], "UninstallString") == '"C:\\x\\new.exe"' and reg_get("HKCU", test_key[5:], HOOK_VALUE) is None
        reg_set("HKCU", test_key[5:], "WindowsInstaller", 1)  # MSI: флаг снимается на время подмены и возвращается
        hook(t_app)
        assert reg_get("HKCU", test_key[5:], "WindowsInstaller") == 0 and reg_get("HKCU", test_key[5:], MSI_VALUE) == 1
        unhook(t_app)
        assert reg_get("HKCU", test_key[5:], "WindowsInstaller") == 1 and reg_get("HKCU", test_key[5:], MSI_VALUE) is None
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, test_key[5:])
    with tempfile.TemporaryDirectory() as root:  # последний запуск: путь из UserAssist, ярлык, имя exe из Prefetch
        for d in ("Foo", "Bar", "Baz"):
            os.makedirs(os.path.join(root, d))
            Path(root, d, "Update.exe").write_text("x")
        mk = lambda n, **k: {"id": n, "name": n, "publisher": "", "location": os.path.join(root, n), "icon": os.path.join(root, n, n + ".exe"), "cmd": "", **k}
        apps = [mk("Foo"), mk("Bar"), mk("Baz")]
        runs = ({os.path.normcase(os.path.join(root, "Foo", "sub", "foo.exe")): 100.0}, {"bar": 200.0}, {"baz.exe": 300.0, "update.exe": 999.0})
        last_used(apps, runs)
        assert [a["used"] for a in apps] == [100, 200, 300], [a["used"] for a in apps]
        assert launches(os.path.join(root, "нет"))[2] == {}
    with tempfile.TemporaryDirectory() as root:  # Adobe: удаляем только папки своей версии и версий, которых уже нет
        for d in (r"After Effects\26.0", r"After Effects\25.6", r"After Effects\Logs", "Adobe Photoshop 2025", "Adobe Photoshop 2026", "Common"):
            os.makedirs(os.path.join(root, d))
        ae = {"id": r"HKCU\Software\нет-ae", "name": "Adobe After Effects 2026", "publisher": "Adobe Inc.", "version": "26.2.1",
              "location": "", "icon": "", "cmd": ""}
        ps26 = {**ae, "id": r"HKCU\Software\нет-ps", "name": "Adobe Photoshop 2026", "version": "27.8"}
        APPS.update((x["id"], x) for x in (ae, ps26))
        assert adobe_family(ae) == "aftereffects" and adobe_family({**ae, "publisher": "Acme"}) == ""
        assert {i["path"] for i in adobe_leftovers([root])} == {os.path.join(root, p) for p in (r"After Effects\25.6", "Adobe Photoshop 2025")}
        assert {i["path"] for i in leftovers(ae, roots=[], soft=[], adobe_roots=[root])} == {os.path.join(root, r"After Effects\26.0")}
        APPS.clear()
    assert human(1536) == "2 КБ" and human(5 * 1024 ** 3) == "5.0 ГБ"
    bcd = "identifier              {current}\nuseplatformtick         Yes\ndisabledynamictick      Да\nnx  OptIn\n"
    assert parse_bcd(bcd) == {"useplatformtick": "yes", "disabledynamictick": "yes"}, parse_bcd(bcd)
    assert all(len(t) == 3 for t in POWER_TWEAKS) and all(h in HIVES for h, *_ in REG_TWEAKS)
    assert plan_name() not in ("—", "") and "?" not in plan_name(), plan_name()
    assert 0.4 < set_timer(True) <= 1.0 and query_timer() <= 1.0, "таймер не встал"
    set_timer(False)
    m = mem()
    assert 0 < m["load"] <= 100 and 0 < m["virt"] <= 100 and m["total"] > 0, m
    assert clean_ram(RAM_DEFAULT) >= 0  # без админа Windows отклонит вызовы — проверяем, что не падает
    print("selftest OK", m)


if __name__ == "__main__":
    # из меню Проводника (--uninstall путь) или кнопкой «Удалить» в «Параметрах» (--uninstall-key ключ):
    # запомнить, что удалять, а дальше обычный запуск без лишних аргументов
    if flag := next((f for f in ("--uninstall", "--uninstall-key") if f in sys.argv), None):
        i = sys.argv.index(flag)
        STATE.parent.mkdir(parents=True, exist_ok=True)
        arg = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
        UNINSTALL_REQ.write_text(("key:" if flag == "--uninstall-key" else "") + arg, "utf-8")
        del sys.argv[i:i + 2]
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY: окно само выйдет вперёд
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        if k.OpenMutexW(0x100000, False, "GameBoostWindow") or ctypes.get_last_error() == 5:
            sys.exit()  # окно уже открыто и через секунду само подхватит путь — без второго запроса прав
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
    elif not {"--window", "--updated"} & set(sys.argv) and agent_alive():
        wake_agent()
    elif not ctypes.windll.shell32.IsUserAnAdmin():
        ctypes.windll.shell32.ShellExecuteW(None, "runas", EXE, SCRIPT + " ".join(sys.argv[1:]), None, 1)  # флаги вроде --window не теряются
    else:
        gui()
