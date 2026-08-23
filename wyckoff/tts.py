"""解读语音播报 (TTS): 把 AI 解读 / 分析结论用中文语音读出来。

设计: 多后端插件式, 按可用性自动降级, 未安装任何引擎时优雅返回 (不影响分析):
  1. edge-tts  (首选, 微软在线语音, 中文质量最好, 需 pip install edge-tts + 网络)
  2. pyttsx3   (离线跨平台, 中文音质一般, 需 pip install pyttsx3)
  3. espeak-ng (离线 Linux, 中文较机械)

引擎判定与文本清洗均为纯逻辑 (不依赖音频硬件/网络), 可被单元测试覆盖;
真正发声通过后台线程进行, 支持 stop_event 中途停止, 主界面不阻塞。
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from ._log import log_exc

# ── 支持的 edge-tts 中文音色 (代码 → 显示名) ──
EDGE_VOICES = {
    "zh-CN-XiaoxiaoNeural": "晓晓 (女·甜美)",
    "zh-CN-XiaoyiNeural": "晓伊 (女·亲切)",
    "zh-CN-XiaohanNeural": "晓涵 (女·温暖)",
    "zh-CN-YunxiNeural": "云希 (男·阳光)",
    "zh-CN-YunjianNeural": "云健 (男·浑厚)",
    "zh-CN-YunyangNeural": "云扬 (男·新闻)",
    "zh-CN-liaoning-XiaobeiNeural": "晓北 (东北女)",
    "zh-CN-shaanxi-XiaoniNeural": "晓妮 (陕西女)",
}

# 引擎优先级 (auto 时依次探测)
_ENGINE_PRIORITY = ("edge", "pyttsx3", "espeak")

_MP3_PLAYERS = ("ffplay", "mplayer", "cvlc", "mpv")

# 缓存引擎可用性探测结果 (进程内探测一次即可, 避免每次点播报都 fork 子进程)
_AVAIL_CACHE = {}


def _probe(cmd):
    """探测可执行文件是否存在。"""
    return shutil.which(cmd) is not None


def _have_edge():
    if _AVAIL_CACHE.get("edge_module") is None:
        try:
            import edge_tts  # noqa: F401
            _AVAIL_CACHE["edge_module"] = True
        except Exception:
            _AVAIL_CACHE["edge_module"] = False
    return _AVAIL_CACHE["edge_module"]


def _have_pyttsx3():
    if _AVAIL_CACHE.get("pyttsx3_module") is None:
        try:
            import pyttsx3  # noqa: F401
            _AVAIL_CACHE["pyttsx3_module"] = True
        except Exception:
            _AVAIL_CACHE["pyttsx3_module"] = False
    if not _AVAIL_CACHE["pyttsx3_module"]:
        return False
    # pyttsx3 在 Linux 上依赖 espeak-ng/espeak 系统库, 缺了即使装了模块也无法发声
    if os.name == "nt" or sys.platform == "darwin":
        return True
    if "pyttsx3_linux_espeak" not in _AVAIL_CACHE:
        _AVAIL_CACHE["pyttsx3_linux_espeak"] = _probe("espeak-ng") or _probe("espeak")
    return _AVAIL_CACHE["pyttsx3_linux_espeak"]


def _have_espeak():
    if "espeak" not in _AVAIL_CACHE:
        _AVAIL_CACHE["espeak"] = _probe("espeak-ng") or _probe("espeak")
    return _AVAIL_CACHE["espeak"]


def available_engines():
    """返回可用引擎列表: [("edge", "微软在线语音 (edge-tts, 中文最佳)"), ...]。"""
    out = []
    if _have_edge():
        out.append(("edge", "微软在线语音 (edge-tts, 中文最佳, 需网络)"))
    if _have_pyttsx3():
        out.append(("pyttsx3", "离线语音 (pyttsx3, 跨平台)"))
    if _have_espeak():
        out.append(("espeak", "系统 espeak (离线, 中文较机械)"))
    return out


def best_engine(settings=None):
    """解析 settings['tts_engine'] (auto/edge/pyttsx3/espeak) 为实际可用引擎; 无可用返回 None。"""
    want = (settings or {}).get("tts_engine", "auto")
    avail = dict(available_engines())
    if want in avail:
        return want
    for eng in _ENGINE_PRIORITY:
        if eng in avail:
            return eng
    return None


def is_enabled(settings=None):
    """播报总开关 (设置启用 + 有可用引擎)。"""
    settings = settings or {}
    if not settings.get("tts_enabled", False):
        return False
    return best_engine(settings) is not None


# ── 文本清洗: 把富文本/解读转成适合朗读的纯文本 ──
_STRIP_MD = re.compile(r"`{1,3}|[*_>#]{1,3}|^[-+]\s+", re.MULTILINE)


def clean_speech_text(text):
    """清洗朗读文本: 去掉 markdown 标记/【】/多余空白, 保留正文内容。

    - 中文括号内容 (术语注释) 保留, 只去除括号本身对朗读无意义的部分不做处理;
    - 统一为无 markdown 的连续段落, 空行保留作为停顿。
    """
    text = (text or "").strip()
    if not text:
        return ""
    text = _STRIP_MD.sub("", text)
    text = text.replace("【", "，").replace("】", "，")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"[ ]*\n[ ]*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _edge_rate(rate_pct):
    """edge-tts rate 参数 (如 -10% / +25%), 限制在 ±50%。"""
    try:
        r = int(float(rate_pct))
    except (TypeError, ValueError):
        r = 0
    r = max(-50, min(50, r))
    return f"{'+' if r >= 0 else ''}{r}%"


# edge-tts 单次合成上限 (微软服务按次限长, 超长文本需分块连续朗读)
_EDGE_CHUNK_CHARS = 3000
# 句子结束符: 分块时优先在此处断开, 避免把一句话腰斩
_SENT_BOUND = re.compile(r"(?<=[。！？；…\n])")


def chunk_speech_text(text, max_chars=_EDGE_CHUNK_CHARS):
    """把长文本按句子边界切分为不超过 max_chars 的块。

    供 edge-tts 分块合成, 保证超长解读/结论也能完整朗读而不被服务限长截断。
    单句本身超过 max_chars 时做硬切兜底。
    """
    text = (text or "").strip()
    if len(text) <= max_chars:
        return [text]
    chunks = []
    cur = ""
    for part in _SENT_BOUND.split(text):
        if not part:
            continue
        if len(cur) + len(part) <= max_chars:
            cur += part
        else:
            if cur:
                chunks.append(cur)
                cur = ""
            while len(part) > max_chars:
                chunks.append(part[:max_chars])
                part = part[max_chars:]
            cur = part
    if cur:
        chunks.append(cur)
    return chunks


def parse_engine_choice(sel):
    """把设置对话框 '引擎' 下拉选中项解析为引擎键 (auto/edge/pyttsx3/espeak)。"""
    eng = (sel or "").split(" - ", 1)[0]
    if eng in ("auto", "auto (自动选择)", "auto (无可用引擎)"):
        return "auto"
    return eng


def parse_voice_choice(sel):
    """把设置对话框 '音色' 下拉选中项解析为 edge-tts 音色代码。"""
    sel = sel or ""
    code = sel.split(" ")[0]
    return code if code in EDGE_VOICES else "zh-CN-XiaoxiaoNeural"


def _play_mp3(path, stop=None):
    """用系统播放器播放 mp3; 返回是否成功。

    stop: 可选 threading.Event, 置位时立即终止播放器进程 (修复: 原阻塞
    subprocess.run 不感知停止事件, 点"停止播报"后 ffplay 仍把整段 mp3 放完,
    长解读 (3~5分钟) 会让人以为"一直响"; 且新播报启动时旧进程继续发声,
    导致两条语音叠播)。
    """
    stop = stop or threading.Event()
    if os.name == "nt":
        os.startfile(path)  # pragma: no cover
        return True
    for player in _MP3_PLAYERS:
        if _probe(player):
            cmd = [player, "-nodisp", "-autoexit", path] if player == "ffplay" \
                else [player, path]
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                while proc.poll() is None:
                    if stop.is_set():
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                        except Exception:
                            proc.kill()
                        return False
                    time.sleep(0.2)
                return True
            except Exception as e:
                log_exc(f"tts 播放器 {player} 播放失败", e)
                continue
    try:
        import pygame  # noqa: F401
        pygame.mixer.init()
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            if stop.is_set():
                pygame.mixer.music.stop()
                return False
            time.sleep(0.1)
        return True
    except Exception:
        return False


def _speak_edge(text, voice, rate_pct, stop):
    """edge-tts 后端: 在线分块合成 mp3 后顺序播放。返回错误信息或 None。

    文本超过单次合成上限时按句子切块, 依次合成并连续播放, 保证完整朗读。
    """
    if not _have_edge():
        return "edge-tts 未安装 (pip install edge-tts)"
    try:
        import asyncio

        import edge_tts

        async def _synth_chunk(chunk):
            if stop.is_set():
                return None
            # mkstemp 原子创建避免 mktemp 竞态 (mktemp 可能指向他人已占用的文件)
            fd, tmp = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)
            try:
                communicate = edge_tts.Communicate(chunk, voice,
                                                   rate=_edge_rate(rate_pct))
                await communicate.save(tmp)
                return tmp
            except Exception:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise

        async def _synth_all():
            out = []
            for chunk in chunk_speech_text(text):
                if stop.is_set():
                    break
                out.append(await _synth_chunk(chunk))
            return out

        paths = asyncio.run(_synth_all())
        paths = [p for p in paths if p]
        if not paths:
            return None if stop.is_set() else "语音合成失败 (网络或服务异常)"
        try:
            for p in paths:
                if stop.is_set():
                    return None
                ok = _play_mp3(p, stop)
                if not ok:
                    return "无可用播放器播放语音 (需 ffplay/mplayer/vlc)"
        finally:
            for p in paths:
                try:
                    os.remove(p)
                except OSError:
                    pass
        return None if not stop.is_set() else None
    except Exception as e:
        log_exc("tts edge 合成失败", e)
        return f"edge-tts 播报失败: {e}"


def _speak_pyttsx3(text, rate_pct, stop):
    """pyttsx3 后端: 离线本地合成并播放。"""
    engine = None
    try:
        import pyttsx3
        engine = pyttsx3.init()
        base = engine.getProperty("rate")
        try:
            pct = int(float(rate_pct))
        except (TypeError, ValueError):
            pct = 0
        engine.setProperty("rate", max(80, int(base * (1 + max(-50, min(50, pct)) / 100))))
        engine.say(text)
        engine.runAndWait()
        engine.stop()
        return None
    except Exception as e:
        if stop.is_set():
            return None
        log_exc("tts pyttsx3 播报失败", e)
        return f"pyttsx3 播报失败: {e}"
    finally:
        if engine is not None:
            try:
                engine.stop()
            except Exception:
                pass


def _speak_espeak(text, rate_pct, stop):
    """espeak-ng/espeak 后端: 命令行合成, 中文用 zh 音色。"""
    try:
        try:
            pct = int(float(rate_pct))
        except (TypeError, ValueError):
            pct = 0
        words = 175
        words = max(100, min(320, int(words * (1 + max(-50, min(50, pct)) / 100))))
        if _probe("espeak-ng"):
            cmd = ["espeak-ng", "-v", "zh+f3", "-s", str(words), text]
        else:
            cmd = ["espeak", "-v", "zh", "-s", str(words), text]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        while proc.poll() is None:
            if stop.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
                return None
            time.sleep(0.2)
        return None
    except Exception as e:
        if stop.is_set():
            return None
        log_exc("tts espeak 播报失败", e)
        return f"espeak 播报失败: {e}"


def _speak_worker(text, settings, stop):
    """后台朗读线程体: 返回 (成功与否, 错误信息)。

    首选引擎失败时自动降级到下一个可用引擎 (如 edge 网络失败 → pyttsx3/espeak
    离线), 保证'语音播报'按钮点了就一定有声音/明确错误, 而不是静默无声。
    """
    engine = best_engine(settings)
    if engine is None:
        return False, "未安装任何语音引擎 (需 edge-tts 或 pyttsx3)"
    if stop.is_set():
        return False, None
    rate = (settings or {}).get("tts_rate", 0)
    voice = (settings or {}).get("tts_voice", "zh-CN-XiaoxiaoNeural")
    failures = []
    tried = set()
    # 优先尝试配置/auto 选出的引擎, 失败后按可用性依次降级
    order = [engine] + [e for e, _ in available_engines() if e != engine]
    for eng in order:
        if eng in tried:
            continue
        tried.add(eng)
        if stop.is_set():
            return False, None
        if eng == "edge":
            err = _speak_edge(text, voice, rate, stop)
        elif eng == "pyttsx3":
            err = _speak_pyttsx3(text, rate, stop)
        else:
            err = _speak_espeak(text, rate, stop)
        if stop.is_set():
            return False, None
        if err is None:
            return True, None
        failures.append(f"{eng}: {err}")
    return False, "；".join(failures)


# ── 线程安全的单播报控制 ──
_lock = threading.Lock()
_current = {"stop": None, "thread": None}


def speak(text, settings=None, on_done=None):
    """非阻塞播报。text 为要朗读的内容; on_done(ok, err) 在主线程回调。

    已有播报进行时先停止旧的再开始新的。返回是否已启动。
    """
    text = clean_speech_text(text)
    if not text:
        if on_done:
            on_done(False, "无内容可播报")
        return False
    if best_engine(settings) is None:
        if on_done:
            on_done(False, "未安装任何语音引擎 (需 edge-tts 或 pyttsx3)")
        return False
    stop = threading.Event()
    with _lock:
        old = _current.get("stop")
        if old is not None:
            old.set()
        _current["stop"] = stop
        _current["thread"] = threading.Thread(
            target=_run_thread, args=(text, settings, stop, on_done),
            daemon=True)
        _current["thread"].start()
    return True


def _run_thread(text, settings, stop, on_done):
    ok, err = _speak_worker(text, settings, stop)
    with _lock:
        if _current.get("stop") is stop:
            _current["stop"] = None
            _current["thread"] = None
    if on_done is not None:
        try:
            on_done(ok, err)
        except Exception as e:
            log_exc("tts on_done 回调失败", e)


def speak_sequence(parts, settings=None, on_done=None):
    """顺序朗读多段文本 (如结论的每个标签一节): 一段完整读完再播下一段。

    parts: [(label, text), ...] 或 [text, ...]。label 非空时先朗读标签名再读正文。
    中间某段失败 (如网络中断) 时停止后续。返回是否已启动。
    """
    cleaned = []
    for p in parts:
        label, text = "", p
        if isinstance(p, (tuple, list)) and len(p) == 2:
            label, text = p[0], p[1]
        text = clean_speech_text(text)
        if not text:
            continue
        label = clean_speech_text(label)
        cleaned.append(label + "。" + text if label else text)
    if not cleaned:
        if on_done:
            on_done(False, "无内容可播报")
        return False
    if best_engine(settings) is None:
        if on_done:
            on_done(False, "未安装任何语音引擎 (需 edge-tts 或 pyttsx3)")
        return False
    stop = threading.Event()
    with _lock:
        old = _current.get("stop")
        if old is not None:
            old.set()
        _current["stop"] = stop
        _current["thread"] = threading.Thread(
            target=_run_sequence_thread, args=(cleaned, settings, stop, on_done),
            daemon=True)
        _current["thread"].start()
    return True


def _run_sequence_thread(parts, settings, stop, on_done):
    ok, err = True, None
    for text in parts:
        if stop.is_set():
            ok, err = False, None
            break
        ok, err = _speak_worker(text, settings, stop)
        if not ok:
            break
    with _lock:
        if _current.get("stop") is stop:
            _current["stop"] = None
            _current["thread"] = None
    if on_done is not None:
        try:
            on_done(ok, err)
        except Exception as e:
            log_exc("tts on_done 回调失败", e)


def stop():
    """停止当前播报 (如有)。"""
    with _lock:
        s = _current.get("stop")
        if s is not None:
            s.set()
        _current["stop"] = None
        _current["thread"] = None


def is_speaking():
    """是否正在播报。"""
    with _lock:
        return _current.get("thread") is not None \
            and _current["thread"].is_alive()


def speak_sync(text, settings=None):
    """同步播报 (供测试/命令行), 返回 (ok, err)。"""
    return _speak_worker(clean_speech_text(text), settings, threading.Event())
