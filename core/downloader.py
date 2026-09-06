import os
import random
import threading
import time
import traceback

from utils.helpers import format_duration

class Downloader:
    """
    Handles yt-dlp operations: extract metadata, queueing, downloading, progress hooks,
    pause/cancel handling, slow mode delays, and retry logic.
    """

    def __init__(self, log_callback=None, progress_callback=None, overall_progress_callback=None,
                 finished_callback=None, queue_update_callback=None, session_update_callback=None):
        """
        log_callback(message: str)
        progress_callback(percent: float, speed: str, eta: str, filename: str)
        overall_progress_callback(completed: int, total: int)
        finished_callback() called when worker finishes
        """
        self.task_queue = []
        self._queue_lock = threading.Lock()
        self._queue_sequence = 0
        self._current_item = None
        self._completed_items = []
        self._waiting_until = None
        self._slow_mode = True
        self._speed_mode = "Doux"
        self._custom_settings = {
            'minimum_delay': 60.0,
            'duration_multiplier': 1.2,
            'random_min': 30.0,
            'random_max': 180.0,
        }
        self.pause_event = threading.Event()  # when set => paused
        self.cancel_event = threading.Event()  # when set => cancel
        self.worker_thread = None
        self._thread_lock = threading.Lock()
        # callbacks set by GUI (should be thread-safe wrappers using root.after)
        self.log = log_callback or (lambda *args, **kwargs: None)
        self.progress = progress_callback or (lambda *args, **kwargs: None)
        self.overall_progress = overall_progress_callback or (lambda *args, **kwargs: None)
        self.finished = finished_callback or (lambda *args, **kwargs: None)
        self.queue_update = queue_update_callback or (lambda *args, **kwargs: None)
        self.session_update = session_update_callback or (lambda *args, **kwargs: None)
        # FIXED: throttle logging state for progress hook to avoid flooding the GUI log
        self._last_log_time = 0.0
        self._last_logged_percent = -1
        self._estimate_cache_key = None
        self._estimate_cache = ()

    @staticmethod
    def _safe_filename(value):
        try:
            from yt_dlp.utils import sanitize_filename
            value = sanitize_filename(str(value or 'unknown'), restricted=False)
        except (ImportError, TypeError, ValueError):
            value = str(value or 'unknown')
        return value.replace('/', '_').replace('\\', '_') or 'unknown'

    @staticmethod
    def _format_extension(format_choice, source_extension):
        if format_choice.startswith("MP3"):
            return 'mp3'
        if format_choice in ("M4A", "FLAC", "OPUS"):
            return format_choice.lower()
        return source_extension

    def log_msg(self, msg):
        try:
            self.log(msg)
        except Exception:
            pass

    def load_info(self, url):
        """Extract video/playlist metadata without downloading. Returns list of info dicts."""
        try:
            import yt_dlp as ytdl_module
        except ImportError as error:
            raise RuntimeError("yt-dlp n’est pas disponible dans l’environnement Python actuel. Utilisez: pip install -U yt-dlp") from error
        ydl_opts = {
            'ignoreerrors': True,
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
            'skip_download': True,
        }
        infos = []
        self.log_msg(f"Fetching info for URL: {url}")
        try:
            with ytdl_module.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return []
                if info.get('entries'):
                    for entry in info['entries']:
                        if not entry:
                            continue
                        infos.append(entry)
                else:
                    infos.append(info)
            self.log_msg(f"Fetched {len(infos)} items.")
        except Exception as e:
            tb = traceback.format_exc()
            self.log_msg(f"Error extracting info: {e}\n{tb}")
            raise
        return infos

    def check_availability(self, info):
        """Check one entry with full extraction and store its availability status."""
        try:
            import yt_dlp as ytdl_module
        except ImportError as error:
            raise RuntimeError("yt-dlp n’est pas disponible dans l’environnement Python actuel. Utilisez: pip install -U yt-dlp") from error
        url = info.get('webpage_url') or info.get('url')
        if not url:
            info['_availability'] = 'unknown'
            return 'unknown'
        opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'ignoreerrors': False,
        }
        try:
            with ytdl_module.YoutubeDL(opts) as ydl:
                extracted = ydl.extract_info(url, download=False)
            status = 'ok' if extracted else 'unknown'
        except Exception as error:
            status = self._classify_availability_error(error)
            info['_availability_error'] = str(error)
        info['_availability'] = status
        return status

    def check_availability_batch(self, info_list, progress_callback=None):
        """Check entries sequentially and report progress from the worker thread."""
        counts = {}
        total = len(info_list)
        for index, info in enumerate(info_list, start=1):
            status = self.check_availability(info)
            counts[status] = counts.get(status, 0) + 1
            if progress_callback:
                progress_callback(index, total, status, info)
        return counts

    @staticmethod
    def _classify_availability_error(error):
        message = str(error).lower()
        if 'private' in message:
            return 'private'
        if 'sign in' in message or 'login' in message or 'authentication' in message:
            return 'login_required'
        if 'age' in message or 'confirm your age' in message:
            return 'age_restricted'
        if 'deleted' in message or 'removed' in message or 'unavailable' in message:
            return 'deleted'
        return 'error'

    def get_expected_filepath(self, info, output_dir, format_choice):
        """Resolve the output path without constructing a YoutubeDL instance."""
        output_dir = os.path.abspath(os.path.expanduser(output_dir))
        title = self._safe_filename(info.get('title', 'unknown'))
        video_id = self._safe_filename(info.get('id', ''))
        source_extension = str(info.get('ext') or 'webm').lstrip('.')
        extension = self._format_extension(format_choice, source_extension)
        filename = f"{title} [{video_id}].{extension}" if video_id else f"{title}.{extension}"
        return os.path.join(output_dir, filename)

    def mark_existing(self, info_list, output_dir, format_choice, skip_existing=True,
                      progress_callback=None):
        """Annotate entries with their expected path and already-present status."""
        output_dir = os.path.abspath(os.path.expanduser(output_dir))
        existing_names = set()
        try:
            if os.path.isdir(output_dir):
                existing_names = {
                    entry.name for entry in os.scandir(output_dir) if entry.is_file()
                }
        except OSError:
            existing_names = set()
        existing = 0
        total = len(info_list)
        for index, info in enumerate(info_list, start=1):
            path = info.get('_expected_filepath')
            path_context = info.get('_expected_filepath_context')
            context = (output_dir, format_choice, info.get('title'), info.get('id'), info.get('ext'))
            if not path or path_context != context:
                path = self.get_expected_filepath(info, output_dir, format_choice)
            info['_expected_filepath'] = path
            info['_expected_filepath_context'] = context
            info['_already_present'] = bool(skip_existing and os.path.basename(path) in existing_names)
            if info['_already_present']:
                existing += 1
            if progress_callback:
                progress_callback(index, total, info)
        return existing

    def enqueue(self, info_list, output_dir=None, format_choice=None, skip_existing=False):
        """Fill the task queue with info dicts (shallow copy)."""
        with self._queue_lock:
            for info in info_list:
                self._queue_sequence += 1
                status = 'Déjà présent' if info.get('_already_present') and skip_existing else 'Pending'
                self.task_queue.append({
                    'id': self._queue_sequence,
                    'info': info,
                    'status': status,
                })
                if status == 'Déjà présent':
                    self.log_msg(f"Déjà présent, ignoré : {info.get('title', 'inconnu')}")
        self._notify_queue_update()
        self._notify_session_update()

    def clear_queue(self):
        """Empty internal queue and return the removed item records."""
        with self._queue_lock:
            removed = list(self.task_queue)
            self.task_queue.clear()
        self._notify_queue_update()
        self._notify_session_update()
        return removed

    def remove_queue_items(self, item_ids):
        """Remove pending items by id and return the removed item records."""
        item_ids = set(item_ids)
        with self._queue_lock:
            removed = [item for item in self.task_queue if item['id'] in item_ids]
            self.task_queue[:] = [item for item in self.task_queue if item['id'] not in item_ids]
        self._notify_queue_update()
        self._notify_session_update()
        return removed

    def move_queue_item(self, item_id, offset):
        """Move one pending item by offset and return whether it moved."""
        moved = False
        with self._queue_lock:
            index = next((index for index, item in enumerate(self.task_queue)
                          if item['id'] == item_id), None)
            if index is not None:
                target = index + offset
                if 0 <= target < len(self.task_queue):
                    self.task_queue[index], self.task_queue[target] = (
                        self.task_queue[target], self.task_queue[index])
                    moved = True
        if moved:
            self._notify_queue_update()
            self._notify_session_update()
        return moved

    def has_pending_items(self):
        with self._queue_lock:
            return bool(self.task_queue)

    def set_speed_mode(self, speed_mode, custom_settings=None):
        """Set the speed configuration used by delay calculations and estimates."""
        if isinstance(speed_mode, bool):
            speed_mode = "Doux" if speed_mode else "Turbo"
        self._speed_mode = speed_mode
        if custom_settings is not None:
            self._custom_settings = dict(custom_settings)
        self._slow_mode = speed_mode != "Turbo"
        self._notify_queue_update()

    def get_session_state(self, output_dir, format_choice, slow_mode=None, speed_mode=None,
                          custom_settings=None):
        """Return JSON-serializable session state without changing queue state."""
        if speed_mode is None:
            speed_mode = "Doux" if slow_mode else "Turbo"
        if custom_settings is None:
            custom_settings = self._custom_settings
        with self._queue_lock:
            remaining = []
            if self._current_item:
                current = dict(self._current_item)
                current['status'] = 'Pending'
                remaining.append(current)
            remaining.extend(dict(item, status='Pending') for item in self.task_queue)
            completed = [dict(item) for item in self._completed_items]
        return {
            'version': 1,
            'output_dir': output_dir,
            'format_choice': format_choice,
            'slow_mode': speed_mode != "Turbo",
            'speed_mode': speed_mode,
            'custom_settings': dict(custom_settings),
            'completed': completed,
            'queue': remaining,
        }

    def restore_session(self, queue_items, completed_items):
        """Restore pending and completed records from a saved session."""
        with self._queue_lock:
            self.task_queue.clear()
            self._completed_items.clear()
            self._current_item = None
            all_items = list(queue_items) + list(completed_items)
            valid_ids = [int(item['id']) for item in all_items if str(item.get('id', '')).isdigit()]
            self._queue_sequence = max(valid_ids, default=0)
            for source in queue_items:
                item = dict(source)
                if not str(item.get('id', '')).isdigit():
                    self._queue_sequence += 1
                    item['id'] = self._queue_sequence
                item['status'] = 'Pending'
                item['info'] = dict(item.get('info') or item)
                self.task_queue.append(item)
            for source in completed_items:
                item = dict(source)
                if not str(item.get('id', '')).isdigit():
                    self._queue_sequence += 1
                    item['id'] = self._queue_sequence
                item['status'] = item.get('status') or 'Done'
                item['info'] = dict(item.get('info') or item)
                self._completed_items.append(item)
        self._notify_queue_update()
        self._notify_session_update()

    def get_queue_snapshot(self):
        """Return a thread-safe view of current, pending, and finished items."""
        now = time.monotonic()
        with self._queue_lock:
            items = []
            if self._current_item:
                current = dict(self._current_item)
                current['position'] = ''
                current['wait_seconds'] = max(0, self._waiting_until - now) if self._waiting_until else 0
                items.append(current)

            pending_wait = max(0, self._waiting_until - now) if self._waiting_until else 0
            estimate_key = (
                self._speed_mode,
                tuple(sorted(self._custom_settings.items())),
                tuple((item['id'], item['info'].get('duration') or 0) for item in self.task_queue),
            )
            if estimate_key != self._estimate_cache_key:
                self._estimate_cache = tuple(
                    self.estimate_delay(item['info'].get('duration') or 0,
                                        self._speed_mode, self._custom_settings)
                    for item in self.task_queue
                )
                self._estimate_cache_key = estimate_key
            for position, (item, delay) in enumerate(zip(self.task_queue, self._estimate_cache), start=1):
                pending = dict(item)
                pending['position'] = position
                pending['wait_seconds'] = pending_wait
                items.append(pending)
                pending_wait += delay

            items.extend(dict(item, position='', wait_seconds=0) for item in self._completed_items)
            return items

    def _notify_queue_update(self):
        try:
            self.queue_update(self.get_queue_snapshot())
        except Exception:
            pass

    def _notify_session_update(self):
        try:
            self.session_update()
        except Exception:
            pass

    def _set_current_status(self, status):
        with self._queue_lock:
            if self._current_item:
                self._current_item['status'] = status
        self._notify_queue_update()

    def _finish_current(self, status):
        with self._queue_lock:
            if self._current_item:
                self._current_item['status'] = status
                self._completed_items.append(self._current_item)
                self._current_item = None
                self._waiting_until = None
        self._notify_queue_update()
        self._notify_session_update()

    def _make_ydl_opts(self, output_dir, format_choice):
        """Return yt-dlp options dict for audio extraction based on format_choice."""
        outtmpl = os.path.join(output_dir, "%(title)s [%(id)s].%(ext)s")
        opts = {
            'format': 'bestaudio/best',
            'outtmpl': outtmpl,
            'output_na_placeholder': '',
            'restrictfilenames': False,
            'windowsfilenames': True,
            'noplaylist': True,
            'ignoreerrors': True,
            'no_warnings': True,
            'continuedl': True,
            'quiet': True,
            'progress_hooks': [self._progress_hook],
            'postprocessors': [],
            'retries': 3,
        }

        if format_choice == "Best (original)":
            opts['postprocessors'] = []
        elif format_choice.startswith("MP3"):
            quality_map = {
                "MP3 320kbps": '0',
                "MP3 256kbps": '3',
            }
            prefq = quality_map.get(format_choice, '0')
            opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': prefq,
            }]
        elif format_choice == "M4A":
            opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'm4a',
            }]
        elif format_choice == "FLAC":
            opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'flac',
            }]
        elif format_choice == "OPUS":
            opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'opus',
            }]
        else:
            opts['postprocessors'] = []

        return opts

    def calculate_delay(self, duration, mode, custom_settings=None):
        """Calculate the actual delay before the next download."""
        duration = max(0.0, float(duration or 0))
        custom_settings = custom_settings or self._custom_settings
        if mode == "Turbo":
            return 0.0
        if mode == "Normal":
            return random.uniform(20.0, 45.0)
        if mode == "Très doux":
            return max(120.0, duration * 2.0 + random.uniform(60.0, 300.0))
        if mode == "Personnalisé":
            minimum = max(0.0, float(custom_settings.get('minimum_delay', 60.0)))
            multiplier = max(0.0, float(custom_settings.get('duration_multiplier', 1.2)))
            random_min = float(custom_settings.get('random_min', 30.0))
            random_max = float(custom_settings.get('random_max', 180.0))
            random_min, random_max = min(random_min, random_max), max(random_min, random_max)
            return max(minimum, duration * multiplier + random.uniform(random_min, random_max))
        return max(60.0, duration * 1.2 + random.uniform(30.0, 180.0))

    def estimate_delay(self, duration, mode, custom_settings=None):
        """Estimate a delay using the midpoint of the configured random range."""
        custom_settings = custom_settings or self._custom_settings
        if mode == "Turbo":
            return 0.0
        if mode == "Normal":
            return 32.5
        if mode == "Très doux":
            return max(120.0, float(duration or 0) * 2.0 + 180.0)
        if mode == "Personnalisé":
            minimum = max(0.0, float(custom_settings.get('minimum_delay', 60.0)))
            multiplier = max(0.0, float(custom_settings.get('duration_multiplier', 1.2)))
            random_min = float(custom_settings.get('random_min', 30.0))
            random_max = float(custom_settings.get('random_max', 180.0))
            return max(minimum, float(duration or 0) * multiplier + (random_min + random_max) / 2.0)
        return max(60.0, float(duration or 0) * 1.2 + 105.0)

    def _progress_hook(self, d):
        try:
            status = d.get('status')
            if status == 'downloading':
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                downloaded = d.get('downloaded_bytes') or 0
                percent = (downloaded / total * 100.0) if total else 0.0
                speed = d.get('speed') or 0
                eta = d.get('eta')
                filename = d.get('filename') or d.get('tmpfilename') or d.get('info_dict', {}).get('title', '')
                speed_str = f"{speed/1024/1024:.2f} MB/s" if speed else "--"
                eta_str = f"{int(eta)}s" if eta else "--"
                with self._queue_lock:
                    if self._current_item:
                        self._current_item['progress'] = percent
                self.progress(percent, speed_str, eta_str, filename)

                now = time.time()
                percent_change = abs(percent - (self._last_logged_percent or 0))
                if (self._last_logged_percent < 0) or (percent_change >= 10.0) or (now - self._last_log_time >= 5.0):
                    self._last_log_time = now
                    self._last_logged_percent = int(percent // 1)
                    self.log_msg(f"{filename} - {percent:.1f}% - {speed_str} - ETA {eta_str}")
            elif status == 'finished':
                filename = d.get('filename') or d.get('info_dict', {}).get('title', '')
                self.log_msg(f"Finished downloading: {filename}")
                self.progress(100.0, "--", "--", filename)
        except Exception:
            self.log_msg("Exception in progress hook:\n" + traceback.format_exc())

    def start_worker(self, output_dir, format_choice, speed_mode="Doux", custom_settings=None,
                     slow_mode=None, skip_existing=True, force_download=False):
        """Start the download worker thread if not already running."""
        with self._thread_lock:
            if self.worker_thread and self.worker_thread.is_alive():
                self.log_msg("Worker already running.")
                return False
            self.pause_event.clear()
            self.cancel_event.clear()
            if slow_mode is not None:
                speed_mode = "Doux" if slow_mode else "Turbo"
            if isinstance(speed_mode, bool):
                speed_mode = "Doux" if speed_mode else "Turbo"
            self._speed_mode = speed_mode
            self._custom_settings = dict(custom_settings or self._custom_settings)
            self._slow_mode = speed_mode != "Turbo"
            self._waiting_until = None
            self.worker_thread = threading.Thread(
                target=self._worker,
                args=(output_dir, format_choice, speed_mode, self._custom_settings,
                      skip_existing, force_download),
                daemon=True
            )
            self.worker_thread.start()
            return True

    def pause(self):
        """Set pause; worker should check and pause soon."""
        self.pause_event.set()
        self.log_msg("Paused.")

    def resume(self):
        """Clear pause; worker will continue."""
        self.pause_event.clear()
        self.log_msg("Resumed.")

    def stop(self):
        """Signal cancel; worker should break out and finish."""
        self.cancel_event.set()
        self.log_msg("Stop requested.")

    def _worker(self, output_dir, format_choice, speed_mode="Doux", custom_settings=None,
                skip_existing=True, force_download=False):
        """Main worker loop consuming the task_queue and downloading sequentially."""
        try:
            import yt_dlp as ytdl_module
        except ImportError as error:
            self.log_msg("yt-dlp est absent de l’environnement Python actuel: " + str(error))
            self.finished()
            return
        if isinstance(speed_mode, bool):
            speed_mode = "Doux" if speed_mode else "Turbo"
        with self._queue_lock:
            total = len(self.task_queue)
        completed = 0
        self.log_msg(f"Starting download worker: {total} items queued.")
        while not self.cancel_event.is_set():
            while self.pause_event.is_set() and not self.cancel_event.is_set():
                time.sleep(0.2)
            with self._queue_lock:
                if not self.task_queue:
                    break
                item = self.task_queue.pop(0)
                item['status'] = 'Downloading'
                self._current_item = item
            self._notify_queue_update()
            info = item['info']
            self.overall_progress(completed, total)
            expected_path = info.get('_expected_filepath') or self.get_expected_filepath(
                info, output_dir, format_choice)
            info['_expected_filepath'] = expected_path
            if skip_existing and not force_download and os.path.isfile(expected_path):
                self.log_msg(f"Déjà présent, téléchargement ignoré : {info.get('title', 'inconnu')}")
                completed += 1
                self.overall_progress(completed, total)
                self._finish_current('Déjà présent')
                continue
            url = info.get('webpage_url') or info.get('url')
            if not url:
                self.log_msg(f"Skipping item with no URL: {info.get('title', 'unknown')}")
                completed += 1
                self._finish_current('Skipped')
                continue

            title = info.get('title', 'unknown')
            duration = info.get('duration') or 0
            self.log_msg(f"Queueing download: {title} ({format_duration(duration)})")

            ydl_opts = self._make_ydl_opts(output_dir, format_choice)
            attempts = 0
            max_attempts = 3
            backoff_times = [60, 120, 240]

            while attempts < max_attempts and not self.cancel_event.is_set():
                attempts += 1
                try:
                    self.log_msg(f"Downloading ({attempts}/{max_attempts}): {title}")
                    with ytdl_module.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([url])
                    completed += 1
                    self.overall_progress(completed, total)
                    delay = self.calculate_delay(duration, speed_mode, custom_settings)
                    if delay > 0 and not self.cancel_event.is_set():
                        self._set_current_status('Waiting')
                        self.log_msg(f"Speed mode {speed_mode}: sleeping for {int(delay)}s before next download.")
                        slept = 0.0
                        while slept < delay and not self.cancel_event.is_set():
                            with self._queue_lock:
                                self._waiting_until = time.monotonic() + max(0, delay - slept)
                            self._notify_queue_update()
                            if self.pause_event.is_set():
                                time.sleep(0.5)
                                continue
                            time.sleep(1.0)
                            slept += 1.0
                        self._finish_current('Done')
                    else:
                        self._finish_current('Done')
                    break
                except Exception as e:
                    msg = str(e)
                    tb = traceback.format_exc()
                    transient = False
                    if "429" in msg or "HTTP Error 429" in msg or "Too Many Requests" in msg or "403" in msg:
                        transient = True
                    self.log_msg(f"Error downloading {title}: {msg}\n{tb}")
                    if transient and attempts < max_attempts:
                        self._set_current_status('Waiting')
                        wait = backoff_times[min(attempts-1, len(backoff_times)-1)]
                        self.log_msg(f"Transient error detected. Waiting {wait}s before retrying...")
                        slept = 0
                        while slept < wait and not self.cancel_event.is_set():
                            with self._queue_lock:
                                self._waiting_until = time.monotonic() + max(0, wait - slept)
                            self._notify_queue_update()
                            if self.pause_event.is_set():
                                time.sleep(0.5)
                                continue
                            time.sleep(1.0)
                            slept += 1.0
                        continue
                    else:
                        self.log_msg(f"Skipping {title} after {attempts} attempts.")
                        completed += 1
                        self.overall_progress(completed, total)
                        self._finish_current('Failed')
                        break
        self.log_msg("Worker finished.")
        try:
            self.finished()
        except Exception:
            pass
