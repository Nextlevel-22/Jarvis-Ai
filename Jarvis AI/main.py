"""VoiceLaunch: a cross-platform, installed-app-only voice launcher.

VoiceLaunch discovers launchable applications on the current computer, caches the
result, and accepts spoken open, switch, and close commands. It intentionally has
no URL or web-search fallback.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional

try:
    import psutil
except ImportError:  # A friendly startup message is printed in main().
    psutil = None  # type: ignore[assignment]

try:
    import pyaudio
except ImportError:
    pyaudio = None  # type: ignore[assignment]

try:
    import sounddevice as sd
except ImportError:
    sd = None  # type: ignore[assignment]

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None  # type: ignore[assignment]

try:
    import speech_recognition as sr
except ImportError:
    sr = None  # type: ignore[assignment]

try:
    from rapidfuzz import fuzz, process
except ImportError:
    fuzz = None  # type: ignore[assignment]
    process = None  # type: ignore[assignment]


OS_NAME = platform.system()  # Detect once: "Windows", "Linux", or "Darwin".
CACHE_VERSION = 1
CACHE_FILE = Path(__file__).resolve().with_name("app_cache.json")
MATCH_THRESHOLD = 68.0
AMBIGUITY_MARGIN = 5.0

AppList = Dict[str, str]
_tts_engine = None


class _SoundDeviceStreamAdapter:
    """Expose sounddevice's blocking stream in SpeechRecognition's format."""

    def __init__(self, raw_stream: object) -> None:
        self.raw_stream = raw_stream

    def read(self, size: int) -> bytes:
        data, overflowed = self.raw_stream.read(size)  # type: ignore[attr-defined]
        if overflowed:
            print("[audio] Warning: microphone input overflowed.")
        return bytes(data)


class SoundDeviceMicrophone(sr.AudioSource if sr is not None else object):
    """SpeechRecognition AudioSource using the default sounddevice microphone.

    This is the Python 3.14-compatible fallback for platforms where PyAudio has
    no prebuilt wheel. Recognizer.adjust_for_ambient_noise() and
    Recognizer.listen() consume it exactly like sr.Microphone.
    """

    def __init__(self, sample_rate: Optional[int] = None, chunk_size: int = 1024):
        if sr is None or sd is None:
            raise RuntimeError("SpeechRecognition and sounddevice are required")
        if sample_rate is None:
            device_info = sd.query_devices(kind="input")
            sample_rate = int(device_info["default_samplerate"])
        self.SAMPLE_RATE = sample_rate
        self.SAMPLE_WIDTH = 2  # signed 16-bit PCM
        self.CHUNK = chunk_size
        self.stream = None
        self._raw_stream = None

    def __enter__(self) -> "SoundDeviceMicrophone":
        if self.stream is not None:
            raise RuntimeError("The microphone source is already active")
        self._raw_stream = sd.RawInputStream(
            samplerate=self.SAMPLE_RATE,
            blocksize=self.CHUNK,
            device=None,  # None selects the operating system's default microphone.
            channels=1,
            dtype="int16",
        )
        self._raw_stream.start()
        self.stream = _SoundDeviceStreamAdapter(self._raw_stream)
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self._raw_stream is not None:
            self._raw_stream.stop()
            self._raw_stream.close()
        self.stream = None
        self._raw_stream = None


@dataclass(frozen=True)
class AppMatch:
    """The result of comparing a spoken target with discovered apps."""

    selected: Optional[str]
    alternatives: tuple[str, ...] = ()
    scores: tuple[tuple[str, float], ...] = ()


def _normalise(text: str) -> str:
    """Normalise names while retaining useful words and digits."""

    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def _add_app(apps: AppList, name: str, launch_value: str) -> None:
    """Add a usable, non-web app without overwriting a better earlier entry."""

    clean_name = " ".join(name.split()).strip()
    launch_value = launch_value.strip()
    if not clean_name or not launch_value:
        return
    # Reject URL-bearing launchers as well as raw URLs. This prevents Linux
    # desktop entries such as `xdg-open https://...` from entering the catalog.
    if re.search(r"(?:^|[\s=])https?://", launch_value, flags=re.IGNORECASE):
        return
    existing = {_normalise(item): item for item in apps}
    if _normalise(clean_name) not in existing:
        apps[clean_name] = launch_value


def _scan_windows() -> AppList:
    """Scan per-user and machine-wide Start Menu .lnk shortcuts.

    A .lnk is itself a valid launch target on Windows, so it is safer than
    guessing executables from uninstall records. Registry entries with a real
    DisplayIcon executable are added as a secondary source.
    """

    apps: AppList = {}
    program_data = os.environ.get("PROGRAMDATA")
    app_data = os.environ.get("APPDATA")
    roots = []
    if app_data:
        roots.append(Path(app_data) / "Microsoft/Windows/Start Menu/Programs")
    if program_data:
        roots.append(Path(program_data) / "Microsoft/Windows/Start Menu/Programs")

    for root in roots:
        if not root.is_dir():
            continue
        try:
            for shortcut in root.rglob("*.lnk"):
                # Shortcut filename is the display name shown in the Start Menu.
                _add_app(apps, shortcut.stem, str(shortcut.resolve()))
        except OSError as exc:
            print(f"[scan] Could not fully scan {root}: {exc}")

    # Some installed programs do not create Start Menu shortcuts. Include only
    # registry records whose DisplayIcon points to an existing local executable.
    try:
        import winreg

        uninstall_keys = (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        )
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for key_path in uninstall_keys:
                try:
                    key = winreg.OpenKey(hive, key_path)
                except OSError:
                    continue
                with key:
                    for index in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            subkey_name = winreg.EnumKey(key, index)
                            with winreg.OpenKey(key, subkey_name) as subkey:
                                name = str(winreg.QueryValueEx(subkey, "DisplayName")[0])
                                icon = str(winreg.QueryValueEx(subkey, "DisplayIcon")[0])
                            executable = _display_icon_executable(icon)
                            if executable and executable.is_file():
                                _add_app(apps, name, str(executable))
                        except OSError:
                            continue
    except (ImportError, OSError) as exc:
        print(f"[scan] Registry scan skipped: {exc}")

    return apps


def _display_icon_executable(value: str) -> Optional[Path]:
    """Extract an executable path from a Windows DisplayIcon registry value."""

    value = os.path.expandvars(value.strip())
    # DisplayIcon commonly ends in an icon resource index such as ",0".
    value = re.sub(r",\s*-?\d+\s*$", "", value).strip().strip('"')
    candidate = Path(value)
    return candidate if candidate.suffix.casefold() in {".exe", ".com"} else None


def _desktop_entry(path: Path) -> Optional[tuple[str, str]]:
    """Read the unlocalised Name and Exec fields from a Linux .desktop file."""

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"[scan] Could not read {path}: {exc}")
        return None

    in_desktop_entry = False
    fields: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_desktop_entry = line == "[Desktop Entry]"
            continue
        if not in_desktop_entry or "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key in {"Name", "Exec", "Type", "Hidden"} and key not in fields:
            fields[key] = value.strip()

    if (
        fields.get("Type", "Application") != "Application"
        or fields.get("Hidden", "false").casefold() == "true"
        or not fields.get("Name")
        or not fields.get("Exec")
    ):
        return None

    command = _clean_desktop_exec(fields["Exec"])
    return (fields["Name"], command) if command else None


def _clean_desktop_exec(command: str) -> str:
    """Remove freedesktop argument field codes from an Exec command."""

    # %% means a literal percent. Other %X values are placeholders for files,
    # URLs, icons, or translated names and do not belong in a plain launch.
    marker = "__VOICELAUNCH_PERCENT__"
    command = command.replace("%%", marker)
    command = re.sub(r"%[a-zA-Z]", "", command)
    return " ".join(command.replace(marker, "%").split())


def _scan_linux() -> AppList:
    """Scan system and user freedesktop .desktop application entries."""

    apps: AppList = {}
    roots = (Path.home() / ".local/share/applications", Path("/usr/share/applications"))
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for desktop_file in root.glob("*.desktop"):
                entry = _desktop_entry(desktop_file)
                if entry:
                    _add_app(apps, entry[0], entry[1])
        except OSError as exc:
            print(f"[scan] Could not fully scan {root}: {exc}")
    return apps


def _scan_macos() -> AppList:
    """Scan machine-wide and per-user .app bundles on macOS."""

    apps: AppList = {}
    for root in (Path.home() / "Applications", Path("/Applications")):
        if not root.is_dir():
            continue
        try:
            # rglob includes apps grouped inside subfolders under /Applications.
            for bundle in root.rglob("*.app"):
                _add_app(apps, bundle.stem, str(bundle.resolve()))
        except OSError as exc:
            print(f"[scan] Could not fully scan {root}: {exc}")
    return apps


def _load_cache() -> Optional[AppList]:
    try:
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if payload.get("version") != CACHE_VERSION or payload.get("os") != OS_NAME:
            return None
        apps = payload.get("apps")
        if not isinstance(apps, dict) or not apps:
            return None
        return {str(name): str(command) for name, command in apps.items()}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _save_cache(apps: AppList) -> None:
    payload = {
        "version": CACHE_VERSION,
        "os": OS_NAME,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "apps": dict(sorted(apps.items(), key=lambda item: item[0].casefold())),
    }
    temporary = CACHE_FILE.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, CACHE_FILE)
    except OSError as exc:
        print(f"[cache] Could not save app cache: {exc}")


def scan_installed_apps(force_refresh: bool = False) -> AppList:
    """Return installed app display names mapped to local launch paths/commands."""

    if not force_refresh:
        cached = _load_cache()
        if cached is not None:
            print(f"[cache] Loaded {len(cached)} applications from {CACHE_FILE}")
            return cached

    print(f"[scan] Discovering installed applications on {OS_NAME}...")
    if OS_NAME == "Windows":
        apps = _scan_windows()
    elif OS_NAME == "Linux":
        apps = _scan_linux()
    elif OS_NAME == "Darwin":
        apps = _scan_macos()
    else:
        print(f"[scan] Unsupported operating system: {OS_NAME}")
        return {}

    apps = dict(sorted(apps.items(), key=lambda item: item[0].casefold()))
    if apps:
        _save_cache(apps)
    print(f"[scan] Found {len(apps)} launchable applications.")
    return apps


def speak(text: str) -> None:
    """Print feedback and speak it through the offline pyttsx3 engine."""

    global _tts_engine
    print(f"VoiceLaunch: {text}")
    if pyttsx3 is None:
        return
    try:
        if _tts_engine is None:
            _tts_engine = pyttsx3.init()
        _tts_engine.say(text)
        _tts_engine.runAndWait()
    except Exception as exc:  # Audio drivers vary substantially by platform.
        print(f"[tts] Speech output failed: {exc}")
        _tts_engine = None


def listen_for_command(
    recognizer: Optional["sr.Recognizer"] = None,
    *,
    timeout: Optional[float] = None,
    phrase_time_limit: float = 8.0,
) -> Optional[str]:
    """Listen with the default microphone and transcribe through Google STT."""

    if sr is None:
        print("[audio] SpeechRecognition is not installed.")
        return None
    recognizer = recognizer or sr.Recognizer()
    try:
        # SpeechRecognition's Microphone requires PyAudio. sounddevice provides
        # the same AudioSource contract when a PyAudio wheel is unavailable.
        microphone = sr.Microphone() if pyaudio is not None else SoundDeviceMicrophone()
        backend = "PyAudio" if pyaudio is not None else "sounddevice"
        with microphone as source:
            print(f"[audio] Using {backend} with the default microphone.")
            print("Listening...")
            recognizer.adjust_for_ambient_noise(source, duration=0.7)
            audio = recognizer.listen(
                source, timeout=timeout, phrase_time_limit=phrase_time_limit
            )
        print("[audio] Recognizing speech...")
        text = recognizer.recognize_google(audio).strip()
        print(f"[heard] {text}")
        return text
    except sr.WaitTimeoutError:
        print("[audio] No speech detected before timeout.")
    except sr.UnknownValueError:
        speak("I couldn't understand that. Please try again.")
    except sr.RequestError as exc:
        speak("The speech recognition service is unavailable right now.")
        print(f"[audio] Google speech recognition error: {exc}")
    except (OSError, AttributeError) as exc:
        speak("I couldn't access the default microphone.")
        print(f"[audio] Microphone error: {exc}")
    except Exception as exc:
        # PortAudio errors differ between the PyAudio and sounddevice backends.
        speak("I couldn't access the default microphone.")
        print(f"[audio] Microphone error ({type(exc).__name__}): {exc}")
    return None


def parse_intent(text: str, app_list: AppList) -> tuple[str, str]:
    """Extract an open/close/switch action and target app from speech."""

    del app_list  # Kept in the public signature for callers and future grammars.
    cleaned = _normalise(text)
    action = "open"

    action_patterns = (
        ("close", r"\b(?:close|quit|terminate|exit from)\b"),
        ("switch", r"\b(?:switch to|switch|focus|go to)\b"),
        ("open", r"\b(?:open|launch|start|run)\b"),
    )
    earliest: Optional[tuple[int, str, re.Match[str]]] = None
    for candidate, pattern in action_patterns:
        found = re.search(pattern, cleaned)
        if found and (earliest is None or found.start() < earliest[0]):
            earliest = (found.start(), candidate, found)
    if earliest:
        action = earliest[1]
        cleaned = (cleaned[: earliest[2].start()] + " " + cleaned[earliest[2].end() :]).strip()

    # Remove conversational padding without removing words from the app name's
    # middle. Leading/trailing "app" is likewise not useful for fuzzy matching.
    cleaned = re.sub(
        r"^(?:(?:hey|please|could you|would you|can you|will you)\s+)*", "", cleaned
    ).strip()
    cleaned = re.sub(r"\s+(?:please|for me|now)$", "", cleaned).strip()
    cleaned = re.sub(r"^(?:the\s+)?(?:app|application)\s+", "", cleaned).strip()
    cleaned = re.sub(r"\s+(?:app|application)$", "", cleaned).strip()
    return action, cleaned


def _similarity(query: str, choice: str) -> float:
    if fuzz is not None:
        return float(fuzz.WRatio(query, choice))
    # Keep a standard-library fallback so a missing optional matcher is graceful.
    import difflib

    return difflib.SequenceMatcher(None, query, choice).ratio() * 100.0


def match_app(spoken_name: str, app_list: AppList) -> AppMatch:
    """Fuzzy-match a spoken target and flag genuinely ambiguous top matches."""

    query = _normalise(spoken_name)
    if not query or not app_list:
        return AppMatch(None)

    normalised_names = {_normalise(name): name for name in app_list}
    if query in normalised_names:
        name = normalised_names[query]
        return AppMatch(name, scores=((name, 100.0),))

    if process is not None and fuzz is not None:
        raw = process.extract(query, list(app_list), scorer=fuzz.WRatio, limit=5)
        scored = [(str(name), float(score)) for name, score, _ in raw]
    else:
        scored = sorted(
            ((name, _similarity(query, _normalise(name))) for name in app_list),
            key=lambda item: item[1],
            reverse=True,
        )[:5]

    eligible = [(name, score) for name, score in scored if score >= MATCH_THRESHOLD]
    if not eligible:
        return AppMatch(None, scores=tuple(scored))

    top_score = eligible[0][1]
    alternatives = tuple(
        name for name, score in eligible if top_score - score <= AMBIGUITY_MARGIN
    )
    if len(alternatives) > 1:
        return AppMatch(None, alternatives=alternatives, scores=tuple(scored))
    return AppMatch(eligible[0][0], scores=tuple(scored))


def _linux_command(command: str) -> list[str]:
    """Convert a desktop Exec string to argv without invoking a shell."""

    parts = shlex.split(command, posix=True)
    if not parts:
        raise ValueError("The desktop entry has an empty Exec command")
    # The desktop spec permits leading NAME=value environment assignments.
    while parts and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", parts[0]):
        parts.pop(0)
    if parts and parts[0] == "env":
        parts.pop(0)
        while parts and (parts[0].startswith("-") or "=" in parts[0]):
            parts.pop(0)
    if not parts:
        raise ValueError("The desktop entry does not contain an executable")
    return parts


def launch_app(app_info: str) -> tuple[bool, str]:
    """Launch one discovered app using only its local path or Exec command."""

    try:
        if OS_NAME == "Windows":
            path = Path(app_info)
            if not path.exists():
                raise FileNotFoundError(path)
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif OS_NAME == "Linux":
            subprocess.Popen(
                _linux_command(app_info),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        elif OS_NAME == "Darwin":
            bundle = Path(app_info)
            if not bundle.is_dir() or bundle.suffix.casefold() != ".app":
                raise FileNotFoundError(bundle)
            subprocess.Popen(
                ["open", "-a", str(bundle)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            raise RuntimeError(f"Unsupported operating system: {OS_NAME}")
        return True, ""
    except (OSError, ValueError, RuntimeError) as exc:
        return False, str(exc)


def _process_aliases(app_name: str, app_info: Optional[str]) -> set[str]:
    aliases = {_normalise(app_name), _normalise(Path(app_name).stem)}
    if app_info:
        if OS_NAME == "Linux":
            try:
                executable = Path(_linux_command(app_info)[0]).name
                aliases.add(_normalise(Path(executable).stem))
            except (ValueError, OSError):
                pass
        elif OS_NAME == "Windows" and Path(app_info).suffix.casefold() == ".lnk":
            # If pywin32 is available, resolve the shortcut so a display name
            # such as "Visual Studio Code" can match a process such as Code.exe.
            try:
                from win32com.client import Dispatch

                shortcut = Dispatch("WScript.Shell").CreateShortCut(app_info)
                target = str(shortcut.Targetpath or "")
                if target:
                    aliases.add(_normalise(Path(target).stem))
            except (ImportError, OSError, AttributeError):
                pass
        elif Path(app_info).suffix.casefold() in {".exe", ".com", ".app"}:
            aliases.add(_normalise(Path(app_info).stem))
    return {alias for alias in aliases if len(alias) >= 2}


def close_app(app_name: str, app_info: Optional[str] = None) -> tuple[bool, str]:
    """Gracefully terminate processes whose executable/title matches the app."""

    if psutil is None:
        return False, "psutil is not installed"
    aliases = _process_aliases(app_name, app_info)
    matches = []
    try:
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            try:
                names = {
                    _normalise(Path(str(proc.info.get("name") or "")).stem),
                    _normalise(Path(str(proc.info.get("exe") or "")).stem),
                }
                # Exact aliases avoid terminating an unrelated similarly named app.
                if aliases.intersection(names):
                    matches.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
    except (psutil.Error, OSError) as exc:
        return False, str(exc)

    if not matches:
        return False, "no matching running process was found"

    terminated = 0
    errors = []
    for proc in matches:
        try:
            proc.terminate()
            terminated += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error) as exc:
            errors.append(str(exc))
    if terminated:
        return True, f"sent a terminate request to {terminated} process(es)"
    return False, "; ".join(errors) or "the process could not be terminated"


def _choose_alternative(
    alternatives: Iterable[str], recognizer: "sr.Recognizer"
) -> Optional[str]:
    options = tuple(alternatives)
    joined = " or ".join(options)
    speak(f"Did you mean {joined}?")
    answer = listen_for_command(recognizer, timeout=7, phrase_time_limit=5)
    if not answer:
        return None
    normalised = _normalise(answer)
    ordinals = {"first": 0, "one": 0, "second": 1, "two": 1, "third": 2, "three": 2}
    for word, index in ordinals.items():
        if word in normalised.split() and index < len(options):
            return options[index]
    scores = sorted(
        ((option, _similarity(normalised, _normalise(option))) for option in options),
        key=lambda item: item[1],
        reverse=True,
    )
    return scores[0][0] if scores and scores[0][1] >= 55 else None


def _missing_dependencies() -> list[str]:
    missing = []
    if sr is None:
        missing.append("SpeechRecognition")
    if pyaudio is None and sd is None:
        missing.append("PyAudio or sounddevice")
    if pyttsx3 is None:
        missing.append("pyttsx3")
    if psutil is None:
        missing.append("psutil")
    return missing


def main() -> None:
    """Run the continuous VoiceLaunch listen/parse/execute loop."""

    if OS_NAME not in {"Windows", "Linux", "Darwin"}:
        print(f"VoiceLaunch does not support {OS_NAME}.")
        return
    missing = _missing_dependencies()
    if missing:
        print("Missing dependencies: " + ", ".join(missing))
        print(
            f"Install them with: {sys.executable} -m pip install "
            "-r requirements.txt"
        )
        return

    apps = scan_installed_apps()
    if not apps:
        speak("I couldn't find any launchable applications on this device.")
        return

    recognizer = sr.Recognizer()
    speak(f"VoiceLaunch is ready. I found {len(apps)} installed applications.")

    while True:
        text = listen_for_command(recognizer)
        if not text:
            continue
        normalised = _normalise(text)

        if normalised in {"exit", "quit", "stop", "stop listening", "goodbye"}:
            speak("Stopping VoiceLaunch. Goodbye.")
            break
        if normalised in {
            "refresh apps",
            "refresh applications",
            "rescan apps",
            "rescan applications",
        }:
            speak("Refreshing the installed application list.")
            apps = scan_installed_apps(force_refresh=True)
            speak(f"Refresh complete. I found {len(apps)} applications.")
            continue

        action, target = parse_intent(text, apps)
        print(f"[intent] action={action!r}, target={target!r}")
        if not target:
            speak("Please say the name of an application.")
            continue

        result = match_app(target, apps)
        print(f"[match] candidates={result.scores}")
        selected = result.selected
        if result.alternatives:
            selected = _choose_alternative(result.alternatives, recognizer)
            if selected is None:
                speak("I couldn't determine which application you meant.")
                continue
        if selected is None:
            speak("That app isn't installed on this device.")
            continue

        app_info = apps[selected]
        print(f"[match] selected={selected!r}, launch_value={app_info!r}")
        if action == "close":
            success, detail = close_app(selected, app_info)
            if success:
                speak(f"Closing {selected} now.")
                print(f"[close] {detail}")
            else:
                speak(f"I couldn't close {selected}.")
                print(f"[close] {detail}")
            continue

        success, error = launch_app(app_info)
        if success:
            verb = "Switching to" if action == "switch" else "Opening"
            speak(f"{verb} {selected} now.")
            print(f"[launch] Successfully invoked {app_info!r}")
        else:
            speak(f"I couldn't launch {selected}.")
            print(f"[launch] Failed: {error}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nVoiceLaunch stopped.")
