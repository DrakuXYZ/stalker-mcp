"""stalker-mcp: MCP-сервер для S.T.A.L.K.E.R. Anomaly (xray-monolith) под MO2.

Связь с живой игрой идёт через папку обмена (см. mod/mcp_bridge.script):
сервер кладёт Lua-код в req.lua, игра исполняет его и отвечает в resp.json.
Остальные инструменты работают без игры: логи, моды MO2, поиск по файлам
с учётом приоритета модов.

Запуск:  python server.py            — MCP-сервер (stdio)
         python server.py install    — поставить мод MCP-Bridge в MO2
"""

from __future__ import annotations

import configparser
import csv
import fnmatch
import io
import json
import msvcrt
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

def _env_path(name: str, default: Path | None = None) -> Path | None:
    value = os.environ.get(name, "").strip().strip('"')
    return Path(value) if value else default


# Папка служебных данных сервера: мост и скриншоты по умолчанию
DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "stalker-mcp"

GAME = _env_path("STALKER_GAME")          # папка игры: Anomaly.exe, bin\, gamedata\, appdata\
MO2 = _env_path("STALKER_MO2")            # папка портативного MO2: ModOrganizer.exe, mods\, profiles\
BRIDGE = _env_path("STALKER_BRIDGE", DATA / "bridge")  # обязательно ВНЕ папки игры и MO2
SCREENSHOTS = _env_path("STALKER_SCREENSHOTS", DATA / "screenshots")
UNPACKED = _env_path("STALKER_UNPACKED")  # необязательно: распакованные .db игры
DEFAULT_EXE = os.environ.get("STALKER_EXE", "Anomaly (DX11-AVX)")

HEARTBEAT_STALE_S = 3.0
BRIDGE_VERSION = 3  # VERSION в mcp_bridge.script
BRIDGE_MOD = "MCP-Bridge"
HERE = Path(__file__).resolve().parent

_instructions = (
    "Инструменты для S.T.A.L.K.E.R. Anomaly 1.5.x (движок xray-monolith) под Mod Organizer 2. "
    "game_* требуют запущенной игры с загруженным сейвом (мост работает и на паузе в меню после "
    "первой загрузки). game_lua исполняет Lua в игре: доступны все глобалы Anomaly (db.actor, "
    "alife(), level, ini_sys, xr_* модули); переменные сохраняются между вызовами. "
    "Строки из игры приходят в cp1251 и декодируются сервером. "
)
if MO2:
    _instructions += f"Моды MO2: {MO2 / 'mods'}. "
if UNPACKED:
    _instructions += f"Распакованные ресурсы игры: {UNPACKED}."

mcp = MCPServer("stalker", instructions=_instructions)


def check_config() -> None:
    missing = [n for n, v in (("STALKER_GAME", GAME), ("STALKER_MO2", MO2)) if v is None]
    if missing:
        raise SystemExit(
            "stalker-mcp: не заданы переменные окружения " + ", ".join(missing) +
            ". Пример: STALKER_GAME=D:\\Anomaly  STALKER_MO2=D:\\Anomaly\\MO2 (см. README).")
    for name, path in (("STALKER_GAME", GAME), ("STALKER_MO2", MO2)):
        if not path.is_dir():
            raise SystemExit(f"stalker-mcp: {name}={path} — такой папки нет.")


# ---------------------------------------------------------------- utils

def smart_decode(data: bytes) -> str:
    """UTF-8 там, где это корректный UTF-8, иначе байт как cp1251.

    В ответах игры смешаны строки исходника запроса и строки движка (cp1251).
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    out: list[str] = []
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b < 0x80:
            out.append(chr(b))
            i += 1
            continue
        size = 2 if 0xC2 <= b <= 0xDF else 3 if 0xE0 <= b <= 0xEF else 4 if 0xF0 <= b <= 0xF4 else 0
        if size and i + size <= n:
            try:
                out.append(data[i:i + size].decode("utf-8"))
                i += size
                continue
            except UnicodeDecodeError:
                pass
        out.append(bytes([b]).decode("cp1251", errors="replace"))
        i += 1
    return "".join(out)


def read_text(path: Path) -> str:
    return smart_decode(path.read_bytes())


def lua_str(s: str) -> str:
    """Строковый литерал Lua."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") + '"'


def processes() -> dict[str, int]:
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                         encoding="cp866", errors="replace").stdout
    found = {}
    for row in csv.reader(io.StringIO(out)):
        if len(row) >= 2 and re.match(r"(?i)^(Anomaly.*|ModOrganizer)\.exe$", row[0]):
            found[row[0]] = int(row[1])
    return found


def heartbeat() -> dict | None:
    path = BRIDGE / "heartbeat.json"
    for _ in range(5):  # игра пишет файл без rename — можно застать его недописанным
        try:
            info = json.loads(read_text(path))
            break
        except ValueError:
            time.sleep(0.02)
        except OSError:
            return None
    else:
        return None
    info["age_s"] = round(time.time() - path.stat().st_mtime, 1)
    return info


# ---------------------------------------------------------------- bridge

class BridgeError(ToolError):
    pass


_bridge_lock = threading.Lock()


@contextmanager
def bridge_slot():
    """Слот запроса один на всех: блокировка и внутри процесса, и между сессиями."""
    with _bridge_lock:
        BRIDGE.mkdir(parents=True, exist_ok=True)
        with open(BRIDGE / "lock", "a+b") as fh:
            while True:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


def ensure_bridge_files() -> None:
    """req.lua и resp.json должны существовать до старта игры.

    usvfs MO2 запоминает файлы на старте процесса: созданный позже файл игра
    не видит даже вне папки игры, а новое содержимое существующего — видит.
    Поэтому оба файла только перезаписываются и никогда не удаляются.
    """
    BRIDGE.mkdir(parents=True, exist_ok=True)
    for name in ("req.lua", "resp.json"):
        (BRIDGE / name).touch(exist_ok=True)


def bridge_call(code: str, timeout: float = 10.0) -> dict:
    hb = heartbeat()
    if hb is None or hb["age_s"] > HEARTBEAT_STALE_S:
        state = "пульса не было ни разу" if hb is None else f"последний пульс {hb['age_s']} с назад"
        raise BridgeError(
            f"Игра не отвечает ({state}). Мост живёт, пока загружен сейв: в главном меню до первой "
            "загрузки он не работает. Проверьте game_status.")
    if hb.get("version") != BRIDGE_VERSION:
        raise BridgeError(
            f"В игре мост версии {hb.get('version')}, сервер ждёт {BRIDGE_VERSION}: "
            "скрипт обновлён после запуска игры. Перезапустите игру.")

    req_id = uuid.uuid4().hex
    req = BRIDGE / "req.lua"
    resp = BRIDGE / "resp.json"

    # Движок думает в cp1251: так русские строки в коде совпадут со строками игры
    source = f"--mcp:{req_id}\n{code}\n--mcp:end\n"
    try:
        payload = source.encode("cp1251")
    except UnicodeEncodeError:
        payload = source.encode("utf-8")

    with bridge_slot():
        req.write_bytes(payload)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.03)
            try:
                result = json.loads(smart_decode(resp.read_bytes()))
            except (OSError, ValueError):
                continue
            if result.get("id") != req_id:
                continue
            return result

        if req.stat().st_size:
            req.write_bytes(b"")
            raise BridgeError(f"Игра не забрала запрос за {timeout} с (загрузка уровня, сворачивание или вылет).")
        raise BridgeError(f"Игра забрала запрос, но не ответила за {timeout} с. Возможен вылет — см. game_crash.")


def lua_result(code: str, timeout: float = 10.0) -> dict:
    res = bridge_call(code, timeout)
    if not res.get("ok"):
        raise BridgeError(res.get("error", "неизвестная ошибка") +
                          ("\n\nвывод:\n" + "\n".join(res["output"]) if res.get("output") else ""))
    return res


# ---------------------------------------------------------------- MO2

def mo2_profile() -> str:
    ini = configparser.ConfigParser(interpolation=None, strict=False)
    ini.read(MO2 / "ModOrganizer.ini", encoding="utf-8-sig")
    raw = ini.get("General", "selected_profile", fallback="Default")
    m = re.match(r"@ByteArray\((.*)\)", raw)
    return m.group(1) if m else raw


def modlist_path() -> Path:
    return MO2 / "profiles" / mo2_profile() / "modlist.txt"


def read_modlist() -> list[tuple[str, bool]]:
    """Моды в порядке modlist.txt: первый в файле — самый высокий приоритет."""
    mods = []
    for line in modlist_path().read_text(encoding="utf-8-sig").splitlines():
        if line[:1] in "+-" and len(line) > 1:
            mods.append((line[1:], line[0] == "+"))
    return mods


def data_layers() -> list[tuple[str, Path]]:
    """Слои gamedata от высшего приоритета к низшему."""
    layers = [("MO2 overwrite", MO2 / "overwrite" / "gamedata")]
    for name, enabled in read_modlist():
        if enabled and not name.endswith("_separator"):
            layers.append((f"mod: {name}", MO2 / "mods" / name / "gamedata"))
    layers.append(("Anomaly\\gamedata", GAME / "gamedata"))
    if UNPACKED:
        layers.append(("распакованные архивы игры", UNPACKED))
    return [(label, p) for label, p in layers if p.is_dir()]


def norm_game_path(p: str) -> str:
    p = p.replace("/", "\\").strip("\\")
    if p.lower().startswith("gamedata\\"):
        p = p[9:]
    return p


# ---------------------------------------------------------------- tools: игра

@mcp.tool()
def game_status() -> dict:
    """Состояние: запущены ли игра и MO2, жив ли мост, где игрок, был ли вылет."""
    procs = processes()
    hb = heartbeat()
    status = {
        "processes": procs,
        "bridge": "нет пульса" if hb is None else ("онлайн" if hb["age_s"] <= HEARTBEAT_STALE_S else "молчит"),
        "heartbeat": hb,
        "mo2_profile": mo2_profile(),
    }
    log = latest_log()
    if log:
        text = read_text(log)
        status["log"] = str(log)
        status["log_has_fatal"] = "FATAL ERROR" in text
    return status


@mcp.tool()
def game_lua(code: str, timeout: float = 10.0) -> dict:
    """Исполнить Lua в запущенной игре (LuaJIT 5.1, окружение Anomaly).

    Однострочное выражение возвращается как результат (`db.actor:position()`),
    блоку нужен явный `return`. print()/out() собираются в output. Глобальные
    переменные, заданные в коде, живут между вызовами. Игровые объекты в
    ответе превращаются в {id, name, section}, векторы — в {x, y, z}.
    """
    res = bridge_call(code, timeout)
    return {k: res[k] for k in ("ok", "result", "output", "error", "ms") if k in res and res[k] not in (None, [], {})}


@mcp.tool()
def game_console(command: str) -> dict:
    """Выполнить консольную команду движка (например `time_factor 10`, `hud_draw 0`, `g_god 1`)."""
    lua_result(f"get_console():execute({lua_str(command)}) return true")
    return {"ok": True, "command": command}


PLAYER_LUA = r"""
local a = db.actor
local p = a:position()
local w = a:active_item()
local r = {
  level = level.name(),
  pos = {x = p.x, y = p.y, z = p.z},
  level_vertex = a:level_vertex_id(), game_vertex = a:game_vertex_id(),
  health = a.health, power = a.power, radiation = a.radiation, bleeding = a.bleeding,
  money = a:money(), rank = a:character_rank(), reputation = a:character_reputation(),
  community = a:character_community(),
  name = a:character_name(),
  active_item = w and w:section() or nil,
  game_time = game.get_game_time():dateToString(game.CTime.DateToDay) .. " " ..
              game.get_game_time():timeToString(game.CTime.TimeToMinutes),
  weather = level.get_weather(),
}
if WITH_INV then
  local inv = {}
  a:iterate_inventory(function(_, item)
    local s = item:section()
    inv[s] = (inv[s] or 0) + 1
  end, a)
  r.inventory = inv
end
return r
"""


@mcp.tool()
def game_player(with_inventory: bool = False) -> dict:
    """Игрок: локация, координаты, здоровье, деньги, ранг, группировка, время и погода; по желанию инвентарь."""
    code = PLAYER_LUA.replace("WITH_INV", "true" if with_inventory else "false")
    return lua_result(code)["result"]


@mcp.tool()
def game_spawn(section: str, count: int = 1, where: str = "inventory", distance: float = 3.0) -> dict:
    """Заспавнить секцию: where="inventory" — предмет в рюкзак, "world" — в мир перед игроком
    (так спавнят НПС, мутантов, физобъекты). Секция должна существовать в system.ltx."""
    if where not in ("inventory", "world"):
        raise ToolError('where: "inventory" или "world"')
    code = f"""
local sec = {lua_str(section)}
if not ini_sys:section_exist(sec) then error("нет секции " .. sec) end
local ids = {{}}
for i = 1, {int(count)} do
  local se
  if {lua_str(where)} == "inventory" then
    se = alife_create_item(sec, db.actor)
  else
    local a = db.actor
    local dir = device().cam_dir
    local pos = a:position():add(vector():set(dir.x, 0, dir.z):normalize():mul({float(distance)}))
    local lv = level.vertex_id(pos)
    if not lv or lv >= 4294967295 then lv = a:level_vertex_id() pos = a:position() end
    se = alife_create(sec, level.vertex_position(lv), lv, a:game_vertex_id())
  end
  ids[#ids + 1] = se and se.id or false
end
return ids
"""
    return {"section": section, "ids": lua_result(code)["result"]}


@mcp.tool()
def game_screenshot(max_width: int = 1280, timeout: float = 8.0) -> list:
    """Скриншот игры движком — то, что видит игрок, с HUD. Оригинал переносится в
    папку STALKER_SCREENSHOTS, в ответе уменьшенная копия и путь к оригиналу."""
    dirs = [GAME / "appdata" / "screenshots", MO2 / "overwrite" / "appdata" / "screenshots"]

    def snapshot() -> dict[Path, float]:
        return {f: f.stat().st_mtime for d in dirs if d.is_dir() for f in d.iterdir() if f.is_file()}

    before = snapshot()
    # без аргумента команда молча ничего не делает
    lua_result('get_console():execute("screenshot 0") return true')
    deadline = time.monotonic() + timeout
    new = []
    while time.monotonic() < deadline and not new:
        time.sleep(0.25)
        new = [f for f, m in snapshot().items() if before.get(f) != m]
    if not new:
        raise BridgeError("Скриншот не появился. Проверьте, где движок сохраняет скриншоты.")
    shot = max(new, key=lambda f: f.stat().st_mtime)
    time.sleep(0.3)  # дописывание файла

    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    saved = SCREENSHOTS / shot.name
    shutil.move(shot, saved)

    from PIL import Image as PILImage
    img = PILImage.open(saved).convert("RGB")
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return [Image(data=buf.getvalue(), format="jpeg"), f"Сохранён: {saved}"]


# ---------------------------------------------------------------- tools: логи

def latest_log() -> Path | None:
    dirs = [GAME / "appdata" / "logs", MO2 / "overwrite" / "appdata" / "logs"]
    logs = [f for d in dirs if d.is_dir() for f in d.glob("xray_*.log")]
    return max(logs, key=lambda f: f.stat().st_mtime) if logs else None


@mcp.tool()
def game_log(lines: int = 100, grep: str | None = None) -> str:
    """Хвост лога движка (xray_<user>.log). grep — регулярка, без учёта регистра: оставить только совпадения."""
    log = latest_log()
    if not log:
        return "Лог не найден."
    text = read_text(log).splitlines()
    if grep:
        rx = re.compile(grep, re.I)
        text = [l for l in text if rx.search(l)]
    return f"{log} ({time.ctime(log.stat().st_mtime)})\n" + "\n".join(text[-lines:])


@mcp.tool()
def game_crash() -> str:
    """Блок последнего вылета из лога: FATAL ERROR, выражение, функция, стек Lua."""
    log = latest_log()
    if not log:
        return "Лог не найден."
    text = read_text(log).splitlines()
    idx = max((i for i, l in enumerate(text) if "FATAL ERROR" in l), default=None)
    if idx is None:
        tail = [l for l in text[-300:] if re.search(r"(?i)^!|stack traceback|error", l)]
        return "FATAL ERROR в последнем логе нет. Ошибки и предупреждения из хвоста:\n" + "\n".join(tail[-60:])
    start = max(0, idx - 40)
    return f"{log}\n" + "\n".join(text[start:idx + 80])


# ---------------------------------------------------------------- tools: MO2

@mcp.tool()
def mo2_mods() -> dict:
    """Список модов профиля MO2 по приоритету (первый перекрывает остальных)."""
    mods = read_modlist()
    return {
        "profile": mo2_profile(),
        "mo2_running": "ModOrganizer.exe" in processes(),
        "mods": [{"name": n, "enabled": e, "separator": n.endswith("_separator")} for n, e in mods],
        "installed_not_listed": sorted(
            p.name for p in (MO2 / "mods").iterdir()
            if p.is_dir() and p.name not in {n for n, _ in mods}),
    }


@mcp.tool()
def mo2_set_mod(name: str, enabled: bool, position: int | None = None) -> dict:
    """Включить/выключить мод в профиле MO2; position — новое место в списке (0 = высший приоритет).

    Работает только при закрытом MO2: он перезаписывает modlist.txt при выходе.
    """
    if "ModOrganizer.exe" in processes():
        raise ToolError("MO2 запущен — закройте его, иначе он затрёт изменения при выходе.")
    if not (MO2 / "mods" / name).is_dir():
        raise ToolError(f"Нет мода {name} в {MO2 / 'mods'}")
    path = modlist_path()
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    header = [l for l in lines if l.startswith("#")]
    mods = [(l[1:], l[0] == "+") for l in lines if l[:1] in "+-" and len(l) > 1]
    old = next((i for i, (n, _) in enumerate(mods) if n == name), None)
    if old is not None:
        mods.pop(old)
    pos = position if position is not None else (old if old is not None else 0)
    mods.insert(max(0, min(pos, len(mods))), (name, enabled))
    path.write_text("\n".join(header + [("+" if e else "-") + n for n, e in mods]) + "\n", encoding="utf-8")
    return {"name": name, "enabled": enabled, "position": mods.index((name, enabled))}


@mcp.tool()
def game_launch(save: str | None = None, executable: str = DEFAULT_EXE, extra_args: str = "") -> dict:
    """Запустить игру через MO2 (с модами). save — имя сейва без расширения: игра сразу его загрузит
    (например "player - quicksave_1"). Список сейвов — game_saves."""
    args = extra_args
    if save:
        args = f"-start server({save}/single/alife/load) client(localhost) {args}".strip()
    cmd = [str(MO2 / "ModOrganizer.exe"), "-p", mo2_profile(), "run", "-e", executable]
    if args:
        cmd += ["-a", args]
    ensure_bridge_files()
    subprocess.Popen(cmd, cwd=MO2, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    return {"started": executable, "args": args, "hint": "Загрузка занимает 1–3 минуты; следите через game_status."}


@mcp.tool()
def game_saves() -> list[dict]:
    """Сейвы, свежие первыми."""
    dirs = [GAME / "appdata" / "savedgames", MO2 / "overwrite" / "appdata" / "savedgames"]
    saves = [f for d in dirs if d.is_dir() for f in d.glob("*.scop")]
    saves.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return [{"name": f.stem, "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime))}
            for f in saves]


# ---------------------------------------------------------------- tools: файлы

@mcp.tool()
def files_resolve(game_path: str) -> dict:
    """Кто поставляет файл gamedata (например `scripts\\ui_main_menu.script`): все слои по приоритету,
    первый — тот, что видит игра."""
    rel = norm_game_path(game_path)
    hits = [{"layer": label, "path": str(p / rel)} for label, p in data_layers() if (p / rel).is_file()]
    return {"game_path": rel, "winner": hits[0] if hits else None, "all": hits}


@mcp.tool()
def files_grep(pattern: str, subdir: str = "scripts", glob: str = "*", max_results: int = 200) -> list[str]:
    """Регулярка по действующим файлам игры (с учётом перекрытий модами) в подпапке gamedata.
    Ответ: `путь:строка: текст [слой]`. Для configs и scripts — по всему, что видит игра."""
    rx = re.compile(pattern, re.I)
    sub = norm_game_path(subdir)
    seen: set[str] = set()
    results: list[str] = []
    for label, base in data_layers():
        root = base / sub
        if not root.is_dir():
            continue
        for dirpath, _, files in os.walk(root):
            for fn in files:
                if not fnmatch.fnmatch(fn.lower(), glob.lower()):
                    continue
                full = Path(dirpath) / fn
                rel = str(full.relative_to(base)).lower()
                if rel in seen:
                    continue
                seen.add(rel)
                try:
                    text = read_text(full)
                except OSError:
                    continue
                for no, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        results.append(f"{full.relative_to(base)}:{no}: {line.strip()[:200]}  [{label}]")
                        if len(results) >= max_results:
                            return results
    return results


# ---------------------------------------------------------------- установка мода

def install_mod() -> None:
    """Поставить мод MCP-Bridge в MO2: скрипт из mod/ с путём к папке моста."""
    check_config()
    if "ModOrganizer.exe" in processes():
        raise SystemExit("Закройте MO2: он перезаписывает modlist.txt при выходе.")
    if any(n.lower().startswith("anomaly") for n in processes()):
        raise SystemExit("Закройте игру: скрипт мода подхватится только при новом запуске.")

    bridge_dir = str(BRIDGE).rstrip("\\/") + "\\"
    template = (HERE / "mod" / "mcp_bridge.script").read_text(encoding="utf-8")
    script = template.replace("{{BRIDGE_DIR}}", bridge_dir.replace("\\", "\\\\"))

    mod_dir = MO2 / "mods" / BRIDGE_MOD
    target = mod_dir / "gamedata" / "scripts" / "mcp_bridge.script"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(script.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))
    meta = mod_dir / "meta.ini"
    if not meta.exists():
        meta.write_text("[General]\nmodid=0\nversion=1.0\ncategory=\"\"\ninstallationFile=\n"
                        "comments=Game side of stalker-mcp\n", encoding="utf-8")

    # включить мод в профиле: в конец списка (низший приоритет), если его там нет
    path = modlist_path()
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else ["# This file was automatically generated by Mod Organizer."]
    names = [l[1:] for l in lines if l[:1] in "+-"]
    if BRIDGE_MOD in names:
        lines = [("+" + BRIDGE_MOD) if l[1:] == BRIDGE_MOD and l[:1] in "+-" else l for l in lines]
    else:
        lines.append("+" + BRIDGE_MOD)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ensure_bridge_files()
    print(f"Мод {BRIDGE_MOD} установлен: {target}")
    print(f"Профиль MO2: {mo2_profile()}, папка моста: {BRIDGE}")


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["install"]:
        install_mod()
    else:
        check_config()
        ensure_bridge_files()
        mcp.run()
