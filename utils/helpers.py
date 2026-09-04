import datetime
import importlib.util
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.request
import json


def format_duration(seconds):
    """Return mm:ss or H:MM:SS if > 1 hour."""
    if not seconds:
        return "--:--"
    try:
        secs = int(seconds)
    except Exception:
        return "--:--"
    if secs >= 3600:
        return str(datetime.timedelta(seconds=secs))
    return f"{secs // 60:02d}:{secs % 60:02d}"


def check_ffmpeg():
    """Return True if ffmpeg is found on PATH, False otherwise."""
    try:
        proc = subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      text=True, timeout=5)
        return proc.returncode == 0
    except FileNotFoundError:
        return False
    except Exception:
        return False


def check_ytdlp_version():
    """Return the yt-dlp version imported by the current Python interpreter."""
    try:
        spec = importlib.util.find_spec('yt_dlp')
        if not spec:
            return None
        module = __import__('yt_dlp')
        return getattr(module, '__version__', None)
    except Exception:
        return None


def _parse_version(version):
    if not version:
        return ()
    return tuple(int(part) for part in re.findall(r'\d+', version)[:3])


def _normalize_version(version):
    if not version:
        return ''
    return '.'.join(str(int(part)) for part in re.findall(r'\d+', version)[:3])


def _run_version_command(command, arguments):
    executable = shutil.which(command)
    result = {
        'installed': executable is not None,
        'path': executable,
        'version': None,
        'error': None,
    }
    if not executable:
        result['error'] = f"{command} not found on PATH"
        return result
    try:
        proc = subprocess.run([executable, *arguments], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=5)
        output = (proc.stdout or proc.stderr or '').strip()
        if proc.returncode != 0:
            result['error'] = output or f"{command} exited with code {proc.returncode}"
        else:
            result['version'] = output.splitlines()[0] if output else None
    except (OSError, subprocess.SubprocessError) as error:
        result['error'] = str(error)
    return result


def get_latest_ytdlp_version(timeout=3):
    """Return the latest yt-dlp version known by PyPI, or None on failure."""
    try:
        with urllib.request.urlopen('https://pypi.org/pypi/yt-dlp/json', timeout=timeout) as response:
            data = json.load(response)
        return data.get('info', {}).get('version')
    except (OSError, ValueError, KeyError):
        return None


def get_environment_diagnostics(include_latest=True):
    """Return dependency diagnostics for yt-dlp and FFmpeg."""
    ytdlp = _run_version_command('yt-dlp', ['--version'])
    ytdlp['import_available'] = False
    ytdlp['module_path'] = None
    ytdlp['system_install'] = False
    ytdlp['minimum_version'] = '2024.01.01'
    base_install = False
    try:
        spec = importlib.util.find_spec('yt_dlp')
        ytdlp['import_available'] = spec is not None
        ytdlp['module_path'] = spec.origin if spec else None
        if spec and spec.origin:
            ytdlp['path'] = spec.origin
            ytdlp['version'] = check_ytdlp_version() or ytdlp['version']
        if sys.base_prefix != sys.prefix:
            base_python = os.path.join(sys.base_prefix, 'python.exe')
            base_check = subprocess.run(
                [base_python, '-c', 'import yt_dlp'], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=5
            )
            base_install = base_check.returncode == 0
    except (ImportError, OSError, ValueError):
        pass
    ytdlp['system_install'] = (base_install or bool(shutil.which('yt-dlp'))) and not ytdlp['import_available']
    ffmpeg = _run_version_command('ffmpeg', ['-version'])
    if ffmpeg['version']:
        ffmpeg['version'] = ffmpeg['version'].replace('ffmpeg version ', '', 1)
    ytdlp['latest_version'] = get_latest_ytdlp_version() if include_latest else None
    ffmpeg['latest_version'] = None
    if ytdlp['import_available'] and _parse_version(ytdlp['version']) < _parse_version(ytdlp['minimum_version']):
        ytdlp['status'] = 'Attention'
        ytdlp['status_detail'] = 'Version trop ancienne'
    elif ytdlp['import_available']:
        ytdlp['status'] = 'OK'
        ytdlp['status_detail'] = 'Disponible dans le venv'
    elif ytdlp['system_install']:
        ytdlp['status'] = 'Manquant'
        ytdlp['status_detail'] = 'Installé sur le système, absent du venv'
    else:
        ytdlp['status'] = 'Manquant'
        ytdlp['status_detail'] = 'Non installé'
    if ytdlp['status'] == 'OK' and ytdlp['version'] and ytdlp['latest_version']:
        current_version = _normalize_version(ytdlp['version'])
        latest_version = _normalize_version(ytdlp['latest_version'])
        if _parse_version(current_version) < _parse_version(latest_version):
            ytdlp['status'] = 'Attention'
            ytdlp['status_detail'] = 'Mise à jour disponible'
    ffmpeg['status'] = 'OK' if ffmpeg['version'] else 'Manquant'
    ffmpeg['status_detail'] = 'Disponible dans le PATH' if ffmpeg['version'] else 'Absent ou introuvable dans le PATH'
    return {
        'os': platform.platform(),
        'yt-dlp': ytdlp,
        'ffmpeg': ffmpeg,
    }


def update_ytdlp(timeout=120):
    """Update yt-dlp in the Python environment running this application."""
    try:
        proc = subprocess.run([sys.executable, '-m', 'pip', 'install', '-U', 'yt-dlp'],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=timeout)
        return {'success': proc.returncode == 0, 'output': proc.stdout.strip()}
    except (OSError, subprocess.SubprocessError) as error:
        return {'success': False, 'output': str(error)}
